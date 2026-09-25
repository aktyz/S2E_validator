"""Near-duplicate detection: lightly-edited photos and paraphrased captions.

Deliberately no embeddings: a model call would break the epic's cost premise.
dHash and SimHash are deterministic, local and bounded.

Every rule is asked as its own question, strictest first ("does this user
already have something this close?" before "does anyone else?"), so a verdict
never depends on whose fingerprint happens to be nearest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .base import PreFilter
from .hashing import (
    DEFAULT_MAX_PIXELS,
    FingerprintRefused,
    TextNotComparable,
    dhash,
    fingerprint_text,
    image_identity,
    simhash,
)
from .interaction import Interaction, NewImage
from .result import PreFilterResult
from .settings import require_int, settings_from_dict
from .stores import DEFAULT_RETENTION, utc_now
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog
from .windows import GLOBAL_BUCKET, HashEntry, HashMatch, HashWindow, user_bucket

REASON_IMAGE_SAME_USER = "NEAR_DUPLICATE_IMAGE_SAME_USER"
REASON_IMAGE_TEXT_ONLY = "NEAR_DUPLICATE_IMAGE_TEXT_ONLY"
REASON_TEXT_TEMPLATE = "NEAR_DUPLICATE_TEXT_TEMPLATE"
REASON_TEXT_COPIED = "NEAR_DUPLICATE_TEXT_ACROSS_USERS"
REASON_TEXT_UNCOMPARABLE = "NEAR_DUPLICATE_TEXT_UNCOMPARABLE"


def _parse_retention(prepared: dict[str, Any]) -> None:
    """Turn a config's `retention_days` into a timedelta, checked first."""
    if "retention_days" in prepared:
        days = require_int(prepared.pop("retention_days"), "retention_days", minimum=1)
        prepared["retention"] = timedelta(days=days)


@dataclass(frozen=True)
class ImageNearDuplicateSettings:
    #: Bits of the 64-bit perceptual hash that may differ. Measured on synthetic
    #: photos only (see #27): resizing and recompressing moves a photo up to 9
    #: bits, so 10 catches re-uploads.
    max_distance: int = 10
    retention: timedelta = DEFAULT_RETENTION
    #: Photos above this are not fingerprinted at all; same bound as image_format.
    max_pixels: int = DEFAULT_MAX_PIXELS

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ImageNearDuplicateSettings:
        prepared = dict(raw)
        _parse_retention(prepared)
        parsed = settings_from_dict(cls, prepared)
        require_int(parsed.max_distance, "max_distance", minimum=0, maximum=64)
        require_int(parsed.max_pixels, "max_pixels", minimum=1)
        return parsed


@dataclass(frozen=True)
class TextNearDuplicateSettings:
    #: Same user, different photos: catches a template caption reused verbatim
    #: or lightly edited. Measured on a small synthetic corpus (see #27):
    #: punctuation-only edits move 2 bits, typos 5, reordering 9, a swapped word
    #: 11, stripped diacritics 13, while the one honest pair measured sat 16
    #: apart. 10 keeps a margin; edits above it are left to the judge, because
    #: a wrong reject costs a contributor real money.
    same_user_max_distance: int = 10
    #: Across users the bar is far tighter: only near-identical text counts,
    #: since two people describing one landmark write genuinely similar things.
    cross_user_max_distance: int = 4
    #: Short captions collide honestly ("Pierogi ruskie"), so cross-user
    #: comparison only applies above this length.
    min_cross_user_chars: int = 60
    #: Same idea for one contributor: "Pierogi ruskie" is a fair caption for two
    #: different plates, and SimHash on very short text is noisy either way. The
    #: campaign requires 100 characters, so this only matters for text that
    #: arrives another way.
    min_same_user_chars: int = 20
    shingle_size: int = 4
    retention: timedelta = DEFAULT_RETENTION

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TextNearDuplicateSettings:
        prepared = dict(raw)
        _parse_retention(prepared)
        parsed = settings_from_dict(cls, prepared)
        require_int(parsed.same_user_max_distance, "same_user_max_distance", minimum=0, maximum=64)
        require_int(
            parsed.cross_user_max_distance,
            "cross_user_max_distance",
            minimum=0,
            maximum=parsed.same_user_max_distance,
        )
        require_int(parsed.min_cross_user_chars, "min_cross_user_chars", minimum=0)
        require_int(parsed.min_same_user_chars, "min_same_user_chars", minimum=0)
        require_int(parsed.shingle_size, "shingle_size", minimum=2)
        return parsed


