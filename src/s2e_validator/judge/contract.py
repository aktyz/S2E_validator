"""What goes into the judge and what comes out.

The one rule that shapes everything here: **a judge failure is never a score.**
A reply that is missing, malformed or off-vocabulary becomes HELD, which pays
nothing and goes to a person. Defaulting it to 0 would silently punish a
contributor for our outage; defaulting it to anything else would pay for a
submission nobody judged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..prefilters.interaction import Interaction
from .rubric import RATING_MAX, RATING_MIN, Rubric

HOLD_JUDGE_UNAVAILABLE = "JUDGE_UNAVAILABLE"
HOLD_REPLY_INVALID = "JUDGE_REPLY_INVALID"
HOLD_IMAGE_UNAVAILABLE = "IMAGE_UNAVAILABLE"

# A model may wrap its JSON in one Markdown fence; nothing else is tolerated.
_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)
_REPLY_KEYS = {"criteria", "reason_codes", "confidence"}


class JudgeStatus(str, Enum):
    SCORED = "SCORED"
    HELD = "HELD"


@dataclass(frozen=True)
class JudgeInput:
    """One submission that passed Stage 1, with the flags Stage 1 attached.

    `flags` are the Stage 1 FLAG reason codes, e.g. `DUPLICATE_IMAGE_TEXT_ONLY`.
    They travel to the verdict so the score-to-Q mapping (#43, M1) can account
    for them; they are deliberately *not* shown to the model, so the quality of a
    description is judged on the description.
    """

    interaction: Interaction
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provenance:
    """Which judge produced a verdict, so any payout can be traced to the exact
    model, prompt and rubric behind it."""

    model: str
    prompt_version: str
    rubric_version: str


@dataclass(frozen=True)
class JudgeVerdict:
    submission_id: str
    status: JudgeStatus
    provenance: Provenance
    quality_score: int | None = None
    is_rewardable: bool = False
    criteria: Mapping[str, int] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    confidence: float | None = None
    flags: tuple[str, ...] = ()
    hold_reason: str | None = None
    attempts: int = 1

    def __post_init__(self) -> None:
        if self.status is JudgeStatus.SCORED:
            if self.quality_score is None or not 0 <= self.quality_score <= 100:
                raise ValueError("a SCORED verdict needs a quality_score from 0 to 100")
            if self.hold_reason is not None:
                raise ValueError("a SCORED verdict must not carry a hold_reason")
        else:
            if self.quality_score is not None or self.is_rewardable:
                raise ValueError("a HELD verdict has no score and is never rewardable")
            if not self.hold_reason:
                raise ValueError("a HELD verdict must say why")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    @classmethod
    def held(
        cls,
        item: JudgeInput,
        provenance: Provenance,
        reason: str,
        attempts: int,
    ) -> JudgeVerdict:
        return cls(
            submission_id=item.interaction.submission_id,
            status=JudgeStatus.HELD,
            provenance=provenance,
            flags=item.flags,
            hold_reason=reason,
            attempts=attempts,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, for the API and the audit log."""
        return {
            "submission_id": self.submission_id,
            "status": self.status.value,
            "quality_score": self.quality_score,
            "is_rewardable": self.is_rewardable,
            "criteria": dict(self.criteria),
            "reason_codes": list(self.reason_codes),
            "confidence": self.confidence,
            "flags": list(self.flags),
            "hold_reason": self.hold_reason,
            "attempts": self.attempts,
            "judge": {
                "model": self.provenance.model,
                "prompt_version": self.provenance.prompt_version,
                "rubric_version": self.provenance.rubric_version,
            },
        }


@dataclass(frozen=True)
class JudgeReply:
    """A model reply that passed validation."""

    criteria: Mapping[str, int]
    reason_codes: tuple[str, ...]
    confidence: float | None


class JudgeReplyInvalid(ValueError):
    """The model answered, but not in the agreed shape."""


def parse_reply(raw: Any, rubric: Rubric) -> JudgeReply:
    """Validate a model's reply strictly against the rubric.

    Strict on purpose: every criterion rated, nothing extra, whole numbers in
    range, reason codes from the rubric's vocabulary only. Anything looser lets
    a confused or manipulated reply turn into a payment.

    Total: whatever is wrong with a reply, the only thing raised is
    `JudgeReplyInvalid`. The description partly steers what the model writes,
    so a reply built to crash the parser has to end as a hold like any other.
    """
    if not isinstance(raw, str):
        # Some provider SDKs return None when the output was filtered.
        raise JudgeReplyInvalid(f"reply must be text, got {type(raw).__name__}")
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError and the plain ValueError for an
        # integer too long to convert; deep nesting raises RecursionError.
        raise JudgeReplyInvalid(f"not JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise JudgeReplyInvalid("reply must be a JSON object")
    extra = set(data) - _REPLY_KEYS
    if extra:
        raise JudgeReplyInvalid(f"unexpected keys: {sorted(extra)}")

    ratings = data.get("criteria")
    if not isinstance(ratings, dict):
        raise JudgeReplyInvalid("criteria must be an object")
    expected = set(rubric.criterion_names)
    if set(ratings) != expected:
        missing = sorted(expected - set(ratings))
        surplus = sorted(set(ratings) - expected)
        raise JudgeReplyInvalid(f"criteria mismatch: missing {missing}, unexpected {surplus}")
    for name, rating in ratings.items():
        # bool is an int in Python; `true` as a rating is always a malformed reply.
        if isinstance(rating, bool) or not isinstance(rating, int):
            raise JudgeReplyInvalid(f"{name} must be a whole number, got {rating!r}")
        if not RATING_MIN <= rating <= RATING_MAX:
            raise JudgeReplyInvalid(f"{name} must be {RATING_MIN}..{RATING_MAX}, got {rating}")

    codes = data.get("reason_codes", [])
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise JudgeReplyInvalid("reason_codes must be a list of strings")
    unknown = sorted(set(codes) - set(rubric.reasons))
    if unknown:
        raise JudgeReplyInvalid(f"reason codes outside the rubric: {unknown}")

    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise JudgeReplyInvalid("confidence must be a number")
        if not 0.0 <= confidence <= 1.0:
            raise JudgeReplyInvalid("confidence must be between 0 and 1")
        confidence = float(confidence)

    # Order kept as the rubric declares it; duplicates dropped.
    return JudgeReply(
        criteria={name: ratings[name] for name in rubric.criterion_names},
        reason_codes=tuple(dict.fromkeys(codes)),
        confidence=confidence,
    )
