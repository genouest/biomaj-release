# About

Biomaj remote bank release watcher

in development

scan every day remote banks release modifications and send stats to prometheus with bank/release info

then goal is to create an intelligent cron mapping for banks according to their remote bank update average time and bank update duration


# Development

    flake8 --ignore E501 biomaj-release

# Prometheus metrics

Endpoint: /api/release/metrics


# Run

python bin/biomaj_release.py

## Web server

In bin directory:
export BIOMAJ_CONFIG=path_to_config.yml
gunicorn biomaj-release.biomaj_release_web:app

Web processes should be behind a proxy/load balancer, API base url /api/release
