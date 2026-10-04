# Navimow collector

The collector records the Trail of every mower on a Navimow account into
PostgreSQL, live and unattended. It can also replay a raw capture through the
same ingestion core, which is how it is tested.

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

If Navimow rejects the stored login, the collector logs that
`navimow-collector login` is needed and keeps running; it picks up the new
login from the state file without a restart.

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
that the newest Trail points are dropped, gaps last, with an error logged. A clean stop moves what is in memory to
the file; a crash while the database is away loses what was still in memory, at
most 1,000 rows. Gap rows go to the file at once, and the note of when a gap
started is kept until its row has been handed over, so a crash at any point
records the gap again rather than losing it. Only when the file cannot take it
does a gap wait in memory with the Trail points, exposed to a crash like them.

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

Run the tests with a local PostgreSQL installation (the suite starts `pg_ctl`
automatically) or point it at an existing server:

```bash
NAVIMOW_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres pytest collector
```
