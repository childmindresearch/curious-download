"""JS value semantics, checked against Node where it matters."""

from __future__ import annotations

import json
import math
import shutil
import subprocess

import pytest

from curious_download.jscompat import UNDEFINED, join_with_comma, number_to_string, replace_all, to_string
from curious_download.subscales import round_to_2

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed")


def _node(expression: str, values: list) -> list:
    script = f"const v = JSON.parse(require('fs').readFileSync(0, 'utf8')); process.stdout.write(JSON.stringify({expression}));"
    result = subprocess.run(
        ["node", "-e", script], input=json.dumps(values), capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


NUMBERS = [
    0,
    -0.0,
    1,
    -240,
    1749138592012,
    1749138592.012 * 1000,
    0.1 + 0.2,
    1e21,
    1.5e-7,
    2.5e-6,
    1e-4,
    123.456,
    -1e-7,
]


@needs_node
def test_number_to_string_matches_js():
    expected = _node("v.map(String)", NUMBERS)
    assert [number_to_string(n) for n in NUMBERS] == expected


@needs_node
def test_round_to_2_matches_js_math_round():
    values = [2.125, -2.125, 2.135, 0.125, 1.005, 45.454545, -0.5, 12.5, 2.675, 1234.565, 0.0]
    expected = _node("v.map((n) => Math.round((n + Number.EPSILON) * 100) / 100)", values)
    assert [round_to_2(n) for n in values] == expected


def test_to_string_template_semantics():
    assert to_string(None) == "null"
    assert to_string(UNDEFINED) == "undefined"
    assert to_string([1, None, [2, 3]]) == "1,,2,3"
    assert to_string({"a": 1}) == "[object Object]"
    assert to_string(True) == "true"
    assert to_string(math.nan) == "NaN"


def test_join_with_comma_capitalizes():
    assert join_with_comma(["apple", None, "", 3]) == "Apple, Null, 3"


def test_replace_all_expands_dollar_patterns():
    import re

    pattern = re.compile(r"\[\[x\]\]", re.IGNORECASE)
    assert replace_all("a [[X]] b", pattern, "<$&|$$>") == "a <[[X]]|$> b"
    assert replace_all("a [[x]] b", pattern, "$1") == "a $1 b"
