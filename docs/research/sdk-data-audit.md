# SDK data audit: Jobs, Trails and Dock origin

Resolves wayfinder ticket #4. A fact sheet for the Job-detection rules and the
collector schema, built from primary sources only. Every claim is tagged:

- **[code]** verified in source code or its test fixtures (fixtures marked as
  copied from real logs count; invented fixtures do not).
- **[obs]** a field observation written down by a maintainer in code comments,
  README or changelog, with dates and numbers. Reliable but from one or two
  mowers and firmwares.
- **[inferred]** our own reading of the above.

## Sources

| Source | Commit | Used for |
|---|---|---|
| `randax/navimow-sdk` (S) | `e9c4744`, 2026-08-30 | `mower_sdk/models.py`, `mqtt.py`, `sdk.py`, `api.py`, `docs/models.md`, `docs/realtime.md`, `docs/rest.md`, `tests/test_location.py`, `tests/test_location_fields.py` |
| `segwaynavimow/navimow-sdk` (U, official upstream) | `6596aa0`, 2026-04-10 | `mower_sdk/models.py` (origin of the `position` field) |
| `TA2k/ioBroker.navimow` (I) | `537721b`, 2026-09-14 | `main.js`, `main.test.js`, `README.md`, `lib/*.json` (the protocol notes S cites) |
| `vahesoo/NaviMower` (N) | `a6c3b2f`, 2026-09-14 | `custom_components/navimower/location.py`, `const.py`, `georeference*.py`, `docs/MAP_GEOREFERENCE_AND_UNDERLAYS.md` (independent third decoding; also talks to the private app cloud) |

All three consumers agree that none of this is officially specified: S
`docs/models.md` ("observerte gjennom ioBroker.navimow, ikkje dokumenterte"),
I `README.md` "Location" section, N `location.py` docstring.

## 1. REST `DeviceStatus.position`

**Verdict: the field exists in the SDK model but nothing in the public API
populates it. Do not plan on it for the Dock origin.**

- **[code]** S `models.py` `DeviceStatus.from_dict` sets `position=data.get("position")`
  with docstring format `{"lat": float, "lng": float}`. There is no parsing,
  no derivation, no fallback key. `DeviceStateMessage` (MQTT `state` channel)
  does the same `payload.get("position")`.
- **[code]** The field is inherited verbatim from the official upstream SDK
  (U `models.py` line 280: `position: 位置信息（可选，格式：{"lat": float, "lng": float}）`,
  line 343 `position=data.get("position")`). It is a generic model slot, not
  something derived from an observed payload.
- **[code]** The only SDK test that exercises it (S `tests/test_location.py`
  `test_state_conversion_preserves_legacy_fields`) feeds a hand-written
  `{"lat": 59.9, "lng": 10.7}` — Oslo, an invented value, not a log capture.
- **[code]** S `api.py`: `getVehicleStatus` is `POST /openapi/smarthome/getVehicleStatus`
  with `{"devices": [{"id": ...}]}`; the response is `data.payload.devices[]`
  and each entry goes straight into `DeviceStatus.from_dict`. Known real keys
  handled by the SDK and by I: `id`, `vehicleState` (string vocabulary
  `isDocked`/`isRunning`/…), `capacityRemaining[].{unit,rawValue}`,
  `descriptiveCapacityRemaining`. Nothing else is handled anywhere.
- **[obs]** I `main.js` `recordDockPosition` docstring: "The API has no endpoint
  for [the charging station] — the official SDK knows only authList,
  mqtt/userInfo, getVehicleStatus, sendCommands and responseCommands, and none
  of them carries a coordinate." I locates the dock from the last MQTT
  position the mower reported while arriving (`isDocking` -> `isDocked`),
  bounded to a reading at most 2 minutes old (`DOCK_POSITION_MAX_AGE_MS`).
- **[obs]** N does report a `device_tracker` lat/lng, but from the **private
  app cloud** ("vendor private-cloud GPS position", N `README.md` line 172 and
  `docs/MAP_GEOREFERENCE_AND_UNDERLAYS.md`), not from the openapi the SDK uses.
  That cloud is out of scope for us (map Notes: no reverse-engineering the
  app cloud).
