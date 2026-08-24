#!/usr/bin/env python3
"""
llm_override_guard.py — minimal last-resort format/schema gate before DB writes.

Business-logic validation (SKU existence, tier safety, multiplier correctness,
region coverage) is now handled by the Phase 3 / Phase 5 LLM agent itself,
guided by data/validation-rules.json. The agent runs those checks before
writing review_fixes.json — by the time apply_review_fixes.py calls this
function the override has already been validated by the LLM.

This guard exists only as a final safety net for things that are cheap to
verify mechanically and catastrophic if wrong:
  1. SKU ID format — GCP SKU IDs are XXXX-XXXX-XXXX. A malformed string
     cannot be a real SKU regardless of what the LLM said.
  2. unit_multiplier type and sign — must be a positive number. A zero or
     negative multiplier produces $0 GCP cost silently; catching it here
     prevents a bad write even if the LLM prompt failed to catch it.
  3. Scope check — the row must have been flagged by auto_review.py. This
     is enforced in apply_review_fixes.py, not here; this function does not
     re-check scope.

All business rules (tier safety, intentional passthrough protection, region
coverage, service-SKU consistency, dimensional correctness) live in
data/validation-rules.json and are enforced by the LLM agent at prompt time.
"""
from __future__ import annotations
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg

_review_cfg = _cfg("review-config")
_MAX_MULT_VCPU   = _review_cfg.get("max_unit_multiplier_vcpu",   512)
_MAX_MULT_RAM_GB = _review_cfg.get("max_unit_multiplier_ram_gb", 8192)

# GCP SKU ID format: three groups of 4 hex characters separated by hyphens.
_SKU_ID_RE = re.compile(r'^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$')


def validate_override(conn, aws_li_key, component, fix):
    """
    Final format/schema gate before writing an LLM override to the DB.
    Returns (ok: bool, reason: str).

    Does NOT re-run business-logic rules — those are the LLM's responsibility
    per data/validation-rules.json. Only catches format errors that are
    cheap to detect and catastrophic if written.
    """
    gcp_sku_id  = fix.get("gcp_sku_id")
    unit_mult   = fix.get("unit_multiplier")

    # 1. SKU ID format check
    if gcp_sku_id and not _SKU_ID_RE.match(str(gcp_sku_id)):
        return False, (
            f"gcp_sku_id {gcp_sku_id!r} does not match the GCP SKU ID format "
            f"(expected XXXX-XXXX-XXXX hex). This is not a real SKU ID — "
            f"run find-sku.sh to get the correct one."
        )

    # 2. Multiplier type and sign check
    if unit_mult is not None:
        try:
            mult_f = float(unit_mult)
        except (TypeError, ValueError):
            return False, f"unit_multiplier {unit_mult!r} is not a number"

        if mult_f <= 0:
            return False, (
                f"unit_multiplier must be > 0 (got {mult_f}). "
                f"A zero or negative multiplier produces $0 GCP cost."
            )

        comp = (component or "core").lower()
        limit = _MAX_MULT_RAM_GB if comp == "ram" else _MAX_MULT_VCPU
        if mult_f > limit:
            return False, (
                f"unit_multiplier {mult_f} exceeds the sanity limit of {limit} "
                f"for component {comp!r}. If this instance is genuinely this large, "
                f"update max_unit_multiplier_* in data/review-config.json."
            )

    return True, ""
