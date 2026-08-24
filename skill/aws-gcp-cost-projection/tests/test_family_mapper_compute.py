"""
Regression guard for family_mapper.py's map_gce_row() — the single highest-
traffic mapper (handles the bulk of ordinary EC2 fleet: m5/c5/r5/t3/etc.),
per its own docstring.

A real bug shipped and went undetected by the rest of the suite: an edit that
moved the `burst_note` computation to after the family-switch logic deleted
the assignment but never re-added it, leaving `burst_note` referenced-but-
undefined in the f-string at the end of map_gce_row(). This raised a bare
NameError for EVERY call — i.e. every non-ARM, non-Windows EC2 compute row.

Because family_mapper.py runs as a soft ('?') PreLLMScript in the pipeline,
this didn't fail the job — it silently meant family_mapper.py produced zero
mappings and pruned zero rows from the Phase 2 manifest for the entire
compute_breakdown group, so every compute row (GPU included) fell through to
the Phase 2 LLM sub-agent unresolved. That sub-agent has no GPU-aware
guidance of its own (it's designed to only confirm SKUs family_mapper.py
already picked), so a GPU row (g5.2xlarge) got mapped to a generic guess
(N4D) instead of the correct GPU-paired family (G2) — a real, customer-
visible mapping error traced back to this crash.

These tests call map_gce_row() directly with real instance types (no DuckDB,
no live catalog/API — resolve_sku is patched) so a regression like this
fails a one-line assertion instead of requiring a live job's family_mapper.py
stderr to be read by hand.
"""
from unittest.mock import patch

from conftest import row
from apply_static_mappings import SKUMeta
from family_mapper import parse_instance, map_gce_row

MOCK_SKU = SKUMeta("MOCK-SKU-1234", unit="h", resource_group="Mock")


def _map(instance_type, **overrides):
    r = row(instance_type=instance_type, instance_vcpus=4, instance_ram_gb=16.0, **overrides)
    parsed = parse_instance(instance_type)
    assert parsed is not None, f"parse_instance() failed to parse {instance_type!r}"
    with patch("family_mapper.resolve_sku", return_value=MOCK_SKU), \
         patch("family_mapper.cheapest_in_scope", return_value=(None, "STUB Instance Core", "STUB Instance Ram", False, None)):
        return map_gce_row(r, parsed)


def test_sustained_instance_does_not_crash_and_has_no_burst_note():
    """m5.xlarge is a sustained (non-burstable) source — must map cleanly with
    no exception, and its note must NOT claim a burst-credit caveat."""
    out = _map("m5.xlarge")
    assert out is not None
    for entry in out:
        assert "burst-credit" not in entry["projection_note"]
        assert entry["mapping_confidence"] == 1.0


def test_burstable_instance_does_not_crash_and_names_actual_family():
    """t3.medium is burstable — the note must name whichever GCP family was
    actually picked, not a hardcoded family name that may not match the
    family cheapest_in_scope() actually switched to."""
    out = _map("t3.medium")
    assert out is not None
    core = next(e for e in out if e["component"] == "core")
    assert "burst-credit" in core["projection_note"]
    assert "E2 has no burst-credit" in core["projection_note"]
    assert core["mapping_confidence"] == 0.85


def test_burstable_instance_switched_family_note_matches_switch_not_default():
    """When cheapest_in_scope() switches the row to a cheaper sustained
    family, the burst-credit note must name THAT family, not the original
    default ('E2') — this is the exact regression class the original bug
    represented (the note used to hardcode 'E2' text unconditionally, even
    on rows that had switched away from it)."""
    r = row(instance_type="t3.medium", instance_vcpus=4, instance_ram_gb=16.0)
    parsed = parse_instance("t3.medium")
    with patch("family_mapper.resolve_sku", return_value=MOCK_SKU), \
         patch("family_mapper.cheapest_in_scope",
               return_value=("N2D AMD", "N2D AMD Instance Core", "N2D AMD Instance Ram", True, "cheaper")):
        out = map_gce_row(r, parsed)
    core = next(e for e in out if e["component"] == "core")
    assert "N2D AMD has no burst-credit" in core["projection_note"]
    assert "E2 has no burst-credit" not in core["projection_note"]


def test_compute_optimized_instance_does_not_crash():
    """c5.2xlarge — compute-optimized workload, exercises the same code path
    with a different workload/tier combination."""
    out = _map("c5.2xlarge")
    assert out is not None
    assert len(out) == 2  # core + ram
