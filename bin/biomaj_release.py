import os
import logging
import datetime
import time
import threading
import redis

import consul
from flask import Flask
from flask import jsonify
from flask import request
from influxdb import InfluxDBClient
import requests
import yaml

from prometheus_client import Gauge
from prometheus_client.exposition import generate_latest

from biomaj_core.utils import Utils
from biomaj.bank import Bank
from biomaj_core.config import BiomajConfig

config_file = 'config.yml'
if 'BIOMAJ_CONFIG' in os.environ:
        config_file = os.environ['BIOMAJ_CONFIG']


app = Flask(__name__)


app.config['biomaj_release_metric'] = Gauge("biomaj_release", "Bank remote release updates", ['bank'])


@app.route('/api/release-daemon')
def ping():
    return jsonify({'msg': 'pong'})


@app.route('/metrics', methods=['GET'])
def metrics():
    return generate_latest()


@app.route('/api/release/metrics', methods=['POST'])
def add_metrics():
    '''
    Expects a JSON request with an array of {'bank': 'bank_name'}
    '''
    procs = request.get_json()
    for proc in procs:
        app.config['biomaj_release_metric'].labels(proc['bank']).inc()
    return jsonify({'msg': 'OK'})

def start_web(config):
    app.run(host='0.0.0.0', port=config['web']['port'])


def consul_declare(config):
    if config['consul']['host']:
        consul_agent = consul.Consul(host=config['consul']['host'])
        consul_agent.agent.service.register(
            'biomaj-release-daemon',
            service_id=config['consul']['id'],
            address=config['web']['hostname'],
            port=config['web']['port'],
            tags=['biomaj']
        )
        check = consul.Check.http(
            url='http://' + config['web']['hostname'] + ':' + str(config['web']['port']) + '/api/release-daemon',
            interval=20
        )
        consul_agent.agent.check.register(
            config['consul']['id'] + '_check',
            check=check,
            service_id=config['consul']['id']
        )


