"""REDCap data dictionaries -> Curious dictionaries, with decisions, and on into an upload."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from curious_download import cli
from curious_download.client import CuriousClient
from curious_download.redcap import (
    Compare,
    Decisions,
    Logic,
    Opaque,
    annotation_tags,
    convert_redcap,
    html_to_markdown,
    parse_logic,
    validate_rows,
)

from .full_applet import FakeBuilderServer

HEADER = [
    "",  # an extra, unnamed column, as in hand-edited files
    "Variable / Field Name",
    "Form Name",
    "Section Header",
    "Field Type",
    "Field Label",
    "Choices, Calculations, OR Slider Labels",
    "Field Note",
    "Text Validation Type OR Show Slider Number",
    "Text Validation Min",
    "Text Validation Max",
    "Identifier?",
    "Branching Logic (Show field only if...)",
    "Required Field?",
    "Custom Alignment",
    "Question Number (surveys only)",
    "Matrix Group Name",
    "Matrix Ranking?",
    "Field Annotation",
]


def field(name, form, kind, label, choices="", *, section="", note="", validation="", vmin="", vmax="",
          identifier="", branching="", required="", matrix="", ranking="", annotation="", extra=""):  # fmt: skip
    return [extra, name, form, section, kind, label, choices, note, validation, vmin, vmax, identifier,
            branching, required, "", "", matrix, ranking, annotation]  # fmt: skip


B, F = "baseline", "followup"
ROWS = [
    field("intro", B, "descriptive", "<b>Welcome</b><br>Thanks for joining &amp; helping."),
    field("age", B, "text", "Age?", note="Whole years", validation="integer", vmin="10", vmax="99", required="y"),
    field("weight", B, "text", "Weight (kg)?", validation="number", vmin="0", vmax="300"),
    field("dob", B, "text", "Date of birth", validation="date_mdy", identifier="y"),
    field("visit_dt", B, "text", "Visit time", validation="datetime_ymd"),
    field(
        "sex", B, "radio", "Sex (check all that apply)", "1, Male | 2, Female | 3, Other", required="y", extra="DEMO"
    ),
    [""] * 19,
    ["", "", "", "", "", "Please answer as on your ID.", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    field("smoke", B, "yesno", "Do you smoke?", required="y"),
    field("cigs", B, "text", "How many per day?", validation="integer", branching="[smoke] = '1'"),
    field(
        "drinks",
        B,
        "checkbox",
        "Drinks?",
        "1, Beer | 2, Wine | 3, Spirits | 0, None",
        annotation="@NONEOFTHEABOVE=0 @RANDOMORDER",
    ),
    field("drinks_other", B, "text", "Which spirits or wine?", branching="[drinks(3)] = '1' or [drinks(2)] = '1'"),
    field("mood", B, "slider", "Mood?", "Bad | OK | Good"),
    field("pain", B, "slider", "Pain?", "None | Worst", vmin="0", vmax="10"),
    field("total", B, "calc", "Total", "[age]+1"),
    field("photo_id", B, "file", "Upload your ID"),
    field("sig", B, "file", "Sign here", validation="signature"),
    field("comments", B, "notes", "Comments?"),
    field("grade", B, "dropdown", "Grade?", "A, Good | B, Bad"),
    field("mixed", B, "text", "Mixed logic", branching="[grade] = 'A' and ([sex] = '1' or [sex] = '2')"),
    field("too_early", B, "text", "Refers ahead", branching="[later] = '1'"),
    field("later", B, "radio", "Later?", "1, Yes | 0, No"),
    field(
        "f1", B, "radio", "Calm", "1, Never | 2, Often", section="How often do you feel...", matrix="feel", required="y"
    ),
    field("f2", B, "radio", "Tense", "1, Never | 2, Often", matrix="feel", required="y"),
    field("older", B, "text", "Older than 18", branching="[age] >= '18'"),
    field("often_calm", B, "text", "Calm often", branching="[f1] > 1"),
    field("secret", B, "text", "Hidden", annotation="@HIDDEN", branching="[smoke] = '1'"),
    field("thanks", B, "descriptive", "Thanks, [sex]! Ignore [unknown]."),
    field(
        "pattern",
        B,
        "text",
        "Most common Virtual Clinic shape",
        branching="([drinks(1)] = '1' or [drinks(2)] = '1') and [smoke] = '1' and [sex] = '2'",
    ),
    field("either", B, "text", "Or of ands", branching="([smoke] = '1' and [sex] = '1') or [grade] = 'B'"),
    field("by_age", B, "text", "Uses a function", branching="datediff([dob], 'today', 'y') >= 18 and [smoke] = '0'"),
    field(
        "ifchoice",
        B,
        "radio",
        "Conditional choices",
        "1, A | 2, B | 3, C",
        annotation="@IF([smoke]='1', @HIDECHOICE='2', '') @RANDOMORDER @PLACEHOLDER='x'",
    ),
    field("showchoice", B, "dropdown", "Some choices", "1, A | 2, B | 3, C", annotation="@SHOWCHOICE='1,3'"),
    field(
        "wrong",
        B,
        "text",
        "<span style='color:red'>Incorrect.</span> Review the video.",
        annotation="@READONLY",
        branching="[smoke] = '0'",
    ),
    field("version", B, "text", "Consent version", annotation='@DEFAULT="13 Oct 2025" @READONLY'),
    field("pipe_text", B, "text", "Computed", annotation="@CALCTEXT(if([smoke]='1', 'or work', ''))\n@HIDDEN"),
    field(
        "groups",
        B,
        "text",
        "Only groups",
        branching="([smoke] = '1' or [sex] = '3') and ([drinks(1)] = '1' or [drinks(2)] = '1')",
    ),
    field("agree", B, "checkbox", "I agree to the terms.", "1,"),
    field("watch", B, "descriptive", "Watch the video below."),
    field("feel_total", B, "calc", "Feelings total", "[f1] + [f2]"),
    field("feel_mean", B, "calc", "Feelings mean", "round(mean([f1], [f2]), 1)"),
    field("grand", B, "calc", "Grand total", "sum([feel_total], [pain])"),
    field("bad_calc", B, "calc", "Uses a number", "[age] + [cigs]"),
    field("yes_count", B, "calc", "How many yes", "if([smoke] = '1', 1, 0) + if([later] >= 1, 1, 0)"),
    field("often_count", B, "calc", "Clashes with feel_total", "if([f1] >= 2, 1, 0) + if([f2] >= 2, 1, 0)"),
    field("fu1", F, "radio", "Still smoking?", "1, Yes | 0, No", branching="[smoke] = '1'"),
]


@pytest.fixture
def codebook(tmp_path) -> Path:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerows([HEADER, *ROWS])
    path = tmp_path / "Study (Pre).csv"
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


def _items(conversion, activity="Baseline") -> dict[str, dict]:
    return {row["item_name"]: row for row in conversion.activities[activity]}


def _notes(conversion, variable=None) -> list[str]:
    return [n.message for n in conversion.notes if variable is None or n.variable == variable]


def test_defaults(codebook):
    conversion = convert_redcap(codebook, Decisions())
    assert list(conversion.activities) == ["Baseline", "Followup"]
    items = _items(conversion)

    assert items["intro"]["item_type"] == "message"
    assert items["intro"]["item_prompt"] == "**Welcome**\nThanks for joining & helping."
    assert (items["age"]["item_type"], items["age"]["min_value"], items["age"]["max_value"]) == ("numberSelect", 10, 99)
    assert items["age"]["item_prompt"] == "Age?\n\n*Whole years*"
    assert items["age"]["skippable"] == "no"
    assert (items["weight"]["item_type"], items["weight"]["response_rules"]) == ("text", "numbers only")
    assert items["dob"]["item_type"] == "date" and items["visit_dt"]["item_type"] == "date"
    assert items["sex"]["item_prompt"] == "Sex (check all that apply)\n\nPlease answer as on your ID."
    assert items["sex"]["value_labels"] == "1=Male | 2=Female | 3=Other"
    assert items["smoke"]["value_labels"] == "1=Yes | 0=No"
    assert items["cigs"]["shown_if"] == 'smoke = "Yes" (1)'
    assert items["drinks"]["value_labels"] == "1=Beer | 2=Wine | 3=Spirits | 0=None (none of the above)"
    assert items["drinks"]["response_rules"] == "options in random order"
    assert items["drinks_other"]["shown_if"] == 'drinks includes "Spirits" (3) OR drinks includes "Wine" (2)'
    assert (items["mood"]["item_type"], items["mood"]["min_value"], items["mood"]["max_value"]) == (
        "numberSelect",
        0,
        100,
    )
    assert items["mood"]["item_prompt"] == "Mood? (0 = Bad, 100 = Good)"
    assert (items["pain"]["item_type"], items["pain"]["max_value"], items["pain"]["max_label"]) == (
        "slider",
        10,
        "Worst",
    )
    assert "total" not in items and "photo_id" not in items
    assert items["sig"]["item_type"] == "drawing"
    assert items["comments"]["item_type"] == "paragraphText"
    assert items["grade"]["value_labels"] == "0=Good | 1=Bad"
    assert items["mixed"]["shown_if"] == 'grade = "Good" (0)' and items["too_early"]["shown_if"] == ""
    assert items["pattern"]["shown_if"] == 'smoke = "Yes" (1) AND sex = "Female" (2)'
    assert items["either"]["shown_if"] == 'smoke = "Yes" (1) OR grade = "Bad" (1)'
    assert items["by_age"]["shown_if"] == 'smoke = "No" (0)'
    assert items["ifchoice"]["value_labels"] == "1=A | 2=B | 3=C"
    assert items["ifchoice"]["response_rules"] == "options in random order"
    assert items["showchoice"]["value_labels"] == "1=A | 2=B (hidden) | 3=C"
    assert (items["wrong"]["item_type"], items["wrong"]["item_prompt"]) == ("message", "Incorrect. Review the video.")
    assert items["wrong"]["shown_if"] == 'smoke = "No" (0)'
    assert items["version"]["item_prompt"] == "Consent version\n\n13 Oct 2025"
    assert "pipe_text" not in items
    assert items["groups"]["shown_if"] == 'smoke = "Yes" (1) OR sex = "Other" (3)'
    assert items["agree"]["value_labels"] == "1=Yes"

    # Calculated sums and averages become subscales; the items they use are scored with their codes.
    assert items["f1"]["scores"] == "1=1 | 2=2" and items["sex"]["scores"] == ""
    assert items["pain"]["scores"] == " | ".join(f"{v}={v}" for v in range(11))
    assert items["subscale_name_feel_total"]["item_type"] == "subscale_score"
    assert items["subscale_name_feel_total"]["item_prompt"] == "Subscale feel_total: Sum of f1, f2"
    assert items["subscale_name_feel_mean"]["item_prompt"] == "Subscale feel_mean: Average of f1, f2"
    assert items["subscale_name_grand"]["item_prompt"] == "Subscale grand: Sum of feel_total (subscale), pain"
    assert "subscale_name_bad_calc" not in items
    assert items["smoke"]["scores"] == "1=1 | 0=0" and items["later"]["scores"] == "1=1 | 0=0"
    assert items["subscale_name_yes_count"]["item_prompt"] == "Subscale yes_count: Sum of smoke, later"
    assert "subscale_name_often_count" not in items
    assert list(items)[-4:] == [
        "subscale_name_feel_total",
        "subscale_name_feel_mean",
        "subscale_name_grand",
        "subscale_name_yes_count",
    ]
    assert (
        items["f1_header"]["item_type"] == "message" and items["f1_header"]["item_prompt"] == "How often do you feel..."
    )
    assert items["f1"]["item_type"] == "singleSelect" and items["f2"]["item_prompt"] == "Tense"
    assert items["older"]["shown_if"] == "age > 17"
    assert items["often_calm"]["shown_if"] == 'f1 = "Often" (2)'
    assert "secret" not in items, "hidden fields are left out by default"
    assert items["thanks"]["item_prompt"] == "Thanks, [[sex]]! Ignore [unknown]."
    assert _items(conversion, "Followup")["fu1"]["shown_if"] == ""

    order = [row["item_name"] for row in conversion.activities["Baseline"]]
    assert order.index("f1_header") == order.index("f1") - 1

    assert conversion.decisions == {
        "orphan_rows": "append",
        "option_scores": "none",
        "activity_name:baseline": "Baseline",
        "field_notes": "append",
        "integer_range": "numberSelect",
        "datetime": "date",
        "slider": "numberSelect",
        "file": "skip",
        "signature": "drawing",
        "notes": "paragraphText",
        "non_integer_codes": "renumber",
        "branching": "approximate",
        "hidden_fields": "skip",
        "readonly": "message",
        "forms": ["baseline", "followup"],
        "matrix": "separate",
        "section_headers": "message",
        "calc_subscales": "subscale",
        "activity_name:followup": "Followup",
    }
    messages = " ".join(_notes(conversion))
    for expected in (
        "column 1 is not a REDCap column; ignored. Values: 'DEMO'",
        "line 9 (no variable name) added to the label",
        "Curious can't check the range 0 to 300",
        "marked as an identifier",
        "single-choice in REDCap",
        "Curious can only add up, average, or count answers, so it was left out (formula: [age]+1)",
        (
            "became the subscale yes_count (sum of smoke, later); each item scores 1 when the answer is = 1, else 0; "
            "1 when the answer is >= 1, else 0"
        ),
        "it scores f1 differently than the subscale feel_total",
        "calculated with @CALCTEXT/@CALCDATE in REDCap",
        "became the subscale feel_total (sum of f1, f2)",
        "REDCap leaves the result empty when an item is unanswered",
        "REDCap rounds the result",
        "can't become a subscale: it uses age, a numberSelect item",
        "@IF annotations",
        "@PLACEHOLDER only affects how REDCap shows the form",
        'skip logic simplified to \'smoke = "Yes" (1) AND sex = "Female" (2)\'',
        "uses the REDCap function datediff()",
        "codes renumbered: A -> 0, B -> 1",
        "mixes 'and' with 'or'",
        "not an earlier converted field",
        "middle label",
        "hidden in REDCap; left out",
        "read-only in REDCap; shown as a message",
        "mentions a video",
    ):
        assert expected in messages, expected
    assert not any("will be refused" in m for m in _notes(conversion))
    for name, rows in conversion.activities.items():
        assert validate_rows(name, rows) == []


def test_other_decisions(codebook):
    preset = {
        "orphan_rows": "ignore",
        "matrix": "grid",
        "section_headers": "prepend",
        "slider": "slider",
        "branching": "skip",
        "non_integer_codes": "renumber_from_1",
        "option_scores": "codes",
        "field_notes": "drop",
        "integer_range": "text",
        "file": "photo",
        "notes": "text",
        "activity_name:baseline": "Baseline Survey",
    }
    conversion = convert_redcap(codebook, Decisions(preset))
    items = _items(conversion, "Baseline Survey")
    assert items["sex"]["item_prompt"] == "Sex (check all that apply)"
    assert items["age"]["item_type"] == "text" and items["age"]["item_prompt"] == "Age?"
    assert (items["mood"]["item_type"], items["mood"]["max_value"], items["mood"]["min_label"]) == ("slider", 10, "Bad")
    assert items["photo_id"]["item_type"] == "photo" and items["comments"]["item_type"] == "text"
    assert items["grade"]["value_labels"] == "1=Good | 2=Bad"
    assert items["sex"]["scores"] == "1=1 | 2=2 | 3=3"
    assert "mixed" not in items and "too_early" not in items
    grid = items["feel"]
    assert (grid["item_type"], grid["rows"], grid["value_labels"]) == (
        "singleSelectRows",
        "Calm | Tense",
        "Never | Often",
    )
    assert grid["item_prompt"] == "How often do you feel..."
    assert items["often_calm"]["shown_if"] == 'feel [Calm] = "Often"'
    assert conversion.decisions["activity_name:baseline"] == "Baseline Survey"
    for name, rows in conversion.activities.items():
        assert validate_rows(name, rows) == []


def test_asks_only_what_the_file_needs(codebook):
    asked = []

    def ask(question, options, context):
        asked.append(question)
        return "each" if any(value == "each" for value, _ in options) else options[-1][0]

    conversion = convert_redcap(codebook, Decisions(ask=ask, ask_text=lambda q, default: default.upper()))
    assert any(q.startswith("mixed:") for q in asked) and any(q.startswith("too_early:") for q in asked)
    assert any(q.startswith("Matrix feel") for q in asked)
    assert conversion.decisions["branching:mixed"] == "skip"
    assert conversion.decisions["matrix:feel"] == "grid"
    assert "BASELINE" in conversion.activities
    assert "section_headers" not in conversion.decisions or conversion.decisions["section_headers"]


@pytest.mark.parametrize(
    ("logic", "expected"),
    [
        ("[a] = '1'", Compare("a", None, "=", "1")),
        ('([a] <> 2) OR ([b(3)] = "1")', Logic("or", [Compare("a", None, "<>", "2"), Compare("b", "3", "=", "1")])),
        ("'1' = [a]", Compare("a", None, "=", "1")),
        ("[a] >= 5 AND [b] != ''", Logic("and", [Compare("a", None, ">=", "5"), Compare("b", None, "<>", "")])),
        (
            "[a] = 1 and [b] = 1 or [c] = 1",
            Logic("or", [Logic("and", [Compare("a", None, "=", "1"), Compare("b", None, "=", "1")]), Compare("c", None, "=", "1")]),
        ),
        (
            "([x(1)] = '1' or [x(2)]='1') and ([y] = 1 and [z] = 2)",
            Logic("and", [Logic("or", [Compare("x", "1", "=", "1"), Compare("x", "2", "=", "1")]), Compare("y", None, "=", "1"), Compare("z", None, "=", "2")]),
        ),
    ],
)  # fmt: skip
def test_parse_logic(logic, expected):
    assert parse_logic(logic) == expected


@pytest.mark.parametrize(
    ("logic", "reason"),
    [
        ("datediff([d], 'today', 'y') > 18", "datediff()"),
        ("(if([a]='1', 1, 0) + if([b]='1', 1, 0)) >= 2", "if()"),
        ("[event_1_arm_1][a] = '1'", "another event"),
        ("[a] = [b]", "two fields"),
        ("[a] + [b] > 3", "arithmetic"),
        ("[event-name] = 'x'", "smart variable"),
        ("([a] = 1", "unbalanced"),
    ],
)
def test_logic_curious_cant_express(logic, reason):
    node = parse_logic(logic)
    assert isinstance(node, Opaque) and reason in node.reason


def test_annotation_tags_set_conditional_blocks_aside():
    tags, blocks = annotation_tags(
        "@IF([a]='1', @HIDECHOICE='2', @SHOWCHOICE='1') @HIDDEN @CALCTEXT(if([b]>1,'x','y')) @CHARLIMIT=5"
    )
    assert tags == [("HIDDEN", ""), ("CHARLIMIT", "5")] and blocks == ["IF", "CALCTEXT"]


def test_only_chosen_forms(codebook):
    conversion = convert_redcap(codebook, Decisions({"forms": ["followup"]}))
    assert list(conversion.activities) == ["Followup"]
    assert "form 'baseline' not converted" in " ".join(_notes(conversion))


def test_html_to_markdown():
    table = "<table><tbody><tr><td><strong>Visit</strong></td><td>Time</td></tr><tr><td>1</td><td>2 h</td></tr></tbody></table>"
    assert html_to_markdown(table) == "| Visit | Time |\n| --- | --- |\n| 1 | 2 h |"
    assert (
        html_to_markdown("<h3>Risks</h3><p>Some <strong>risk </strong>here.</p>") == "### Risks\n\nSome **risk** here."
    )
    assert html_to_markdown("<ol><li>One</li><li>Two</li></ol>") == "1. One\n2. Two"
    assert html_to_markdown("<p>a\u202fb&nbsp;c</p>") == "a b c"
    assert html_to_markdown('<p>One</p><p>Two <i>it</i> <a href="https://x.org">link</a></p>') == (
        "One\n\nTwo *it* [link](https://x.org)"
    )
    assert html_to_markdown("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"
    assert html_to_markdown("plain  text ") == "plain  text"


def test_not_a_redcap_file(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("a,b\n1,2\n")
    from curious_download.redcap import RedcapError

    with pytest.raises(RedcapError, match="Variable / Field Name"):
        convert_redcap(path, Decisions())


def test_cli_convert_then_upload(codebook, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)
    out = tmp_path / "out"

    result = CliRunner().invoke(app, ["--convert-redcap", str(codebook), "--no-input", "--output", str(out)])
    assert result.exit_code == 0, result.output
    folder = next(out.glob("redcap_Study_Pre_*"))
    files = sorted(p.name for p in folder.iterdir())
    assert files == [
        "Study_Pre__Baseline__data_dictionary.csv",
        "Study_Pre__Followup__data_dictionary.csv",
        "conversion_report.csv",
        "export_info.json",
        "redcap_decisions.json",
    ]
    decisions = json.loads((folder / "redcap_decisions.json").read_text())

    # The saved decisions reproduce the same files.
    again = CliRunner().invoke(
        app,
        ["--convert-redcap", str(codebook), "--decisions", str(folder / "redcap_decisions.json"), "--no-input", "--output", str(tmp_path / "again")],
    )  # fmt: skip
    assert again.exit_code == 0, again.output
    second = next((tmp_path / "again").glob("redcap_*"))
    for name in ("Study_Pre__Baseline__data_dictionary.csv", "Study_Pre__Followup__data_dictionary.csv"):
        assert (second / name).read_bytes() == (folder / name).read_bytes()
    assert json.loads((second / "redcap_decisions.json").read_text()) == decisions

    server = FakeBuilderServer()
    monkeypatch.setattr(cli, "CuriousClient", lambda url: CuriousClient(url, transport=server.transport()))
    base = ["--server", "https://api.curious.test", "--email", "r@example.org", "--no-input", "--output", str(out)]
    result = CliRunner().invoke(
        app, [*base, "--upload", str(folder), "--applet", "EMA Study", "--yes"], env={"CURIOUS_PASSWORD": "pw"}
    )
    assert result.exit_code == 0, result.output
    activities = server.puts[0]["activities"]
    assert [a["name"] for a in activities] == ["Daily", "Weekly", "Baseline", "Followup"]
    baseline = {i["name"]: i for i in activities[2]["items"]}
    assert baseline["cigs"]["conditionalLogic"] == {
        "match": "all",
        "conditions": [{"itemName": "smoke", "type": "EQUAL_TO_OPTION", "payload": {"optionValue": "1"}}],
    }
    assert baseline["drinks"]["config"]["randomizeOptions"] is True
    assert baseline["age"]["responseValues"] == {"minValue": 10, "maxValue": 99}
    written = list(
        csv.DictReader(
            io.StringIO((folder / "Study_Pre__Baseline__data_dictionary.csv").read_text(encoding="utf-8-sig"))
        )
    )
    assert len(baseline) == sum(1 for row in written if not row["item_type"].endswith("_score"))
    setting = activities[2]["subscaleSetting"]
    assert setting["calculateTotalScore"] is None
    assert [(s["name"], s["scoring"]) for s in setting["subscales"]] == [
        ("feel_total", "sum"),
        ("feel_mean", "average"),
        ("grand", "sum"),
        ("yes_count", "sum"),
    ]
    assert setting["subscales"][2]["items"] == [
        {"name": "feel_total", "type": "subscale"},
        {"name": "pain", "type": "item"},
    ]
    assert baseline["f1"]["config"]["addScores"] is True
    assert [o["score"] for o in baseline["f1"]["responseValues"]["options"]] == [1, 2]
    assert baseline["pain"]["responseValues"]["scores"] == list(range(11))


def test_cli_rejects_a_non_redcap_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SETTINGS_PATH", tmp_path / "settings.json")
    app = typer.Typer()
    app.command()(cli.download)
    path = tmp_path / "x.csv"
    path.write_text("a,b\n")
    result = CliRunner().invoke(app, ["--convert-redcap", str(path), "--no-input"])
    assert result.exit_code == 2 and "doesn't look like a REDCap" in result.output
    result = CliRunner().invoke(app, ["--decisions", str(path), "--no-input"])
    assert result.exit_code == 2 and "--decisions only applies" in result.output


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("[a] + [b]", ("sum", [("a", None), ("b", None)], False, "plus")),
        ("sum([a], [b])", ("sum", [("a", None), ("b", None)], False, "function")),
        ("rounddown(mean([a],[b]), 1)", ("average", [("a", None), ("b", None)], True, "function")),
        ("([a] + [b] + [c]) / 3", ("average", [("a", None), ("b", None), ("c", None)], False, "plus")),
        ("if([a] >= 2, 1, 0) +\nif([b] <= '3', 1, 0)", ("sum", [("a", (">=", 2.0, 1, 0)), ("b", ("<=", 3.0, 1, 0))], False, "count")),
        ("([a] + [b]) / 3", None),
        ("rounddown(datediff([dob], 'today', 'y'))", None),
        ("if([a] >= 2 or [b] >= 2, 1, 0)", None),
        ("if([score] >= 4, 1, 0)", None),
        ("", None),
    ],
)  # fmt: skip
def test_parse_calc(formula, expected):
    from curious_download.redcap import parse_calc

    assert parse_calc(formula) == expected
