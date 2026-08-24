"""
Guards against the cache-drift bug found via a live customer job: resolve_sku()
trusts a HIT in data/resolved_skus.json forever, with no re-verification against
the current matching logic in lookup_sku_in_catalog() (noise-qualifier filtering,
region-match rules, etc.). When that logic is fixed or tightened, entries cached
under the OLD logic don't automatically get corrected — they just keep returning
the old, now-wrong SKU on every future job, silently, until someone happens to
notice a suspicious cost ratio and investigates by hand.

Confirmed real: two entries in resolved_skus.json ("Hyperdisk Balanced IOPS" for
asia-south1 and asia-south2) were still pointing at "...Confidential Mode" SKU
variants (5-6x cheaper) that the current _has_noise() qualifier filter would
now correctly exclude — the cache was written before that filter existed (or
before it covered "confidential mode") and was never revalidated.

This test is the fix: it re-runs lookup_sku_in_catalog() fresh for every cached
entry and asserts the result still agrees with what's cached. Run this after
ANY change to lookup_sku_in_catalog()/_has_noise()/_NOISE_QUALIFIERS/region
matching — a failure here means the cache has drifted and needs the same kind
of correction applied to data/resolved_skus.json (see git history for the
Hyperdisk IOPS fix as a template) plus a check of which live jobs it touched.

This does NOT catch the OTHER class of cache problem (a cached SKU that's
completely phantom-unavailable despite matching correctly — see
_KNOWN_PHANTOM_AVAILABILITY and the N4D/asia-south2 case) — that's a
catalog-vs-reality problem no amount of re-running our own matching logic can
detect; it requires a human-confirmed exception, not a freshness check.
"""
import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import apply_static_mappings as sm  # noqa: E402

DATA_DIR = os.path.join(SCRIPTS_DIR, "..", "data")
RESOLVED_SKUS_FILE = os.path.join(DATA_DIR, "resolved_skus.json")


def _cache_entries():
    if not os.path.exists(RESOLVED_SKUS_FILE):
        return []
    with open(RESOLVED_SKUS_FILE) as f:
        cache = json.load(f)
    entries = []
    for key, v in cache.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        service, desc_pattern, region = parts
        cached_id = v.get("sku_id") if isinstance(v, dict) else v
        if cached_id:
            entries.append((key, service, desc_pattern, region, cached_id))
    return entries


@pytest.mark.parametrize("key,service,desc_pattern,region,cached_id", _cache_entries())
def test_cached_sku_matches_fresh_lookup(key, service, desc_pattern, region, cached_id):
    fresh_id, _unit, _group = sm.lookup_sku_in_catalog(service, desc_pattern, region)
    assert fresh_id == cached_id, (
        f"resolved_skus.json entry {key!r} is stale: cached {cached_id!r} but "
        f"lookup_sku_in_catalog() now resolves to {fresh_id!r}. The matching logic "
        f"has moved on since this was cached — update data/resolved_skus.json to "
        f"the fresh value (and audit which live jobs used the stale one)."
    )
