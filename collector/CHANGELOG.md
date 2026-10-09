# Changelog

## Unreleased

- Records every mower on a Navimow account into PostgreSQL, live and unattended: positions, progress reports, state and battery, and each mower's name, model and firmware.
- Works out where each Job begins and ends and which Zone each position was mowed in. A return to the dock to charge stays inside its Job.
- Signs in once with a normal Navimow account, through a local browser or by pasting a code on a headless machine, and refreshes its own credentials from then on. A login that Navimow rejects is logged with the command that renews it, and never stops the collector.
- Reconnects to the broker by itself, and records every reconnection and restart as a gap, so that a gap in a Trail is drawn as one.
- Holds rows in memory and then on disk while the database is away, and writes them when it returns. A database that is slow, or stops answering altogether, holds up neither collection nor a stop.
- Serves `/health` for a container or service manager, and `/metrics` in the Prometheus text format.
- Creates and migrates its own tables, unless told not to. The schema only ever gains tables and columns.
- Keeps everything by default. With `storage.retention_days` set, positions, progress reports, states and gaps older than that are removed once a day; Jobs and mowers are always kept.
- Replays a raw capture through the same ingestion as live collection.
- Released as a container image for 64-bit x86 and ARM and as `navimow-collector` on the package index, with a sample systemd unit.
