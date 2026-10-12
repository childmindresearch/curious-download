"""Convert a REDCap data dictionary into Curious data dictionary CSVs (one per REDCap form).

Where the two platforms differ, a `Decisions` object supplies the answer: from a saved decisions
file, from the user (the CLI asks), or the recommended default. Every decision and every
mismatch is recorded in the conversion report, so the result can be checked before uploading.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .csvout import file_slug, write_csv
from .dictionary import DICTIONARY_COLUMNS, subscale_cells
from .itembuilder import (
    SCORE_ROW_TYPES,
    ItemRow,
    apply_scores,
    build_item,
    check_activity,
    plan_scores,
    resolve_logic,
    response_options_cell,
)
from .itembuilder import CellError as _CellError

REPORT_COLUMNS = ["line", "variable", "form", "kind", "message"]


class RedcapError(Exception):
    """The file is not a REDCap data dictionary."""


# -- decisions ------------------------------------------------------------------------------------

Choice = tuple[str, str]  # (value, label); the first one is the recommended default
AskChoice = Callable[[str, list[Choice], str], str]
AskText = Callable[[str, str], str]
AskMany = Callable[[str, list[Choice]], list[str]]


class Decisions:
    """Answers to the questions a conversion raises, from a saved file, the user, or the defaults."""

    def __init__(
        self,
        preset: dict | None = None,
        ask: AskChoice | None = None,
        ask_text: AskText | None = None,
        ask_many: AskMany | None = None,
    ):
        self.preset = dict(preset or {})
        self.ask = ask
        self.ask_text = ask_text
        self.ask_many = ask_many
        self.used: dict[str, Any] = {}

    def choose_many(self, key: str, question: str, options: list[Choice]) -> list[str]:
        """Several of the options; all of them by default."""
        if key in self.used:
            return self.used[key]
        allowed = [value for value, _ in options]
        value = self.preset.get(key)
        if isinstance(value, list) and any(v in allowed for v in value):
            value = [v for v in allowed if v in value]
        elif self.ask_many:
            value = self.ask_many(question, options)
        else:
            value = allowed
        self.used[key] = value
        return value

    def choose(self, key: str, question: str, options: list[Choice], context: str = "") -> str:
        if key in self.used:
            return self.used[key]
        allowed = [value for value, _ in options]
        value = self.preset.get(key)
        if value not in allowed:
            value = self.ask(question, options, context) if self.ask else allowed[0]
        self.used[key] = value
        return value

    def text(self, key: str, question: str, default: str) -> str:
        if key in self.used:
            return self.used[key]
        value = self.preset.get(key)
        if not isinstance(value, str) or not value.strip():
            value = (self.ask_text(question, default) if self.ask_text else default).strip() or default
        self.used[key] = value
        return value


@dataclass
class Note:
    line: int | str
    variable: str
    form: str
    kind: str  # decision, warning, info
    message: str


# -- reading ----------------------------------------------------------------------------------------

_COLUMNS = [
    ("variable", "name"),
    ("formname", "form"),
    ("sectionheader", "section"),
    ("fieldtype", "type"),
    ("fieldlabel", "label"),
    ("choices", "choices"),
    ("fieldnote", "note"),
    ("textvalidationtype", "validation"),
    ("textvalidationmin", "vmin"),
    ("textvalidationmax", "vmax"),
    ("identifier", "identifier"),
    ("branching", "branching"),
    ("required", "required"),
    ("customalignment", "alignment"),
    ("questionnumber", "question_number"),
    ("matrixgroup", "matrix"),
    ("matrixranking", "ranking"),
    ("fieldannotation", "annotation"),
]


@dataclass
class Field:
    line: int
    name: str
    form: str
    type: str
    label: str = ""
    section: str = ""
    choices: str = ""
    note: str = ""
    validation: str = ""
    vmin: str = ""
    vmax: str = ""
    identifier: str = ""
    branching: str = ""
    required: str = ""
    alignment: str = ""
    question_number: str = ""
    matrix: str = ""
    ranking: str = ""
    annotation: str = ""


def _key_for(header: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", header.lower())
    return next((key for prefix, key in _COLUMNS if normalized.startswith(prefix)), None)


def _read_rows(path: Path) -> tuple[list[tuple[int, dict]], dict[str, list[tuple[int, str]]]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RedcapError(f"{path.name} is empty.")
    header = rows[0]
    keys = [_key_for(h) for h in header]
    for needed in ("name", "form", "type", "label"):
        if needed not in keys:
            raise RedcapError(
                f"{path.name} doesn't look like a REDCap data dictionary: no {needed!r} column "
                "(expected headers like 'Variable / Field Name', 'Form Name', 'Field Type', 'Field Label')."
            )
    unknown: dict[str, list[tuple[int, str]]] = {}
    parsed = []
    for line, row in enumerate(rows[1:], start=2):
        values: dict[str, str] = {}
        for index, cell in enumerate(row):
            key = keys[index] if index < len(keys) else None
            if key is None:
                if cell.strip():
                    label = (
                        header[index].strip()
                        if index < len(header) and header[index].strip()
                        else f"column {index + 1}"
                    )
                    unknown.setdefault(label, []).append((line, cell.strip()))
            elif key not in values:
                values[key] = cell
        parsed.append((line, values))
    return parsed, unknown


# -- text cleaning ----------------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


_FLAGS = re.IGNORECASE | re.DOTALL


def _table_to_markdown(match: re.Match) -> str:
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match[0], _FLAGS):
        cells = [plain_text(cell) for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, _FLAGS)]
        if any(cells):
            rows.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    if not rows:
        return "\n"
    width = rows[0].count(" | ") + 1
    return "\n\n" + "\n".join([rows[0], "|" + " --- |" * width, *rows[1:]]) + "\n\n"


def _ordered_list(match: re.Match) -> str:
    items = re.findall(r"<li[^>]*>(.*?)</li>", match[0], _FLAGS)
    return "\n" + "".join(f"\n{n}. {item}" for n, item in enumerate(items, start=1)) + "\n"


def html_to_markdown(text: str) -> str:
    """REDCap rich text to the Markdown Curious prompts use: emphasis, headings, lists, tables, links, images."""
    text = re.sub(r"\r\n?", "\n", text)
    if "<" not in text and "&" not in text:
        return text.strip()
    text = re.sub(r"<table.*?</table>", _table_to_markdown, text, flags=_FLAGS)
    text = re.sub(r"<ol[^>]*>.*?</ol>", _ordered_list, text, flags=_FLAGS)
    text = re.sub(
        r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: f"\n\n{'#' * int(m[1])} {plain_text(m[2])}\n\n", text, flags=_FLAGS
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<hr[^>]*>", "\n\n---\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div)>\s*", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<(b|strong)(?:\s[^>]*)?>(.*?)</\1>", lambda m: _emphasis("**", m[2]), text, flags=_FLAGS)
    text = re.sub(r"<(i|em)(?:\s[^>]*)?>(.*?)</\1>", lambda m: _emphasis("*", m[2]), text, flags=_FLAGS)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=_FLAGS)
    text = re.sub(r'<a [^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=_FLAGS)
    text = re.sub(r'<img [^>]*src="([^"]*)"[^>]*>', r"![](\1)", text, flags=re.IGNORECASE)
    text = html.unescape(_TAG.sub("", text))
    text = re.sub(r"[\xa0  ]", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+(?=[-\d|#])", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _emphasis(mark: str, inner: str) -> str:
    """Markdown emphasis must hug the text: '** bold **' doesn't render."""
    stripped = inner.strip()
    if not stripped:
        return inner
    lead, trail = inner[: len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()) :]
    return f"{lead}{mark}{stripped}{mark}{trail}"


def plain_text(text: str) -> str:
    text = html.unescape(_TAG.sub(" ", text))
    return re.sub(r"\s+", " ", re.sub(r"[\xa0  ]", " ", text)).strip()


