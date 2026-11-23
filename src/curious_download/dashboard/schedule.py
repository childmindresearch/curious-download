"""Turn Curious schedule events into the days and time windows an activity was expected on.

This mirrors what the mobile app does when it decides whether an activity is due today
(`ScheduledDateCalculator.ts` and `GroupUtility.ts` in the app), so the dashboard expects
exactly what the participant's phone offered:

- ``ALWAYS``: available every day; nothing is "expected", completions are simply counted.
  With ``oneTimeCompletion`` the activity can be done once in total.
- ``ONCE``: on ``selectedDate`` only.
- ``DAILY``: every day from ``startDate`` to ``endDate``.
- ``WEEKLY``: every day in that range with the same weekday as ``selectedDate``.
- ``WEEKDAYS``: Monday to Friday in that range.
- ``MONTHLY``: the day of month of ``selectedDate`` in that range, clamped to shorter months
  (as date-fns ``addMonths`` does on the phone).

Each occurrence has a window: ``startTime`` to ``endTime`` on that day in the participant's
local time. ``accessBeforeSchedule`` opens the window at midnight; an ``endTime`` before the
``startTime`` spreads the window into the next day. All dates and times here are participant-local
and naive.

Default events (``respondentId`` null) apply to everyone; an individual event replaces the
default events for *that one activity or flow* only (backend ``get_general_events_by_user``).
Event versions from the schedule history carry an active range, so a change made on a date
is only expected from that date on.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

ALWAYS = "ALWAYS"
ONCE = "ONCE"
DAILY = "DAILY"
WEEKLY = "WEEKLY"
WEEKDAYS = "WEEKDAYS"
MONTHLY = "MONTHLY"
PERIODICITIES = (ALWAYS, ONCE, DAILY, WEEKLY, WEEKDAYS, MONTHLY)

DAY_START = time(0, 0)
DAY_END = time(23, 59)


def entity_key(applet_id: str, kind: str, entity_id: str) -> str:
    """Identity of an activity or flow across the study: ``<applet>:<activity|flow>:<id>``."""
    return f"{applet_id}:{kind}:{entity_id}"


@dataclass(frozen=True)
class EventRule:
    """One version of one schedule event, as the dashboard reasons about it."""

    event_id: str
    applet_id: str
    kind: str  # "activity" | "flow"
    entity_id: str
    user_id: str | None  # None: default schedule; otherwise the respondent's user id
    periodicity: str
    start_date: date | None = None
    end_date: date | None = None
    selected_date: date | None = None
    start_time: time = DAY_START
    end_time: time = DAY_END
    access_before_schedule: bool = False
    one_time_completion: bool = False
    version: str | None = None
    # Days this version was in force (inclusive); None means unbounded on that side.
    active_from: date | None = None
    active_to: date | None = None

    @property
    def entity(self) -> str:
        return entity_key(self.applet_id, self.kind, self.entity_id)

    @property
    def is_individual(self) -> bool:
        return self.user_id is not None

    @property
    def spreads_to_next_day(self) -> bool:
        """The phone's `isSpreadToNextDay`: an end time earlier than the start time ends tomorrow."""
        return self.periodicity != ALWAYS and self.end_time < self.start_time

    def window(self, day: date) -> tuple[datetime, datetime]:
        """Local start and end of the occurrence on `day` (the phone's allowed interval)."""
        if self.periodicity == ALWAYS:
            return datetime.combine(day, DAY_START), datetime.combine(day, DAY_END)
        start = datetime.combine(day, DAY_START if self.access_before_schedule else self.start_time)
        end_day = day + timedelta(days=1) if self.spreads_to_next_day else day
        return start, datetime.combine(end_day, self.end_time)

    def scheduled_at(self, day: date) -> datetime:
        """The phone's `scheduledAt` for the day: the start time, ignoring access-before-schedule."""
        return datetime.combine(day, self.start_time if self.periodicity != ALWAYS else DAY_START)


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_time(value, default: time) -> time:
    if not value:
        return default
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value)
    hours, minutes = text.split(":")[:2]
    return time(int(hours), int(minutes))


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is not None:
        # The backend stores naive UTC; an offset-aware value is normalised to UTC first.
        moment = (moment - moment.utcoffset()).replace(tzinfo=None)
    return moment


