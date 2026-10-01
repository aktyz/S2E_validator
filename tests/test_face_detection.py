"""Issue #21: faces are excluded by the campaign's own rules.

The positive case uses a *drawn* face. Real photographs are the only honest
validation, and they arrive with the Bielik data; see the README note.
"""

import io
import logging

import pytest
from PIL import Image, ImageDraw

from s2e_validator.prefilters import (
    ExistingImage,
    FaceAction,
    FaceBox,
    FaceDetectionFilter,
    FaceDetectionSettings,
    FilterMode,
    NewImage,
    PipelineConfig,
    PreflightFailed,
    Verdict,
    build_pipeline,
)
from s2e_validator.prefilters.face_detection import REASON_FACE_DETECTED, YuNetFaceDetector

from helpers import make_interaction, photo


def drawn_face(width: int = 320, height: int = 320) -> bytes:
    """A simple face the detector recognises, so tests need no photograph."""
    image = Image.new("RGB", (width, height), (235, 225, 215))
    draw = ImageDraw.Draw(image)
    draw.ellipse([70, 50, 250, 280], fill=(222, 184, 150), outline=(120, 90, 70))
    draw.ellipse([60, 40, 262, 120], fill=(60, 45, 35))
    draw.ellipse([110, 130, 140, 152], fill=(255, 255, 255), outline=(0, 0, 0))
    draw.ellipse([180, 130, 210, 152], fill=(255, 255, 255), outline=(0, 0, 0))
    draw.ellipse([120, 135, 132, 147], fill=(30, 30, 30))
    draw.ellipse([190, 135, 202, 147], fill=(30, 30, 30))
    draw.polygon([(160, 155), (150, 195), (172, 195)], fill=(200, 160, 130))
    draw.arc([130, 200, 195, 240], start=10, end=170, fill=(120, 60, 60), width=5)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def phone_jpeg() -> bytes:
    """A phone-sized JPEG: 4032x3024, flat colour so it stays small."""
    buffer = io.BytesIO()
    Image.new("RGB", (4032, 3024), (120, 130, 140)).save(buffer, format="JPEG")
    return buffer.getvalue()


class StubDetector:
    """Returns fixed boxes, for behaviour that shouldn't depend on the model."""

    def __init__(self, *boxes: FaceBox):
        self._boxes = boxes
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        return self._boxes


def check(image, settings=None, detector=None):
    return FaceDetectionFilter(settings, detector).check(make_interaction(image=image))


class TestRealDetector:
    def test_a_face_is_detected(self):
        result = check(NewImage(drawn_face()))

        assert result.verdict is Verdict.REJECT
        assert result.reason_code == REASON_FACE_DETECTED
        assert result.details["faces"] >= 1
        assert result.details["top_score"] >= 0.6

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_ordinary_photos_are_not_flagged(self, seed):
        """The textured photos used across the suite must stay clean."""
        assert check(NewImage(photo(seed, 400, 300))).verdict is Verdict.PASS

    def test_the_result_records_no_face_coordinates(self):
        """A privacy filter records that a face was found, never where."""
        result = check(NewImage(drawn_face()))

        assert set(result.details) == {"faces", "top_score", "largest_area_ratio"}

    def test_a_large_photo_is_downscaled_before_detection(self):
        result = check(NewImage(drawn_face(1600, 1600)))
        assert result.reason_code == REASON_FACE_DETECTED


class TestActions:
    FACE = FaceBox(10, 10, 80, 80, 0.95)

    def test_reject_is_the_default(self):
        result = check(NewImage(photo(1)), detector=StubDetector(self.FACE))
        assert result.verdict is Verdict.REJECT

    def test_flag_lets_the_judge_decide(self):
        settings = FaceDetectionSettings(action=FaceAction.FLAG)

        result = check(NewImage(photo(1)), settings, StubDetector(self.FACE))

        assert result.verdict is Verdict.FLAG
        assert result.reason_code == REASON_FACE_DETECTED

    def test_faces_below_the_score_threshold_are_ignored(self):
        settings = FaceDetectionSettings(score_threshold=0.9)
        faint = FaceBox(10, 10, 80, 80, 0.7)

        assert check(NewImage(photo(1)), settings, StubDetector(faint)).verdict is Verdict.PASS

    def test_faces_below_the_size_threshold_are_ignored(self):
        """A distant bystander need not sink an otherwise good photo."""
        settings = FaceDetectionSettings(min_area_ratio=0.05)
        tiny = FaceBox(5, 5, 8, 8, 0.95)

        assert check(NewImage(photo(1)), settings, StubDetector(tiny)).verdict is Verdict.PASS

    def test_the_largest_face_drives_the_reported_size(self):
        small = FaceBox(0, 0, 20, 20, 0.8)
        large = FaceBox(50, 50, 120, 120, 0.9)

        result = check(NewImage(photo(1, 400, 300)), detector=StubDetector(small, large))

        assert result.details["faces"] == 2
        assert result.details["largest_area_ratio"] == pytest.approx(
            (120 * 120) / (400 * 300), rel=0.01
        )


