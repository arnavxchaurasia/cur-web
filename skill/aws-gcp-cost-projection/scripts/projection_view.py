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

_PROJECTION_VIEW_SQL = """
CREATE OR REPLACE VIEW gcp_projection AS
WITH passthrough_rank AS (
  -- 'passthrough' strategy means "no real per-component GCP price exists,
  -- carry the AWS cost through as the honest baseline" — that AWS cost is a
  -- property of the LINE ITEM, not of each component. When a passthrough row
  -- is split into multiple components (e.g. an LLM emitting core+ram for a
  -- SageMaker instance-hour with no per-instance-type pricer yet), assigning
  -- the full aws_amortized_cost to EVERY component and then SUMing across
  -- components (both here and in render_report.py's per-row/headline totals)
  -- silently doubles (or N-multiplies) the projected cost — confirmed real:
  -- a SageMaker Canvas row showed AWS $1413.56 -> GCP $2827.12, exactly 2x,
  -- because it had 2 components (core+ram), each carrying the full AWS cost.
  -- Rank components per aws_li_key so only the first carries the passthrough
  -- cost; any additional components for the same passthrough line item carry
  -- $0, so SUM() across components always reconstructs the original AWS cost
  -- exactly once, never a multiple of it.
  -- Keyed by rowid, not (aws_li_key, component) — two rows can legitimately
  -- share the same component label (e.g. two 'core' rows for the same key),
  -- and joining back on the label alone would fan out into a many-to-many
  -- join. rowid is always unique per row, so the join back below is always 1:1.
  SELECT m.rowid AS row_id, m.aws_li_key,
         ROW_NUMBER() OVER (PARTITION BY m.aws_li_key ORDER BY m.rowid) AS rn
  FROM aws_li_to_gcp_li m
  WHERE m.strategy = 'passthrough'
),
od_pick AS (
  SELECT m.aws_li_key, m.gcp_sku_id,
         COALESCE(
           MAX(CASE WHEN r.region = c.gcp_region THEN r.rate_usd END),
           MAX(CASE WHEN r.region = 'global'     THEN r.rate_usd END),
           MAX(r.rate_usd)  -- fallback for uniformly-priced SKUs (e.g. KMS) with no global row
         ) AS rate_usd
  FROM   aws_li_to_gcp_li m
  JOIN   aws_li_catalog   c USING (aws_li_key)
  LEFT JOIN gcp_sku_rates r ON r.gcp_sku_id = m.gcp_sku_id
                            AND r.pricing_type = CASE
                                WHEN c.pricing_model = 'Spot' THEN 'Preemptible'
                                ELSE 'OnDemand'
                              END
  GROUP BY m.aws_li_key, m.gcp_sku_id
),
c1_pick AS (
  SELECT m.aws_li_key, m.gcp_sku_id,
         COALESCE(
           MAX(CASE WHEN r.region = c.gcp_region THEN r.rate_usd END),
           MAX(CASE WHEN r.region = 'global'     THEN r.rate_usd END),
           MAX(r.rate_usd)
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
           MAX(r.rate_usd)
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
        m.unit_multiplier, m.projection_note,
        -- unit_multiplier is COALESCEd to 1: a NULL multiplier must not
        -- silently null out the whole cost (x*NULL=NULL) and vanish from
        -- SUM(). rate_usd is deliberately NOT coalesced — a NULL rate is a
        -- genuine coverage gap the gate must catch, not paper over.
        -- AWS's aws_amortized_cost is already RI/Savings-Plan-discounted for
        -- Committed/DiscountedUsage rows. Comparing that against GCP's OnDemand
        -- rate compares a discounted price to a full-price one — a structural
        -- 2-10x over-projection, not a per-row bug (see gcp_cost_1yr_cud below,
        -- which already computes the like-for-like number but previously fed
        -- only a side display column, never the headline total). For these rows
        -- the headline number now uses the GCP 1yr-CUD rate — AWS-committed vs
        -- GCP-committed — falling back to the OD rate only if no CUD rate is
        -- loaded for that SKU, so committed rows are never left uncosted.
        CASE m.strategy
          WHEN 'ignore'      THEN 0
          WHEN 'passthrough' THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN c.aws_amortized_cost ELSE 0 END
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) *
               CASE WHEN c.pricing_model = 'Committed' AND c.line_item_type = 'DiscountedUsage'
                    THEN COALESCE(c1.rate_usd, od.rate_usd)
                    ELSE od.rate_usd
               END
        END AS gcp_projected_cost,
        CASE m.strategy
          WHEN 'ignore'      THEN 0
          WHEN 'passthrough' THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN c.aws_amortized_cost ELSE 0 END
          -- Spot/Preemptible VMs cannot be combined with CUDs on GCP.
          -- Use the preemptible rate (od.rate_usd) for Spot rows.
          -- For non-Spot rows with no CUD catalog entry (c1.rate_usd IS NULL),
          -- return NULL so the report can display "—" instead of the OD rate.
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) *
               CASE WHEN c.pricing_model = 'Spot'
                    THEN od.rate_usd
                    ELSE c1.rate_usd   -- NULL when no CUD rate; propagates NULL upward
               END
        END AS gcp_cost_1yr_cud,
        CASE m.strategy
          WHEN 'ignore'      THEN 0
          WHEN 'passthrough' THEN CASE WHEN COALESCE(pr.rn, 1) = 1 THEN c.aws_amortized_cost ELSE 0 END
          ELSE c.total_usage * COALESCE(m.unit_multiplier, 1) *
               CASE WHEN c.pricing_model = 'Spot'
                    THEN od.rate_usd
                    ELSE c3.rate_usd   -- NULL when no 3yr CUD rate
               END
        END AS gcp_cost_3yr_cud
FROM    aws_li_catalog c
LEFT JOIN aws_li_to_gcp_li m ON m.aws_li_key = c.aws_li_key
LEFT JOIN od_pick od ON od.aws_li_key = m.aws_li_key AND od.gcp_sku_id = m.gcp_sku_id
LEFT JOIN c1_pick c1 ON c1.aws_li_key = m.aws_li_key AND c1.gcp_sku_id = m.gcp_sku_id
LEFT JOIN c3_pick c3 ON c3.aws_li_key = m.aws_li_key AND c3.gcp_sku_id = m.gcp_sku_id
LEFT JOIN passthrough_rank pr ON pr.row_id = m.rowid;
"""


def create_projection_view(conn):
    """(Re)create the gcp_projection VIEW. Requires aws_li_catalog,
    aws_li_to_gcp_li, and gcp_sku_rates to exist. Idempotent."""
    conn.execute(_PROJECTION_VIEW_SQL)