def rule_from_event(applet_id: str, event: dict) -> EventRule | None:
    """An EventRule from a `GET /applets/{id}/events` row. None for rows without an activity or flow."""
    periodicity = event.get("periodicity") or {}
    kind, entity_id = ("flow", event.get("flowId")) if event.get("flowId") else ("activity", event.get("activityId"))
    if not entity_id:
        return None
    ptype = str(periodicity.get("type") or ALWAYS).upper()
    return EventRule(
        event_id=str(event["id"]),
        applet_id=applet_id,
        kind=kind,
        entity_id=str(entity_id),
        user_id=str(event["respondentId"]) if event.get("respondentId") else None,
        periodicity=ptype if ptype in PERIODICITIES else ALWAYS,
        start_date=_parse_date(periodicity.get("startDate")),
        end_date=_parse_date(periodicity.get("endDate")),
        selected_date=_parse_date(periodicity.get("selectedDate")),
        start_time=_parse_time(event.get("startTime"), DAY_START),
        end_time=_parse_time(event.get("endTime"), DAY_END),
        access_before_schedule=bool(event.get("accessBeforeSchedule")),
        one_time_completion=bool(event.get("oneTimeCompletion")),
        version=event.get("version"),
    )


def rules_from_history(applet_id: str, rows: Iterable[dict], tz_offset_minutes: int = 0) -> list[EventRule]:
    """EventRules from `GET /applets/{id}/events/history`, one per event version with its active days.

    The export lists each version once per applet version it was linked with, so rows are
    de-duplicated on (event, version). A version is active from the day it was created until
    the day before the next version; a deleted last version ends on the day it was deleted.
    """
    by_event: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        event_id = str(row.get("eventId") or row.get("event_id") or "")
        version = str(row.get("eventVersion") or row.get("event_version") or "")
        if not event_id or not version:
            continue
        by_event[event_id].setdefault(version, row)

    def local_day(value) -> date | None:
        moment = _parse_datetime(value)
        return (moment + timedelta(minutes=tz_offset_minutes)).date() if moment else None

    rules: list[EventRule] = []
    for event_id, versions in by_event.items():
        ordered = sorted(
            versions.values(),
            key=lambda r: str(r.get("eventVersionCreatedAt") or r.get("event_version_created_at") or ""),
        )
        for index, row in enumerate(ordered):
            entity_id = row.get("activityOrFlowId") or row.get("activity_or_flow_id")
            if not entity_id:
                continue
            event_type = str(row.get("eventType") or row.get("event_type") or "activity").lower()
            created = local_day(row.get("eventVersionCreatedAt") or row.get("event_version_created_at"))
            if index + 1 < len(ordered):
                following = ordered[index + 1]
                next_created = local_day(
                    following.get("eventVersionCreatedAt") or following.get("event_version_created_at")
                )
                active_to = next_created - timedelta(days=1) if next_created else None
            elif row.get("eventVersionIsDeleted", row.get("event_version_is_deleted")):
                active_to = local_day(row.get("eventVersionUpdatedAt") or row.get("event_version_updated_at"))
            else:
                active_to = None
            if active_to is not None and created is not None and active_to < created:
                active_to = created
            ptype = str(row.get("periodicity") or ALWAYS).upper()
            user_id = row.get("userId") or row.get("user_id")
            rules.append(
                EventRule(
                    event_id=event_id,
                    applet_id=applet_id,
                    kind="flow" if event_type == "flow" else "activity",
                    entity_id=str(entity_id),
                    user_id=str(user_id) if user_id else None,
                    periodicity=ptype if ptype in PERIODICITIES else ALWAYS,
                    start_date=_parse_date(row.get("startDate") or row.get("start_date")),
                    end_date=_parse_date(row.get("endDate") or row.get("end_date")),
                    selected_date=_parse_date(row.get("selectedDate") or row.get("selected_date")),
                    start_time=_parse_time(row.get("startTime") or row.get("start_time"), DAY_START),
                    end_time=_parse_time(row.get("endTime") or row.get("end_time"), DAY_END),
                    access_before_schedule=bool(row.get("accessBeforeSchedule", row.get("access_before_schedule"))),
                    one_time_completion=bool(row.get("oneTimeCompletion", row.get("one_time_completion"))),
                    version=str(row.get("eventVersion") or row.get("event_version") or "") or None,
                    active_from=created,
                    active_to=active_to,
                )
            )
    return rules


