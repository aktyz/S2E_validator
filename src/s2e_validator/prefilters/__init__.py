"""Stage 1: deterministic pre-filters that run before the paid judge call."""

from .base import NoOpFilter, PreFilter
from .config import FilterMode, PipelineConfig
from .hashing import ImageIdentity, ImageKind, image_identity
from .image_duplicate import ImageExactDuplicateFilter
from .image_format import ImageFormatFilter, ImageFormatSettings
from .interaction import ExistingImage, ImageInput, Interaction, NewImage
from .pipeline import FilterRecord, PipelineResult, PreFilterPipeline
from .result import PreFilterResult, Verdict
from .settings import settings_from_dict
from .stores import DuplicateClaimResult, ImageKeyStore, InMemoryImageKeyStore, SeenImage
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog
from .text_format import TextFormatFilter, TextFormatSettings

__all__ = [
    "DuplicateClaimResult",
    "DuplicateImageEvent",
    "ExistingImage",
    "FilterMode",
    "FilterRecord",
    "ImageExactDuplicateFilter",
    "ImageFormatFilter",
    "ImageFormatSettings",
    "ImageIdentity",
    "ImageInput",
    "ImageKeyStore",
    "ImageKind",
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
]
