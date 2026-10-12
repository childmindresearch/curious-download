"""Dictionary rows back into Curious items: round trips, new items, skip logic, and local checks."""

from __future__ import annotations

import copy

import pytest

from curious_download.csvout import sanitize_value
from curious_download.dictionary import describe_logic, item_cells
from curious_download.itembuilder import (
    CREATABLE_TYPES,
    CellError,
    ItemRow,
    _unsanitize,
    build_item,
    check_activity,
    parse_rules,
    parse_shown_if,
    parse_value_labels,
    resolve_logic,
)

from .full_applet import applet


def _daily() -> dict:
    return copy.deepcopy(applet()["activities"][0])


def _as_read(cells: dict) -> dict[str, str]:
    """A row as it comes back from a CSV: sanitized on write, unsanitized and stripped on read."""
    return {k: _unsanitize(sanitize_value(v).strip()) for k, v in cells.items()}


def _row(**cells) -> ItemRow:
    base = {"activity_name": "Daily", "item_name": "x", "item_type": "text", "item_prompt": "X?"}
    return ItemRow({**base, **{k: str(v) for k, v in cells.items()}}, "test line")


@pytest.mark.parametrize("index", range(13))
def test_unedited_row_rebuilds_the_same_item(index):
    activity = _daily()
    original = activity["items"][index]
    cells = _as_read({**item_cells(original, activity, index + 1), "activity_name": "Daily"})
    item, warnings = build_item(ItemRow(cells, "line"), original, activity)
    assert item == original and warnings == []


def test_unedited_skip_logic_is_kept_as_it_was():
    activity = _daily()
    items = activity["items"]
    rows = {
        i: ItemRow(_as_read({**item_cells(item, activity, i + 1), "activity_name": "Daily"}), "line")
        for i, item in enumerate(items)
    }
    rebuilt = copy.deepcopy(items)
    assert resolve_logic(rebuilt, rows, items, activity) == []
    assert rebuilt == items


def test_every_kind_of_condition_reads_back():
    activity = _daily()
    by_name = {item["name"]: item for item in activity["items"]}
    for index, item in enumerate(activity["items"]):
        if not item["conditionalLogic"]:
            continue
        text = describe_logic(item["conditionalLogic"], by_name)
        parsed = parse_shown_if(text, {i["name"]: i for i in activity["items"][:index]})
        assert describe_logic(parsed, by_name) == text, text


def test_edits_change_only_what_was_edited():
    activity = _daily()
    mood = activity["items"][0]
    cells = _as_read({**item_cells(mood, activity, 1), "activity_name": "Daily"})
    cells["value_labels"] = "0=Bad | 1=Great | 2=Super"
    cells["scores"] = "0=0 | 1=2 | 2=5"
    item, _ = build_item(ItemRow(cells, "line"), mood, activity)
    options = item["responseValues"]["options"]
    assert [(o["value"], o["text"], o["score"]) for o in options] == [(0, "Bad", 0), (1, "Great", 2), (2, "Super", 5)]
    assert options[1]["id"] == mood["responseValues"]["options"][1]["id"], "existing option keeps its id"
    assert (options[1]["color"], options[1]["tooltip"]) == ("#ff0000", "tip"), "settings the CSV doesn't show stay"
    assert len(options[2]["id"]) == 36
    assert item["question"] == mood["question"] and item["config"] == mood["config"]


def test_prompt_edit_keeps_other_languages():
    activity = _daily()
    mood = copy.deepcopy(activity["items"][0])
    mood["question"] = {"fr": "Humeur ?", "en": "Mood?"}
    cells = _as_read({**item_cells(mood, activity, 1), "activity_name": "Daily", "item_prompt": "Your mood?"})
    item, _ = build_item(ItemRow(cells, "line"), mood, activity)
    assert item["question"] == {"fr": "Humeur ?", "en": "Your mood?"}