- **[obs]** REST `getVehicleStatus` is served from a server-side cache that
  "runs a minute or two behind" and "reported isDocked for a mower that had
  been mowing for a while" (I `main.js` ~line 2479 and `STATE_CHANNEL_TRUST_MS`).
  So even if a coordinate appeared, it would be stale.

**Consequence for the standing decision** "REST `position` pre-fills the Dock
origin": with the public API there is nothing to pre-fill from. The realistic
pre-fill options are (a) user clicks the dock on the map (manual calibration,
already the source of truth), or (b) the collector's own IP/browser geolocation
as a coarse starting view. Ticket #6 should confirm by dumping one raw
`getVehicleStatus` response; if a `position`/`lat`/`lng` key does show up, this
section is wrong and the decision stands.

## 2. MQTT topics and transport

- **[code]** Topics (S `mqtt.py`, I `main.js`, N `location.py`):
  `/downlink/vehicle/{device_id}/realtimeDate/{state|event|attributes|location}`.
  `location` is opt-in in S (`subscribe_location=True`).
- **[code]** The location payload is a JSON **array** of point objects (or a
  single object; S `parse_location_payload` accepts both). One MQTT message can
  hold several points of different `type`, and I observed one message holding
  the end of one Job and the start of the next (`[80 %, 0 %]`, I `updateMowingMap`).
- **[code]** Numeric fields arrive as **strings** on the wire (`"postureX": "-0.262"`,
  `"subtotalArea": "19.11"`) — S `tests/test_location_fields.py` fixtures
  "ordrett frå loggen"; I `NUMERIC_LOCATION_FIELDS`. Integers (`type`,
  `vehicleState`, `action`, `currentMowProgress`, `mowingPercentage`) arrive as
  numbers. `subtotalArea` can be the empty string `""` (I `numericLocationFields`
  comment) — treat as null, not 0.
- **[obs]** Delivery is **reordered and sometimes very late**: positions
  routinely arrive a few seconds out of order; one type-2 progress sent 11:48
  arrived 13:34 (I `isFreshLocationReading`). S `LocationFilter` and I both keep
  a high-water mark per `(device_id, type)` and drop older readings. The
  collector must order by the payload `time`, never by arrival time.
- **[obs]** Idle connections die silently after ~10 min without a FIN on the
  wss path; I fixed it with keepalive 60 s (S still uses 2400 s). Mowing
  traffic masks it. Relevant for collector reliability, not for data.
- **[obs]** The `state` channel is pushed only on change and goes quiet while
  docked "for hours" (I header comments); N `docs/MULTI_MOWER.md`: "a docked
  mower may stop publishing continuous position packets while state and battery
  messages continue".

## 3. Location message types

Wire key -> S `DeviceLocationMessage` attribute. `type` is an int on the wire; S
stores it as `str` ("1"), so compare against `"1"` when using the SDK object.

### type 1 — pose (Trail points)

| Wire | S attr | Unit | Notes |
|---|---|---|---|
| `postureX`, `postureY` | `x`, `y` | metres, local frame | string on wire |
| `postureTheta` | `theta` | radians | string on wire |
| `vehicleState` | `vehicle_state` | int | see section 5 |
| `time` | `timestamp` | **epoch milliseconds** | see section 7 |
| `type` | `type` | `1` | |

- **[code]** Real fixture: `{"postureTheta":"1.039","postureX":"-0.262","postureY":"-0.411","time":1788087137087,"type":1,"vehicleState":2}`
  (S `POSE_CHARGING`).
