#!/usr/bin/env python3
"""
unit_equivalence.py — Physical unit dimensions and equivalence system.

Implements dimensional compatibility and scale conversion between AWS and GCP billing units.
Replaces legacy substring heuristics with typed dimension vector comparisons.
"""

from __future__ import annotations

import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.environ.get("SKILL_DIR") or os.path.dirname(_HERE)
_DATA_DIR = os.path.join(_SKILL_DIR, "data")
_UNIT_DIMS_PATH = os.path.join(_DATA_DIR, "unit_dimensions.json")

_DIMS_CACHE: dict | None = None


def _load_unit_dimensions() -> dict:
    global _DIMS_CACHE
    if _DIMS_CACHE is not None:
        return _DIMS_CACHE
    path = os.path.join(os.environ.get("SKILL_DIR", _SKILL_DIR), "data", "unit_dimensions.json")
    if not os.path.exists(path):
        path = _UNIT_DIMS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            _DIMS_CACHE = json.load(f).get("units", {})
    except Exception:
        _DIMS_CACHE = {}
    return _DIMS_CACHE


def normalize_unit(unit: str | None) -> str:
    """Normalize unit string: lowercase, strip, unify punctuation/separators."""
    if not unit:
        return ""
    clean = str(unit).strip().lower()
    clean = re.sub(r'[\s_]+', '-', clean)
    return clean


def get_unit_entry(unit: str | None) -> dict | None:
    if not unit:
        return None
    units = _load_unit_dimensions()
    raw = str(unit).strip().lower()
    if raw in units:
        return units[raw]

    norm = normalize_unit(unit)
    if norm in units:
        return units[norm]

    dot_norm = norm.replace("-", ".")
    if dot_norm in units:
        return units[dot_norm]

    dash_norm = norm.replace(".", "-")
    if dash_norm in units:
        return units[dash_norm]

    return None


def unit_dims(unit: str | None) -> dict | None:
    """Return physical dimension vector e.g. {'data': 1, 'time': 1}, or None if unknown."""
    entry = get_unit_entry(unit)
    if entry:
        return entry.get("dims")
    return None


def unit_scale(unit: str | None) -> float | None:
    """Return scale factor relative to base unit dimension, or None if unknown."""
    entry = get_unit_entry(unit)
    if entry:
        return entry.get("scale")
    return None


def compatible(a: str | None, b: str | None) -> tuple[bool, str]:
    """
    Check if unit A and unit B are dimensionally compatible.
    Fails closed: if either unit is unknown, returns (False, 'unknown unit ...').
    """
    if not a:
        return False, "empty unit a"
    if not b:
        return False, "empty unit b"

    dims_a = unit_dims(a)
    if dims_a is None:
        return False, f"unknown unit '{a}'"

    dims_b = unit_dims(b)
    if dims_b is None:
        return False, f"unknown unit '{b}'"

    if dims_a != dims_b:
        return False, f"dimension mismatch: '{a}' has dims {dims_a} vs '{b}' has dims {dims_b}"

    return True, "compatible"


def conversion_factor(a: str | None, b: str | None) -> float | None:
    """
    Returns factor to multiply quantity in unit A to get quantity in unit B (scale_a / scale_b).
    Returns None if incompatible or unknown.
    """
    ok, _ = compatible(a, b)
    if not ok:
        return None
    scale_a = unit_scale(a)
    scale_b = unit_scale(b)
    if scale_a is None or scale_b is None or scale_b == 0:
        return None
    return scale_a / scale_b


def unit_class(unit: str | None) -> str | None:
    """
    Backward-compatible shim for legacy callers.
    Maps typed dimensions to legacy class names:
      time: {'time': 1}
      storage-month: {'data': 1, 'time': 1}
      data: {'data': 1}
      count: {'count': 1}
      seat: {'user': 1} or {'user': 1, 'time': 1}
    """
    if not unit:
        return None

    # Exact legacy aliases
    u_lower = str(unit).strip().lower()
    if u_lower in ("time", "storage-month", "data", "count", "seat"):
        return u_lower

    dims = unit_dims(unit)
    if dims is None:
        return None

    if dims == {"time": 1}:
        return "time"
    if dims == {"data": 1, "time": 1}:
        return "storage-month"
    if dims == {"data": 1}:
        return "data"
    if dims == {"count": 1}:
        return "count"
    if dims.get("user"):
        return "seat"

    return None
