"""The four dashboards as grids: rows of participants, columns of days or activities, cells of counts.

Every view produces the same shape, so the page renders them with one component::

    {"view": ..., "groups": [...], "columns": [...], "rows": [{"key", "label", "sub", "cells": [...], "summary"}],
     "totals": [...], "legend": [...], "notes": [...], "params": {...}}

A cell is ``{"e": expected, "d": done, "l": late, "p": partly done, "m": missed, "u": open or upcoming,
"x": done without being scheduled, "s": status, "t": first completion day}`` or ``null`` when the
participant is not in that applet. Cell details (each occurrence with its window and completions)
are kept server-side per (row, column) for the click-through and the long CSV.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import StudyConfig
from .model import (
    DONE,
    DUE,
    LATE,
    MISSED,
    PARTIAL,
    UPCOMING,
    Completion,
    Done,
    Entity,
    Extra,
    FlowSubmission,
    Occurrence,
    Options,
    Participant,
    StudyData,
    compute,
    participant_rules,
)

LEGEND = [
    {"status": "done", "label": "Completed"},
    {"status": "late", "label": "Completed outside the time window"},
    {"status": "partial", "label": "Partly done"},
    {"status": "missed", "label": "Missed"},
    {"status": "due", "label": "Open now"},
    {"status": "upcoming", "label": "Upcoming"},
    {"status": "extra", "label": "Done, not scheduled"},
    {"status": "none", "label": "Nothing scheduled"},
]

CROSS_LEGEND = [
    {"status": "done", "label": "Done"},
    {"status": "partial", "label": "Flow started, not finished"},
    {"status": "missed", "label": "Scheduled date passed"},
    {"status": "due", "label": "Available now, not done yet"},
    {"status": "upcoming", "label": "Scheduled for later"},
    {"status": "none", "label": "Not done"},
]

MAX_DAYS = 400


@dataclass
class ViewParams:
    view: str
    first: date
    last: date
    granularity: str = "day"
    late_counts_as_done: bool = True
    count_from_joined: bool = True
    include_removed: bool = False
    entity_keys: set[str] | None = None
    participant_keys: set[str] | None = None
    only_with_data: bool = False
    focus: str | None = None  # individual view: one participant's own timeline
    start_rule: str = "first_completion"
    start_entity: str | None = None
    study_days: int = 28
    today: date = field(default_factory=date.today)
    now_utc: datetime = field(default_factory=datetime.utcnow)
    host_tz_offset: int = 0


def _parse_day(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as e:
        raise ValueError(f"Not a valid date: {text!r} (use YYYY-MM-DD)") from e


def view_params(
    config: StudyConfig, query: dict, data: StudyData, *, today: date, now_utc: datetime, host_tz: int
) -> ViewParams:
    """Combine the saved study with per-request overrides from the page."""
    view = query.get("view") or config.view
    span_first, span_last = data.day_span()
    last = _parse_day(query.get("last")) or min(max(span_last or today, today), today + timedelta(days=7))
    first = _parse_day(query.get("first")) or max(
        span_first or (today - timedelta(days=30)), last - timedelta(days=120)
    )
    if first > last:
        first, last = last, first
    if (last - first).days > MAX_DAYS:
        first = last - timedelta(days=MAX_DAYS)

    def flag(name: str, default: bool) -> bool:
        value = query.get(name)
        if value is None or value == "":
            return default
        return str(value).lower() in ("1", "true", "yes", "on")

    entities = query.get("entities")
    entity_keys: set[str] | None
    if entities:
        entity_keys = {key for key in str(entities).split(",") if key}
    elif config.entities:
        entity_keys = set(config.entities)
    else:
        entity_keys = None
    participants = query.get("participants")
    participant_keys: set[str] | None
    if participants:
        participant_keys = {key for key in str(participants).split(",") if key}
    elif config.participants:
        participant_keys = set(config.participants)
    else:
        participant_keys = None
    study_days = int(query.get("studyDays") or config.study_days)
    return ViewParams(
        view=view,
        first=first,
        last=last,
        granularity=query.get("granularity") or config.granularity or "day",
        late_counts_as_done=flag("lateCountsAsDone", config.late_counts_as_done),
        count_from_joined=flag("countFromJoined", config.count_from_joined),
        include_removed=flag("includeRemoved", config.include_removed),
        entity_keys=entity_keys,
        participant_keys=participant_keys,
        only_with_data=flag("onlyWithData", config.only_with_data),
        focus=query.get("focus") or None,
        start_rule=query.get("startRule") or config.start_rule,
        start_entity=query.get("startEntity") or config.start_entity,
        study_days=max(1, min(study_days, 730)),
        today=today,
        now_utc=now_utc,
        host_tz_offset=host_tz,
    )


# -- cells ------------------------------------------------------------------------------------


@dataclass
class Counts:
    e: int = 0
    d: int = 0
    l: int = 0  # short keys travel to the browser as-is
    p: int = 0
    m: int = 0
    due: int = 0
    up: int = 0
    x: int = 0
    first_done: datetime | None = None
    last_done: datetime | None = None

    def _mark(self, moment: datetime) -> None:
        self.first_done = moment if self.first_done is None else min(self.first_done, moment)
        self.last_done = moment if self.last_done is None else max(self.last_done, moment)

    def add(self, occurrence: Occurrence) -> None:
        self.e += 1
        if occurrence.status == DONE:
            self.d += 1
        elif occurrence.status == LATE:
            self.l += 1
        elif occurrence.status == PARTIAL:
            self.p += 1
        elif occurrence.status == MISSED:
            self.m += 1
        elif occurrence.status == DUE:
            self.due += 1
        else:
            self.up += 1
        for item in occurrence.done:
            self._mark(item.end_local)

    def add_extra(self, extra: Extra) -> None:
        self.x += 1
        self._mark(extra.item.end_local)

    def merge(self, other: Counts) -> None:
        for name in ("e", "d", "l", "p", "m", "due", "up", "x"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        if other.first_done:
            self._mark(other.first_done)
        if other.last_done:
            self._mark(other.last_done)

    @property
    def closed(self) -> int:
        return self.d + self.l + self.p + self.m

    def credited(self, late_counts: bool) -> int:
        return self.d + (self.l if late_counts else 0)

    def pct(self, late_counts: bool) -> float | None:
        return round(self.credited(late_counts) / self.closed, 3) if self.closed else None

    def status(self, late_counts: bool) -> str:
        if self.e == 0:
            return "extra" if self.x else "none"
        if self.closed == 0:
            return "due" if self.due else "upcoming"
        credited = self.credited(late_counts)
        pending = self.due + self.up
        if credited == self.e:
            return "done"
        if credited + pending == self.e:
            return "due"  # nothing missed so far, more to come
        if credited == 0 and self.p == 0:
            if self.l and not late_counts and self.l == self.closed:
                return "late"
            return "missed" if pending == 0 else "due" if self.m == 0 else "partial"
        return "partial"

    def to_json(self, late_counts: bool) -> dict:
        payload = {
            "e": self.e,
            "d": self.credited(late_counts),
            "l": self.l,
            "p": self.p,
            "m": self.m,
            "u": self.due + self.up,
            "x": self.x,
            "s": self.status(late_counts),
        }
        if self.first_done:
            payload["t"] = self.first_done.date().isoformat()
        return payload


@dataclass
class GridResult:
    grid: dict
    details: dict[tuple[str, str], list[dict]]


# -- shared helpers ---------------------------------------------------------------------------


def _bucket(day: date, granularity: str) -> date:
    return day if granularity == "day" else day - timedelta(days=day.weekday())


def _bucket_label(bucket: date, granularity: str) -> tuple[str, str]:
    if granularity == "day":
        return bucket.strftime("%a %-d %b"), bucket.strftime("%Y")
    return f"Week of {bucket.strftime('%-d %b')}", bucket.strftime("%Y")


def _buckets(first: date, last: date, granularity: str) -> list[date]:
    buckets: list[date] = []
    day = _bucket(first, granularity)
    step = timedelta(days=1 if granularity == "day" else 7)
    while day <= last:
        buckets.append(day)
        day += step
    return buckets


def _in_applet(participant: Participant, entity: Entity) -> bool:
    return entity.applet_id in participant.applet_ids


def _entities(data: StudyData, params: ViewParams) -> list[Entity]:
    entities = data.visible_entities()
    if params.entity_keys is not None:
        entities = [e for e in entities if e.key in params.entity_keys]
    return entities


def _entity_label(entity: Entity, multi_applet: bool) -> str:
    return f"{entity.applet_name}: {entity.label}" if multi_applet else entity.label


def _iso(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else None


def _item_detail(data: StudyData, item: Done, occurrence: Occurrence | None) -> dict:
    in_window = occurrence is not None and occurrence.start <= item.end_local <= occurrence.end
    detail = {
        "start": _iso(item.start_local),
        "end": _iso(item.end_local),
        "inWindow": in_window,
        "complete": True,
        "submitId": item.submit_id,
    }
    if isinstance(item, FlowSubmission):
        entity = data.entities.get(item.entity)
        names = []
        for activity_id in item.expected_activities or sorted(item.done_activities):
            activity = data.entities.get(f"{entity.applet_id}:activity:{activity_id}") if entity else None
            name = activity.name if activity else activity_id
            names.append({"name": name, "done": activity_id in item.done_activities})
        detail.update(complete=item.complete, activities=names)
    return detail


def _occurrence_detail(data: StudyData, occurrence: Occurrence) -> dict:
    entity = data.entities.get(occurrence.entity)
    participant = data.participants.get(occurrence.participant)
    return {
        "kind": "occurrence",
        "participant": participant.secret_id if participant else occurrence.participant,
        "applet": entity.applet_name if entity else "",
        "entity": entity.label if entity else occurrence.entity,
        "day": occurrence.day.isoformat(),
        "windowStart": _iso(occurrence.start),
        "windowEnd": _iso(occurrence.end),
        "status": occurrence.status,
        "schedule": "individual" if occurrence.individual else "default",
        "periodicity": occurrence.periodicity,
        "completions": [_item_detail(data, item, occurrence) for item in occurrence.done],
    }


def _extra_detail(data: StudyData, extra: Extra) -> dict:
    entity = data.entities.get(extra.entity)
    participant = data.participants.get(extra.participant)
    return {
        "kind": "extra",
        "participant": participant.secret_id if participant else extra.participant,
        "applet": entity.applet_name if entity else "",
        "entity": entity.label if entity else extra.entity,
        "day": extra.day.isoformat(),
        "windowStart": None,
        "windowEnd": None,
        "status": "extra",
        "schedule": "",
        "periodicity": "",
        "completions": [_item_detail(data, extra.item, None)],
    }


def _row_summary(cells: list[Counts | None], late_counts: bool, next_due: datetime | None = None) -> dict:
    total = Counts()
    for cell in cells:
        if cell is not None:
            total.merge(cell)
    summary = total.to_json(late_counts)
    summary["pct"] = total.pct(late_counts)
    summary["last"] = _iso(total.last_done)
    summary["next"] = _iso(next_due)
    summary.pop("t", None)
    return summary


def _next_due(occurrences: list[Occurrence]) -> datetime | None:
    pending = [o.start for o in occurrences if o.status in (DUE, UPCOMING)]
    return min(pending) if pending else None


def _participant_meta(participant: Participant, data: StudyData, start: date | None = None, source: str = "") -> dict:
    """The facts behind a row's label and sub-line, for exports."""
    joined = [day for day in participant.joined.values() if day]
    return {
        "kind": "participant",
        "nickname": participant.nickname,
        "tag": participant.tag,
        "applets": [data.applets[a]["name"] for a in sorted(participant.applet_ids) if a in data.applets],
        "joined": min(joined).isoformat() if joined else None,
        "schedule": "individual" if any(participant.individual.values()) else "default",
        "removed": participant.is_removed_everywhere(),
        "start": start.isoformat() if start else None,
        "startSource": source or None,
    }