- **[obs]** **Rate while mowing: one type-1 point every ~2 s** ("reports its
  position every couple of seconds", "the two seconds the positions themselves
  arrive in", I constants block; ~7237 positions in one day's log).
- **[obs]** **Rate while docked/idle: one point every ~5 min**, alternating
  between a real (drifting) pose and the all-zero placeholder (I
  `isPlaceholderPosture`, "every five minutes"). S `docs/models.md` says the
  type-3 heartbeat in the dock is ~6 min.
- **[obs]** Straight-lane density: at 2 cm tolerance half the points of a
  session are collinear with their neighbours (I `MAP_TRACK_MIN_DEVIATION_M`).
  Trail storage can be thinned aggressively without visible loss.

### type 2 — progress (Job and Zone progress)

| Wire | S attr | Unit | Notes |
|---|---|---|---|
| `mowingPercentage` | `mowing_percentage` | % of whole Job, integer steps | |
| `subtotalArea` | `subtotal_area` | m², 2 decimals | string; may be `""` |
| `mowingWeekArea` | `week_area` | m² | string |
| `currentMowBoundary` | `current_zone` | zone id | the Zone being mowed now |
| `currentMowProgress` | `zone_progress` | S divides by 100 -> % | wire is 0–10000 |
| `action`, `subAction` | `action`, `sub_action` | int | meaning unknown; see below |
| `mowStartType` | `mow_start_type` | int | 1 normally, 0 = "no task" |
| `mapWorkPosition` | not exposed (in `raw`) | hex string | packed 5× int32 BE: action, subAction, mowStartType, currentMowBoundary, currentMowProgress; `-1` = `FFFFFFFF` |
| `time`, `type` | | ms, `2` | |

- **[code]** Real fixture (S `PROGRESS_ZONE8`): `action 8, currentMowBoundary 11,
  currentMowProgress 2401, mowStartType 1, mowingPercentage 11, mowingWeekArea
  "19.11", subAction 6, subtotalArea "19.11", type 2`. Note `subtotalArea ==
  mowingWeekArea` at the start of a week.
- **[obs]** Rate: roughly **once per whole percent** of `mowingPercentage`,
  "about every two minutes" on a whole-lawn Job (I). `currentMowProgress` is
  route progress in 1/100 %, so finer than `mowingPercentage`.
- **[obs]** `mowingPercentage` follows the **planned path**, `subtotalArea`
  the **ground covered**; they are not one quantity rescaled (4.21 m² at 1.04 %
  vs 224.15 m² at 61.01 % in one session, I). `subtotalArea` is the Job's
  share of `mowingWeekArea` and tracked its rise to within 0.05 m² across a
  charging break (I).
- **[obs]** `action` values seen: 8/6 (`action`/`subAction`) -> 5 -> short `-1`
  (S `docs/models.md`); N treats `action in {5, 8}` as "normal mowing / boundary
  mowing" (`MQTT_CUTTING_ACTIONS`). `action: -1` rides on the new-task
  announcement, the first message after a charging break **and** the routine
  progress tick, so it does not discriminate anything (I).
- **[obs]** **Zone Jobs send no type-2 at all**: "A partition task sends no
  `type: 2` message whatsoever: measured 2026-08-25 over a zone run of an hour
  and a half, not one" (I). N's fixtures contradict this partly: `PROGRESS_ZONE8`
  is a type-2 *while mowing zone 8*. Firmware or task-type dependent; see open
  questions.

### type 3 — Zone list / heartbeat

- **[code]** `{"partitionIds":[10,11],"time":...,"type":3}` while a task runs;
  `{"time":...,"type":3}` without `partitionIds` as a heartbeat (S fixtures
  `PARTITIONS`, `HEARTBEAT`). `partition_ids` is `list[int] | None`.
- **[obs]** Rate ~every 5 min during a task (S docs, I), ~every 5–6 min as a
  bare heartbeat in the dock.
- **[obs]** N: `partitionIds` is the **target** zone set chosen at task start
  (absent for "mow all"); `currentMowBoundary` in type 2 is the **live**
  physical zone and updates only when the mower crosses into it. Keep them as
  two columns.

### type 4 — task delay

- **[code]** `{"taskDelay": false, "type": 4}` (S `TASK_DELAY`); N adds that it
  may carry `vehicleState` and `time`. Meaning: rain / schedule delay (N).

## 4. Coordinate frame of x/y/theta

- **[code+obs]** Local Cartesian frame in **metres**, **origin at the charging
  dock** (the mower's RTK reference). Docked readings sit at e.g.
  `(0.195, 0.062)`, `(0.329, 0.042)`, `(-0.262, -0.411)` — near but never
  exactly on the origin, because the mower does not park to the millimetre
  (I `isPlaceholderPosture`; S `POSE_CHARGING`; N: "origin is ~the dock / RTK
  reference"). So the dock is *approximately* `(0,0)`; the Dock origin should
  be calibrated as a lat/lng for local `(0,0)`, and the true dock marker may be
  drawn at the median docked pose if wanted.
- **[obs]** **theta is the mower heading measured from +X counter-clockwise,
  atan2 convention, radians** — I checked it against the direction actually
  driven over twelve samples of a straight lane, mean disagreement 0.003 rad
  (I `renderMap` comment). Observed range includes negatives (`-2.833`,
  `-2.859`), so it is in (−π, π], not [0, 2π).
- **[inferred]** The frame is right-handed with y "up" on a top-down view: I
  only flips Y for the canvas ("real-world Y grows up, canvas Y grows down")
  and N's local->East/North transform is a pure rotation with no reflection
  (`east = dx cos r + dy sin r; north = -dx sin r + dy cos r`).
- **[code]** **The axes are NOT aligned to east/north.** N fits a free rotation
  (`rotation_rad`) between local X/Y and East/North from GPS pairs, and the
  private app cloud exposes a `map_north_offset` per map (N `georeference.py`
  lines 4, 77, 93: "local -> EN therefore uses R(-rotation_rad)"). N also
  bounds the fitted scale to 0.90–1.10, i.e. local metres are real metres.
  This matches our Dock origin definition (lat/lng plus rotation of the local
  x-axis relative to north). N's sign convention for `rotation_rad` is theirs;
  ours must be fixed in the schema ticket and verified against one straight
  lane of known compass bearing in the live capture.
- **[obs]** Dock-pose **drift**: a mower standing in the dock reports a pose
  that wanders — 1.16 m over one morning in steps of 2–47 cm, heading stable at
  about −2.86 rad (I `DRIFT` fixture and comment). Trail collection must stop
  while docked or the Trail grows overnight; a Coverage layer must ignore
  docked points.
- **[obs]** Stray points: a jump > 10 m between consecutive points is not
  driving (I `LOCATION_JUMP_MAX_M`); I holds such a point until the next one
  confirms it. Real Jobs do start with a legitimate jump (adapter restart onto
  an old track, leaving the dock after a reset).

## 5. `vehicleState` on the location channel

Numeric, distinct from the string `vehicleState`/`state` vocabulary of REST
and the `state` channel (`isDocked`, `isRunning`, `isDocking`, `isPaused`,
`isLifted`, `isMapping`, `isIdle`/`isIdel`, `Error`, `inSoftwareUpdate`,
`Self-Checking`, `Offline`).

| value | S `VEHICLE_STATE_TO_STATUS` (one real session) | I observations | N `const.py` |
|---|---|---|---|
| 1 | DOCKED ("in station, fully charged") | on both all-zero placeholders "and on nothing else" | IDLE (generic idle, not necessarily docked) |
| 2 | CHARGING ("battery rising, went to 1 at 90 %") | "a docked one a 2" | DOCKED |
| 3 | — (unmapped -> UNKNOWN) | occurs | CHARGING / "coarse stopped state, must not imply docked" |
| 4 | MOWING | "a mowing position is a 4" | MOWING |
| 5 | RETURNING | occurs | RETURNING |
| 6 | — | — | MAPPING |

**[inferred]** 4 and 5 are agreed by all three. 1/2/3 disagree across mowers
and are the main thing the live capture must pin down. For Job detection use
the **string `state` channel** (`isRunning`, `isDocking`, `isDocked`) as the
primary state signal and the numeric one only as a per-point annotation; that
is also what I does (`SESSION_END_STATES = {isDocked, docked, charging}`).

**[obs]** `vehicleState` on the location channel **lags the state channel by
up to a minute**: "while the mower drives back out it still reads as docked"
(I `recordDockPosition`), and the state channel itself reports the moment of
change while REST lags 1–2 min.

## 6. How Jobs and Zones manifest (the detection rules I converged on)

I's `updateMowingMap` is 18 months of field-tested heuristics; the rules below
are theirs, re-stated in our vocabulary. They are **[obs]** unless noted.

**Job start**
1. State channel transition from a docked state (`isDocked`, or numeric 1/2)
   to an active one (`isRunning`, `isMapping`). This says "left the dock", not
   yet "new Job vs. continuing after a charge".
2. The message announcing a new task carries `subtotalArea: "0.0"` next to the
   **stale** `mowingPercentage` of the previous Job (still 100 %). The area is
   zeroed the moment a task is taken on; the percentage only falls once the
   first whole percent is mowed, "which on a large lawn is several minutes".
   So: **`subtotalArea` fall of ≥ 1 m² = new Job, decided immediately**;
   `mowingPercentage` falling = new Job, decided minutes later (second witness
   for firmwares that send no area).
3. A rise of `mowingPercentage` above the last value after leaving the dock =
   **continuation** of the Job it went to charge from (docked at 224.15 m² /
   61 %, came back at 227.26 m² / 62 %). Neither counter resets for a charge.
4. `mowStartType: 0` with `currentMowBoundary 0`, `currentMowProgress 0`,
   `mowingPercentage 0`, `subtotalArea "0.0"`, `mapWorkPosition` all
   `FFFFFFFF…` = **"no task"** message sent hours after stopping; it must not
   start a Job (I `NO_TASK` fixture, 2026-08-13 03:14). Every other type-2 in
   three days had `mowStartType: 1`.
5. For Zone Jobs (which may send no type-2 at all): a **change of
   `partitionIds`** from a previously known set = previous Job ended, new one
   started. The first `partitionIds` after a start only says which zones, not
   that they are new. If no progress has been seen for > 6 h, treat leaving the
   dock as a new Job (I `MOWING_PROGRESS_STALE_MS`).
6. Grace: for up to 5 min after leaving the dock, points are held as
   "undecided" until 2/3/5 answers; points driven since the state change belong
   to whatever the answer is (I `SESSION_START_GRACE_MS`).

**Job end**
7. State channel reaches `isDocked` (or `charging`). `isDocking`/returning and
   a short `isIdle` are **not** ends — a cancelled dock, a pause or a recovery
   can go back to `isRunning`. `isPaused`, `isLifted`, `Error`, `Offline` are
   interruptions, not ends.
8. The pose reported while still `isDocking` is the arrival pose = dock
   location; after `isDocked`, stop appending to the Trail.
9. `mowingPercentage` reaching 100 and `currentMowProgress` reaching 10000
   mark completion (N), but a Job can also end at the dock without reaching
   100 (battery, rain, user).

**Zone change inside a Job**
10. `currentMowBoundary` changes and `currentMowProgress` restarts from ~0
    (S docs: "nullstillast ved sonebyte"). `mowingPercentage` keeps rising
    across the change. **[code]** for the fields, **[obs]** for the reset.

**Weekly area**
11. `mowingWeekArea` is a week accumulator that is never reset by a Job; it is
    the natural source for the dashboard's "weekly area" and, differenced,
    for per-Job area when `subtotalArea` is absent.

## 7. Timestamps

- **[code]** `time` on the location channel is **epoch milliseconds**: real
  fixtures `1788085035337`, `1786441689297` (S `test_location_fields.py`,
  I `main.test.js`); I divides differences by 1000 to print seconds. S stores
  it as `int` without rescaling. Note S's older invented fixtures use
  seconds-looking values (`1755000000`); ignore those.
- **[code]** S accepts `timestamp` as a fallback key, but no real fixture uses it.
- **[obs]** The mower clock can run ahead of the host; I refuses to advance
  its high-water mark for readings > 5 min in the future. Store both the
  device `time` and the collector's receive time.
- **[code]** `DeviceStatus.timestamp` / `DeviceStateMessage.timestamp` come
  from a `timestamp` key that no observed REST or state payload carries;
  expect `None`.

## 8. Behaviour when positioning is lost or the mower stands still

- **[code+obs]** **All-zero placeholder**: exactly `postureX == postureY ==
  postureTheta == 0.0` is "the mower saying nothing, not saying here". Sent
  every ~5 min while standing, with `vehicleState 1` in I's log. S
  `LocationFilter` and I drop it; N (`zero_pose_sentinel`) treats it as a
  vendor placeholder that can appear "while docked while retaining an older
  geographic point". A single zero coordinate is a real reading (a mowed point
  at `(-6.586, …)` with heading exactly `0.0` exists).
- **[inferred]** There is **no observed evidence of an explicit "positioning
  lost" flag** in any channel. The candidates are: the placeholder above, a
  pause in type-1 traffic, a `state` change to `Error`/`isLifted`, or an
  `event` channel message (whose vocabulary nobody has documented). This is a
  live-capture question.
- **[obs]** A gap in type-1 traffic while `state` is active for ≥ 3 min is
  treated by I as a broken MQTT stream, not as lost positioning
  (`LOCATION_STALE_MS`).
- **[obs]** Dock drift (section 4) means a "stationary" mower still emits
  moving poses; do not use zero velocity to detect standing.

## 9. Where this leaves the collector schema (input to #8)

Columns the sources justify, per Trail point: `device_id`, `t_device_ms`,
`t_received`, `x`, `y`, `theta`, `vehicle_state_num`, plus a nullable
`job_id`. Per progress sample: `t_device_ms`, `mowing_pct`, `subtotal_area_m2`,
`week_area_m2`, `zone_id` (`currentMowBoundary`), `zone_progress_bp`
(`currentMowProgress` raw 0–10000), `action`, `sub_action`, `mow_start_type`,
`raw`. Per Zone-list sample: `t_device_ms`, `partition_ids int[]`. Per state
change: `t_received`, `state_str`, `battery`. Keep `raw` JSON on everything
until the live capture settles the unknowns.

## 10. Open questions for the live capture (#6)

1. Dump one raw `getVehicleStatus` response and one `authList` response: is
   there any `position`/`lat`/`lng`/`longitude` key at all? (Decides the
   "REST pre-fills Dock origin" decision; section 1 says no.)
2. Numeric `vehicleState` on type-1: record the value in each of docked-charging,
   docked-full, idle-off-dock, mowing, returning, paused, lifted. S/I/N disagree
   on 1/2/3 (section 5).
3. Does this mower/firmware send type-2 progress during a **Zone** Job (S
   fixture says yes for zone 8; I says never)? And does `currentMowProgress`
   restart at a Zone change while `mowingPercentage` continues?
4. Exact type-1 cadence while mowing (2 s?) and while docked (5 min?), and
   whether points are batched several per MQTT message.
5. Sign/reference of theta vs. north: drive or observe one straight lane of
   known compass bearing and compare with `theta`, to fix our Dock-origin
   rotation convention. Also confirm y is "up"/counter-clockwise-positive.
6. What appears when positioning is lost (RTK loss under trees, lifted,
   carried): placeholder, silence, `state = Error`, or an `event` message? Dump
   the `event` channel vocabulary during a Job.
7. Behaviour at the Job boundary: capture the exact sequence of `state`
   channel values and type-2 fields from `isDocked` -> start -> first percent,
   and from last lane -> `isDocking` -> `isDocked`, with timestamps, to check
   the 1-min lag and the `subtotalArea`-before-`mowingPercentage` ordering.
8. Whether `mowingWeekArea` resets on Monday 00:00 local, and whether
   `subtotalArea` survives a charging break on this firmware (I says yes).
9. Whether the `state` channel carries anything beyond `state` and `battery`
   (`signal_strength`, `error`, `position`?) — S models these keys but no
   fixture shows them.
10. Distance between the median docked pose and `(0,0)` on this mower, to
    decide whether the dock marker is drawn at the origin or at the median.
