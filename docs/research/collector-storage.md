# Research: storage options for the collector

Resolves [#5](https://github.com/randax/navimow-plugin/issues/5). Researched 2026-09-15 against official docs, source repositories and release pages; every claim links to its source. Items that could not be pinned to a primary source are marked UNVERIFIED.

## Question

Which store should the Python collector write Jobs, Trails and metrics to, for a self-hosted Grafana on a Raspberry Pi 4/5? Candidates: PostgreSQL (+TimescaleDB, +PostGIS), InfluxDB 2/3, Prometheus, SQLite via a Grafana plugin. A fifth candidate, MariaDB, is covered because it already runs beside many Home Assistant installs.

## What the data looks like

From the SDK docs ([models.md](https://github.com/randax/navimow-sdk/blob/main/docs/models.md), [realtime.md](https://github.com/randax/navimow-sdk/blob/main/docs/realtime.md)):

- A Trail point is one `DeviceLocationMessage` of type `1`: `device_id`, `x`, `y` (metres from the dock), `theta` (radians), `timestamp` (set on the wire, not by the collector), `vehicle_state`. One MQTT payload may carry several points; the broker may delay or reorder them, so the store must accept **out-of-order historical timestamps**.
- Per-Zone progress (type `2`): `current_zone`, `zone_progress`, `mowing_percentage`, `subtotal_area`; a zone list (type `3`) about every five minutes; `week_area`; error events on a separate topic with `level` and `message` (strings).
- **There is no Job id on the wire.** The collector derives a Job from the dock -> mowing -> dock state sequence and assigns its own `job_id`. Job is therefore a first-class row the collector owns, not a tag it copies.
- Config (Dock origin lat/lon/rotation, Boundary GeoJSON) is a handful of rows that change rarely and must be readable without a time range.

Volume is small in every candidate: one mower at roughly 1 Hz (assumption; the SDK does not state a rate) for a two-hour Job is ~7,000 points, ~2.6 M points a year, well under 1 GB a year in any of the stores below.

## Criteria

1. **Modelling**: Trail rows with several values per timestamp; Job/Zone as entities; string events; config rows.
2. **Grafana ergonomics**: one signed datasource; a query that returns one Job's ordered Trail for the dashboard time range; query variables for picking a Job.
3. **Pi footprint and ops**: ARM64 images, memory, retention, licence.
4. **Python client**: mature asyncio write path (the SDK is asyncio-based and lists Raspberry Pi OS as a first-class target; [README](https://github.com/randax/navimow-sdk)).
5. **Config storage** for Boundary and Dock origin.
6. **Runs beside Home Assistant** already.

## Candidates

### 1. PostgreSQL, optionally with TimescaleDB (and PostGIS)

**Modelling.** Plain relational tables: `job`, `trail`, `zone_progress`, `metric`, `event`, `config`. Trail is one row per point with `x`, `y`, `theta` as `double precision`; Boundary as `jsonb`. Nothing here needs an extension. TimescaleDB adds hypertables (`CREATE TABLE ... WITH (timescaledb.hypertable)`, default 7-day chunks; [docs](https://www.tigerdata.com/docs/use-timescale/latest/hypertables/create)), a one-line retention policy (`add_retention_policy('trail', drop_after => INTERVAL '6 months')`; [docs](https://www.tigerdata.com/docs/api/latest/data-retention/add_retention_policy)) and columnstore compression ([docs](https://www.tigerdata.com/docs/api/latest/hypercore/add_columnstore_policy)). PostGIS adds server-side `ST_MakeLine`, `ST_Length`, `ST_Area`, `ST_Transform` ([reference](https://postgis.net/docs/manual-3.5/reference.html)); for a local metric grid these are simple window sums in SQL, and georeferencing already happens in the panel, so PostGIS is optional.

**Grafana.** Built-in, Grafana-signed datasource with `$__timeFilter`, `$__timeGroup`, `$__unixEpoch*` macros; table format returns any SQL result; time-series format needs a column named `time`, sorted ([query editor](https://grafana.com/docs/grafana/latest/datasources/postgres/query-editor/)). Query variables are full SQL, with `$__timeFilter` allowed when refresh is "On time range change" ([template variables](https://grafana.com/docs/grafana/latest/datasources/postgres/template-variables/)). A TimescaleDB toggle enables `time_bucket` in the editor; PostGIS `geometry` columns are returned as strings, so use `ST_X`/`ST_Y`/`ST_AsGeoJSON` if geometry columns are used ([datasource docs](https://grafana.com/docs/grafana/latest/datasources/postgres/)).

```sql
-- Job picker variable (refresh on time range change)
SELECT job_id AS __value, to_char(started_at, 'YYYY-MM-DD HH24:MI') AS __text
FROM job WHERE device_id = '$device' AND $__timeFilter(started_at) ORDER BY started_at DESC;

-- One Job's ordered Trail (format: table)
SELECT time, x, y, theta, status FROM trail
WHERE job_id = $job AND $__timeFilter(time) ORDER BY time;
```

**Pi footprint and ops.** `timescale/timescaledb:latest-pg17` (Alpine, no PostGIS) and `timescale/timescaledb-ha:pg17` (Ubuntu, ~2 GB, includes PostGIS and Toolkit) both publish `linux/arm64` ([Hub](https://hub.docker.com/r/timescale/timescaledb/tags), [Hub](https://hub.docker.com/r/timescale/timescaledb-ha/tags), [install docs](https://www.tigerdata.com/docs/self-hosted/latest/install/installation-docker)). The image runs `timescaledb-tune` at start and can be capped with `TS_TUNE_MEMORY` so Postgres does not claim 25% of the Pi's RAM ([timescaledb-docker](https://github.com/timescale/timescaledb-docker)). PostgreSQL's own default `shared_buffers` is 128 MB ([runtime config](https://www.postgresql.org/docs/current/runtime-config-resource.html)); no minimum RAM is published for TimescaleDB (UNVERIFIED). Licence: PostgreSQL licence; TimescaleDB Apache 2 for basic hypertables, Community features (retention, compression, continuous aggregates) under the Timescale Licence, which permits free self-hosting and forbids offering it as a service ([editions](https://www.tigerdata.com/docs/about/latest/timescaledb-editions), [TSL](https://github.com/timescale/timescaledb/blob/main/tsl/LICENSE-TIMESCALE)). Timescale rebranded to Tiger Data in June 2025; the extension is still TimescaleDB, latest 2.30.0 (Sep 2026) for PG 16-18 ([releases](https://github.com/timescale/timescaledb/releases)). Without TimescaleDB, retention is a nightly `DELETE FROM trail WHERE time < now() - interval '1 year'`, which is fine at this volume.

**Python.** psycopg 3.3.5 (Aug 2026) has native asyncio, `COPY` via `cursor.copy().write_row()`, and `executemany()` uses pipeline mode ([PyPI](https://pypi.org/project/psycopg/), [COPY](https://www.psycopg.org/psycopg3/docs/basic/copy.html), [pipeline](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html)). asyncpg 0.31.0 (Nov 2025) is the faster alternative with binary `copy_records_to_table()` ([PyPI](https://pypi.org/project/asyncpg/), [API](https://magicstack.github.io/asyncpg/current/api/index.html)). One dependency, no compiled extras beyond the wheel.

**Config.** A `config` table (or `device` table) with `dock_lat`, `dock_lon`, `rotation_rad`, `boundary jsonb`. Trivial.

**Beside Home Assistant.** HA's recorder officially supports PostgreSQL 12+ ([recorder](https://www.home-assistant.io/integrations/recorder/)). The community add-on `Expaso/hassos-addon-timescaledb` bundles PostgreSQL + TimescaleDB + PostGIS for aarch64 and others, last release Oct 2025 ([repo](https://github.com/Expaso/hassos-addon-timescaledb)); it is not in the official add-on repository.

### 2. SQLite via `frser-sqlite-datasource`

**Modelling.** Same relational schema as Postgres; `x`/`y`/`theta` as REAL, Boundary as TEXT. SpatiaLite cannot be loaded from the Grafana plugin (`load_extension` is "not authorized"; [issue #139](https://github.com/fr-ser/grafana-sqlite-datasource/issues/139)), so geometry stays in Python.

**Grafana.** Community-signed plugin, v4.0.6 (May 2026), Grafana >= 7.3.3, backend binaries for `linux_arm64`, `linux_arm`, `linux_amd64` bundled in one zip ([catalog](https://grafana.com/grafana/plugins/frser-sqlite-datasource/), [releases](https://github.com/fr-ser/grafana-sqlite-datasource/releases), [Magefile](https://github.com/fr-ser/grafana-sqlite-datasource/blob/main/Magefile.go)). No `$__timeFilter`: SQLite has no time type, so you mark a column as time (Unix **seconds** or RFC3339) and filter with the global `$__from`/`$__to` ([README](https://github.com/fr-ser/grafana-sqlite-datasource#support-for-time-formatted-columns), [global variables](https://grafana.com/docs/grafana/latest/visualizations/dashboards/variables/global-variables/)). Query variables work ([examples](https://github.com/fr-ser/grafana-sqlite-datasource/blob/main/docs/examples.md)). Grafana must be able to open the `.db` on its own filesystem: same host or a shared volume ([FAQ](https://github.com/fr-ser/grafana-sqlite-datasource/blob/main/docs/faq.md)). Infinity does **not** read SQLite; it reads JSON/CSV/XML over HTTP ([Infinity](https://github.com/grafana/grafana-infinity-datasource)), so "SQLite via Infinity" means the collector also serves an HTTP API. Grafana's core SQL datasources are MySQL, PostgreSQL and MSSQL only ([data sources](https://grafana.com/docs/grafana/latest/datasources/)).

```sql
SELECT ts, x, y, theta FROM trail
WHERE job_id = ${job} AND ts BETWEEN ${__from:date:seconds} AND ${__to:date:seconds}
ORDER BY ts;
```

**Pi footprint and ops.** No extra process. Collector opens the database in WAL mode so Grafana can read while it writes ([WAL](https://sqlite.org/wal.html)); the plugin opens with `query_only` ([source](https://github.com/fr-ser/grafana-sqlite-datasource/blob/main/pkg/plugin/sqlite_datasource.go)) but in WAL mode still needs write access to the **directory** for the `-shm` file ([FAQ](https://github.com/fr-ser/grafana-sqlite-datasource/blob/main/docs/faq.md)), and readers can hit `SQLITE_BUSY` unless `busy_timeout` is set ([issue #99](https://github.com/fr-ser/grafana-sqlite-datasource/issues/99)). Retention is `DELETE` plus `VACUUM`/`auto_vacuum` ([VACUUM](https://sqlite.org/lang_vacuum.html)). The plugin is maintained by one person (last push June 2026, 2026 releases are dependency bumps; [API](https://api.github.com/repos/fr-ser/grafana-sqlite-datasource)).

**Python.** `sqlite3` is stdlib ([docs](https://docs.python.org/3/library/sqlite3.html)); `aiosqlite` 0.22.1 (Dec 2025) is production/stable and runs one thread per connection ([PyPI](https://pypi.org/project/aiosqlite/)).

**Config.** Same as Postgres.

**Beside Home Assistant.** SQLite is HA's default and recommended recorder engine and copes with HA-scale writes on a Pi; HA warns about SD-card wear and suggests a longer `commit_interval` ([recorder](https://www.home-assistant.io/integrations/recorder/)). That advice applies here too: batch Trail inserts per second or per payload, not per point.

### 3. InfluxDB 2.x

**Modelling.** Measurement `trail` with tags `device_id`, `job_id` and fields `x`, `y`, `theta`; separate measurements for zone progress, metrics and events. A Job is not an entity, only a tag value; "list Jobs with start and end" becomes `first()`/`last()` aggregates. The cardinality docs warn against unbounded ids as tags ([cardinality](https://docs.influxdata.com/influxdb/v2/write-data/best-practices/resolve-high-cardinality/)); a few Jobs a day for one mower stays far below any practical limit, but it is against documented practice.

**Grafana.** Built-in datasource supports Flux, InfluxQL and (for 3.x) SQL ([datasource](https://grafana.com/docs/grafana/latest/datasources/influxdb/)); query variables via `SHOW TAG VALUES` or `schema.tagValues` ([template variables](https://grafana.com/docs/grafana/latest/datasources/influxdb/template-variables/)). Flux needs a `pivot` to get `x`, `y`, `theta` on one row; InfluxQL returns them as columns directly:

```sql
SELECT "x","y","theta" FROM "trail" WHERE "job_id" = '$job' AND $timeFilter ORDER BY time ASC
```

Flux is "in maintenance mode and is not supported in InfluxDB 3" ([future of Flux](https://docs.influxdata.com/flux/v0/future-of-flux/)), so use InfluxQL to stay portable.

**Pi footprint and ops.** MIT ([LICENSE](https://github.com/influxdata/influxdb/blob/main-2.x/LICENSE)); official image has `arm64v8` and Raspberry Pi 4+ on 64-bit OS is explicitly supported ([install](https://docs.influxdata.com/influxdb/v2/install/), [Hub](https://hub.docker.com/_/influxdb)). Still released: 2.9.1 in May 2026 ([releases](https://github.com/influxdata/influxdb/releases/tag/v2.9.1)), but the docs label it an earlier version and the Docker `latest` tag switched to 3 Core on 2026-09-15, so pin `influxdb:2` ([install](https://docs.influxdata.com/influxdb/v2/install/)). Retention is a per-bucket period ([buckets](https://docs.influxdata.com/influxdb/v2/admin/buckets/update-bucket/)).

**Python.** `influxdb-client` has `InfluxDBClientAsync` with `WriteApiAsync` and batching ([repo](https://github.com/influxdata/influxdb-client-python)).

**Config.** Possible as a point with a string field (64 KB limit; [line protocol](https://docs.influxdata.com/influxdb/v2/reference/syntax/line-protocol/)) read back with `last()`, but a config row with a timestamp is a workaround, not a model.

**Beside Home Assistant.** HA's InfluxDB integration writes to 1.x, 2.x and 3.x ([integration](https://www.home-assistant.io/integrations/influxdb/)). There is no InfluxDB add-on in the official add-on repository ([addons](https://github.com/home-assistant/addons)); the community add-on shipped 1.8, is deprecated and was archived in Aug 2026 ([addon-influxdb](https://github.com/hassio-addons/addon-influxdb)). "InfluxDB already runs beside HA" is largely a 1.8 install that this project should not depend on.

### 4. InfluxDB 3 Core / Enterprise

Core is MIT/Apache-2 and GA since April 2025 ([repo](https://github.com/influxdata/influxdb), [GA](https://www.influxdata.com/blog/influxdata-announces-influxdb-3-OSS-GA/)), SQL + InfluxQL, no Flux, unlimited tag cardinality ([schema design](https://docs.influxdata.com/influxdb3/core/write-data/best-practices/schema-design/)). Three things rule it out for this project:

- Core "limits query time ranges to approximately 72 hours" (soft limit via `--query-file-limit`, with an OOM warning if raised) ([query](https://docs.influxdata.com/influxdb3/core/get-started/query/), [config](https://docs.influxdata.com/influxdb3/core/reference/config-options/)). A "Jobs this month" dashboard needs Enterprise.
- Retention is fixed at database creation in Core ([create database](https://docs.influxdata.com/influxdb3/core/admin/databases/create/)).
- `influxdb3-python` depends on pyarrow and its async write path is deprecated ([client docs](https://docs.influxdata.com/influxdb3/core/reference/client-libraries/v3/python/)). Grafana's SQL mode requires FlightSQL over HTTP/2 and Grafana 12.2+ is recommended ([Grafana with v3](https://docs.influxdata.com/influxdb3/core/visualize-data/grafana/)).

Enterprise's free at-home licence (2 cores, hobbyist only, non-commercial; [licence](https://docs.influxdata.com/influxdb3/enterprise/admin/license/)) lifts the first two but is not open source and conflicts with "design for publishing". The smallest tuning example in the docs is a 4-core/16 GB host ([tuning](https://docs.influxdata.com/influxdb3/core/admin/performance-tuning/)); Pi memory guidance is UNVERIFIED.

### 5. Prometheus (+ remote write / Pushgateway / OTLP)

Prometheus is built for pull-scraped numeric metrics, and its own docs say that "if you need 100% accuracy ... Prometheus is not a good choice" ([overview](https://prometheus.io/docs/introduction/overview/)). For the Trail it fails on the model:

- Samples are float64 only; no strings, so status names, error messages, GeoJSON and Dock origin cannot be stored as values ([data model](https://prometheus.io/docs/concepts/data_model/)).
- `x`, `y`, `theta` become three separate series that PromQL cannot join row-wise; range queries return step-aligned, lookback-filled values rather than raw samples, and only an instant query with a range vector (`mower_x{job_id="..."}[3h]`) returns stored samples, capped at 11,000 points per series ([API](https://prometheus.io/docs/prometheus/latest/querying/api/), [basics](https://prometheus.io/docs/prometheus/latest/querying/basics/), [api.go](https://github.com/prometheus/prometheus/blob/main/web/api/v1/api.go)). Whether Grafana renders that as rows is UNVERIFIED.
- Pushgateway rejects timestamps and "is not an event store" ([README](https://github.com/prometheus/pushgateway/blob/master/README.md)); remote write requires in-order samples and TSDB rejects anything older than `out_of_order_time_window` (default 0s, 30 min suggested) ([spec](https://prometheus.io/docs/specs/prw/remote_write_spec/), [config](https://github.com/prometheus/prometheus/blob/main/docs/configuration/configuration.md)), so a collector that buffers offline cannot catch up.
- `job_id` as a label is the documented anti-pattern for unbounded label values ([naming](https://prometheus.io/docs/practices/naming/)).
- No official Python remote-write client; `prometheus-client` covers exposition and Pushgateway only ([client_python](https://prometheus.github.io/client_python/)).

Prometheus is fine for the numeric per-Job/Zone metrics via a scrape endpoint on the collector, and HA exposes `/api/prometheus` the same way ([integration](https://www.home-assistant.io/integrations/prometheus/)), but that would mean two stores. ARM64 binaries and multi-arch images exist ([releases](https://api.github.com/repos/prometheus/prometheus/releases/latest), [Hub](https://hub.docker.com/r/prom/prometheus/tags)).

### 6. MariaDB (Home Assistant add-on)

The official `home-assistant/addons` MariaDB add-on runs on aarch64 ([DOCS](https://github.com/home-assistant/addons/blob/master/mariadb/DOCS.md)), and Grafana's core MySQL datasource supports MariaDB 10.5+ with `$__timeFilter` ([MySQL datasource](https://grafana.com/docs/grafana/latest/datasources/mysql/)). Same relational model as Postgres; JSON column instead of `jsonb`, no TimescaleDB-style retention or PostGIS. Worth supporting by keeping the schema portable SQL, but not worth choosing over Postgres for a fresh install.

## Comparison

| | Postgres (+Timescale) | SQLite (frser) | InfluxDB 2 | InfluxDB 3 Core | Prometheus | MariaDB |
|---|---|---|---|---|---|---|
| Trail row (x,y,theta per ts) | native | native | 3 fields, pivot in Flux | native (SQL) | 3 series, no join | native |
| Job / Zone as entities, string events | native | native | tags + `first/last` | tags (unbounded ok) | no strings | native |
| Config rows (Dock origin, Boundary) | `jsonb` | TEXT | string field, `last()` | string field | no | JSON |
| Grafana datasource | core, signed | community, signed | core, signed | core, needs HTTP/2, Grafana 12.2+ | core, signed | core, signed |
| Time macros | `$__timeFilter` | `$__from`/`$__to` only | `$timeFilter` | `$__timeFilter` | n/a | `$__timeFilter` |
| Grafana location | anywhere | same filesystem | anywhere | anywhere | anywhere | anywhere |
| ARM64 image | yes | plugin binary yes | yes | yes | yes | yes (add-on) |
| Extra process on Pi | 1 (128 MB+ default) | 0 | 1 | 1 (pyarrow client, 16 GB tuning example) | 1 | 1 |
| Retention | policy (Timescale) or cron DELETE | DELETE + VACUUM | bucket period | fixed at create | flags | cron DELETE |
| Async Python | psycopg 3 / asyncpg | aiosqlite | `InfluxDBClientAsync` | deprecated | none for remote write | aiomysql / asyncmy |
| Licence | PostgreSQL / Apache-2 + TSL | Public domain / MIT plugin | MIT | MIT/Apache-2 | Apache-2 | GPL server |
| Beside HA | recorder supports; community TimescaleDB add-on | HA's own default | integration writes; add-on deprecated | integration writes | HA scrape endpoint | official add-on |

## Ranked recommendation

1. **PostgreSQL, with TimescaleDB as an optional extension. Recommended.** It is the only candidate that models Jobs, Zones, Trails, events and config natively, uses a core signed Grafana datasource with real time macros, has a first-class asyncio Python client with `COPY`, ships `linux/arm64` images, and works whether Grafana runs on the Pi or elsewhere. Trade-off: one extra service to run (Docker or apt) and memory to cap on a 4 GB Pi. Keep the schema plain SQL; enable TimescaleDB (hypertable + `add_retention_policy` + columnstore) when a user has it, otherwise a nightly `DELETE`. Skip PostGIS: the panel georeferences via the Dock origin anyway, and PostGIS `geometry` comes back to Grafana as a string.
2. **SQLite via `frser-sqlite-datasource`. Recommended for the personal, single-box install and as the zero-ops fallback.** Same schema, no service, HA proves it copes on a Pi. Trade-offs: Grafana must share the filesystem with the collector, a community plugin maintained by one person, no `$__timeFilter`, WAL directory permissions and `busy_timeout` to get right. If the collector's schema is portable SQL, supporting both 1 and 2 is cheap; the panel plugin should accept any data frame with `time, x, y, theta` columns so the datasource is the user's choice.
3. **InfluxDB 2.9. Acceptable, not recommended for new installs.** Workable with InfluxQL, MIT, ARM64, async client, and HA's integration writes to it. Trade-offs: Jobs and config become tag/`last()` workarounds, Flux is frozen, the product line has moved to 3, and the HA add-on people "already have" is a deprecated 1.8.
4. **MariaDB. Honourable mention.** Only if the official HA add-on is already running; the schema from 1 ports with minor changes. No reason to pick it fresh.
5. **InfluxDB 3 Core. Not recommended.** The ~72-hour query window and fixed retention break multi-day dashboards; the Python client is sync-only with pyarrow; Enterprise at-home fixes the limits but is non-commercial and closed.
6. **Prometheus. Not suitable for the Trail.** No strings, no row-wise join of x/y/theta, no raw-sample retrieval in graphs, pull model versus reordered MQTT timestamps. Fine as a secondary scrape target for numeric metrics if someone wants alerting there, but not as the collector's store.

## Consequences for the spec

- Collector writes portable SQL (Postgres first, SQLite second) through one thin storage trait; batch Trail inserts per MQTT payload; timestamps stored as `timestamptz` (Postgres) / Unix seconds (SQLite, because of the plugin).
- `job_id` is collector-assigned (surrogate key or Job start time) and lives in a `job` table with `device_id`, `started_at`, `ended_at`, zone list and totals; Trail and Zone rows reference it.
- Dock origin and Boundary go in a `device`/`config` table; the panel reads them via a second query, with panel options as the source of truth per the standing decision.
- The panel plugin must not assume a datasource: it consumes data frames with `time, x, y, theta` (and optional `status`, `zone`) columns, which every SQL datasource above can produce.
- Open questions for the schema ticket (#7): chunking/retention defaults, whether to store `status`/`zone` on each Trail row or in separate tables, and the bundled dashboard's provisioning for both datasources.