def _participant_sub(participant: Participant, data: StudyData) -> str:
    parts = [part for part in (participant.nickname, participant.tag) if part]
    if len(data.applets) > 1:
        names = [data.applets[a]["name"] for a in sorted(participant.applet_ids) if a in data.applets]
        parts.append(", ".join(names))
    if participant.is_removed_everywhere():
        parts.append("removed")
    return " · ".join(parts)


def _finish(
    view: str,
    params: ViewParams,
    groups: list[dict],
    columns: list[dict],
    rows: list[dict],
    cells_by_row: list[list[Counts | None]],
    details: dict[tuple[str, str], list[dict]],
    legend: list[dict],
    notes: list[str],
    extra_params: dict | None = None,
) -> GridResult:
    totals: list[Counts | None] = [None] * len(columns)
    for cells in cells_by_row:
        for index, cell in enumerate(cells):
            if cell is None:
                continue
            if totals[index] is None:
                totals[index] = Counts()
            totals[index].merge(cell)  # type: ignore[union-attr]
    overall = Counts()
    for cell in totals:
        if cell is not None:
            overall.merge(cell)
    late = params.late_counts_as_done
    grid = {
        "view": view,
        "groups": groups,
        "columns": columns,
        "rows": rows,
        "totals": [cell.to_json(late) if cell is not None else None for cell in totals],
        "overall": {
            **{k: v for k, v in overall.to_json(late).items() if k != "t"},
            "pct": overall.pct(late),
            "participants": len(rows),
        },
        "legend": legend,
        "notes": notes,
        "params": {
            "first": params.first.isoformat(),
            "last": params.last.isoformat(),
            "granularity": params.granularity,
            "lateCountsAsDone": late,
            "countFromJoined": params.count_from_joined,
            "includeRemoved": params.include_removed,
            "entities": sorted(params.entity_keys) if params.entity_keys is not None else None,
            "participants": sorted(params.participant_keys) if params.participant_keys is not None else None,
            "onlyWithData": params.only_with_data,
            "focus": params.focus,
            "startRule": params.start_rule,
            "startEntity": params.start_entity,
            "studyDays": params.study_days,
            "today": params.today.isoformat(),
            **(extra_params or {}),
        },
    }
    return GridResult(grid, details)


