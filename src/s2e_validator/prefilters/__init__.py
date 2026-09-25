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
from .registry import (
    FILTER_BUILDERS,
    FilterDependencies,
    build_filters,
    build_pipeline,
    load_config,
)
from .result import PreFilterResult, Verdict
from .settings import settings_from_dict
from .stores import DuplicateClaimResult, ImageKeyStore, InMemoryImageKeyStore, SeenImage
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog
from .text_format import TextFormatFilter, TextFormatSettings
from .windows import GLOBAL_BUCKET, HashEntry, HashMatch, HashWindow, user_bucket

# Sorted, so adding a name is a one-line diff wherever it belongs.
__all__ = [
    "DuplicateClaimResult",
    "DuplicateImageEvent",
    "ExistingImage",
    "FILTER_BUILDERS",
    "FilterDependencies",
    "FilterMode",
    "FilterRecord",
    "GLOBAL_BUCKET",
    "HashEntry",
    "HashMatch",
    "HashWindow",
    "ImageExactDuplicateFilter",
    "ImageFormatFilter",
    "ImageFormatSettings",
    "ImageIdentity",
    "ImageInput",
    "ImageKeyStore",
    "ImageKind",
    "ImageNearDuplicateFilter",
    "ImageNearDuplicateSettings",
    "InMemoryImageKeyStore",
    "InMemorySuspicionLog",
    "Interaction",
    "NewImage",
    "NoOpFilter",
    "PipelineConfig",
    "PipelineResult",
    "PreFilter",
    "PreFilterPipeline",
    "PreFilterResult",
    "SeenImage",
    "SuspicionLog",
    "TextFormatFilter",
    "TextFormatSettings",
    "TextNearDuplicateFilter",
    "TextNearDuplicateSettings",
    "Verdict",
    "build_filters",
    "build_pipeline",
    "dhash",
    "hamming",
    "image_identity",
    "load_config",
    "settings_from_dict",
    "simhash",
    "user_bucket",
]
