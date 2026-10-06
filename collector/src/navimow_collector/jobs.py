"""Decide where each Job begins and ends: nothing on the wire carries a Job identifier.

The rules, and the capture they were checked against, are recorded in
docs/adr/0001-job-and-zone-detection.md.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, TypeVar

from .records import Job, MowerState, Progress, Row, TrailPoint

Reading = TypeVar("Reading", MowerState, Progress)
Number = TypeVar("Number", int, float)

# How far the area mowed must fall, in square metres, before it is another Job's area.
AREA_FALL = 1.0
# The mower docks within seconds of its last pose on the way in. An older pose is from
# before a silence, and says nothing of where the dock is.
ARRIVAL_POSE_AGE = timedelta(minutes=2)
# How many changes of Job a tracker remembers, to place a reading that is delivered late.
SPANS_KEPT = 64


class Span(NamedTuple):
    """From `since` on, readings belong to this Job and Zone (or to none, at the dock)."""

    since: datetime
    job_id: str | None = None
    zone: int | None = None


class JobTracker:
    """One mower's Jobs and Zones, decided from its stream in the order it arrives.

    Every reading comes back as the rows to store for it: each Job the reading changed,
    and the reading itself, naming the Job the mower was on at the reading's own time.
    """

    def __init__(self, mower_id: str, job: Job | None = None) -> None:
        """`job` is the mower's latest Job, where a collector starting up knows of one."""
        self._mower_id = mower_id
        self._job = job
        away = job is not None and job.end_time is None
        self._spans = [Span(job.start_time, job.job_id)] if job is not None and away else []
        # When the newest state and the newest progress report heard were sent. Whatever
        # was sent before a stored Job last changed was heard by the collector that stored it.
        self._stated_at = self._reported = job.updated_time if job is not None else None
        self._pose: TrailPoint | None = None  # the newest position
        self._heard = False  # whether the state channel has spoken since the last gap
        # When the Job was left off, while it is only taken to be resumed: the mower left
        # the dock, and has yet to report progress on it.
        self._left_off: datetime | None = None

    def point(self, point: TrailPoint) -> list[Row]:
        if self._pose is None or point.device_time >= self._pose.device_time:
            self._pose = point
        span = self._span(point.device_time)
        return [replace(point, job_id=span.job_id, zone=span.zone)]

    def state(self, reading: MowerState) -> list[Row]:
        self._heard = True
        if self._stated_at is not None and reading.device_time < self._stated_at:
            return [self._in_its_job(reading)]  # delivered after a newer one: decides nothing
        self._stated_at = reading.device_time
        return [*self._on_state(reading), self._in_its_job(reading)]

    def polled(self, reading: MowerState) -> list[Row]:
        """A status poll's answer. The REST API runs a minute or two behind the state
        channel, so it only counts while that channel has said nothing since connecting."""
        return [] if self._heard else self._on_state(reading)

    def gap(self) -> None:
        """The stream was interrupted: what the state channel last said may be out of date."""
        self._heard = False

    def _on_state(self, reading: MowerState) -> list[Row]:
        """Decide what a state says of the Job; the Job to store, if it changed."""
        time, job, zone = reading.device_time, self._job, self._zone
        if job is not None and time < job.start_time:
            return []  # sent before this Job began: it is the Job before that it speaks of
        away = job is not None and job.end_time is None
        if reading.state == "isRunning" and not away:
            if job is not None and _unfinished(job):
                # Back from charging, as far as can be told: if it is on another Job
                # instead, that is announced within the second.
                job, self._left_off = replace(job, end_time=None), job.end_time
            else:
                job = self._begin(time)
        elif reading.state == "isDocked" and job is not None and away:
            job = replace(job, end_time=time)
            pose = self._pose
            if pose is not None and abs(time - pose.device_time) <= ARRIVAL_POSE_AGE:
                job = replace(job, arrival_x=pose.x, arrival_y=pose.y, arrival_theta=pose.theta)
        elif reading.state == "isDocking":
            zone = None  # on its way back across the lawn, it is mowing no Zone
        return self._take(job, time, zone)

    def progress(self, report: Progress) -> list[Row]:
        changed = self._on_progress(report)
        # A report of no area at all announces a Job, and reports no progress to store. The
        # rest of it is left over from the Job before: a percentage that one finished on,
        # a Zone it is not in.
        return changed if report.area == 0 else [*changed, self._in_its_job(report)]

    def _on_progress(self, report: Progress) -> list[Row]:
        """Decide what a progress report says of the Job; the Jobs to store, if any changed."""
        time, job, zone = report.device_time, self._job, self._zone
        if self._reported is not None and time < self._reported:
            return []
        heard, self._reported = self._reported, time
        if job is not None and time < job.start_time:
            return []  # sent before this Job began: it is the Job before that it speaks of
        left_off, self._left_off = self._left_off, None
        given_up: list[Row] = []
        # Progress sent since the mower docked: it has left again, unseen.
        left_unseen = job is not None and job.end_time is not None and time > job.end_time
        if job is None or _fell(job, report) or (left_unseen and not _unfinished(job)):
            if job is not None and job.end_time is None:
                # Away on a Job, and the mower has started another: it gave the first up
                # at the dock if it never took it up again, and else where last heard.
                last_heard = max(job.updated_time, heard or job.updated_time)
                given_up = [replace(job, end_time=left_off or last_heard)]
            job, zone = self._begin(time), None
        elif left_unseen:
            job = replace(job, end_time=None)
        if report.area != 0:
            zone = report.zone
            # A report may leave either figure out: the Job keeps what it last knew.
            percentage = _known(report.mowing_percentage, job.mowing_percentage)
            job = replace(
                job,
                mowing_percentage=percentage,
                area=_known(report.area, job.area),
                completed=job.completed or (percentage is not None and percentage >= 100),
            )
        return [*given_up, *self._take(job, time, zone)]

    @property
    def _zone(self) -> int | None:
        return self._spans[-1].zone if self._spans else None

    def _begin(self, time: datetime) -> Job:
        return Job(self._mower_id, _job_id(time), start_time=time, updated_time=time)

    def _take(self, job: Job | None, time: datetime, zone: int | None) -> list[Row]:
        """Take `job`, in `zone`, as what the mower is on from `time`; the Job to store,
        if it changed."""
        changed: list[Row] = []
        if job is not None and job != self._job:
            self._job = replace(job, updated_time=max(job.updated_time, time))
            changed.append(self._job)
        last = self._spans[-1] if self._spans else Span(time)
        span = Span(max(time, last.since))
        if self._job is not None and self._job.end_time is None:
            span = span._replace(job_id=self._job.job_id, zone=zone)
        # A reading that arrives after a later one does not move the Zone that one decided.
        if span.job_id != last.job_id or (span.zone != last.zone and time >= last.since):
            self._spans = [*self._spans[-SPANS_KEPT:], span]
        return changed

    def _span(self, time: datetime) -> Span:
        index = bisect_right(self._spans, time, key=lambda span: span.since)
        return self._spans[index - 1] if index else Span(time)

    def _in_its_job(self, reading: Reading) -> Reading:
        """The reading, naming the Job the mower was on at the reading's own time."""
        return replace(reading, job_id=self._span(reading.device_time).job_id)


def _unfinished(job: Job) -> bool:
    """Whether the Job is known to have work left, for the mower to take up again when it
    next leaves the dock. One that never reported progress is not known to."""
    return job.mowing_percentage is not None and not job.completed


def _fell(job: Job, report: Progress) -> bool:
    """Whether the report counts from the start again, as only another Job would."""
    if report.area == 0:  # an announcement: of another Job, if this one has reported at all
        return job.area is not None or job.mowing_percentage is not None
    if job.area is not None and report.area is not None:
        return report.area < job.area - AREA_FALL
    if job.mowing_percentage is not None and report.mowing_percentage is not None:
        return report.mowing_percentage < job.mowing_percentage
    return False


def _known(reported: Number | None, before: Number | None) -> Number | None:
    return before if reported is None else reported


def _job_id(start: datetime) -> str:
    """A mower starts one Job at a time, so when it started names it, replay after replay."""
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
