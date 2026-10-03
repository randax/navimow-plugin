# Navimow collector

The collector replays raw Navimow captures into PostgreSQL Trail rows. It also
keeps a Navimow OAuth credential available for the forthcoming live transport.

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

Run the tests with a local PostgreSQL installation (the suite starts `pg_ctl`
automatically) or point it at an existing server:

```bash
NAVIMOW_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres pytest collector
```
