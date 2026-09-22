"""Stage 1: deterministic pre-filters that run before the paid judge call."""

from .base import NoOpFilter, PreFilter
from .config import FilterMode, PipelineConfig
from .hashing import ImageIdentity, ImageKind, dhash, hamming, image_identity, simhash
from .image_duplicate import ImageExactDuplicateFilter
from .image_format import ImageFormatFilter, ImageFormatSettings
from .interaction import ExistingImage, ImageInput, Interaction, NewImage
from .near_duplicate import (
    ImageNearDuplicateFilter,
    ImageNearDuplicateSettings,
    TextNearDuplicateFilter,
    TextNearDuplicateSettings,
)
from .pipeline import FilterRecord, PipelineResult, PreFilterPipeline
from .result import PreFilterResult, Verdict
from .settings import settings_from_dict
from .stores import DuplicateClaimResult, ImageKeyStore, InMemoryImageKeyStore, SeenImage
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog
from .text_format import TextFormatFilter, TextFormatSettings
from .windows import GLOBAL_BUCKET, HashEntry, HashMatch, HashWindow, user_bucket

__all__ = [
    "DuplicateClaimResult",
    "DuplicateImageEvent",
    "ExistingImage",
    "FilterMode",
    "FilterRecord",
    "ImageExactDuplicateFilter",
    "ImageFormatFilter",
    "ImageFormatSettings",
    "GLOBAL_BUCKET",
    "HashEntry",
    "HashMatch",
    "HashWindow",
    "ImageIdentity",
    "ImageInput",
    "ImageKeyStore",
    "ImageKind",
    "ImageNearDuplicateFilter",
    "ImageNearDuplicateSettings",
    "TextNearDuplicateFilter",
    "TextNearDuplicateSettings",
    "dhash",
    "hamming",
    "simhash",
    "InMemoryImageKeyStore",
    "InMemorySuspicionLog",
    "SuspicionLog",
    "TextFormatFilter",
    "TextFormatSettings",
    "settings_from_dict",
    "Interaction",
    "NewImage",
    "NoOpFilter",
    "PipelineConfig",
    "PipelineResult",
    "PreFilter",
    "PreFilterPipeline",
    "PreFilterResult",
    "SeenImage",
    "Verdict",
    "image_identity",
    "user_bucket",
]