def test_new_items_of_every_creatable_type_pass_the_checks():
    activity = {"name": "New", "isSkippable": False, "items": []}
    rows = [
        _row(item_name="a", item_type="singleSelect", value_labels="1=Yes | 0=No", scores="1=1 | 0=0", skippable="no"),
        _row(item_name="b", item_type="multiSelect", value_labels="1=A | 2=B | 9=None (none of the above)"),
        _row(item_name="c", item_type="slider", min_value=0, max_value=10, min_label="no", max_label="yes"),
        _row(item_name="d", item_type="numberSelect", min_value=0, max_value=100),
        _row(item_name="e", item_type="text", response_rules="max 50 characters; numbers only; response required"),
        _row(item_name="f", item_type="paragraphText"),
        _row(item_name="g", item_type="date"),
        _row(item_name="h", item_type="time"),
        _row(item_name="i", item_type="timeRange"),
        _row(item_name="j", item_type="message"),
        _row(item_name="k", item_type="sliderRows", rows="Head (0=none to 5=worst) | Back (1 to 4)", scores="Head: 0=0, 1=1, 2=2, 3=3, 4=4, 5=5 | Back: 1=0, 2=1, 3=2, 4=3"),
        _row(item_name="l", item_type="singleSelectRows", rows="Morning | Evening", value_labels="Calm | Tense", scores="Morning: Calm=0, Tense=1 | Evening: Calm=0, Tense=2"),
        _row(item_name="m", item_type="multiSelectRows", rows="Mon | Tue", value_labels="Run | Swim"),
        _row(item_name="n", item_type="audio", response_rules="max 30 seconds"),
        _row(item_name="o", item_type="photo", other_text_option="required"),
        _row(item_name="p", item_type="video"),
        _row(item_name="q", item_type="drawing"),
        _row(item_name="r", item_type="geolocation", hidden="yes"),
    ]  # fmt: skip
    assert {r.cells["item_type"] for r in rows} == set(CREATABLE_TYPES)
    items = [build_item(row, None, activity)[0] for row in rows]
    activity["items"] = items
    assert check_activity(activity) == []
    by_name = {item["name"]: item for item in items}
    assert by_name["a"]["config"]["addScores"] and by_name["a"]["config"]["skippableItem"] is False
    assert [o["isNoneAbove"] for o in by_name["b"]["responseValues"]["options"]] == [False, False, True]
    assert by_name["c"]["responseValues"] == {
        "minValue": 0,
        "maxValue": 10,
        "minLabel": "no",
        "maxLabel": "yes",
        "scores": None,
    }
    rules = by_name["e"]["config"]
    assert (rules["maxResponseLength"], rules["numericalResponseRequired"], rules["responseRequired"]) == (
        50,
        True,
        True,
    )
    head, back = by_name["k"]["responseValues"]["rows"]
    assert (head["minLabel"], head["maxValue"], head["scores"], back["scores"]) == (
        "none",
        5,
        [0, 1, 2, 3, 4, 5],
        [0, 1, 2, 3],
    )
    matrix = by_name["l"]["responseValues"]["dataMatrix"]
    assert [[o["score"] for o in row["options"]] for row in matrix] == [[0, 1], [0, 2]]
    assert by_name["n"]["responseValues"] == {"maxDuration": 30}
    assert by_name["o"]["config"]["additionalResponseOption"] == {"textInputOption": True, "textInputRequired": True}
    assert by_name["r"]["isHidden"] is True
    for name in "fghijopq":
        assert by_name[name]["responseValues"] is None or name in "q"


