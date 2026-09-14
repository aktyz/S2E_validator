"""Per-filter run modes, so each filter can be switched without code changes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FilterMode(str, Enum):
    """OFF     - the filter does not run.
    SHADOW  - the filter runs and its verdict is recorded, but a REJECT does not
              stop the submission. Every filter ships in this mode first.
    ENFORCE - a REJECT stops the pipeline.
    """

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class PipelineConfig:
    # Filters without an explicit entry run in shadow mode, so a newly added
    # filter can never start rejecting real contributors by accident.
    default_mode: FilterMode = FilterMode.SHADOW
    modes: Mapping[str, FilterMode] = field(default_factory=dict)

    def mode_for(self, filter_name: str) -> FilterMode:
        return self.modes.get(filter_name, self.default_mode)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PipelineConfig:
        """Build from plain data, e.g. a parsed JSON/YAML config file.

        {"default_mode": "shadow", "filters": {"length_format": "enforce"}}
        """
        unknown = set(raw) - {"default_mode", "filters"}
        if unknown:
            raise ValueError(f"Unknown pipeline config keys: {sorted(unknown)}")
        default_mode = FilterMode(raw.get("default_mode", FilterMode.SHADOW.value))
        modes = {name: FilterMode(mode) for name, mode in raw.get("filters", {}).items()}
        return cls(default_mode=default_mode, modes=modes)
