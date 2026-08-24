#!/usr/bin/env bash
# Search the bundled SKU catalog via the pre-indexed catalog.duckdb.
# Output (default):  TSV — service<TAB>skuId<TAB>resource_group<TAB>usage_type<TAB>regions<TAB>description<TAB>rate_usd<TAB>unit
# Output (--all-tiers): CSV — tier_start,rate_usd (all pricing tiers for one SKU;
#   use this to compute blended rates for tiered services like Cloud Storage,
#   BigQuery, internet egress — see phases/04-rate-fill.md)
#
# Usage:
#   scripts/find-sku.sh --service "Compute Engine" --region asia-southeast1 \
#                       --resource-group CPU --keyword "N2D"
#   scripts/find-sku.sh --keyword "Hyperdisk Balanced" --region asia-southeast1
#   scripts/find-sku.sh --sku-id "8B4E-B458-AD51" --all-tiers
#
# All flags optional. Without --service, all services are scanned.
# Requires: duckdb on PATH, data/catalog.duckdb (run build-catalog-index.sh once).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${SKILL_DIR:-$(dirname "$SCRIPT_DIR")}"
DATA_DIR="$SKILL_DIR/data"
INDEX="$DATA_DIR/catalog.duckdb"

SERVICE=""; REGION=""; KEYWORD=""; USAGE_TYPE=""; RESOURCE_GROUP=""
SKU_ID=""; ALL_TIERS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)        SERVICE="$2";        shift 2 ;;
    --region)         REGION="$2";         shift 2 ;;
    --keyword)        KEYWORD="$2";        shift 2 ;;
    --usage-type)     USAGE_TYPE="$2";     shift 2 ;;
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --sku-id)         SKU_ID="$2";         shift 2 ;;
    --all-tiers)      ALL_TIERS="1";       shift   ;;
    -h|--help) head -20 "$0" | tail -19; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ ! -f "$INDEX" ]]; then
  echo "ERROR: catalog.duckdb not found at $INDEX" >&2
  echo "Run:   bash scripts/build-catalog-index.sh" >&2
  exit 1
fi

# ── --all-tiers mode: return every pricing tier for one SKU ─────────────────
# Used by Phase 4 blended-rate computation on tiered services
# (Cloud Storage, BigQuery, internet egress, Cloud CDN, Cloud Run requests).
if [[ -n "$ALL_TIERS" ]]; then
  if [[ -z "$SKU_ID" ]]; then
    echo "ERROR: --all-tiers requires --sku-id <skuId>" >&2
    exit 1
  fi
  duckdb -csv "$INDEX" \
    "SELECT tier_start, rate_usd
     FROM   tiered_rates
     WHERE  sku_id = '$(echo "$SKU_ID" | sed "s/'/''/g")'
     ORDER BY tier_start"
  exit 0
fi

# ── Standard SKU search mode ─────────────────────────────────────────────────
# Build WHERE predicates (DuckDB SQL, safely quoted)
PREDS="1=1"

if [[ -n "$SERVICE" ]]; then
  SVC_ESC="${SERVICE//\'/\'\'}"
  PREDS+=" AND s.service_name = '$SVC_ESC'"
fi
if [[ -n "$REGION" ]]; then
  RGN_ESC="${REGION//\'/\'\'}"
  # Match explicit region OR global (globally-priced SKUs apply everywhere)
  PREDS+=" AND (list_contains(s.service_regions, '$RGN_ESC') OR list_contains(s.service_regions, 'global'))"
fi
if [[ -n "$KEYWORD" ]]; then
  KW_ESC="${KEYWORD//\'/\'\'}"
  PREDS+=" AND regexp_matches(s.description, '(?i)$KW_ESC')"
fi
if [[ -n "$USAGE_TYPE" ]]; then
  UT_ESC="${USAGE_TYPE//\'/\'\'}"
  PREDS+=" AND s.usage_type = '$UT_ESC'"
fi
if [[ -n "$RESOURCE_GROUP" ]]; then
  RG_ESC="${RESOURCE_GROUP//\'/\'\'}"
  PREDS+=" AND s.resource_group = '$RG_ESC'"
fi
if [[ -n "$SKU_ID" ]]; then
  SK_ESC="${SKU_ID//\'/\'\'}"
  PREDS+=" AND s.sku_id = '$SK_ESC'"
fi

# rate_usd = first non-zero tier (same semantics as old jq script for flat-rate SKUs)
# -csv (not the default box renderer) is required here: duckdb's box output caps
# displayed rows even when stdout is redirected to a file, not just an interactive
# terminal — confirmed live, a 122-row match silently produced only 40 visible rows
# with no truncation indicator. -csv returns every matching row.
duckdb -csv -separator $'\t' "$INDEX" "
SELECT s.service_name,
       s.sku_id,
       s.resource_group,
       s.usage_type,
       array_to_string(s.service_regions, ',') AS regions,
       s.description,
       COALESCE(
           MIN(t.rate_usd) FILTER (WHERE t.rate_usd > 0),
           0.0
       ) AS rate_usd,
       s.usage_unit
FROM   skus s
LEFT JOIN tiered_rates t ON t.sku_id = s.sku_id
WHERE  $PREDS
GROUP BY s.sku_id, s.service_name, s.resource_group, s.usage_type,
         s.service_regions, s.description, s.usage_unit
ORDER BY s.service_name, s.description
"
