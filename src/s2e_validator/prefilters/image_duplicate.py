"""Exact-duplicate image detection: the cheapest, highest-confidence farming check."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .base import PreFilter
from .hashing import ImageIdentity, image_identity, text_contribution_key
from .interaction import Interaction
from .result import PreFilterResult
from .stores import ImageKeyStore, InMemoryImageKeyStore, SeenImage, utc_now
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog

REASON_SAME_USER = "DUPLICATE_IMAGE_SAME_USER"
REASON_SAME_USER_REFERENCE = "DUPLICATE_IMAGE_REFERENCE"
REASON_TEXT_ONLY = "DUPLICATE_IMAGE_TEXT_ONLY"


def store_key(identity: ImageIdentity, user_id: str) -> str:
    """Scope an image key for lookup.

    Uploaded photos are keyed globally: the same file from a second contributor
    means only one of them owns it. References are keyed per user, because many
    contributors describing the same pooled photo is exactly what the campaign
    asks for — only the *same* user describing it twice is farming.
    """
    if identity.is_upload:
        return identity.key
    return user_scoped_key(identity, user_id)


def user_scoped_key(identity: ImageIdentity, user_id: str) -> str:
    """One contribution per user per photo, whoever owns the photo."""
    return f"{identity.key}|user:{user_id}"


class ImageExactDuplicateFilter(PreFilter):
    """Handles byte-identical photos and repeat descriptions of one photo.

    Same-user repeats are rejected outright. A *different* user's identical
    upload is not rejected: they are describing a photo someone else already
    owns, which is what an `ExistingImage` submission is, so the contribution is
    downgraded to text-only via a FLAG and the match is written to the suspicion
    log. That downgrade claims a per-user key, so text-only carries the same
    one-per-user-per-photo cap as a pooled-photo description: a second attempt at
    the same photo is a REJECT, not another priced contribution.

    `check` only reads. `commit` claims the keys once the whole pipeline has
    passed, so a submission rejected by a later filter does not lock its own
    photo out of a corrected resubmission.
    """

    name = "image_exact_duplicate"
    # Tier 20 (hashing) rather than 30 (stateful lookup): SHA-256 over the file
    # bytes dominates, and the store lookup behind it is a dict get.
    cost_rank = 20

    def __init__(
        self,
        store: ImageKeyStore | None = None,
        suspicion_log: SuspicionLog | None = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self._store = store if store is not None else InMemoryImageKeyStore()
        self._suspicion_log = suspicion_log if suspicion_log is not None else InMemorySuspicionLog()
        self._clock = clock

    def uniqueness_key(self, interaction: Interaction) -> str:
        """Identity of this submission's photo, for the settlement layer.

        A submission downgraded to text-only reports a different key — see the
        `uniqueness_key` in that result's details.
        """
        return image_identity(interaction.image).key

    def check(self, interaction: Interaction) -> PreFilterResult:
        identity = image_identity(interaction.image)

        own = self._store.find(user_scoped_key(identity, interaction.user_id))
        if own is not None and own.submission_id != interaction.submission_id:
            # This user already has a contribution against this photo, whether
            # they uploaded it or described someone else's.
            return self._rejected(self._repeat_reason(identity, interaction), identity, own)

        if not identity.is_upload:
            return PreFilterResult.passed()

        seen = self._store.find(identity.key)
        if seen is None or seen.submission_id == interaction.submission_id:
            # Unseen, or this very submission being re-validated after a retry.
            return PreFilterResult.passed()
        if seen.user_id == interaction.user_id:
            return self._rejected(REASON_SAME_USER, identity, seen)

        self._log_cross_user_match(interaction, identity, seen)
        return self._text_only(interaction, identity, seen)

    def commit(self, interaction: Interaction) -> PreFilterResult | None:
        """Claim this submission's keys. The first claim always wins.

        Returns a revised verdict when this submission lost a claim race: two
        uploads of one photo can both pass `check` before either commits, and
        the loser is the one that must be downgraded to text-only.
        """
        identity = image_identity(interaction.image)
        own_key = user_scoped_key(identity, interaction.user_id)

        # Claimed first, so a repeat by the same user is caught even when the
        # photo belongs to someone else.
        own_claim = self._store.add(self._entry(own_key, interaction))
        if not own_claim.claimed and own_claim.seen.submission_id != interaction.submission_id:
            return self._rejected(
                self._repeat_reason(identity, interaction), identity, own_claim.seen
            )

        if not identity.is_upload:
            return None

        claim = self._store.add(self._entry(identity.key, interaction))
        if claim.claimed or claim.seen.submission_id == interaction.submission_id:
            return None

        if claim.seen.user_id != interaction.user_id:
            self._log_cross_user_match(interaction, identity, claim.seen)
            return self._text_only(interaction, identity, claim.seen)
        # Same-user repeat that slipped past check(); report what check() would.
        return self._rejected(REASON_SAME_USER, identity, claim.seen)

    def _repeat_reason(self, identity: ImageIdentity, interaction: Interaction) -> str:
        """Why this user's second attempt at one photo is refused.

        Re-uploading your own photo and re-describing someone else's are both
        capped at one, but they are different behaviours and metrics and appeals
        should be able to tell them apart.
        """
        if identity.is_upload:
            owner = self._store.find(identity.key)
            if owner is not None and owner.user_id == interaction.user_id:
                return REASON_SAME_USER
        return REASON_SAME_USER_REFERENCE

    def _entry(self, key: str, interaction: Interaction) -> SeenImage:
        return SeenImage(
            key=key,
            submission_id=interaction.submission_id,
            user_id=interaction.user_id,
            seen_at=self._clock(),
        )

    @staticmethod
    def _rejected(reason: str, identity: ImageIdentity, seen: SeenImage) -> PreFilterResult:
        return PreFilterResult.rejected(
            reason,
            confidence=1.0,
            uniqueness_key=identity.key,
            first_submission_id=seen.submission_id,
            first_seen_at=seen.seen_at.isoformat(),
        )

    @staticmethod
    def _text_only(
        interaction: Interaction, identity: ImageIdentity, seen: SeenImage
    ) -> PreFilterResult:
        """The photo belongs to an earlier submitter; only the text is new."""
        return PreFilterResult.flagged(
            REASON_TEXT_ONLY,
            confidence=1.0,
            # Keyed on the text, not the photo: the photo's key belongs to the
            # original submitter, and settlement must not treat the two as one.
            uniqueness_key=text_contribution_key(interaction.text),
            photo_key=identity.key,
            contribution="text_only",
            first_submission_id=seen.submission_id,
            first_seen_at=seen.seen_at.isoformat(),
        )

    def _log_cross_user_match(
        self, interaction: Interaction, identity: ImageIdentity, original: SeenImage
    ) -> None:
        """Record the match. Safe to call repeatedly: the log is idempotent."""
        self._suspicion_log.record(
            DuplicateImageEvent(
                image_key=identity.key,
                campaign_id=interaction.campaign_id,
                original_submission_id=original.submission_id,
                original_user_id=original.user_id,
                duplicate_submission_id=interaction.submission_id,
                duplicate_user_id=interaction.user_id,
                occurred_at=self._clock(),
            )
        )
