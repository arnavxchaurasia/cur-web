#!/usr/bin/env bash
# Refreshes data/services.json and data/skus/*.json.gz from Cloud Billing Catalog API.
# Run when GCP rates change (quarterly is fine; pricing rarely shifts day-to-day).
# Requires: gcloud auth (any GCP-authenticated user — the catalog API is public),
# jq, gzip, GNU bash 4+ (for mapfile).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$SKILL_DIR/data/skus"
SERVICES_OUT="$SKILL_DIR/data/services.json"
META_OUT="$SKILL_DIR/data/CATALOG_META.json"
ALLOW_LIST="$SCRIPT_DIR/firstparty-allowlist.txt"
MAX_CONCURRENCY=10

TOKEN=$(gcloud auth print-access-token)
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: gcloud auth print-access-token returned empty" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

echo ">> Fetching service list..."
curl -sf "https://cloudbilling.googleapis.com/v1/services?pageSize=5000" \
  -H "Authorization: Bearer $TOKEN" -o /tmp/refresh-all-services.json

# Filter to first-party allow-list
jq --slurpfile names <(jq -R . "$ALLOW_LIST" | jq -s .) \
   '[.services[] | select(.displayName as $d | $names[0] | index($d))]' \
   /tmp/refresh-all-services.json > "$SERVICES_OUT"
SVC_COUNT=$(jq 'length' "$SERVICES_OUT")
echo "   $SVC_COUNT first-party services"

# Per-service SKU fetcher
fetch_one() {
  local SVC_ID="$1" SVC_NAME="$2"
  local OUT="$OUT_DIR/${SVC_ID}.json"
  local PAGE_TOKEN="" RESP COUNT
  echo "[]" > "$OUT.tmp"
  while :; do
    if [[ -z "$PAGE_TOKEN" ]]; then
      RESP=$(curl -sf "https://cloudbilling.googleapis.com/v1/services/${SVC_ID}/skus?pageSize=5000" -H "Authorization: Bearer $TOKEN") || { echo "FAIL ${SVC_ID} ${SVC_NAME}"; rm -f "$OUT.tmp"; return 1; }
    else
      RESP=$(curl -sf "https://cloudbilling.googleapis.com/v1/services/${SVC_ID}/skus?pageSize=5000&pageToken=${PAGE_TOKEN}" -H "Authorization: Bearer $TOKEN") || { echo "FAIL ${SVC_ID} ${SVC_NAME}"; rm -f "$OUT.tmp"; return 1; }
    fi
    echo "$RESP" | jq '.skus // []' > "$OUT.tmp.page"
    jq -s '.[0] + .[1]' "$OUT.tmp" "$OUT.tmp.page" > "$OUT.tmp.merged"
    mv "$OUT.tmp.merged" "$OUT.tmp"
    rm -f "$OUT.tmp.page"
    PAGE_TOKEN=$(echo "$RESP" | jq -r '.nextPageToken // empty')
    [[ -z "$PAGE_TOKEN" ]] && break
  done
  COUNT=$(jq 'length' "$OUT.tmp")
  mv "$OUT.tmp" "$OUT"
  gzip -f "$OUT"
  echo "OK ${SVC_ID} ${SVC_NAME} skus=${COUNT}"
}
export -f fetch_one
export OUT_DIR TOKEN

echo ">> Fetching SKUs (concurrency=$MAX_CONCURRENCY)..."
START=$(date +%s)

# Clean old SKU files
rm -f "$OUT_DIR"/*.json.gz

mapfile -t SERVICES < <(jq -r '.[] | "\(.serviceId)\t\(.displayName)"' "$SERVICES_OUT")
COUNT=0
for line in "${SERVICES[@]}"; do
  ID="${line%%$'\t'*}"
  NAME="${line#*$'\t'}"
  fetch_one "$ID" "$NAME" &
  COUNT=$((COUNT + 1))
  if (( COUNT % MAX_CONCURRENCY == 0 )); then wait; fi
done
wait

END=$(date +%s)

# Write metadata
TOTAL=0
for f in "$OUT_DIR"/*.json.gz; do
  N=$(gunzip -c "$f" | jq 'length')
  TOTAL=$((TOTAL + N))
done
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$META_OUT" <<META
{
  "fetched_at": "$NOW",
  "source": "https://cloudbilling.googleapis.com/v1/services/{id}/skus",
  "service_count": $SVC_COUNT,
  "sku_count": $TOTAL,
  "filter": "first-party (curated allow-list excludes marketplace VM images and 3rd-party integrations)",
  "refresh_command": "scripts/refresh-catalog.sh"
}
META

echo ""
echo "Done in $((END - START))s. $SVC_COUNT services, $TOTAL SKUs, $(du -sh "$OUT_DIR" | cut -f1) gz."

# Rebuild the DuckDB index so find-sku.sh uses the fresh data immediately.
echo ">> Rebuilding catalog.duckdb index..."
bash "$(dirname "$0")/build-catalog-index.sh"
