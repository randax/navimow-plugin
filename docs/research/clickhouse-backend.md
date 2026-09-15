# Research: ClickHouse as a collector backend

Resolves [#17](https://github.com/randax/navimow-plugin/issues/17). Researched 2026-09-15 against clickhouse.com docs, ClickHouse and Grafana source repositories, PyPI, Docker Hub and the Grafana plugin catalog; every claim links to its source. Where a docs page and the shipped source disagree, the source at a tagged release is cited. Items that could not be pinned to a primary source are marked UNVERIFIED.

## Question

[#7](https://github.com/randax/navimow-plugin/issues/7) decided the collector supports ClickHouse beside PostgreSQL, TimescaleDB and InfluxDB. The storage research ([collector-storage.md](https://github.com/randax/navimow-plugin/blob/research/collector-storage/docs/research/collector-storage.md)) did not cover it. What does supporting it require: the Grafana datasource and its macros, the Python write path, the ARM64 image and memory footprint on a Raspberry Pi, the table engine for out-of-order Trail points, TTL retention, JSON columns for Boundary and Dock origin, and the licence? And is there a reason to label it "supported but not recommended on a Pi"?

## Summary

- **Grafana:** `grafana-clickhouse-datasource` 4.21.3 (2026-09-15), Grafana-signed, not bundled, needs Grafana >= 11.6. Ships a `linux-arm64` backend binary. Native protocol on port 9000 by default. `$__timeFilter` casts to second precision; use `$__timeFilter_ms` on `DateTime64` columns. Query variables use positional columns (first = value, second = text), not `__value`/`__text`.
- **Python:** `clickhouse-connect` 1.8.0 (2026-09-03), Apache-2.0, HTTP only (8123), aarch64 wheels, pure-Python fallback. Since 1.0.0 the async client is aiohttp-native (`pip install "clickhouse-connect[async]"`), no longer a thread-pool wrapper. `insert()` takes a `settings` dict, so `async_insert` can be set per call.
- **Pi:** the official `clickhouse/clickhouse-server` arm64 image requires ARMv8.2-A plus LDAPR and **names Raspberry Pi 4 as unsupported**. Pi 5 (Cortex-A76) meets the bar on paper. Docs recommend 32 GB RAM, say anything under 16 GB needs tuning, and give 2 GB as the floor "at a low rate". No `lts` Docker tag; pin `26.8`.
- **Schema:** plain `MergeTree ORDER BY (device_id, job_id, time)` accepts out-of-order rows with no configuration; `ReplacingMergeTree(updated_at)` for `job` and `config`. `TTL time + INTERVAL 1 YEAR` works on `DateTime64` from 25.6. Boundary GeoJSON as `String`.
- **Licence:** Apache-2.0 for server, plugin and client.
- **Verdict:** supported, **not recommended on a Pi**: the stock image does not run on a Pi 4 at all, the documented memory floor is a third of a 4 GB Pi 5 before Grafana and the collector, and nothing in the data (3 M rows a year) needs a column store.

## 1. Grafana datasource

**Plugin.** `grafana-clickhouse-datasource` is maintained by Grafana Labs, latest v4.21.3 released 2026-09-15, `signatureType: grafana` ([catalog API](https://grafana.com/api/plugins/grafana-clickhouse-datasource), [release](https://github.com/grafana/clickhouse-datasource/releases/tag/v4.21.3)). It is not a core plugin: install with `grafana-cli plugins install grafana-clickhouse-datasource` or by unzipping into the plugins directory ([catalog](https://grafana.com/grafana/plugins/grafana-clickhouse-datasource/?tab=installation)); `GF_INSTALL_PLUGINS` is Grafana's generic Docker mechanism and is not documented for this plugin specifically (UNVERIFIED as tested). `grafanaDependency` is `>=11.6.0-0` on main and in the 4.21.3 release ([plugin.json](https://github.com/grafana/clickhouse-datasource/blob/main/src/plugin.json)); the docs' matrix is "Grafana 11.6.0 and later: plugin v4.15+; Grafana 9.x to 11.5.x: v4.0 to v4.14" ([requirements](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/_index.md#requirements)). This is below the Grafana >= 12.4 target set by the panel-constraints research, so it imposes no extra constraint. The catalog package for 4.21.3 lists `linux-arm64` among its backend binaries ([versions API](https://grafana.com/api/plugins/grafana-clickhouse-datasource/versions/4.21.3)); GitHub releases carry no assets, distribution is catalog-only. Available in Grafana Cloud ([catalog](https://grafana.com/grafana/plugins/grafana-clickhouse-datasource/?tab=installation)). Licence Apache-2.0 ([LICENSE](https://github.com/grafana/clickhouse-datasource/blob/main/LICENSE)).

**Connection.** "The data source supports two transport protocols: Native (default) and HTTP" with ports 9000/9440 (native) and 8123/8443 (HTTP) ([configure](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/configure.md#clickhouse-protocol-support)); the default is set in code ([CHConfigEditorHooks.ts](https://github.com/grafana/clickhouse-datasource/blob/main/src/views/CHConfigEditorHooks.ts)). Driver is `clickhouse-go/v2` ([go.mod](https://github.com/grafana/clickhouse-datasource/blob/main/go.mod)). Provisioning keys are `host` (not `server`, which was the v3 name), `port`, `protocol`, `username`, `defaultDatabase`, `secure`, `tlsSkipVerify` under `jsonData`, and `password` under `secureJsonData` ([provisioning](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/configure.md#provision-the-data-source), [settings.go](https://github.com/grafana/clickhouse-datasource/blob/main/pkg/plugin/settings.go)):

```yaml
apiVersion: 1
datasources:
  - name: ClickHouse
    type: grafana-clickhouse-datasource
    jsonData: { host: clickhouse, port: 9000, protocol: native, username: grafana_reader, defaultDatabase: navimow }
    secureJsonData: { password: ... }
```

**Macros.** Expansions verbatim from [macros.go](https://github.com/grafana/clickhouse-datasource/blob/main/pkg/macros/macros.go), documented in [query-editor.md](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/query-editor.md#macros):

| Macro | Expands to |
|---|---|
| `$__timeFilter(col)` | `col >= toDateTime(<from_s>) AND col <= toDateTime(<to_s>)` |
| `$__timeFilter_ms(col)` | `col >= fromUnixTimestamp64Milli(<from_ms>) AND col <= fromUnixTimestamp64Milli(<to_ms>)` |
| `$__dateFilter(col)` | `col >= toDate('YYYY-MM-DD') AND col <= toDate('YYYY-MM-DD')` (for `Date` columns) |
| `$__dateTimeFilter(dateCol, timeCol)` / `$__dt` | both of the above, for schemas with separate `Date` and `DateTime` columns |
| `$__fromTime`, `$__toTime` (+`_ms`) | bare `toDateTime(..)` / `fromUnixTimestamp64Milli(..)` literals |
| `$__timeInterval(col)` | `toStartOfInterval(toDateTime(col), INTERVAL N second)`, N >= 1 |
| `$__timeInterval_ms(col)` | `toStartOfInterval(toDateTime64(col, 3), INTERVAL N millisecond)` |
| `$__interval_s` | dashboard interval in whole seconds, minimum 1 |
| `$__conditionalAll(cond, $var)` | `cond`, or `1=1` when the variable is empty or `$__all` (frontend-only) |

Gotchas: `$__timeFilter` and `$__timeInterval` cast to `DateTime` (seconds), so on a `DateTime64(3)` Trail column use the `_ms` variants ([ClickHouse Grafana docs](https://clickhouse.com/docs/integrations/grafana/query-builder)). Grafana's display layer truncates sub-second timestamps even though the plugin returns full precision; the workaround is a "Convert field type" transformation with a `.SSS` format ([troubleshooting](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/troubleshooting.md)). Brace notation `$__timeFilter{expr}` is allowed when the argument is an expression. Statement macros `$__columns`, `$__rateColumns`, `$__lttb(...)` replace the whole `SELECT` and are not needed here.

**Query variables.** Supported ([feature table](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/_index.md)). Column handling is positional: with two or more columns "the first column is used as the value, the second column is used as the text" ([template-variables.md](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/template-variables.md), [CHDatasource.ts](https://github.com/grafana/clickhouse-datasource/blob/main/src/data/CHDatasource.ts)), the inverse of the classic `__text, __value` order used in the Postgres example of the storage research. Variable queries go through the same macro path as panel queries, so `$__timeFilter` works with refresh "On time range change" ([CHVariableSupport.tsx](https://github.com/grafana/clickhouse-datasource/blob/main/src/data/CHVariableSupport.tsx)). Multi-value variables need `${var:singlequote}` in `IN (...)` lists, and `$__conditionalAll` must receive the bare `$var` ([template-variables.md](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/template-variables.md)). `$__searchFilter` in variable queries is an open issue ([#2102](https://github.com/grafana/clickhouse-datasource/issues/2102)).

```sql
-- Job picker variable (value, text; refresh on time range change)
SELECT job_id, formatDateTime(started_at, '%Y-%m-%d %H:%i') FROM job FINAL
WHERE device_id = '$device' AND $__timeFilter_ms(started_at) ORDER BY started_at DESC;

-- One Job's ordered Trail (format: table)
SELECT time, x, y, theta, status FROM trail
WHERE job_id = '$job' AND $__timeFilter_ms(time) ORDER BY time;
```

**Result shapes.** "Table visualizations are available for any valid ClickHouse query"; time series need a `DateTime`/`DateTime64` column aliased `time`, treated as UTC when no zone is set ([query-editor.md](https://github.com/grafana/clickhouse-datasource/blob/main/docs/sources/query-editor.md)). `ORDER BY time` is mandatory because ClickHouse returns rows in "arbitrary and non-deterministic" order without it ([ORDER BY](https://clickhouse.com/docs/sql-reference/statements/select/order-by)). The query builder's time-series mode adds a default `LIMIT 1000`, which must be set to 0 for a full Trail ([query builder](https://clickhouse.com/docs/integrations/grafana/query-builder)). Type mapping ([registry.go](https://github.com/grafana/clickhouse-datasource/blob/main/pkg/converters/registry.go)): `Date*`/`DateTime64` -> time field, `Float64` -> float64, `String`/`LowCardinality(String)`/`Enum` -> string, `JSON`, `Array`, `Map` -> Grafana JSON field. Rendering of the native `JSON` type in panels is UNVERIFIED beyond that converter entry; the ClickHouse Grafana docs describe "JSON as strings" with alias columns as the supported pattern ([config](https://clickhouse.com/docs/integrations/grafana/config)).

## 2. Python client

`clickhouse-connect` 1.8.0, released 2026-09-03, Python >= 3.10 < 3.15, Apache-2.0 ([PyPI](https://pypi.org/project/clickhouse-connect/), [LICENSE](https://github.com/ClickHouse/clickhouse-connect/blob/main/LICENSE), [CHANGELOG](https://github.com/ClickHouse/clickhouse-connect/blob/main/CHANGELOG.md)). Required dependencies are `certifi`, `urllib3`, `lz4` and `backports.zstd` (below 3.14); optional extras `async` (aiohttp), `arrow`, `pandas`, `numpy`. Wheels exist for `manylinux_2_17_aarch64` and `musllinux_1_2_aarch64` on every supported CPython ([PyPI files](https://pypi.org/project/clickhouse-connect/#files)); the Cython extensions are optional and "a pure Python path remains available on platforms where the extensions cannot be built" ([docs](https://clickhouse.com/docs/integrations/python)).

**Protocol.** "The standard ClickHouse Connect clients use the HTTP interface" ([docs](https://clickhouse.com/docs/integrations/python)): port 8123/8443, no native TCP. So the collector needs 8123 and Grafana (native by default) needs 9000, or Grafana is switched to HTTP so only one port is exposed.

**Async path.** History matters here because most write-ups are stale ([CHANGELOG](https://github.com/ClickHouse/clickhouse-connect/blob/main/CHANGELOG.md)): 0.7.16 (2024-07) added `AsyncClient` as a `run_in_executor` wrapper; 0.12.0rc1 (2026-02) implemented "a native async client"; 1.0.0 (2026-05) removed the executor wrapper, so `get_async_client()` now "creates a native aiohttp-based async client directly". Current docs: install `clickhouse-connect[async]`, `await clickhouse_connect.get_async_client(...)`, then `query`, `command`, `insert`, `insert_df`, `insert_arrow` are coroutines; "CPU-bound Native-format parsing may run in an executor so that it doesn't block the event loop"; session ids are disabled by default so coroutines can share a client ([advanced usage](https://clickhouse.com/docs/integrations/language-clients/python/advanced-usage)). Alternatives are not ClickHouse-official: `asynch` (native TCP, asyncio, Apache-2.0; [PyPI](https://pypi.org/project/asynch/)), `aiochclient` (HTTP, MIT; [PyPI](https://pypi.org/project/aiochclient/)); with an aiohttp-native official client there is no reason to use them.

**Batching.** `insert(table, data, column_names=..., column_type_names=..., settings=...)` takes row- or column-oriented Python sequences; passing `column_type_names` or reusing an `InsertContext` avoids a metadata pre-query per insert ([driver API](https://clickhouse.com/docs/integrations/language-clients/python/driver-api), [client.py](https://github.com/ClickHouse/clickhouse-connect/blob/main/clickhouse_connect/driver/client.py)). "All key ClickHouse Connect Client 'insert' and 'select' methods accept an optional `settings` keyword argument to pass ClickHouse server user settings", so `settings={'async_insert': 1, 'wait_for_async_insert': 1}` works per call. Server-side guidance: "We recommend inserting data in batches of at least 1,000 rows, and ideally between 10,000-100,000 rows" and "keeping the number of insert queries around one insert query per second" ([insert strategy](https://clickhouse.com/docs/best-practices/selecting-an-insert-strategy)); too many small inserts hit `parts_to_delay_insert` (1000 parts per partition) and `parts_to_throw_insert` (3000) ([MergeTreeSettings.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Storages/MergeTree/MergeTreeSettings.cpp), [Too many parts](https://clickhouse.com/docs/knowledgebase/exception-too-many-parts)). At ~1 Trail point per 2 s the collector cannot reach 1,000 rows per batch during a Job, so it should either buffer and flush every few seconds or lean on async inserts, where the server buffers and flushes. Async-insert defaults from [Settings.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Core/Settings.cpp): `async_insert` is **on by default from 26.2** ([SettingsChangesHistory.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Core/SettingsChangesHistory.cpp)) and off in 25.8 LTS; `wait_for_async_insert=1`; `async_insert_max_data_size` 10 MiB in OSS (the [docs page](https://clickhouse.com/docs/optimize/asynchronous-inserts) quotes the 100 MiB Cloud default); `async_insert_busy_timeout_max_ms` 200 with adaptive timeout. The docs' "strong recommendation is to use `async_insert=1,wait_for_async_insert=1`". Set both explicitly so behaviour is the same on 25.8 and 26.x.

**Types.** Python `datetime` maps to `DateTime` at second resolution unless the column is `DateTime64(3)`; attach `tzinfo` to fix the instant (`naive_datetime_insert` defaults to the process timezone; [additional options](https://clickhouse.com/docs/integrations/language-clients/python/additional-options)). Since the wire gives epoch ms, insert `datetime.fromtimestamp(ms / 1000, tz=UTC)` into `DateTime64(3, 'UTC')`. JSON columns accept a dict or a JSON string on insert and read back as dicts; typed JSON paths arrived in 1.8.0 ([additional options](https://clickhouse.com/docs/integrations/language-clients/python/additional-options), [CHANGELOG](https://github.com/ClickHouse/clickhouse-connect/blob/main/CHANGELOG.md)).

## 3. Raspberry Pi footprint

**CPU.** The image README, also rendered in the install docs, is explicit ([docker/server/README.md](https://github.com/ClickHouse/ClickHouse/blob/master/docker/server/README.md), [install/docker](https://clickhouse.com/docs/install/docker)):

> The arm64 image requires support for the ARMv8.2-A architecture and additionally the Load-Acquire RCpc register. The register is optional in version ARMv8.2-A and mandatory in ARMv8.3-A. Supported in Graviton >=2, Azure and GCP instances. Examples for unsupported devices are Raspberry Pi 4 (ARMv8.0-A) and Jetson AGX Xavier/Orin (ARMv8.2-A).

Pi 4 users hit `Illegal instruction (core dumped)` with the official image ([#50852](https://github.com/ClickHouse/ClickHouse/issues/50852), [#66021](https://github.com/ClickHouse/ClickHouse/issues/66021)); the maintainers' answer was to skip Docker and use `curl https://clickhouse.com/ | sh`, which reads `/proc/cpuinfo` and downloads `aarch64v80compat` when `lrcpc`/`atomics` are missing ([install script](https://clickhouse.com/), lines 26-37). That script fetches `https://builds.clickhouse.com/master/<arch>/clickhouse`, a master build, not a release; a versioned compat binary in the tgz/deb repositories was not found (UNVERIFIED), and the GitHub release assets carry only standard `aarch64` RPMs ([v26.8.5.13-lts](https://github.com/ClickHouse/ClickHouse/releases/tag/v26.8.5.13-lts)). The compat build is selected by CMake `NO_ARMV81_OR_HIGHER` ([PR #41610](https://github.com/ClickHouse/ClickHouse/pull/41610)). Pi 5's Cortex-A76 implements ARMv8.2-A with ARMv8.3-A LDAPR ([Wikipedia](https://en.wikipedia.org/wiki/ARM_Cortex-A76), secondary source), and Altinity ran a replicated cluster on three Pi 5 8 GB boards in 2025 while warning "you may struggle ... on 4GB or less" ([Altinity blog](https://altinity.com/blog/creating-a-clickhouse-cluster-on-raspberry-pis), secondary). No ClickHouse-published test on a Pi 5 exists (UNVERIFIED in practice).

**Image.** `clickhouse/clickhouse-server` publishes multi-arch `amd64` + `arm64` manifests for every tag; `latest` = `26.8.5.13` (2026-09-15), about 249 MiB compressed for arm64, `-alpine` and `-distroless` variants about 220 MiB ([Docker Hub tags](https://hub.docker.com/r/clickhouse/clickhouse-server/tags)). There is **no `lts` tag** (Docker Hub returns 404 for it); the LTS lines are 26.8 (newest), 26.3 and 25.8 ([releases](https://github.com/ClickHouse/ClickHouse/releases)), so pin `clickhouse/clickhouse-server:26.8`. The only run flag the README asks for is `--ulimit nofile=262144:262144`.

**Memory.** The docs are blunt ([operations/tips](https://clickhouse.com/docs/operations/tips)): "The recommended amount of RAM is 32 GB or more", "If your system has less than 16 GB of RAM, you may experience various memory exceptions because default settings do not match this amount of memory", and "You can use ClickHouse in a system with a small amount of RAM (as low as 2 GB), but these setups require additional tuning and can only ingest at a low rate." The sizing guide adds "total memory shouldn't be below 8GB" ([sizing](https://clickhouse.com/docs/guides/sizing-and-hardware-recommendations)). No idle RSS figure is published (UNVERIFIED; issue threads such as [#60744](https://github.com/ClickHouse/ClickHouse/issues/60744) show a 1.5 GB container tripping `MEMORY_LIMIT_EXCEEDED` under load). Settings to cap it, defaults from [ServerSettings.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Core/ServerSettings.cpp) and [Settings.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Core/Settings.cpp), recipe from the "Using less than 16GB of RAM" section of the tips page:

| Setting | Default | On a Pi |
|---|---|---|
| `max_server_memory_usage_to_ram_ratio` | 0.9 of available RAM, cgroup-aware when the container has a limit | keep; also `docker run --memory=1g` (ClickHouse recomputes its hard limit from the cgroup) |
| `max_server_memory_usage` | 0 (unlimited, bounded by the ratio) | set an absolute cap if not using cgroups |
| `mark_cache_size` | 5 GiB | 500 MB, "cannot be set to zero" |
| `index_mark_cache_size` | 5 GiB | lower similarly |
| `uncompressed_cache_size` | 0 | leave off |
| `max_threads` (user) | number of cores | 1 |
| `max_block_size` (user) | 65409 | 8192 |
| `input_format_parallel_parsing`, `output_format_parallel_formatting` | 1 | 0 |
| `background_pool_size` | 16 | lower (merges run on `background_pool_size` x `background_merges_mutations_concurrency_ratio` 2 slots) |
| `asynchronous_metric_log`, `metric_log`, `text_log`, `trace_log` | on | disable, "it keeps the background merge task reserving RAM" |
| `max_memory_usage` (user) | 0 | e.g. 512 MiB per query |

`clickhouse-local` and chDB are in-process engines with no server endpoint for Grafana, so they are not backends here ([clickhouse-local](https://clickhouse.com/docs/operations/utilities/clickhouse-local), [chDB](https://clickhouse.com/docs/chdb)).

## 4. Table engine and keys

**Out-of-order points.** MergeTree sorts each inserted block by the sorting key into its own part and merges parts in the background; "the merge mechanism does not guarantee that all rows with the same primary key will be in the same data part" and nothing requires monotonic insert time ([MergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree), [parts](https://clickhouse.com/docs/managing-data/core-concepts/parts)). Late MQTT points need no setting; this confirms #7's assumption.

**ORDER BY.** Put filter columns first, in ascending cardinality ([choosing a primary key](https://clickhouse.com/docs/best-practices/choosing-a-primary-key), [sparse primary indexes](https://clickhouse.com/docs/optimize/sparse-primary-indexes)); `index_granularity` 8192. For Trail: `ORDER BY (device_id, job_id, time)`. Ordering keys cannot be added later.

**PARTITION BY.** "In most cases, you don't need a partition key, and if you do ... generally you do not need a partition key more granular than by month. Partitioning does not speed up queries" ([MergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree)); partitioning is "a data management technique", target under 100-1,000 partitions ([partitioning key](https://clickhouse.com/docs/best-practices/choosing-a-partitioning-key)). `PARTITION BY toYYYYMM(time)` gives 12 partitions a year and makes TTL a whole-part drop.

**MergeTree vs ReplacingMergeTree.** ReplacingMergeTree "removes duplicate entries with the same sorting key value" but only at merge time, "so you can't plan for it ... it does not guarantee the absence of duplicates"; correct reads need `FINAL`, which has "a small performance overhead" and disables `PREWHERE` when the filter is not on a key column ([ReplacingMergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree), [guide](https://clickhouse.com/docs/guides/replacing-merge-tree)). `optimize_on_insert=1` (default) already collapses duplicates inside one insert block. Recommendation:

- `trail`: plain `MergeTree`. Duplicate points are rare, harmless on a plot, and the ORDER BY would otherwise double as a uniqueness key that collapses two distinct points at the same millisecond. If exact dedup is ever wanted, `ORDER BY time LIMIT 1 BY device_id, job_id, time` at query time ([LIMIT BY](https://clickhouse.com/docs/sql-reference/statements/select/limit-by)).
- `job`, `config`: `ReplacingMergeTree(updated_at)` with `ORDER BY (device_id, job_id)` / `ORDER BY device_id`; `ver` may be `DateTime64`. These tables are tiny, so `SELECT ... FINAL` is free, or use `argMax(col, updated_at) GROUP BY key` ([argMax](https://clickhouse.com/docs/sql-reference/aggregate-functions/reference/argmax)). This is how the collector "updates" `ended_at` and totals when a Job closes.
- Avoid `ALTER TABLE ... UPDATE` mutations ("should thus not be used for high numbers of small changes"); the lightweight `UPDATE` statement is Beta from 25.8 and cannot touch key columns ([UPDATE](https://clickhouse.com/docs/sql-reference/statements/update)).
- Insert-block dedup on a single node needs `non_replicated_deduplication_window > 0` (default 0) and only catches byte-identical resent blocks ([merge-tree settings](https://clickhouse.com/docs/operations/settings/merge-tree-settings)); not a substitute for either pattern above.

## 5. TTL retention

`TTL time + INTERVAL 1 YEAR DELETE` at table level; expired rows go "when ClickHouse merges data parts", re-checked every `merge_with_ttl_timeout` = 14400 s (4 h), `OPTIMIZE TABLE trail FINAL` forces it ([MergeTree TTL](https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree#table_engine-mergetree-ttl), [TTL guide](https://clickhouse.com/docs/guides/developer/ttl)). With month partitions set `ttl_only_drop_parts = 1` so expiry drops whole parts ([MergeTreeSettings.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Storages/MergeTree/MergeTreeSettings.cpp)). The MergeTree page still says the TTL expression "must result in a `Date` or `DateTime`", but [PR #80710](https://github.com/ClickHouse/ClickHouse/pull/80710) (merged 2025-05-24, first in 25.6) added `Date32` and `DateTime64`; the check in [TTLDescription.cpp](https://github.com/ClickHouse/ClickHouse/blob/master/src/Storages/TTLDescription.cpp) now reads "should have Date, Date32, DateTime or DateTime64 type". On anything older than 25.6 write `TTL toDateTime(time) + INTERVAL 1 YEAR`. Minimum version for the schema: **25.6**, practically **25.8 LTS or 26.8 LTS**.

## 6. Column types, Boundary and Dock origin

- `time DateTime64(3, 'UTC')`: Int64 ticks since epoch, precision 3 = milliseconds, matching the wire's epoch ms ([DateTime64](https://clickhouse.com/docs/sql-reference/data-types/datetime64)).
- `x`, `y`, `theta Float64`.
- `device_id`, `status`, `level`, `zone LowCardinality(String)`: dictionary encoding pays off under ~10,000 distinct values ([LowCardinality](https://clickhouse.com/docs/sql-reference/data-types/lowcardinality), [select data types](https://clickhouse.com/docs/best-practices/select-data-types)). `job_id` as `UUID` or `String`, not LowCardinality (unbounded).
- Avoid `Nullable`: "almost always negatively affects performance"; use defaults (`ended_at DateTime64(3) DEFAULT 0`) ([Nullable](https://clickhouse.com/docs/sql-reference/data-types/nullable)).
- Boundary GeoJSON and any Dock-origin blob as `String`. The `JSON` type is production-ready from 25.3 ([JSON](https://clickhouse.com/docs/sql-reference/data-types/newjson)) but its own docs say "reading entire documents is also slower than String alternatives", the panel reads the whole document, Grafana renders it as an opaque JSON field, and the ClickHouse Grafana docs describe "JSON as strings" as the supported pattern ([config](https://clickhouse.com/docs/integrations/grafana/config)). Dock origin itself is three numbers: store `dock_lat`, `dock_lon`, `rotation_rad Float64` as columns. `JSONExtract*` is available if a field ever needs querying ([JSON functions](https://clickhouse.com/docs/sql-reference/functions/json-functions)).

Sketch for the schema ticket:

```sql
CREATE TABLE trail (
  device_id LowCardinality(String), job_id UUID, time DateTime64(3, 'UTC'),
  x Float64, y Float64, theta Float64, status LowCardinality(String), zone LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(time) ORDER BY (device_id, job_id, time)
TTL time + INTERVAL 1 YEAR DELETE SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE job (
  device_id LowCardinality(String), job_id UUID, started_at DateTime64(3, 'UTC'),
  ended_at DateTime64(3, 'UTC') DEFAULT 0, total_area Float64 DEFAULT 0, updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (device_id, job_id);

CREATE TABLE config (
  device_id LowCardinality(String), dock_lat Float64, dock_lon Float64, rotation_rad Float64,
  boundary String, updated_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY device_id;
```

## 7. Licence

Apache-2.0 for ClickHouse server ([LICENSE](https://github.com/ClickHouse/ClickHouse/blob/master/LICENSE)), the Grafana datasource ([LICENSE](https://github.com/grafana/clickhouse-datasource/blob/main/LICENSE)) and clickhouse-connect ([LICENSE](https://github.com/ClickHouse/clickhouse-connect/blob/main/LICENSE)). No restriction on bundling dashboards or publishing the collector.

## Comparison with the reference backend

| | PostgreSQL (reference) | ClickHouse |
|---|---|---|
| Grafana datasource | core, signed | Grafana-signed, separate install, Grafana >= 11.6 |
| Time macro on ms column | `$__timeFilter` | `$__timeFilter_ms` (`$__timeFilter` truncates to seconds) |
| Query variable columns | `__value`, `__text` by name | positional: value, text |
| Update a row (Job end) | `UPDATE` | `ReplacingMergeTree(ver)` + `FINAL` |
| Out-of-order points | native | native (MergeTree) |
| Retention | Timescale policy or `DELETE` | table `TTL`, applied at merge, 4 h cadence |
| Write cadence | per point fine | batch or `async_insert`; 1 insert/s guidance |
| Async Python | psycopg 3 / asyncpg | clickhouse-connect 1.x (aiohttp, HTTP) |
| ARM64 image | yes, Pi 4 and 5 | yes, **Pi 4 explicitly unsupported**, Pi 5 on paper |
| Documented RAM floor | none (128 MB `shared_buffers` default) | 2 GB "at a low rate", 32 GB recommended |
| Licence | PostgreSQL | Apache-2.0 |

## Recommendation

**Support ClickHouse as decided in #7, label it "supported, not recommended on a Raspberry Pi".** Reasons, each sourced above:

1. The stock arm64 image will not start on a Pi 4; the only route is a master-build compat binary with no pinned release.
2. ClickHouse's own docs put the untuned floor at 16 GB and the tuned floor at 2 GB, on a box that also runs Grafana and the collector, and Altinity's Pi 5 experience says 4 GB is a struggle.
3. Nothing in the workload (3 M rows a year, one mower) benefits from a column store; ClickHouse's insert guidance (batches of thousands, one insert per second) works against a 0.5 Hz stream and needs `async_insert` to be comfortable.
4. The Grafana plugin is a separate install below the project's Grafana 12.4 floor, so it adds no version constraint but does add a provisioning step and macro differences the bundled dashboard variant must carry.

Where ClickHouse already runs on real hardware (a home server, not a Pi), it is a good fit: out-of-order inserts and TTL are native, the async client is first-class, and the schema is three small DDL statements.

## Consequences for the schema ticket

- Minimum ClickHouse 25.6 (TTL on `DateTime64`); document and test against `clickhouse/clickhouse-server:26.8` (LTS) and `25.8`; no `lts` tag exists.
- Trail `MergeTree`, `job`/`config` `ReplacingMergeTree(updated_at)`; the collector writes Job end as a new row, never an `UPDATE`. Reads on `job`/`config` use `FINAL`.
- Collector buffers Trail points and flushes per MQTT payload or every few seconds via `AsyncClient.insert(..., settings={'async_insert': 1, 'wait_for_async_insert': 1})`, timestamps as tz-aware `datetime` into `DateTime64(3, 'UTC')`.
- ClickHouse dashboard variant: `$__timeFilter_ms`, positional variable columns, `'$job'` quoted (UUID/String), `ORDER BY time`, query-builder limit 0.
- Deployment doc: HTTP 8123 for the collector, native 9000 (or HTTP) for Grafana, `--ulimit nofile=262144:262144`, a memory cap via `--memory` plus the low-RAM settings table in section 3, and the Pi 4 warning.
- UNVERIFIED items to confirm on hardware before promising Pi 5 support: idle RSS with the low-RAM settings; that the arm64 image starts on a Cortex-A76; whether a versioned `aarch64v80compat` artifact exists for Pi 4.
