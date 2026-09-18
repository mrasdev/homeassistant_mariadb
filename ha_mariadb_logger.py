#!/usr/bin/env python3
"""
ha_mariadb_logger.py

Reads all Home Assistant entities tagged with a given label (default: "db")
and writes their current value to MariaDB on every run - unconditionally,
regardless of whether the value changed since the last write.

Entity discovery, name and unit are all read from Home Assistant itself via
the /api/template endpoint (label_entities() + state_attr()). Adding or
removing the "db" label on an entity in Home Assistant is enough to start/stop
logging it - the config.yaml only needs the HA connection, DB connection and
the label name.

Designed to be run via cron: the script performs a small, fixed number of
HTTP requests to HA (one to resolve the labelled entities + one to fetch all
states) and then exits. It keeps no state in the process itself and needs
none from the database either (no "what was the last value" lookup) since
every value is written every time. If the database or HA is briefly
unreachable, only that one run fails; the next cron run (a minute later)
simply tries again - no crash loop, no lost state.

Recommended cron entry (every minute, with a lock against overlapping runs):

    * * * * * /usr/bin/flock -n /tmp/ha_mariadb_logger.lock /usr/bin/python3 /opt/ha-mariadb-logger/ha_mariadb_logger.py --config /opt/ha-mariadb-logger/config.yaml >> /var/log/ha_mariadb_logger.log 2>&1

DB schema:
    entities        - one row per configured entity (metadata)
    entity_values   - one row per written measurement (time series)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import pymysql
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ha_mariadb_logger")

DEFAULT_LABEL = "db"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS entities (
        id INT AUTO_INCREMENT PRIMARY KEY,
        entity_id VARCHAR(255) NOT NULL UNIQUE,
        name VARCHAR(255) NULL,
        unit VARCHAR(50) NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS entity_values (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        entity_id INT NOT NULL,
        ts DATETIME NOT NULL,
        value_numeric DOUBLE NULL,
        value_text VARCHAR(255) NULL,
        CONSTRAINT fk_entity_values_entity
            FOREIGN KEY (entity_id) REFERENCES entities(id)
            ON DELETE CASCADE,
        INDEX idx_entity_ts (entity_id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
]

# Example query for Grafana (not executed here, just for reference):
#
# SELECT
#     v.ts AS "time",
#     v.value_numeric AS "value",
#     e.name AS "metric"
# FROM entity_values v
# JOIN entities e ON e.id = v.entity_id
# WHERE e.entity_id = 'sensor.wallbox_power'
#   AND $__timeFilter(v.ts)
# ORDER BY v.ts


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect_db(db_cfg: dict) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=db_cfg["host"],
        port=int(db_cfg.get("port", 3306)),
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )


def ensure_schema(conn: pymysql.connections.Connection) -> None:
    with conn.cursor() as cur:
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)


def fetch_labelled_entities(ha_cfg: dict, label: str) -> list:
    """
    Asks Home Assistant, via the /api/template endpoint, for every entity
    tagged with the given label. Returns a list of dicts with entity_id,
    name (the HA friendly_name) and unit (unit_of_measurement), fully
    replacing what used to be hand-maintained in config.yaml's entities:
    list.
    """
    url = f"{ha_cfg['url'].rstrip('/')}/api/template"
    headers = {
        "Authorization": f"Bearer {ha_cfg['token']}",
        "Content-Type": "application/json",
    }
    timeout = ha_cfg.get("timeout_seconds", 10)

    template = (
        "{% set result = namespace(items=[]) %}"
        "{% for eid in label_entities('" + label + "') %}"
        "{% set entry = {"
        "'entity_id': eid,"
        "'name': state_attr(eid, 'friendly_name') or eid,"
        "'unit': state_attr(eid, 'unit_of_measurement') or ''"
        "} %}"
        "{% set result.items = result.items + [entry] %}"
        "{% endfor %}"
        "{{ result.items | tojson }}"
    )

    resp = requests.post(url, headers=headers, json={"template": template}, timeout=timeout)
    resp.raise_for_status()

    # /api/template returns plain text - here that text is a JSON string.
    import json
    return json.loads(resp.text)


def sync_entities(conn: pymysql.connections.Connection, entities_cfg: list) -> dict:
    """
    Syncs the 'entities' table with the entities currently found via the HA
    label (label is authoritative). Returns a dict: entity_id (HA, str) -> db_id (int)
    """
    result = {}
    with conn.cursor() as cur:
        for ent in entities_cfg:
            entity_id = ent["entity_id"]
            name = ent.get("name")
            unit = ent.get("unit")

            cur.execute(
                """
                INSERT INTO entities (entity_id, name, unit, enabled)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE
                    name = VALUES(name),
                    unit = VALUES(unit),
                    enabled = 1
                """,
                (entity_id, name, unit),
            )
            cur.execute("SELECT id FROM entities WHERE entity_id = %s", (entity_id,))
            result[entity_id] = cur.fetchone()[0]

        # Disable entities that no longer carry the label (don't delete them,
        # so existing measurements in entity_values are preserved).
        configured_ids = [e["entity_id"] for e in entities_cfg]
        if configured_ids:
            fmt = ",".join(["%s"] * len(configured_ids))
            cur.execute(
                f"UPDATE entities SET enabled = 0 WHERE entity_id NOT IN ({fmt})",
                tuple(configured_ids),
            )
        else:
            cur.execute("UPDATE entities SET enabled = 0")

    return result


def fetch_all_ha_states(ha_cfg: dict) -> dict:
    """
    Fetches ALL states in a single request.
    Returns: entity_id -> raw state string (valid values only;
    'unavailable'/'unknown' are skipped).
    """
    url = f"{ha_cfg['url'].rstrip('/')}/api/states"
    headers = {
        "Authorization": f"Bearer {ha_cfg['token']}",
        "Content-Type": "application/json",
    }
    timeout = ha_cfg.get("timeout_seconds", 10)

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    states = {}
    for item in data:
        state = item.get("state")
        if state in ("unavailable", "unknown", None):
            continue
        states[item["entity_id"]] = state
    return states


def parse_numeric(raw_state: str):
    """Returns float(raw_state), or None if it can't be parsed."""
    try:
        return float(raw_state)
    except (TypeError, ValueError):
        return None


