"""
Guards against the class of bug found twice in one session: a static mapper
hardcodes a (gcp_service, sku_desc_pattern) pair that reads as plausible GCP
terminology but doesn't match ANY real SKU description in the bundled catalog
— e.g. "Classic Load Balancer Forwarding Rule Minimum" (real name has no
"Classic") and "Cloud Run CPU Allocation Time" (real names are "Services CPU
(Instance-based billing)" / "Jobs CPU" / "Worker Pools CPU"). Both silently
downgraded every matching row to passthrough with no visible error — the only
signal was a suspiciously-labeled "word-overlap, verify if inflated" row in a
live customer report.

This scans every literal (gcp_service, desc_pattern) pair the static mappers
can produce and asserts each resolves to at least one real catalog SKU in at
least one representative region. Catches the bug at test time instead of in a
live job's report.
"""
import ast
import os
import re
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import apply_static_mappings as sm  # noqa: E402

SOURCE_PATH = os.path.join(SCRIPTS_DIR, "apply_static_mappings.py")

# One region per continent/family quirk bucket is enough — a pattern that's
# wrong is wrong everywhere; we don't need to check all ~40 GCP regions.
_SAMPLE_REGIONS = ["us-central1", "europe-west1", "asia-southeast1", "asia-south2", "global"]

# Service name aliases used as constants elsewhere in this file (mirrors the
# GCP_* constants in apply_static_mappings.py so extraction can resolve them).
_SERVICE_CONST_NAMES = {
    "GCP_CLOUD_STORAGE", "GCP_CLOUD_SQL", "GCP_COMPUTE_ENGINE", "GCP_MEMORYSTORE",
    "GCP_DATAPROC",
}


def _literal_str(node):
    """Return the literal string value of an AST node, or None if not a plain
    string/f-string-without-interpolation constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _resolve_name(node, module_ns):
    """Resolve a bare Name node to its module-level string constant, if any."""
    if isinstance(node, ast.Name) and node.id in module_ns:
        val = module_ns[node.id]
        if isinstance(val, str):
            return val
    return None


def _extract_flat_hourly_pairs():
    """FLAT_HOURLY_MAP is a clean literal list of (regex, service, desc_pattern,
    multiplier) tuples — pull (service, desc_pattern) straight from the module.
    (service=None, desc=None) is a deliberate "no honest SKU exists, passthrough"
    marker (see Global Accelerator) — nothing to check there."""
    pairs = []
    for _match_re, service, desc_pattern, _mult in sm.FLAT_HOURLY_MAP:
        if service is None and desc_pattern is None:
            continue
        pairs.append((service, desc_pattern))
    return pairs


def _extract_literal_resolve_sku_calls():
    """Parse the source for resolve_sku(service, desc, region) calls whose
    service/desc args are plain string literals or resolve to a module-level
    string constant. Skips calls built from row-derived variables (f-strings,
    dict lookups, etc.) — those are validated dynamically per-row already and
    can't be checked statically without executing the mapper."""
    with open(SOURCE_PATH) as f:
        tree = ast.parse(f.read(), filename=SOURCE_PATH)

    module_ns = {
        name: getattr(sm, name)
        for name in dir(sm)
        if not name.startswith("_") and isinstance(getattr(sm, name), str)
    }

    pairs = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "resolve_sku"):
            continue
        if len(node.args) < 2:
            continue
        service_node, desc_node = node.args[0], node.args[1]
        service = _literal_str(service_node) or _resolve_name(service_node, module_ns)
        desc = _literal_str(desc_node) or _resolve_name(desc_node, module_ns)
        if service and desc:
            pairs.add((service, desc))
    return pairs


def _all_static_pairs():
    pairs = set(_extract_flat_hourly_pairs())
    pairs |= _extract_literal_resolve_sku_calls()
    return sorted(pairs)


_STATIC_PAIRS = _all_static_pairs()


@pytest.mark.parametrize("service,desc_pattern", _STATIC_PAIRS,
                          ids=[f"{s}::{d}" for s, d in _STATIC_PAIRS])
def test_static_sku_pattern_resolves_somewhere(service, desc_pattern):
    """Every hardcoded (service, desc_pattern) a static mapper can emit must
    match at least one real SKU in the bundled catalog, in at least one region."""
    # desc_pattern is used as a regex against catalog descriptions — a literal
    # phrase like "Cloud Load Balancer Forwarding Rule Minimum" matches itself
    # via re.search, so this is a faithful reproduction of the real lookup path.
    try:
        re.compile(desc_pattern)
    except re.error:
        pytest.skip(f"{desc_pattern!r} is not a valid regex — likely a dynamic/templated string")

    found = None
    for region in _SAMPLE_REGIONS:
        sku_id, _unit, _rg = sm.lookup_sku_in_catalog(service, desc_pattern, region)
        if sku_id:
            found = (region, sku_id)
            break
    assert found is not None, (
        f"No catalog SKU matches service={service!r} pattern={desc_pattern!r} in any of "
        f"{_SAMPLE_REGIONS} — every row mapped through this pattern silently falls back "
        f"to passthrough (or an unresolved word-overlap guess) with no visible error. "
        f"Check the real SKU description in data/skus/*.json.gz and correct the pattern."
    )
