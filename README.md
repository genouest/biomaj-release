# About

Biomaj remote bank release watcher

Scan every day remote banks release modifications and send stats to prometheus and influxdb about new release detection.
When a new release is detected, according to minimal delay configuration (in global or bank properties), a new bank update is sent.

If influxdb is available and updated with bank updates, minimal delay will also be computed based on mean workflow duration. In any case, minimal duration is 1 day.

Properties in global.properties and/or your bank property file:

    schedule.delay=3 # minimal 3 days between checks
    schedule.auto=true # Default=true, if false, auto scan is disabled

Program will try to increase delay between updates when no new release is detected and to decrease it when a new release occurs.


# Requirements

redis

# Development

    flake8 --ignore E501 biomaj-release

# Prometheus metrics

Endpoint:

    /metrics
    /api/release/schedule/bank : schedule checks for all banks
    /api/release/schedule/bank/<bank> : schedule check for *bank*


# Run

python bin/biomaj_release.py
