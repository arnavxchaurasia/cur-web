#!/usr/bin/env bash
# Preflight for aws-gcp-cost-projection.
#   bash "$SKILL_DIR/preflight.sh" [bill-path]
#
# Verifies tooling and bundled-catalog presence so Phase 1 fails fast
# instead of mid-pipeline. The skill agent should run this and parse
# the JSON; if verdict=FAIL, surface the failed checks to the user
# and stop.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$SCRIPT_DIR"
BILL_PATH="${1:-}"

results=()
fail=0

esc() { printf '%s' "$1" | sed 's/"/\\"/g; s/\\/\\\\/g; s/\t/ /g; s/\r//g'; }

add_check() {
  # add_check <name> <status> <detail>
  local entry
  entry="$(printf '    {"name":"%s","status":"%s","detail":"%s"}' "$1" "$2" "$(esc "$3")")"
  if [[ ${#results[@]} -eq 0 ]]; then
    results=("$entry")
  else
    results=("${results[@]}" "$entry")
  fi
}

# --- 1. duckdb on PATH (CLI or python lib both work; we check CLI here) ---
if command -v duckdb >/dev/null 2>&1; then
  add_check duckdb pass "$(duckdb --version 2>/dev/null | head -1)"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import duckdb' >/dev/null 2>&1; then
  add_check duckdb pass "python3 duckdb module available (CLI not on PATH)"
else
  add_check duckdb fail "duckdb CLI not on PATH and python3 duckdb module not importable; install via 'brew install duckdb' or 'pip install duckdb'"
  fail=1
fi

# --- 2. jq (used by scripts/find-sku.sh) ---
if command -v jq >/dev/null 2>&1; then
  add_check jq pass "$(jq --version 2>/dev/null)"
else
  add_check jq fail "jq not on PATH; install via 'brew install jq'"
  fail=1
fi

# --- 3. gzcat / gunzip (used to read data/skus/*.json.gz) ---
if command -v gzcat >/dev/null 2>&1; then
  add_check gzip pass "gzcat"
elif command -v gunzip >/dev/null 2>&1; then
  add_check gzip pass "gunzip"
else
  add_check gzip fail "gzcat/gunzip not on PATH"
  fail=1
fi

# --- 4. Bundled catalog present ---
META="$SKILL_DIR/data/CATALOG_META.json"
SVC="$SKILL_DIR/data/services.json"
SKUS="$SKILL_DIR/data/skus"
if [[ -f "$META" ]] && [[ -f "$SVC" ]] && [[ -d "$SKUS" ]]; then
  sku_count="?"
  svc_count="?"
  if command -v jq >/dev/null 2>&1; then
    sku_count="$(jq -r '.sku_count // "?"' "$META" 2>/dev/null || echo "?")"
    svc_count="$(jq -r '.service_count // "?"' "$META" 2>/dev/null || echo "?")"
  fi
  add_check catalog pass "$svc_count services, $sku_count SKUs"
else
  missing=""
  [[ ! -f "$META" ]] && missing="$missing CATALOG_META.json"
  [[ ! -f "$SVC" ]] && missing="$missing services.json"
  [[ ! -d "$SKUS" ]] && missing="$missing skus/"
  add_check catalog fail "missing in data/:$missing — partial install?"
  fail=1
fi

# --- 5. Catalog age (warn-only; never fails) ---
if [[ -f "$META" ]] && command -v jq >/dev/null 2>&1; then
  fetched_at="$(jq -r '.fetched_at // empty' "$META" 2>/dev/null || true)"
  if [[ -n "$fetched_at" ]]; then
    # Strip subseconds and 'Z' for portable parsing.
    raw="${fetched_at%.*}"
    raw="${raw%Z}"
    fetched_epoch=""
    # macOS BSD date
    fetched_epoch="$(date -j -f "%Y-%m-%dT%H:%M:%S" "$raw" "+%s" 2>/dev/null || true)"
    # GNU date fallback
    if [[ -z "$fetched_epoch" ]]; then
      fetched_epoch="$(date -d "$fetched_at" "+%s" 2>/dev/null || true)"
    fi
    if [[ -n "$fetched_epoch" ]]; then
      now_epoch="$(date "+%s")"
      age_days="$(( (now_epoch - fetched_epoch) / 86400 ))"
      if [[ "$age_days" -gt 90 ]]; then
        add_check catalog_age warn "catalog is $age_days days old (fetched $fetched_at); rates may be stale — ask maintainer to run scripts/refresh-catalog.sh if signing off"
      else
        add_check catalog_age pass "$age_days days (fetched $fetched_at)"
      fi
    else
      add_check catalog_age warn "could not parse fetched_at='$fetched_at'"
    fi
  else
    add_check catalog_age warn "no fetched_at in CATALOG_META.json"
  fi
fi

# --- 6. SKU file count vs services.json ---
if [[ -f "$SVC" ]] && [[ -d "$SKUS" ]] && command -v jq >/dev/null 2>&1; then
  expected="$(jq 'length' "$SVC" 2>/dev/null || echo 0)"
  actual="$(find "$SKUS" -maxdepth 1 -name '*.json.gz' -type f | wc -l | tr -d ' ')"
  if [[ "$actual" = "$expected" ]]; then
    add_check sku_files pass "$actual files match $expected services"
  else
    add_check sku_files warn "$actual *.json.gz files but $expected services in services.json — partial install?"
  fi
fi

# --- 7. Bill input (only if path provided) ---
if [[ -n "$BILL_PATH" ]]; then
  if [[ -f "$BILL_PATH" ]]; then
    size="$(wc -c < "$BILL_PATH" 2>/dev/null | tr -d ' ' || echo "?")"
    add_check bill pass "$BILL_PATH ($size bytes)"
  elif [[ -d "$BILL_PATH" ]]; then
    count="$(find "$BILL_PATH" -type f \( -name '*.csv*' -o -name '*.parquet*' -o -name '*.gz' \) | wc -l | tr -d ' ')"
    add_check bill pass "$BILL_PATH (directory, $count candidate part files)"
  else
    add_check bill fail "bill not found at $BILL_PATH"
    fail=1
  fi
fi

# --- Emit JSON ---
printf '{\n  "skill": "aws-gcp-cost-projection",\n  "checks": [\n'
n=${#results[@]}
i=0
while [[ $i -lt $n ]]; do
  printf '%s' "${results[$i]}"
  i=$((i + 1))
  if [[ $i -lt $n ]]; then printf ',\n'; else printf '\n'; fi
done
printf '  ],\n'
if [[ $fail -eq 0 ]]; then
  printf '  "verdict": "PASS"\n}\n'
else
  printf '  "verdict": "FAIL"\n}\n'
fi

exit $fail
