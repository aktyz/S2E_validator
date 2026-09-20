"""Image identity: the exact-match key a photo is recognised by.

The same value doubles as the `uniqueness_key` reported to the settlement layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from .interaction import ExistingImage, ImageInput, NewImage


class ImageKind(str, Enum):
    """UPLOAD    - the contributor supplied the photo with this submission.
    REFERENCE - the contributor described a photo already in the pool.
    """

    UPLOAD = "upload"
    REFERENCE = "reference"


@dataclass(frozen=True)
class ImageIdentity:
    key: str
    kind: ImageKind

    @property
    def is_upload(self) -> bool:
        return self.kind is ImageKind.UPLOAD


def image_identity(image: ImageInput) -> ImageIdentity:
    """Identify a photo exactly.

    Uploads hash their raw file bytes: any re-encode, resize or recompression
    changes the hash, so this catches byte-identical repeats only. Perceptual
    near-duplicates are #16's job.

    References identify the pooled photo instead, so "this user already described
    this photo" is answerable without holding the bytes.
    """
    if isinstance(image, NewImage):
        return ImageIdentity(hashlib.sha256(image.data).hexdigest(), ImageKind.UPLOAD)
    if isinstance(image, ExistingImage):
        return ImageIdentity(f"ref:{image.image_id}", ImageKind.REFERENCE)
    raise TypeError(f"Unsupported image input: {type(image).__name__}")


def text_contribution_key(text: str) -> str:
    """Identity of a *text-only* contribution, for settlement.

    When someone describes a photo an earlier submitter owns, the photo is not
    theirs to be paid for — the description is. Keying such a contribution by the
    photo hash would collide with the original and block both.
    """
    normalized = " ".join(text.split()).casefold()
    return f"text:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
