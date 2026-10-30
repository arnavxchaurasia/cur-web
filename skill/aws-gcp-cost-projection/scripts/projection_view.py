#!/usr/bin/env python3
"""
projection_view.py — single source of truth for the gcp_projection VIEW.

The VIEW derives per-line-item GCP cost (OnDemand / 1yr / 3yr CUD) from
aws_li_catalog × aws_li_to_gcp_li × gcp_sku_rates. It is created as soon as
rates exist (end of Phase 4 / apply_rates.py) so the Phase-4 gate and the
validator autofix can query it, and re-created idempotently in Phase 5
(detect_outliers.py). Both callers import create_projection_view() from here so
the SQL never drifts between phases.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_assumptions

_ASSUMPTIONS = load_assumptions()


def compute_dominant_region(conn) -> str:
    """Return the customer's dominant region (most common non-global gcp_region or aws_region)."""
    try:
        cols = {r[1].lower() for r in conn.execute("PRAGMA table_info('aws_li_catalog')").fetchall()}
        region_expr = None
        if "gcp_region" in cols and "aws_region" in cols:
            region_expr = "COALESCE(NULLIF(gcp_region, ''), NULLIF(aws_region, ''))"
        elif "gcp_region" in cols:
            region_expr = "NULLIF(gcp_region, '')"
        elif "aws_region" in cols:
            region_expr = "NULLIF(aws_region, '')"

        if region_expr:
            row = conn.execute(f"""
                SELECT {region_expr} AS reg, COUNT(*) as cnt
                FROM aws_li_catalog
                WHERE {region_expr} IS NOT NULL
                  AND lower({region_expr}) NOT IN ('global', 'unknown', '')
                GROUP BY reg
                ORDER BY cnt DESC, reg ASC
                LIMIT 1
            """).fetchone()
            if row and row[0]:
                return row[0].strip()
    except Exception:
        pass
    return "us-central1"


def classify_commitment_basis(pricing_model: str | None, usage_type: str | None = None, operation: str | None = None, line_item_type: str | None = None, product: str | None = None) -> str:
    """Classify row into on_demand, spot, committed_1yr, or committed_3yr.
    Commitment charges/fees (RIFee, SavingsPlanRecurringFee, hourly commitment) return on_demand.
    """
    lit = (line_item_type or "").lower().strip()
    prod = (product or "").lower().strip()
    pm = (pricing_model or "").lower().strip()
    ut = (usage_type or "").lower().strip()
    op = (operation or "").lower().strip()
    text = f"{prod} {ut} {op}"

    # C1: A commitment charge (fee) is not discounted usage — never apply a divisor
    if (
        lit in ("rifee", "savingsplanrecurringfee")
        or "hourly commitment" in op
        or "hourly commitment" in ut
        or "recurring fee" in op
        or "recurring fee" in ut
        or "reserved instance fee" in op
        or "reserved instance fee" in ut
        or ("savings plan" in prod and "hourly commitment" in text)
    ):
        return "on_demand"

    if pm == "spot" or "spot" in ut or "spot" in op:
        return "spot"

    is_committed = (
        pm in ("committed", "reserved")
        or lit == "discountedusage"
        or "reserved" in ut
        or "reserved" in op
        or "savings plan" in ut
        or "savingsplan" in ut
        or "savings plan" in op
        or "savingsplan" in op
    )
    if is_committed:
        if re.search(r'\b(3-year|36\s*mo|36\s*month)\b|3\s+yr|3\s+year|3yr', text):
            return "committed_3yr"
        return "committed_1yr"

    return "on_demand"


