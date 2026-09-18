# Home Assistant to MariaDB Logger

A small, single-purpose cron script that logs Home Assistant sensor values
into a MariaDB database for long-term storage, e.g. to power Grafana
dashboards beyond Home Assistant's own recorder retention period.

Instead of maintaining a list of entities in a config file, the script asks
Home Assistant itself which entities carry a given label (default: `db`).
Name and unit are also read directly from Home Assistant. To start or stop
logging an entity, you simply add or remove the label on it in Home
Assistant - no code or config change required.

## How it works

On every run, the script:

1. Connects to MariaDB and makes sure the required tables exist.
2. Asks Home Assistant (via the `/api/template` REST endpoint) for every
   entity tagged with the configured label, together with its friendly
   name and unit of measurement.
3. Syncs that entity list into the `entities` table (inserts new ones,
   updates name/unit of existing ones, disables ones that lost the label -
   without deleting their historical data).
4. Fetches the current state of all entities in a single request to
   `/api/states`.
5. Writes one row per entity into `entity_values`, unconditionally -
   whether or not the value has changed since the last run.

The script is stateless between runs: it keeps nothing in memory or on
disk, and it does not look at previously stored values. This makes it
simple and robust - if the database or Home Assistant is briefly
unreachable, only that single run fails and the next cron run (a minute
later) just tries again.

Because every value is written on every run, the row count grows steadily
over time. This is a deliberate trade-off for simplicity; if you need to
store only value changes to reduce table size, you will need to add that
change-detection logic yourself.

## Requirements

- Python 3.8+
- A running Home Assistant instance with a long-lived access token
- A MariaDB (or MySQL-compatible) server and an existing database
- Python packages: `PyYAML`, `requests`, `PyMySQL`

Install the dependencies with:

```
pip install PyYAML requests PyMySQL
```

## Setup

### 1. Label the entities you want to log

In Home Assistant, go to Settings, then Entities, select the entities you
want logged, and assign them a label (for example `db`). The label name is
configurable, see below - it does not have to be called `db`.

### 2. Create a long-lived access token

In Home Assistant, go to your profile, scroll down to "Long-Lived Access
Tokens" and create one. You will need it for the configuration file.

### 3. Configure the script

Copy the example configuration and fill in your own values:

```
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

```yaml
homeassistant:
  url: "http://homeassistant.local:8123"
  token: "YOUR_HOME_ASSISTANT_LONG_LIVED_ACCESS_TOKEN"
  timeout_seconds: 10

label: "db"

database:
  host: "your-db-host.local"
  port: 3306
  user: "your-db-user"
  password: "your-db-password"
  database: "your-db-name"
```

`config.yaml` is listed in `.gitignore` and should never be committed,
since it contains your real credentials. Only `config.example.yaml`, with
placeholder values, is meant to be tracked in version control.

### 4. Run it once manually

```
python3 ha_mariadb_logger.py --config config.yaml --debug
```

The `--debug` flag prints per-phase timing and every value that gets
written, which is useful for verifying the setup before scheduling it.

### 5. Schedule it with cron

Run the script every minute, with a lock file to prevent overlapping runs
in case a single run takes longer than expected:

```
* * * * * /usr/bin/flock -n /tmp/ha_mariadb_logger.lock /usr/bin/python3 /path/to/ha_mariadb_logger.py --config /path/to/config.yaml >> /var/log/ha_mariadb_logger.log 2>&1
```

Adjust the paths to wherever you place the script and configuration file.

## Database schema

The script creates two tables automatically on first run:

```sql
CREATE TABLE entities (
    id INT AUTO_INCREMENT PRIMARY KEY,
    entity_id VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NULL,
    unit VARCHAR(50) NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE entity_values (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    entity_id INT NOT NULL,
    ts DATETIME NOT NULL,
    value_numeric DOUBLE NULL,
    value_text VARCHAR(255) NULL,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);
```

`entities` holds one row per logged entity, with its current name and
unit. `entity_values` holds one row per measurement. Because `name` and
`unit` live only in `entities` and are updated in place, a renamed entity
in Home Assistant will show its new name for its entire history the next
time it is queried - there is no per-value snapshot of the name.

## Example query for Grafana

```sql
SELECT
    v.ts AS "time",
    v.value_numeric AS "value",
    e.name AS "metric"
FROM entity_values v
JOIN entities e ON e.id = v.entity_id
WHERE e.entity_id = 'sensor.example'
  AND $__timeFilter(v.ts)
ORDER BY v.ts
```

## License

MIT, see LICENSE.