def insert_value(conn, db_entity_id: int, value_numeric, raw_value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entity_values (entity_id, ts, value_numeric, value_text) VALUES (%s, %s, %s, %s)",
            (db_entity_id, datetime.now(timezone.utc), value_numeric, raw_value),
        )


def run(config_path: str) -> None:
    cfg = load_config(config_path)
    ha_cfg = cfg["homeassistant"]
    db_cfg = cfg["database"]
    label = cfg.get("label", DEFAULT_LABEL)

    t0 = time.monotonic()
    conn = connect_db(db_cfg)
    log.debug("connect_db: %.2fs", time.monotonic() - t0)

    t = time.monotonic()
    ensure_schema(conn)
    log.debug("ensure_schema: %.2fs", time.monotonic() - t)

    t = time.monotonic()
    entities_cfg = fetch_labelled_entities(ha_cfg, label)
    log.debug("fetch_labelled_entities (label=%s, %d entities): %.2fs", label, len(entities_cfg), time.monotonic() - t)

    t = time.monotonic()
    entity_map = sync_entities(conn, entities_cfg)  # ha_entity_id -> db_id
    log.debug("sync_entities (%d entities): %.2fs", len(entity_map), time.monotonic() - t)

    t = time.monotonic()
    ha_states = fetch_all_ha_states(ha_cfg)
    log.debug("fetch_all_ha_states (%d states received): %.2fs", len(ha_states), time.monotonic() - t)

    t = time.monotonic()
    written, missing = 0, 0

    for entity_id, db_id in entity_map.items():
        raw_value = ha_states.get(entity_id)
        if raw_value is None:
            log.warning("No valid state received from HA for %s (missing/unavailable/unknown).", entity_id)
            missing += 1
            continue

        value_numeric = parse_numeric(raw_value)
        insert_value(conn, db_id, value_numeric, raw_value)
        log.debug("Written: %s = %s", entity_id, raw_value)
        written += 1

    log.debug("insert loop (%d writes): %.2fs", written, time.monotonic() - t)

    log.info(
        "Run finished: %d written, %d without a valid value (of %d entities, label=%s).",
        written, missing, len(entity_map), label,
    )
    log.debug("TOTAL run() time: %.2fs", time.monotonic() - t0)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Home Assistant -> MariaDB Logger (single pass, for cron)")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging, including per-phase timing (connect, sync, HA fetch, insert loop).",
    )
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        run(args.config)
    except FileNotFoundError:
        log.error("Config file not found: %s", args.config)
        sys.exit(1)
    except KeyError as e:
        log.error("Missing config key: %s", e)
        sys.exit(1)
    except pymysql.MySQLError as e:
        log.error("Database error: %s", e)
        sys.exit(1)
    except requests.RequestException as e:
        log.error("Error connecting to Home Assistant: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
