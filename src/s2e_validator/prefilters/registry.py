"""Builds the pre-filter stage from configuration alone.

Adding a filter to a campaign, turning one off, putting one back into shadow
mode or retuning a threshold is a config change, not a code change.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .base import PreFilter
from .config import PipelineConfig
from .face_detection import FaceDetectionFilter, FaceDetectionSettings
from .image_duplicate import ImageExactDuplicateFilter
from .image_format import ImageFormatFilter, ImageFormatSettings
from .near_duplicate import (
    ImageNearDuplicateFilter,
    ImageNearDuplicateSettings,
    TextNearDuplicateFilter,
    TextNearDuplicateSettings,
)
from .pipeline import MetricsSink, PreFilterPipeline
from .stores import ImageKeyStore, InMemoryImageKeyStore, utc_now
from .suspicion import InMemorySuspicionLog, SuspicionLog
from .text_format import TextFormatFilter, TextFormatSettings
from .velocity import (
    InMemorySubmissionTimes,
    SubmissionTimesStore,
    SubmissionVelocityFilter,
    VelocitySettings,
)
from .windows import HashWindow


class FilterDependencies:
    """State shared between filters for one campaign.

    The suspicion log is shared on purpose: exact and near-duplicate matches
    belong in one record, not two.

    **Reuse one instance across config reloads.** Retuning a threshold means
    calling `build_pipeline` again; with a fresh `FilterDependencies` that hands
    the new filters empty state, which would silently disable duplicate
    detection for a whole retention window.
    """

    def __init__(
        self,
        image_key_store: ImageKeyStore | None = None,
        suspicion_log: SuspicionLog | None = None,
        clock: Callable[[], datetime] = utc_now,
        hash_windows: MutableMapping[str, HashWindow] | None = None,
        submission_times: SubmissionTimesStore | None = None,
    ):
        # A real store by default, never None: the filter would otherwise build a
        # private one on every `build_pipeline`, and a reload would silently
        # forget every claimed photo.
        self.image_key_store: ImageKeyStore = (
            image_key_store if image_key_store is not None else InMemoryImageKeyStore(clock=clock)
        )
        self.suspicion_log = suspicion_log if suspicion_log is not None else InMemorySuspicionLog()
        self.clock = clock
        # A real store by default, for the same reason as the key store: a
        # filter left to build its own would forget every contributor's
        # recent rate on each config reload.
        self.submission_times: SubmissionTimesStore = (
            submission_times if submission_times is not None else InMemorySubmissionTimes()
        )
        #: Fingerprint windows by filter name. Injectable for the same reason the
        #: key store is: a durable or shared implementation can be plugged in.
        self.hash_windows: MutableMapping[str, HashWindow] = (
            hash_windows if hash_windows is not None else {}
        )

    def window_for(self, filter_name: str, retention: timedelta) -> HashWindow:
        """The window this filter should use, created once and kept.

        `retention` applies only when the window is created: an existing window
        keeps the entries it holds, so a reload cannot wipe them. Changing
        retention takes a new `FilterDependencies`.
        """
        window = self.hash_windows.get(filter_name)
        if window is None:
            window = HashWindow(retention=retention, clock=self.clock)
            self.hash_windows[filter_name] = window
        return window


FilterBuilder = Callable[[Mapping[str, Any], FilterDependencies], PreFilter]


def _text_format(settings: Mapping[str, Any], _: FilterDependencies) -> PreFilter:
    return TextFormatFilter(TextFormatSettings.from_dict(settings))


def _image_format(settings: Mapping[str, Any], _: FilterDependencies) -> PreFilter:
    return ImageFormatFilter(ImageFormatSettings.from_dict(settings))


def _image_exact_duplicate(
    settings: Mapping[str, Any], deps: FilterDependencies
) -> PreFilter:
    if settings:
        raise ValueError(f"{ImageExactDuplicateFilter.name} takes no settings yet")
    return ImageExactDuplicateFilter(
        store=deps.image_key_store, suspicion_log=deps.suspicion_log, clock=deps.clock
    )


def _image_near_duplicate(settings: Mapping[str, Any], deps: FilterDependencies) -> PreFilter:
    parsed = ImageNearDuplicateSettings.from_dict(settings)
    return ImageNearDuplicateFilter(
        parsed,
        window=deps.window_for(ImageNearDuplicateFilter.name, parsed.retention),
        suspicion_log=deps.suspicion_log,
        clock=deps.clock,
    )


def _submission_velocity(settings: Mapping[str, Any], deps: FilterDependencies) -> PreFilter:
    return SubmissionVelocityFilter(VelocitySettings.from_dict(settings), deps.submission_times)


def _face_detection(settings: Mapping[str, Any], _: FilterDependencies) -> PreFilter:
    return FaceDetectionFilter(FaceDetectionSettings.from_dict(settings))


def _text_near_duplicate(settings: Mapping[str, Any], deps: FilterDependencies) -> PreFilter:
    parsed = TextNearDuplicateSettings.from_dict(settings)
    return TextNearDuplicateFilter(
        parsed,
        window=deps.window_for(TextNearDuplicateFilter.name, parsed.retention),
        clock=deps.clock,
    )


#: Every filter the stage knows how to build, by config name.
FILTER_BUILDERS: dict[str, FilterBuilder] = {
    TextFormatFilter.name: _text_format,
    ImageFormatFilter.name: _image_format,
    ImageExactDuplicateFilter.name: _image_exact_duplicate,
    ImageNearDuplicateFilter.name: _image_near_duplicate,
    TextNearDuplicateFilter.name: _text_near_duplicate,
    FaceDetectionFilter.name: _face_detection,
    SubmissionVelocityFilter.name: _submission_velocity,
}


def build_filters(
    config: PipelineConfig, dependencies: FilterDependencies | None = None
) -> list[PreFilter]:
    """Build every known filter, each with its configured thresholds.

    Filters are always built; whether one runs, and whether it can reject, is
    the mode's job. Execution order comes from each filter's `cost_rank`, not
    from this list.
    """
    deps = dependencies or FilterDependencies()
    unknown = set(config.modes) | set(config.settings)
    unknown -= set(FILTER_BUILDERS)
    if unknown:
        raise ValueError(
            f"Config names unknown filters: {sorted(unknown)}; "
            f"known: {sorted(FILTER_BUILDERS)}"
        )
    return [build(config.settings_for(name), deps) for name, build in FILTER_BUILDERS.items()]


def build_pipeline(
    config: PipelineConfig | None = None,
    dependencies: FilterDependencies | None = None,
    metrics: MetricsSink | None = None,
) -> PreFilterPipeline:
    """The pre-filter stage, ready to run.

    Pass a `StageMetrics` as `metrics` to collect per-filter counts and latency
    (#24); without one the stage runs unmeasured.
    """
    resolved = config or PipelineConfig()
    return PreFilterPipeline(build_filters(resolved, dependencies), resolved, metrics)


def load_config(path: str | Path) -> PipelineConfig:
    """Read a JSON config file, e.g. `campaigns/bielik.json`."""
    with Path(path).open(encoding="utf-8") as handle:
        return PipelineConfig.from_dict(json.load(handle))
