"""Shared test data. Plain helpers, not tests, so test modules never import
each other."""

from __future__ import annotations

import functools
import io
import random

from PIL import Image

CASTLE = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w porannym słońcu"
#: One typo away: the cheapest way to dodge exact matching.
CASTLE_TYPO = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w poranym słońcu"
#: Diacritics stripped. Measured at 13 bits, above the same-user bar, so it is
#: deliberately left to the judge rather than risking honest rewordings.
CASTLE_NO_DIACRITICS = "Zamek w Malborku widziany od strony Nogatu, ceglane mury w porannym sloncu"
PIEROGI = "Pierogi ruskie"


@functools.lru_cache(maxsize=None)
def photo(seed: int, width: int = 320, height: int = 240, quality: int = 92) -> bytes:
    """A textured JPEG; `seed` changes its content.

    Deliberately noisy rather than a smooth gradient: a gradient survives any
    edit with an identical hash, which would make the tests far too kind. Noise
    is the opposite extreme (see #27 on calibrating against real photos).
    """
    rng = random.Random(seed)
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def recompressed(data: bytes, quality: int = 40, scale: float = 0.5) -> bytes:
    """The same photo re-saved smaller and at lower quality, as a re-upload would be."""
    with Image.open(io.BytesIO(data)) as image:
        resized = image.resize((int(image.width * scale), int(image.height * scale)))
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


@functools.lru_cache(maxsize=None)
def phone_photo(seed: int = 1, width: int = 4032, height: int = 3024) -> bytes:
    """A 12 MP JPEG at phone resolution, for realistic latency figures.

    Smooth blocks rather than noise, so it compresses to a phone-like file size
    (a few MB) instead of the tens of MB pure noise would produce.
    """
    rng = random.Random(seed)
    small = Image.frombytes("RGB", (64, 48), rng.randbytes(64 * 48 * 3))
    image = small.resize((width, height), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()