def _common_notes(data: StudyData) -> list[str]:
    notes = list(data.warnings)
    missing_history = [
        data.applets[a]["name"] for a, ok in data.history_available.items() if not ok and a in data.applets
    ]
    if missing_history:
        notes.append(
            "Schedule history not available for "
            + ", ".join(missing_history)
            + ": the current schedule is assumed for the whole period (changes made along the way are not reflected)."
        )
    return notes


def _options(params: ViewParams, first: date, last: date) -> Options:
    return Options(
        first=first,
        last=last,
        now_utc=params.now_utc,
        late_counts_as_done=params.late_counts_as_done,
        count_from_joined=params.count_from_joined,
        include_removed=params.include_removed,
        entity_keys=params.entity_keys,
        participant_keys=params.participant_keys,
        only_with_data=params.only_with_data,
        host_tz_offset=params.host_tz_offset,
    )


# -- the views ---------------------------------------------------------------------------------


def build_view(data: StudyData, params: ViewParams) -> GridResult:
    if params.view == "fixed":
        return _fixed(data, params)
    if params.view == "rolling":
        return _rolling(data, params)
    if params.view == "cross":
        return _cross(data, params)
    if params.view == "individual":
        return _individual(data, params)
    raise ValueError(f"Unknown view {params.view!r}.")