def test_shown_if_for_new_items():
    activity = {"name": "New", "isSkippable": False, "items": []}
    rows = [
        _row(item_name="a", item_type="singleSelect", value_labels="1=Yes | 0=No"),
        _row(item_name="b", item_type="multiSelect", value_labels="1=Run | 2=Swim", shown_if='a = "Yes" (1)'),
        _row(item_name="c", item_type="text", shown_if='a = Yes AND b does not include "Swim"'),
        _row(item_name="d", item_type="text", shown_if="a = 0 OR b includes 1"),
    ]
    items = [build_item(row, None, activity)[0] for row in rows]
    assert resolve_logic(items, dict(enumerate(rows)), [], activity) == []
    assert items[1]["conditionalLogic"] == {"match": "all", "conditions": [{"itemName": "a", "type": "EQUAL_TO_OPTION", "payload": {"optionValue": "1"}}]}  # fmt: skip
    assert [c["type"] for c in items[2]["conditionalLogic"]["conditions"]] == ["EQUAL_TO_OPTION", "NOT_INCLUDES_OPTION"]
    assert items[2]["conditionalLogic"]["conditions"][1]["payload"] == {"optionValue": "2"}
    assert items[3]["conditionalLogic"]["match"] == "any"


@pytest.mark.parametrize(
    ("shown_if", "message"),
    [
        ("b = 1", "not an earlier item"),
        ("a includes 1", "use = or !="),
        ('a = "Maybe"', "has no option"),
        ("t = 3", "can't use"),
        ("a = 1 AND a = 0 OR a = 1", "mix AND and OR"),
        ("a 1", "no operator"),
    ],
)
def test_bad_shown_if(shown_if, message):
    items = [
        build_item(_row(item_name="a", item_type="singleSelect", value_labels="1=Yes | 0=No"), None, {})[0],
        build_item(_row(item_name="t", item_type="text"), None, {})[0],
    ]
    with pytest.raises(CellError, match=message):
        parse_shown_if(shown_if, {i["name"]: i for i in items})


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        ({"item_type": "singleSelect", "value_labels": "Yes | No"}, "value=label"),
        ({"item_type": "singleSelect", "value_labels": "1=Yes | 1=No"}, "used twice"),
        ({"item_type": "singleSelect", "value_labels": "1=Yes | 0=No", "scores": "1=1"}, "scores are missing"),
        ({"item_type": "singleSelect", "value_labels": "1=Yes", "scores": "5=1"}, "aren't options"),
        ({"item_type": "slider", "min_value": "0", "max_value": "3", "scores": "0=1 | 1=2"}, "cover every value"),
        ({"item_type": "flanker"}, "can't be created"),
        (
            {"item_type": "sliderRows", "rows": "A (0 to 2) | B (0 to 2)", "scores": "A: 0=0, 1=1, 2=2"},
            "every slider row",
        ),
        ({"item_type": "text", "response_rules": "sing loudly"}, "unknown rule"),
        ({"item_type": "text", "skippable": "maybe"}, "yes or no"),
    ],
)
def test_bad_cells(cells, message):
    with pytest.raises(CellError, match=message):
        build_item(_row(**cells), None, {"name": "New"})


def test_checks_catch_what_the_backend_rejects():
    activity = _daily()
    items = activity["items"]
    items[2]["responseValues"]["maxValue"] = 20  # slider beyond 12
    items[1]["isHidden"] = True  # hidden with skip logic
    items[4]["name"] = "has space"
    activity["items"] = [i for i in items if i["name"] != "mood"]  # used by a subscale, a report, and skip logic
    messages = [p.message for p in check_activity(activity) if p.severity == "error"]
    assert any("slider range 0–20" in m for m in messages)
    assert any("hidden and have skip logic" in m for m in messages)
    assert any("letters, digits" in m for m in messages)
    assert any("subscale 'Core' uses 'mood'" in m for m in messages)
    assert any("score report 'Mood score' uses 'mood'" in m for m in messages)
    assert any("refers to 'mood'" in m for m in messages)


def test_rules_and_flags_parse():
    assert parse_rules("max 20 characters; numbers only; correct answer: 42; time limit 30 seconds") == {
        "maxResponseLength": 20,
        "numericalResponseRequired": True,
        "correctAnswer": "42",
        "timer": 30,
    }
    assert parse_value_labels("0=Other (please say) | 1=None (none of the above, hidden)") == [
        (0, "Other (please say)", set()),
        (1, "None", {"none of the above", "hidden"}),
    ]
    assert _unsanitize("'=SUM(1)\n'-a\nplain") == "=SUM(1)\n-a\nplain"
