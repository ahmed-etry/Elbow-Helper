"""Check destination spacing across finite and recurring standing schedules."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import gcd

from ..actions.contracts import ActionRefused
from ..wording import ACTION_SCHEDULE_DESTINATION_LIMIT
from .time_rules import MIN_MESSAGE_INTERVAL_SECONDS, _utc, next_occurrences, schedule_expired

MIN_DESTINATION_INTERVAL_SECONDS = MIN_MESSAGE_INTERVAL_SECONDS
_SECOND = 1_000_000
_GAP = MIN_DESTINATION_INTERVAL_SECONDS * _SECOND
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CALENDAR_CYCLE = 146097 * 86400 * _SECOND


def destinations(rule, requester_id, channel_id):
    if rule.get("deliver_to") == "dm" or rule.get("dm_member_ids"):
        return frozenset(("dm", identifier) for identifier in
                         rule.get("dm_member_ids", (requester_id,)))
    return frozenset({("channel", channel_id)})


def _micros(instant):
    delta = instant - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _SECOND + delta.microseconds


@dataclass(frozen=True)
class Progression:
    first: int
    step: int
    last_index: int | None


def _ceil(value, divisor):
    return -(-value // divisor)


def _near_point(point, sequence):
    index = max(0, (point - sequence.first) // sequence.step)
    if sequence.last_index is not None:
        index = min(index, sequence.last_index)
    return any(abs(point - (sequence.first + selected * sequence.step)) < _GAP
               for selected in (index, index + 1)
               if sequence.last_index is None or selected <= sequence.last_index)


def progressions_conflict(left, right):
    """Solve bounded integer progressions rather than sample their first few runs."""
    if left.last_index is not None and left.last_index < 0:
        return False
    if right.last_index is not None and right.last_index < 0:
        return False
    if not left.step:
        return (abs(left.first - right.first) < _GAP if not right.step
                else _near_point(left.first, right))
    if not right.step:
        return _near_point(right.first, left)
    divisor = gcd(left.step, right.step)
    left_stride, right_stride = right.step // divisor, left.step // divisor
    difference = left.first - right.first
    for multiple in range(_ceil(-_GAP + 1 - difference, divisor),
                          (_GAP - 1 - difference) // divisor + 1):
        # i*left.step - j*right.step == multiple*divisor.
        i = (multiple * pow(right_stride, -1, left_stride)) % left_stride if left_stride > 1 else 0
        j = (i * left.step - multiple * divisor) // right.step
        lower = max(_ceil(-i, left_stride), _ceil(-j, right_stride))
        upper = None
        if left.last_index is not None:
            upper = (left.last_index - i) // left_stride
        if right.last_index is not None:
            bound = (right.last_index - j) // right_stride
            upper = bound if upper is None else min(upper, bound)
        if upper is None or lower <= upper:
            return True
    return False


def _progression(rule, next_at):
    schedule = rule["schedule"]
    first = _micros(datetime.fromtimestamp(next_at, timezone.utc))
    if schedule["kind"] == "once":
        return Progression(first, 0, 0)
    step = schedule["seconds"] * _SECOND
    last = None
    if "runs" in schedule:
        last = schedule["runs"] - rule.get("runs_completed", 0) - 1
    if "end_utc" in schedule:
        end_index = (_micros(_utc(schedule["end_utc"])) - first) // step
        last = end_index if last is None else min(last, end_index)
    return Progression(first, step, last)


def _calendar_times(rule, next_at, until):
    after = datetime.fromtimestamp(next_at, timezone.utc) - timedelta(microseconds=1)
    completed = rule.get("runs_completed", 0)
    while after < until:
        times = next_occurrences(rule["schedule"], after=after, count=10, runs_completed=completed)
        if not times:
            return
        for instant in times:
            if instant >= until:
                return
            yield instant
        completed += len(times)
        after = times[-1]


def _cycle_start(first):
    # Future timezone tail rules and the Gregorian calendar repeat every 400 years.
    year = max(2400, (first.year // 400 + 1) * 400)
    return datetime(min(year, 9600), 1, 1, tzinfo=timezone.utc)


def _calendar_interval_conflict(calendar_rule, calendar_at, interval):
    first = datetime.fromtimestamp(calendar_at, timezone.utc)
    cycle_start = _cycle_start(first)
    # The last supported cycle ends at year 10000, beyond datetime's range.
    if cycle_start.year == 9600:
        cycle_end = datetime.max.replace(tzinfo=timezone.utc)
    else:
        cycle_end = cycle_start.replace(year=cycle_start.year + 400)
    if interval.last_index is not None:
        latest = interval.first + interval.step * interval.last_index + _GAP
        latest = min(latest, _micros(datetime.max.replace(tzinfo=timezone.utc)))
        cycle_end = min(cycle_end, _EPOCH + timedelta(microseconds=latest))
    cycle = []
    count = 0
    for instant in _calendar_times(calendar_rule, calendar_at, cycle_end):
        count += 1
        if instant < cycle_start:
            if progressions_conflict(Progression(_micros(instant), 0, 0), interval):
                return True
        else:
            cycle.append((_micros(instant), count))
    schedule = calendar_rule["schedule"]
    remaining = schedule.get("runs")
    if remaining is not None:
        remaining -= calendar_rule.get("runs_completed", 0)
    end = _micros(_utc(schedule["end_utc"])) if "end_utc" in schedule else None
    # Each occurrence in a full calendar cycle is itself an arithmetic progression.
    for instant, ordinal in cycle:
        last = (_micros(datetime.max.replace(tzinfo=timezone.utc)) - instant) // _CALENDAR_CYCLE
        if remaining is not None:
            last = min(last, (remaining - ordinal) // len(cycle))
        if end is not None:
            bound = (end - instant) // _CALENDAR_CYCLE
            last = bound if last is None else min(last, bound)
        if progressions_conflict(Progression(instant, _CALENDAR_CYCLE, last), interval):
            return True
    return False


def schedules_conflict(left, left_at, right, right_at):
    if not left.get("schedule") or not right.get("schedule"):
        return False
    # Completed/expired repeating rules do not reserve destination slots.
    for rule, due in ((left, left_at), (right, right_at)):
        if due is None or schedule_expired(rule["schedule"],
                datetime.fromtimestamp(due, timezone.utc), rule.get("runs_completed", 0)):
            return False
    left_linear = left["schedule"]["kind"] in ("once", "interval")
    right_linear = right["schedule"]["kind"] in ("once", "interval")
    if left_linear and right_linear:
        return progressions_conflict(_progression(left, left_at), _progression(right, right_at))
    if left_linear:
        left, left_at, right, right_at = right, right_at, left, left_at
        right_linear = True
    if right_linear:
        return _calendar_interval_conflict(left, left_at, _progression(right, right_at))
    start = _cycle_start(datetime.fromtimestamp(max(left_at, right_at), timezone.utc))
    until = (start.replace(year=start.year + 400) if start.year < 9600
             else datetime.max.replace(tzinfo=timezone.utc))
    left_times, right_times = _calendar_times(left, left_at, until), _calendar_times(right, right_at, until)
    a, b = next(left_times, None), next(right_times, None)
    while a is not None and b is not None:
        if abs(a - b) < timedelta(seconds=MIN_DESTINATION_INTERVAL_SECONDS):
            return True
        if a < b:
            a = next(left_times, None)
        else:
            b = next(right_times, None)
    return False


def require_destination_spacing(rule, next_at, *, requester_id, channel_id, existing):
    schedule = rule.get("schedule", {})
    if schedule.get("kind") == "interval" and schedule["seconds"] < MIN_DESTINATION_INTERVAL_SECONDS:
        raise ActionRefused(ACTION_SCHEDULE_DESTINATION_LIMIT)
    selected = destinations(rule, requester_id, channel_id)
    for other in existing:
        if not selected.intersection(destinations(other["rule"], requester_id,
                                                  other["destination_channel_id"])):
            continue
        if schedules_conflict(rule, next_at, other["rule"],
                              other.get("next_run_at", other.get("next_check_at"))):
            raise ActionRefused(ACTION_SCHEDULE_DESTINATION_LIMIT)
