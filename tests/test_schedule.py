"""The schedule engine: which days and windows an event produces, and how completions are matched."""

from __future__ import annotations

from datetime import date, datetime, time

from curious_download.dashboard.model import (
    DONE,
    LATE,
    MISSED,
    PARTIAL,
    UPCOMING,
    Completion,
    Entity,
    Options,
    Participant,
    StudyData,
    compute,
)
from curious_download.dashboard.schedule import (
    EventRule,
    bound_current_rules,
    expand,
    merge_rules,
    occurrence_days,
    rule_from_event,
    rules_from_history,
)

APPLET = "applet-1"


def rule(periodicity, **kwargs) -> EventRule:
    base = {
        "event_id": "ev",
        "applet_id": APPLET,
        "kind": "activity",
        "entity_id": "act",
        "user_id": None,
        "periodicity": periodicity,
    }
    return EventRule(**{**base, **kwargs})


def days(r: EventRule, first=date(2026, 10, 1), last=date(2026, 10, 31)) -> list[date]:
    return list(occurrence_days(r, first, last))


# -- occurrence days -------------------------------------------------------------------


def test_daily_within_its_dates_only():
    r = rule("DAILY", start_date=date(2026, 10, 5), end_date=date(2026, 10, 7))
    assert days(r) == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]


def test_weekdays_skip_weekends():
    r = rule("WEEKDAYS", start_date=date(2026, 10, 2), end_date=date(2026, 10, 7))  # Fri .. Wed
    assert [d.weekday() for d in days(r)] == [4, 0, 1, 2]


def test_weekly_uses_the_weekday_of_the_selected_date():
    r = rule(
        "WEEKLY", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), selected_date=date(2026, 10, 7)
    )  # a Wednesday
    assert days(r) == [date(2026, 10, 7), date(2026, 10, 14), date(2026, 10, 21), date(2026, 10, 28)]


def test_monthly_clamps_to_short_months_like_the_phone():
    r = rule("MONTHLY", start_date=date(2026, 1, 31), end_date=date(2026, 4, 30), selected_date=date(2026, 1, 31))
    assert days(r, date(2026, 1, 1), date(2026, 4, 30)) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_once_is_the_selected_day_only_and_always_has_no_expected_days():
    once = rule("ONCE", selected_date=date(2026, 10, 12))
    assert days(once) == [date(2026, 10, 12)]
    assert days(once, date(2026, 11, 1), date(2026, 11, 30)) == []
    assert days(rule("ALWAYS")) == []


def test_active_range_from_history_limits_the_days():
    r = rule(
        "DAILY",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        active_from=date(2026, 10, 10),
        active_to=date(2026, 10, 12),
    )
    assert days(r) == [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]


# -- windows -----------------------------------------------------------------------------


def test_window_spreads_into_the_next_day_when_end_is_before_start():
    r = rule("DAILY", start_time=time(20, 0), end_time=time(2, 0))
    start, end = r.window(date(2026, 10, 1))
    assert (start, end) == (datetime(2026, 10, 1, 20, 0), datetime(2026, 10, 2, 2, 0))
    assert r.spreads_to_next_day


def test_access_before_schedule_opens_the_window_at_midnight():
    r = rule("DAILY", start_time=time(9, 0), end_time=time(17, 0), access_before_schedule=True)
    start, end = r.window(date(2026, 10, 1))
    assert (start, end) == (datetime(2026, 10, 1, 0, 0), datetime(2026, 10, 1, 17, 0))


# -- rules from the API -------------------------------------------------------------------


def test_rule_from_event_reads_the_api_shape():
    event = {
        "id": "e1",
        "startTime": "08:30:00",
        "endTime": "11:00:00",
        "accessBeforeSchedule": False,
        "oneTimeCompletion": None,
        "timerType": "NOT_SET",
        "periodicity": {
            "type": "WEEKLY",
            "startDate": "2026-10-01",
            "endDate": "2026-12-31",
            "selectedDate": "2026-10-05",
        },
        "respondentId": "user-9",
        "activityId": None,
        "flowId": "flow-1",
        "version": "20261001-1",
    }
    r = rule_from_event(APPLET, event)
    assert r.kind == "flow" and r.entity == f"{APPLET}:flow:flow-1" and r.is_individual
    assert (r.start_time, r.end_time, r.selected_date) == (time(8, 30), time(11, 0), date(2026, 10, 5))
    assert rule_from_event(APPLET, {**event, "flowId": None}) is None


def test_merge_rules_replaces_defaults_per_activity_only():
    default_morning = rule("DAILY", event_id="d1", entity_id="morning")
    default_evening = rule("DAILY", event_id="d2", entity_id="evening")
    own_morning = rule("WEEKDAYS", event_id="i1", entity_id="morning", user_id="u1")
    merged = merge_rules([default_morning, default_evening], [own_morning])
    assert {r.event_id for r in merged} == {"i1", "d2"}


