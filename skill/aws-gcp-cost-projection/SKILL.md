---
name: aws-gcp-cost-projection-gemini
description: "Project an AWS bill to GCP cost. Loads a CUR or flat AWS Cost Explorer Detail Report into DuckDB, maps each unique line item to its GCP equivalent, and produces a per-row cost comparison report. Use when user says CUR, AWS-to-GCP cost, GCP migration projection, AWS bill projection, GCP cost estimate, AWS CUR analysis, projection audit, AWS to GCP TCO, AWS to GCP migration cost."
---

# AWS CUR → GCP Cost Projection

Project the cost of an AWS workload running on GCP, line item by line
item. Output: per-LI cost-comparison report (MD + HTML) with three GCP
totals — On-Demand, 1-year CUD, 3-year CUD.

This skill is a guide, not a recipe. You run the queries, fetch the
data, decide the mappings, and render the report.

## Phase progress tracking — REQUIRED

At the **start** of each phase, write `progress.json` in the working
directory (the job dir, not the skill dir). The web UI reads this file
every 5 seconds to display which phase is running. Omitting it leaves
the user with a blank progress screen for the entire run.

```python
import json, os
def write_progress(phase_num: int, phase_name: str, activity: str = ""):
    with open("progress.json", "w") as f:
        json.dump({"phase": phase_num, "phase_name": phase_name,
                   "last_activity": activity}, f)
```

Call `write_progress(N, "Name", "short description")` as the very first
line of each phase block — before any queries or file I/O — so the UI
updates immediately when the phase begins:

| Call | When |
|---|---|
| `write_progress(1, "Ingestion", "Loading bill into DuckDB")` | start of Phase 1 |
| `write_progress(2, "Mapping", "Mapping AWS line items to GCP")` | start of Phase 2 |
| `write_progress(3, "Review", "Verifying mappings")` | start of Phase 3 |
| `write_progress(4, "Rate-Card Fill", "Fetching GCP rates")` | start of Phase 4 |
| `write_progress(5, "Outlier Triage", "Running outlier queries")` | start of Phase 5 |
| `write_progress(6, "Reporting", "Generating HTML report")` | start of Phase 6 |

**Scope: project the entire dataset provided.** Don't ask the user to
pick a time range, month, or service subset. Whatever rows are in the
input file, all of them are in scope — the report covers the full
period the file represents. Only filter if the user explicitly asks.

## Valid input shapes — check this BEFORE writing failure.txt

Three input shapes are ALL valid. Do not write `failure.txt` for any of them:

| Shape | How to recognise | Example first row |
|---|---|---|
| **Flat AWS Cost Explorer Detail Report** | 6 columns: `Service, Region, Custom Usage Type, Description, Usage Quantity, Cost ($)` — comma-separated, may have thousand-separator quotes | `Amazon Elastic Compute Cloud,Asia Pacific (Mumbai),APS3-BoxUsage:m6g.4xlarge,"$0.616 per On Demand Linux m6g.4xlarge Instance Hour",720,"443.52"` |
| **Raw AWS CUR** | Many columns (50+), has `lineItem/LineItemType`, may be CSV/parquet/gzip/dir of part files | `lineItem/LineItemType,product/instanceType,...` |
| **AWS console PDF bill** | `file` reports `PDF document`; convert via `pdftotext -layout` first | N/A |

**The 6-column flat CSV is the most common input shape. It is a valid AWS bill export. Do NOT reject it.**

Only write `failure.txt` when the file is clearly from a different cloud provider (Azure, GCP), is corrupted/unreadable, or has a completely unknown structure that matches none of the three shapes above.

## Pipeline (six phases)

```
                ┌─────────────────────────────────────────────────┐
input bill ────►│ Phase 1 — Ingestion             [sub-agent]     │
                │   load → dedup → classify is_workload           │
                │   → reconcile sum                               │
                └────────────────────┬────────────────────────────┘
                                     │ aws_li_catalog
                ┌────────────────────▼────────────────────────────┐
                │ Phase 2 — Mapping             [4–5 parallel]    │
                │   compute / managed-db / networking /           │
                │   storage-analytics / misc                      │
                │   → aws_li_to_gcp_li + mapping-notes.md         │
                └────────────────────┬────────────────────────────┘
                                     │
                ┌────────────────────▼────────────────────────────┐
                │ Phase 3 — Review            [fresh sub-agent]   │
                │   challenge picks, resolve open notes,          │
                │   write corrections                             │
                └────────────────────┬────────────────────────────┘
                                     │ corrected mappings
                ┌────────────────────▼────────────────────────────┐
                │ Phase 4 — Rate-card fill        [main agent]    │
                │   lazy-load gcp_sku_rates for matched sku_ids   │
                │   only; alias / synthesize CUD rates            │
                └────────────────────┬────────────────────────────┘
                                     │ gcp_sku_rates
                ┌────────────────────▼────────────────────────────┐
                │ Phase 5 — Outlier triage         [sub-agent]    │
                │   build gcp_projection; A1/A2/B/C/D/E/F →       │
                │   fix or accept-with-cited-mechanism            │
                └────────────────────┬────────────────────────────┘
                                     │
                ┌────────────────────▼────────────────────────────┐
                │ Phase 6 — Report                [main agent]    │
                │   MD + HTML, layout per phase 6 spec            │
                └─────────────────────────────────────────────────┘
```

