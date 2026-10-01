"""Face detection: the campaign's own rule, not a quality judgement.

Obywatel Bielik excludes photos of people's faces, so this filter enforces a
client rule rather than deciding whether a contribution is good. A small local
model keeps it inside the "no paid or remote call, bounded local CPU" premise.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageOps

from .base import PreFilter
from .hashing import DEFAULT_MAX_PIXELS
from .interaction import Interaction, NewImage
from .result import PreFilterResult
from .settings import require_float, require_int, settings_from_dict

logger = logging.getLogger(__name__)

REASON_FACE_DETECTED = "IMAGE_CONTAINS_FACE"

#: Bundled YuNet model: 227 KB, MIT licensed. See models/README.md.
DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[3] / "models" / "face_detection_yunet_2023mar.onnx"


class FaceAction(str, Enum):
    """What a detected face does to the submission.

    BLUR is not implemented: this stage inspects submissions, it never rewrites
    them, so blurring belongs to a transform step the client has to ask for.
    Pending the client's answer in #28.
    """

    REJECT = "reject"
    FLAG = "flag"


@dataclass(frozen=True)
class FaceBox:
    x: int
    y: int
    width: int
    height: int
    score: float

    def area_ratio(self, image_width: int, image_height: int) -> float:
        total = image_width * image_height
        return (self.width * self.height) / total if total else 0.0


class FaceDetector(Protocol):
    def detect(self, image: Image.Image) -> Sequence[FaceBox]: ...


@dataclass(frozen=True)
class FaceDetectionSettings:
    model_path: str = str(DEFAULT_MODEL_PATH)
    #: YuNet confidence below which a detection is ignored.
    score_threshold: float = 0.6
    #: Ignore faces smaller than this share of the photo. 0.0 catches everything,
    #: including distant bystanders in a market scene; raise it if the client
    #: only cares about people who are the subject.
    min_area_ratio: float = 0.0
    #: Photos are downscaled before detection; detection cost scales with pixels.
    max_side: int = 1024
    #: Never decoded above this, the same bound as image_format: that filter may
    #: be in shadow mode, which never stops a photo, so this one bounds itself.
    max_pixels: int = DEFAULT_MAX_PIXELS
    action: FaceAction = FaceAction.REJECT

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FaceDetectionSettings:
        prepared = dict(raw)
        if "action" in prepared:
            known = ", ".join(a.value for a in FaceAction)
            try:
                prepared["action"] = FaceAction(prepared["action"])
            except ValueError:
                raise ValueError(
                    f"action must be one of {known}, got {prepared['action']!r}"
                ) from None
        parsed = settings_from_dict(cls, prepared)
        if not isinstance(parsed.model_path, str) or not parsed.model_path:
            raise ValueError("model_path must be a path to the ONNX model")
        require_float(parsed.score_threshold, "score_threshold", minimum=0.0, maximum=1.0)
        require_float(parsed.min_area_ratio, "min_area_ratio", minimum=0.0, maximum=1.0)
        require_int(parsed.max_side, "max_side", minimum=64)
        require_int(parsed.max_pixels, "max_pixels", minimum=1)
        return parsed


class YuNetFaceDetector:
    """OpenCV's YuNet, loaded lazily and guarded by a lock.

    Lazily because the whole filter set is constructed up front: a missing model
    file should surface as one filter's error, not stop the stage from starting.
    The lock serialises detection because one `FaceDetectorYN` instance is not
    safe to share across threads; at about 60 ms a photo, that is the ceiling on
    this filter's throughput per process.
    """

    def __init__(self, model_path: str | Path, score_threshold: float = 0.6):
        self._model_path = Path(model_path)
        self._score_threshold = score_threshold
        self._detector: Any | None = None
        self._lock = threading.Lock()

    def detect(self, image: Image.Image) -> list[FaceBox]:
        import cv2  # imported here so the rest of the stage runs without OpenCV

        frame = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        height, width = frame.shape[:2]

        with self._lock:
            if self._detector is None:
                if not self._model_path.is_file():
                    raise FileNotFoundError(
                        f"Face detection model missing: {self._model_path}. "
                        "See validator/models/README.md."
                    )
                self._detector = cv2.FaceDetectorYN.create(
                    str(self._model_path), "", (width, height), self._score_threshold
                )
            self._detector.setInputSize((width, height))
            _, detections = self._detector.detect(frame)

        if detections is None:
            return []
        return [
            FaceBox(int(row[0]), int(row[1]), int(row[2]), int(row[3]), float(row[-1]))
            for row in detections
        ]


class FaceDetectionFilter(PreFilter):
    """Flags or rejects photos containing a recognisable face."""

    name = "face_detection"
    # Tier 40: the only filter that runs a model, even a small local one.
    cost_rank = 40

    def __init__(
        self,
        settings: FaceDetectionSettings | None = None,
        detector: FaceDetector | None = None,
    ):
        self._settings = settings or FaceDetectionSettings()
        self._detector = detector or YuNetFaceDetector(
            self._settings.model_path, self._settings.score_threshold
        )

    def preflight(self) -> None:
        """Load the model and run it once, so a missing or broken model stops
        the service at startup instead of disabling this filter for a whole
        campaign one fail-open error at a time."""
        self._detector.detect(Image.new("RGB", (64, 64)))

    def check(self, interaction: Interaction) -> PreFilterResult:
        image = interaction.image
        if not isinstance(image, NewImage):
            # Pooled photos were checked when they arrived.
            return PreFilterResult.passed()

        try:
            with Image.open(io.BytesIO(image.data)) as opened:
                # `size` comes from the header; nothing is decoded yet.
                width, height = opened.size
                if width * height > self._settings.max_pixels:
                    return PreFilterResult.passed(
                        face_check="skipped",
                        reason=f"{width}x{height} exceeds {self._settings.max_pixels} pixels",
                    )
                # For JPEG this decodes up to 8x smaller from the start, so a
                # 12 MP phone photo never exists at full size. Other formats
                # ignore it and are bounded by the check above.
                side = self._settings.max_side
                opened.draft("RGB", (side, side))
                # Decode here, so a photo that cannot be decoded is handled
                # below as unreadable, and _upright only ever sees the tag.
                opened.load()
                upright = self._upright(opened, interaction.submission_id)
                prepared = self._downscale(upright.convert("RGB"))
        except MemoryError:
            # Running out of memory is not "these bytes are not a photo". As in
            # image_format, let it surface as this filter's error, which the
            # stage records and the error-rate alert counts, never as a pass.
            raise
        except Exception:  # noqa: BLE001 - image_format owns undecodable photos
            logger.info(
                "face_detection_skipped_unreadable submission=%s", interaction.submission_id
            )
            return PreFilterResult.passed()

        faces = [
            face
            for face in self._detector.detect(prepared)
            if face.score >= self._settings.score_threshold
            and face.area_ratio(prepared.width, prepared.height) >= self._settings.min_area_ratio
        ]
        if not faces:
            return PreFilterResult.passed()

        largest = max(faces, key=lambda face: face.width * face.height)
        details = {
            "faces": len(faces),
            "top_score": round(max(face.score for face in faces), 3),
            "largest_area_ratio": round(largest.area_ratio(prepared.width, prepared.height), 4),
        }
        # Deliberately no face coordinates in the result: this is a privacy
        # filter, so it records that a face was found, not where.
        if self._settings.action is FaceAction.REJECT:
            return PreFilterResult.rejected(
                REASON_FACE_DETECTED, confidence=details["top_score"], **details
            )
        return PreFilterResult.flagged(
            REASON_FACE_DETECTED, confidence=details["top_score"], **details
        )

    @staticmethod
    def _upright(image: Image.Image, submission_id: str) -> Image.Image:
        """Apply the orientation tag, or check the photo as stored.

        Phones store portrait shots sideways plus an orientation tag, and
        neither open() nor convert() applies it; the detector is not
        rotation-invariant, so it should see the photo upright. But the tag is
        metadata a contributor controls: one that cannot be read must never
        skip the face check. Checked sideways beats not checked at all.
        """
        try:
            return ImageOps.exif_transpose(image)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - a bad tag must not skip the face check
            logger.info("face_detection_orientation_unreadable submission=%s", submission_id)
            return image

    def _downscale(self, image: Image.Image) -> Image.Image:
        longest = max(image.width, image.height)
        if longest <= self._settings.max_side:
            return image
        scale = self._settings.max_side / longest
        # At least 1 pixel each way: a very tall, thin image would otherwise
        # round to zero, resize would raise, and the photo would pass unchecked.
        width = max(1, round(image.width * scale))
        height = max(1, round(image.height * scale))
        return image.resize((width, height))
