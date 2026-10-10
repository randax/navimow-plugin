# Navimow collector

The collector records the Trail of every mower on a Navimow account into
PostgreSQL, live and unattended, and works out which Job and Zone each part of
it belongs to. It can also replay a raw capture through the same ingestion
core, which is how it is tested. PostgreSQL is the database it is built around;
[TimescaleDB](#timescaledb), [ClickHouse](#clickhouse) and
[InfluxDB](#influxdb) are written to as well.

## Install

There are two ways to run it: as a container, or as a system service. Either
needs a PostgreSQL database it can reach, which it fills with its own tables,
and one interactive sign-in to Navimow. After that it is left alone.

The image and the package below are published by the collector's releases, of
which there has been none yet: see
[Before the first release](#release). Until then, build the image from this
directory with `docker build -t ghcr.io/randax/navimow-collector collector`, and
in place of the package name give pip
`"git+https://github.com/randax/navimow-plugin#subdirectory=collector"`, which
needs git.

### As a container

The image is built for 64-bit x86 and 64-bit ARM, so a Raspberry Pi on a 64-bit
system runs it as it is. One command starts it:

```bash
docker run -d --name navimow-collector --restart unless-stopped \
  -e NAVIMOW_STORAGE_DSN=postgresql://user:password@host/navimow \
  -v navimow-collector:/var/lib/navimow-collector \
  ghcr.io/randax/navimow-collector
```

It waits for a login and collects from the moment there is one. Sign in from
inside the running container, as on any machine without a browser:

```bash
docker exec navimow-collector navimow-collector login --no-browser
docker exec navimow-collector navimow-collector login --code '<the address the browser ended on>'
```

The first prints an address to open in a browser on any machine. Signing in
there ends on a page that cannot load, at `localhost:1`, and the second command
takes that page's whole address. [Navimow login](#navimow-login) has the rest.

- **The volume** holds the login and the rows waiting for a database that is
  away. Without it every new container has to be signed in again. A directory
  mounted in its place must be writable by user 10001, which the collector runs
  as.
- **Configuration** is by environment variable, as above: every value in
  [Configuration](#configuration) has one. To use a file, mount it and name it
  in `NAVIMOW_CONFIG`. The image itself sets `NAVIMOW_AUTH_STATE_FILE` and
  `NAVIMOW_COLLECTOR_STATE_DIR` to the volume, and the environment wins over a
  file, so those two stay there whatever a file says.
- **Health.** The image checks `/health` itself, so `docker ps` says whether the
  collector is healthy. To reach `/health` and `/metrics` from outside the
  container, add `-e NAVIMOW_HEALTH_LISTEN=0.0.0.0:9477 -p 9477:9477`. A
  container that moves `health.listen` or turns it off needs a health check of
  its own, or none.
- **Versions.** Without a tag the newest release is pulled. To stay on one, name
  it: `ghcr.io/randax/navimow-collector:0.1.0`.

### As a system service

For a machine with systemd and Python 3.11 or later, which Raspberry Pi OS
Bookworm has. Install the collector into an environment of its own, for a user
of its own, with the [sample unit](navimow-collector.service) and the
[example configuration](navimow-collector.example.toml):

```bash
sudo useradd --system --shell /usr/sbin/nologin navimow
sudo python3 -m venv /opt/navimow-collector
sudo /opt/navimow-collector/bin/pip install navimow-collector
sudo ln -s /opt/navimow-collector/bin/navimow-collector /usr/local/bin/

files=https://raw.githubusercontent.com/randax/navimow-plugin/main/collector
sudo curl -fsSL -o /etc/systemd/system/navimow-collector.service $files/navimow-collector.service
sudo curl -fsSL -o /etc/navimow-collector.toml $files/navimow-collector.example.toml
sudo chown root:navimow /etc/navimow-collector.toml
sudo chmod 640 /etc/navimow-collector.toml
```

In `/etc/navimow-collector.toml`, say where the database is: either put its
DSN in the file that `dsn_file` names, or replace the `dsn_file` line with
`dsn = "postgresql://..."`, as the two cannot both be set. Take the `#` off
`state_file` and `state_dir`: the service keeps its login and its buffer in
`/var/lib/navimow-collector`, which systemd creates for it as it starts. So
start it first, then sign in as the user it runs as:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now navimow-collector
sudo -u navimow navimow-collector --config /etc/navimow-collector.toml login --no-browser
sudo -u navimow navimow-collector --config /etc/navimow-collector.toml login --code '<the address the browser ended on>'
```

`journalctl -u navimow-collector -f` then shows it collecting. To upgrade, run
the `pip install` again with `--upgrade` and restart the service.

## Configuration

One TOML file holds everything, and any value in it can be given instead as an
environment variable named `NAVIMOW_<SECTION>_<KEY>`, such as
`NAVIMOW_STORAGE_MIGRATE=false`. The file is the one named with `--config`, or
in `NAVIMOW_CONFIG`.

```toml
[storage]
backend = "postgres"
dsn_file = "/run/secrets/navimow-postgres-dsn"
migrate = true
# retention_days = 365
```

An inline `dsn = "postgresql://..."` is also accepted. Secret files have their
trailing newline removed, and `NAVIMOW_STORAGE_DSN_FILE` can supply the path.
`retention_days` is described under [Retention](#retention). `backend` is
`postgres`, which is TimescaleDB's too, [`clickhouse`](#clickhouse) or
[`influxdb`](#influxdb).

`navimow-collector config` prints what the collector would run with, secrets
left out.

## Navimow login

No developer registration is needed. The collector uses Navimow's shared public
Home Assistant client, though `[auth]` lets an operator override its client id
or secret. The first login is interactive and is a one-off action; after that,
the collector refreshes its rotating credential automatically. Navimow does not
document refresh-token lifetime, so an occasional re-login remains an operator
action.

On a machine with a browser, run:

```bash
navimow-collector login
```

The command opens (and prints) a Navimow sign-in URL, listens briefly on a
temporary localhost callback, then writes the credential state with owner-only
permissions. The callback is answered on both 127.0.0.1 and ::1, whichever of
them the browser takes `localhost` to be. Use `--timeout` to change the
five-minute callback wait.

For a headless machine, print a URL and open it on any browser. After the
browser redirects to the deliberately unavailable `localhost:1` address, copy
either its `code` value or its entire address-bar URL back to the collector:

```bash
navimow-collector login --no-browser
navimow-collector login --code 'http://localhost:1/callback?code=...'
```

Credentials default to `~/.local/state/navimow-collector/tokens.json`; set
`auth.state_file` to use an operational state directory instead.

If Navimow rejects the stored login, the collector logs the exact login command
to run (see [Health and metrics](#health-and-metrics)) and keeps running; it
picks up the new login from the state file without a restart.

## Live collection

After logging in once, start collecting and leave the collector alone. This is
what the system service runs, and the container the same without a file:

```bash
navimow-collector --config /etc/navimow-collector.toml collect
```

One process records every mower on the account, and every row carries its
`mower_id`. A mower added to the account later is picked up at the next start.
The command collects until it receives SIGINT or SIGTERM. Started before any login
exists, it waits for one rather than failing.

**Reconnection** needs no operator. The broker connection keeps alive every 60
seconds, because the broker silently drops a connection that has been idle for
about ten minutes. Broker credentials are fetched once and reused for every
reconnect. They are fetched again only when the access token has been refreshed
or the connection has stayed down for a minute, never twice within a minute,
and at growing intervals (1, 5, 15, then 60 minutes) while the connection stays
down or the fetch keeps failing: that endpoint rate-limits aggressively.

Trail points are stored only from the current broker connection. When the
collector replaces a connection, for new credentials or a moved broker, whatever
the old one still delivers is not recorded: it lies inside the gap that the new
connection closes.

**Gaps.** Nothing can backfill what was missed while disconnected, so every
reconnection or restart writes one `collector_gap` row per mower with its
`start_time`, `end_time` and `reason` (`reconnect` or `restart`). A consumer
should draw a gap as a gap, not as a line between the points on either side. A
restart's gap starts when the previous process last noted a live connection,
which it does once a minute and when it shuts down. Each new connection also
asks Navimow once for every mower's current status.

Live messages reach the ingestion core as records in the capture format of
`tools/capture.py`, exactly as replay feeds them. A gap is one more kind of
record, so a capture that contains it replays to the same row:

```json
{"recv_ms": 1788084400000, "kind": "gap", "mower_id": "DEVICE_1", "start_ms": 1788084160000, "reason": "reconnect"}
```

**Database outages.** The database must be reachable when the collector starts.
After that, rows it cannot take are held in memory (1,000 rows), then appended
to `buffer.jsonl` in the state directory (up to 64 MiB of rows waiting), and
written when the database returns, by this process or the next. Once the file
is full, or if it cannot be written, memory keeps what it can hold and beyond
that the newest rows are dropped, gaps last, with an error logged. A clean stop moves what is in memory to
the file; a crash loses what was still in memory: the rows not yet written, a
moment's worth while the database answers and at most 1,000 while it is away.
Gap rows go to the file at once, and the note of when a gap
started is kept until its row has been handed over, so a crash at any point
records the gap again rather than losing it. Only when the file cannot take it
does a gap wait in memory with the other rows, exposed to a crash like them.

A backlog is written back 200 rows at a time; a stop in the middle leaves the
unwritten rest in the file for the next start, while after a crash the next
start writes the file again from its beginning, which costs time and nothing
else. Rows already written stay in the file until all of it is, so while it
drains the file can reach twice its limit.

A row the database refuses for what it holds is logged and dropped, never
retried, so it cannot hold up the rows behind it. Trouble with the buffer file
itself never stops collection: one that cannot be read is tried again every ten
seconds while new rows are written all the same, and one that cannot be
removed after its rows were written is logged and not written twice. Emptying or
deleting the file by hand is safe at any time and costs only the rows in it.
Do not overwrite it in place, for instance by copying a backup over it while
the collector is working through it: the collector would carry on from its old
position in the new contents and skip what lies before it.

A slow database counts as an outage too. By default a connection attempt gets
5 seconds and each statement 5 seconds (a lock held by maintenance included),
and TCP keep-alives notice a server that vanished from the network. These are
defaults, for `collect`, `replay` and migrations alike: a `connect_timeout` or
keep-alive setting in the DSN is used instead, and so is a `statement_timeout`
set anywhere at all (the DSN's `options`, the role, the database or the server
configuration). One wait is 5 seconds at most, whatever longer is set: the
wait for a lock while the collector makes or changes its tables, or tells
TimescaleDB how long to keep rows, behind another collector doing the same or
a query that holds one of the tables. A start that fails for it is tried again
like any outage; TimescaleDB not told is logged, and told at the next start.

The collector never waits on the database to do anything else. Rows are written
one batch at a time on a thread of their own, so collection, token refresh,
the health endpoint and a stop carry on whatever the database is doing; rows
arriving while a batch is being written wait in the same buffer, memory first
and then the file. A stop allows the writing 5 more seconds and leaves the rest
in the file.

The limits above all need the server, or the network in its place, to say
something. A server that keeps its connection open but stops answering, such as
a suspended backend, says nothing. So the collector keeps a limit of its own: a
batch the database has not answered within 30 seconds counts as an outage, and
the next attempt is made over a new connection. This one is not a default: a
`statement_timeout` set longer than 30 seconds is cut short by it. A connection
given up on is left until the database answers or drops it, and once two are
waiting like that no more are opened, so a database that answers nobody is not
crowded with connections.

```toml
[collector]
state_dir = "/var/lib/navimow-collector"
```

`collector.state_dir` holds that buffer and the connection note; it defaults to
`~/.local/state/navimow-collector`. The collector does not start if it cannot
write there. Should the directory stop being writable while it collects,
collection goes on and the failure is logged every minute, but the connection
note stays at its last value: the gap recorded at the next restart then starts
too early and lies across Trail that was in fact collected.

## Retention

Everything is kept unless you say otherwise: a season of mowing is on the order
of 100 to 150 MB in PostgreSQL.

```toml
[storage]
retention_days = 365
```

With `storage.retention_days` set (or `NAVIMOW_STORAGE_RETENTION_DAYS`), `collect`
removes what is older than that many days:

| Removed once old | Always kept |
|---|---|
| `trail_point`, `job_progress`, `mower_state`, `collector_gap` | `job`, `mower` |

So the history of Jobs (when each ran, how far it got, the area it mowed)
outlives their Trails. A position, progress report or state is as old as its
`device_time`, and a gap as old as its `end_time`. The value must be a positive
whole number of days.

Old rows are removed when the collector starts and once a day after that, so a
row can outlive the setting by a day. The removal has a thread and a database
connection of its own and takes at most 5,000 rows a statement: collection does
not wait for it. One that fails, or finds the database away, is logged and left
to the next day, and so is one the database has not answered by then. The day
between removals is counted in time elapsed, but what is old goes by the
collector's clock: one set far ahead removes rows early.

`replay` removes nothing, whatever the setting. What it stores that is older
than the setting is removed by `collect`, the next time it removes old rows.

## TimescaleDB

TimescaleDB (2.x) needs no setting of its own: to the collector it is
PostgreSQL, with `backend = "postgres"` and a DSN. Where the `timescaledb`
extension is installed in the database, the collector does two things more:

- As it creates its tables, it makes hypertables of the readings:
  `trail_point`, `job_progress` and `mower_state`, by their `device_time`.
  `collector_gap`, `job` and `mower` stay ordinary tables. Only a table it is
  just creating is made one: see below for a database it made its tables in
  before.
- When `collect` starts, it gives TimescaleDB a retention policy for each
  hypertable if `storage.retention_days` is set, replaces it if the number has
  changed or the policy was set aside, and removes it if the setting is gone:
  a policy you made by hand on one of these tables is replaced or removed like
  its own. Only its age and whether it is set aside (`scheduled`) are looked
  at: one whose schedule you changed some other way is left as it is. Whatever
  else TimescaleDB does with these tables, compression for one, is left alone. TimescaleDB then
  removes old readings itself, a whole chunk at a time, so a reading can
  outlive the setting by a week or so. Old gaps are removed by the collector,
  as on PostgreSQL.

`replay` gives TimescaleDB no policy and changes none. What it stores that is
older than a policy already there, TimescaleDB removes when it next applies it.

Should TimescaleDB refuse either (it makes no policies under its Apache
licence), the collector logs a warning and starts all the same: whatever table has no
retention policy, it removes old rows from itself. So it does where it is not
let read TimescaleDB's policies at all.

A database the collector made its tables in before the extension was
installed keeps them as ordinary tables, whether they hold rows yet or not,
and the collector goes on removing old rows from them itself. It does not make
hypertables of tables that are there: TimescaleDB rewrites a table that holds
a Trail under a lock, for as long as that takes, and a reading written at that
moment by a collector at work could be lost. So it is yours to do, with the
collector stopped:

```sql
SELECT create_hypertable('trail_point', 'device_time', migrate_data => true, create_default_indexes => false);
SELECT create_hypertable('job_progress', 'device_time', migrate_data => true, create_default_indexes => false);
SELECT create_hypertable('mower_state', 'device_time', migrate_data => true, create_default_indexes => false);
```

The next start of `collect` gives each its retention policy.

With `storage.migrate = false` the collector makes neither hypertables nor
policies, and leaves any policy as it finds it: a table with a policy is left
to TimescaleDB, however long that policy keeps rows.

## ClickHouse

```toml
[storage]
backend = "clickhouse"
dsn_file = "/run/secrets/navimow-clickhouse-url"   # http://user:password@clickhouse:8123/navimow
```

The collector speaks to ClickHouse's HTTP interface (`https://` and port 8443
where it is set up for TLS). It is tested against ClickHouse 26.8, and needs
25.6 or later. The database named must exist; the collector creates its tables in it, the same
tables and columns as in PostgreSQL. Times are `DateTime64(6, 'UTC')`, what a
mower may leave unsaid is `Nullable`, and a Job's `zones` is an array that is
empty until they are known.

**Do not run ClickHouse for this on a Raspberry Pi.** Its official image does
not start on a Raspberry Pi 4, whose processor lacks instructions it requires.
ClickHouse recommends 32 GB of memory and wants tuning below 16 GB, on a
machine that here also runs Grafana and the collector. And a year of one mower
is some three million rows, which a column store is not needed for. Use
PostgreSQL there. ClickHouse is supported for whoever already runs one.

Three things differ from PostgreSQL:

- **Read every table with `FINAL`.** ClickHouse holds no row to a key: a row
  told of again is a second row, until ClickHouse merges the two. The collector
  asks what is stored before it writes, and writes only what is new, so that a
  capture replayed twice leaves nothing behind; but two writers at once can
  each store the same row, and a query without `FINAL` then counts it twice.
  The tables are `ReplacingMergeTree`, so with `FINAL`, or once merged, there
  is one.
- **Retention is a TTL.** When `collect` starts with `storage.retention_days`
  set, it gives the four tables that expire a TTL of that many days, changes it
  if the number has changed, and removes it if the setting is gone: a TTL you
  set by hand on one of them is replaced or removed like its own. ClickHouse
  removes old rows as it merges, some hours later, and the collector removes
  none itself. `replay` sets no TTL and changes none. With
  `storage.migrate = false` the collector sets none either: if a number of days
  is set all the same and a table has no TTL, it says once a day in its log
  that nothing removes old rows from it.
- **The bundled dashboard is PostgreSQL's.** For ClickHouse it needs the
  [ClickHouse data source](https://grafana.com/grafana/plugins/grafana-clickhouse-datasource/)
  and queries of its own, with `$__timeFilter_ms` on these times.

A mower sends a position every two seconds, and the collector writes rows as
they come, a few at a time. ClickHouse would rather have them by the thousand,
but merges what it is given, and at this rate keeps up.

## InfluxDB

InfluxDB 1, 2 and 3 are all written to by the one `influxdb` backend. Each of
them takes rows at the older `/write` and at the newer `/api/v2/write`, and
answers InfluxQL at `/query`; the address says which way in to use.

```toml
[storage]
backend = "influxdb"
# The older way in: a database, and a user with a password if it asks for one.
dsn = "http://navimow:password@influxdb:8086/navimow"
# The newer: a bucket, with a token and, for InfluxDB 2, the organisation.
# dsn = "http://influxdb:8086/navimow?org=home&token=..."
```

A DSN with a `token` or an `org` is written through `/api/v2/write`, with the
token sent as `Authorization: Token ...`; any other through `/write`, with the
user and password, if any, as HTTP basic authentication. InfluxDB 2 wants the
organisation named beside a token. A password or a database name with marks in
it is written as an address writes them, `%2F` for a `/`. Keep the DSN in a
file (`dsn_file`) like any other.

The database or bucket must exist; nothing is made in it but points. When it
opens the database, the collector asks it one question and sends it one write
with nothing in it, so that a wrong address, login or organisation stops it
there, and is not found out later one row at a time.

The data has another shape here, since InfluxDB has measurements and tags
where the others have tables and keys:

| | |
|---|---|
| Measurements | named as the tables: `trail_point`, `job_progress`, `mower_state`, `job`, `collector_gap`, `mower` |
| Time | `device_time`; a Job's and a gap's `start_time`; a mower's `updated_time` |
| Tags | `mower_id` on all; `job_id` wherever a row names a Job; `zone` on `trail_point` and `job_progress` |
| Fields | every other column, by its name. Other times are whole milliseconds since 1970, and a Job's `zones` is text such as `1,6,7,9,10,11` |

A point written again at the same time under the same tags is written over the
first, field by field, and a field once written cannot be unset. What follows
from that differs from the other databases:

- A reading delivered twice is stored once, as elsewhere, but with the later
  `received_time`. The collector cannot tell that it was there, and counts it
  as written.
- A Job is told of again as it goes on, each telling written over the one
  before. An older telling written late (after an outage, from the buffer) is
  written over a later one, where the other databases refuse it: what it says
  is put back, and what only the later one said stays.
- A Job that is not ended has an `end_time` of 0, since it is ended and begun
  again around each charge.
- A reading stored again under another Job or Zone is a second point.
- A mower described anew is a new point: the descriptions before stay, as its
  history. The latest is how it is now. A mower of which neither name, model
  nor firmware is known is no point at all.

**Retention is InfluxDB's own.** Points are kept for as long as their bucket,
or in InfluxDB 1 their retention policy, says. `storage.retention_days` is
refused with this backend when the configuration is read: set it there.

InfluxDB 3 answers a write once it has flushed its log, which it does each
second, so replaying a long capture into it takes about a second for every 500
rows. Live collection, which writes a few rows at a time off its own loop, is
not held up by that.

The bundled dashboard is PostgreSQL's; for InfluxDB it needs queries of its
own, in InfluxQL, which all three answer.

## Jobs and Zones

Nothing the mower sends names a Job, so the collector decides where each one
begins and ends. In short: a Job starts when the mower leaves the dock, a return
to charge is part of it, and it ends when the mower is back at the dock with the
Job finished or given up. The rules, and the real capture they were checked
against, are in [ADR 0001](../docs/adr/0001-job-and-zone-detection.md).

| Table          | One row per                 | Holds                                                                                              |
| -------------- | --------------------------- | -------------------------------------------------------------------------------------------------- |
| `job`          | Job                         | `start_time`, `end_time`, `completed`, `mowing_percentage`, `area` (m²), the dock arrival pose and its `zones` |
| `trail_point`  | position                    | the pose, with the `job_id` and `zone` it was mowed in                                             |
| `job_progress` | progress report             | `zone`, `zone_progress` and `mowing_percentage` (both percent), `area` and `week_area` (m²)        |
| `mower_state`  | state channel message       | `state` and `battery`: the status timeline and the battery level over time                         |
| `mower`        | mower                       | `name`, `model` and `firmware`, as the account's device list describes it                          |

Every row carries its `mower_id`, and every row but a `job` names its Job in
`job_id`, which is empty while the mower is at the dock, a charging break
included. A Job's `job_id` is the second it started, in UTC, such as
`2026-09-30T13:33:33Z`.

A `job` row is rewritten as the Job goes on. While the mower is away its
`end_time` is empty; a Job that ended with `completed` false was interrupted, and
is taken up again if the mower next leaves the dock to carry on with it. `arrival_x`,
`arrival_y` and `arrival_theta` are the pose the mower docked in, which is where
the dock stands on the mower's own axes: a starting point for the Dock origin.
`zones` are the Zones the Job was set to mow, as the mower last listed them
while away on it, and empty until it has.

A `mower` row is rewritten when the device list describes the mower
differently, a firmware update for one, and `updated_time` says since when. The
collector reads the list as it starts, so that is when a change is picked up.

There is no table for error events or signal strength: no capture has yet held
a message carrying either. A mower in trouble shows as a `mower_state` row
whose `state` is `Error` or `isLifted`, and a message on a channel nothing is
stored of is counted and logged (see [Health and metrics](#health-and-metrics)).

Changes to this layout only ever add tables and columns. Why it is laid out so,
what was left out and how long rows are kept are in
[ADR 0002](../docs/adr/0002-data-schema.md). A Trail recorded before Jobs were
detected has no `job_id`: replaying a capture over rows already stored does not
fill it in.

A collector that restarts carries on from each mower's latest stored Job, so a
restart or an outage inside a Job leaves a gap in it rather than splitting it.
`replay` does not: a capture is decided on its own, whatever is already stored.

## Health and metrics

While `collect` runs it serves two endpoints, by default on `127.0.0.1:9477`:

```toml
[health]
listen = "127.0.0.1:9477"  # "0.0.0.0:9477" for a scraper elsewhere; "" serves neither
```

If that address cannot be used, `collect` does not start.

`GET /health` (or `HEAD`) answers with a JSON report of every signal:

```json
{"status": "live", "seconds_since_tick": 2.5,
 "broker": {"connected": true},
 "mowers": {"DEVICE_1": {"last_message_age_seconds": 12.0, "unstored_messages": {"event": 4}},
            "DEVICE_2": {"last_message_age_seconds": null, "unstored_messages": {}}},
 "database": {"reachable": true, "buffered_rows": 0,
              "rows_written": 1500, "rows_dropped": 0, "rows_rejected": 0},
 "auth": {"state": "fresh", "reauth_required": false,
          "login_command": "navimow-collector --config /etc/navimow-collector.toml login"}}
```

A mower's age is `null` until it has sent something since the collector started.

The status code is the health check: **200 while the collection loop is
running, 503 only when it has not completed a tick for 120 seconds**, the one
fault a restart fixes. A broker that is down, a database that is away and a
login that needs renewing are all reported in the body and the metrics but
answered with 200: reconnection and the buffer already deal with the first two,
and no credential problem may ever restart the collector. A Navimow request
that hangs, such as a token refresh, cannot stall the loop either: each tick waits
at most 5 seconds for it, and the request carries on in the background. The
container image's own health check is this endpoint, asked with Python, as the
image has no curl:

```dockerfile
HEALTHCHECK CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9477/health', timeout=4)"]
```

`GET /metrics` serves the same signals in the Prometheus text format:

| Metric | Type | Meaning |
| --- | --- | --- |
| `navimow_collector_seconds_since_tick` | gauge | seconds since the collection loop last completed a tick |
| `navimow_collector_broker_connected` | gauge | 1 while the broker connection is up |
| `navimow_collector_last_message_age_seconds{mower_id}` | gauge | seconds since the mower's last broker message, `NaN` until the first |
| `navimow_collector_database_reachable` | gauge | 1 while the database answers |
| `navimow_collector_buffered_rows` | gauge | rows waiting for the database |
| `navimow_collector_reauth_required` | gauge | 1 when only `navimow-collector login` can restore access |
| `navimow_collector_auth_state{state}` | gauge | 1 for the current state: `fresh`, `refreshing`, `retry-pending`, `relogin-required` |
| `navimow_collector_rows_written_total` | counter | rows the database stored, backlog included |
| `navimow_collector_rows_dropped_total` | counter | rows lost because no buffer could hold them |
| `navimow_collector_rows_rejected_total` | counter | rows the database refused for what they hold |
| `navimow_collector_unstored_messages_total{mower_id,channel}` | counter | messages on a channel nothing is stored of, such as `event` or `attributes` |

Nothing is stored of the mower's `event` and `attributes` channels, because no
capture has yet held a message on either and what they carry is not known. If
`navimow_collector_unstored_messages_total` ever rises, the first message from
each mower on each channel is in the log at INFO, with up to 300 characters of
what it carried, and the rest at DEBUG: that is what a table for them would be
designed from.

Database reachability is as of the last write or retry: while nothing is
written it keeps its last value. A mower that is docked and quiet may send
nothing for hours, so alert on its message age only together with the time of
day or season you expect it to mow.

To be alerted in Grafana when a login is needed, alert on:

```promql
navimow_collector_reauth_required == 1
```

and on collection stopping altogether with `absent(navimow_collector_seconds_since_tick)`
or `navimow_collector_seconds_since_tick > 120`.

**Logs** go to standard error as one JSON object per line, with `time`,
`level`, `logger` and `message`, plus any fields specific to the line. When a
login is needed the line says what to run, including the `--config` the
collector was started with, and carries it in `command` as well:

```json
{"time": "2026-10-05T07:12:03.114Z", "level": "ERROR", "logger": "navimow_collector.auth", "message": "Navimow rejected the stored login (HTTP 400: Refresh token is invalid or server rejected the request); run `navimow-collector --config /etc/navimow-collector.toml login`", "command": "navimow-collector --config /etc/navimow-collector.toml login"}
```

That error is logged once, when the login is found rejected; the hourly check
that finds it still rejected logs a warning with the same command.

## Develop

```bash
pip install -e 'collector[dev]'
navimow-collector --config collector/navimow-collector.example.toml config
navimow-collector replay fixtures/synthetic-job.jsonl.gz   # into the database NAVIMOW_STORAGE_DSN names
```

Run the tests with a local PostgreSQL installation (the suite starts `pg_ctl`
automatically) or point it at an existing server:

```bash
NAVIMOW_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres pytest collector
```

`tests/test_conformance.py` asks the same of every database the collector
writes to, where they differ: a schema made from nothing, rows written out of
order and from years ago, a batch, one Job's Trail read back in order,
retention, a row told of again, and a Job read back as it was written.
PostgreSQL is the one above.
For TimescaleDB, start one that keeps nothing once stopped, and name it:

```bash
docker run --rm -d --name navimow-timescale -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1:55433:5432 --tmpfs /var/lib/postgresql/data timescale/timescaledb:latest-pg16
NAVIMOW_TEST_TIMESCALE_DSN=postgresql://postgres@127.0.0.1:55433/postgres pytest collector/tests/test_conformance.py
docker stop navimow-timescale
```

And for ClickHouse, the same way:

```bash
docker run --rm -d --name navimow-clickhouse -e CLICKHOUSE_SKIP_USER_SETUP=1 \
  -p 127.0.0.1:58123:8123 --tmpfs /var/lib/clickhouse clickhouse/clickhouse-server:26.8
NAVIMOW_TEST_CLICKHOUSE_URL=http://default@127.0.0.1:58123 pytest collector/tests/test_conformance.py
docker stop navimow-clickhouse
```

And for InfluxDB, one of each line, each named by a variable of its own with
the login of one who may make databases on it:

```bash
docker run --rm -d --name navimow-influx1 -p 127.0.0.1:58086:8086 --tmpfs /var/lib/influxdb:uid=1500,gid=1500 \
  -e INFLUXDB_HTTP_AUTH_ENABLED=true -e INFLUXDB_ADMIN_USER=navimow -e INFLUXDB_ADMIN_PASSWORD=conformance influxdb:1.11
docker run --rm -d --name navimow-influx2 -p 127.0.0.1:58087:8086 --tmpfs /var/lib/influxdb2 --tmpfs /etc/influxdb2 \
  -e DOCKER_INFLUXDB_INIT_MODE=setup -e DOCKER_INFLUXDB_INIT_USERNAME=navimow -e DOCKER_INFLUXDB_INIT_PASSWORD=conformance \
  -e DOCKER_INFLUXDB_INIT_ORG=home -e DOCKER_INFLUXDB_INIT_BUCKET=navimow -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=conformance influxdb:2.7
docker run --rm -d --name navimow-influx3 -p 127.0.0.1:58181:8181 -e INFLUXDB3_NODE_IDENTIFIER_PREFIX=conformance \
  -e INFLUXDB3_OBJECT_STORE=memory -e INFLUXDB3_START_WITHOUT_AUTH=true influxdb:3-core
export NAVIMOW_TEST_INFLUXDB1_URL='http://navimow:conformance@127.0.0.1:58086'
export NAVIMOW_TEST_INFLUXDB2_URL='http://127.0.0.1:58087?org=home&token=conformance'
export NAVIMOW_TEST_INFLUXDB3_URL='http://127.0.0.1:58181?org=home'
pytest collector/tests/test_conformance.py
docker stop navimow-influx1 navimow-influx2 navimow-influx3
```

A backend whose variable is not set is skipped, unless `NAVIMOW_TEST_REQUIRE`
names it: then its absence fails the run. One that is named and does not
answer fails the tests that need it. That is how each backend has a job of
its own on every pull request and each night, the night being for what changes
outside the repository, such as a database image.

Two more checks run on every pull request. `scripts/package.sh` builds the wheel
and the source distribution and installs the wheel into an environment of its
own. `scripts/smoke.sh`, which needs Docker, builds the container image and
waits for it to report itself healthy beside a PostgreSQL.

## Release

The collector has its own version, the one in `pyproject.toml`, and its own
tags, `collector/v<version>`. The panel's are `panel/v<version>`, and releasing
either never releases the other: [why that holds](../README.md#two-versions).

1. Set `version` in `pyproject.toml`, and in `CHANGELOG.md` rename
   `## Unreleased` to `## <version>`. The changelog is written by hand, as
   changes are made. Merge.
2. Tag the merged commit and push the tag:
   `git tag collector/v<version> && git push origin collector/v<version>`.

`.github/workflows/collector-release.yml` then runs the tests, builds the
package and the image as every pull request does, and publishes, in this order:

- the image, for `linux/amd64` and `linux/arm64`, as
  `ghcr.io/randax/navimow-collector:<version>` and `:latest`;
- the wheel and the source distribution to the package index, as
  `navimow-collector`;
- a GitHub release holding both of those, the sample unit and the example
  configuration, with that version's changelog entries as its notes.

It publishes nothing if the tag and `pyproject.toml` disagree, if the changelog
has no entries for the version, or if `pyproject.toml` depends on a URL, which
the package index refuses. The image goes first because its tag can be pushed
again and a version on the package index cannot: should a later step fail, put
right what stopped it and re-run the failed jobs, not the whole workflow.

**Before the first release**, three things that are done once:

- **The SDK on the package index.** `pyproject.toml` installs
  `randax-navimow-sdk` from its repository, by URL, so the release stops at the
  check above. Once the SDK is published, depend on it as
  `randax-navimow-sdk>=0.5,<0.6`, and take `git` out of the `Dockerfile` and
  `allow-direct-references` out of `pyproject.toml`.
- **A trusted publisher.** On pypi.org, under Publishing, add a pending
  publisher for the project `navimow-collector`: owner `randax`,
  repository `navimow-plugin`, workflow `collector-release.yml`, environment
  `pypi`. The workflow then needs no token.
- **A public image.** GitHub keeps a new container package private. After the
  first release, set `navimow-collector` to public under the repository's
  Packages.

## Licence

GPL-3.0-only, in [`LICENSE`](LICENSE), in the package and in the image. It is
inherited, not chosen: the collector imports the Navimow SDK, which is
GPL-3.0-only. The panel in the same repository is Apache-2.0, and
[the repository's README](../README.md#two-licences) explains the split.