def _fixed(data: StudyData, params: ViewParams) -> GridResult:
    """Everyone on the same calendar: columns are days (or weeks) times activities."""
    entities = _entities(data, params)
    computed = compute(data, _options(params, params.first, params.last))
    buckets = _buckets(params.first, params.last, params.granularity)
    multi = len(data.applets) > 1
    single = len(entities) == 1
    groups: list[dict] = []
    columns: list[dict] = []
    today_bucket = _bucket(params.today, params.granularity)
    for bucket in buckets:
        label, sub = _bucket_label(bucket, params.granularity)
        if single:
            columns.append(
                {
                    "key": f"{bucket.isoformat()}|{entities[0].key}",
                    "label": label,
                    "sub": sub,
                    "entity": entities[0].key,
                    "today": bucket == today_bucket,
                }
            )
            continue
        groups.append(
            {
                "key": bucket.isoformat(),
                "label": label,
                "sub": sub,
                "span": len(entities),
                "today": bucket == today_bucket,
            }
        )
        for entity in entities:
            columns.append(
                {
                    "key": f"{bucket.isoformat()}|{entity.key}",
                    "group": bucket.isoformat(),
                    "label": _entity_label(entity, multi),
                    "entity": entity.key,
                    "applet": entity.applet_name,
                    "today": bucket == today_bucket,
                }
            )
    return _grid_by_bucket(
        "fixed",
        data,
        params,
        computed,
        entities,
        groups,
        columns,
        bucket_of=lambda participant, day: _bucket(day, params.granularity).isoformat(),
        notes=_common_notes(data),
    )


def _grid_by_bucket(
    view,
    data,
    params,
    computed,
    entities,
    groups,
    columns,
    *,
    bucket_of,
    notes,
    row_sub=None,
    row_meta=None,
    extra_params=None,
) -> GridResult:
    col_index = {column["key"]: index for index, column in enumerate(columns)}
    entity_by_key = {entity.key: entity for entity in entities}
    details: dict[tuple[str, str], list[dict]] = defaultdict(list)
    rows: list[dict] = []
    cells_by_row: list[list[Counts | None]] = []
    occurrences_by_participant: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in computed.occurrences:
        occurrences_by_participant[occurrence.participant].append(occurrence)
    extras_by_participant: dict[str, list[Extra]] = defaultdict(list)
    for extra in computed.extras:
        extras_by_participant[extra.participant].append(extra)

    for participant in computed.participants:
        cells: list[Counts | None] = [
            Counts() if _in_applet(participant, entity_by_key[column["entity"]]) else None for column in columns
        ]
        for occurrence in occurrences_by_participant.get(participant.key, ()):
            bucket = bucket_of(participant, occurrence.day)
            if bucket is None:
                continue
            index = col_index.get(f"{bucket}|{occurrence.entity}")
            if index is None or cells[index] is None:
                continue
            cells[index].add(occurrence)  # type: ignore[union-attr]
            details[(participant.key, columns[index]["key"])].append(_occurrence_detail(data, occurrence))
        for extra in extras_by_participant.get(participant.key, ()):
            bucket = bucket_of(participant, extra.day)
            if bucket is None:
                continue
            index = col_index.get(f"{bucket}|{extra.entity}")
            if index is None or cells[index] is None:
                continue
            cells[index].add_extra(extra)  # type: ignore[union-attr]
            details[(participant.key, columns[index]["key"])].append(_extra_detail(data, extra))
        rows.append(
            {
                "key": participant.key,
                "label": participant.secret_id,
                "sub": row_sub(participant) if row_sub else _participant_sub(participant, data),
                "meta": row_meta(participant) if row_meta else _participant_meta(participant, data),
                "cells": [cell.to_json(params.late_counts_as_done) if cell is not None else None for cell in cells],
                "summary": _row_summary(
                    cells, params.late_counts_as_done, _next_due(occurrences_by_participant.get(participant.key, []))
                ),
            }
        )
        cells_by_row.append(cells)
    if not entities:
        notes = [*notes, "No activities to show: every activity is hidden, reviewer-only, or filtered out."]
    return _finish(view, params, groups, columns, rows, cells_by_row, details, LEGEND, notes, extra_params)