def merge_rules(default_rules: Iterable[EventRule], individual_rules: Iterable[EventRule]) -> list[EventRule]:
    """The rules one participant lives by: their individual events, plus default events for every
    activity or flow they have no individual event for (the backend's rule, per entity)."""
    individual = list(individual_rules)
    overridden = {rule.entity for rule in individual}
    return individual + [rule for rule in default_rules if rule.entity not in overridden]


def _month_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def occurrence_days(rule: EventRule, first: date, last: date) -> Iterator[date]:
    """Local days in [first, last] on which `rule` offered its activity. Nothing for ALWAYS."""
    if rule.periodicity == ALWAYS or last < first:
        return
    lo, hi = first, last
    for bound in (rule.start_date, rule.active_from):
        if bound and bound > lo:
            lo = bound
    for bound in (rule.end_date, rule.active_to):
        if bound and bound < hi:
            hi = bound
    if rule.periodicity == ONCE:
        day = rule.selected_date
        in_range = day is not None and first <= day <= last
        active = (rule.active_from is None or day >= rule.active_from) and (
            rule.active_to is None or day <= rule.active_to
        )
        if in_range and active:
            yield day
        return
    if hi < lo:
        return
    if rule.periodicity == DAILY:
        day = lo
        while day <= hi:
            yield day
            day += timedelta(days=1)
    elif rule.periodicity == WEEKDAYS:
        day = lo
        while day <= hi:
            if day.weekday() < 5:
                yield day
            day += timedelta(days=1)
    elif rule.periodicity == WEEKLY:
        if rule.selected_date is None:
            return
        day = lo + timedelta(days=(rule.selected_date.weekday() - lo.weekday()) % 7)
        while day <= hi:
            yield day
            day += timedelta(days=7)
    elif rule.periodicity == MONTHLY:
        if rule.selected_date is None:
            return
        year, month = lo.year, lo.month
        while True:
            day = _month_day(year, month, rule.selected_date.day)
            if day > hi:
                return
            if day >= lo:
                yield day
            month += 1
            if month > 12:
                month, year = 1, year + 1


@dataclass(frozen=True)
class Slot:
    """One expected occurrence: an event on a local day, with its window."""

    rule: EventRule
    day: date
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[str, date]:
        return self.rule.event_id, self.day


def expand(rules: Iterable[EventRule], first: date, last: date) -> list[Slot]:
    """Every occurrence of every rule in [first, last], one per (event, day).

    Versions of the same event never overlap in days, so the (event, day) key is unique; if
    two sources do overlap (current events plus history), the version active later wins.
    """
    slots: dict[tuple[str, date], Slot] = {}
    for rule in sorted(rules, key=lambda r: r.active_from or date.min):
        for day in occurrence_days(rule, first, last):
            start, end = rule.window(day)
            slots[(rule.event_id, day)] = Slot(rule, day, start, end)
    return sorted(slots.values(), key=lambda slot: (slot.day, slot.start, slot.rule.entity))


def bound_current_rules(current: Iterable[EventRule], history: Iterable[EventRule]) -> list[EventRule]:
    """Give current events the active range of their latest history version, and add past
    versions and deleted events from the history. Current events without history stay unbounded."""
    history_by_event: dict[str, list[EventRule]] = defaultdict(list)
    for rule in history:
        history_by_event[rule.event_id].append(rule)
    result: list[EventRule] = []
    seen: set[str] = set()
    for rule in current:
        seen.add(rule.event_id)
        versions = sorted(history_by_event.get(rule.event_id, ()), key=lambda r: r.active_from or date.min)
        if not versions:
            result.append(rule)
            continue
        latest = versions[-1]
        result.extend(versions[:-1])
        result.append(
            replace(rule, active_from=latest.active_from, active_to=None, version=rule.version or latest.version)
        )
    for event_id, versions in history_by_event.items():
        if event_id not in seen:
            # Deleted events: still needed to judge the days they were in force.
            result.extend(versions)
    return result
