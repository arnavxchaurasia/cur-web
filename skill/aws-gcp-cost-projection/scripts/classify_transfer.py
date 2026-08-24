from __future__ import annotations
#!/usr/bin/env python3
"""
classify_transfer.py — Deterministic AWS DataTransfer → GCP egress SKU classifier.

Uses pure regex on usage_type, operation, and description. Zero AI judgment.

Usage:
    python3 classify_transfer.py <projection.duckdb>
"""

import re
import sys
import traceback

try:
    import duckdb
except ImportError:
    print("ERROR: duckdb package not installed. Run: pip install duckdb", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Classification rules — evaluated in order; first match wins.
# Each rule is (pattern_fields, regex_pattern, component, resource_group, gcp_sku_id, unit_multiplier, strategy)
# pattern_fields: list of field names to test the regex against (OR logic across fields)
# ---------------------------------------------------------------------------
RULES = [
    # Inbound — free on GCP
    {
        "fields": ["usage_type"],
        "pattern": re.compile(r"DataTransfer-In-Bytes", re.IGNORECASE),
        "component": "egress_inbound_free",
        "resource_group": None,
        "gcp_sku_id": "INTRA_ZONE_FREE",
        "unit_multiplier": 0,
        "strategy": "ignore",
    },
    # Intra-AZ (regional) — free on GCP
    {
        "fields": ["usage_type"],
        "pattern": re.compile(r"DataTransfer-Regional-Bytes", re.IGNORECASE),
        "component": "egress_intra_az_free",
        "resource_group": None,
        "gcp_sku_id": "INTRA_ZONE_FREE",
        "unit_multiplier": 0,
        "strategy": "ignore",
    },
    # Inter-AZ (within region) — matched before generic Out-Bytes
    # AWS description often contains "Inter-AZ" or usage_type may mention it
    {
        "fields": ["usage_type", "operation", "description"],
        "pattern": re.compile(r"Inter-AZ", re.IGNORECASE),
        "component": "egress_inter_zone",
        "resource_group": "InterZoneEgress",
        "gcp_sku_id": None,
        "unit_multiplier": 1,
        "strategy": "map",
    },
    # Cross-region transfer
    {
        "fields": ["usage_type"],
        "pattern": re.compile(r"DataTransfer-CrossRegion(DataTransfer)?", re.IGNORECASE),
        "component": "egress_inter_region",
        "resource_group": "InterRegionEgress",
        "gcp_sku_id": None,
        "unit_multiplier": 1,
        "strategy": "map",
    },
    # CloudFront-Out → treat as internet egress
    {
        "fields": ["usage_type", "operation"],
        "pattern": re.compile(r"CloudFront-Out", re.IGNORECASE),
        "component": "egress_internet",
        "resource_group": "InternetEgress",
        "gcp_sku_id": None,
        "unit_multiplier": 1,
        "strategy": "map",
    },
    # Intra-VPC (EC2-Bytes, VpcEndpoint)
    {
        "fields": ["usage_type", "operation"],
        "pattern": re.compile(r"(EC2-Bytes|VpcEndpoint)", re.IGNORECASE),
        "component": "egress_intra_vpc",
        "resource_group": None,
        "gcp_sku_id": None,
        "unit_multiplier": 1,
        "strategy": "map",
    },
    # Generic internet egress (Out-Bytes not already matched above)
    {
        "fields": ["usage_type"],
        "pattern": re.compile(r"DataTransfer-Out-Bytes", re.IGNORECASE),
        "component": "egress_internet",
        "resource_group": "InternetEgress",
        "gcp_sku_id": None,
        "unit_multiplier": 1,
        "strategy": "map",
    },
]


def classify_row(usage_type: str, operation: str, description: str) -> dict | None:
    """Return the first matching rule dict, or None if no rule matched."""
    field_values = {
        "usage_type": usage_type or "",
        "operation": operation or "",
        "description": description or "",
    }
    for rule in RULES:
        for field in rule["fields"]:
            if rule["pattern"].search(field_values[field]):
                return rule
    return None


def main(db_path: str) -> int:
    try:
        con = duckdb.connect(db_path)
    except Exception as exc:
        print(f"ERROR: Cannot open database {db_path!r}: {exc}", file=sys.stderr)
        return 1

    try:
        # ------------------------------------------------------------------
        # 1. Fetch DataTransfer rows from aws_li_catalog
        # ------------------------------------------------------------------
        try:
            rows = con.execute(
                """
                SELECT
                    aws_li_key,
                    usage_type,
                    operation,
                    operation AS description,
                    product
                FROM aws_li_catalog
                WHERE product ILIKE '%DataTransfer%'
                   OR product ILIKE '%data transfer%'
                """
            ).fetchall()
        except Exception as exc:
            print(f"ERROR: Query on aws_li_catalog failed: {exc}", file=sys.stderr)
            return 1

        if not rows:
            print("No DataTransfer rows found in aws_li_catalog.")
            return 0

        # ------------------------------------------------------------------
        # 2. Fetch existing keys in aws_li_to_gcp_li
        # ------------------------------------------------------------------
        try:
            existing = {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT aws_li_key, strategy FROM aws_li_to_gcp_li"
                ).fetchall()
            }
        except Exception as exc:
            print(f"ERROR: Query on aws_li_to_gcp_li failed: {exc}", file=sys.stderr)
            return 1

        # ------------------------------------------------------------------
        # 3. Classify and apply
        # ------------------------------------------------------------------
        counts: dict[str, int] = {}
        updated = 0
        inserted = 0
        skipped = 0

        for aws_li_key, usage_type, operation, description, product in rows:
            rule = classify_row(usage_type or "", operation or "", description or "")
            if rule is None:
                skipped += 1
                continue

            component = rule["component"]
            counts[component] = counts.get(component, 0) + 1

            if aws_li_key in existing:
                # Only update if the existing row is not already 'ignore'
                if existing[aws_li_key] == "ignore":
                    continue
                try:
                    params = [component]
                    set_clauses = ["component = ?"]

                    if rule["resource_group"] is not None:
                        set_clauses.append("resource_group = ?")
                        params.append(rule["resource_group"])

                    if rule["gcp_sku_id"] is not None:
                        set_clauses.append("gcp_sku_id = ?")
                        params.append(rule["gcp_sku_id"])

                    if rule["unit_multiplier"] is not None:
                        set_clauses.append("unit_multiplier = ?")
                        params.append(rule["unit_multiplier"])

                    params.append(aws_li_key)
                    con.execute(
                        f"UPDATE aws_li_to_gcp_li SET {', '.join(set_clauses)} "
                        f"WHERE aws_li_key = ? AND strategy != 'ignore'",
                        params,
                    )
                    updated += 1
                except Exception as exc:
                    print(
                        f"WARN: UPDATE failed for key {aws_li_key!r}: {exc}",
                        file=sys.stderr,
                    )
            else:
                # Insert new row with deterministic mapping
                try:
                    con.execute(
                        """
                        INSERT INTO aws_li_to_gcp_li (
                            aws_li_key,
                            component,
                            resource_group,
                            gcp_sku_id,
                            unit_multiplier,
                            strategy
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            aws_li_key,
                            component,
                            rule["resource_group"],
                            rule["gcp_sku_id"],
                            rule["unit_multiplier"],
                            rule["strategy"],
                        ],
                    )
                    inserted += 1
                except Exception as exc:
                    print(
                        f"WARN: INSERT failed for key {aws_li_key!r}: {exc}",
                        file=sys.stderr,
                    )

        con.close()

        # ------------------------------------------------------------------
        # 4. Summary
        # ------------------------------------------------------------------
        total = updated + inserted
        print(f"classify_transfer: {total} rows classified ({updated} updated, {inserted} inserted), {skipped} unmatched.")
        print("Breakdown by component:")
        for comp, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {comp}: {n}")

        return 0

    except Exception as exc:
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            con.close()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python3 {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
