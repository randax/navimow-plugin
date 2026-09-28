# Navimow collector

The collector replays raw Navimow captures into PostgreSQL Trail rows. It has no
live transport yet, so captures are the single ingestion seam.

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

Run the tests with a local PostgreSQL installation (the suite starts `pg_ctl`
automatically) or point it at an existing server:

```bash
NAVIMOW_TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres pytest collector
```
