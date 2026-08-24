#!/usr/bin/env python3
"""
auto_triage.py — Phase 5 suggestion engine. NEVER touches the database.

Reads outliers_data.json (written by detect_outliers.py) and the DB (read-only).
Computes candidate fixes for structural outliers (D/E/G/B/C/H/I) with confidence labels.
Enriches pricing outliers (A1/A2/F) with full context — NO candidate suggested
(word-overlap re-resolution is the mechanism behind Glacier 120x and S3 50x inflation).

Writes:
  triage_suggestions.md   — LLM input: all rows with candidates or enriched context
  triage_candidates.json  — machine-readable candidates for apply_outlier_fixes.py
"""
import duckdb
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
from apply_static_mappings import resolve_sku, _strict_resolve_sku

JOB_DIR        = os.getcwd()
DB_PATH        = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
DATA_FILE      = os.path.join(JOB_DIR, "outliers_data.json")
SUGGESTIONS_MD = os.path.join(JOB_DIR, "triage_suggestions.md")
CANDIDATES_JSON = os.path.join(JOB_DIR, "triage_candidates.json")


def main():
    if not os.path.exists(DATA_FILE):
        print("outliers_data.json not found — detect_outliers.py may have failed. Writing empty suggestions.")
        with open(SUGGESTIONS_MD, "w", encoding="utf-8") as f:
            f.write("# Triage Suggestions\n\nNo outliers data available. Return `[]`.\n")
        with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
            json.dump({}, f)
        sys.exit(0)

    with open(DATA_FILE) as f:
        data = json.load(f)

    total = data.get("total", 0)
    if total == 0:
        # No outliers — write empty suggestion file and exit cleanly.
        with open(SUGGESTIONS_MD, "w", encoding="utf-8") as f:
            f.write("# Triage Suggestions\n\nNo outliers detected. Return `[]`.\n")
        with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
            json.dump({}, f)
        print("auto_triage: 0 outliers — nothing to triage.")
        return

    # Open DB read-only for context lookups.
    conn = duckdb.connect(DB_PATH, read_only=True)

    candidates = {}  # dict_key(aws_li_key, component) -> candidate dict

    # A break_down row shares ONE aws_li_key across multiple components
    # (core/ram/storage). Keying `candidates` by aws_li_key alone meant that
    # when 2+ components of the SAME row were independently flagged (common —
    # confirmed across nearly every job in this system), only the LAST one
    # processed survived; the others were silently dropped. Worse, several
    # apply-side actions (set_sku, set_service) update by aws_li_key alone with
    # no component filter, so the ONE surviving candidate could get applied to
    # ALL of that row's components — including ones that were never flagged.
    # dict_key() below makes every candidate addressable by its actual
    # (aws_li_key, component) identity; apply_outlier_fixes.py does the same
    # when looking candidates up from the LLM's decisions.
    def dict_key(k, component):
        return f"{k}|{component}" if component else k

    # ------------------------------------------------------------------ #
    # Structural outliers — compute candidates                             #
    # ------------------------------------------------------------------ #

    # B: Phantom cost — AWS ~$0 but GCP cost is large → unit_multiplier bug
    for row in data.get("B", []):
        key = row["aws_li_key"]
        component = row.get("component")
        candidates[dict_key(key, component)] = {
            "query": "B",
            "confidence": "HIGH",
            "candidate": {
                "action": "set_multiplier",
                "component": component,
                "unit_multiplier": 0.0,
                "reason": "Phantom cost: AWS cost ~$0 but GCP cost non-zero. unit_multiplier=0 eliminates phantom."
            }
        }

    # C: Zero rate on billable row → try resolve_sku for a different SKU
    for row in data.get("C", []):
        key = row["aws_li_key"]
        component = row.get("component")
        gcp_service = row.get("gcp_service", "")
        gcp_sku_name = row.get("gcp_sku_name", "")
        # Try to find a SKU in the catalog with a non-zero rate
        region = _lookup_region(conn, key)
        candidate = None
        confidence = "LOW"
        if gcp_service and gcp_sku_name:
            try:
                # _strict_resolve_sku, not resolve_sku — same reasoning as
                # auto_review.py's illegal-passthrough retry: refuses
                # lookup_sku_in_catalog()'s continent-fallback so a family a
                # static mapper already determined has no exact-region rate
                # can't get "fixed" onto a different region's SKU/price here.
                sku_id = _strict_resolve_sku(gcp_service, gcp_sku_name, region)
                if sku_id and sku_id != row.get("gcp_sku_id"):
                    candidate = {
                        "action": "set_sku",
                        "component": component,
                        "gcp_sku_id": sku_id,
                        "gcp_sku_name": gcp_sku_name,
                        "reason": f"Zero rate on current SKU {row.get('gcp_sku_id')} — found alternate SKU with same name"
                    }
            except Exception:
                pass
        candidates[dict_key(key, component)] = {
            "query": "C",
            "confidence": confidence,
            "candidate": candidate,
        }

    # D: Cross-service mismatch → fix gcp_service to match catalog value
    for row in data.get("D", []):
        key = row["aws_li_key"]
        component = row.get("component")
        correct_service = row.get("sku_actually", "")
        candidates[dict_key(key, component)] = {
            "query": "D",
            "confidence": "HIGH",
            "candidate": {
                "action": "set_service",
                "component": component,
                "gcp_service": correct_service,
                "reason": f"SKU catalog says service is '{correct_service}', mapping says '{row.get('mapping_says')}'"
            }
        }

    # E: Missing CUD alias → synthesize from OnDemand SKU (LOW — requires catalog search)
    for row in data.get("E", []):
        key = row["aws_li_key"]
        component = row.get("component")
        # Can't compute the Commit1Yr SKU ID without a catalog search.
        # Provide the OnDemand SKU ID as context so LLM can find the paired CUD SKU.
        candidates[dict_key(key, component)] = {
            "query": "E",
            "confidence": "LOW",
            "candidate": None,
            "context": {
                "component": component,
                "gcp_service": row.get("gcp_service"),
                "od_sku_id": row.get("gcp_sku_id"),
                "hint": "Find the Commit1Yr SKU paired with this OnDemand SKU and INSERT into gcp_sku_rates."
            }
        }

    # G: break_down multiplier mismatch → spec_value is ground truth
    for row in data.get("G", []):
        key = row["aws_li_key"]
        component = row.get("component", "core")
        spec_val = row.get("spec_value")
        if spec_val is not None:
            candidates[dict_key(key, component)] = {
                "query": "G",
                "confidence": "HIGH",
                "candidate": {
                    "action": "set_multiplier",
                    "component": component,
                    "unit_multiplier": float(spec_val),
                    "reason": f"Instance spec says {component}={spec_val}, mapping has {row.get('mapped')}"
                }
            }
        else:
            candidates[dict_key(key, component)] = {"query": "G", "confidence": "NONE", "candidate": None}

    # H: RI/CUD parity — CUD rate row missing in gcp_sku_rates
    for row in data.get("H", []):
        key = row["aws_li_key"]
        component = row.get("component")
        candidates[dict_key(key, component)] = {
            "query": "H",
            "confidence": "LOW",
            "candidate": None,
            "context": {
                "component": component,
                "gcp_service": row.get("gcp_service"),
                "od_sku_id": row.get("gcp_sku_id"),
                "gcp_od": row.get("gcp_od"),
                "hint": "1yr CUD equals OD rate — find and INSERT the Commit1Yr rate row into gcp_sku_rates."
            }
        }

    # I: NULL projection — diagnose cause (region or SKU missing)
    for row in data.get("I", []):
        key = row["aws_li_key"]
        component = row.get("component")
        gcp_region = row.get("gcp_region")
        gcp_sku_id = row.get("gcp_sku_id")
        if not gcp_region:
            candidates[dict_key(key, component)] = {
                "query": "I",
                "confidence": "HIGH",
                "candidate": {
                    "action": "set_region",
                    "table": "aws_li_catalog",
                    "gcp_region": "us-central1",
                    "reason": "NULL gcp_region prevents rate lookup — default to us-central1"
                }
            }
        elif not gcp_sku_id:
            candidates[dict_key(key, component)] = {
                "query": "I",
                "confidence": "HIGH",
                "candidate": {
                    "action": "needs_sku",
                    "component": component,
                    "reason": "gcp_sku_id is NULL — SKU was never resolved. Assign correct SKU."
                }
            }
        else:
            candidates[dict_key(key, component)] = {
                "query": "I",
                "confidence": "LOW",
                "candidate": None,
                "context": {
                    "component": component,
                    "gcp_service": row.get("gcp_service"),
                    "gcp_sku_id": gcp_sku_id,
                    "gcp_region": gcp_region,
                    "hint": "SKU and region both present but cost is NULL — rate missing for this SKU+region combination."
                }
            }

    # Fetch projection_note for pricing rows (A1/A2/F) before closing conn.
    # Without this, LLM queries the DB directly to get context it needs.
    pricing_keys = [r["aws_li_key"] for r in data.get("A1", []) + data.get("A2", []) + data.get("F", [])]
    projection_notes = {}
    if pricing_keys:
        try:
            placeholders = ",".join("?" * len(pricing_keys))
            rows = conn.execute(
                f"SELECT aws_li_key, projection_note FROM aws_li_to_gcp_li WHERE aws_li_key IN ({placeholders})",
                pricing_keys
            ).fetchall()
            projection_notes = {r[0]: r[1] for r in rows if r[1]}
        except Exception:
            pass

    # Pre-check the Phase 5 gate SQL so the LLM knows which rows MUST be fixed
    # to pass the gate on first attempt. Rows that already violate are marked
    # ⚠️ GATE VIOLATION in triage_suggestions.md — LLM must address these first.
    gate_violations = set()
    try:
        over_rows = conn.execute("""
            SELECT aws_li_key FROM gcp_projection
            WHERE is_workload AND strategy IN ('map','break_down')
            AND aws_amortized_cost > 20 AND gcp_projected_cost > aws_amortized_cost * 3
        """).fetchall()
        zero_rows = conn.execute("""
            SELECT aws_li_key FROM gcp_projection
            WHERE is_workload AND strategy IN ('map','break_down')
            AND aws_amortized_cost > 10 AND gcp_projected_cost IS NOT NULL AND gcp_projected_cost = 0
        """).fetchall()
        gate_violations = {r[0] for r in over_rows + zero_rows}
        if gate_violations:
            print(f"auto_triage: {len(gate_violations)} row(s) already violate the gate — marking in suggestions")
    except Exception:
        pass

    conn.close()

    # ------------------------------------------------------------------ #
    # Write triage_candidates.json                                         #
    # ------------------------------------------------------------------ #

    with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, default=str)

    # ------------------------------------------------------------------ #
    # Write triage_suggestions.md                                          #
    # ------------------------------------------------------------------ #

    structural_rows = (
        data.get("B", []) + data.get("C", []) + data.get("D", []) +
        data.get("E", []) + data.get("G", []) + data.get("H", []) + data.get("I", [])
    )
    pricing_rows = data.get("A1", []) + data.get("A2", []) + data.get("F", [])

    with open(SUGGESTIONS_MD, "w", encoding="utf-8") as f:
        f.write("# Triage Suggestions\n\n")
        f.write(f"Structural outliers: {data.get('total_structural', 0)}  |  "
                f"Pricing outliers: {data.get('total_pricing', 0)}\n\n")
        f.write("Return `outlier_fixes.json` — a JSON array:\n")
        f.write('```json\n[{"aws_li_key": "...", "decision": "confirm|override|veto",\n'
                '  "gcp_sku_id": "...", "gcp_sku_name": "...", "unit_multiplier": N,\n'
                '  "gcp_service": "...", "gcp_region": "...", "reason": "..."}]\n```\n\n')
        f.write("- `confirm`: apply the pre-computed candidate exactly\n")
        f.write("- `override`: apply your own values (include the fields you want changed)\n")
        f.write("- `veto`: leave row unchanged, document in reason (rate gap)\n\n")
        f.write("---\n\n")

        if structural_rows:
            f.write("## Structural Outliers (pre-computed candidates available)\n\n")
            f.write("HIGH confidence = ground truth from spec/catalog — confirm unless something is visibly wrong.\n")
            f.write("LOW confidence = attempted resolution, validate the candidate carefully.\n\n")

            for row in structural_rows:
                key = row["aws_li_key"]
                cand_info = candidates.get(dict_key(key, row.get("component")), {})
                conf = cand_info.get("confidence", "NONE")
                cand = cand_info.get("candidate")
                ctx  = cand_info.get("context", {})
                qid  = cand_info.get("query", "?")

                gate_flag = " — ⚠️ GATE VIOLATION (must fix to pass gate)" if key in gate_violations else ""
                f.write(f"### `{key}` — Query {qid} — Confidence: `{conf}`{gate_flag}\n\n")

                # Write available row fields as context
                for k, v in row.items():
                    if k != "aws_li_key" and v is not None:
                        f.write(f"- **{k}**: `{v}`\n")

                if cand:
                    f.write(f"\n**Candidate**: `{cand.get('action')}` — {cand.get('reason', '')}\n")
                    for ck, cv in cand.items():
                        if ck not in ("action", "reason") and cv is not None:
                            f.write(f"  - {ck}: `{cv}`\n")
                elif ctx:
                    f.write(f"\n**Context** (no candidate — reason LLM):\n")
                    for ck, cv in ctx.items():
                        f.write(f"  - {ck}: {cv}\n")
                else:
                    f.write("\n**No candidate** — requires LLM reasoning.\n")
                f.write("\n")

        if pricing_rows:
            f.write("---\n\n")
            f.write("## Pricing Outliers (context only — no candidate suggested)\n\n")
            f.write("Word-overlap catalog re-resolution is how Glacier 120x and S3 50x inflation happened.\n")
            f.write("Use the current SKU name, ratio, and projection_note to reason about the correct fix.\n\n")

            for row in pricing_rows:
                key = row["aws_li_key"]
                qid = _infer_pricing_query(row, data)
                gate_flag = " — ⚠️ GATE VIOLATION (must fix to pass gate)" if key in gate_violations else ""
                f.write(f"### `{key}` — Query {qid}{gate_flag}\n\n")
                for k, v in row.items():
                    if k != "aws_li_key" and v is not None:
                        f.write(f"- **{k}**: `{v}`\n")
                note = projection_notes.get(key)
                if note:
                    f.write(f"- **projection_note**: `{note}`\n")
                f.write("\n")

    n_high = sum(1 for c in candidates.values() if c.get("confidence") == "HIGH" and c.get("candidate"))
    n_low  = sum(1 for c in candidates.values() if c.get("confidence") == "LOW")
    print(f"auto_triage: {len(structural_rows)} structural ({n_high} HIGH, {n_low} LOW), "
          f"{len(pricing_rows)} pricing (context-only)")


def _lookup_region(conn, aws_li_key):
    try:
        rows = conn.execute(
            "SELECT gcp_region FROM aws_li_catalog WHERE aws_li_key = ?", [aws_li_key]
        ).fetchone()
        return rows[0] if rows else "us-central1"
    except Exception:
        return "us-central1"


def _infer_pricing_query(row, data):
    key = row["aws_li_key"]
    for qid in ("A1", "A2", "F"):
        if any(r["aws_li_key"] == key for r in data.get(qid, [])):
            return qid
    return "?"


if __name__ == "__main__":
    main()