class TestSkippedSubmissions:
    def test_pooled_photo_references_are_not_scanned(self):
        detector = StubDetector(FaceBox(1, 1, 50, 50, 0.99))

        result = check(ExistingImage("img-1"), detector=detector)

        assert result.verdict is Verdict.PASS
        assert detector.calls == 0

    def test_an_oversized_photo_is_never_decoded(self):
        """image_format may be in shadow mode, so this filter bounds itself: a
        photo over max_pixels is refused from its header alone."""
        detector = StubDetector(FaceBox(1, 1, 50, 50, 0.99))
        buffer = io.BytesIO()
        # 1-bit mode keeps the test cheap: 60 MP, a few MB in memory, tiny as PNG.
        Image.new("1", (10_000, 6_000)).save(buffer, format="PNG")

        result = check(NewImage(buffer.getvalue()), detector=detector)

        assert result.verdict is Verdict.PASS
        assert result.details["face_check"] == "skipped"
        assert "10000x6000" in result.details["reason"]
        assert detector.calls == 0

    def test_the_detector_never_sees_more_than_max_side(self):
        seen = []

        class Recording(StubDetector):
            def detect(self, image):
                seen.append(image.size)
                return ()

        check(NewImage(phone_jpeg()), FaceDetectionSettings(max_side=640), Recording())

        assert max(seen[0]) <= 640

    def test_a_jpeg_is_decoded_small_from_the_start(self, monkeypatch):
        """draft() asks the JPEG decoder itself to downscale, so a 12 MP photo
        is never held at full size. The downscale afterwards would hide its
        absence, so this checks the request directly."""
        from PIL import JpegImagePlugin

        requested = []
        original = JpegImagePlugin.JpegImageFile.draft

        def recording_draft(self, mode, size):
            requested.append(size)
            return original(self, mode, size)

        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", recording_draft)

        check(NewImage(phone_jpeg()), FaceDetectionSettings(max_side=640), StubDetector())

        assert requested == [(640, 640)]

    def test_an_unreadable_photo_passes_here(self):
        """image_format owns undecodable photos; this filter must not double-reject."""
        detector = StubDetector(FaceBox(1, 1, 50, 50, 0.99))

        result = check(NewImage(b"not a photo"), detector=detector)

        assert result.verdict is Verdict.PASS
        assert detector.calls == 0


class TestSettings:
    def test_action_is_parsed_from_config(self):
        settings = FaceDetectionSettings.from_dict({"action": "flag"})
        assert settings.action is FaceAction.FLAG

    @pytest.mark.parametrize(
        "raw, message",
        [
            ({"score_threshold": 1.5}, "score_threshold"),
            ({"score_threshold": "0.6"}, "score_threshold must be a number"),
            ({"min_area_ratio": -0.1}, "min_area_ratio"),
            ({"max_side": 10}, "max_side"),
            ({"max_side": 512.5}, "max_side must be a whole number"),
            ({"max_pixels": 0}, "max_pixels"),
            ({"model_path": ""}, "model_path"),
            ({"actions": "flag"}, "action"),
        ],
    )
    def test_invalid_settings_are_rejected(self, raw, message):
        with pytest.raises(ValueError, match=message):
            FaceDetectionSettings.from_dict(raw)

    def test_an_unknown_action_is_rejected(self):
        with pytest.raises(ValueError, match="reject, flag"):
            FaceDetectionSettings.from_dict({"action": "blur"})


class TestMissingModel:
    def test_a_missing_model_file_fails_loudly_on_first_use(self, tmp_path):
        detector = YuNetFaceDetector(tmp_path / "absent.onnx")

        with pytest.raises(FileNotFoundError, match="models/README.md"):
            detector.detect(Image.new("RGB", (64, 64)))

    def test_building_the_filter_does_not_load_the_model(self, tmp_path):
        """One missing file must not stop the whole stage from starting."""
        FaceDetectionFilter(FaceDetectionSettings(model_path=str(tmp_path / "absent.onnx")))


class Recording:
    """A detector that remembers the size of every image it was shown."""

    def __init__(self):
        self.sizes = []

    def detect(self, image):
        self.sizes.append(image.size)
        return ()