def _build_projection_view_sql(dominant_region: str, assumptions: dict | None = None, catalog_cols: set[str] | None = None) -> str:
    dom = re.sub(r'[^a-zA-Z0-9-]', '', str(dominant_region)) or "us-central1"
    assump = assumptions or _ASSUMPTIONS or {}
    spot_disc = float(assump.get("aws_spot_discount_pct", 0.70))
    ri1_disc = float(assump.get("aws_ri_1yr_discount_pct", 0.38))
    ri3_disc = float(assump.get("aws_ri_3yr_discount_pct", 0.52))
    sp_disc = float(assump.get("aws_sp_discount_pct", 0.30))

    spot_div = (1.0 - spot_disc) if spot_disc < 1.0 else 1.0
    if spot_div <= 0:
        spot_div = 1.0
    ri1_div = (1.0 - ri1_disc) if ri1_disc < 1.0 else 1.0
    if ri1_div <= 0:
        ri1_div = 1.0
    ri3_div = (1.0 - ri3_disc) if ri3_disc < 1.0 else 1.0
    if ri3_div <= 0:
        ri3_div = 1.0
    sp_div = (1.0 - sp_disc) if sp_disc < 1.0 else 1.0
    if sp_div <= 0:
        sp_div = 1.0

    cols = {c.lower() for c in (catalog_cols or set())}
    has_ut = "usage_type" in cols if cols else True
    has_op = "operation" in cols if cols else True
    has_aws_od = "aws_od_cost" in cols if cols else False
    has_od_basis = "od_basis" in cols if cols else False
    ut_expr = "COALESCE(c.usage_type, '')" if has_ut else "''"
    op_expr = "COALESCE(c.operation, '')" if has_op else "''"
    text_expr = f"(LOWER({ut_expr} || ' ' || {op_expr}))"

    reconstructed_od_expr = f"""CASE
          WHEN LOWER(COALESCE(c.line_item_type, '')) IN ('rifee', 'savingsplanrecurringfee')
            OR {op_expr} ILIKE '%hourly commitment%'
            OR {ut_expr} ILIKE '%hourly commitment%'
            OR {op_expr} ILIKE '%recurring fee%'
            OR {ut_expr} ILIKE '%recurring fee%'
            OR {op_expr} ILIKE '%reserved instance fee%'
            OR {ut_expr} ILIKE '%reserved instance fee%'
            OR (LOWER(COALESCE(c.product, '')) ILIKE '%savings plan%' AND {text_expr} ILIKE '%hourly commitment%')
            THEN c.aws_amortized_cost
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'spot'
            OR {ut_expr} ILIKE '%spot%'
            OR {op_expr} ILIKE '%spot%'
            THEN c.aws_amortized_cost / {spot_div}
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'committed'
            OR LOWER(COALESCE(c.pricing_model, '')) = 'reserved'
            OR LOWER(COALESCE(c.line_item_type, '')) = 'discountedusage'
            OR {ut_expr} ILIKE '%reserved%'
            OR {op_expr} ILIKE '%reserved%'
            OR {ut_expr} ILIKE '%savings%plan%'
            OR {op_expr} ILIKE '%savings%plan%'
            THEN
              CASE
                WHEN REGEXP_MATCHES({text_expr}, '\\b(3-year|36\\s*mo|36\\s*month)\\b|3\\s+yr|3\\s+year|3yr')
                  THEN c.aws_amortized_cost / {ri3_div}
                ELSE c.aws_amortized_cost / {ri1_div}
              END
          ELSE c.aws_amortized_cost
        END"""

    od_cost_expr = f"COALESCE(c.aws_od_cost, {reconstructed_od_expr})" if has_aws_od else reconstructed_od_expr

    od_basis_expr = (
        f"""COALESCE(c.od_basis,
          CASE
            WHEN LOWER(COALESCE(c.pricing_model, '')) = 'spot'
              OR {ut_expr} ILIKE '%spot%'
              OR {op_expr} ILIKE '%spot%'
              THEN 'reconstructed_default'
            WHEN LOWER(COALESCE(c.pricing_model, '')) IN ('committed', 'reserved')
              OR LOWER(COALESCE(c.line_item_type, '')) = 'discountedusage'
              OR {ut_expr} ILIKE '%reserved%'
              OR {op_expr} ILIKE '%reserved%'
              OR {ut_expr} ILIKE '%savings%plan%'
              OR {op_expr} ILIKE '%savings%plan%'
              THEN 'reconstructed_default'
            WHEN LOWER(COALESCE(c.pricing_model, '')) = 'ondemand'
              AND LOWER(COALESCE(c.line_item_type, '')) = 'usage'
              THEN 'public_od'
            ELSE 'amortized_fallback'
          END
        )""" if has_od_basis else f"""CASE
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'spot'
            OR {ut_expr} ILIKE '%spot%'
            OR {op_expr} ILIKE '%spot%'
            THEN 'reconstructed'
          WHEN LOWER(COALESCE(c.pricing_model, '')) IN ('committed', 'reserved')
            OR LOWER(COALESCE(c.line_item_type, '')) = 'discountedusage'
            OR {ut_expr} ILIKE '%reserved%'
            OR {op_expr} ILIKE '%reserved%'
            OR {ut_expr} ILIKE '%savings%plan%'
            OR {op_expr} ILIKE '%savings%plan%'
            THEN 'reconstructed'
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'ondemand'
            AND LOWER(COALESCE(c.line_item_type, '')) = 'usage'
            THEN 'public_od'
          ELSE 'amortized_fallback'
        END"""
    )
    return f"""
CREATE OR REPLACE VIEW gcp_projection AS
-- passthrough double-count guard: a passthrough's AWS cost belongs to the LINE ITEM,
-- not each component. If a passthrough row is split into N components (e.g. core+ram),
-- assigning the full aws_amortized_cost to every component and SUMing would N-multiply
-- the cost (confirmed real: SageMaker row AWS $1413.56 -> $2827.12, exactly 2x). Rank
-- components per aws_li_key by rowid; only rn=1 carries the cost, the rest carry $0, so
-- SUM() reconstructs the AWS cost exactly once. (rowid, not component label, keeps the
-- join back 1:1 — two rows can share a component label.)
WITH passthrough_rank AS (
  SELECT m.rowid AS row_id, m.aws_li_key,
         ROW_NUMBER() OVER (PARTITION BY m.aws_li_key ORDER BY m.rowid) AS rn
  FROM aws_li_to_gcp_li m
  WHERE m.strategy IN ('passthrough', 'needs_reprice')
),
od_pick AS (
  SELECT m.aws_li_key, m.gcp_sku_id,
         COALESCE(
           MAX(CASE WHEN r.region = c.gcp_region THEN r.rate_usd END),
           MAX(CASE WHEN r.region = 'global'     THEN r.rate_usd END),
           MAX(CASE WHEN r.region = '{dom}'      THEN r.rate_usd END)
         ) AS rate_usd,
         MAX(CASE
           WHEN (c.gcp_region = 'global' OR c.gcp_region IS NULL OR c.gcp_region = ''
                 OR c.aws_region = 'global' OR c.aws_region IS NULL OR c.aws_region = '')
            AND NOT EXISTS (
                SELECT 1 FROM gcp_sku_rates r_glob
                WHERE r_glob.gcp_sku_id = m.gcp_sku_id
                  AND r_glob.region = 'global'
                  AND r_glob.pricing_type = 'OnDemand'
            )
            AND EXISTS (
                SELECT 1 FROM gcp_sku_rates r_dom
                WHERE r_dom.gcp_sku_id = m.gcp_sku_id
                  AND r_dom.region = '{dom}'
                  AND r_dom.pricing_type = 'OnDemand'
            )
           THEN 'global charge; priced at {dom} rate'
           WHEN c.gcp_region IS NOT NULL AND c.gcp_region != '' AND c.gcp_region != 'global'
            AND NOT EXISTS (
                SELECT 1 FROM gcp_sku_rates r_reg
                WHERE r_reg.gcp_sku_id = m.gcp_sku_id
                  AND r_reg.region = c.gcp_region
                  AND r_reg.pricing_type = 'OnDemand'
            )
            AND NOT EXISTS (
                SELECT 1 FROM gcp_sku_rates r_glob2
                WHERE r_glob2.gcp_sku_id = m.gcp_sku_id
                  AND r_glob2.region = 'global'
                  AND r_glob2.pricing_type = 'OnDemand'
            )
            AND EXISTS (
                SELECT 1 FROM gcp_sku_rates r_dom2
                WHERE r_dom2.gcp_sku_id = m.gcp_sku_id
                  AND r_dom2.region = '{dom}'
                  AND r_dom2.pricing_type = 'OnDemand'
            )
           THEN 'priced at {dom} rate — no ' || c.gcp_region || ' rate'
           WHEN c.gcp_region IS NOT NULL AND c.gcp_region != ''
            AND NOT EXISTS (
                SELECT 1 FROM gcp_sku_rates r_reg2
                WHERE r_reg2.gcp_sku_id = m.gcp_sku_id
                  AND r_reg2.region IN (c.gcp_region, 'global', '{dom}')
                  AND r_reg2.pricing_type = 'OnDemand'
            )
           THEN 'no rate in ' || c.gcp_region || '/global/{dom}; unpriced'
           ELSE NULL
         END) AS dominant_note
  FROM   aws_li_to_gcp_li m
  JOIN   aws_li_catalog   c USING (aws_li_key)
  LEFT JOIN gcp_sku_rates r ON r.gcp_sku_id = m.gcp_sku_id
                            AND r.pricing_type = 'OnDemand'
  GROUP BY m.aws_li_key, m.gcp_sku_id
),
c1_pick AS (
  SELECT m.aws_li_key, m.gcp_sku_id,
         COALESCE(
           MAX(CASE WHEN r.region = c.gcp_region THEN r.rate_usd END),
           MAX(CASE WHEN r.region = 'global'     THEN r.rate_usd END),
           MAX(CASE WHEN r.region = '{dom}'      THEN r.rate_usd END)
         ) AS rate_usd
  FROM   aws_li_to_gcp_li m
  JOIN   aws_li_catalog   c USING (aws_li_key)
  LEFT JOIN gcp_sku_rates r ON r.gcp_sku_id = m.gcp_sku_id
                            AND r.pricing_type = 'Commit1Yr'
  GROUP BY m.aws_li_key, m.gcp_sku_id
),
c3_pick AS (
  SELECT m.aws_li_key, m.gcp_sku_id,
         COALESCE(
           MAX(CASE WHEN r.region = c.gcp_region THEN r.rate_usd END),
           MAX(CASE WHEN r.region = 'global'     THEN r.rate_usd END),
           MAX(CASE WHEN r.region = '{dom}'      THEN r.rate_usd END)
         ) AS rate_usd
  FROM   aws_li_to_gcp_li m
  JOIN   aws_li_catalog   c USING (aws_li_key)
  LEFT JOIN gcp_sku_rates r ON r.gcp_sku_id = m.gcp_sku_id
                            AND r.pricing_type = 'Commit3Yr'
  GROUP BY m.aws_li_key, m.gcp_sku_id
)
SELECT  c.aws_li_key, c.product, c.aws_region, c.gcp_region,
        c.line_item_type, c.pricing_model, c.is_workload,
        c.total_usage, c.aws_amortized_cost,
        m.strategy, m.gcp_service, m.gcp_sku_id, m.component,
        m.unit_multiplier, m.passthrough_reason, m.decided_by,
        m.rate_region_basis, m.rate_region_used, m.license_status, m.review_flags,
        CASE
          WHEN LOWER(COALESCE(c.line_item_type, '')) IN ('rifee', 'savingsplanrecurringfee')
            OR {op_expr} ILIKE '%hourly commitment%'
            OR {ut_expr} ILIKE '%hourly commitment%'
            OR {op_expr} ILIKE '%recurring fee%'
            OR {ut_expr} ILIKE '%recurring fee%'
            OR {op_expr} ILIKE '%reserved instance fee%'
            OR {ut_expr} ILIKE '%reserved instance fee%'
            OR (LOWER(COALESCE(c.product, '')) ILIKE '%savings plan%' AND {text_expr} ILIKE '%hourly commitment%')
            THEN 'on_demand'
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'spot'
            OR {ut_expr} ILIKE '%spot%'
            OR {op_expr} ILIKE '%spot%'
            THEN 'spot'
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'committed'
            OR LOWER(COALESCE(c.pricing_model, '')) = 'reserved'
            OR LOWER(COALESCE(c.line_item_type, '')) = 'discountedusage'
            OR {ut_expr} ILIKE '%reserved%'
            OR {op_expr} ILIKE '%reserved%'
            OR {ut_expr} ILIKE '%savings%plan%'
            OR {op_expr} ILIKE '%savings%plan%'
            THEN
              CASE
                WHEN REGEXP_MATCHES({text_expr}, '\\b(3-year|36\\s*mo|36\\s*month)\\b|3\\s+yr|3\\s+year|3yr')
                  THEN 'committed_3yr'
                ELSE 'committed_1yr'
              END
          ELSE 'on_demand'
        END AS commitment_basis,
        {od_cost_expr} AS aws_cost_od_est,
        {od_cost_expr} AS aws_od_cost,
        {od_basis_expr} AS od_basis,
        CASE
          WHEN LOWER(COALESCE(c.line_item_type, '')) IN ('rifee', 'savingsplanrecurringfee')
            OR {op_expr} ILIKE '%hourly commitment%'
            OR {ut_expr} ILIKE '%hourly commitment%'
            OR {op_expr} ILIKE '%recurring fee%'
            OR {ut_expr} ILIKE '%recurring fee%'
            OR {op_expr} ILIKE '%reserved instance fee%'
            OR {ut_expr} ILIKE '%reserved instance fee%'
            OR (LOWER(COALESCE(c.product, '')) ILIKE '%savings plan%' AND {text_expr} ILIKE '%hourly commitment%')
            THEN m.projection_note
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'spot'
            OR {ut_expr} ILIKE '%spot%'
            OR {op_expr} ILIKE '%spot%'
            THEN CONCAT(
                   NULLIF(m.projection_note || '; ', '; '),
                   'AWS list price estimated from the billed Spot rate using an assumed ' || CAST(ROUND({spot_disc} * 100) AS INTEGER) || '% Spot discount.'
                 )
          WHEN LOWER(COALESCE(c.pricing_model, '')) = 'committed'
            OR LOWER(COALESCE(c.pricing_model, '')) = 'reserved'
            OR LOWER(COALESCE(c.line_item_type, '')) = 'discountedusage'
            OR {ut_expr} ILIKE '%reserved%'
            OR {op_expr} ILIKE '%reserved%'
            OR {ut_expr} ILIKE '%savings%plan%'
            OR {op_expr} ILIKE '%savings%plan%'
            THEN
              CASE
                WHEN REGEXP_MATCHES({text_expr}, '\\b(3-year|36\\s*mo|36\\s*month)\\b|3\\s+yr|3\\s+year|3yr')
                THEN CONCAT(
                       NULLIF(m.projection_note || '; ', '; '),
                       'AWS list price estimated from the billed reserved rate using an assumed ' || CAST(ROUND({ri3_disc} * 100) AS INTEGER) || '% 3-year RI discount.'
                     )
                ELSE CONCAT(
                       NULLIF(m.projection_note || '; ', '; '),
                       'AWS list price estimated from the billed reserved rate using an assumed ' || CAST(ROUND({ri1_disc} * 100) AS INTEGER) || '% 1-year RI discount (term not stated on the bill).'
                     )
              END
          ELSE m.projection_note
        END AS projection_note,
        CASE m.strategy
          WHEN 'ignore'         THEN 0
          WHEN 'outlier_triage' THEN NULL
          WHEN 'passthrough'    THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          WHEN 'needs_reprice'  THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) * od.rate_usd
        END AS gcp_projected_cost,
        CASE m.strategy
          WHEN 'ignore'         THEN 0
          WHEN 'outlier_triage' THEN NULL
          WHEN 'passthrough'    THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          WHEN 'needs_reprice'  THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) *
               CASE WHEN c.pricing_model = 'Spot'
                    THEN od.rate_usd
                    ELSE COALESCE(c1.rate_usd, od.rate_usd)
               END
        END AS gcp_cost_1yr_cud,
        CASE m.strategy
          WHEN 'ignore'         THEN 0
          WHEN 'outlier_triage' THEN NULL
          WHEN 'passthrough'    THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          WHEN 'needs_reprice'  THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN {od_cost_expr} ELSE 0 END
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) *
               CASE WHEN c.pricing_model = 'Spot'
                    THEN od.rate_usd
                    ELSE COALESCE(c3.rate_usd, od.rate_usd)
               END
        END AS gcp_cost_3yr_cud

FROM    aws_li_catalog c
LEFT JOIN aws_li_to_gcp_li m ON m.aws_li_key = c.aws_li_key
LEFT JOIN od_pick od ON od.aws_li_key = m.aws_li_key AND od.gcp_sku_id = m.gcp_sku_id
LEFT JOIN c1_pick c1 ON c1.aws_li_key = m.aws_li_key AND c1.gcp_sku_id = m.gcp_sku_id
LEFT JOIN c3_pick c3 ON c3.aws_li_key = m.aws_li_key AND c3.gcp_sku_id = m.gcp_sku_id
LEFT JOIN passthrough_rank pr ON pr.row_id = m.rowid;
"""

_PROJECTION_VIEW_SQL = _build_projection_view_sql("us-central1")


def create_projection_view(conn, dominant_region=None, assumptions=None):
    """(Re)create the gcp_projection VIEW with dominant region fallback.
    Requires aws_li_catalog, aws_li_to_gcp_li, and gcp_sku_rates to exist. Idempotent.
    """
    if dominant_region is None:
        dominant_region = compute_dominant_region(conn)
    clean_reg = re.sub(r'[^a-zA-Z0-9-]', '', str(dominant_region)) or "us-central1"

    conn.execute("""
        SELECT table_type FROM information_schema.tables WHERE table_name = 'gcp_projection'
    """)
    row = conn.fetchone()
    if row and row[0] == "BASE TABLE":
        conn.execute("DROP TABLE gcp_projection")

    # Defensive schema migration: ensure required columns exist on aws_li_to_gcp_li
    try:
        import row_state
        row_state.ensure_schema(conn)
    except Exception:
        pass

    cat_cols = set()
    try:
        cat_cols = {r[1].lower() for r in conn.execute("PRAGMA table_info('aws_li_catalog')").fetchall()}
    except Exception:
        pass

    conn.execute(_build_projection_view_sql(clean_reg, assumptions, cat_cols))
