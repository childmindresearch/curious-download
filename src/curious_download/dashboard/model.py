"""The study model: participants, activities, completions, and the matching of completions to
expected occurrences. Pure functions over data the loader fetched; nothing here talks to the API."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .schedule import ALWAYS, EventRule, Slot, expand, merge_rules

# Occurrence statuses, from best to worst for colouring.
DONE = "done"
LATE = "late"  # completed, but outside the time window of the occurrence
PARTIAL = "partial"  # a flow with some of its activities missing
DUE = "due"  # window open now, nothing yet
UPCOMING = "upcoming"
MISSED = "missed"


@dataclass
class Entity:
    """An activity or activity flow of one applet."""

    key: str
    applet_id: str
    kind: str  # "activity" | "flow"
    id: str
    name: str
    applet_name: str = ""
    hidden: bool = False
    reviewable: bool = False
    deleted: bool = False  # no longer in the applet; known from old answers or the schedule history
    activity_ids: tuple[str, ...] = ()  # flows: the activities they contain, in order
    order: int = 0

    @property
    def label(self) -> str:
        name = f"Flow: {self.name}" if self.kind == "flow" else self.name
        return f"{name} (deleted)" if self.deleted else name


@dataclass
class Participant:
    """One person across the study's applets, matched by secret ID (or an alias)."""

    key: str
    secret_id: str
    nickname: str = ""
    tag: str = ""
    user_ids: dict[str, str | None] = field(default_factory=dict)  # applet id -> account id (None: shell)
    subject_ids: dict[str, str] = field(default_factory=dict)  # applet id -> subject id
    joined: dict[str, date | None] = field(default_factory=dict)  # applet id -> day the subject was created
    individual: dict[str, bool] = field(default_factory=dict)  # applet id -> has an individual schedule
    removed: dict[str, bool] = field(default_factory=dict)  # applet id -> subject deleted from the applet
    tz_offset: int | None = None  # minutes, local minus UTC, from the latest completion
    last_completion: datetime | None = None  # UTC

    @property
    def label(self) -> str:
        extra = ", ".join(part for part in (self.nickname, self.tag) if part)
        return f"{self.secret_id} ({extra})" if extra else self.secret_id

    @property
    def applet_ids(self) -> set[str]:
        return set(self.subject_ids) | set(self.user_ids)

    def is_removed_everywhere(self) -> bool:
        return bool(self.removed) and all(self.removed.get(a, False) for a in self.applet_ids)


@dataclass
class Completion:
    """One completed activity (one export row), with its times in the participant's local time."""

    answer_id: str
    submit_id: str
    participant: str
    applet_id: str
    activity_id: str
    flow_id: str | None
    scheduled_event_id: str | None
    scheduled_local: datetime | None
    start_local: datetime
    end_local: datetime
    created_utc: datetime
    tz_offset: int
    version: str = ""

    @property
    def activity_key(self) -> str:
        return f"{self.applet_id}:activity:{self.activity_id}"

    @property
    def flow_key(self) -> str | None:
        return f"{self.applet_id}:flow:{self.flow_id}" if self.flow_id else None

    @property
    def entity(self) -> str:
        return self.flow_key or self.activity_key


@dataclass
class FlowSubmission:
    """The activities of one flow completed under one submission."""

    submit_id: str
    participant: str
    entity: str
    answers: list[Completion]
    expected_activities: tuple[str, ...]

    @property
    def done_activities(self) -> set[str]:
        return {answer.activity_id for answer in self.answers}

    @property
    def complete(self) -> bool:
        expected = set(self.expected_activities)
        return not expected or expected <= self.done_activities

    @property
    def start_local(self) -> datetime:
        return min(answer.start_local for answer in self.answers)

    @property
    def end_local(self) -> datetime:
        return max(answer.end_local for answer in self.answers)

    @property
    def scheduled_event_id(self) -> str | None:
        return next((a.scheduled_event_id for a in self.answers if a.scheduled_event_id), None)

    @property
    def scheduled_local(self) -> datetime | None:
        return next((a.scheduled_local for a in self.answers if a.scheduled_local), None)


Done = Completion | FlowSubmission


