"""Stage 1: deterministic pre-filters that run before the paid judge call."""

from .base import NoOpFilter, PreFilter
from .config import FilterMode, PipelineConfig
from .hashing import ImageIdentity, ImageKind, image_identity
from .image_duplicate import ImageExactDuplicateFilter
from .interaction import ExistingImage, ImageInput, Interaction, NewImage
from .pipeline import FilterRecord, PipelineResult, PreFilterPipeline
from .result import PreFilterResult, Verdict
from .stores import DuplicateClaimResult, ImageKeyStore, InMemoryImageKeyStore, SeenImage
from .suspicion import DuplicateImageEvent, InMemorySuspicionLog, SuspicionLog

__all__ = [
    "DuplicateClaimResult",
    "DuplicateImageEvent",
    "ExistingImage",
    "FilterMode",
    "FilterRecord",
    "InMemorySuspicionLog",
    "SuspicionLog",
    "ImageExactDuplicateFilter",
    "ImageIdentity",
    "ImageInput",
    "ImageKeyStore",
    "ImageKind",
    "InMemoryImageKeyStore",
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