def pretty_form_name(form: str) -> str:
    return " ".join(word.capitalize() for word in re.split(r"[_\s]+", form) if word) or form


# -- branching logic --------------------------------------------------------------------------------


class Unsupported(Exception):
    """Branching logic Curious skip logic can't express."""


_TOKEN = re.compile(
    r"""\s*(?:
        (?P<ref>(?:\[[^\[\]]+\]){1,2})
      | (?P<str>'[^']*'|"[^"]*")
      | (?P<num>-?\d+(?:\.\d+)?)
      | (?P<op><>|!=|>=|<=|==|=|>|<)
      | (?P<join>and|or)(?![\w\[])
      | (?P<lp>\()
      | (?P<rp>\))
      | (?P<other>[^\s()\[\]'"<>=!]+)
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_FLIP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "=": "=", "<>": "<>"}


@dataclass
class Compare:
    """[ref] op value, or [ref(code)] op value for a checkbox option."""

    ref: str
    code: str | None
    op: str
    value: str


@dataclass
class Opaque:
    """A piece of logic Curious can't express at all (a function, arithmetic, two fields, …)."""

    text: str
    reason: str


@dataclass
class Logic:
    joiner: str  # "and" or "or"
    parts: list[Any]  # Compare, Opaque, or Logic with the other joiner


def _group(joiner: str, parts: list[Any]) -> Any:
    flat: list[Any] = []
    for part in parts:
        flat += part.parts if isinstance(part, Logic) and part.joiner == joiner else [part]
    return flat[0] if len(flat) == 1 else Logic(joiner, flat)


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens, position = [], 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match or match.end() == position:
            if text[position:].strip():
                tokens.append(("other", text[position:].strip()))
            break
        position = match.end()
        tokens.append((match.lastgroup, match[match.lastgroup]))
    return tokens


def _source(tokens: list[tuple[str, str]]) -> str:
    return " ".join(value for _, value in tokens)


def _term(tokens: list[tuple[str, str]]) -> Any:
    text = _source(tokens)
    if not tokens:
        return Opaque(text, "is incomplete")
    if tokens[0][0] == "lp" and tokens[-1][0] == "rp":
        depth = 0
        for index, (kind, _) in enumerate(tokens):
            depth += 1 if kind == "lp" else -1 if kind == "rp" else 0
            if depth == 0:
                break
        if index == len(tokens) - 1:  # the parentheses wrap the whole term
            return _Parser(tokens[1:-1]).parse()
    if len(tokens) == 3 and tokens[1][0] == "op":
        (left_kind, left), (_, op), (right_kind, right) = tokens
        op = {"==": "=", "!=": "<>"}.get(op, op)
        if left_kind != "ref" and right_kind == "ref":
            (left_kind, left), (right_kind, right), op = (right_kind, right), (left_kind, left), _FLIP[op]
        if left_kind == "ref" and right_kind in ("str", "num"):
            refs = re.findall(r"\[([^\]]+)\]", left)
            if len(refs) > 1:
                return Opaque(text, f"refers to {left}, a field in another event or instance")
            match = re.fullmatch(r"([A-Za-z0-9_]+)(?:\((\w+)\))?(?::[\w-]+)?", refs[0].strip())
            if not match:
                return Opaque(text, f"uses {left}, a REDCap smart variable")
            return Compare(match[1], match[2], op, right[1:-1] if right_kind == "str" else right)
        if left_kind == "ref" and right_kind == "ref":
            return Opaque(text, "compares two fields with each other")
    function = next((v for i, (k, v) in enumerate(tokens[:-1]) if k == "other" and tokens[i + 1][0] == "lp"), None)
    if function:
        return Opaque(text, f"uses the REDCap function {function}()")
    if any(k == "other" and re.fullmatch(r"[-+*/^]+", v) for k, v in tokens):
        return Opaque(text, "does arithmetic")
    return Opaque(text, "is not a simple comparison")


class _Parser:
    """REDCap logic as a tree; 'and' binds tighter than 'or', as in REDCap."""

    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.index = 0

    def parse(self) -> Any:
        node = self._or()
        if self.index < len(self.tokens):
            return Opaque(_source(self.tokens), "has unbalanced parentheses")
        return node

    def _is_join(self, word: str) -> bool:
        if self.index >= len(self.tokens):
            return False
        kind, value = self.tokens[self.index]
        return kind == "join" and value.lower() == word

    def _or(self) -> Any:
        parts = [self._and()]
        while self._is_join("or"):
            self.index += 1
            parts.append(self._and())
        return _group("or", parts)

    def _and(self) -> Any:
        parts = [self._collect()]
        while self._is_join("and"):
            self.index += 1
            parts.append(self._collect())
        return _group("and", parts)

    def _collect(self) -> Any:
        start, depth = self.index, 0
        while self.index < len(self.tokens):
            kind, _ = self.tokens[self.index]
            if kind == "lp":
                depth += 1
            elif kind == "rp":
                if depth == 0:
                    break
                depth -= 1
            elif kind == "join" and depth == 0:
                break
            self.index += 1
        return _term(self.tokens[start : self.index])


def parse_logic(text: str) -> Any:
    """REDCap branching logic -> Compare, Opaque, or Logic."""
    tokens = _tokens(text.strip())
    if not tokens:
        return Opaque(text, "is empty")
    if sum(k == "lp" for k, _ in tokens) != sum(k == "rp" for k, _ in tokens):
        return Opaque(text, "has unbalanced parentheses")
    return _Parser(tokens).parse()


@dataclass
class _Ok:
    text: str  # a Curious shown_if condition


@dataclass
class _Bad:
    reason: str


def _exact(tree: Any) -> tuple[str, list[str]] | None:
    if isinstance(tree, _Ok):
        return "and", [tree.text]
    if isinstance(tree, Logic) and all(isinstance(p, _Ok) for p in tree.parts):
        return tree.joiner, [p.text for p in tree.parts]
    return None


def _approximate(tree: Any) -> tuple[str, list[str]] | None:
    """Conditions Curious can express that hold whenever `tree` does (so the field shows at least as often)."""
    if not isinstance(tree, Logic):
        return None
    if tree.joiner == "and":
        # Dropping some of the conditions only makes it true more often.
        kept = [p.text for p in tree.parts if isinstance(p, _Ok)]
        if kept:
            return "and", kept
        # Only groups: the whole is true only when each group is, so any one group will do.
        return next((found for p in tree.parts if isinstance(p, Logic) and (found := _approximate(p))), None)
    texts: list[str] = []  # 'or': each alternative must be replaced by something it implies
    for part in tree.parts:
        if isinstance(part, _Ok):
            texts.append(part.text)
            continue
        found = _approximate(part) if isinstance(part, Logic) else None
        if found is None:
            return None
        texts += found[1][:1] if found[0] == "and" else found[1]
    return "or", list(dict.fromkeys(texts))


def _reasons(tree: Any) -> list[str]:
    if isinstance(tree, _Bad):
        return [tree.reason]
    if isinstance(tree, Logic):
        found = [r for p in tree.parts for r in _reasons(p)]
        nested = any(isinstance(p, Logic) for p in tree.parts)
        return found + (["mixes 'and' with 'or', but Curious applies one of them to all conditions"] if nested else [])
    return []


# -- conversion ---------------------------------------------------------------------------------------


@dataclass
class _Item:
    name: str
    type: str
    prompt: str
    line: int | str = ""
    options: list[tuple[int, str, set[str]]] = field(default_factory=list)
    scores: dict[int, Any] = field(default_factory=dict)
    min_value: int | None = None
    max_value: int | None = None
    min_label: str = ""
    max_label: str = ""
    rows: list[str] = field(default_factory=list)  # grid rows
    grid_options: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    skippable: str = ""
    hidden: bool = False
    shown_if: str = ""
    source: Field | None = None
    code_map: dict[str, int] = field(default_factory=dict)  # REDCap code -> Curious value
    grid_codes: dict[str, str] = field(default_factory=dict)  # grids: REDCap code -> option label
    members: list[Field] = field(default_factory=list)  # grids: one field per row


@dataclass
class Conversion:
    activities: dict[str, list[dict]]  # activity name -> dictionary rows
    forms: dict[str, str]  # activity name -> REDCap form
    notes: list[Note]
    decisions: dict[str, str]
    counts: Counter


_NUMERIC_VALIDATION = re.compile(r"^(integer|number.*)$")
_DATE_VALIDATION = ("date_ymd", "date_mdy", "date_dmy")
_ANNOTATION = re.compile(r"@([A-Z][A-Z0-9-]*)(?:\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s@]+)))?")
_BLOCK_TAGS = re.compile(r"@(IF|CALCTEXT|CALCDATE)\s*\(")
# Tags that only change how REDCap shows the form, so they don't matter for Curious.
_LAYOUT_TAGS = {
    "HIDDEN-FORM", "HIDDEN-APP", "HIDDEN-PDF", "READONLY-FORM", "READONLY-APP", "HIDEBUTTON",
    "PLACEHOLDER", "NOMISSING", "INLINE", "RICHTEXT", "MAXCHECKED", "MAXCHOICE-SURVEY-COMPLETE",
}  # fmt: skip
# Tags that make REDCap fill in a value, which Curious doesn't do.
_VALUE_TAGS = {"DEFAULT", "SETVALUE", "NOW", "TODAY", "USERNAME", "LATITUDE", "LONGITUDE", "NOW-SERVER", "TODAY-SERVER"}


def annotation_tags(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """The action tags of a Field Annotation, and the names of conditional or computed blocks
    (@IF(…), @CALCTEXT(…)) whose content was set aside, since it can't apply unconditionally."""
    blocks: list[str] = []
    while match := _BLOCK_TAGS.search(text):
        depth, quote, end = 0, "", len(text)
        for index in range(match.end() - 1, len(text)):
            char = text[index]
            if quote:
                quote = "" if char == quote else quote
            elif char in "'\"":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        blocks.append(match[1])
        text = text[: match.start()] + " " + text[end:]
    tags = [(m[1], next((g for g in m.groups()[1:] if g is not None), "")) for m in _ANNOTATION.finditer(text)]
    return tags, blocks


class _Converter:
    def __init__(self, decisions: Decisions):
        self.d = decisions
        self.notes: list[Note] = []
        self.counts: Counter = Counter()

    def note(self, f: Field | None, kind: str, message: str, line: int | str = "") -> None:
        self.notes.append(Note(f.line if f else line, f.name if f else "", f.form if f else "", kind, message))

    # --- one field ---------------------------------------------------------------------------------

    def choices(self, f: Field) -> tuple[list[tuple[int, str, set[str]]], dict[str, int]] | None:
        if f.type == "yesno":
            pairs = [("1", "Yes"), ("0", "No")]
        elif f.type == "truefalse":
            pairs = [("1", "True"), ("0", "False")]
        else:
            pairs = []
            for part in f.choices.split("|"):
                if not part.strip():
                    continue
                code, sep, label = part.partition(",")
                pairs.append((code.strip(), plain_text(label if sep else code)))
        if not pairs:
            self.note(f, "warning", "has no choices, so it was left out")
            return None
        if any(not label for _, label in pairs):
            # REDCap shows a bare box; Curious options need text.
            blank = "Yes" if len(pairs) == 1 else None
            pairs = [(code, label or blank or code) for code, label in pairs]
            self.note(
                f,
                "warning",
                "a choice has no label in REDCap; it was labelled " + (f"{blank!r}" if blank else "with its code"),
            )
        seen: dict[str, str] = {}
        unique = []
        for code, label in pairs:
            if code in seen:
                self.note(f, "warning", f"choice code {code!r} appears twice; only the first ({seen[code]!r}) was kept")
                continue
            seen[code] = label
            unique.append((code, label))
        if all(re.fullmatch(r"-?\d+", code) for code, _ in unique):
            code_map = {code: int(code) for code, _ in unique}
        else:
            choice = self.d.choose(
                "non_integer_codes",
                "Some choice codes aren't whole numbers, which Curious option values must be. What should happen?",
                [
                    ("renumber", "Number the options 0, 1, 2, … (the report lists the new codes)"),
                    ("renumber_from_1", "Number the options 1, 2, 3, …"),
                    ("skip", "Leave those fields out"),
                ],
                f"For example {f.name}: {', '.join(code for code, _ in unique)}",
            )
            if choice == "skip":
                self.note(f, "decision", "choice codes aren't whole numbers; left out")
                return None
            start = 1 if choice == "renumber_from_1" else 0
            code_map = {code: start + i for i, (code, _) in enumerate(unique)}
            self.note(f, "decision", "codes renumbered: " + ", ".join(f"{c} -> {v}" for c, v in code_map.items()))
        return [(code_map[code], label, set()) for code, label in unique], code_map

    def apply_annotations(self, f: Field, item: _Item, tags: list[tuple[str, str]], blocks: list[str]) -> None:
        if "IF" in blocks:
            self.note(
                f,
                "warning",
                "@IF annotations (choices or settings that depend on other answers) can't be converted; ignored, so all choices are shown",
            )
        for tag, value in tags:
            if tag in ("HIDDEN", "HIDDEN-SURVEY", "READONLY", "READONLY-SURVEY"):
                continue  # handled in field_item
            if tag in _LAYOUT_TAGS:
                self.note(f, "info", f"@{tag} only affects how REDCap shows the form; ignored")
            elif tag in _VALUE_TAGS:
                self.note(
                    f,
                    "warning",
                    f"@{tag}{'=' + value if value else ''} fills in a value in REDCap; Curious leaves it to the participant",
                )
            elif tag == "SHOWCHOICE" and item.options:
                codes = {c.strip() for c in value.split(",") if c.strip()}
                shown = {item.code_map[c] for c in codes if c in item.code_map}
                for value_, _, flags in item.options:
                    if value_ not in shown:
                        flags.add("hidden")
            elif tag in ("NONEOFTHEABOVE", "HIDECHOICE") and item.options:
                codes = {c.strip() for c in value.split(",") if c.strip()}
                values = {item.code_map[c] for c in codes if c in item.code_map}
                flag = "none of the above" if tag == "NONEOFTHEABOVE" else "hidden"
                if flag == "none of the above" and item.type != "multiSelect":
                    self.note(f, "warning", "@NONEOFTHEABOVE only applies to checkboxes; ignored")
                    continue
                if flag == "none of the above" and len(values) > 1:
                    self.note(f, "warning", "Curious allows one 'none of the above' option; only the first was marked")
                    values = {min(values, key=[v for v, _, _ in item.options].index)}
                for value_, _, flags in item.options:
                    if value_ in values:
                        flags.add(flag)
            elif tag == "RANDOMORDER" and item.type in ("singleSelect", "multiSelect"):
                item.rules.append("options in random order")
            elif tag == "CHARLIMIT" and item.type in ("text", "paragraphText") and value.isdigit():
                item.rules = [r for r in item.rules if not r.startswith("max ")] + [f"max {value} characters"]
            else:
                self.note(f, "warning", f"@{tag} has no Curious equivalent; ignored")

    def text_item(self, f: Field, item: _Item) -> _Item | None:
        validation = f.validation.strip().lower()
        if not validation:
            return item
        if _NUMERIC_VALIDATION.match(validation):
            low, high = f.vmin.strip(), f.vmax.strip()
            whole = re.fullmatch(r"\d+", low or "x") and re.fullmatch(r"\d+", high or "x")
            if validation == "integer" and whole and int(low) < int(high) and int(high) - int(low) <= 100:
                choice = self.d.choose(
                    "integer_range",
                    "Some whole-number fields have a range of 100 or fewer values. How should they look in Curious?",
                    [("numberSelect", "A number picker limited to the range"), ("text", "A text box for numbers")],
                    f"For example {f.name}: {low} to {high}",
                )
                if choice == "numberSelect":
                    item.type, item.min_value, item.max_value = "numberSelect", int(low), int(high)
                    return item
            item.rules.append("numbers only")
            if low or high:
                self.note(
                    f, "warning", f"Curious can't check the range {low or '…'} to {high or '…'}; it only allows numbers"
                )
            return item
        if validation in _DATE_VALIDATION:
            item.type = "date"
            if f.vmin.strip() or f.vmax.strip():
                self.note(f, "warning", "Curious can't limit the date range")
            return item
        if validation.startswith("datetime"):
            choice = self.d.choose(
                "datetime",
                "Curious has no date-and-time item. How should date-time fields be converted?",
                [("date", "A date item (the time is not asked)"), ("text", "A text box")],
                f"For example {f.name}",
            )
            item.type = "date" if choice == "date" else "text"
            self.note(f, "decision", "date-time field converted to " + ("a date item" if choice == "date" else "text"))
            return item
        if validation in ("time", "time_hh_mm_ss"):
            item.type = "time"
            if validation == "time_hh_mm_ss":
                self.note(f, "warning", "Curious times have no seconds")
            return item
        if validation == "time_mm_ss":
            self.note(f, "warning", "a minutes:seconds duration; kept as a text box")
            return item
        self.note(f, "warning", f"Curious can't check that answers are valid ({validation}); kept as a text box")
        return item

    def slider_item(self, f: Field, item: _Item) -> _Item:
        low = int(f.vmin) if re.fullmatch(r"-?\d+", f.vmin.strip()) else 0
        high = int(f.vmax) if re.fullmatch(r"-?\d+", f.vmax.strip()) else 100
        labels = [plain_text(p) for p in f.choices.split("|")] if f.choices.strip() else []
        left, right = (labels[0], labels[-1]) if labels else ("", "")
        if len(labels) == 3 and labels[1]:
            self.note(f, "warning", f"Curious sliders have no middle label ({labels[1]!r} was dropped)")
        if 0 <= low < high <= 12:
            item.type, item.min_value, item.max_value = "slider", low, high
            item.min_label, item.max_label = left[:100], right[:100]
            return item
        choice = self.d.choose(
            "slider",
            f"REDCap sliders go from {low} to {high}, but Curious sliders stop at 12. How should sliders be converted?",
            [
                ("numberSelect", "A number picker with the same range (answers keep REDCap's values)"),
                ("slider", "A Curious slider from 0 to 10 (answers are on a different scale than REDCap's)"),
                ("text", "A text box for numbers"),
            ],
            f"For example {f.name}: {left or low} to {right or high}",
        )
        ends = f" ({low} = {left}, {high} = {right})" if left or right else ""
        if choice == "numberSelect" and low >= 0:
            item.type, item.min_value, item.max_value = "numberSelect", low, high
            item.prompt += ends
        elif choice == "slider":
            item.type, item.min_value, item.max_value = "slider", 0, 10
            item.min_label, item.max_label = left[:100], right[:100]
            self.note(f, "decision", f"slider rescaled from {low}–{high} to 0–10")
        else:
            item.type = "text"
            item.rules.append("numbers only")
            item.prompt += ends
        return item

    def special_field(self, f: Field, kind: str, tags: dict[str, str], blocks: list[str]) -> str | None:
        """'skip', 'calc', 'message', 'hidden', or None for fields REDCap computes, hides, or shows read-only."""
        if kind == "calc":
            return "calc"  # becomes a subscale if it can, once the whole form is converted
        if "CALCTEXT" in blocks or "CALCDATE" in blocks or kind == "sql":
            what = "an SQL field" if kind == "sql" else "calculated with @CALCTEXT/@CALCDATE"
            self.note(f, "warning", f"{what} in REDCap; Curious can't compute it, so it was left out")
            return "skip"
        if "HIDDEN" in tags or "HIDDEN-SURVEY" in tags:
            choice = self.d.choose(
                "hidden_fields",
                "Some fields are hidden from participants in REDCap (@HIDDEN, @HIDDEN-SURVEY). In Curious nobody can "
                "fill in a hidden item. What should happen to them?",
                [("skip", "Leave them out"), ("hidden", "Keep them as hidden items")],
                f"For example {f.name}: {plain_text(f.label)[:60]}",
            )
            self.note(
                f, "decision", "hidden in REDCap; " + ("left out" if choice == "skip" else "kept as a hidden item")
            )
            return choice
        if ("READONLY" in tags or "READONLY-SURVEY" in tags) and kind != "descriptive":
            choice = self.d.choose(
                "readonly",
                "Some fields are read-only in REDCap (@READONLY): participants see them but can't answer. "
                "What should they become?",
                [
                    ("message", "A message screen showing the field's label (and its @DEFAULT value)"),
                    ("keep", "An ordinary question"),
                    ("skip", "Leave them out"),
                ],
                f"For example {f.name}: {plain_text(f.label)[:60]}",
            )
            if choice != "keep":
                self.note(
                    f,
                    "decision",
                    "read-only in REDCap; " + ("shown as a message" if choice == "message" else "left out"),
                )
                return choice
        return None

    def field_item(self, f: Field, names_before: set[str], all_names: set[str]) -> _Item | None:
        kind = f.type.strip().lower()
        tag_list, blocks = annotation_tags(f.annotation or "")
        tags = dict(tag_list)
        special = self.special_field(f, kind, tags, blocks)
        if special == "calc":
            self.calcs.append(f)
            return None
        if special == "skip":
            return None
        item = _Item(name=f.name, type="", prompt=self.prompt(f, names_before, all_names), line=f.line, source=f)
        item.hidden = special == "hidden"
        if special == "message":
            if tags.get("DEFAULT"):
                item.prompt = f"{item.prompt}\n\n{tags['DEFAULT']}".strip()
            if not item.prompt:
                self.note(f, "warning", "read-only field without text; left out")
                return None
            item.type = "message"
            return item
        if kind in ("radio", "dropdown", "yesno", "truefalse", "checkbox"):
            parsed = self.choices(f)
            if parsed is None:
                return None
            item.options, item.code_map = parsed
            item.type = "multiSelect" if kind == "checkbox" else "singleSelect"
            single = item.type == "singleSelect"
            shown = f"{f.label} {f.section}".lower()
            if single and re.search(r"(check|select|mark) all that apply", shown):
                self.note(
                    f, "warning", "says 'check all that apply' but is single-choice in REDCap; kept single-choice"
                )
        elif kind == "text":
            item.type = "text"
            if self.text_item(f, item) is None:
                return None
        elif kind == "notes":
            choice = self.d.choose(
                "notes",
                "How should REDCap notes boxes (long text) be converted?",
                [
                    ("paragraphText", "A paragraph text item (up to 1000 characters)"),
                    ("text", "A text item (up to 300 characters)"),
                ],
            )
            item.type = choice
        elif kind == "descriptive":
            if not item.prompt:
                self.note(
                    f, "info", "descriptive field without text (in REDCap it may show a file or video, which the "
                    "data dictionary doesn't include); left out",
                )  # fmt: skip
                return None
            item.type = "message"
            if re.search(r"\bvideos?\b", item.prompt, re.IGNORECASE):
                self.note(
                    f, "warning", "mentions a video, but REDCap data dictionaries don't include attached videos or "
                    "files; add it to this screen in the Curious admin panel",
                )  # fmt: skip
        elif kind == "slider":
            item.type = "slider"
            self.slider_item(f, item)
        elif kind == "file":
            if f.validation.strip().lower() == "signature":
                choice = self.d.choose(
                    "signature",
                    "How should REDCap signature fields be converted?",
                    [("drawing", "A drawing item"), ("skip", "Leave them out")],
                )
            else:
                choice = self.d.choose(
                    "file",
                    "Curious has no general file upload. How should REDCap file fields be converted?",
                    [
                        ("skip", "Leave them out"),
                        ("photo", "A photo item"),
                        ("video", "A video item"),
                        ("audio", "An audio recording item"),
                    ],
                    f"For example {f.name}: {plain_text(f.label)[:60]}",
                )
            if choice == "skip":
                self.note(f, "decision", "file field left out")
                return None
            item.type = choice
        else:
            self.note(f, "warning", f"unknown field type {f.type!r}; left out")
            return None

        if item.type != "message":
            required = f.required.strip().lower() in ("y", "yes", "1")
            item.skippable = "no" if required else "yes"
            if required and item.type in ("text", "paragraphText"):
                item.rules.append("response required")
        if f.identifier.strip().lower() in ("y", "yes", "1"):
            self.note(
                f, "info", "marked as an identifier in REDCap; Curious has no such flag (all answers are encrypted)"
            )
        if tag_list or blocks:
            self.apply_annotations(f, item, tag_list, blocks)
        return item

    def prompt(self, f: Field, names_before: set[str], all_names: set[str]) -> str:
        text = html_to_markdown(f.label)
        if text != f.label.strip() and "<" in f.label:
            self.note(f, "info", "HTML formatting in the label was converted to Markdown")
        text = self.piping(f, text, names_before, all_names)
        if f.note.strip():
            choice = self.d.choose(
                "field_notes",
                "REDCap field notes are the small text under a question. What should happen to them?",
                [("append", "Add them to the prompt, in italics"), ("drop", "Leave them out")],
                f"For example {f.name}: {plain_text(f.note)[:60]}",
            )
            if choice == "append":
                text += f"\n\n*{plain_text(f.note)}*"
        return text

    def piping(self, f: Field, text: str, names_before: set[str], all_names: set[str]) -> str:
        def replace(match: re.Match) -> str:
            name = match[1]
            if name in names_before:
                return f"[[{name}]]"
            if name in all_names:
                self.note(
                    f, "warning", f"shows [{name}], which is not an earlier converted field of this form; kept as text"
                )
            return match[0]

        return re.sub(r"\[([a-z][a-z0-9_]*)(?:\(\w+\))?(?::[a-z-]+)?\]", replace, text)

    # --- branching -----------------------------------------------------------------------------------

    @staticmethod
    def matching_codes(ref: str, op: str, value: str, codes: Iterable[str]) -> list[str]:
        """The choice codes for which `[ref] op value` holds (a single-choice field compared with > or <)."""
        try:
            limit = float(value)
            numbers = {c: float(c) for c in codes}
        except ValueError:
            raise Unsupported(f"compares {ref} with {op} on values that aren't numbers") from None
        test = {">": limit.__lt__, "<": limit.__gt__, ">=": limit.__le__, "<=": limit.__ge__}[op]
        matching = [c for c, number in numbers.items() if test(number)]
        if not matching:
            raise Unsupported(f"{ref} {op} {value} can never be true")
        return matching

    def condition_texts(self, ref: str, code: str | None, op: str, value: str, known: dict[str, Any]) -> list[str]:
        target = known.get(ref)
        if target is None:
            raise Unsupported(f"refers to {ref}, which is not an earlier converted field in this form")
        item, row = target
        if value == "":
            raise Unsupported("checks whether a field was answered, which Curious can't")
        if item.type == "multiSelectRows":
            label = item.grid_codes.get(code or "")
            if label is None or (op, value) not in (("=", "1"), ("=", "0"), ("<>", "1"), ("<>", "0")):
                raise Unsupported(f"compares the checkbox {ref} in a way Curious can't")
            verb = "includes" if (op, value) in (("=", "1"), ("<>", "0")) else "does not include"
            return [f'{item.name} [{row}] {verb} "{label}"']
        if item.type == "singleSelectRows":
            if op in ("=", "<>"):
                if value not in item.grid_codes:
                    raise Unsupported(f"uses a value {ref} doesn't have ({value})")
                return [f'{item.name} [{row}] {"=" if op == "=" else "!="} "{item.grid_codes[value]}"']
            codes = self.matching_codes(ref, op, value, item.grid_codes)
            return [f'{item.name} [{row}] = "{item.grid_codes[c]}"' for c in codes]
        if item.type == "multiSelect":
            if code is None:
                raise Unsupported(f"compares the checkbox {ref} without saying which option, e.g. [{ref}(1)]")
            if code not in item.code_map or (op, value) not in (("=", "1"), ("=", "0"), ("<>", "1"), ("<>", "0")):
                raise Unsupported(f"compares the checkbox {ref} in a way Curious can't")
            label = next(text for v, text, _ in item.options if v == item.code_map[code])
            verb = "includes" if (op, value) in (("=", "1"), ("<>", "0")) else "does not include"
            return [f'{item.name} {verb} "{label}" ({item.code_map[code]})']
        if item.type == "singleSelect":
            if code is not None:
                raise Unsupported(f"treats {ref} as a checkbox, but it is single-choice")
            if op in ("=", "<>"):
                if value not in item.code_map:
                    raise Unsupported(f"uses a value {ref} doesn't have ({value})")
                matching = [item.code_map[value]]
                symbol = "=" if op == "=" else "!="
            else:
                matching = [item.code_map[c] for c in self.matching_codes(ref, op, value, item.code_map)]
                symbol = "="
            labels = {v: text for v, text, _ in item.options}
            return [f'{item.name} {symbol} "{labels[v]}" ({v})' for v in matching]
        if item.type in ("numberSelect", "slider"):
            try:
                number = float(value)
            except ValueError:
                raise Unsupported(f"compares {ref} with {value!r}, which is not a number") from None
            shown = int(number) if number.is_integer() else number
            if op in (">=", "<="):
                if not number.is_integer():
                    raise Unsupported(f"uses {op} with a decimal on {ref}")
                return [f"{item.name} {op[0]} {int(number) - 1 if op == '>=' else int(number) + 1}"]
            return [f"{item.name} {'!=' if op == '<>' else op} {shown}"]
        if item.type in ("date", "time"):
            try:
                if item.type == "date":
                    moment = date.fromisoformat(value)
                    step, show = timedelta(days=1), lambda d: d.isoformat()
                else:
                    moment = datetime.combine(date.min, time.fromisoformat(value))
                    step, show = timedelta(minutes=1), lambda d: d.strftime("%H:%M")
            except ValueError:
                raise Unsupported(f"compares {ref} with {value!r}, which Curious can't read") from None
            if op in (">=", "<="):
                return [f"{item.name} {op[0]} {show(moment - step if op == '>=' else moment + step)}"]
            return [f"{item.name} {'!=' if op == '<>' else op} {show(moment)}"]
        raise Unsupported(f"depends on {ref}, a {item.type} item, which Curious skip logic can't use")

    def branching(self, f: Field, known: dict[str, Any]) -> str | None:
        """shown_if text, '' for none, or None when the field should be left out."""
        logic = f.branching.strip()
        if not logic:
            return ""
        tree = self.resolve(parse_logic(logic), known)
        exact = _exact(tree)
        if exact:
            return (" OR " if exact[0] == "or" else " AND ").join(exact[1])

        approximate = _approximate(tree)
        reasons = list(dict.fromkeys(_reasons(tree))) or ["can't be expressed"]
        reason = "; ".join(reasons[:3])
        shown_logic = " ".join(logic.split())
        choice = self.d.choose(
            "branching",
            "Some branching logic can't be expressed exactly in Curious. What should happen to those fields?",
            [
                (
                    "approximate",
                    "Keep the conditions Curious can express (the field may show in more cases than in REDCap)",
                ),
                ("show", "Always show the field"),
                ("skip", "Leave the field out"),
                ("each", "Decide for each field"),
            ],
            f"For example {f.name}: {shown_logic[:160]} — it {reason}",
        )
        if choice == "each":
            kept = (" OR " if approximate[0] == "or" else " AND ").join(approximate[1]) if approximate else ""
            options = [("approximate", f"Keep only: {kept}")] if approximate else []
            options += [("show", "Always show the field"), ("skip", "Leave the field out")]
            choice = self.d.choose(
                f"branching:{f.name}", f"{f.name}: {shown_logic[:200]!r} {reason}. What should happen?", options
            )
        if choice == "approximate" and approximate:
            text = (" OR " if approximate[0] == "or" else " AND ").join(approximate[1])
            self.note(
                f, "decision", f"skip logic simplified to {text!r}, so it may show in more cases than in REDCap. "
                f"REDCap's logic {reason}: {shown_logic}",
            )  # fmt: skip
            return text
        if choice == "skip":
            self.note(f, "decision", f"left out: its branching logic {reason} ({shown_logic})")
            return None
        self.note(f, "decision", f"shown always: its branching logic {reason} ({shown_logic})")
        return ""

    def resolve(self, node: Any, known: dict[str, Any]) -> Any:
        """Turn each comparison into Curious conditions (_Ok) or a reason it can't be (_Bad)."""
        if isinstance(node, Opaque):
            return _Bad(node.reason)
        if isinstance(node, Compare):
            try:
                texts = self.condition_texts(node.ref, node.code, node.op, node.value, known)
            except Unsupported as e:
                return _Bad(str(e))
            return _Ok(texts[0]) if len(texts) == 1 else Logic("or", [_Ok(t) for t in texts])
        return _group(node.joiner, [self.resolve(part, known) for part in node.parts])

    # --- matrix groups ----------------------------------------------------------------------------

    def grid_reason(self, members: list[Field]) -> str | None:
        kinds = {m.type.strip().lower() for m in members}
        if kinds - {"radio", "checkbox"} or len(kinds) > 1:
            return "its fields aren't all radio buttons or all checkboxes"
        if len({m.choices.strip() for m in members}) > 1:
            return "its fields have different choices"
        if len({m.branching.strip() for m in members}) > 1:
            return "its fields have different branching logic"
        if any(len(plain_text(m.label)) > 100 for m in members):
            return "some labels are longer than the 100 characters a Curious grid row allows"
        if any(len(plain_text(p.partition(",")[2])) > 100 for p in members[0].choices.split("|")):
            return "some choices are longer than 100 characters"
        return None

    def grid_item(self, members: list[Field], name: str, prompt: str) -> _Item:
        first = members[0]
        kind = "multiSelectRows" if first.type.strip().lower() == "checkbox" else "singleSelectRows"
        pairs = [p.partition(",") for p in first.choices.split("|") if p.strip()]
        item = _Item(name=name, type=kind, prompt=prompt or "Answer each row.", line=first.line, source=first)
        item.rows = [plain_text(m.label) for m in members]
        item.grid_options = [plain_text(label) for _, _, label in pairs]
        item.grid_codes = {code.strip(): plain_text(label) for code, _, label in pairs}
        item.members = members
        required = all(m.required.strip().lower() in ("y", "yes", "1") for m in members)
        item.skippable = "no" if required else "yes"
        return item

    # --- a whole form --------------------------------------------------------------------------------

    def form(self, fields: list[Field], all_names: set[str]) -> tuple[list[_Item], list[tuple[str, str, list[str]]]]:
        """The form's items, and the subscales made from its calculated fields."""
        self.calcs: list[Field] = []
        self.item_scoring: dict[str, tuple[Rule | None, str]] = {}  # field -> (rule, subscale that set it)
        items: list[_Item] = []
        known: dict[str, Any] = {}  # REDCap field -> (Curious item, grid row label or None)
        names_before: set[str] = set()
        used_names = {f.name for f in fields}
        header_choice = None
        index = 0
        while index < len(fields):
            f = fields[index]
            group = [f]
            if f.matrix.strip():
                while (
                    index + len(group) < len(fields) and fields[index + len(group)].matrix.strip() == f.matrix.strip()
                ):
                    group.append(fields[index + len(group)])
            index += len(group)

            section = html_to_markdown(f.section) if f.section.strip() else ""
            as_grid = False
            if f.matrix.strip():
                if any(m.ranking.strip().lower() in ("y", "yes", "1") for m in group):
                    self.note(
                        f,
                        "warning",
                        f"matrix {f.matrix!r} is ranked in REDCap; Curious can't stop the same rank being chosen twice",
                    )
                choice = self.d.choose(
                    "matrix",
                    "How should REDCap matrix groups be converted?",
                    [
                        ("separate", "One item per field (keeps every variable name and coded value)"),
                        ("grid", "One Curious grid item per group (answers are stored as option labels)"),
                        ("each", "Decide for each group"),
                    ],
                    f"For example {f.matrix} ({len(group)} fields)",
                )
                if choice == "each":
                    choice = self.d.choose(
                        f"matrix:{f.matrix.strip()}",
                        f"Matrix {f.matrix} ({len(group)} fields, starting with {f.name}): separate items or one grid?",
                        [("separate", "One item per field"), ("grid", "One grid item")],
                    )
                if choice == "grid":
                    reason = self.grid_reason(group)
                    if reason:
                        self.note(f, "warning", f"matrix {f.matrix!r} kept as separate items: {reason}")
                    else:
                        as_grid = True

            if section and not as_grid:
                header_choice = header_choice or self.d.choose(
                    "section_headers",
                    "Curious has no section headers. What should happen to them?",
                    [
                        ("message", "Show each one as its own screen, before its fields"),
                        ("prepend", "Add it to the prompt of the field (every field, for a matrix group)"),
                        ("drop", "Leave them out"),
                    ],
                    f"For example before {f.name}: {plain_text(f.section)[:60]}",
                )
            if as_grid:
                name = f.matrix.strip()
                while name in used_names:
                    name += "_grid"
                used_names.add(name)
                grid = self.grid_item(group, name, section)
                shown = self.branching(f, known)
                if shown is None:
                    continue
                grid.shown_if = shown
                self.note(f, "decision", f"matrix {f.matrix!r} became the grid item {name}; REDCap codes are not kept")
                items.append(grid)
                for m, row in zip(group, grid.rows):
                    known[m.name] = (grid, row)
                    names_before.add(m.name)
                continue

            group_items: list[_Item] = []
            for m in group:
                item = self.field_item(m, names_before, all_names)
                if item is None:
                    continue
                shown = self.branching(m, known)
                if shown is None:
                    continue
                if item.hidden and shown:
                    self.note(m, "warning", "hidden fields can't have skip logic in Curious; the logic was dropped")
                    shown = ""
                item.shown_if = shown
                group_items.append(item)
                known[m.name] = (item, None)
                names_before.add(m.name)
            if section and header_choice == "message" and group_items:
                name = f"{group_items[0].name}_header"
                while name in used_names:
                    name += "_"
                used_names.add(name)
                header = _Item(name=name, type="message", prompt=section, line=f.line, shown_if=group_items[0].shown_if)
                group_items.insert(0, header)
                self.note(f, "decision", f"section header shown as the screen {name}")
            elif section and header_choice == "prepend":
                for item in group_items if f.matrix.strip() else group_items[:1]:
                    item.prompt = f"{section}\n\n{item.prompt}"
            items += group_items
        return items, self.calc_subscales(known)

    # --- calculated fields -----------------------------------------------------------------------------

    def calc_subscales(self, known: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
        """(name, scoring, items) for each calculated field that adds up or averages answers of this form.

        The items it uses get scores equal to their REDCap codes, so Curious adds up the same values.
        """
        parsed: dict[str, tuple[Field, tuple]] = {}
        for f in self.calcs:
            found = parse_calc(f.choices)
            if found is None:
                self.note(
                    f, "warning", "calculated in REDCap; Curious can only add up, average, or count answers, so it was "
                    f"left out (formula: {plain_text(f.choices)[:200] or 'empty'})",
                )  # fmt: skip
            else:
                parsed[f.name] = (f, found)
        if not parsed:
            return []
        first = next(iter(parsed.values()))[0]
        choice = self.d.choose(
            "calc_subscales",
            "Some calculated fields add up, average, or count answers. Curious can compute these as subscales. "
            "What should happen?",
            [
                ("subscale", "Make them Curious subscales (the items they use are scored with their REDCap codes)"),
                ("skip", "Leave them out"),
            ],
            f"For example {first.name}: {plain_text(first.choices)[:100]}",
        )
        if choice == "skip":
            for f, _ in parsed.values():
                self.note(f, "decision", "calculated field left out")
            return []

        made: dict[str, tuple[str, list[str]]] = {}
        pending = dict(parsed)
        while pending:
            progress = False
            for name, (f, (scoring, terms, rounded, style)) in list(pending.items()):
                refs = [ref for ref, _ in terms]
                if any(ref in pending and ref != name for ref in refs):
                    continue  # uses another calculated field that isn't done yet
                del pending[name]
                progress = True
                parts, problem = [], None
                for ref, rule in terms:
                    target = known.get(ref)
                    scored_by = self.item_scoring.get(ref)
                    if ref in made:
                        if rule is not None:
                            problem = f"it compares the subscale {ref} with a threshold, which Curious can't"
                        parts.append(f"{ref} (subscale)")
                    elif target is None:
                        problem = f"it uses {ref}, which is not a converted field of this form"
                    elif target[1] is not None:
                        problem = f"it uses {ref}, which became a row of a grid"
                    elif target[0].type not in ("singleSelect", "multiSelect", "slider"):
                        problem = f"it uses {ref}, a {target[0].type} item; Curious subscales can only add up choice and slider answers"
                    elif scored_by is not None and scored_by[0] != rule:
                        problem = f"it scores {ref} differently than the subscale {scored_by[1]}, and an item has one set of scores"
                    else:
                        parts.append(ref)
                    if problem:
                        break
                if problem:
                    self.note(f, "warning", f"calculated in REDCap but can't become a subscale: {problem}; left out")
                    continue
                for ref, rule in terms:
                    if ref not in made:
                        _score_item(known[ref][0], rule)
                        self.item_scoring.setdefault(ref, (rule, name))
                made[name] = (scoring, parts)
                if style == "count":
                    rules = "; ".join(dict.fromkeys(_describe_rule(rule) for _, rule in terms if rule))
                    details = [f"became the subscale {name} (sum of {', '.join(parts)}); each item scores {rules}"]
                else:
                    how = "average" if scoring == "average" else "sum"
                    details = [
                        f"became the subscale {name} ({how} of {', '.join(parts)}); its items are scored with their codes"
                    ]
                if style == "plus":
                    details.append(
                        "REDCap leaves the result empty when an item is unanswered, but Curious counts it as 0"
                    )
                elif scoring == "average":
                    details.append(
                        "REDCap's mean() averages only answered items, but Curious counts unanswered ones as 0"
                    )
                if rounded:
                    details.append("REDCap rounds the result; Curious keeps 2 decimals")
                self.note(f, "decision", "; ".join(details))
            if not progress:
                for name, (f, _) in pending.items():
                    self.note(
                        f, "warning", "calculated fields that use each other in a loop can't be converted; left out"
                    )
                break
        return [(name, *made[name]) for name in parsed if name in made]


def _score_item(item: _Item, rule: Rule | None = None) -> None:
    """Score each option with its code, or 1/0 (or whatever the rule says) by a threshold."""
    if item.type == "slider":
        values = list(range(item.min_value or 0, (item.max_value or 0) + 1))
    else:
        values = [v for v, _, _ in item.options]
    item.scores = {v: (v if rule is None else rule[2] if _holds(rule, v) else rule[3]) for v in values}


def _rows_for(
    activity: str, items: list[_Item], scores_from_codes: bool, subscales: list[tuple[str, str, list[str]]] = ()
) -> list[dict]:
    rows = []
    for position, item in enumerate(items, start=1):
        if not item.scores and scores_from_codes and item.options:
            _score_item(item)
        scores = item.scores
        values: dict[str, Any] = {}
        cells: dict[str, Any] = {
            "activity_name": activity,
            "item_order": position,
            "item_name": item.name,
            "item_type": item.type,
            "item_prompt": item.prompt,
            "response_rules": "; ".join(dict.fromkeys(item.rules)),
            "skippable": item.skippable,
            "hidden": "yes" if item.hidden else "no",
            "shown_if": item.shown_if,
        }
        if item.type in ("singleSelect", "multiSelect"):
            parts = []
            for value, text, flags in item.options:
                order = [flag for flag in ("none of the above", "hidden") if flag in flags]
                parts.append(f"{value}={text}" + (f" ({', '.join(order)})" if order else ""))
            cells["value_labels"] = " | ".join(parts)
            cells["scores"] = " | ".join(f"{v}={s}" for v, s in scores.items())
            values = {"options": [{"text": t, "value": v, "score": scores.get(v)} for v, t, _ in item.options]}
        elif item.type in ("slider", "numberSelect"):
            cells["min_value"], cells["max_value"] = item.min_value, item.max_value
            values = {"minValue": item.min_value, "maxValue": item.max_value}
            if item.type == "slider":
                cells["min_label"], cells["max_label"] = item.min_label, item.max_label
                if scores:
                    cells["scores"] = " | ".join(f"{v}={s}" for v, s in scores.items())
                    values["scores"] = list(scores.values())
                ends = [(item.min_value, item.min_label), (item.max_value, item.max_label)]
                cells["value_labels"] = " | ".join(f"{v}={label}" for v, label in ends if label)
        elif item.type in ("singleSelectRows", "multiSelectRows"):
            cells["rows"] = " | ".join(item.rows)
            cells["value_labels"] = " | ".join(item.grid_options)
        cells["item_response_options"] = response_options_cell({"responseType": item.type, "responseValues": values})
        rows.append(cells)
    for name, scoring, parts in subscales:
        rows.append({"activity_name": activity, **subscale_cells(name, scoring, parts)})
    return rows


_ROUNDED = re.compile(r"(?:round|rounddown|roundup)\((?P<inner>.*?)(?:,\s*-?\d+)?\)", re.IGNORECASE | re.DOTALL)
_FIELD = r"\[[a-z][a-z0-9_]*\]"


def _wrapped(text: str) -> bool:
    """Whether one pair of parentheses encloses all of `text`."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for index, char in enumerate(text):
        depth += 1 if char == "(" else -1 if char == ")" else 0
        if depth == 0 and index < len(text) - 1:
            return False
    return True


_THRESHOLD = re.compile(
    r"if\(\s*\[(?P<field>[a-z][a-z0-9_]*)\]\s*(?P<op>>=|<=|<>|!=|==|=|>|<)\s*(?P<q>['\"]?)(?P<value>-?\d+(?:\.\d+)?)(?P=q)"
    r"\s*,\s*(?P<yes>-?\d+)\s*,\s*(?P<no>-?\d+)\s*\)",
    re.IGNORECASE,
)
Rule = tuple[str, float, int, int]  # if([field] op value, yes, no)
Term = tuple[str, "Rule | None"]  # a field, scored with its codes (None) or by a threshold rule


def parse_calc(formula: str) -> tuple[str, list[Term], bool, str] | None:
    """A REDCap calculation that adds up or averages answers -> (scoring, terms, rounded, style), else None.

    `style` is "plus" for [a] + [b] (REDCap gives no result when one is empty), "function" for
    sum()/mean(), or "count" for if([a] >= 2, 1, 0) + … (each answer counts as yes or no).
    """
    text = " ".join(formula.split())
    rounded = False
    while match := _ROUNDED.fullmatch(text):
        text, rounded = match["inner"].strip(), True
    while _wrapped(text):
        text = text[1:-1].strip()

    def fields(part: str) -> list[Term]:
        return [(name, None) for name in re.findall(r"\[([a-z0-9_]+)\]", part)]

    if match := re.fullmatch(r"(sum|mean)\((.*)\)", text, re.IGNORECASE):
        args = [a.strip() for a in match[2].split(",")]
        if all(re.fullmatch(_FIELD, a) for a in args):
            scoring = "sum" if match[1].lower() == "sum" else "average"
            return scoring, [(a[1:-1], None) for a in args], rounded, "function"
        return None
    if re.fullmatch(rf"{_FIELD}(?:\s*\+\s*{_FIELD})+", text):
        return "sum", fields(text), rounded, "plus"
    if match := re.fullmatch(rf"\((?P<sum>{_FIELD}(?:\s*\+\s*{_FIELD})+)\)\s*/\s*(?P<n>\d+)", text):
        if int(match["n"]) == len(fields(match["sum"])):
            return "average", fields(match["sum"]), rounded, "plus"
        return None
    parts = [part.strip() for part in text.split("+")]
    terms: list[Term] = []
    for part in parts:
        if found := _THRESHOLD.fullmatch(part):
            op = {"==": "=", "!=": "<>"}.get(found["op"], found["op"])
            terms.append((found["field"], (op, float(found["value"]), int(found["yes"]), int(found["no"]))))
        elif re.fullmatch(_FIELD, part):
            terms.append((part[1:-1], None))
        else:
            return None
    if len(terms) > 1 and any(rule for _, rule in terms):
        return "sum", terms, rounded, "count"
    return None


def _holds(rule: Rule, value: float) -> bool:
    op, limit, _, _ = rule
    return {
        ">": value > limit, "<": value < limit, ">=": value >= limit,
        "<=": value <= limit, "=": value == limit, "<>": value != limit,
    }[op]  # fmt: skip


def _describe_rule(rule: Rule) -> str:
    op, limit, yes, no = rule
    shown = int(limit) if limit.is_integer() else limit
    return f"{yes} when the answer is {op} {shown}, else {no}"


def convert_redcap(path: Path, decisions: Decisions) -> Conversion:
    rows, unknown = _read_rows(path)
    converter = _Converter(decisions)
    for column, values in unknown.items():
        shown = ", ".join(f"{v!r} (line {line})" for line, v in values[:5])
        more = f" and {len(values) - 5} more" if len(values) > 5 else ""
        converter.note(None, "info", f"{column} is not a REDCap column; ignored. Values: {shown}{more}", line="")

    fields: list[Field] = []
    orphans: list[tuple[int, dict]] = []
    for line, values in rows:
        if not any(v.strip() for v in values.values()):
            continue
        if not values.get("name", "").strip():
            orphans.append((line, values))
            fields.append(None)  # type: ignore[arg-type]  # placeholder, resolved below
            continue
        fields.append(
            Field(
                line=line, **{k: (v.strip() if k not in ("label", "section", "note") else v) for k, v in values.items()}
            )
        )

    if orphans:
        first_line, first = orphans[0]
        choice = decisions.choose(
            "orphan_rows",
            f"{len(orphans)} rows have text but no variable name. They look like the end of the label above them. "
            "What should happen?",
            [("append", "Add their text to the label of the field above"), ("ignore", "Leave them out")],
            f"For example line {first_line}: {plain_text(first.get('label', ''))[:70]!r}",
        )
    merged: list[Field] = []
    orphan_iter = iter(orphans)
    for entry in fields:
        if entry is not None:
            merged.append(entry)
            continue
        line, values = next(orphan_iter)
        text = values.get("label", "").strip()
        if choice == "append" and merged and text:
            merged[-1].label = f"{merged[-1].label.rstrip()}\n\n{text}"
            converter.note(merged[-1], "decision", f"line {line} (no variable name) added to the label")
        else:
            converter.note(
                None, "decision", f"line {line} has no variable name; left out ({plain_text(text)[:60]!r})", line=line
            )
        extra = [k for k, v in values.items() if v.strip() and k != "label"]
        if extra:
            converter.note(None, "warning", f"line {line} also had {', '.join(extra)}, which were ignored", line=line)

    seen: set[str] = set()
    by_form: dict[str, list[Field]] = {}
    for f in merged:
        if f.name in seen:
            converter.note(f, "warning", "this variable name is used twice; the second one was left out")
            continue
        seen.add(f.name)
        if not f.form:
            converter.note(f, "warning", "has no form name; left out")
            continue
        by_form.setdefault(f.form, []).append(f)

    if len(by_form) > 1:
        chosen = decisions.choose_many(
            "forms",
            "Which REDCap forms should become Curious activities?",
            [(form, f"{form} ({len(fields)} fields)") for form, fields in by_form.items()],
        )
        for form in [f for f in by_form if f not in chosen]:
            converter.note(None, "decision", f"form {form!r} not converted", line="")
        by_form = {form: fields for form, fields in by_form.items() if form in chosen}
        if not by_form:
            raise RedcapError("No forms were chosen, so there is nothing to convert.")
    selected = [f for fields in by_form.values() for f in fields]

    activities: dict[str, list[dict]] = {}
    forms: dict[str, str] = {}
    has_choices = any(
        f.type.strip().lower() in ("radio", "dropdown", "checkbox", "yesno", "truefalse") for f in selected
    )
    scores_from_codes = has_choices and (
        decisions.choose(
            "option_scores",
            "REDCap options have codes but no scores. Should Curious options get scores?",
            [("none", "No scores (you can add them later)"), ("codes", "Score each option with its code")],
        )
        == "codes"
    )
    for form, form_fields in by_form.items():
        name = decisions.text(
            f"activity_name:{form}", f"Curious activity name for the REDCap form {form!r}:", pretty_form_name(form)
        )
        if len(name) > 55:
            converter.note(
                None, "warning", f"the activity name {name!r} is longer than the admin panel's 55 characters", line=""
            )
        items, subscales = converter.form(form_fields, seen)
        if not items:
            converter.note(None, "warning", f"form {form!r} has no fields left, so it has no activity", line="")
            continue
        converter.counts.update(item.type for item in items)
        activities[name] = _rows_for(name, items, scores_from_codes, subscales)
        forms[name] = form

    for name, dictionary_rows in activities.items():
        for problem in validate_rows(name, dictionary_rows):
            converter.note(None, "warning", f"will be refused on upload: {problem}", line="")
    return Conversion(activities, forms, converter.notes, dict(decisions.used), converter.counts)


def validate_rows(activity_name: str, rows: list[dict]) -> list[str]:
    """Build the rows as new items and check them, so problems surface before an upload."""
    activity = {"name": activity_name, "items": [], "isSkippable": False}
    items, by_index, problems, score_rows = [], {}, [], []
    for row in rows:
        cells = {k: ("" if v is None else str(v)) for k, v in row.items()}
        item_row = ItemRow(cells, f"item {cells.get('item_name')}")
        if cells.get("item_type") in SCORE_ROW_TYPES:
            score_rows.append(item_row)
            continue
        try:
            item, _ = build_item(item_row, None, activity)
        except _CellError as e:
            problems.append(f"{cells.get('item_name')}: {e}")
            continue
        by_index[len(items)] = item_row
        items.append(item)
    problems += [str(p) for p in resolve_logic(items, by_index, [], activity)]
    if score_rows:
        scores = plan_scores(score_rows, activity)
        problems += [str(p) for p in scores.problems]
        apply_scores(scores, activity, keep=False)
    activity["items"] = items
    problems += [str(p) for p in check_activity(activity) if p.severity == "error"]
    return problems


def write_conversion(conversion: Conversion, folder: Path, source: Path) -> list[Path]:
    """One dictionary CSV per activity, the report, and the decisions (reusable with --decisions)."""
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    stem = file_slug(source.stem)[:80]
    for name, rows in conversion.activities.items():
        path = folder / f"{stem}__{file_slug(name)[:80]}__data_dictionary.csv"
        write_csv(path, rows, DICTIONARY_COLUMNS)
        written.append(path)
    write_csv(folder / "conversion_report.csv", [vars(n) for n in conversion.notes], REPORT_COLUMNS)
    (folder / "redcap_decisions.json").write_text(json.dumps(conversion.decisions, indent=2, ensure_ascii=False))
    return written
