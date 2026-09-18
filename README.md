# homeassistant_mariadb
Lightweight cron-based logger that reads Home Assistant entities tagged with a label via the REST API (no manual entity list needed) and writes their values to MariaDB for long-term storage, e.g. for Grafana dashboards. Single-pass script, no persistent state, safe to run every minute via cron.