def test_history_gives_versions_active_ranges_and_deleted_events():
    rows = [
        {"eventId": "e1", "eventVersion": "v1", "eventVersionCreatedAt": "2026-10-01T09:00:00", "eventVersionUpdatedAt": "2026-10-01T09:00:00",
         "eventVersionIsDeleted": False, "activityOrFlowId": "act", "eventType": "activity", "periodicity": "DAILY",
         "startDate": "2026-10-01", "endDate": "2026-10-31", "startTime": "08:00:00", "endTime": "12:00:00", "userId": None},
        # the same version linked with another applet version: a duplicate
        {"eventId": "e1", "eventVersion": "v1", "eventVersionCreatedAt": "2026-10-01T09:00:00", "eventVersionUpdatedAt": "2026-10-01T09:00:00",
         "eventVersionIsDeleted": False, "activityOrFlowId": "act", "eventType": "activity", "periodicity": "DAILY",
         "startDate": "2026-10-01", "endDate": "2026-10-31", "startTime": "08:00:00", "endTime": "12:00:00", "userId": None},
        {"eventId": "e1", "eventVersion": "v2", "eventVersionCreatedAt": "2026-10-10T15:00:00", "eventVersionUpdatedAt": "2026-10-10T15:00:00",
         "eventVersionIsDeleted": False, "activityOrFlowId": "act", "eventType": "activity", "periodicity": "DAILY",
         "startDate": "2026-10-01", "endDate": "2026-10-31", "startTime": "09:00:00", "endTime": "12:00:00", "userId": None},
        {"eventId": "gone", "eventVersion": "v1", "eventVersionCreatedAt": "2026-10-01T09:00:00", "eventVersionUpdatedAt": "2026-10-03T09:00:00",
         "eventVersionIsDeleted": True, "activityOrFlowId": "other", "eventType": "activity", "periodicity": "DAILY",
         "startDate": "2026-10-01", "endDate": "2026-10-31", "startTime": "08:00:00", "endTime": "12:00:00", "userId": None},
    ]  # fmt: skip
    history = rules_from_history(APPLET, rows)
    by_version = {(r.event_id, r.version): r for r in history}
    assert len(history) == 3
    assert (by_version[("e1", "v1")].active_from, by_version[("e1", "v1")].active_to) == (
        date(2026, 10, 1),
        date(2026, 10, 9),
    )
    assert (by_version[("e1", "v2")].active_from, by_version[("e1", "v2")].active_to) == (date(2026, 10, 10), None)
    assert (by_version[("gone", "v1")].active_from, by_version[("gone", "v1")].active_to) == (
        date(2026, 10, 1),
        date(2026, 10, 3),
    )

    current = [
        rule(
            "DAILY",
            event_id="e1",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            start_time=time(9, 0),
            end_time=time(12, 0),
            version="v2",
        )
    ]
    bounded = bound_current_rules(current, history)
    slots = expand(bounded, date(2026, 10, 1), date(2026, 10, 31))
    act = [s for s in slots if s.rule.entity_id == "act"]
    assert len(act) == 31 and len({s.day for s in act}) == 31, "one occurrence per day, versions never overlap"
    assert act[0].start.time() == time(8, 0) and act[-1].start.time() == time(9, 0)
    assert [s.day for s in slots if s.rule.entity_id == "other"] == [
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
    ]


# -- matching ---------------------------------------------------------------------------------


def study() -> StudyData:
    data = StudyData(study_id="s", server="https://x", loaded_at=datetime(2026, 10, 15))
    data.applets[APPLET] = {"id": APPLET, "name": "EMA", "ownerId": "o", "version": "1"}
    for order, (entity_id, name) in enumerate(
        (("morning", "Morning"), ("evening", "Evening"), ("w1", "W1"), ("w2", "W2"))
    ):
        key = f"{APPLET}:activity:{entity_id}"
        data.entities[key] = Entity(
            key=key, applet_id=APPLET, kind="activity", id=entity_id, name=name, applet_name="EMA", order=order
        )
    flow_key = f"{APPLET}:flow:weekly"
    data.entities[flow_key] = Entity(
        key=flow_key,
        applet_id=APPLET,
        kind="flow",
        id="weekly",
        name="Weekly",
        applet_name="EMA",
        activity_ids=("w1", "w2"),
    )
    data.rules[APPLET] = [
        rule("DAILY", event_id="ev-m", entity_id="morning", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), start_time=time(8), end_time=time(12)),
        rule("DAILY", event_id="ev-e", entity_id="evening", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), start_time=time(20), end_time=time(2)),
        rule("WEEKLY", event_id="ev-f", kind="flow", entity_id="weekly", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), selected_date=date(2026, 10, 5), start_time=time(0), end_time=time(23, 59)),
    ]  # fmt: skip
    data.participants["P001"] = Participant(
        key="P001",
        secret_id="P001",
        user_ids={APPLET: "u1"},
        subject_ids={APPLET: "s1"},
        joined={APPLET: date(2026, 10, 1)},
        tz_offset=-240,
    )
    return data


