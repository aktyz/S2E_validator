"""Structural checks on the description text."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import PreFilter
from .interaction import Interaction
from .result import PreFilterResult
from .text import normalize
from .settings import (
    require_bool,
    require_float,
    require_int,
    require_str_sequence,
    settings_from_dict,
)

REASON_EMPTY = "TEXT_EMPTY"
REASON_TOO_SHORT = "TEXT_TOO_SHORT"
REASON_TOO_LONG = "TEXT_TOO_LONG"
REASON_ENCODING = "TEXT_ENCODING_INVALID"
REASON_CONTROL_CHARACTERS = "TEXT_CONTROL_CHARACTERS"
REASON_REPEATED_CHARACTERS = "TEXT_REPEATED_CHARACTERS"
REASON_REPEATED_PATTERN = "TEXT_REPEATED_PATTERN"
REASON_LOW_VARIETY = "TEXT_LOW_CHARACTER_VARIETY"
REASON_INSTRUCTION_ECHO = "TEXT_INSTRUCTION_ECHO"
REASON_LOOKS_TRUNCATED = "TEXT_LOOKS_TRUNCATED"

REPLACEMENT_CHARACTER = "\ufffd"
#: Unicode category Cc only, minus the whitespace people actually type. Format
#: characters (Cf) are deliberately excluded: U+200D joins every multi-part
#: emoji, and U+200B/U+FEFF ride along with ordinary pasted text.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
#: Non-greedy unit of 2-6 characters immediately repeated: "asdfasdfasdf".
_REPEATED_PATTERN = re.compile(r"(.{2,6}?)\1+", re.DOTALL)
#: A hyphen attached to a word, not a spaced dash used as punctuation.
_TRAILING_HYPHEN = re.compile(r"\w-$")


@dataclass(frozen=True)
class TextFormatSettings:
    """Thresholds are deliberately loose: a description that is merely odd
    belongs to the judge, not to a structural check.
    """

    #: A sanity floor, not a copy of the campaign's own rule. Obywatel Bielik
    #: requires 100 characters, so anything shorter should be impossible through
    #: their UI; this only catches what arrives another way.
    min_chars: int = 5
    max_chars: int = 2000
    max_control_characters: int = 0
    #: 6+ identical letters in a row ("aaaaaa"). Digits and punctuation are not
    #: counted: "1000000" and "Wow!!!!!!" are odd, not structurally invalid.
    max_repeated_run: int = 5
    #: A 2-6 character unit repeated this many times, covering most of the text.
    min_pattern_repeats: int = 3
    min_pattern_coverage: float = 0.6
    #: Single-token text with very few distinct characters ("asdfasdf"). Flagged
    #: rather than rejected: "Mississippi" scores the same.
    variety_min_length: int = 8
    min_character_variety: float = 0.5
    #: Campaign placeholder/instruction strings pasted back as the description.
    #: Empty until Bielik's real app strings are known (see #28).
    placeholders: Sequence[str] = field(default_factory=tuple)
    flag_truncated: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TextFormatSettings:
        parsed = settings_from_dict(cls, raw)
        require_int(parsed.min_chars, "min_chars", minimum=1)
        require_int(parsed.max_chars, "max_chars", minimum=parsed.min_chars)
        require_int(parsed.max_control_characters, "max_control_characters", minimum=0)
        require_int(parsed.max_repeated_run, "max_repeated_run", minimum=1)
        require_int(parsed.min_pattern_repeats, "min_pattern_repeats", minimum=2)
        require_float(parsed.min_pattern_coverage, "min_pattern_coverage", 0.0, 1.0)
        require_int(parsed.variety_min_length, "variety_min_length", minimum=1)
        require_float(parsed.min_character_variety, "min_character_variety", 0.0, 1.0)
        require_str_sequence(parsed.placeholders, "placeholders")
        require_bool(parsed.flag_truncated, "flag_truncated")
        return parsed


class TextFormatFilter(PreFilter):
    """Rejects text that is structurally invalid before anything heavier runs."""

    name = "text_format"
    cost_rank = 10

    def __init__(self, settings: TextFormatSettings | None = None):
        self._settings = settings or TextFormatSettings()
        self._placeholders = tuple(
            normalize(p).casefold() for p in self._settings.placeholders if p.strip()
        )

    def check(self, interaction: Interaction) -> PreFilterResult:
        # Cheapest and most decisive first. Encoding damage is found on the raw
        # text; everything after it works on the normalized form, so per
        # character work never runs on unbounded input before the length bound.
        encoding_failure = self._encoding_failure(interaction.text)
        if encoding_failure is not None:
            return encoding_failure

        normalized = normalize(interaction.text)
        steps: tuple[Callable[[str], PreFilterResult | None], ...] = (
            self._length_failure,
            self._control_failure,
            self._echo_failure,
            self._junk_verdict,
            self._truncation_flag,
        )
        for step in steps:
            verdict = step(normalized)
            if verdict is not None:
                return verdict
        return PreFilterResult.passed()

    @staticmethod
    def _encoding_failure(text: str) -> PreFilterResult | None:
        """Catch text that never survived decoding intact.

        Lone surrogates cannot be encoded as UTF-8 at all; U+FFFD is what a
        lossy decode leaves behind.
        """
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return PreFilterResult.rejected(REASON_ENCODING, confidence=1.0, cause="surrogates")
        replacements = text.count(REPLACEMENT_CHARACTER)
        if replacements:
            return PreFilterResult.rejected(
                REASON_ENCODING,
                confidence=1.0,
                cause="replacement_characters",
                count=replacements,
            )
        return None

    def _length_failure(self, normalized: str) -> PreFilterResult | None:
        settings = self._settings
        if not normalized:
            return PreFilterResult.rejected(REASON_EMPTY, confidence=1.0)
        length = len(normalized)
        if length < settings.min_chars:
            return PreFilterResult.rejected(
                REASON_TOO_SHORT, confidence=1.0, length=length, minimum=settings.min_chars
            )
        if length > settings.max_chars:
            return PreFilterResult.rejected(
                REASON_TOO_LONG, confidence=1.0, length=length, maximum=settings.max_chars
            )
        return None

    def _control_failure(self, normalized: str) -> PreFilterResult | None:
        found = len(_CONTROL_CHARACTERS.findall(normalized))
        if found > self._settings.max_control_characters:
            return PreFilterResult.rejected(
                REASON_CONTROL_CHARACTERS, confidence=1.0, count=found
            )
        return None

    def _echo_failure(self, normalized: str) -> PreFilterResult | None:
        folded = normalized.casefold()
        if any(folded == placeholder for placeholder in self._placeholders):
            return PreFilterResult.rejected(REASON_INSTRUCTION_ECHO, confidence=1.0)
        return None

    def _junk_verdict(self, normalized: str) -> PreFilterResult | None:
        settings = self._settings

        run = _longest_letter_run(normalized)
        if run > settings.max_repeated_run:
            return PreFilterResult.rejected(
                REASON_REPEATED_CHARACTERS, confidence=1.0, run_length=run
            )

        pattern = _repeated_pattern(
            normalized, settings.min_pattern_repeats, settings.min_pattern_coverage
        )
        if pattern is not None:
            repeats, coverage = pattern
            return PreFilterResult.rejected(
                REASON_REPEATED_PATTERN,
                confidence=1.0,
                repeats=repeats,
                coverage=round(coverage, 2),
            )

        # Only single-token text: spaces imply real words, where a low distinct
        # character count is normal.
        if " " not in normalized and len(normalized) >= settings.variety_min_length:
            variety = len(set(normalized.casefold())) / len(normalized)
            if variety <= settings.min_character_variety:
                # A flag, not a reject: "Mississippi" scores as low as
                # "asdfasdf", and the judge can tell them apart.
                return PreFilterResult.flagged(
                    REASON_LOW_VARIETY, confidence=0.6, variety=round(variety, 2)
                )
        return None

    def _truncation_flag(self, normalized: str) -> PreFilterResult | None:
        if self._settings.flag_truncated and _TRAILING_HYPHEN.search(normalized):
            return PreFilterResult.flagged(REASON_LOOKS_TRUNCATED, confidence=0.5)
        return None


def _longest_letter_run(text: str) -> int:
    """Longest run of one repeated letter.

    Digits and punctuation are excluded: a year, a price or an excited row of
    exclamation marks is not a structural problem.
    """
    longest = current = 0
    previous = ""
    for char in text:
        if not char.isalpha():
            current, previous = 0, ""
            continue
        current = current + 1 if char == previous else 1
        previous = char
        longest = max(longest, current)
    return longest


def _repeated_pattern(
    text: str, min_repeats: int, min_coverage: float
) -> tuple[int, float] | None:
    """First immediately-repeating unit that meets *both* thresholds.

    Judging only the single best match lets a short, heavily repeated fragment
    mask a larger one: in "hahahahahaha asdfasdfasdfasdfasdf" the `ha` run wins
    on repeats but fails on coverage, and the `asdf` block is never considered.
    """
    for match in _REPEATED_PATTERN.finditer(text):
        unit = len(match.group(1))
        repeats = len(match.group(0)) // unit
        coverage = len(match.group(0)) / len(text)
        if repeats >= min_repeats and coverage >= min_coverage:
            return repeats, coverage
    return None
