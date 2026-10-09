---
status: accepted
---

# The schema holds what a mower was seen to send, and nothing guessed

The collector's tables were laid out while the Job and Zone rules were being built, and
marked provisional until a real capture could say what the mower sends. That capture
(`fixtures/job-2026-09-30.jsonl.gz`) now has, and the layout stands as built, with a Job's
Zones and the mower's own details added. Nothing is stored that no capture has held: a
column cannot be renamed or removed once released, so one guessed from no data would be
kept for good. This resolves
[#9](https://github.com/randax/navimow-plugin/issues/9).

## The relational shape

PostgreSQL is the reference. TimescaleDB is the same adapter and holds the same tables and
columns, the three of readings (`trail_point`, `job_progress`, `mower_state`) as
hypertables where its extension is installed when the collector makes them. ClickHouse
holds them too, each a `ReplacingMergeTree` by the key in the table below: it holds no row
to a key itself, so the collector writes only what is not stored yet, and a consumer reads
with `FINAL`.

| Table | Key | Columns |
|---|---|---|
| `trail_point` | `mower_id, device_time` | `received_time, x, y, theta, vehicle_state, job_id, zone` |
| `job` | `mower_id, job_id` | `start_time, end_time, completed, mowing_percentage, area, arrival_x, arrival_y, arrival_theta, updated_time, zones` |
| `job_progress` | `mower_id, device_time` | `received_time, zone, zone_progress, mowing_percentage, area, week_area, job_id` |
| `mower_state` | `mower_id, device_time` | `received_time, state, battery, job_id` |
| `collector_gap` | `mower_id, start_time` | `end_time, reason` |
| `mower` | `mower_id` | `name, model, firmware, updated_time` |

- **A mower is its `mower_id`**, the identifier the vendor gives it, on every row.
- **`job_id` is the second the Job started, in UTC, as text** (ADR 0001). It is empty on a
  row from a mower at the dock.
- **`zone` is the mower's own number for the Zone**, as it reports it.
- **Every reading keeps both clocks**: `device_time` is the mower's, and with the mower
  identifies the row, so a reading delivered twice is stored once; `received_time` is the
  collector's.
- **Positions are stored as sent**: metres from the dock on the mower's own axes, `theta`
  in radians. Placing them on a map is the panel's work, from the Dock origin.
- **Normalised**: times (to timestamps), and `zone_progress` (to percent; the mower sends
  hundredths of one). **Raw**: `state` and `vehicle_state`, whose vocabularies are not
  fully known, so an unknown value is stored as it came.
- **`job.zones`** is the list of Zones the mower last sent while away on the Job
  (`partitionIds`): the Zones the Job was set to mow. It is empty until the mower has
  listed them, which it does every five minutes or so. A message of that kind with no list
  in it, as the mower sends near the dock, changes nothing.
- **A Job is told of again whenever it changes**, and a telling replaces the stored one
  unless it is older by `updated_time`. That is when the mower sent the message that last
  changed the Job, moved on where need be to a microsecond after the telling before: a
  message sent no later than the last can still change a Job, and of two tellings as of one
  moment only the order of writing would say which stands. Rows are written out of order by
  a write that live collection gave up on and the database took late, and by a buffer file
  read after the rows held in memory. The mower's times are whole milliseconds, and a
  telling is moved on a microsecond at a time, so short of a thousand tellings as of one
  millisecond it stays in that millisecond, and when the message was sent is still read
  from it.
- **`mower`** is one row per mower as the account's device list describes it, rewritten
  when the list describes it differently; `updated_time` says since when. The firmware
  matters because ADR 0001's rules were checked on one.

Nothing is stored for Coverage: the panel computes it from the Trail.

## What is not stored

- **Error events and signal strength.** The capture tool listens on every channel, and in a
  Job of four hours the mower sent nothing on `event` or `attributes`, and no message
  carried a signal strength. A mower in trouble is a `mower_state` row whose `state` is
  `Error` or `isLifted`. The collector counts any message on a channel it stores nothing
  of (`navimow_collector_unstored_messages_total`) and logs the first from each mower on
  each channel, so the first one to arrive is noticed and can be read.
- **Total mowing hours.** The mower sends no such counter; it is a query over `job`.
- **`action`, `subAction`, `mowStartType`, `taskDelay` and `mapWorkPosition`.** Nobody has
  documented what they mean and nothing would read them.
- **The raw message.** `tools/capture.py` records raw messages, and is the tool for
  finding out what a mower sends.

## Retention

Everything is kept unless the owner sets `storage.retention_days`. Rows older than that are
then removed from `trail_point`, `job_progress`, `mower_state` and `collector_gap`. `job`
and `mower` rows are never removed, so the history of Jobs outlives their Trails. On
PostgreSQL the collector removes the rows itself, once a day, taking a reading to be as
old as its `device_time` and a gap as old as its `end_time`. TimescaleDB is given the rule
to apply to its hypertables, as a retention policy that live collection keeps to the
setting, and removes old readings a chunk at a time; gaps, which are no hypertable, the
collector still removes. ClickHouse is given the rule as a TTL on all four tables, and
removes old rows as it merges. InfluxDB keeps retention on the bucket, which the owner
creates, so there the setting is refused when the configuration is read.

## The InfluxDB shape

One shape for InfluxDB 1, 2 and 3, written through `/write` or `/api/v2/write` and read
in InfluxQL, which all three take. Measurements are named as the tables and fields as the
columns.

- **Time** is `device_time`; for `job` and `collector_gap`, `start_time`; for `mower`,
  `updated_time`.
- **Tags** are `mower_id` on everything, `job_id` wherever the row names a Job, and `zone`
  on `trail_point` and `job_progress`. InfluxQL can only group by tags, and progress by
  Zone is a dashboard panel.
- **Everything else is a field.** The other times are integer epoch milliseconds, `state`
  and `reason` are strings, and `job.zones` is a string such as `1,6,7,9,10,11`.
- **A Job is written as a `job` point as well**, at its start time, and written again as
  the Job goes on. One that is not ended has an `end_time` of 0: a field cannot be unset,
  and a Job is ended and begun again around each charge.
- **A value not known is a field not written**, InfluxDB having no way to say so.

## Considered options

- **Renaming while nothing is released** (`device_id` for `mower_id`, `zone_id`, units in
  `area` and `week_area`). Rejected: the names are the glossary's, ADR 0001 describes the
  rows by them, and the panel maps columns by name with an override.
- **A `mower_event` table and a `signal_strength` column now, to be filled when a mower
  sends them.** Rejected: their layout would be a guess, and frozen once released.
- **A table of raw messages from the channels nothing is stored of.** Rejected for a log
  line and a counter, which tell the owner as much without a table to keep.
- **A raw JSON column on every row.** Rejected: it would roughly triple `trail_point` for
  what the capture tool already does.
- **The Zone list as rows of its own.** Rejected: it is a property of the Job, and never
  changed in the Job observed.
- **Retention by default**, of a year or two. Rejected: a season of positions is on the
  order of 100 to 150 MB in PostgreSQL, and a default that deletes mowing history costs the
  owner more than the disk it saves.
- **A revision number for a Job**, a column beside `updated_time` raised with every
  telling. Rejected: ClickHouse keeps the later of two rows by `updated_time` alone, which
  no added column changes, and the buffer file and InfluxDB would each have carried it
  too, for what a microsecond does.
- **An attempt given up on rolled back** instead of committed. Rejected: it leaves a
  commit already sent, and a buffer file read late.
- **A Job on InfluxDB derived by aggregating its Trail**, as the collector's spec first
  had it. Rejected: a collector starting up reads each mower's latest Job to carry on from
  it, and whether a Job completed, the area it mowed and its dock arrival pose are not in
  the Trail to derive.

## Consequences

- From here a change to the layout only adds tables and columns. A consumer reading an
  older database finds fewer of them.
- `job.zones` is an array, which PostgreSQL and ClickHouse have and InfluxDB does not:
  there it is text, and a query that wants one Zone of the list has to parse it. In
  ClickHouse, where no array can be unset, it is empty until the Zones are known.
- A Job has no `zones` for its first minutes, and one given up within them never has. The
  mower lists its Zones as it leaves the dock, but a third of a second before the state
  channel says it has left, so that list falls outside the Job; the next came 225 seconds
  later in the capture.
- A collector carries on from each mower's latest Job as storage and its buffer have it.
  One that starts while its buffer file cannot be read does not see a telling waiting
  there, and may tell the Job anew as of the same moment: whichever of the two is written
  last stands, as before #80. A revision number would not have changed that.
- The device list is read when the collector starts, so a firmware update is recorded at
  the next start, not when it happened.
- The bundled dashboard has no signal-strength panel until a mower is seen to send one.
- InfluxDB cannot do three things the relational shape does. It overwrites a point with the
  same measurement, tags and time, field by field, so a row delivered again replaces the
  one stored, where the relational shape skips it; the values are the same but for
  `received_time`. It cannot refuse an older version of a Job, so one written out of order
  is written over a newer, and what only the newer said stays; the collector writes them
  in order, and it takes an outage and a buffer replayed to do otherwise. And a row stored again under a different Job or Zone is a second point, not a
  replacement; ADR 0001's rules decide from a row's own time, so that takes the restart in
  the middle of a decision which that ADR already lists.
- In InfluxDB a mower described anew is a new point, at the time it was so described: the
  descriptions before stay, where the relational shape rewrites the one row. A mower of
  which nothing is known is no point, a point needing a field. And the collector cannot
  tell a point that was there from one that was not, so there it counts every row it
  writes as written.
