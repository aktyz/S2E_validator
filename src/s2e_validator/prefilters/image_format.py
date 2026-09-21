"""Structural checks on the uploaded photo."""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from .base import PreFilter
from .interaction import Interaction, NewImage
from .result import PreFilterResult
from .settings import require_int, require_str_sequence, settings_from_dict

REASON_UNDECODABLE = "IMAGE_UNDECODABLE"
REASON_FORMAT_NOT_ALLOWED = "IMAGE_FORMAT_NOT_ALLOWED"
REASON_RESOLUTION_TOO_LOW = "IMAGE_RESOLUTION_TOO_LOW"
REASON_FILE_TOO_SMALL = "IMAGE_FILE_TOO_SMALL"
REASON_FILE_TOO_LARGE = "IMAGE_FILE_TOO_LARGE"
REASON_DECOMPRESSION_BOMB = "IMAGE_DECOMPRESSION_BOMB"

KIB = 1024
MIB = 1024 * KIB


@dataclass(frozen=True)
class ImageFormatSettings:
    """Bounds sized for phone photos, which is what the campaign collects."""

    allowed_formats: Sequence[str] = field(default_factory=lambda: ("JPEG", "PNG", "WEBP"))
    min_width: int = 256
    min_height: int = 256
    min_file_bytes: int = 4 * KIB
    max_file_bytes: int = 25 * MIB
    #: Decoded size, checked before any pixels are read. Phone cameras top out
    #: around 50 MP, while a small, highly compressible file can claim far more.
    #: Pillow's own guard is not enough on its own: it only warns between 1x and
    #: 2x MAX_IMAGE_PIXELS, and that global can be changed by any other filter.
    max_pixels: int = 50_000_000

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ImageFormatSettings:
        parsed = settings_from_dict(cls, raw)
        formats = require_str_sequence(parsed.allowed_formats, "allowed_formats")
        if not formats:
            raise ValueError("allowed_formats must not be empty")
        require_int(parsed.min_width, "min_width", minimum=1)
        require_int(parsed.min_height, "min_height", minimum=1)
        require_int(parsed.min_file_bytes, "min_file_bytes", minimum=0)
        require_int(parsed.max_file_bytes, "max_file_bytes", minimum=parsed.min_file_bytes)
        require_int(parsed.max_pixels, "max_pixels", minimum=1)
        return parsed


class ImageFormatFilter(PreFilter):
    """Rejects photos that cannot be decoded or are too small, large or odd.

    Submissions describing a photo already in the pool carry no bytes, so this
    filter passes them through: the pooled photo was checked when it arrived.
    """

    name = "image_format"
    cost_rank = 12

    def __init__(self, settings: ImageFormatSettings | None = None):
        self._settings = settings or ImageFormatSettings()
        self._allowed = {fmt.upper() for fmt in self._settings.allowed_formats}

    def check(self, interaction: Interaction) -> PreFilterResult:
        image = interaction.image
        if not isinstance(image, NewImage):
            return PreFilterResult.passed()

        size_failure = self._file_size_failure(len(image.data))
        if size_failure is not None:
            return size_failure

        try:
            # open() is lazy: format and dimensions are known before any pixel
            # is read, so everything cheap is decided first and only a photo
            # that passes is handed to a decoder.
            with Image.open(io.BytesIO(image.data)) as decoded:
                image_format = (decoded.format or "").upper()
                width, height = decoded.size

                for failure in (
                    self._format_failure(image_format),
                    self._resolution_failure(width, height),
                    self._pixel_failure(width, height),
                ):
                    if failure is not None:
                        return failure

                # load() actually decodes the pixels. verify() only walks the
                # structure and lets a truncated or corrupt JPEG through, which
                # would then fail inside perceptual hashing (#16) or face
                # detection (#21) instead of here.
                decoded.load()
        except Image.DecompressionBombError as exc:
            # Backstop: max_pixels above should have caught this first.
            return PreFilterResult.rejected(
                REASON_DECOMPRESSION_BOMB, confidence=1.0, error=str(exc)
            )
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - Pillow's decoders raise widely
            # struct.error, zlib.error and friends all mean the same thing here:
            # these bytes are not a usable photo. Letting them escape would make
            # the pipeline fail open and send a broken file to the judge.
            return PreFilterResult.rejected(
                REASON_UNDECODABLE, confidence=1.0, error=f"{type(exc).__name__}: {exc}"
            )

        return PreFilterResult.passed()

    def _file_size_failure(self, size: int) -> PreFilterResult | None:
        settings = self._settings
        if size < settings.min_file_bytes:
            return PreFilterResult.rejected(
                REASON_FILE_TOO_SMALL,
                confidence=1.0,
                bytes=size,
                minimum=settings.min_file_bytes,
            )
        if size > settings.max_file_bytes:
            return PreFilterResult.rejected(
                REASON_FILE_TOO_LARGE,
                confidence=1.0,
                bytes=size,
                maximum=settings.max_file_bytes,
            )
        return None

    def _format_failure(self, image_format: str) -> PreFilterResult | None:
        if image_format not in self._allowed:
            return PreFilterResult.rejected(
                REASON_FORMAT_NOT_ALLOWED,
                confidence=1.0,
                format=image_format,
                allowed=sorted(self._allowed),
            )
        return None

    def _resolution_failure(self, width: int, height: int) -> PreFilterResult | None:
        settings = self._settings
        if width < settings.min_width or height < settings.min_height:
            return PreFilterResult.rejected(
                REASON_RESOLUTION_TOO_LOW,
                confidence=1.0,
                width=width,
                height=height,
                minimum=[settings.min_width, settings.min_height],
            )
        return None

    def _pixel_failure(self, width: int, height: int) -> PreFilterResult | None:
        pixels = width * height
        if pixels > self._settings.max_pixels:
            return PreFilterResult.rejected(
                REASON_DECOMPRESSION_BOMB,
                confidence=1.0,
                pixels=pixels,
                maximum=self._settings.max_pixels,
            )
        return None
