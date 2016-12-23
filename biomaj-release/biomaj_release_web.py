import ssl
import os
import yaml

from flask import Flask
from flask import jsonify
from flask import request

from prometheus_client import Gauge
from prometheus_client.exposition import generate_latest

import consul

from biomaj_core.config import BiomajConfig
from biomaj_core.utils import Utils


config_file = 'config.yml'
if 'BIOMAJ_CONFIG' in os.environ:
        config_file = os.environ['BIOMAJ_CONFIG']

config = None
with open(config_file, 'r') as ymlfile:
    config = yaml.load(ymlfile)
    Utils.service_config_override(config)

BiomajConfig.load_config(config['biomaj']['config'])


app = Flask(__name__)

biomaj_release_metric = Gauge("biomaj_release", "Bank remote release updates", ['bank', 'release'])


def consul_declare(config):
    if config['consul']['host']:
        consul_agent = consul.Consul(host=config['consul']['host'])
        consul_agent.agent.service.register('biomaj-release', service_id=config['consul']['id'], address=config['web']['hostname'], port=config['web']['port'], tags=['biomaj'])
        check = consul.Check.http(url='http://' + config['web']['hostname'] + ':' + str(config['web']['port']) + '/api/release', interval=20)
        consul_agent.agent.check.register(config['consul']['id'] + '_check', check=check, service_id=config['consul']['id'])


consul_declare(config)


@app.route('/api/release', methods=['GET'])
def ping():
    return jsonify({'msg': 'pong'})


@app.route('/metrics', methods=['GET'])
def metrics():
    return generate_latest()


@app.route('/api/release/metrics', methods=['POST'])
def add_metrics():
    '''
    Expects a JSON request with an array of {'bank': 'bank_name', 'release': '123'}
    '''
    procs = request.get_json()
    for proc in procs:
        if 'release' in proc and proc['release']:
            biomaj_release_metric.labels(proc['bank'], proc['release']).inc()
    return jsonify({'msg': 'OK'})


if __name__ == "__main__":
    context = None
    if config['tls']['cert']:
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        context.load_cert_chain(config['tls']['cert'], config['tls']['key'])
    app.run(host='0.0.0.0', port=config['web']['port'], ssl_context=context, threaded=True, debug=config['web']['debug'])
