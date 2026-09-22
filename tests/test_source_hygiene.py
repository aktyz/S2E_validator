"""Source files must not hide characters a reviewer cannot see.

Zero-width spaces, joiners, the BOM, variation selectors and the replacement
character all render as nothing (or as an ambiguous box) in most editors and
diffs, so a string that contains one reads as something it is not. Tests that
need them spell them as escape sequences instead.
"""

from __future__ import annotations

import pathlib
import unicodedata

VALIDATOR = pathlib.Path(__file__).resolve().parents[1]


def _invisible(char: str) -> bool:
    code = ord(char)
    return (
        unicodedata.category(char) == "Cf"
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