def participant_starts(
    data: StudyData, params: ViewParams, config_manual: dict[str, str]
) -> dict[str, tuple[date | None, str]]:
    """Day 1 of each participant for the rolling view, with where it came from."""
    completions_by_participant: dict[str, list[Completion]] = defaultdict(list)
    for completion in data.completions:
        completions_by_participant[completion.participant].append(completion)

    def manual(p: Participant) -> date | None:
        text = config_manual.get(p.secret_id) or config_manual.get(p.key)
        return _parse_day(text) if text else None

    def joined(p: Participant) -> date | None:
        days = [day for day in p.joined.values() if day]
        return min(days) if days else None

    def individual(p: Participant) -> date | None:
        days = [
            day
            for rule in participant_rules(data, p)
            if rule.is_individual
            for day in (rule.start_date, rule.selected_date)
            if day
        ]
        return min(days) if days else None

    def first_completion(p: Participant) -> date | None:
        items = completions_by_participant.get(p.key, [])
        if params.start_entity:
            items = [c for c in items if c.entity == params.start_entity or c.activity_key == params.start_entity]
        return min((c.end_local.date() for c in items), default=None)

    sources = {
        "manual": manual,
        "joined": joined,
        "individual_schedule": individual,
        "first_completion": first_completion,
    }
    labels = {
        "manual": "entered by hand",
        "joined": "added to the applet",
        "individual_schedule": "individual schedule",
        "first_completion": "first completion",
    }
    order = [params.start_rule] + [rule for rule in ("joined", "first_completion") if rule != params.start_rule]
    starts: dict[str, tuple[date | None, str]] = {}
    for participant in data.participants.values():
        chosen: tuple[date | None, str] = (None, "")
        for rule in order:
            day = sources[rule](participant)
            if day:
                chosen = (day, labels[rule] + ("" if rule == params.start_rule else " (fallback)"))
                break
        starts[participant.key] = chosen
    return starts