def jpeg_with_orientation(width: int, height: int, orientation: int, image=None) -> bytes:
    """A JPEG stored as width x height, tagged to be displayed rotated."""
    image = image or Image.new("RGB", (width, height), (120, 130, 140))
    exif = Image.Exif()
    exif[0x0112] = orientation  # the EXIF Orientation tag
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class TestAwkwardPhotos:
    def test_a_very_thin_image_is_still_checked(self):
        """20000x1 fits max_pixels, but scaling it to max_side rounded its height
        to zero; resize raised, and the photo passed with no face check."""
        detector = Recording()
        buffer = io.BytesIO()
        Image.new("1", (20_000, 1)).save(buffer, format="PNG")

        result = check(NewImage(buffer.getvalue()), detector=detector)

        assert result.verdict is Verdict.PASS
        assert "face_check" not in result.details
        assert detector.sizes == [(1024, 1)]

    @pytest.mark.parametrize("orientation, shown", [(1, (300, 200)), (6, (200, 300)), (8, (200, 300))])
    def test_the_detector_sees_the_photo_upright(self, orientation, shown):
        """Phones store portrait shots sideways plus an orientation tag."""
        detector = Recording()

        check(NewImage(jpeg_with_orientation(300, 200, orientation)), detector=detector)

        assert detector.sizes == [shown]

    def test_a_sideways_stored_face_is_found(self):
        """The real detector, on a face stored rotated with a tag that says so."""
        face = Image.open(io.BytesIO(drawn_face())).convert("RGB")
        # Orientation 6 means "rotate 90 clockwise to display", so store the
        # face rotated the other way, as a phone sensor would.
        stored = face.transpose(Image.Transpose.ROTATE_90)

        result = check(NewImage(jpeg_with_orientation(*stored.size, 6, stored)))

        assert result.reason_code == REASON_FACE_DETECTED

    def test_a_malformed_orientation_tag_does_not_skip_the_check(self):
        """Pillow 12.2 tolerates this tag (it warns and carries on); kept as a
        guard in case a Pillow inside the pinned range is stricter."""
        buffer = io.BytesIO()
        Image.new("RGB", (300, 200), (120, 130, 140)).save(
            buffer, format="JPEG", exif=b"Exif\x00\x00not a TIFF header"
        )
        detector = Recording()

        check(NewImage(buffer.getvalue()), detector=detector)

        assert detector.sizes == [(300, 200)]

    def test_an_orientation_tag_that_fails_to_apply_leaves_the_photo_checked(self, monkeypatch):
        """Whatever Pillow does with a bad tag, failing to apply it falls back
        to checking the photo as stored, never to not checking it."""
        from s2e_validator.prefilters import face_detection

        def unreadable(image):
            raise SyntaxError("not a TIFF file")

        monkeypatch.setattr(face_detection.ImageOps, "exif_transpose", unreadable)
        detector = Recording()

        result = check(NewImage(jpeg_with_orientation(300, 200, 6)), detector=detector)

        assert detector.sizes == [(300, 200)]
        assert "face_check" not in result.details

    def test_an_undecodable_photo_is_not_logged_as_a_bad_tag(self, caplog):
        """exif_transpose decodes the pixels before it reads the tag, so without
        an explicit decode first a truncated photo was reported as an
        unreadable orientation tag."""
        buffer = io.BytesIO()
        Image.effect_noise((400, 300), 64).convert("RGB").save(buffer, format="JPEG")
        truncated = buffer.getvalue()[: len(buffer.getvalue()) * 2 // 3]
        detector = Recording()

        with caplog.at_level(logging.INFO):
            result = check(NewImage(truncated), detector=detector)

        assert "orientation_unreadable" not in caplog.text
        # It is unreadable, and handled as that: image_format owns the verdict.
        assert "skipped_unreadable" in caplog.text
        assert result.verdict is Verdict.PASS
        assert detector.sizes == []

    def test_running_out_of_memory_is_not_a_pass(self, monkeypatch):
        """As in image_format: resource exhaustion surfaces as an error, which
        the stage records and alerts on, never as a clean pass."""
        from s2e_validator.prefilters import face_detection

        def exhausted(image):
            raise MemoryError()

        monkeypatch.setattr(face_detection.ImageOps, "exif_transpose", exhausted)

        with pytest.raises(MemoryError):
            check(NewImage(photo(1)), detector=Recording())


class TestPreflight:
    def test_a_working_model_passes(self):
        FaceDetectionFilter().preflight()

    def test_a_missing_model_stops_the_launch(self, tmp_path):
        missing = FaceDetectionSettings(model_path=str(tmp_path / "absent.onnx"))

        with pytest.raises(FileNotFoundError, match="models/README.md"):
            FaceDetectionFilter(missing).preflight()

    def test_the_stage_names_the_filter_that_cannot_run(self, tmp_path):
        config = PipelineConfig(
            settings={"face_detection": {"model_path": str(tmp_path / "absent.onnx")}}
        )

        with pytest.raises(PreflightFailed, match="face_detection cannot run"):
            build_pipeline(config).preflight()

    def test_a_filter_that_is_off_is_not_checked(self, tmp_path):
        config = PipelineConfig(
            modes={"face_detection": FilterMode.OFF},
            settings={"face_detection": {"model_path": str(tmp_path / "absent.onnx")}},
        )

        build_pipeline(config).preflight()
