"""Per-filter run modes and thresholds, so both can change without a redeploy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

EMPTY_SETTINGS: Mapping[str, Any] = MappingProxyType({})


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
    # Raw threshold values per filter; each filter parses its own (e.g.
    # TextFormatSettings.from_dict), so the pipeline never needs to know them.
    settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def mode_for(self, filter_name: str) -> FilterMode:
        return self.modes.get(filter_name, self.default_mode)

    def settings_for(self, filter_name: str) -> Mapping[str, Any]:
        return self.settings.get(filter_name, EMPTY_SETTINGS)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PipelineConfig:
        """Build from plain data, e.g. a parsed JSON/YAML config file.

        A filter entry is either a mode on its own, or a mapping carrying a mode
        and that filter's thresholds:

        {
          "default_mode": "shadow",
          "filters": {
            "image_exact_duplicate": "enforce",
            "text_format": {"mode": "enforce", "settings": {"min_chars": 8}}
          }
        }
        """
        unknown = set(raw) - {"default_mode", "filters"}
        if unknown:
            raise ValueError(f"Unknown pipeline config keys: {sorted(unknown)}")

        default_mode = FilterMode(raw.get("default_mode", FilterMode.SHADOW.value))
        modes: dict[str, FilterMode] = {}
        settings: dict[str, Mapping[str, Any]] = {}

        for name, entry in raw.get("filters", {}).items():
            if isinstance(entry, str):
                modes[name] = FilterMode(entry)
                continue
            if not isinstance(entry, Mapping):
                raise ValueError(f"Filter '{name}' must be a mode string or a mapping")
            unknown_entry = set(entry) - {"mode", "settings"}
            if unknown_entry:
                raise ValueError(f"Unknown keys for filter '{name}': {sorted(unknown_entry)}")
            if "mode" in entry:
                modes[name] = FilterMode(entry["mode"])
            if "settings" in entry:
                if not isinstance(entry["settings"], Mapping):
                    raise ValueError(f"Settings for filter '{name}' must be a mapping")
                settings[name] = dict(entry["settings"])

        return cls(default_mode=default_mode, modes=modes, settings=settings)