def _rolling(data: StudyData, params: ViewParams, config_manual: dict[str, str] | None = None) -> GridResult:
    """Same schedule for everyone, but each participant's own day 1: columns are study days."""
    entities = _entities(data, params)
    starts = participant_starts(data, params, config_manual or {})
    known = [day for day, _ in starts.values() if day]
    notes = _common_notes(data)
    if not known:
        notes.append("No participant has a start date yet, so there is nothing to line up.")
        computed = compute(data, _options(params, params.today, params.today - timedelta(days=1)))
        first = params.today
    else:
        first = min(known)
        last = max(known) + timedelta(days=params.study_days - 1)
        computed = compute(data, _options(params, first, last))

    multi = len(data.applets) > 1
    single = len(entities) == 1
    weekly = params.granularity == "week"
    buckets = list(range(1, (params.study_days + 6) // 7 + 1)) if weekly else list(range(1, params.study_days + 1))
    groups: list[dict] = []
    columns: list[dict] = []
    for bucket in buckets:
        label = f"Week {bucket}" if weekly else f"Day {bucket}"
        sub = f"days {(bucket - 1) * 7 + 1}–{min(bucket * 7, params.study_days)}" if weekly else ""
        if single:
            columns.append(
                {"key": f"{bucket}|{entities[0].key}", "label": label, "sub": sub, "entity": entities[0].key}
            )
            continue
        groups.append({"key": str(bucket), "label": label, "sub": sub, "span": len(entities)})
        for entity in entities:
            columns.append(
                {
                    "key": f"{bucket}|{entity.key}",
                    "group": str(bucket),
                    "label": _entity_label(entity, multi),
                    "entity": entity.key,
                    "applet": entity.applet_name,
                }
            )

    def bucket_of(participant: Participant, day: date) -> str | None:
        start, _ = starts.get(participant.key, (None, ""))
        if start is None:
            return None
        index = (day - start).days + 1
        if index < 1 or index > params.study_days:
            return None
        return str((index - 1) // 7 + 1) if weekly else str(index)

    def row_sub(participant: Participant) -> str:
        start, source = starts.get(participant.key, (None, ""))
        base = _participant_sub(participant, data)
        start_text = f"day 1 = {start.isoformat()} ({source})" if start else "no start date"
        return f"{start_text} · {base}" if base else start_text

    def row_meta(participant: Participant) -> dict:
        start, source = starts.get(participant.key, (None, ""))
        return _participant_meta(participant, data, start, source)

    return _grid_by_bucket(
        "rolling",
        data,
        params,
        computed,
        entities,
        groups,
        columns,
        bucket_of=bucket_of,
        notes=notes,
        row_sub=row_sub,
        row_meta=row_meta,
        extra_params={"first": first.isoformat(), "last": (first + timedelta(days=params.study_days - 1)).isoformat()},
    )


def _cross(data: StudyData, params: ViewParams) -> GridResult:
    """One column per activity or flow; a cell is done, partly done, or not done."""
    entities = _entities(data, params)
    span_first, span_last = data.day_span()
    first = span_first or params.today - timedelta(days=365)
    last = max(span_last or params.today, params.today)
    if (last - first).days > 3 * 365:
        first = last - timedelta(days=3 * 365)
    computed = compute(data, _options(params, first, last))
    multi = len(data.applets) > 1
    columns = [
        {"key": entity.key, "label": _entity_label(entity, multi), "entity": entity.key, "applet": entity.applet_name}
        for entity in entities
    ]
    by_pe = computed.by_participant_entity()
    extras_pe = computed.extras_by_participant_entity()
    details: dict[tuple[str, str], list[dict]] = defaultdict(list)
    rows: list[dict] = []
    cells_by_row: list[list[Counts | None]] = []
    for participant in computed.participants:
        cells: list[Counts | None] = []
        for entity in entities:
            if not _in_applet(participant, entity):
                cells.append(None)
                continue
            occurrences = by_pe.get((participant.key, entity.key), [])
            extras = extras_pe.get((participant.key, entity.key), [])
            items: list[Done] = [item for o in occurrences for item in o.done] + [extra.item for extra in extras]
            complete = [item for item in items if isinstance(item, Completion) or item.complete]
            cell = Counts()
            if complete:
                cell.e, cell.d, cell.x = 1, 1, len(items)
                for item in items:
                    cell._mark(item.end_local)
            elif items:
                cell.e, cell.p, cell.x = 1, 1, len(items)
                for item in items:
                    cell._mark(item.end_local)
            elif any(o.status == MISSED for o in occurrences):
                cell.e, cell.m = 1, 1
            elif any(o.status == DUE for o in occurrences) or (participant.key, entity.key) in computed.once:
                cell.e, cell.due = 1, 1  # available now (an always-available one-time activity counts as open)
            elif occurrences:
                cell.e, cell.up = 1, 1
            cells.append(cell)
            for occurrence in occurrences:
                details[(participant.key, entity.key)].append(_occurrence_detail(data, occurrence))
            for extra in extras:
                details[(participant.key, entity.key)].append(_extra_detail(data, extra))
        applicable = [cell for cell in cells if cell is not None]
        done = sum(1 for cell in applicable if cell.d)
        summary = {
            "e": len(applicable),
            "d": done,
            "l": 0,
            "p": sum(1 for cell in applicable if cell.p),
            "m": sum(1 for cell in applicable if cell.m),
            "u": sum(1 for cell in applicable if cell.due or cell.up),
            "x": sum(cell.x for cell in applicable),
            "pct": round(done / len(applicable), 3) if applicable else None,
            "last": _iso(max((cell.last_done for cell in applicable if cell.last_done), default=None)),
            "next": _iso(_next_due([o for e in entities for o in by_pe.get((participant.key, e.key), [])])),
        }
        rows.append(
            {
                "key": participant.key,
                "label": participant.secret_id,
                "sub": _participant_sub(participant, data),
                "meta": _participant_meta(participant, data),
                "cells": [cell.to_json(True) if cell is not None else None for cell in cells],
                "summary": summary,
            }
        )
        cells_by_row.append(cells)
    result = _finish("cross", params, [], columns, rows, cells_by_row, details, CROSS_LEGEND, _common_notes(data))
    # Totals for a one-shot study: how many participants have done each activity.
    totals = []
    for index in range(len(columns)):
        column_cells = [cells[index] for cells in cells_by_row if cells[index] is not None]
        if not column_cells:
            totals.append(None)
            continue
        total = Counts()
        total.e = len(column_cells)
        total.d = sum(1 for cell in column_cells if cell.d)
        total.p = sum(1 for cell in column_cells if cell.p)
        total.m = sum(1 for cell in column_cells if cell.m)
        total.up = sum(1 for cell in column_cells if cell.due or cell.up)
        total.x = sum(cell.x for cell in column_cells)
        payload = total.to_json(True)
        payload["s"] = "none"
        totals.append(payload)
    result.grid["totals"] = totals
    result.grid["overall"] = {
        "participants": len(rows),
        "e": sum(row["summary"]["e"] for row in rows),
        "d": sum(row["summary"]["d"] for row in rows),
        "pct": round(sum(r["summary"]["d"] for r in rows) / sum(r["summary"]["e"] for r in rows), 3)
        if sum(r["summary"]["e"] for r in rows)
        else None,
    }
    return result


def _individual(data: StudyData, params: ViewParams) -> GridResult:
    """Everyone on their own schedule: one column per day, all activities folded into the cell.
    With `focus`, one participant's activities become the rows."""
    entities = _entities(data, params)
    computed = compute(data, _options(params, params.first, params.last))
    buckets = _buckets(params.first, params.last, params.granularity)
    today_bucket = _bucket(params.today, params.granularity)
    columns = []
    for bucket in buckets:
        label, sub = _bucket_label(bucket, params.granularity)
        columns.append({"key": bucket.isoformat(), "label": label, "sub": sub, "today": bucket == today_bucket})
    col_index = {column["key"]: index for index, column in enumerate(columns)}
    details: dict[tuple[str, str], list[dict]] = defaultdict(list)
    rows: list[dict] = []
    cells_by_row: list[list[Counts | None]] = []
    notes = _common_notes(data)
    multi = len(data.applets) > 1

    if params.focus:
        participant = data.participants.get(params.focus)
        if participant is None:
            raise ValueError("No such participant.")
        own_occurrences = [o for o in computed.occurrences if o.participant == participant.key]
        own_extras = [x for x in computed.extras if x.participant == participant.key]
        for entity in entities:
            if not _in_applet(participant, entity):
                continue
            cells: list[Counts | None] = [Counts() for _ in columns]
            for occurrence in own_occurrences:
                if occurrence.entity != entity.key:
                    continue
                index = col_index.get(_bucket(occurrence.day, params.granularity).isoformat())
                if index is not None:
                    cells[index].add(occurrence)  # type: ignore[union-attr]
                    details[(entity.key, columns[index]["key"])].append(_occurrence_detail(data, occurrence))
            for extra in own_extras:
                if extra.entity != entity.key:
                    continue
                index = col_index.get(_bucket(extra.day, params.granularity).isoformat())
                if index is not None:
                    cells[index].add_extra(extra)  # type: ignore[union-attr]
                    details[(entity.key, columns[index]["key"])].append(_extra_detail(data, extra))
            rows.append(
                {
                    "key": entity.key,
                    "label": _entity_label(entity, multi),
                    "sub": "individual schedule"
                    if any(o.individual for o in own_occurrences if o.entity == entity.key)
                    else "default schedule",
                    "meta": {
                        "kind": "activity",
                        "applet": entity.applet_name,
                        "activityKind": entity.kind,
                        "schedule": "individual"
                        if any(o.individual for o in own_occurrences if o.entity == entity.key)
                        else "default",
                    },
                    "cells": [cell.to_json(params.late_counts_as_done) for cell in cells],
                    "summary": _row_summary(
                        cells,
                        params.late_counts_as_done,
                        _next_due([o for o in own_occurrences if o.entity == entity.key]),
                    ),
                }
            )
            cells_by_row.append(cells)
        return _finish(
            "individual",
            params,
            [],
            columns,
            rows,
            cells_by_row,
            details,
            LEGEND,
            notes,
            {"focusLabel": participant.label},
        )

    occurrences_by_participant: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in computed.occurrences:
        occurrences_by_participant[occurrence.participant].append(occurrence)
    extras_by_participant: dict[str, list[Extra]] = defaultdict(list)
    for extra in computed.extras:
        extras_by_participant[extra.participant].append(extra)
    entity_keys = {entity.key for entity in entities}
    for participant in computed.participants:
        cells = [Counts() for _ in columns]
        for occurrence in occurrences_by_participant.get(participant.key, ()):
            if occurrence.entity not in entity_keys:
                continue
            index = col_index.get(_bucket(occurrence.day, params.granularity).isoformat())
            if index is not None:
                cells[index].add(occurrence)  # type: ignore[union-attr]
                details[(participant.key, columns[index]["key"])].append(_occurrence_detail(data, occurrence))
        for extra in extras_by_participant.get(participant.key, ()):
            if extra.entity not in entity_keys:
                continue
            index = col_index.get(_bucket(extra.day, params.granularity).isoformat())
            if index is not None:
                cells[index].add_extra(extra)  # type: ignore[union-attr]
                details[(participant.key, columns[index]["key"])].append(_extra_detail(data, extra))
        individual_count = sum(1 for applet_id, flag in participant.individual.items() if flag)
        base = _participant_sub(participant, data)
        schedule = "individual schedule" if individual_count else "default schedule"
        rows.append(
            {
                "key": participant.key,
                "label": participant.secret_id,
                "sub": f"{schedule} · {base}" if base else schedule,
                "meta": _participant_meta(participant, data),
                "cells": [cell.to_json(params.late_counts_as_done) for cell in cells],
                "summary": _row_summary(
                    cells, params.late_counts_as_done, _next_due(occurrences_by_participant.get(participant.key, []))
                ),
            }
        )
        cells_by_row.append(cells)
    return _finish("individual", params, [], columns, rows, cells_by_row, details, LEGEND, notes)


def build(data: StudyData, config: StudyConfig, params: ViewParams) -> GridResult:
    if params.view == "rolling":
        return _rolling(data, params, config.manual_starts)
    return build_view(data, params)


# -- CSV -----------------------------------------------------------------------------------


def cell_text(cell: dict | None, view: str) -> str:
    if cell is None:
        return ""
    if view == "cross":
        if cell["d"]:
            return f"done {cell.get('t', '')}".strip()
        if cell["p"]:
            return "partial"
        if cell["m"]:
            return "missed"
        if cell["u"]:
            return "scheduled"
        return ""
    if cell["e"] == 0:
        return f"+{cell['x']}" if cell["x"] else ""
    text = f"{cell['d']}/{cell['e']}"
    if cell["x"]:
        text += f" +{cell['x']}"
    return text


def grid_csv(grid: dict) -> str:
    """The grid as shown: one row per participant, one column per cell, with the status in the text."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    groups = {group["key"]: group["label"] for group in grid.get("groups") or []}
    headers = []
    for column in grid["columns"]:
        label = column["label"]
        if column.get("group"):
            label = f"{groups.get(column['group'], column['group'])} / {label}"
        headers.append(label)
    writer.writerow(["participant", "details", "adherence", *headers])
    for row in grid["rows"]:
        pct = row["summary"].get("pct")
        writer.writerow(
            [row["label"], row.get("sub") or "", f"{pct:.0%}" if pct is not None else ""]
            + [
                (f"{cell_text(cell, grid['view'])} ({cell['s']})" if cell_text(cell, grid["view"]) else cell["s"])
                if cell is not None
                else ""
                for cell in row["cells"]
            ]
        )
    return out.getvalue()


LONG_COLUMNS = [
    "participant",
    "applet",
    "activity",
    "day",
    "window_start",
    "window_end",
    "status",
    "schedule",
    "periodicity",
    "completed_start",
    "completed_end",
    "in_window",
    "complete",
    "submit_id",
]


def long_csv(result: GridResult) -> str:
    """One row per expected occurrence (and per completion of it), across the whole grid."""
    out = io.StringIO()
    writer = csv.DictWriter(out, LONG_COLUMNS, lineterminator="\n")
    writer.writeheader()
    seen: set[tuple] = set()
    for (row_key, column_key), items in sorted(result.details.items()):
        for detail in items:
            base = {
                "participant": detail["participant"],
                "applet": detail["applet"],
                "activity": detail["entity"],
                "day": detail["day"],
                "window_start": detail["windowStart"] or "",
                "window_end": detail["windowEnd"] or "",
                "status": detail["status"],
                "schedule": detail["schedule"],
                "periodicity": detail["periodicity"],
            }
            if not detail["completions"]:
                key = (detail["participant"], detail["entity"], detail["day"], detail["windowStart"], "")
                if key not in seen:
                    seen.add(key)
                    writer.writerow(
                        {
                            **base,
                            "completed_start": "",
                            "completed_end": "",
                            "in_window": "",
                            "complete": "",
                            "submit_id": "",
                        }
                    )
                continue
            for completion in detail["completions"]:
                key = (
                    detail["participant"],
                    detail["entity"],
                    detail["day"],
                    detail["windowStart"],
                    completion["submitId"],
                )
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow(
                    {
                        **base,
                        "completed_start": completion["start"] or "",
                        "completed_end": completion["end"] or "",
                        "in_window": "yes" if completion["inWindow"] else "no",
                        "complete": "yes" if completion["complete"] else "no",
                        "submit_id": completion["submitId"],
                    }
                )
    return out.getvalue()


CELL_COLUMNS = [
    "participant",
    "column",
    "column_group",
    "column_label",
    "applet",
    "activity",
    "expected",
    "done",
    "late",
    "partly_done",
    "missed",
    "open_or_upcoming",
    "unscheduled",
    "status",
    "first_completion",
]

COUNT_FIELDS = [
    ("expected", "e"),
    ("done", "d"),
    ("late", "l"),
    ("partly_done", "p"),
    ("missed", "m"),
    ("open_or_upcoming", "u"),
    ("unscheduled", "x"),
]


def _column_parts(column: dict) -> tuple[str, str]:
    """(applet, activity) of a column; the activity label without the applet prefix."""
    applet = column.get("applet") or ""
    label = column.get("label") or ""
    if column.get("entity") is None:
        return "", ""  # a day column of the individual view: all activities folded in
    prefix = f"{applet}: " if applet else ""
    return applet, label[len(prefix) :] if prefix and label.startswith(prefix) else label


def cells_csv(grid: dict) -> str:
    """The grid as data: one row per participant (or activity, in a focused timeline) and column."""
    out = io.StringIO()
    columns = list(CELL_COLUMNS)
    if grid["params"].get("focus"):
        columns[0] = "row"
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    groups = {group["key"]: group["label"] for group in grid.get("groups") or []}
    for row in grid["rows"]:
        for column, cell in zip(grid["columns"], row["cells"], strict=True):
            applet, activity = _column_parts(column)
            base = [
                row["label"],
                column["key"],
                groups.get(column.get("group"), ""),
                column["label"],
                applet,
                activity,
            ]
            if cell is None:
                writer.writerow(base + [""] * len(COUNT_FIELDS) + ["not_in_applet", ""])
                continue
            writer.writerow(base + [cell[short] for _, short in COUNT_FIELDS] + [cell["s"], cell.get("t", "")])
    return out.getvalue()


def rows_csv(grid: dict) -> str:
    """One row per participant with their totals (or per activity, in a focused timeline)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    totals = [name for name, _ in COUNT_FIELDS]
    focused = bool(grid["params"].get("focus"))
    if focused:
        writer.writerow(["activity", "applet", "kind", "schedule", *totals, "adherence", "last_completion", "next_due"])
    else:
        writer.writerow(
            [
                "participant",
                "nickname",
                "tag",
                "applets",
                "joined",
                "schedule",
                "removed",
                "start_date",
                "start_source",
                *totals,
                "adherence",
                "last_completion",
                "next_due",
            ]
        )
    for row in grid["rows"]:
        meta = row.get("meta") or {}
        summary = row["summary"]
        counts = [summary.get(short, "") for _, short in COUNT_FIELDS]
        pct = summary.get("pct")
        tail = [f"{pct:.3f}" if pct is not None else "", summary.get("last") or "", summary.get("next") or ""]
        if focused:
            writer.writerow(
                [
                    row["label"],
                    meta.get("applet", ""),
                    meta.get("activityKind", ""),
                    meta.get("schedule", ""),
                    *counts,
                    *tail,
                ]
            )
        else:
            writer.writerow(
                [
                    row["label"],
                    meta.get("nickname", ""),
                    meta.get("tag", ""),
                    ", ".join(meta.get("applets") or []),
                    meta.get("joined") or "",
                    meta.get("schedule", ""),
                    "yes" if meta.get("removed") else "no",
                    meta.get("start") or "",
                    meta.get("startSource") or "",
                    *counts,
                    *tail,
                ]
            )
    return out.getvalue()
