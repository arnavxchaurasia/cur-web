#!/usr/bin/env python3
"""
auto_review.py — Phase 3 suggestion engine. NEVER modifies the database.

Detects mapping issues and pre-computes candidate fixes:
  - Illegal passthroughs: core services (EC2/RDS/S3/etc.) marked passthrough
  - Spec violations: break_down rows with wrong unit_multipliers vs instance spec

Writes:
  review_flags.md         — human-readable report with candidates (LLM input)
  review_candidates.json  — machine-readable candidates (for apply_review_fixes.py)
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
from apply_static_mappings import resolve_sku, intentional_passthrough_exclude_clause, _strict_resolve_sku
from config_loader import load_data_config as _cfg

JOB_DIR         = os.getcwd()
DB_PATH         = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
FLAGS_FILE      = os.path.join(JOB_DIR, "review_flags.md")
CANDIDATES_FILE = os.path.join(JOB_DIR, "review_candidates.json")

# Materiality threshold loaded from data/review-config.json.
# Passthrough rows below this cost are not worth the LLM's time.
# Increase to reduce review volume; decrease to catch more edge cases.
_PASSTHROUGH_MATERIALITY_USD = _cfg("review-config").get("passthrough_materiality_usd", 5.0)


def main():
    if not os.path.exists(DB_PATH):
        print("Database not found.")
        sys.exit(0)

    conn = duckdb.connect(DB_PATH)

    tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
    if "aws_li_to_gcp_li" not in tables:
        print("ERROR: aws_li_to_gcp_li table missing — Phase 2 did not complete.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 1. Detect candidate passthroughs for agent review                   #
    # All workload passthrough rows above the materiality threshold are   #
    # flagged — no hardcoded service list. The agent decides which are    #
    # legitimate (AWS Support, Marketplace, truly no GCP equivalent) and  #
    # which need a real mapping. Rows that static mappers intentionally   #
    # stamped "no GCP/GCS equivalent" are excluded automatically.        #
    # ------------------------------------------------------------------ #

    note_exclude = intentional_passthrough_exclude_clause("m.projection_note")
    illegal_rows = conn.execute(f"""
        SELECT c.aws_li_key, c.product, c.usage_type, c.operation,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region,
               m.gcp_service, m.gcp_sku_name, m.gcp_sku_id, m.projection_note
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy = 'passthrough'
          AND c.is_workload
          AND c.aws_amortized_cost >= {_PASSTHROUGH_MATERIALITY_USD}
          AND {note_exclude}
        ORDER BY c.aws_amortized_cost DESC
    """).fetchall()

    # ------------------------------------------------------------------ #
    # 2. Detect spec violations (break_down multiplier wrong)              #
    # ------------------------------------------------------------------ #

    SKILL_DIR_PATH = os.environ.get("SKILL_DIR", "")
    CATALOG_DB = os.path.join(SKILL_DIR_PATH, "data", "catalog.duckdb")
    spec_violations = []

    if os.path.exists(CATALOG_DB):
        try:
            conn.execute(f"ATTACH '{CATALOG_DB}' AS catalog (READ_ONLY)")
            spec_violations = conn.execute("""
                WITH gcp_caps AS (
                    SELECT
                        aws_li_key,
                        MAX(CASE WHEN component = 'core' THEN unit_multiplier ELSE 0.0 END) AS gcp_vcpu,
                        MAX(CASE WHEN component = 'ram'  THEN unit_multiplier ELSE 0.0 END) AS gcp_ram,
                        MAX(CASE WHEN component = 'core' THEN gcp_sku_id ELSE NULL END) AS core_sku
                    FROM aws_li_to_gcp_li
                    WHERE strategy IN ('map', 'break_down')
                    GROUP BY aws_li_key
                )
                SELECT
                    c.aws_li_key, c.instance_type,
                    c.instance_vcpus, c.instance_ram_gb,
                    g.gcp_vcpu, g.gcp_ram,
                    cat.description, c.gcp_region,
                    m.gcp_service, m.gcp_sku_name
                FROM aws_li_catalog c
                JOIN gcp_caps g USING (aws_li_key)
                JOIN catalog.skus cat ON cat.sku_id = g.core_sku
                JOIN aws_li_to_gcp_li m
                  ON m.aws_li_key = c.aws_li_key AND m.component = 'core'
                -- shared-core SKUs are identified dynamically from the catalog:
                -- GCP marks burstable/shared-core instances with "shared" in the
                -- description or via the resource_group field — no hardcoded name list.
                WHERE c.instance_vcpus IS NOT NULL AND c.instance_ram_gb IS NOT NULL
                  AND (
                    ((g.gcp_ram / g.gcp_vcpu) < (c.instance_ram_gb / c.instance_vcpus) AND g.gcp_vcpu > 0)
                    OR
                    (c.instance_type NOT LIKE 't%' AND (
                        cat.description ILIKE '%shared-core%'
                        OR cat.description ILIKE '%shared core%'
                        OR cat.resource_group ILIKE '%SharedCore%'
                        OR cat.resource_group ILIKE '%Micro%'
                    ))
                  )
            """).fetchall()
            conn.execute("DETACH catalog")
        except Exception as e:
            print(f"Warning: catalog spec check skipped: {e}")

    # ------------------------------------------------------------------ #
    # 3. Detect mapped rows with no SKU (rate-fill will produce $0)        #
    # ------------------------------------------------------------------ #
    no_sku_rows = conn.execute("""
        SELECT c.aws_li_key, c.product, c.usage_type,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region, m.gcp_service, m.gcp_sku_name, m.component, m.strategy
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy IN ('map', 'break_down')
          AND (m.gcp_sku_id IS NULL OR m.gcp_sku_id = '')
          AND c.is_workload
          AND c.aws_amortized_cost >= {threshold}
        ORDER BY c.aws_amortized_cost DESC
    """.format(threshold=_PASSTHROUGH_MATERIALITY_USD)).fetchall()

    # ------------------------------------------------------------------ #
    # 4. Detect zero / null unit_multiplier on mapped rows (silent $0)     #
    # ------------------------------------------------------------------ #
    zero_mult_rows = conn.execute("""
        SELECT c.aws_li_key, c.product, c.usage_type,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region, m.gcp_service, m.gcp_sku_name,
               m.component, m.unit_multiplier, m.strategy
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy IN ('map', 'break_down')
          AND (m.unit_multiplier IS NULL OR m.unit_multiplier <= 0)
          AND c.is_workload
          AND c.aws_amortized_cost >= {threshold}
        ORDER BY c.aws_amortized_cost DESC
    """.format(threshold=_PASSTHROUGH_MATERIALITY_USD)).fetchall()

    conn.close()

    # ------------------------------------------------------------------ #
    # 5. Compute candidates                                                #
    # ------------------------------------------------------------------ #

    candidates = {}

    # Illegal passthrough candidates: try resolve_sku
    for row in illegal_rows:
        key = row[0]
        gcp_service  = row[6] or ""
        gcp_sku_name = row[7] or ""
        region       = row[5] or "us-central1"

        candidate  = None
        confidence = "NONE"

        if gcp_service and gcp_sku_name:
            try:
                # _strict_resolve_sku, not resolve_sku — a row a static mapper
                # already correctly left as passthrough because
                # cheapest_in_scope()/_family_hourly_rate() found no rate in
                # this EXACT region (e.g. an ARM EC2 row with no GCP ARM
                # family available in-region) still carries its intended
                # gcp_service/gcp_sku_name, so it matches this "illegal
                # passthrough" heuristic by name alone. Plain resolve_sku()
                # would then continent-fallback to a DIFFERENT region's SKU
                # and "fix" this row back into a silently wrong-region price
                # — undoing that mapper's correct decision. Confirmed real:
                # this is exactly what happened for an ARM row in Delhi
                # resolving to Taiwan's C4A Arm SKU/price.
                sku_id = _strict_resolve_sku(gcp_service, gcp_sku_name, region)
                if sku_id:
                    candidate = {
                        "action": "set_sku_and_map",
                        "gcp_sku_id": sku_id,
                        "gcp_sku_name": gcp_sku_name,
                        "gcp_service": gcp_service,
                    }
                    confidence = "HIGH"
                else:
                    confidence = "LOW"
            except Exception:
                confidence = "LOW"

        candidates[key] = {
            "type": "illegal_passthrough",
            "confidence": confidence,
            "candidate": candidate,
        }

    # Spec violation candidates: correct multipliers from instance spec
    for row in spec_violations:
        key = row[0]
        instance_vcpus  = row[2]
        instance_ram_gb = row[3]
        candidates[key] = {
            "type": "spec_violation",
            "confidence": "HIGH",
            "candidate": {
                "action": "fix_multipliers",
                "core_multiplier": float(instance_vcpus) if instance_vcpus is not None else None,
                "ram_multiplier":  float(instance_ram_gb) if instance_ram_gb is not None else None,
            },
        }

    # No-SKU rows: no pre-computed candidate — agent must supply gcp_sku_id via override
    for row in no_sku_rows:
        key = row[0]
        if key not in candidates:  # spec_violation or passthrough may already cover this key
            candidates[key] = {
                "type": "no_sku",
                "confidence": "LOW",
                "candidate": None,
            }

    # Zero/null multiplier rows: no pre-computed candidate — agent must supply correct value
    for row in zero_mult_rows:
        key = row[0]
        if key not in candidates:
            candidates[key] = {
                "type": "zero_multiplier",
                "confidence": "LOW",
                "candidate": None,
            }

    # ------------------------------------------------------------------ #
    # 4. Write review_candidates.json                                      #
    # ------------------------------------------------------------------ #

    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)

    # ------------------------------------------------------------------ #
    # 5. Write review_flags.md                                             #
    # ------------------------------------------------------------------ #

    total_flags = len(illegal_rows) + len(spec_violations) + len(no_sku_rows) + len(zero_mult_rows)

    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        f.write("# Phase 3 Review Flags\n\n")
        f.write(f"Total flags: **{total_flags}** "
                f"({len(illegal_rows)} passthrough, {len(spec_violations)} spec violations, "
                f"{len(no_sku_rows)} no-SKU, {len(zero_mult_rows)} zero-multiplier)\n\n")

        if total_flags == 0:
            f.write("No issues detected. Return `[]` (empty array).\n")
            print("auto_review: 0 flags — no issues.")
            return

        f.write("Return `review_fixes.json` — a JSON array:\n")
        f.write('```json\n[{"aws_li_key": "...", "decision": "confirm|override|veto",\n'
                '  "gcp_sku_id": "...", "gcp_sku_name": "...",\n'
                '  "unit_multiplier": 4.0, "component": "core", "reason": "..."}]\n```\n\n')
        f.write("- `confirm`: apply the pre-computed candidate as-is\n")
        f.write("- `override`: supply your own values\n")
        f.write("- `veto`: skip (document why in reason)\n\n")
        f.write("---\n\n")

        if illegal_rows:
            f.write("## Passthrough Rows Requiring Review\n\n")
            f.write("These workload rows are set to passthrough but were not intentionally stamped "
                    "by a static mapper as having no GCP equivalent. For each row: confirm a "
                    "pre-computed candidate, supply your own SKU via override, or veto if it is "
                    "genuinely a valid passthrough (AWS Support, Marketplace, no GCP equivalent).\n\n")
            for row in illegal_rows:
                key = row[0]
                cand_info = candidates.get(key, {})
                conf = cand_info.get("confidence", "NONE")
                cand = cand_info.get("candidate")

                f.write(f"### `{key}` — Confidence: `{conf}`\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **Operation**: {row[3]}\n")
                f.write(f"- **AWS cost**: ${row[4]}\n")
                f.write(f"- **Current gcp_service**: {row[6]!r}\n")
                f.write(f"- **Current gcp_sku_name**: {row[7]!r}\n")
                if row[9]:
                    f.write(f"- **projection_note**: {row[9]}\n")

                if cand:
                    f.write(f"\n**Candidate** (`{conf}`): "
                            f"set gcp_sku_id=`{cand['gcp_sku_id']}`, "
                            f"gcp_sku_name=`{cand['gcp_sku_name']}`, strategy=`map`\n\n")
                else:
                    f.write("\n**No candidate found** — provide gcp_sku_id and gcp_sku_name in override.\n\n")

        if spec_violations:
            f.write("---\n\n")
            f.write("## Spec Violations\n\n")
            f.write("These break_down rows have wrong unit_multipliers. "
                    "Correct values (HIGH confidence) are from the instance spec.\n\n")
            for row in spec_violations:
                key = row[0]
                f.write(f"### `{key}` — Confidence: `HIGH`\n\n")
                f.write(f"- **Instance**: {row[1]}\n")
                f.write(f"- **AWS spec**: {row[2]} vCPU, {row[3]} GB RAM\n")
                f.write(f"- **Current GCP mapping**: {row[4]} vCPU, {row[5]} GB RAM\n")
                f.write(f"- **SKU description**: {row[6]}\n")
                f.write(f"\n**Candidate**: set core unit_multiplier={row[2]}, "
                        f"ram unit_multiplier={row[3]}\n\n")

        if no_sku_rows:
            f.write("---\n\n")
            f.write("## Mapped Rows With No SKU (silent $0 risk)\n\n")
            f.write("These rows have strategy='map'/'break_down' but no gcp_sku_id. "
                    "Rate-fill cannot price them — they will produce $0 GCP cost. "
                    "For each row: use `override` with the correct gcp_sku_id, or `veto` "
                    "if the row should be passthrough.\n\n")
            for row in no_sku_rows:
                key = row[0]
                f.write(f"### `{key}` — No SKU\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **AWS cost**: ${row[3]}\n")
                f.write(f"- **Region**: {row[4]}\n")
                f.write(f"- **gcp_service**: {row[5]!r}\n")
                f.write(f"- **gcp_sku_name**: {row[6]!r}\n")
                f.write(f"- **component**: {row[7]}, **strategy**: {row[8]}\n")
                f.write("\n**Action required**: override with correct gcp_sku_id, or veto.\n\n")

        if zero_mult_rows:
            f.write("---\n\n")
            f.write("## Zero / Null unit_multiplier (silent $0 risk)\n\n")
            f.write("These mapped rows have unit_multiplier = 0 or NULL. "
                    "GCP cost = usage × multiplier × rate, so a zero multiplier produces $0 regardless of rate. "
                    "For each row: use `override` with the correct unit_multiplier value.\n\n")
            for row in zero_mult_rows:
                key = row[0]
                f.write(f"### `{key}` — Zero Multiplier\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **AWS cost**: ${row[3]}\n")
                f.write(f"- **Region**: {row[4]}\n")
                f.write(f"- **gcp_service**: {row[5]!r}, **gcp_sku_name**: {row[6]!r}\n")
                f.write(f"- **component**: {row[7]}, **current unit_multiplier**: {row[8]}\n")
                f.write("\n**Action required**: override with correct unit_multiplier.\n\n")

    print(f"auto_review: {len(illegal_rows)} passthrough, {len(spec_violations)} spec violations, "
          f"{len(no_sku_rows)} no-SKU, {len(zero_mult_rows)} zero-multiplier. "
          f"Total {total_flags} flags.")


if __name__ == "__main__":
    main()