@dataclass
class StudyData:
    """Everything the loader fetched for a study configuration."""

    study_id: str
    server: str
    loaded_at: datetime  # UTC
    applets: dict[str, dict] = field(default_factory=dict)  # applet id -> {id, name, ownerId, version}
    entities: dict[str, Entity] = field(default_factory=dict)
    participants: dict[str, Participant] = field(default_factory=dict)
    completions: list[Completion] = field(default_factory=list)
    rules: dict[str, list[EventRule]] = field(default_factory=dict)  # applet id -> every rule (all versions)
    schedule_available: dict[str, bool] = field(default_factory=dict)
    history_available: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def visible_entities(self) -> list[Entity]:
        """Activities and flows worth a column: not hidden, not reviewer-only, and not an activity that
        only ever appears inside a flow (no schedule of its own and never completed on its own)."""
        scheduled = {rule.entity for rules in self.rules.values() for rule in rules}
        standalone = {c.activity_key for c in self.completions if not c.flow_id}
        in_flows = {
            f"{e.applet_id}:activity:{activity_id}"
            for e in self.entities.values()
            if e.kind == "flow"
            for activity_id in e.activity_ids
        }
        return sorted(
            (
                e
                for e in self.entities.values()
                if not e.hidden
                and not e.reviewable
                and (e.key not in in_flows or e.key in scheduled or e.key in standalone)
            ),
            key=lambda e: (self.applets.get(e.applet_id, {}).get("name", ""), e.kind == "flow", e.order, e.name),
        )

    def day_span(self) -> tuple[date | None, date | None]:
        """Earliest and latest local day with a schedule date or a completion, for default ranges."""
        days: list[date] = []
        for rules in self.rules.values():
            for rule in rules:
                for bound in (rule.start_date, rule.end_date, rule.selected_date, rule.active_from):
                    if bound:
                        days.append(bound)
        for completion in self.completions:
            days.append(completion.end_local.date())
        return (min(days), max(days)) if days else (None, None)


# -- matching ---------------------------------------------------------------------------


@dataclass
class Options:
    first: date
    last: date
    now_utc: datetime
    late_counts_as_done: bool = True
    count_from_joined: bool = True
    include_removed: bool = False
    entity_keys: set[str] | None = None  # None: every visible entity
    host_tz_offset: int = 0  # minutes, used for participants with no completion yet


@dataclass
class Occurrence:
    participant: str
    entity: str
    day: date
    start: datetime
    end: datetime
    event_id: str
    periodicity: str
    individual: bool
    done: list[Done] = field(default_factory=list)
    status: str = UPCOMING

    @property
    def in_window(self) -> list[Done]:
        return [item for item in self.done if self.start <= item.end_local <= self.end]

    def judge(self, now_local: datetime) -> None:
        complete = [item for item in self.done if isinstance(item, Completion) or item.complete]
        if complete:
            self.status = DONE if any(self.start <= item.end_local <= self.end for item in complete) else LATE
        elif self.done:
            self.status = PARTIAL  # only unfinished flows
        elif now_local > self.end:
            self.status = MISSED
        elif now_local >= self.start:
            self.status = DUE
        else:
            self.status = UPCOMING


@dataclass
class Extra:
    """A completion that matched no expected occurrence (always-available activities, or unscheduled)."""

    participant: str
    entity: str
    day: date
    item: Done


@dataclass
class Computed:
    occurrences: list[Occurrence]
    extras: list[Extra]
    # Always-available activities that may be completed once: expected once per participant.
    once: set[tuple[str, str]]
    # Participants whose rows were built, in label order.
    participants: list[Participant]
    now_local: dict[str, datetime]

    def by_participant_entity(self) -> dict[tuple[str, str], list[Occurrence]]:
        index: dict[tuple[str, str], list[Occurrence]] = defaultdict(list)
        for occurrence in self.occurrences:
            index[(occurrence.participant, occurrence.entity)].append(occurrence)
        return index

    def extras_by_participant_entity(self) -> dict[tuple[str, str], list[Extra]]:
        index: dict[tuple[str, str], list[Extra]] = defaultdict(list)
        for extra in self.extras:
            index[(extra.participant, extra.entity)].append(extra)
        return index


def flow_submissions(data: StudyData, completions: list[Completion]) -> tuple[list[FlowSubmission], list[Completion]]:
    """Group flow answers by submission; return (flow submissions, standalone activity completions)."""
    grouped: dict[tuple[str, str], list[Completion]] = defaultdict(list)
    standalone: list[Completion] = []
    for completion in completions:
        if completion.flow_id:
            grouped[(completion.submit_id, completion.flow_key or "")].append(completion)
        else:
            standalone.append(completion)
    flows = []
    for (submit_id, flow_key), answers in grouped.items():
        entity = data.entities.get(flow_key)
        flows.append(
            FlowSubmission(
                submit_id=submit_id,
                participant=answers[0].participant,
                entity=flow_key,
                answers=sorted(answers, key=lambda a: a.end_local),
                expected_activities=entity.activity_ids if entity else (),
            )
        )
    return flows, standalone


