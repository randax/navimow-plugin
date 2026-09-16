#!/usr/bin/env python3
"""Generate a PROVISIONAL synthetic capture in the `tools/capture.py` format.

    python fixtures/make_synthetic.py            # writes fixtures/synthetic-job.jsonl.gz

Why a generator and not a committed blob: every assumption below is reviewable as
code. When a real capture lands (ticket #6), diff the observed values against the
OBSERVED/INVENTED notes here, correct this script, and regenerate.

Provenance of every payload shape used:

  OBSERVED  -- copied from `tests/test_location_fields.py` in randax/navimow-sdk,
               whose docstring says the fields are "observerte i ein reell
               klippeøkt" (observed in a real mowing session): PROGRESS_ZONE8,
               PARTITIONS, HEARTBEAT, TASK_DELAY, POSE_CHARGING. Field names,
               types (numbers-as-strings vs numbers), and the epoch-millisecond
               timestamps come from there.
  OBSERVED  -- behaviours catalogued in docs/research/sdk-data-audit.md from
               TA2k/ioBroker.navimow's field notes: ~2 s pose cadence while
               mowing, ~5 min while docked, the all-zero placeholder, dock
               drift, subtotalArea falling to 0.0 at task start beside a stale
               mowingPercentage, mowingPercentage rising (not resetting) after a
               charging break, currentMowProgress restarting at a zone change,
               reordered/late delivery, mowStartType 0 meaning "no task".
  INVENTED  -- the specific lawn geometry, the ordering of events into one
               narrative session, device identifiers, battery curve, and the
               numeric vehicleState for states nobody has pinned down. The
               live capture settles these; see the ten open questions on #6.

Known-unknown, deliberately encoded as the audit describes it: vehicleState 1/2
are used for docked-full/charging and 4/5 for mowing/returning. Sources disagree
on 1/2/3. Any replay assertion that depends on those numbers is provisional.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "synthetic-job.jsonl.gz"

DEVICE = "DEVICE_1"  # matches tools/capture.py redaction placeholders
TOPIC = f"/downlink/vehicle/{DEVICE}/realtimeDate"

# OBSERVED: epoch milliseconds, taken from the real-session fixtures so the
# magnitude and era of the clock match. 1788084093268 is HEARTBEAT's `time`.
T0 = 1788084093268

rng = random.Random(20260916)
records: list[dict] = []
_clock = {"recv": T0}


def emit(kind: str, device_ms: int, **fields) -> None:
    """Append one record. `recv_ms` is the collector's clock, which normally
    trails the device clock slightly and occasionally by a lot (see late_point)."""
    recv = fields.pop("recv_ms", None)
    if recv is None:
        recv = device_ms + rng.randint(120, 900)
    _clock["recv"] = max(_clock["recv"], recv)
    records.append({"recv_ms": recv, "kind": kind, **fields})


def mqtt(channel: str, payload, device_ms: int, **kw) -> None:
    emit("mqtt", device_ms, topic=f"{TOPIC}/{channel}", payload=payload, **kw)


# --- REST snapshots -----------------------------------------------------------
# OBSERVED (by absence): the audit found no position/lat/lng key anywhere in the
# public API's responses. This fixture therefore contains none, which is what the
# "Dock origin cannot be pre-filled" decision rests on. If the real capture shows
# one, that decision is wrong and this file must change.
def auth_list(device_ms: int) -> None:
    emit(
        "rest",
        device_ms,
        endpoint="authList",
        payload={
            "code": 1,
            "data": {
                "payload": {
                    "devices": [
                        {
                            "id": DEVICE,
                            "name": "NAME_1",
                            "model": "Navimow H500E",
                            "firmwareVersion": "1.2.3",
                            "serialNumber": "SERIAL_1",
                            "macAddress": "MAC_1",
                            "online": True,
                            "productKey": "PK_1",
                            "deviceName": "DEVNAME_1",
                            "iotId": "IOTID_1",
                        }
                    ]
                }
            },
        },
    )


def vehicle_status(device_ms: int, state: str, battery: int) -> None:
    # OBSERVED: the SDK handles only these keys; `vehicleState` here is the STRING
    # vocabulary, distinct from the numeric one on the location channel.
    emit(
        "rest",
        device_ms,
        endpoint="getVehicleStatus",
        payload={
            "code": 1,
            "data": {
                "payload": {
                    "devices": [
                        {
                            "id": DEVICE,
                            "vehicleState": state,
                            "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": battery}],
                            "descriptiveCapacityRemaining": f"{battery}%",
                        }
                    ]
                }
            },
        },
    )


# --- channels -----------------------------------------------------------------
def state(device_ms: int, s: str, battery: int) -> None:
    # OBSERVED: the state channel is pushed on change only and carries `state`
    # plus `battery`. Whether it carries anything else is open question 9 on #6.
    mqtt("state", {"state": s, "battery": battery}, device_ms)


def pose(device_ms: int, x: float, y: float, theta: float, vstate: int, **kw) -> None:
    # OBSERVED: numeric pose fields arrive as STRINGS on the wire; `type` and
    # `vehicleState` arrive as numbers. Copied from POSE_CHARGING.
    mqtt(
        "location",
        [
            {
                "postureTheta": f"{theta:.3f}",
                "postureX": f"{x:.3f}",
                "postureY": f"{y:.3f}",
                "time": device_ms,
                "type": 1,
                "vehicleState": vstate,
            }
        ],
        device_ms,
        **kw,
    )


def placeholder(device_ms: int, vstate: int = 1) -> None:
    # OBSERVED: exactly zero on all three means "saying nothing", not "here".
    pose(device_ms, 0.0, 0.0, 0.0, vstate)


def progress(
    device_ms: int,
    pct: int,
    subtotal: float,
    week: float,
    zone: int,
    zone_bp: int,
    action: int = 8,
    sub_action: int = 6,
    mow_start_type: int = 1,
) -> None:
    # OBSERVED: shape and field names copied verbatim from PROGRESS_ZONE8,
    # including mapWorkPosition as packed big-endian int32s in hex.
    packed = "".join(
        f"{v & 0xFFFFFFFF:08X}" for v in (action, sub_action, mow_start_type, zone, zone_bp)
    )
    mqtt(
        "location",
        [
            {
                "action": action,
                "currentMowBoundary": zone,
                "currentMowProgress": zone_bp,
                "mapWorkPosition": packed + "0" * 88,
                "mowStartType": mow_start_type,
                "mowingPercentage": pct,
                "mowingWeekArea": f"{week:.2f}",
                "subAction": sub_action,
                "subtotalArea": f"{subtotal:.2f}",
                "time": device_ms,
                "type": 2,
            }
        ],
        device_ms,
    )


def partitions(device_ms: int, ids: list[int] | None) -> None:
    # OBSERVED: PARTITIONS / HEARTBEAT. A bare type 3 is the heartbeat.
    payload = {"time": device_ms, "type": 3}
    if ids is not None:
        payload["partitionIds"] = ids
    mqtt("location", payload, device_ms)


def note(device_ms: int, text: str) -> None:
    emit("note", device_ms, text=text)


# --- the session --------------------------------------------------------------
# INVENTED: lawn is 30 lanes 0.25 m apart (about the Navimow cutting width),
# 40 m long, split into zone 10 (lanes 0-14) and zone 11 (lanes 15-29). Local
# metres from the dock, axes not north-aligned. 30 x 0.25 x 40 = 300 m2, within
# an H500E's rated area. At 0.35 m/s a lane takes ~114 s, so the Job runs about
# an hour, which matches the ~2 s pose cadence producing a few thousand points.
LANES, LANE_GAP, LANE_LEN, STEP_S = 30, 0.25, 40.0, 2
AREA_TOTAL = LANES * LANE_GAP * LANE_LEN


def lane_points(lane: int) -> list[tuple[float, float, float]]:
    """Poses along one lane at ~0.35 m/s, alternating direction."""
    y = lane * LANE_GAP
    forward = lane % 2 == 0
    n = int(LANE_LEN / 0.7)  # 0.35 m/s * 2 s
    out = []
    for i in range(n):
        frac = i / (n - 1)
        x = frac * LANE_LEN if forward else LANE_LEN - frac * LANE_LEN
        theta = 0.0 if forward else math.pi
        out.append((x, y, theta + rng.uniform(-0.02, 0.02)))
    return out


def build() -> None:
    t = T0

    # 1. Docked, charging, before the Job. OBSERVED: ~5 min cadence, alternating
    # a drifting real pose with the all-zero placeholder; dock drift is real.
    auth_list(t)
    vehicle_status(t, "isDocked", 64)
    state(t, "isDocked", 64)
    drift_x, drift_y = -0.262, -0.411  # OBSERVED: POSE_CHARGING's exact values
    for i in range(4):
        partitions(t, None)  # bare heartbeat
        if i % 2 == 0:
            pose(t + 1000, drift_x, drift_y, 1.039, 2)  # vehicleState 2 = charging
            drift_x += rng.uniform(-0.2, 0.25)
            drift_y += rng.uniform(-0.2, 0.25)
        else:
            placeholder(t + 1000, 1)
        state(t + 2000, "isDocked", 64 + i * 8)
        t += 5 * 60_000
    vehicle_status(t, "isDocked", 96)

    # 2. Departure. OBSERVED: subtotalArea drops to 0.0 while mowingPercentage is
    # still the PREVIOUS Job's 100; this is the earliest new-Job signal.
    note(t, "left dock")
    state(t, "isRunning", 96)
    week_area = 19.11  # OBSERVED: PROGRESS_ZONE8's mowingWeekArea
    progress(t + 4000, pct=100, subtotal=0.0, week=week_area, zone=0, zone_bp=0)
    partitions(t + 9000, [10, 11])
    t += 15_000

    # 3. Mow zone 10, then zone 11. OBSERVED: currentMowProgress restarts at a
    # zone change while mowingPercentage keeps climbing.
    area = 0.0
    pct = 0
    charged_break_done = False
    late: tuple[int, float, float, float] | None = None

    for lane in range(LANES):
        zone = 10 if lane < 15 else 11
        zone_lanes = 15
        zone_lane = lane if zone == 10 else lane - 15

        # 3a. A charging break partway through zone 11. OBSERVED: the Job
        # continues afterwards; mowingPercentage rises from where it stopped.
        if zone == 11 and zone_lane == 4 and not charged_break_done:
            charged_break_done = True
            note(t, "battery low, returning to charge")
            state(t, "isDocking", 21)
            for k in range(6):  # drive home
                pose(t, 20 - k * 3, 12 - k * 2, -2.86, 5)  # vehicleState 5 = returning
                t += STEP_S * 1000
            # OBSERVED: the pose reported while still isDocking is the arrival
            # pose, and is how the dock location is learned.
            pose(t, 0.195, 0.062, -2.859, 5)
            t += 3000
            state(t, "isDocked", 20)
            vehicle_status(t, "isDocked", 20)
            for i in range(6):  # charging, 30 min
                partitions(t, None)
                if i % 2:
                    placeholder(t + 1000, 1)
                else:
                    pose(t + 1000, 0.21, 0.07, -2.86, 2)
                t += 5 * 60_000
            state(t, "isRunning", 92)
            note(t, "resumed after charge")
            # OBSERVED: no reset. Percentage and area continue upward.
            progress(t + 4000, pct=pct, subtotal=area, week=week_area + area, zone=zone, zone_bp=0)
            t += 10_000

        for idx, (x, y, theta) in enumerate(lane_points(lane)):
            # 3b. One deliberately late point: device time here, delivered ~90 s
            # later. OBSERVED: reordering and very late delivery are routine.
            if lane == 7 and idx == 10:
                late = (t, x, y, theta)
                t += STEP_S * 1000
                continue

            # 3c. A four-minute MQTT gap mid-lane, while state stays active.
            # OBSERVED: ioBroker treats >= 3 min of type-1 silence as a broken
            # stream rather than lost positioning.
            if lane == 12 and idx == 15:
                note(t, "gap begins (simulated broker drop)")
                t += 4 * 60_000
                note(t, "gap ends")
                continue

            pose(t, x, y, theta, 4)  # vehicleState 4 = mowing
            if late and t > late[0] + 90_000:
                lt, lx, ly, lth = late
                pose(lt, lx, ly, lth, 4, recv_ms=_clock["recv"] + 400)
                late = None
            t += STEP_S * 1000

        # 3d. Progress sample at the end of each lane. OBSERVED: roughly one per
        # whole percent, about every two minutes.
        area += LANE_GAP * LANE_LEN
        pct = min(99, int(100 * area / AREA_TOTAL))
        zone_bp = min(10000, int(10000 * (zone_lane + 1) / zone_lanes))
        progress(t, pct=pct, subtotal=area, week=week_area + area, zone=zone, zone_bp=zone_bp)
        if lane % 5 == 0:
            partitions(t + 1000, [10, 11])
            vehicle_status(t, "isRunning", max(20, 96 - lane * 2))

    # 4. Finish and dock.
    progress(t, pct=100, subtotal=area, week=week_area + area, zone=11, zone_bp=10000)
    state(t + 2000, "isDocking", 46)
    for k in range(8):
        pose(t, 30 - k * 4, 40 - k * 5, -2.86, 5)
        t += STEP_S * 1000
    pose(t, 0.329, 0.042, -2.859, 5)  # OBSERVED: arrival pose values from audit
    t += 3000
    note(t, "arrived dock")
    state(t, "isDocked", 45)
    vehicle_status(t, "isDocked", 45)

    # 5. Charging afterwards, then the "no task" message hours later.
    for i in range(4):
        partitions(t, None)
        if i % 2:
            placeholder(t + 1000, 1)
        else:
            pose(t + 1000, 0.33, 0.05, -2.86, 2)
        t += 5 * 60_000
    # OBSERVED: mowStartType 0 with everything zeroed and mapWorkPosition all
    # FFFFFFFF. Must NOT be read as a new Job.
    t += 3 * 3600_000
    progress(t, pct=0, subtotal=0.0, week=week_area + area, zone=0, zone_bp=0,
             action=-1, sub_action=-1, mow_start_type=0)
    mqtt("location", {"taskDelay": False, "type": 4}, t + 60_000)  # OBSERVED: TASK_DELAY
    vehicle_status(t + 120_000, "isDocked", 100)


def main() -> None:
    emit("meta", T0, text="synthetic capture start", synthetic=True,
         generator="fixtures/make_synthetic.py")
    build()
    records.sort(key=lambda r: r["recv_ms"])
    records.append({"recv_ms": records[-1]["recv_ms"] + 1, "kind": "meta",
                    "text": "synthetic capture stop", "synthetic": True})
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    kinds: dict[str, int] = {}
    for r in records:
        key = r.get("topic", r.get("endpoint", r["kind"]))
        key = key.rsplit("/", 1)[-1] if isinstance(key, str) and "/" in key else key
        kinds[key] = kinds.get(key, 0) + 1
    span = (records[-1]["recv_ms"] - records[0]["recv_ms"]) / 3600_000
    print(f"wrote {OUT} ({len(records)} records, {span:.1f} h simulated)")
    print("per channel:", kinds)


if __name__ == "__main__":
    main()
