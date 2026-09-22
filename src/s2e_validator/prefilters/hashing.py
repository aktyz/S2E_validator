"""Fingerprints a submission is recognised by.

Exact identity (SHA-256) for #15; near-identity (dHash, SimHash) for #16. All are
deterministic and local: no model, no remote call.

Every function here is bounded on its own. A filter upstream may be running in
shadow mode, which never stops a submission, so nothing may assume an earlier
filter has already refused an oversized photo or a multi-megabyte caption.
"""

from __future__ import annotations

import hashlib
import io
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from enum import Enum

from PIL import Image

from .interaction import ExistingImage, ImageInput, NewImage
from .text import canonical

HASH_BITS = 64
#: Same bound as ImageFormatSettings.max_pixels: phone cameras top out near
#: 50 MP, and anything larger is not decoded here either.
DEFAULT_MAX_PIXELS = 50_000_000
#: Descriptions are capped at 2000 characters by text_format; fingerprinting
#: more than that would only let an unbounded string cost unbounded time.
MAX_FINGERPRINT_CHARS = 2000
#: Raw text read before canonicalising. Generous, because removing invisible
#: characters can shrink the text, but bounded.
_MAX_RAW_CHARS = MAX_FINGERPRINT_CHARS * 4
_CACHE_SIZE = 256


class FingerprintRefused(ValueError):
    """The input is too large, or not a photo at all, to fingerprint safely."""


class TextNotComparable(FingerprintRefused):
    """The caption is too long to compare whole.

    Comparing only a prefix is not safe: invisible characters in front of a
    copied caption, or a unique preamble pushing it past the limit, would leave
    nothing of the copy inside the prefix.
    """


class _DigestCache:
    """Memoises fingerprints by a digest of the input, never the input itself.

    Keyed on content, so check() and commit() for one submission hash once and
    a resubmission with a different photo under the same id gets a fresh
    answer. Keyed on a digest rather than the bytes, so the cache holds a few
    kilobytes rather than up to 256 photos of 25 MiB each.
    """

    def __init__(self, size: int = _CACHE_SIZE):
        self._size = size
        self._values: OrderedDict[Hashable, int] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, compute: Callable[[], int]) -> int:
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key]
        value = compute()  # outside the lock: decoding must not serialise workers
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self._size:
                self._values.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_DHASH_CACHE = _DigestCache()
_SIMHASH_CACHE = _DigestCache()


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


def hamming(left: int, right: int) -> int:
    """Number of differing bits: how far apart two fingerprints are."""
    return (left ^ right).bit_count()


def dhash(data: bytes, size: int = 8, max_pixels: int = DEFAULT_MAX_PIXELS) -> int:
    """Perceptual hash of a photo: `size * size` bits of brightness gradients.

    Each bit says "is this pixel brighter than the one to its right" on a tiny
    greyscale version of the photo. Resizing, recompressing or lightly editing
    barely moves those comparisons, so the hash stays close; a different photo
    lands far away. Heavy crops and rotations do defeat it; that gap is
    accepted for v1 and left to the judge.

    Raises FingerprintRefused rather than decoding a photo that is too large or
    not a photo at all.
    """
    key = (hashlib.sha256(data).digest(), size, max_pixels)
    return _DHASH_CACHE.get(key, lambda: _dhash(data, size, max_pixels))


def _dhash(data: bytes, size: int, max_pixels: int) -> int:
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > max_pixels:
                raise FingerprintRefused(
                    f"{width}x{height} exceeds {max_pixels} pixels"
                )
            # For JPEG (and MPO) this asks the decoder itself to downscale by up
            # to 8x while decoding, so a 12 MP photo never exists in memory at
            # full size. Other formats ignore it.
            image.draft("L", (size * 8, size * 8))
            # size+1 columns so each row yields `size` left-to-right comparisons.
            small = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            # One byte per pixel in "L" mode, so the raw buffer indexes directly.
            pixels = small.tobytes()
    except FingerprintRefused:
        raise
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow's decoders raise widely
        raise FingerprintRefused(f"{type(exc).__name__}: {exc}") from exc

    bits = 0
    for row in range(size):
        offset = row * (size + 1)
        for column in range(size):
            bits = (bits << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    return bits


def simhash(text: str, shingle_size: int = 4, bits: int = HASH_BITS) -> int:
    """Similarity hash of a description, over overlapping character shingles.

    Character shingles rather than words: Polish inflection changes endings
    ("zamek" / "zamku"), and short captions have too few words for word shingles
    to say anything. Light edits change few shingles, so the hash moves a few
    bits; unrelated text moves about half of them.

    Built on the canonical form, so zero-width characters and decomposed letters
    cannot disguise a copy. Raises TextNotComparable for a caption too long to
    compare whole.
    """
    prepared = fingerprint_text(text)
    key = (hashlib.sha256(prepared.encode("utf-8")).digest(), shingle_size, bits)
    return _SIMHASH_CACHE.get(key, lambda: _simhash(prepared, shingle_size, bits))


def fingerprint_text(text: str) -> str:
    """The whole caption in canonical form, or TextNotComparable.

    Never a prefix. The raw bound keeps canonicalising itself bounded; the
    canonical bound is the real limit on what is compared.
    """
    if len(text) > _MAX_RAW_CHARS:
        raise TextNotComparable(f"{len(text)} raw characters exceeds {_MAX_RAW_CHARS}")
    prepared = canonical(text)
    if len(prepared) > MAX_FINGERPRINT_CHARS:
        raise TextNotComparable(
            f"{len(prepared)} visible characters exceeds {MAX_FINGERPRINT_CHARS}"
        )
    return prepared


def _simhash(prepared: str, shingle_size: int, bits: int) -> int:
    shingles = Counter(
        prepared[i : i + shingle_size] for i in range(max(len(prepared) - shingle_size + 1, 0))
    )
    if not shingles:
        return 0

    columns = [0] * bits
    for shingle, weight in shingles.items():
        digest = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=bits // 8).digest(), "big"
        )
        for index in range(bits):
            columns[index] += weight if (digest >> index) & 1 else -weight

    value = 0
    for index in range(bits):
        if columns[index] > 0:
            value |= 1 << index
    return value


def text_contribution_key(text: str) -> str:
    """Identity of a *text-only* contribution, for settlement.

    When someone describes a photo an earlier submitter owns, the photo is not
    theirs to be paid for; the description is. Keying such a contribution by the
    photo hash would collide with the original and block both. Built on the
    canonical form, so an invisible edit is still the same contribution.

    A caption too long to compare is keyed on its raw bytes instead: still
    unique and deterministic, just not normalised.
    """
    try:
        prepared = fingerprint_text(text)
    except TextNotComparable:
        return f"text-raw:{hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()}"
    return f"text:{hashlib.sha256(prepared.encode('utf-8')).hexdigest()}"
