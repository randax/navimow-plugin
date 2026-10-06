---
status: accepted
---

# Jobs and Zones are decided from the area mowed and the state channel

Nothing the mower sends names a Job, so the collector decides where each one begins and ends
and names it itself. It decides from two signals: the **state channel** says when the mower
leaves the dock and when it is back, and the **area mowed in the Job** (`subtotalArea` in a
progress report) says whether a departure takes up the Job the mower left off or starts
another. Everything else on the wire annotates. The rules were checked against the first real
capture (`fixtures/job-2026-09-30.jsonl.gz`, an X420 on firmware 005D: one Job over six Zones
with a charging break at 76 %), and resolve
[#8](https://github.com/randax/navimow-plugin/issues/8).

## The rules

A Job is **away** from the moment the mower leaves the dock until it is back, and a mower can
be away on one Job at a time.

**A Job starts** when:

- the state channel says `isRunning` and the mower is not away, unless that resumes a Job
  (below); or
- a progress report's area is back at zero when the latest Job has reported progress, or has
  fallen by more than a square metre from that Job's. This is how the mower announces a Job:
  with the area at zero, in the same second as it leaves the dock. It holds whether or not
  the departure was seen; or
- a progress report arrives and there is no Job it could belong to: none is known at all
  (collection began mid-Job), or the latest has nothing left to mow and the report was sent
  after it ended.

**A Job resumes**, rather than a new one starting, when the mower leaves the dock with the
Job unfinished: its percentage reported, and below 100. That is a charging break, or a wait
for rain to pass, however long it lasts; the real break lasted 63 minutes. The area and the
percentage carry on from where they stood, and that is what tells a resumed Job from a new
one: a new one is announced with its area at zero, which starts it even after the departure
was taken for a resume.

**A Job ends** when the state channel says `isDocked`, unless the mower has reported progress
since that was sent. The end is provisional while the Job is unfinished, since a charging
break looks the same. `isDocking` does not end it (a return
can be called off), and neither do `isPaused`, `isLifted`, `Error`, `Offline` or `isIdle`: an
interrupted Job stays away until the mower is at the dock, however long that takes.

**A Job is completed** once its percentage reaches 100. One that ends without that was
interrupted: by a low battery, rain, an error, or the owner.

**A Job given up** for another, which is to say one still away when the next is announced,
ends at the last thing recorded of it, as a rule its last progress report; or, if the mower
had only just left the dock and not yet reported on it, when it docked.

**The Zone** is the one named by the latest progress report (`currentMowBoundary`). It
changes when that identifier changes, and for no other reason: Zone progress also starts
over after a charging break, in the same Zone. From the moment the mower turns for the dock
(`isDocking`) until its next report, it is in no Zone, so the drive back across the lawn is
not counted as mowing whichever Zone it left.

**The announcement is believed for its area only.** A report with an area of zero carries a
percentage and a Zone left over from the Job before: 100 % in the audit's field notes, Zone 1
in the real capture, whose Job began in Zone 11. It is not stored as progress. A report with
`mowStartType: 0` is the mower saying it is on no Job, and is ignored altogether.

**Every stored row names the Job the mower was away on at the row's own time**, not at its
arrival: Trail points (with their Zone), progress reports and states. At the dock, including
during a charging break, a row names none. A message delivered after a newer one of its kind
still gets stored in its place, but decides nothing; and neither does one sent before the
latest Job began, which speaks of the Job before. A row delivered again never replaces the
one stored: only a Job's own row is rewritten, as the Job goes on.

**Outages do not split a Job.** A gap is stored as a gap. The state channel only speaks on a
change, so after a gap the one status poll made on reconnecting stands in for it, until the
channel speaks again; at any other time the poll is ignored, because the REST API runs a
minute or two behind. A collector starting up carries on from each mower's latest stored Job,
counting what the last one left waiting to be written, and from when that Job last changed:
nothing sent before then decides anything.

**The dock arrival pose** is the newest pose reported when `isDocked` is heard, if the two
are no more than two minutes apart, and is kept on the Job.

**A Job's identifier** is the second it started, in UTC (`2026-09-30T13:33:33Z`), unique
within a mower. Replaying a capture therefore names its Jobs the same every time, with no
round trip to the database to ask for one.

## Considered options

- **The numeric `vehicleState` on each pose** as the state signal. Rejected: three
  integrations disagree on what 1, 2 and 3 mean, the capture settled only docked (1),
  charging (2), mowing (4) and returning (5), and paused, lifted and idle are still unseen.
  It is stored on every Trail point and decides nothing.
- **The percentage alone** to tell a new Job from a resumed one. Rejected: it only falls once
  the first whole percent is mowed, minutes into a Job, and the announcement shows it stale.
  It is the fallback when a report carries no area.
- **A change in the Zone list (`partitionIds`)** as a sign of a new Job. Not needed: this
  firmware reports progress during Zone Jobs. Where a firmware reports none, every departure
  is a Job of its own, charging breaks included, because nothing says otherwise.
- **A limit on how long a Job waits at the dock to be resumed**, such as the six hours an
  ioBroker integration applies when it has heard no progress. Rejected: it would split a Job
  that waited out a day of rain, and the area already says which Job a departure belongs to.
- **Holding positions back until a departure is known to be a resume.** Rejected: it delays
  the live map by up to a minute and a half after every charging break (the real mower took
  that long to report), to cover a case that arrives in the same second in practice.
- **A wall-clock timeout to end a Job.** Rejected: every rule reads only times carried by the
  stream, so a replay decides exactly what live collection decided.

## Consequences

- A position is not given a different Job or Zone after it is stored. One delivered before
  the message that would have placed it differently keeps what it got: at worst the few
  positions between leaving the dock and a new Job's announcement, when the Job before was
  left unfinished, count towards that one.
- The Zones already decided are not revised either. A report delivered after the mower turned
  for the dock, naming a Zone it entered before that, is stored but moves no position into
  that Zone.
- After a restart inside a Job the Zone is unknown until the next progress report, some ten
  seconds while mowing: the Job is stored, the Zone it was in is not.
- A restart keeps the Job, not what the collector was in the middle of deciding. Restarted in
  the second between a mower leaving the dock and announcing a new Job, it can take that Job
  for the one before resumed, or end the one before at the departure instead of at the dock.
- A state held up across an outage, and sent before the status poll that followed the
  outage, is still believed over that poll: the two are stamped by different clocks, and
  the poll is the one known to run behind.
- A mower that stops for good away from the dock leaves its Job without an end until it is
  docked or starts another. That is what happened, so nothing is made up to close it.
- A new Job whose announcement was missed, and which is first heard of only after it has
  mowed more than the unfinished Job before it had, is taken for that Job resumed. It takes
  an outage over both the announcement and that much mowing.
- Not yet observed, so not ruled on by evidence: a Job paused, lifted or in error, positioning
  lost, a rain delay, progress reported from the dock, and the event channel. Unknown states
  are stored as they come and change nothing.
