# Navimow collector

The collector records the Trail of every mower on a Navimow account into
PostgreSQL, live and unattended, and works out which Job and Zone each part of
it belongs to. It can also replay a raw capture through the same ingestion
core, which is how it is tested.

Install it for local development:

```bash
pip install -e 'collector[dev]'
```

Configure PostgreSQL in one TOML file. Values can be overridden with environment
variables such as `NAVIMOW_STORAGE_MIGRATE=false`.

```toml
[storage]
backend = "postgres"
dsn_file = "/run/secrets/navimow-postgres-dsn"
migrate = true
```

An inline `dsn = "postgresql://..."` is also accepted. Secret files have their
trailing newline removed, and `NAVIMOW_STORAGE_DSN_FILE` can supply the path.

```bash
navimow-collector --config collector/navimow-collector.example.toml config
navimow-collector --config collector/navimow-collector.example.toml replay fixtures/synthetic-job.jsonl.gz
```

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
permissions. Use `--timeout` to change the five-minute callback wait.

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

After logging in once, start collecting and leave the collector alone:

```bash
navimow-collector --config collector/navimow-collector.example.toml collect
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
the file; a crash while the database is away loses what was still in memory, at
most 1,000 rows. Gap rows go to the file at once, and the note of when a gap
started is kept until its row has been handed over, so a crash at any point
records the gap again rather than losing it. Only when the file cannot take it
does a gap wait in memory with the other rows, exposed to a crash like them.

A backlog is written back 200 rows at a time, a slice every tenth of a second,
so collection, token refresh and shutdown carry on while it drains; a stop in
the middle leaves only the unwritten rest in the file for the next start, while
after a crash the next start writes the file again from its beginning, which
costs time and nothing else. Rows already written stay in the file until all of
it is, so while it drains the file can reach twice its limit.

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
configuration).

Database work still happens on the collector's own event loop, so while one
call waits, up to those limits, nothing else is done. The limits do not cover a
server that keeps its connection open but stops answering, such as a suspended
backend: that holds collection up until it answers.

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

## Jobs and Zones

Nothing the mower sends names a Job, so the collector decides where each one
begins and ends. In short: a Job starts when the mower leaves the dock, a return
to charge is part of it, and it ends when the mower is back at the dock with the
Job finished or given up. The rules, and the real capture they were checked
against, are in [ADR 0001](../docs/adr/0001-job-and-zone-detection.md).

| Table          | One row per                 | Holds                                                                                              |
| -------------- | --------------------------- | -------------------------------------------------------------------------------------------------- |
| `job`          | Job                         | `start_time`, `end_time`, `completed`, `mowing_percentage`, `area` (m²) and the dock arrival pose  |
| `trail_point`  | position                    | the pose, with the `job_id` and `zone` it was mowed in                                             |
| `job_progress` | progress report             | `zone`, `zone_progress` and `mowing_percentage` (both percent), `area` and `week_area` (m²)        |
| `mower_state`  | state channel message       | `state` and `battery`: the status timeline and the battery level over time                         |

Every row carries its `mower_id`, and every row but a `job` names its Job in
`job_id`, which is empty while the mower is at the dock, a charging break
included. A Job's `job_id` is the second it started, in UTC, such as
`2026-09-30T13:33:33Z`.

A `job` row is rewritten as the Job goes on. While the mower is away its
`end_time` is empty; a Job that ended with `completed` false was interrupted, and
is taken up again if the mower next leaves the dock to carry on with it. `arrival_x`,
`arrival_y` and `arrival_theta` are the pose the mower docked in, which is where
the dock stands on the mower's own axes: a starting point for the Dock origin.

The layout is provisional until the schema is settled; changes to it only ever
add columns. A Trail recorded before this version has no `job_id`: replaying a
capture over rows already stored does not fill it in.

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
 "mowers": {"DEVICE_1": {"last_message_age_seconds": 12.0},
            "DEVICE_2": {"last_message_age_seconds": null}},
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
at most 5 seconds for it, and the request carries on in the background. Use it as it is:

```dockerfile
HEALTHCHECK CMD curl -fsS http://127.0.0.1:9477/health || exit 1
```

Without curl in the image, `python -c "import urllib.request;
urllib.request.urlopen('http://127.0.0.1:9477/health')"` fails the same way.

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

## Tests

Run the tests with a local PostgreSQL installation (the suite starts `pg_ctl`
automatically) or point it at an existing server:

```bash
NAVIMOW_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres pytest collector
```
