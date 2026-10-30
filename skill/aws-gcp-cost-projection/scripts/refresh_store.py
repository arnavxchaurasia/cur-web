#!/usr/bin/env python3
"""
refresh_store.py — Storage and promotion engine for confirmed mappings,
learned product codes, and confirmed rates.

Writable store lives in a per-user state dir (~/.cur-web/refresh_store.json by
default, overridable via CUR_WEB_REFRESH_STORE / CUR_WEB_STATE_DIR); the copy in
data/refresh_store.json is a READ-ONLY seed. Schema:
{
  "version": "1.0.0",
  "updated_at": "...",
  "_meta": {...},
  "mappings": {
    "<key>": {
      "gcp_service": "...",
      "gcp_sku_name": "...",
      "gcp_sku_id": "...",
      "strategy": "map|break_down|passthrough",
      "unit_multiplier": 1.0,
      "confirmation_count": 2,
      "status": "trusted|provisional",
      "last_confirmed_at": "...",
      "sources": ["job_1", "admin"]
    }
  },
  "product_codes": {...},
  "rates": {...}
}
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIRMATION_THRESHOLD = 2


def _get_store_path(store_path: str | None = None) -> str:
    """Writable store location. NEVER the shared, version-controlled data/ dir —
    the store is mutated at runtime (promotions), so it lives in a per-user state
    dir. data/refresh_store.json is a READ-ONLY seed (see _get_seed_path)."""
    if store_path:
        return store_path
    env = os.environ.get("CUR_WEB_REFRESH_STORE")
    if env:
        return env
    state_dir = os.environ.get("CUR_WEB_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cur-web")
    return os.path.join(state_dir, "refresh_store.json")


def _get_seed_path() -> str:
    """Read-only seed shipped with the skill. Loaded when the writable store does
    not yet exist; never written to."""
    skill_dir = os.environ.get("SKILL_DIR") or os.path.dirname(_HERE)
    seed = os.path.join(skill_dir, "data", "refresh_store.json")
    return seed


def _empty_store() -> dict:
    return {
        "version": "1.0.0",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mappings": {},
        "product_codes": {},
        "rates": {},
    }


def load_refresh_store(store_path: str | None = None) -> dict:
    path = _get_store_path(store_path)
    # Writable store not created yet: fall back to the read-only seed in data/ so
    # a fresh install still benefits from shipped facts. Reading the seed is fine;
    # only WRITES must avoid the shared data/ dir.
    read_path = path if os.path.exists(path) else (
        _get_seed_path() if store_path is None and os.path.exists(_get_seed_path()) else None)
    if read_path is None:
        return _empty_store()
    try:
        with open(read_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: failed to load refresh store at {path}: {e}", file=sys.stderr)
        return {
            "version": "1.0.0",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mappings": {},
            "product_codes": {},
            "rates": {},
        }


def save_refresh_store(store: dict, store_path: str | None = None) -> None:
    path = _get_store_path(store_path)
    store["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + f".tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp_path, path)


def get_trusted_mapping(canonical_service_or_code: str, store_path: str | None = None,
                        store: dict | None = None) -> dict | None:
    """Return the TRUSTED mapping for a service/code, or None.

    Pass a preloaded `store` (from load_refresh_store) to avoid re-reading the
    file on every lookup — the classifier calls this up to 4x per misc row."""
    if not canonical_service_or_code:
        return None
    if store is None:
        store = load_refresh_store(store_path)
    mappings = store.get("mappings", {})

    raw = canonical_service_or_code.lower().strip()
    candidates = [
        raw,
        raw.replace(" ", "_"),
        raw.replace("_", " "),
        raw.replace("amazon ", "").replace("aws ", "").strip(),
        raw.replace("amazon ", "").replace("aws ", "").replace(" ", "_").strip(),
    ]
    for c in candidates:
        entry = mappings.get(c)
        if entry and entry.get("status") == "trusted":
            return entry
    return None


def record_or_promote_mapping(
    key: str,
    gcp_service: str,
    gcp_sku_name: str = "",
    gcp_sku_id: str = "",
    strategy: str = "map",
    unit_multiplier: float = 1.0,
    source: str = "reviewer",
    admin_accepted: bool = False,
    store_path: str | None = None,
) -> dict:
    """
    Record a mapping into the refresh store.
    If admin_accepted is True -> immediately 'trusted'.
    If confirmation_count >= CONFIRMATION_THRESHOLD -> 'trusted'.
    Otherwise -> 'provisional'.
    """
    store = load_refresh_store(store_path)
    mappings = store.setdefault("mappings", {})
    clean_key = key.lower().strip()

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = mappings.get(clean_key)

    if not existing:
        entry = {
            "gcp_service": gcp_service,
            "gcp_sku_name": gcp_sku_name,
            "gcp_sku_id": gcp_sku_id,
            "strategy": strategy,
            "unit_multiplier": unit_multiplier,
            "confirmation_count": 1,
            "status": "trusted" if admin_accepted else "provisional",
            "last_confirmed_at": now_iso,
            "sources": [source],
        }
        mappings[clean_key] = entry
        save_refresh_store(store, store_path)
        return entry

    # Existing entry - check compatibility
    same_target = (
        existing.get("gcp_service", "").lower() == gcp_service.lower()
        and (not gcp_sku_id or not existing.get("gcp_sku_id") or existing.get("gcp_sku_id") == gcp_sku_id)
    )

    if same_target:
        # Independence gate: confirmation_count tracks DISTINCT sources, not raw
        # calls. Re-running the reviewer on the same bill (same source) must NOT
        # inflate the count or auto-promote — that would promote on repetition,
        # not on independent agreement. Pass a per-job source (e.g. job id) for
        # genuine independence; a generic/repeated source stays at count 1 and so
        # requires an admin accept to become trusted.
        srcs = existing.setdefault("sources", [])
        if source not in srcs:
            srcs.append(source)
        existing["confirmation_count"] = len(srcs)
        existing["last_confirmed_at"] = now_iso
        if admin_accepted or existing["confirmation_count"] >= CONFIRMATION_THRESHOLD:
            existing["status"] = "trusted"
        if gcp_sku_id and not existing.get("gcp_sku_id"):
            existing["gcp_sku_id"] = gcp_sku_id
        if gcp_sku_name and not existing.get("gcp_sku_name"):
            existing["gcp_sku_name"] = gcp_sku_name
    else:
        # Conflict / change
        if admin_accepted:
            # Admin overrides existing
            existing["gcp_service"] = gcp_service
            existing["gcp_sku_name"] = gcp_sku_name
            existing["gcp_sku_id"] = gcp_sku_id
            existing["strategy"] = strategy
            existing["unit_multiplier"] = unit_multiplier
            existing["confirmation_count"] = 1
            existing["status"] = "trusted"
            existing["last_confirmed_at"] = now_iso
            existing["sources"] = [source]
        else:
            # Do not promote conflicting provisional guess without admin accept
            pass

    save_refresh_store(store, store_path)
    return existing
