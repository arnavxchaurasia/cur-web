"""
confidence_engine.py  —  Phase P: Confidence Cascade.

Keeps two things separate that were previously conflated:

  classification_confidence — how sure are we about what the AWS SKU IS
                              (from catalog lookup quality in Phase A/B)

  mapping_confidence        — how good is the GCP candidate we chose
                              (from scoring in Phase D)

And adds the missing components:

  data_confidence           — how complete is the input data
                              (from contract validation in Phase J)

  validation_confidence     — did the candidate pass all checks cleanly,
                              or did it squeak through on relaxed constraints

  pricing_confidence        — are the GCP prices confirmed or estimated
                              (injected by Phase 4/apply_rates.py)

  overall_confidence        — combined, with a floor effect: if any single
                              component is critically low, the overall is
                              capped even if the others are high

This makes it obvious to reviewers WHY confidence is low, rather than
presenting one opaque number.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class ConfidenceReport:
    data_confidence:           float   # 0–1: completeness of input fields
    classification_confidence: float   # 0–1: catalog lookup quality
    validation_confidence:     float   # 0–1: how cleanly candidate passed checks
    mapping_confidence:        float   # 0–1: quality of the GCP candidate chosen
    pricing_confidence:        float   # 0–1: confirmed prices vs. estimates (default 1.0)
    overall_confidence:        float   # 0–1: combined with floor effect
    tier: str                          # "Perfect" | "Equivalent" | "Compromise" | "Unsupported"
    notes: list[str]

    def display(self) -> str:
        """Human-readable confidence breakdown for Phase 6 reporting."""
        bar = lambda v: ("█" * int(v * 10)).ljust(10)
        lines = [
            f"  Overall        {self.overall_confidence:.0%}  [{bar(self.overall_confidence)}]  ({self.tier})",
            f"  ├─ Data        {self.data_confidence:.0%}  [{bar(self.data_confidence)}]",
            f"  ├─ Classification {self.classification_confidence:.0%}  [{bar(self.classification_confidence)}]",
            f"  ├─ Validation  {self.validation_confidence:.0%}  [{bar(self.validation_confidence)}]",
            f"  ├─ Mapping     {self.mapping_confidence:.0%}  [{bar(self.mapping_confidence)}]",
            f"  └─ Pricing     {self.pricing_confidence:.0%}  [{bar(self.pricing_confidence)}]",
        ]
        if self.notes:
            lines.append("  Notes:")
            for n in self.notes:
                lines.append(f"    · {n}")
        return "\n".join(lines)


def compute_confidence(
    data_confidence:           float,
    classification_confidence: float,
    mapping_confidence:        float,
    tier_value:                int,           # 1=Perfect, 2=Equivalent, 3=Compromise, 4=Unsupported
    tier_label:                str,
    validation_checks:         dict[str, bool] | None = None,  # {check_name: passed}
    pricing_confidence:        float = 1.0,
    unavailable_fields:        list[str] | None = None,
    notes_in:                  list[str] | None = None,
) -> ConfidenceReport:
    """
    Computes the full confidence cascade.

    validation_confidence is derived from the per-check results in the
    candidate's structured rejection trace — how many checks passed cleanly
    vs. how many were bypassed or relaxed.
    """
    notes: list[str] = list(notes_in or [])
    unavailable = unavailable_fields or []

    # ── Validation confidence ──────────────────────────────────────────────
    if validation_checks:
        passed  = sum(1 for v in validation_checks.values() if v)
        total   = len(validation_checks)
        validation_confidence = passed / total if total else 1.0
    else:
        validation_confidence = 1.0

    # ── Tier penalty ───────────────────────────────────────────────────────
    # Tier affects mapping_confidence floor. A Compromise candidate can't
    # claim the same mapping quality as a Perfect one even if the scoring
    # formula returns a similar number.
    tier_floor = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    tier_cap   = {1: 1.0, 2: 0.90, 3: 0.72, 4: 0.40}
    effective_mapping = min(
        max(mapping_confidence, tier_floor[tier_value]),
        tier_cap[tier_value],
    )

    # ── Notes ──────────────────────────────────────────────────────────────
    if unavailable:
        notes.append(f"CUR-unavailable fields assumed (external API needed): "
                     f"{', '.join(unavailable)}")
    if tier_value == 2:
        notes.append("Equivalent match — minor technical compromise, verify before committing.")
    if tier_value == 3:
        notes.append("Compromise match — meaningful technical gap, manual review strongly advised.")
    if tier_value == 4:
        notes.append("Unsupported — no automatic mapping possible, route to outlier flow.")
    if pricing_confidence < 0.85:
        notes.append("Pricing confidence is low — some prices are estimated, not confirmed from GCP APIs.")
    if data_confidence < 0.8:
        notes.append("Data confidence is low — critical fields missing or unknown, "
                     "review input CUR row before trusting this recommendation.")

    # ── Weighted average ───────────────────────────────────────────────────
    # Weights reflect importance to recommendation quality.
    # Price is intentionally low — it's already handled by apply_rates.py.
    components = {
        "data":           (data_confidence,           0.20),
        "classification": (classification_confidence, 0.25),
        "validation":     (validation_confidence,     0.20),
        "mapping":        (effective_mapping,         0.25),
        "pricing":        (pricing_confidence,        0.10),
    }
    weighted = sum(v * w for v, w in components.values())

    # ── Floor effect ───────────────────────────────────────────────────────
    # If any component is critically low, overall is dragged down
    # proportionally — a single bad component can't be averaged away.
    worst = min(v for v, _ in components.values())
    if worst < 0.5:
        # Worst component pulls overall down toward its value
        overall = weighted * 0.6 + worst * 0.4
        notes.append(f"Overall capped: weakest component is {worst:.0%}.")
    elif worst < 0.7:
        overall = weighted * 0.8 + worst * 0.2
    else:
        overall = weighted

    return ConfidenceReport(
        data_confidence=round(data_confidence, 3),
        classification_confidence=round(classification_confidence, 3),
        validation_confidence=round(validation_confidence, 3),
        mapping_confidence=round(effective_mapping, 3),
        pricing_confidence=round(pricing_confidence, 3),
        overall_confidence=round(overall, 3),
        tier=tier_label,
        notes=notes,
    )


def compute_job_confidence(db_path: str) -> dict:
    """
    Compute a job-level confidence summary from projection.duckdb.

    Opens the DuckDB at db_path, reads aggregate stats, reads
    validation_report.json if present, and returns a dict with keys:
      overall, data, mapping, validation, tier_label, notes
    """
    import os, json

    try:
        import duckdb
    except ImportError as e:
        return {
            "overall": 0.0, "data": 0.0, "mapping": 0.0, "validation": 0.0,
            "tier_label": "Unknown", "notes": [f"duckdb not importable: {e}"]
        }

    notes: list[str] = []

    # ── Open DB ────────────────────────────────────────────────────────────
    try:
        con = duckdb.connect(db_path, read_only=True)
    except Exception as e:
        return {
            "overall": 0.0, "data": 0.0, "mapping": 0.0, "validation": 0.0,
            "tier_label": "Unknown", "notes": [f"Cannot open {db_path}: {e}"]
        }

    # ── Row stats from aws_li_to_gcp_li ───────────────────────────────────
    try:
        row = con.execute("""
            SELECT
                COUNT(*)                                                   AS total_rows,
                COUNT(*) FILTER (WHERE strategy = 'passthrough')           AS passthrough_rows,
                COUNT(*) FILTER (WHERE strategy IN ('map','break_down'))   AS mapped_rows
            FROM aws_li_to_gcp_li
        """).fetchone()
        total_rows, passthrough_rows, mapped_rows = row if row else (0, 0, 0)
        total_rows = total_rows or 1  # guard div-by-zero
    except Exception as e:
        con.close()
        return {
            "overall": 0.0, "data": 0.0, "mapping": 0.0, "validation": 0.0,
            "tier_label": "Unknown", "notes": [f"Cannot read aws_li_to_gcp_li: {e}"]
        }

    # ── Data confidence: unavailable required fields from aws_li_catalog ──
    rows_with_unavailable = 0
    try:
        # field_states is stored as JSON in aws_li_catalog; count rows where
        # any required field has state 'unavailable'.
        result = con.execute("""
            SELECT COUNT(*) FROM aws_li_catalog
            WHERE field_states IS NOT NULL
              AND json_extract_string(field_states, '$.required_unavailable') IS NOT NULL
              AND CAST(json_extract(field_states, '$.required_unavailable') AS INTEGER) > 0
        """).fetchone()
        rows_with_unavailable = result[0] if result else 0
    except Exception:
        # Fallback: count rows where critical fields are NULL
        try:
            result = con.execute("""
                SELECT COUNT(*) FROM aws_li_catalog
                WHERE (instance_type IS NULL OR instance_type = '')
                  AND (product IS NULL OR product = '')
            """).fetchone()
            rows_with_unavailable = result[0] if result else 0
            notes.append("field_states column not present; data confidence estimated from NULL critical fields.")
        except Exception:
            rows_with_unavailable = 0

    data_confidence = 1.0 - (rows_with_unavailable / total_rows) * 0.3

    # ── Mapping confidence ─────────────────────────────────────────────────
    # Passthroughs count against mapping quality.
    mapping_confidence = mapped_rows / total_rows

    if passthrough_rows > 0:
        pct = passthrough_rows / total_rows * 100
        notes.append(f"{passthrough_rows} passthrough row(s) ({pct:.1f}%) reduce mapping confidence.")

    # ── Validation confidence: read validation_report.json ────────────────
    validation_confidence = 1.0
    jobdir = os.path.dirname(db_path)
    # db may be in projection-audit subdir; look one level up too
    candidates = [jobdir, os.path.dirname(jobdir)]
    report_path = None
    for d in candidates:
        p = os.path.join(d, "validation_report.json")
        if os.path.exists(p):
            report_path = p
            break

    if report_path:
        try:
            with open(report_path) as f:
                vrep = json.load(f)
            violations = vrep.get("violations", {})
            # Count hard (non-empty lists) vs soft
            hard_gates = {
                "phantom_zero", "under_projection_gcp_zero", "unreachable_rate",
                "storage_transfer_over_projection", "instance_family_mismatch",
                "reconciliation", "capacity_reconciliation", "projection_view_missing",
            }
            soft_gates = {"passthrough_on_mappable_service", "cud_coverage_missing"}
            has_hard = any(len(violations.get(g, [])) > 0 for g in hard_gates)
            has_soft = any(len(violations.get(g, [])) > 0 for g in soft_gates)
            if has_hard:
                validation_confidence = 0.4
                notes.append("Hard validation violations present — review validation_report.json.")
            elif has_soft:
                validation_confidence = 0.7
                notes.append("Soft validation violations present — minor issues may affect accuracy.")
        except Exception as e:
            notes.append(f"Could not read validation_report.json: {e}")
    else:
        notes.append("validation_report.json not found; validation confidence assumed 1.0.")

    # ── Overall ────────────────────────────────────────────────────────────
    overall = data_confidence * mapping_confidence * validation_confidence

    # ── Tier label ────────────────────────────────────────────────────────
    if overall >= 0.85:
        tier_label = "Perfect"
    elif overall >= 0.70:
        tier_label = "Equivalent"
    elif overall >= 0.50:
        tier_label = "Compromise"
    else:
        tier_label = "Unsupported"

    con.close()

    return {
        "overall":    round(overall, 3),
        "data":       round(data_confidence, 3),
        "mapping":    round(mapping_confidence, 3),
        "validation": round(validation_confidence, 3),
        "tier_label": tier_label,
        "notes":      notes,
    }


if __name__ == "__main__":
    import sys, json
    print(json.dumps(compute_job_confidence(sys.argv[1]), indent=2))
