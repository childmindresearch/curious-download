"""Write CSV files the way the admin panel does (sanitizeCSVValue + SheetJS writeFile)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .jscompat import is_number, nullish, to_string

_DANGEROUS_START = re.compile(r"^[=+\-@\t\r\n]")
_NEWLINES = re.compile(r"\r\n|\r|\n")


def sanitize_value(value: Any) -> str:
    """csvSanitization.ts sanitizeCSVValue: prefix formula-like lines with a quote."""
    if nullish(value):
        return ""
    if isinstance(value, list):
        return ",".join("" if nullish(item) else to_string(item) for item in value)
    text = to_string(value)
    if text == "":
        return text
    if is_number(value) and value < 0:
        return text
    return "\n".join(f"'{line}" if _DANGEROUS_START.match(line) else line for line in _NEWLINES.split(text))


def _csv_field(text: str) -> str:
    """SheetJS sheet_to_csv quoting."""
    if "," in text or "\n" in text or '"' in text:
        return '"' + text.replace('"', '""') + '"'
    if text == "ID":
        return '"ID"'
    return text


def header_for(rows: Iterable[dict], base_columns: list[str]) -> list[str]:
    """SheetJS json_to_sheet header: keys in order of first appearance."""
    header = list(base_columns)
    seen = set(header)
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                header.append(key)
    return header


def write_csv(path: Path, rows: list[dict], base_columns: list[str]) -> list[str]:
    """Write rows as UTF-8 CSV with a BOM and LF line endings. Returns the header used."""
    header = header_for(rows, base_columns)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        fh.write(",".join(_csv_field(column) for column in header) + "\n")
        for row in rows:
            fh.write(",".join(_csv_field(sanitize_value(row.get(column))) for column in header) + "\n")
    return header
