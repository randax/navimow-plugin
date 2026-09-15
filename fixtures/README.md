# Fixtures

Raw captures from a real Navimow, used by the collector's Job detection rules,
the schema design, and the Coverage prototype. Produced with `tools/capture.py`.

- `raw-*.jsonl` is the unredacted capture. **Do not commit it** (gitignored).
- `job-*.jsonl` is the redacted version (`tools/capture.py redact`). Commit this.

Record format: one JSON object per line with `recv_ms` (collector clock),
`kind` (`mqtt`, `rest`, `note`, `meta`) and the payload. MQTT records are the
raw broker payloads, unfiltered.
