import functools
import io
import random

import pytest
from PIL import Image

from s2e_validator.prefilters import (
    ExistingImage,
    ImageFormatFilter,
    ImageFormatSettings,
    NewImage,
    Verdict,
)
from s2e_validator.prefilters.image_format import (
    KIB,
    REASON_DECOMPRESSION_BOMB,
    REASON_FILE_TOO_LARGE,
    REASON_FILE_TOO_SMALL,
    REASON_FORMAT_NOT_ALLOWED,
    REASON_RESOLUTION_TOO_LOW,
    REASON_UNDECODABLE,
)

from conftest import make_interaction


@functools.lru_cache(maxsize=None)
def photo_bytes(width: int = 640, height: int = 480, image_format: str = "JPEG") -> bytes:
    """A noisy image, so it compresses to a realistic file size.

    randbytes runs at C speed and the result is cached: the naive generator was
    ~920k Python iterations per call, across twenty-odd calls.
    """
    rng = random.Random(f"{width}x{height}{image_format}")
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def check(image, settings: ImageFormatSettings | None = None):
    return ImageFormatFilter(settings).check(make_interaction(image=image))


def reason(image, settings: ImageFormatSettings | None = None) -> str | None:
    return check(image, settings).reason_code


class TestValidPhotos:
    @pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
    def test_allowed_formats_pass(self, image_format):
        image = NewImage(photo_bytes(image_format=image_format), f"image/{image_format.lower()}")
        assert check(image).verdict is Verdict.PASS

    def test_minimum_resolution_boundary_passes(self):
        assert check(NewImage(photo_bytes(256, 256))).verdict is Verdict.PASS

    def test_pooled_photo_reference_is_not_checked(self):
        """There are no bytes to check; the photo was vetted when it arrived."""
        assert check(ExistingImage("img-42")).verdict is Verdict.PASS


