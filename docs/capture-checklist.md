# Capture checklist: one full Job

Goal: record everything the mower sends during one complete Job (dock, mow,
return) so the collector's Job detection rules and schema rest on your firmware,
not on other people's mowers. Takes one mow plus five minutes of setup.

## Before the mow

This is written for whoever operates the mower; no repo clone is needed.

1. Python 3.10+ on any machine that will stay awake for the whole mow (a laptop
   on the same Wi-Fi is fine; the data comes from the cloud, not the mower).
   ```bash
   mkdir navimow-capture && cd navimow-capture
   python3 -m venv .venv && source .venv/bin/activate
   pip install randax-navimow-sdk
   curl -fsSLO https://raw.githubusercontent.com/randax/navimow-plugin/main/tools/capture.py
   ```
   On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and fetch the
   script with `Invoke-WebRequest ... -OutFile capture.py`.
2. Export the account's OpenAPI bearer token (the one the Home Assistant
   integration or earlier SDK scripts use; the account owner supplies it). If the
   region is not Frankfurt, also set the API URL.
   ```bash
   export NAVIMOW_TOKEN=...
   # export NAVIMOW_API_URL=https://navimow-fra.ninebot.com
   ```
3. Start recording **while the mower is still docked and fully charged**, ideally
   ten minutes before the scheduled start, so the capture contains docked
   heartbeats and the exact departure sequence:
   ```bash
   python capture.py capture
   ```
   It prints the device list and writes `fixtures/raw-<date>.jsonl` in the
   current folder.

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
2. Redact, then send the redacted file back to the project owner:
   ```bash
   python capture.py redact fixtures/raw-<date>.jsonl job-<date>.jsonl
   ```
   Skim `job-<date>.jsonl` for anything personal first. Positions are metres
   relative to the dock, not coordinates, so they are safe to share. Keep or
   delete the raw file; do not send it.
3. Project owner: commit the file as `fixtures/job-<date>.jsonl`, then run
   `/wayfinder` naming the capture task; that session resolves it from the data.

## Troubleshooting

- Nothing arrives for minutes while mowing: the token may be expired
  (REST snapshot records will show an error). Refresh it and restart; the file
  is appended to, not overwritten.
- "Request too frequent" on start: the MQTT info endpoint is rate-limited to one
  call a minute. Wait a minute and rerun.
