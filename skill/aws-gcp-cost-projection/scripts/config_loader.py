#!/usr/bin/env python3
"""
config_loader.py — Loads JSON config files from the skill's data/ directory.

All hardcoded tables (instance specs, GPU models, service aliases, etc.) live in
data/*.json. This module reads them so Python scripts never need to be edited
when new AWS/GCP services appear — only the JSON files need updating.

Usage:
    from config_loader import load_data_config
    specs = load_data_config("instance-specs")   # → parsed dict from data/instance-specs.json
"""
from __future__ import annotations
import json
import logging
import os

log = logging.getLogger(__name__)

# Resolved once at import time: the skill root is the parent of the scripts/ directory.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR   = os.environ.get("SKILL_DIR", os.path.dirname(_SCRIPTS_DIR))
_DATA_DIR    = os.path.join(_SKILL_DIR, "data")


def load_data_config(name: str) -> dict:
    """Load and return data/<name>.json. Returns {} and logs a warning on failure."""
    path = os.path.join(_DATA_DIR, f"{name}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("config_loader: %s not found — using empty defaults", path)
        return {}
    except Exception as e:
        log.warning("config_loader: failed to load %s: %s — using empty defaults", path, e)
        return {}