class TestDecoding:
    def test_bytes_that_are_not_an_image(self):
        assert reason(NewImage(b"this is a text file, not a photo" * 500)) == REASON_UNDECODABLE

    def test_truncated_photo(self):
        data = photo_bytes()
        assert reason(NewImage(data[: len(data) // 2])) == REASON_UNDECODABLE

    def test_corrupt_but_decodable_photo_passes(self):
        """Documented limitation: garbage pixels still decode.

        A JPEG whose scan data is overwritten decodes to a meaningless image
        rather than raising. Structure is all this filter can judge; whether the
        picture shows anything is the judge's call.
        """
        data = bytearray(photo_bytes())
        data[len(data) // 2 :] = b"\x00" * (len(data) - len(data) // 2)
        assert check(NewImage(bytes(data))).verdict is Verdict.PASS

    def test_a_photo_larger_than_max_pixels_is_rejected(self):
        """Our own pixel bound, checked before a single pixel is decoded."""
        settings = ImageFormatSettings(max_pixels=1000)
        result = check(NewImage(photo_bytes()), settings)

        assert result.reason_code == REASON_DECOMPRESSION_BOMB
        assert result.details["pixels"] == 640 * 480

    def test_the_window_pillow_only_warns_about_is_still_rejected(self, monkeypatch):
        """Pillow warns between 1x and 2x MAX_IMAGE_PIXELS and only errors above
        2x, so a file in that window would otherwise be decoded in full. Our own
        bound does not depend on that global, which any other filter can change.
        """
        pixels = 640 * 480
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", int(pixels * 0.75))
        settings = ImageFormatSettings(max_pixels=pixels - 1)

        # Pillow warns rather than raising, which is the whole problem.
        with pytest.warns(Image.DecompressionBombWarning):
            result = check(NewImage(photo_bytes()), settings)

        assert result.reason_code == REASON_DECOMPRESSION_BOMB
        assert result.details["pixels"] == pixels  # ours, not Pillow's

    def test_pillow_decompression_bomb_error_is_still_caught(self, monkeypatch):
        """Backstop for anything our own bound misses."""
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
        settings = ImageFormatSettings(max_pixels=10**9)

        assert reason(NewImage(photo_bytes()), settings) == REASON_DECOMPRESSION_BOMB


class TestCheapChecksFirst:
    """Format and size are known from the lazy open(), so a photo is never
    decoded just to be rejected for something already visible.

    A truncated file proves the order: decoding it fails, so if load() ran
    first the reason would be IMAGE_UNDECODABLE instead.
    """

    def test_a_disallowed_format_is_rejected_before_decoding(self):
        data = photo_bytes(image_format="GIF")

        assert reason(NewImage(data[: len(data) // 2])) == REASON_FORMAT_NOT_ALLOWED

    def test_a_small_photo_is_rejected_before_decoding(self):
        data = photo_bytes(120, 120)

        assert reason(NewImage(data[: len(data) // 2])) == REASON_RESOLUTION_TOO_LOW


class TestFormat:
    def test_disallowed_format_is_rejected(self):
        result = check(NewImage(photo_bytes(image_format="GIF")))
        assert result.reason_code == REASON_FORMAT_NOT_ALLOWED
        assert result.details["format"] == "GIF"

    def test_allowed_formats_are_configurable(self):
        settings = ImageFormatSettings(allowed_formats=("GIF",))
        assert check(NewImage(photo_bytes(image_format="GIF")), settings).verdict is Verdict.PASS
        assert reason(NewImage(photo_bytes()), settings) == REASON_FORMAT_NOT_ALLOWED


class TestResolution:
    def test_photo_below_minimum_resolution(self):
        result = check(NewImage(photo_bytes(120, 120)))
        assert result.reason_code == REASON_RESOLUTION_TOO_LOW
        assert (result.details["width"], result.details["height"]) == (120, 120)

    def test_minimum_is_configurable(self):
        settings = ImageFormatSettings(min_width=64, min_height=64)
        assert check(NewImage(photo_bytes(120, 120)), settings).verdict is Verdict.PASS


class TestFileSize:
    def test_file_too_small(self):
        assert reason(NewImage(b"\xff\xd8\xff")) == REASON_FILE_TOO_SMALL

    def test_file_too_large(self):
        settings = ImageFormatSettings(min_file_bytes=0, max_file_bytes=2 * KIB)
        assert reason(NewImage(photo_bytes()), settings) == REASON_FILE_TOO_LARGE

    def test_size_is_checked_before_decoding(self):
        """Cheap guard first: an oversized file is never handed to the decoder."""
        settings = ImageFormatSettings(min_file_bytes=0, max_file_bytes=10)
        assert reason(NewImage(b"not an image at all"), settings) == REASON_FILE_TOO_LARGE


class TestSettings:
    def test_from_dict_applies_values(self):
        settings = ImageFormatSettings.from_dict({"min_width": 1024, "min_height": 1024})
        assert reason(NewImage(photo_bytes()), settings) == REASON_RESOLUTION_TOO_LOW

    def test_from_dict_rejects_unknown_keys(self):
        with pytest.raises(ValueError, match="min_width"):
            ImageFormatSettings.from_dict({"minimum_width": 1024})

    def test_from_dict_rejects_inverted_size_bounds(self):
        with pytest.raises(ValueError, match="max_file_bytes must be at least"):
            ImageFormatSettings.from_dict({"min_file_bytes": 100, "max_file_bytes": 10})

    def test_from_dict_rejects_empty_format_list(self):
        with pytest.raises(ValueError, match="allowed_formats"):
            ImageFormatSettings.from_dict({"allowed_formats": []})

    def test_a_bare_string_is_not_a_format_list(self):
        """"JPEG" is a Sequence[str]; without a check it becomes {J,P,E,G} and
        every photo is rejected."""
        with pytest.raises(ValueError, match="list of strings"):
            ImageFormatSettings.from_dict({"allowed_formats": "JPEG"})

    @pytest.mark.parametrize(
        "raw, message",
        [
            ({"allowed_formats": ["JPEG", 5]}, "only strings"),
            ({"min_width": "256"}, "whole number"),
            ({"min_width": True}, "whole number"),
            ({"min_width": 0}, "at least 1"),
            ({"max_pixels": 0}, "at least 1"),
        ],
    )
    def test_bad_values_fail_at_load_not_at_runtime(self, raw, message):
        with pytest.raises(ValueError, match=message):
            ImageFormatSettings.from_dict(raw)