def completion(
    answer_id, activity, end: datetime, *, event=None, scheduled: datetime | None = None, flow=None, submit=None
) -> Completion:
    return Completion(
        answer_id=answer_id, submit_id=submit or answer_id, participant="P001", applet_id=APPLET, activity_id=activity, flow_id=flow,
        scheduled_event_id=event, scheduled_local=scheduled, start_local=end, end_local=end, created_utc=end, tz_offset=-240,
    )  # fmt: skip


def run(data: StudyData, now_local=datetime(2026, 10, 8, 10, 0)):
    options = Options(
        first=date(2026, 10, 1), last=date(2026, 10, 10), now_utc=now_local.replace(hour=now_local.hour + 4)
    )
    return compute(data, options)


def status_of(computed, entity_id: str, day: date) -> str:
    kind = "flow" if entity_id == "weekly" else "activity"
    return next(o.status for o in computed.occurrences if o.entity == f"{APPLET}:{kind}:{entity_id}" and o.day == day)


def test_completions_are_pinned_by_the_phones_event_and_scheduled_day():
    data = study()
    data.completions = [
        completion("a", "morning", datetime(2026, 10, 1, 9), event="ev-m", scheduled=datetime(2026, 10, 1, 8)),
        completion("b", "morning", datetime(2026, 10, 2, 13), event="ev-m", scheduled=datetime(2026, 10, 2, 8)),
        # finished at 01:30 on the 3rd, but it was the evening of the 2nd
        completion("c", "evening", datetime(2026, 10, 3, 1, 30), event="ev-e", scheduled=datetime(2026, 10, 2, 20)),
    ]
    computed = run(data)
    assert status_of(computed, "morning", date(2026, 10, 1)) == DONE
    assert status_of(computed, "morning", date(2026, 10, 2)) == LATE
    assert status_of(computed, "evening", date(2026, 10, 2)) == DONE
    assert status_of(computed, "evening", date(2026, 10, 3)) == MISSED
    assert status_of(computed, "morning", date(2026, 10, 8)) == "due"  # 10:00 local, window open
    assert status_of(computed, "morning", date(2026, 10, 9)) == UPCOMING
    assert computed.extras == []


def test_legacy_completions_match_by_window_and_repeats_become_extras():
    data = study()
    data.completions = [
        completion("a", "morning", datetime(2026, 10, 3, 10)),
        completion("b", "morning", datetime(2026, 10, 3, 11)),
        completion("c", "morning", datetime(2026, 10, 4, 15)),  # nothing open at 15:00: nearest occurrence, late
    ]
    computed = run(data)
    assert status_of(computed, "morning", date(2026, 10, 3)) == DONE
    assert status_of(computed, "morning", date(2026, 10, 4)) == LATE
    assert [(x.entity.rsplit(":", 1)[1], x.day) for x in computed.extras] == [("morning", date(2026, 10, 3))]


def test_flow_is_partial_until_every_activity_arrived():
    data = study()
    data.completions = [
        completion(
            "a",
            "w1",
            datetime(2026, 10, 5, 10),
            flow="weekly",
            submit="sub-1",
            event="ev-f",
            scheduled=datetime(2026, 10, 5, 0),
        ),
    ]
    assert status_of(run(data), "weekly", date(2026, 10, 5)) == PARTIAL
    data.completions.append(
        completion(
            "b",
            "w2",
            datetime(2026, 10, 5, 10, 5),
            flow="weekly",
            submit="sub-1",
            event="ev-f",
            scheduled=datetime(2026, 10, 5, 0),
        )
    )
    assert status_of(run(data), "weekly", date(2026, 10, 5)) == DONE


def test_nothing_is_expected_before_the_participant_joined():
    data = study()
    data.participants["P001"].joined[APPLET] = date(2026, 10, 6)
    computed = run(data)
    assert min(o.day for o in computed.occurrences) == date(2026, 10, 6)
    computed = compute(
        data,
        Options(
            first=date(2026, 10, 1), last=date(2026, 10, 10), now_utc=datetime(2026, 10, 8, 14), count_from_joined=False
        ),
    )
    assert min(o.day for o in computed.occurrences) == date(2026, 10, 1)


def test_flow_member_activities_get_no_column_of_their_own():
    data = study()
    labels = [e.name for e in data.visible_entities()]
    assert labels == ["Morning", "Evening", "Weekly"]
    data.completions = [completion("a", "w1", datetime(2026, 10, 3, 10))]  # done on its own, outside the flow
    assert [e.name for e in data.visible_entities()] == ["Morning", "Evening", "W1", "Weekly"]
