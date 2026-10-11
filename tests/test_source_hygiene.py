"""Source files must not hide characters a reviewer cannot see, nor name a
former counterparty.

Zero-width spaces, joiners, the BOM, variation selectors and the replacement
character all render as nothing (or as an ambiguous box) in most editors and
diffs, so a string that contains one reads as something it is not. Tests that
need them spell them as escape sequences instead.
"""

from __future__ import annotations

import pathlib
import re
import unicodedata

VALIDATOR = pathlib.Path(__file__).resolve().parents[1]

#: The outreach is closed, so the name stays out of the code, tests and docs.
#: Built from parts, so this file does not contain it either.
_COUNTERPARTY = re.compile("|".join(("bie" + "lik", "obywa" + "tel")), re.IGNORECASE)
_SCANNED_SUFFIXES = (".py", ".md", ".json", ".toml")
_SKIPPED_PARTS = (".venv", "__pycache__", ".pytest_cache")


def _invisible(char: str) -> bool:
    code = ord(char)
    return (
        unicodedata.category(char) in ("Cf", "Zl", "Zp")
        or code == 0x85
        or code == 0xFFFD
        or 0xFE00 <= code <= 0xFE0F
    )


def test_no_invisible_characters_in_python_source():
    offenders = [
        f"{path.relative_to(VALIDATOR)}:{number}: "
        + ", ".join(f"U+{ord(c):04X}" for c in line if _invisible(c))
        for path in sorted(VALIDATOR.rglob("*.py"))
        if ".venv" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(_invisible(c) for c in line)
    ]

    assert not offenders, "Spell these as escape sequences:\n" + "\n".join(offenders)


def _scanned(path: pathlib.Path) -> bool:
    parts = path.relative_to(VALIDATOR).parts
    return (
        path.suffix in _SCANNED_SUFFIXES
        and not any(part in _SKIPPED_PARTS or part.endswith(".egg-info") for part in parts)
    )


def test_no_counterparty_name():
    offenders = [
        f"{path.relative_to(VALIDATOR)}:{number}"
        for path in sorted(VALIDATOR.rglob("*"))
        if path.is_file() and _scanned(path)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _COUNTERPARTY.search(line)
    ]

    assert not offenders, "Remove the former counterparty's name:\n" + "\n".join(offenders)