| Phase | Run by | Reads | Writes | Detail |
|---|---|---|---|---|
| 1 — Ingestion | one sub-agent | input bill | `aws_raw`, `aws_li_catalog` | [phases/01-ingestion.md](phases/01-ingestion.md) |
| 2 — Mapping | 4–5 parallel sub-agents | catalog slice, `data/skus/`, `find-sku.sh` | `aws_li_to_gcp_li`, `mapping-notes.md` | [phases/02-mapping.md](phases/02-mapping.md) |
| 3 — Review | one fresh sub-agent | `mapping-notes.md`, `aws_li_to_gcp_li` | corrections to `aws_li_to_gcp_li` | [phases/03-review.md](phases/03-review.md) |
| 4 — Rate-card fill | main agent | `aws_li_to_gcp_li`, `data/skus/` | `gcp_sku_rates` | [phases/04-rate-fill.md](phases/04-rate-fill.md) |
| 5 — Outlier triage | one sub-agent (split to ≤4 only if >50 rows) | `gcp_projection` view | corrections to `aws_li_to_gcp_li` | [phases/05-outlier-triage.md](phases/05-outlier-triage.md) |
| 6 — Report | main agent | `aws_li_catalog`, `gcp_projection` | `report.md`, `report.html` | [phases/06-report.md](phases/06-report.md) |

The DuckDB schemas (`aws_raw`, `aws_li_catalog`, `aws_li_to_gcp_li`,
`gcp_sku_rates`) are referenced from every phase. Definitions in
[reference/schemas.md](reference/schemas.md).

## Setup (do this once at the start)

1. **Run preflight.**

   ```bash
   bash "$SKILL_DIR/preflight.sh" "<bill-path>"
   ```

   Verifies `duckdb`, `jq`, `gzip` are on PATH, the bundled catalog
   under `data/` is intact, the input bill exists. Output is JSON.
   - `verdict: PASS` → continue.
   - `verdict: FAIL` → surface the failed checks to the user and stop.
     Don't try to work around tool-missing failures.
   - `catalog_age` is `warn`-only (>90 days). When you see it, surface
     a one-line note to the user:

     > "GCP rate card was last refreshed on YYYY-MM-DD (~N days ago).
     > GCP list prices change rarely — usually only at quarterly
     > product events — so the projection is still directionally
     > accurate. Ask the skill maintainer to run
     > `scripts/refresh-catalog.sh` if you need fresh rates before
     > signing off."

     Then proceed. **Do not run `refresh-catalog.sh` from inside the
     skill** — refresh is a maintainer action, not a per-run action.

2. Create `projection-audit/projection.duckdb`. The four tables in
   [reference/schemas.md](reference/schemas.md) live there. Initialize
   them empty; each phase populates its own.

## Adding new AWS/GCP services (zero Python required)

All instance specs, service classifications, and model mappings live in JSON files under `data/`. When a new service or instance type is encountered and not recognized, update the relevant JSON file — no Python changes needed:

| File | What to update |
|---|---|
| `data/instance-specs.json` | New ElastiCache node type (`elasticache_ram_gib`), EMR instance (`emr_vcpu_map`), burstable EC2 size (`burstable_ec2_specs`), Redshift node (`redshift_slot_map`) |
| `data/gcp-model-config.json` | New GCP GPU model (`gpu_model_paired_family`, `gpu_vram_gb_fallback`, `gpu_class_rank`), new GCP compute family (`gp_family_workload`, `gp_family_tier`, `gp_family_arch`), new Memorystore tier (`memorystore_tier_max_gib`) |
| `data/service-classification.json` | New AWS service that should passthrough (`passthrough_services`), self-host on GCE (`self_host_on_gce`), or is sizing-sensitive (`sizing_sensitive`). Also add new entries to `misc_service_hints` for LLM context. |
| `data/aws-aliases.json` | New AWS product name variant that should map to an existing canonical key |
| `data/egress-rates.json` | When GCP publishes updated network egress rates |
| `data/confidence-caps.json` | New service with architectural ambiguity that should have a confidence ceiling |
| `data/cud_pct.json` | When GCP updates CUD discount percentages |

