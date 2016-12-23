import os
import logging
import datetime
import time
import threading
import redis

import consul
from flask import Flask
from flask import jsonify
import requests
import yaml

from biomaj_core.utils import Utils
from biomaj.bank import Bank
from biomaj_core.config import BiomajConfig

config_file = 'config.yml'
if 'BIOMAJ_CONFIG' in os.environ:
        config_file = os.environ['BIOMAJ_CONFIG']


app = Flask(__name__)


@app.route('/api/release-daemon')
def ping():
    return jsonify({'msg': 'pong'})


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

    def check(self):
        self.logger.info('Check for banks releases')
        current = datetime.datetime.now()
        next_run = current
        banks = Bank.list()
        while True:
            for one_bank in banks:
                bank_name = one_bank['name']
                try:
                    bank = Bank(bank_name, no_log=True)
                    prev_release = self.redis_client.get(self.config['redis']['prefix'] + ':release:last:' + bank.name)
                    (res, remoterelease) = bank.check_remote_release()
                    if res and remoterelease:
                        if not prev_release or prev_release != remoterelease:
                            self.logger.info('New %s remote release: %s' % (bank.name, str(remoterelease)))
                            # Send metric
                            try:
                                metrics = [{'bank': bank.name, 'release': remoterelease}]
                                requests.post(self.config['web']['local_endpoint'] + '/api/release/metrics', json=metrics)
                            except Exception as e:
                                logging.error('Failed to post metrics: ' + str(e))

                            self.redis_client.set(self.config['redis']['prefix'] + ':release:last:' + bank.name, remoterelease)
                        else:
                            self.logger.debug('Same %s release' % (bank.name))
                    self.logger.warn('Failed to get %s remote release' % (bank.name))

                except Exception as e:
                    self.logger.error('Failed to get remote release for %s: %s' % (bank_name, str(e)))
                # TODO check bank update average interval and bank update duration and update cron to the best
            next_run = current + datetime.timedelta(days=1)
            while current < next_run:
                time.sleep(3600)
                current = datetime.datetime.now()
                self.logger.info('Next run: ' + str(next_run))
        return


process = ReleaseService(config_file)
process.check()