class ReleaseService(object):

    def __init__(self, config_file):
        self.logger = logging
        self.session = None
        with open(config_file, 'r') as ymlfile:
            self.config = yaml.load(ymlfile)
            Utils.service_config_override(self.config)

        consul_declare(self.config)

        BiomajConfig.load_config(self.config['biomaj']['config'])

        if self.config['consul']['host']:
            web_thread = threading.Thread(target=start_web, args=(self.config,))
            web_thread.start()
        if 'log_config' in self.config:
            for handler in list(self.config['log_config']['handlers'].keys()):
                self.config['log_config']['handlers'][handler] = dict(self.config['log_config']['handlers'][handler])
            logging.config.dictConfig(self.config['log_config'])
            self.logger = logging.getLogger('biomaj')

        self.redis_client = redis.StrictRedis(
            host=self.config['redis']['host'],
            port=self.config['redis']['port'],
            db=self.config['redis']['db'],
            decode_responses=True
        )

        self.logger.info('Release service started')

    def get_next_check_in(self, check_in, attempts, min_delay=1):
        if check_in < min_delay:
            return min_delay

        if check_in == 1:
            if attempts < 3:
                return check_in
            else:
                return 7
        if check_in == 7:
            if attempts < 3:
                return check_in
            else:
                return 14
        if check_in == 14:
            if attempts < 3:
                return check_in
            else:
                return 30
        if check_in == 30:
            if attempts < 3:
                return check_in
            else:
                return 90
        return 90

    def get_previous_check_in(self, check_in, min_delay=1):
        if check_in == 1:
            new_check_in = check_in
        elif check_in <= 7:
            new_check_in = check_in - 1
        elif check_in <= 14:
            new_check_in = check_in - 7
        elif check_in <= 30:
            new_check_in = check_in - 14
        elif check_in <= 90:
            new_check_in = check_in - 30
        else:
            new_check_in = 90
        if new_check_in <= min_delay:
            return min_delay
        return new_check_in

    def check(self):
        self.logger.info('Check for banks releases')
        current = datetime.datetime.now()
        next_run = current
        while True:
            banks = Bank.list()
            influxdb = None
            if not banks:
                time.sleep(3600)
                continue

            bank = Bank(banks[0]['name'], no_log=True)
            db_host = bank.config.get('influxdb.host', default=None)
            if db_host:
                db_port = int(bank.config.get('influxdb.port', default='8086'))
                db_user = bank.config.get('influxdb.user', default=None)
                db_password = bank.config.get('influxdb.password', default=None)
                db_name = bank.config.get('influxdb.db', default='biomaj')
                if db_user and db_password:
                    influxdb = InfluxDBClient(host=db_host, port=db_port, username=db_user, password=db_password, database=db_name)
                else:
                    influxdb = InfluxDBClient(host=db_host, port=db_port, database=db_name)

            for one_bank in banks:
                bank_name = one_bank['name']
                new_bank_available = False
                try:
                    bank = Bank(bank_name, no_log=True)
                    # in days
                    min_delay = int(bank.config.get('schedule.delay', default=0)) * 3600 * 24
                    if not bank.config.get_bool('schedule.auto', default=True):
                        self.logger.info('Skip bank %s per configuration' % (bank_name))
                        continue
                    # Get mean workflow update duration
                    if influxdb:
                        res = influxdb.query('select mean("value") from "biomaj.workflow.duration" where "bank" =~ /^%s$/' % (bank_name))
                        if res:
                            for r in res:
                                min_delay = max(min_delay, int(r[0]['mean'] / (3600 * 24)))
                                self.logger.debug('Minimum delay based on mean workflow duration and config: %d' % (min_delay))

                    prev_release = self.redis_client.get(self.config['redis']['prefix'] + ':release:last:' + bank.name)
                    cur_check_time = datetime.datetime.now()
                    cur_check_timestamp = time.mktime(cur_check_time.timetuple())
                    last_check_timestamp = self.redis_client.get(self.config['redis']['prefix'] + ':release:last_check:' + bank.name)
                    planned_check_in = self.redis_client.get(self.config['redis']['prefix'] + ':release:check_in:' + bank.name)
                    if not planned_check_in:
                        planned_check_in = 0

                    attempts = self.redis_client.get(self.config['redis']['prefix'] + ':release:attempts:' + bank.name)
                    if not attempts:
                        attempts = 0
                    attempts = int(attempts)

                    if last_check_timestamp is not None and cur_check_timestamp < int(last_check_timestamp) + (int(planned_check_in) * 3600 * 24) and cur_check_timestamp < (int(last_check_timestamp) + (min_delay * 3600 * 24)):
                        # Date for next planned check not reached, continue to next bank
                        self.logger.debug('plan trigger not reached, skipping: %s' % (str(datetime.datetime.fromtimestamp(int(last_check_timestamp) + (int(planned_check_in) * 3600 * 24)))))
                        continue
                    (res, remoterelease) = bank.check_remote_release()
                    if res and remoterelease:
                        if not prev_release or prev_release != remoterelease:
                            self.logger.info('New %s remote release: %s' % (bank.name, str(remoterelease)))
                            # Send metric
                            try:
                                metrics = [{'bank': bank.name, 'release': remoterelease}]
                                requests.post('http://localhost:' + str(self.config['web']['port']) + '/api/release/metrics', json=metrics)
                                if influxdb:
                                    influx_metric = {
                                         "measurement": 'biomaj.release.new',
                                             "fields": {
                                                 "value": 1
                                                 },
                                            "tags": {
                                                "bank": bank_name
                                            }
                                    }
                                    metrics = [influx_metric]

                                    influxdb.write_points(metrics, time_precision="s")
                            except Exception as e:
                                logging.error('Failed to post metrics: ' + str(e))
                            new_bank_available = True
                            self.redis_client.set(self.config['redis']['prefix'] + ':release:last:' + bank.name, remoterelease)
                            self.redis_client.set(self.config['redis']['prefix'] + ':release:attempts:' + bank.name, 0)
                            # TODO send bank update request
                            self.logger.warn('TODO: Should send an update request')
                        else:
                            self.logger.debug('Same %s release' % (bank.name))

                    check_in = self.redis_client.get(self.config['redis']['prefix'] + ':release:check_in:' + bank.name)
                    if not check_in:
                        check_in = 1
                    check_in = int(check_in)

                    if not new_bank_available:
                        next_check_in = self.get_next_check_in(check_in, attempts + 1, min_delay=min_delay)
                        self.redis_client.incr(self.config['redis']['prefix'] + ':release:attempts:' + bank.name)
                    else:
                        next_check_in = check_in
                        if attempts == 0:
                            # Got a match on first attempt, try to reduce duration
                            next_check_in = self.get_previous_check_in(check_in, min_delay=min_delay)
                        else:
                            next_check_in = check_in * attempts
                    self.redis_client.set(self.config['redis']['prefix'] + ':release:check_in:' + bank.name, next_check_in)
                    self.redis_client.set(self.config['redis']['prefix'] + ':release:last_check:' + bank.name, int(cur_check_timestamp))
                    self.logger.debug('Next check in: %d days' % (next_check_in))
                except Exception as e:
                    self.logger.exception(e)
                    self.logger.error('Failed to get remote release for %s: %s' % (bank_name, str(e)))
            next_run = current + datetime.timedelta(days=1)
            while current < next_run:
                time.sleep(3600)
                current = datetime.datetime.now()
                self.logger.info('Next run: ' + str(next_run))
        return


process = ReleaseService(config_file)
process.check()