After editing any JSON file, all scripts automatically pick up the change on the next run — no restart or rebuild needed.

## Large bills (>500 deduped line items after Phase 1)

When `aws_li_catalog` has >500 rows after Phase 1 dedup:

1. **Phase 2 — split compute partition further** if it has >200 rows:
   split by region group (Americas / EMEA / APAC) before launching
   sub-agents, keeping each agent's slice under 100 rows.
2. **Phase 5 — always split** across 4 sub-agents by query family
   (A1+A2 / B+C / D+E+G / F+H+I). Don't attempt all 9 queries in
   one agent for a large bill.
3. **Phase 2 — pre-filter obvious service splits**: If the bill has
   distinct product families that a single agent would never finish
   in one context (e.g. 150 Lambda rows + 200 EC2 rows), add an
   extra partition or subdivide compute further. The concurrency cap
   is 4–5 agents; more slices = more serial turns, so balance.

## Mid-run failure recovery

If a phase exits or errors before writing its output (DuckDB table
empty, `mapping-notes.md` missing, agy killed mid-run):

1. **Don't restart from scratch.** The DuckDB at
   `projection-audit/projection.duckdb` is durable. Check what's
   there:
   ```sql
   SELECT table_name, estimated_size FROM duckdb_tables();
   ```
2. **Re-run only the failed phase** — the prior phases' tables are
   intact. Brief the sub-agent with the current state of the database
   and the phase file; it picks up from where the failure left.
3. **If `aws_li_to_gcp_li` is partially populated** (Phase 2 failed
   mid-flight): run the coverage query from Phase 2 to find which
   `aws_li_key`s are missing, then dispatch only the missing slices
   to new sub-agents.
4. **If `agy-internal.log` exists in the job dir**, read it first —
   it captures quota exhaustion, auth errors, and hard crashes that
   the main log doesn't show.

## Scripts Added in Product Build

These scripts were added during the product build phase and extend the core skill pipeline with deterministic classification and confidence layers.

| Script | Purpose |
|---|---|
| `classify_mechanics.py` | Stamps `mechanic_group` on `aws_li_catalog` rows — groups each line item by billing mechanic (On-Demand, Spot, Reserved, Savings Plan, etc.) so downstream phases can apply the correct GCP equivalent |
| `classify_transfer.py` | Deterministic DataTransfer direction classification — reads the AWS usage type and description to assign `transfer_direction` (ingress / egress / inter-AZ / inter-region / internet) without relying on LLM inference |
| `field_state.py` | Annotates field availability from CUR vs external APIs — marks each schema field as `present_in_cur`, `needs_api`, or `not_available` so Phase 2 agents know which fields they can rely on |
| `service_contracts.py` | Validates required fields per service — enforces that each mapped GCP service has the minimum fields populated (e.g. `gcp_sku_id`, `unit_multiplier`) before Phase 5 runs |
| `candidate_pool.py` | Scored GCP candidate pool for compute rows — queries the catalog to produce a ranked list of GCP SKU candidates for each EC2/RDS instance type, used by Phase 2 mapping agents as a starting point |
| `confidence_engine.py` | Computes overall projection confidence score — aggregates per-row mapping quality signals (field completeness, mechanic match, rate coverage) into a single `confidence` label (`HIGH` / `MEDIUM` / `LOW`) written to the report |

## What you never do

- Render the report while any outlier query has unreviewed results.
- Ask the user for a date range, month, or service filter. The
  projection covers the whole input as-is unless they explicitly
  scope it down.
- Web-search for GCP rates. The rate card lives in `gcp_sku_rates`,
  loaded from the bundled `data/` catalog.
- Run `scripts/refresh-catalog.sh` from inside the skill. If the
  catalog is stale, note it to the user and proceed — refresh is a
  maintainer task.
- Bulk-load the entire SKU catalog into `gcp_sku_rates`. Populate it
  lazily — only for `gcp_sku_id` values that ended up in
  `aws_li_to_gcp_li`. Use `scripts/find-sku.sh` for discovery, then
  read the specific service's `data/skus/<id>.json.gz` for the rows
  you need.
- Sum `aws_amortized_cost` from `gcp_projection` for the report total
  (it double-counts `break_down` mappings). Always sum from
  `aws_li_catalog`.
- Mutate AWS data or write back to GCP. Read-only audit.
- Use `view_file` (or similar viewing tools) to read the contents of raw SKU catalog files (e.g., `data/skus/<id>.json.gz` or `data/services.json`). Instead, read and filter them programmatically using Python scripts or DuckDB JSON table readers without printing their raw content to the stdout.
- Print large query results, arrays, or text outputs containing more than 50 rows/lines to the stdout of your command executions, as this inflates the token context window. Use `LIMIT` in SQL or summarize outputs in your python scripts.
