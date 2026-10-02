"""JavaScript value semantics needed to reproduce the admin panel's CSV text exactly.

The admin export builds its strings with template literals, `??`, `||`, and
`String.prototype.replace`. These helpers mirror those rules for values decoded
from JSON (None stands for JS null; UNDEFINED stands for a missing property).
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any


class _Undefined:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNDEFINED"

    def __bool__(self) -> bool:
        return False


UNDEFINED: Any = _Undefined()


def nullish(value: Any) -> bool:
    """True for JS null and undefined (the operands `??` skips)."""
    return value is None or value is UNDEFINED


def coalesce(value: Any, fallback: Any) -> Any:
    """JS `value ?? fallback`."""
    return fallback if nullish(value) else value


def truthy(value: Any) -> bool:
    """JS truthiness."""
    if nullish(value) or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def get(obj: Any, key: Any) -> Any:
    """JS `obj?.[key]` for decoded JSON values (dicts, lists, and strings)."""
    if isinstance(obj, dict):
        return obj.get(key, UNDEFINED)
    if isinstance(obj, (list, str)) and isinstance(key, int) and not isinstance(key, bool):
        return obj[key] if 0 <= key < len(obj) else UNDEFINED
    if isinstance(obj, (list, str)) and key == "length":
        return len(obj)
    return UNDEFINED


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def number_to_string(value: float) -> str:
    """JS Number.prototype.toString()."""
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" not in text:
        return text
    if 1e-6 <= abs(value) < 1e21:
        return format(Decimal(text), "f")
    mantissa, exponent = text.split("e")
    exp = int(exponent)
    return f"{mantissa}e{'+' if exp > 0 else '-'}{abs(exp)}"


def to_string(value: Any) -> str:
    """JS String(value), which is also what `${value}` produces."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_number(value):
        return number_to_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return join(value, ",")
    return "[object Object]"


def join(items: list, separator: str = ",") -> str:
    """JS Array.prototype.join: null and undefined become empty strings."""
    return separator.join("" if nullish(item) else to_string(item) for item in items)


def to_number(value: Any) -> float:
    """JS Number(value) for the inputs that occur in item settings."""
    if nullish(value):
        return math.nan if value is UNDEFINED else 0
    if isinstance(value, bool):
        return int(value)
    if is_number(value):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0
        try:
            number = float(text)
        except ValueError:
            return math.nan
        return int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number
    return math.nan


def capitalize(text: str) -> str:
    """Admin `capitalize`: upper-case the first character."""
    return text[:1].upper() + text[1:]


def join_with_comma(items: list, *, capitalize_items: bool = True) -> str:
    """Admin `joinWihComma`."""
    if items is None:
        return ""
    parts = []
    for item in items:
        text = to_string(item)
        if capitalize_items:
            text = capitalize(text)
        if text:
            parts.append(text)
    return ", ".join(parts)


def _expand_replacement(replacement: str, match: re.Match) -> str:
    """Expand the `$` patterns JS recognises in String.prototype.replace replacement strings."""
    out = []
    i = 0
    source = match.string
    while i < len(replacement):
        char = replacement[i]
        if char == "$" and i + 1 < len(replacement):
            nxt = replacement[i + 1]
            if nxt == "$":
                out.append("$")
                i += 2
                continue
            if nxt == "&":
                out.append(match.group(0))
                i += 2
                continue
            if nxt == "`":
                out.append(source[: match.start()])
                i += 2
                continue
            if nxt == "'":
                out.append(source[match.end() :])
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def replace_all(text: str, pattern: re.Pattern, replacement: str) -> str:
    """JS `text.replace(/pattern/g, replacement)` with a string replacement."""
    return pattern.sub(lambda m: _expand_replacement(replacement, m), text)