class ImageNearDuplicateFilter(PreFilter):
    """Same photo, re-encoded or lightly edited.

    Asked strictest first:
      1. Does this user already have a photo this close?  REJECT.
      2. Does another user own a photo this close?  Text-only FLAG, and the
         match goes to the suspicion log.

    Every committed photo enters its owner's bucket, text-only ones included,
    so repeated edits of someone else's photo are capped at one, as with exact
    duplicates. Only originals enter the global bucket: first claim wins.
    """

    name = "image_near_duplicate"
    cost_rank = 30

    def __init__(
        self,
        settings: ImageNearDuplicateSettings | None = None,
        window: HashWindow | None = None,
        suspicion_log: SuspicionLog | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._settings = settings or ImageNearDuplicateSettings()
        # `is not None`, never `or`: an empty window is falsy, and an injected
        # one (shared, durable, or under test) must not be silently replaced.
        self._window = window if window is not None else HashWindow(
            retention=self._settings.retention, clock=clock
        )
        self._suspicion_log = suspicion_log if suspicion_log is not None else InMemorySuspicionLog()
        self._clock = clock

    def check(self, interaction: Interaction) -> PreFilterResult:
        fingerprint = self._fingerprint(interaction)
        if isinstance(fingerprint, str):
            return PreFilterResult.passed(fingerprint="skipped", reason=fingerprint)
        if fingerprint is None:
            return PreFilterResult.passed()

        match, same_user = self._classify(interaction, fingerprint)
        if match is None:
            return PreFilterResult.passed()
        # Not logged here: `check` only reads. `commit` records every match that
        # survives the stage, so a submission rejected by a later filter leaves
        # no fraud record against the account.
        return self._verdict(match, same_user)

    def commit(self, interaction: Interaction) -> PreFilterResult | None:
        """Record the fingerprint, deciding and inserting as one step."""
        fingerprint = self._fingerprint(interaction)
        if not isinstance(fingerprint, int):
            return None

        entry = HashEntry(
            value=fingerprint,
            submission_id=interaction.submission_id,
            user_id=interaction.user_id,
            seen_at=self._clock(),
        )
        with self._window.locked():
            match, same_user = self._classify(interaction, fingerprint)
            if match is not None and same_user:
                return self._verdict(match, same_user)
            own = user_bucket(interaction.user_id)
            self._window.add(own, entry)
            if match is None:
                self._window.add(GLOBAL_BUCKET, entry)
                return None
            # Text-only on someone else's photo. The cap has to attach to *that
            # photo*, not to this user's edit of it: two edits can sit further
            # from each other than either sits from the original. So the
            # original's fingerprint goes into this user's bucket too, and any
            # later copy of it by this user is a same-user repeat.
            self._window.add(own, replace(entry, value=match.entry.value))

        self._log_match(interaction, match)
        return self._verdict(match, same_user)

    def rollback(self, interaction: Interaction) -> None:
        """Take this submission's fingerprints back out, and withdraw its match."""
        with self._window.locked():
            self._window.remove(user_bucket(interaction.user_id), interaction.submission_id)
            self._window.remove(GLOBAL_BUCKET, interaction.submission_id)
        if isinstance(interaction.image, NewImage):
            self._suspicion_log.retract(
                image_identity(interaction.image).key, interaction.submission_id
            )

    def _fingerprint(self, interaction: Interaction) -> int | str | None:
        """The photo's dHash, a reason it could not be taken, or None if there
        is no photo to take it from (a pooled-photo reference)."""
        if not isinstance(interaction.image, NewImage):
            return None
        try:
            return dhash(interaction.image.data, max_pixels=self._settings.max_pixels)
        except FingerprintRefused as refused:
            return str(refused)

    def _classify(self, interaction: Interaction, value: int) -> tuple[HashMatch | None, bool]:
        distance = self._settings.max_distance
        with self._window.locked():
            own = self._window.nearest(
                user_bucket(interaction.user_id),
                value,
                distance,
                exclude_submission_id=interaction.submission_id,
            )
            if own is not None:
                return own, True
            other = self._window.nearest(
                GLOBAL_BUCKET,
                value,
                distance,
                exclude_submission_id=interaction.submission_id,
                accept=lambda entry: entry.user_id != interaction.user_id,
            )
            return other, False

    @staticmethod
    def _verdict(match: HashMatch, same_user: bool) -> PreFilterResult:
        details = {
            "distance": match.distance,
            "first_submission_id": match.entry.submission_id,
            "first_seen_at": match.entry.seen_at.isoformat(),
        }
        if same_user:
            return PreFilterResult.rejected(REASON_IMAGE_SAME_USER, confidence=0.9, **details)
        return PreFilterResult.flagged(
            REASON_IMAGE_TEXT_ONLY, confidence=0.9, contribution="text_only", **details
        )

    def _log_match(self, interaction: Interaction, match: HashMatch) -> None:
        self._suspicion_log.record(
            DuplicateImageEvent(
                image_key=image_identity(interaction.image).key,
                campaign_id=interaction.campaign_id,
                original_submission_id=match.entry.submission_id,
                original_user_id=match.entry.user_id,
                duplicate_submission_id=interaction.submission_id,
                duplicate_user_id=interaction.user_id,
                occurred_at=self._clock(),
            )
        )


@dataclass(frozen=True)
class _PreparedCaption:
    """Everything about a caption that is expensive to compute, done once and
    outside the window's lock."""

    value: int
    image_key: str
    length: int


class TextNearDuplicateFilter(PreFilter):
    """Reused captions.

    Asked strictest first:
      1. Did this user write something this close for a *different* photo?
         Template farming, REJECT.
      2. Did anyone else write something near-identical? Copied, REJECT.

    Both apply only above a length floor, since short captions collide honestly.
    The same caption on the same photo is left to the image filters. A caption
    too long to compare whole is FLAGged for the judge rather than compared on
    a prefix, which padding could defeat.
    """

    name = "text_near_duplicate"
    cost_rank = 32

    def __init__(
        self,
        settings: TextNearDuplicateSettings | None = None,
        window: HashWindow | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._settings = settings or TextNearDuplicateSettings()
        self._window = window if window is not None else HashWindow(
            retention=self._settings.retention, clock=clock
        )
        self._clock = clock

    def check(self, interaction: Interaction) -> PreFilterResult:
        prepared = self._prepare(interaction)
        if isinstance(prepared, PreFilterResult):
            return prepared
        return self._decide(interaction, prepared) or PreFilterResult.passed()

    def commit(self, interaction: Interaction) -> PreFilterResult | None:
        """Record the caption, deciding and inserting as one step.

        Hashing happens first, outside the lock, so one worker's SimHash or
        SHA-256 never blocks another's commit; only the lookup and insert are
        serialised.
        """
        prepared = self._prepare(interaction)
        if isinstance(prepared, PreFilterResult):
            return None  # not comparable: nothing to record, check() flagged it
        entry = HashEntry(
            value=prepared.value,
            submission_id=interaction.submission_id,
            user_id=interaction.user_id,
            seen_at=self._clock(),
            image_key=prepared.image_key,
        )
        with self._window.locked():
            verdict = self._decide(interaction, prepared)
            if verdict is not None:
                return verdict
            self._window.add(user_bucket(interaction.user_id), entry)
            if prepared.length >= self._settings.min_cross_user_chars:
                self._window.add(GLOBAL_BUCKET, entry)
        return None

    def rollback(self, interaction: Interaction) -> None:
        """Take this caption's fingerprint back out of both buckets."""
        with self._window.locked():
            self._window.remove(user_bucket(interaction.user_id), interaction.submission_id)
            self._window.remove(GLOBAL_BUCKET, interaction.submission_id)

    def _prepare(self, interaction: Interaction) -> _PreparedCaption | PreFilterResult:
        """The expensive part, lock-free: fingerprint, photo key, length."""
        try:
            canonical_text = fingerprint_text(interaction.text)
        except TextNotComparable as refused:
            return PreFilterResult.flagged(
                REASON_TEXT_UNCOMPARABLE, confidence=0.5, reason=str(refused)
            )
        return _PreparedCaption(
            value=simhash(interaction.text, self._settings.shingle_size),
            # For an upload this is a SHA-256 over the whole photo.
            image_key=image_identity(interaction.image).key,
            length=len(canonical_text),
        )

    def _decide(
        self, interaction: Interaction, prepared: _PreparedCaption
    ) -> PreFilterResult | None:
        """The cheap part: two lookups, strictest first. Caller may hold the lock."""
        settings = self._settings
        with self._window.locked():
            if prepared.length >= settings.min_same_user_chars:
                own = self._window.nearest(
                    user_bucket(interaction.user_id),
                    prepared.value,
                    settings.same_user_max_distance,
                    exclude_submission_id=interaction.submission_id,
                    accept=lambda entry: entry.image_key != prepared.image_key,
                )
                if own is not None:
                    return PreFilterResult.rejected(
                        REASON_TEXT_TEMPLATE,
                        confidence=0.8,
                        distance=own.distance,
                        first_submission_id=own.entry.submission_id,
                    )

            if prepared.length >= settings.min_cross_user_chars:
                other = self._window.nearest(
                    GLOBAL_BUCKET,
                    prepared.value,
                    settings.cross_user_max_distance,
                    exclude_submission_id=interaction.submission_id,
                    accept=lambda entry: entry.user_id != interaction.user_id,
                )
                if other is not None:
                    return PreFilterResult.rejected(
                        REASON_TEXT_COPIED,
                        confidence=0.8,
                        distance=other.distance,
                        first_submission_id=other.entry.submission_id,
                    )
        return None
