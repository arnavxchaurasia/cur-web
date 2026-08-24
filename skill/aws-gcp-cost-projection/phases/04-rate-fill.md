# Phase 4 — Rate-card fill

**Run by:** main agent. Fully script-based — no judgment calls.
**Reads:** `aws_li_to_gcp_li` (corrected by Phase 3).
**Writes:** `gcp_sku_rates`, `progress.json`, `projection-audit/rate-fill-gaps.md` (if gaps exist).

## Execution

Run the script:

```bash
python3 scripts/apply_rates.py
```

The script handles everything:
- Writes `progress.json` progress marker
- Enforces accelerator/ElastiCache/managed-DB-storage mapping corrections
- Auto-resolves NULL `gcp_sku_id` rows via catalog word-overlap search
- Loads all OnDemand rates from `catalog.duckdb`
- Loads real `Commit1Yr`/`Commit3Yr` rates from catalog where available; synthesizes from `cud_pct.json` percentages only for gaps
- Synthesizes Preemptible rates (22% of OnDemand) for Spot rows
- Synthesizes `global` fallback rates for any unregioned rows
- Runs the sanity check — unreachable SKU/region pairs are moved to `outlier_triage` and written to `rate-fill-gaps.md`
- Converts any remaining NULL-cost mapped rows to `passthrough`
- Stamps `rate_source` on every row

If `catalog.duckdb` is missing entirely, all mapped rows are set to `passthrough` with a clear note and the script exits cleanly (Phase 5 will triage them). Run `bash scripts/refresh-catalog.sh` to rebuild the catalog.

The phase is complete once the script exits 0.