def participant_rules(data: StudyData, participant: Participant) -> list[EventRule]:
    """The merged rules of one participant across the study's applets."""
    rules: list[EventRule] = []
    for applet_id in sorted(participant.applet_ids):
        all_rules = data.rules.get(applet_id, [])
        defaults = [rule for rule in all_rules if rule.user_id is None]
        user_id = participant.user_ids.get(applet_id)
        individual = [rule for rule in all_rules if user_id and rule.user_id == user_id]
        rules.extend(merge_rules(defaults, individual))
    return rules


def compute(data: StudyData, options: Options) -> Computed:
    """Expand every participant's schedule over the day range and attach their completions."""
    entities = {e.key for e in data.visible_entities()}
    if options.entity_keys is not None:
        entities &= options.entity_keys

    participants = sorted(
        (p for p in data.participants.values() if options.include_removed or not p.is_removed_everywhere()),
        key=lambda p: p.secret_id.lower(),
    )
    completions_by_participant: dict[str, list[Completion]] = defaultdict(list)
    for completion in data.completions:
        completions_by_participant[completion.participant].append(completion)

    occurrences: list[Occurrence] = []
    extras: list[Extra] = []
    once: set[tuple[str, str]] = set()
    now_local_by_participant: dict[str, datetime] = {}

    for participant in participants:
        offset = participant.tz_offset if participant.tz_offset is not None else options.host_tz_offset
        now_local = options.now_utc + timedelta(minutes=offset)
        now_local_by_participant[participant.key] = now_local

        rules = [rule for rule in participant_rules(data, participant) if rule.entity in entities]
        for rule in rules:
            if rule.periodicity == ALWAYS and rule.one_time_completion:
                once.add((participant.key, rule.entity))

        first = options.first
        if options.count_from_joined:
            joined = [day for day in participant.joined.values() if day]
            if joined and min(joined) > first:
                first = min(joined)
        slots: list[Slot] = expand(rules, first, options.last) if first <= options.last else []

        own: list[Occurrence] = [
            Occurrence(
                participant=participant.key,
                entity=slot.rule.entity,
                day=slot.day,
                start=slot.start,
                end=slot.end,
                event_id=slot.rule.event_id,
                periodicity=slot.rule.periodicity,
                individual=slot.rule.is_individual,
            )
            for slot in slots
        ]
        by_event_day = {(o.event_id, o.day): o for o in own}
        by_entity_day: dict[tuple[str, date], list[Occurrence]] = defaultdict(list)
        for occurrence in own:
            by_entity_day[(occurrence.entity, occurrence.day)].append(occurrence)

        flows, standalone = flow_submissions(data, completions_by_participant.get(participant.key, []))
        items: list[Done] = [*flows, *standalone]
        for item in items:
            if item.entity not in entities:
                continue
            target = _match(item, by_event_day, by_entity_day)
            if target is None:
                extras.append(Extra(participant.key, item.entity, item.end_local.date(), item))
            else:
                target.done.append(item)

        for occurrence in own:
            occurrence.judge(now_local)
        occurrences.extend(own)

    return Computed(occurrences, extras, once, participants, now_local_by_participant)


def _match(
    item: Done,
    by_event_day: dict[tuple[str, date], Occurrence],
    by_entity_day: dict[tuple[str, date], list[Occurrence]],
) -> Occurrence | None:
    """The occurrence a completion fulfilled.

    The phone records which event it was answering and the time it was scheduled for, which
    pins the exact day even when the answer arrived late or after midnight. Older answers
    without that are matched to an occurrence of the same activity whose window contains the
    completion, or else the nearest one on that day.
    """
    if item.scheduled_event_id and item.scheduled_local:
        found = by_event_day.get((item.scheduled_event_id, item.scheduled_local.date()))
        if found is not None:
            return found
    end = item.end_local
    same_day = list(by_entity_day.get((item.entity, end.date()), ()))
    # Yesterday's occurrence counts only if its window spreads into today (an evening ending at 02:00).
    spilled = [
        o for o in by_entity_day.get((item.entity, end.date() - timedelta(days=1)), ()) if o.end.date() >= end.date()
    ]
    # Without the phone's event reference, an occurrence takes one completion; a second one in the
    # same window is a repeat and is counted separately.
    free = [o for o in same_day + spilled if not o.done]
    if not free:
        return None
    containing = [o for o in free if o.start <= end <= o.end]
    if containing:
        return min(containing, key=lambda o: o.start)
    same_day_free = [o for o in free if o.day == end.date()]
    if not same_day_free:
        return None
    return min(same_day_free, key=lambda o: abs((o.start - end).total_seconds()))
