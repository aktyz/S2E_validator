"""The submission every pre-filter receives: an image + text pair plus metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class NewImage:
    """A photo uploaded together with this submission."""

    data: bytes
    mime_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("NewImage.data must be bytes")


@dataclass(frozen=True)
class ExistingImage:
    """A reference to a photo already in the campaign's pool.

    Used when a contributor describes a photo someone else uploaded.
    """

    image_id: str

    def __post_init__(self) -> None:
        if not self.image_id:
            raise ValueError("ExistingImage.image_id must not be empty")


ImageInput = NewImage | ExistingImage


@dataclass(frozen=True)
class Interaction:
    """One contributor submission, as seen by the pre-filter stage.

    `user_id` identifies the contributor (their wallet pubkey once wallets are
    wired in). `metadata` holds anything campaign-specific that has no field yet.
    """

    submission_id: str
    campaign_id: str
    user_id: str
    image: ImageInput
    text: str
    submitted_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("submission_id", "campaign_id", "user_id"):
            if not getattr(self, name):
                raise ValueError(f"Interaction.{name} must not be empty")
        if not isinstance(self.image, (NewImage, ExistingImage)):
            raise TypeError("Interaction.image must be NewImage or ExistingImage")
        if self.submitted_at.tzinfo is None:
            raise ValueError("Interaction.submitted_at must be timezone-aware")
