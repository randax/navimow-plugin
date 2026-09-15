# Capture checklist: one full Job

Goal: record everything the mower sends during one complete Job (dock, mow,
return) so the collector's Job detection rules and schema rest on your firmware,
not on other people's mowers. Takes one mow plus five minutes of setup.

## Before the mow

1. Python 3.10+ on any machine that will stay awake for the whole mow.
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install randax-navimow-sdk
   ```
2. Export your OpenAPI bearer token (the same one your Home Assistant integration
   or earlier SDK scripts use). If your region is not Frankfurt, also set the API URL.
   ```bash
   export NAVIMOW_TOKEN=...
   # export NAVIMOW_API_URL=https://navimow-fra.ninebot.com
   ```
3. Start recording **while the mower is still docked and fully charged**, ideally
   ten minutes before the scheduled start, so the capture contains docked
   heartbeats and the exact departure sequence:
   ```bash
   python tools/capture.py capture
   ```
   It prints the device list and writes `fixtures/raw-<date>.jsonl`.

## During the mow

Type a line and press Enter to store a note with a timestamp. The notes we need:

- `left dock` when you see it drive out.
- `straight lane compass <deg>` once: pick one long straight lane, read the
  compass bearing the mower drives along from your phone, and note it. This
  fixes the rotation convention between the mower's axes and north.
- `lifted` / `put down` if you lift it briefly (optional, but it answers what the
  stream does when positioning is lost).
- `paused` / `resumed` if you pause from the app (optional).
- `arrived dock` when it parks.

If the mower is set up with zones, a Job that covers two or more zones is the
most valuable capture. A "mow all" Job is fine too.

## After the mow

1. Leave it recording for **ten more minutes** after it docks, so the capture has
   the charging heartbeats and the REST snapshot after the Job. Then Ctrl-C.
2. Redact and commit:
   ```bash
   python tools/capture.py redact fixtures/raw-<date>.jsonl fixtures/job-<date>.jsonl
   git add fixtures/job-<date>.jsonl && git commit -m "Add captured Job fixture"
   ```
   Skim the redacted file for anything personal before committing. Positions
   are metres relative to your dock, not coordinates, so they are safe.
3. Tell the next `/wayfinder` session the capture is in; it will resolve the
   ticket and record what the data showed.

## Troubleshooting

- Nothing arrives for minutes while mowing: the token may be expired
  (REST snapshot records will show an error). Refresh it and restart; the file
  is appended to, not overwritten.
- "Request too frequent" on start: the MQTT info endpoint is rate-limited to one
  call a minute. Wait a minute and rerun.
