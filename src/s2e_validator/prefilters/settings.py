"""Turning config values into filter settings, loudly.

A threshold that silently keeps its default, or a filter that only fails once a
real submission reaches it, is worse than a config file that refuses to load.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import Any, TypeVar

S = TypeVar("S")


def settings_from_dict(settings_type: type[S], raw: Mapping[str, Any]) -> S:
    """Build a filter's settings dataclass, rejecting unknown keys.

    A typo in a config file should fail loudly rather than silently leave a
    threshold at its default. Value checks belong to each settings class, which
    knows its own ranges; the helpers below cover the common ones.
    """
    known = {f.name for f in fields(settings_type)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"Unknown {settings_type.__name__} keys: {sorted(unknown)}; known: {sorted(known)}"
        )
    return settings_type(**raw)


def require_int(value: Any, name: str, minimum: int | None = None, maximum: int | None = None) -> int:
    # bool is an int in Python, and "true" as a threshold is always a mistake.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number, got {type(value).__name__}")
    _check_range(value, name, minimum, maximum)
    return value


def require_float(
    value: Any, name: str, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    _check_range(value, name, minimum, maximum)
    return float(value)


def require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {type(value).__name__}")
    return value


def require_str_sequence(value: Any, name: str) -> tuple[str, ...]:
    """A list of strings, never a bare string.

    `"JPEG"` is a `Sequence[str]`, so without this check it becomes the set
    {"J", "P", "E", "G"} and every photo is rejected.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a list of strings, got {type(value).__name__}")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain only strings, got {type(item).__name__}")
    return tuple(value)


def _check_range(
    value: float, name: str, minimum: float | None, maximum: float | None
) -> None:
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}, got {value}")
