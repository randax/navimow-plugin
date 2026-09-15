#!/usr/bin/env python3
"""Capture one full mowing Job from a Navimow into a JSONL file, unfiltered.

    python tools/capture.py capture  --out fixtures/raw-2026-09-20.jsonl
    python tools/capture.py redact   fixtures/raw-2026-09-20.jsonl fixtures/job-2026-09-20.jsonl

Environment: NAVIMOW_TOKEN (bearer token for the OpenAPI), optional
NAVIMOW_API_URL (default https://navimow-fra.ninebot.com).

Every record is one JSON object per line:
    {"recv_ms": <collector clock, epoch ms>, "kind": "mqtt"|"rest"|"note"|"meta",
     "topic"|"endpoint"|"text": ..., "payload": <parsed JSON, or the raw string>}

The MQTT feed is taken from the SDK's on_raw hook, so nothing is filtered: the
SDK's own location filter (which drops the all-zero placeholder and out-of-order
points) never runs here. REST snapshots of authList and getVehicleStatus are taken
at start, then every --rest-interval seconds, then at exit. Type a line and press
Enter at any time to store a timestamped note (e.g. "straight lane, compass 92").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from mower_sdk import MowerClient, NavimowSDK, UrllibSession

API_BASE_URL = os.environ.get("NAVIMOW_API_URL", "https://navimow-fra.ninebot.com")
REST_ENDPOINTS = {
    "authList": ("GET", "/openapi/smarthome/authList", None),
    "getVehicleStatus": ("POST", "/openapi/smarthome/getVehicleStatus", "devices"),
}


def now_ms() -> int:
    return int(time.time() * 1000)


def parse(payload: bytes) -> Any:
    text = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class Recorder:
    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.counts: dict[str, int] = {}

    def write(self, kind: str, **fields: Any) -> None:
        record = {"recv_ms": now_ms(), "kind": kind, **fields}
        self.out.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.out.flush()
        key = fields.get("topic", fields.get("endpoint", kind))
        if isinstance(key, str) and "/" in key:
            key = key.rsplit("/", 1)[-1]
        self.counts[key] = self.counts.get(key, 0) + 1


async def rest_snapshot(client: MowerClient, device_ids: list[str], rec: Recorder) -> None:
    for name, (method, endpoint, body_key) in REST_ENDPOINTS.items():
        data = {body_key: [{"id": d} for d in device_ids]} if body_key else None
        try:
            # Raw response on purpose: the SDK models drop unknown keys, and the
            # data audit needs to know whether a position key exists at all.
            response = await client.api._async_request(method, endpoint, data=data)
            rec.write("rest", endpoint=name, payload=response)
        except Exception as err:  # noqa: BLE001
            rec.write("rest", endpoint=name, error=repr(err))


async def read_notes(rec: Recorder, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        if line.strip():
            rec.write("note", text=line.strip())
            print(f"  noted: {line.strip()}")


async def capture(args: argparse.Namespace) -> None:
    token = os.environ.get("NAVIMOW_TOKEN") or sys.exit("Set NAVIMOW_TOKEN")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as fh:
        rec = Recorder(fh)
        rec.write("meta", text="capture start", api=API_BASE_URL, argv=sys.argv[1:])
        async with UrllibSession() as session:
            client = MowerClient(session=session, token=token, api_base_url=API_BASE_URL)
            devices = await client.async_discover_devices()
            if not devices:
                sys.exit("No devices on this account")
            ids = [d.id for d in devices]
            for d in devices:
                print(f"device {d.name} id={d.id} model={d.model} fw={d.firmware_version} online={d.online}")
            await rest_snapshot(client, ids, rec)
            await client.async_refresh_mqtt_info()

            sdk = NavimowSDK(
                broker=client.mqtt_broker,
                port=client.mqtt_port,
                username=client.mqtt_username,
                password=client.mqtt_password,
                ws_path=client.mqtt_ws_path,
                auth_headers={"Authorization": f"Bearer {client.get_token()}"},
                records=devices,
                subscribe_location=True,
                keepalive_seconds=60,  # idle wss connections die silently after ~10 min at the default
            )
            sdk.on_raw(lambda topic, payload: rec.write("mqtt", topic=topic, payload=parse(payload)))

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            sdk.connect()
            print(f"\nrecording to {args.out}. Type a note + Enter any time. Ctrl-C to stop.\n")
            notes = asyncio.create_task(read_notes(rec, stop))
            try:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=args.rest_interval)
                    except asyncio.TimeoutError:
                        await rest_snapshot(client, ids, rec)
                        print(f"  {time.strftime('%H:%M:%S')} counts: {rec.counts}")
            finally:
                notes.cancel()
                await rest_snapshot(client, ids, rec)
                rec.write("meta", text="capture stop", counts=rec.counts)
                sdk.disconnect()
        print(f"\nstopped. records per channel: {rec.counts}")


def redact(args: argparse.Namespace) -> None:
    """Replace device ids, serials, MACs, names and credentials with placeholders."""
    lines = args.src.read_text(encoding="utf-8").splitlines()
    replacements: dict[str, str] = {}

    def learn(record: dict[str, Any]) -> None:
        if record.get("kind") == "rest" and record.get("endpoint") == "authList":
            devices = (record.get("payload") or {}).get("data", {}).get("payload", {}).get("devices", [])
            for n, dev in enumerate(devices, 1):
                for key, tag in (("id", "DEVICE"), ("serialNumber", "SERIAL"), ("macAddress", "MAC"),
                                 ("name", "NAME"), ("deviceName", "DEVNAME"), ("iotId", "IOTID")):
                    val = dev.get(key)
                    if isinstance(val, str) and len(val) >= 4:
                        replacements[val] = f"{tag}_{n}"

    for line in lines:
        learn(json.loads(line))
    if not replacements:
        sys.exit("No authList record found; cannot learn device ids. Redact by hand.")
    text = "\n".join(lines) + "\n"
    for old, new in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(old, new)
    args.dst.write_text(text, encoding="utf-8")
    print(f"wrote {args.dst} ({len(lines)} records); replaced: {sorted(set(replacements.values()))}")
    print("Check the file by eye before committing: grep for tokens, emails, and lat/lng.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out", type=Path, default=Path(f"fixtures/raw-{time.strftime('%Y-%m-%d')}.jsonl"))
    c.add_argument("--rest-interval", type=float, default=300, help="seconds between REST snapshots")
    r = sub.add_parser("redact")
    r.add_argument("src", type=Path)
    r.add_argument("dst", type=Path)
    args = p.parse_args()
    if args.cmd == "capture":
        asyncio.run(capture(args))
    else:
        redact(args)


if __name__ == "__main__":
    main()
