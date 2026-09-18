# Phase 2 — Mapping

## Pre-Processing (ALREADY DONE by orchestrator — do NOT re-run)

The orchestrator ran these scripts before spawning this agent:

1. `classify_mechanics.py` → `mechanic_group` stamped on every row, `projection-audit/phase2_manifest.json` written
2. `apply_commitment_ignores.py` → `projection-audit/mappings/commitment_discount_mappings.json` written
3. `apply_static_mappings.py` → flat_hourly, object_storage, per_request mapping files written

**Do NOT run any of these scripts.** They are already done. Your input is `phase2_manifest.json`.

Phase 2 agents MUST check mechanic_group before mapping. The groups and their handling:

| mechanic_group | Who maps it | Approach |
|---|---|---|
| compute_breakdown | candidate_pool.py (script) | Family already resolved; agent confirms SKU ID |
| data_transfer | classify_transfer.py (script) | Pre-filled; agent only handles unclassified rows |
| block_storage | Phase 2 storage agent | volume_type→PD type lookup table (below) |
| managed_db | Phase 2 managed-db agent | Engine + tier judgment |
| flat_hourly | Phase 2 networking agent | Fixed SKU per service type |
| per_request | Phase 2 misc agent | Request-unit mapping |
| object_storage | Phase 2 storage-analytics agent | S3 class → GCS class lookup |
| commitment_discount | Auto-ignore | Set strategy='ignore', gcp_sku_id=NULL |
| opensearch | Phase 2 OpenSearch agent | Row-type dispatch + structured SKU selection |
| misc | Phase 2 misc agent | Best-effort, document confidence |

### Block Storage Lookup Table (deterministic — use this, do not guess)

**This table must stay in sync with `apply_static_mappings.py::map_block_storage()`** —
it previously fell out of sync (said gp3/io1/io2 → plain `pd-balanced`/`pd-ssd`) after a
real bug fix changed the deterministic code to route gp3 and io1/io2-under-ceiling to
**Hyperdisk Balanced Capacity** instead (classic `Balanced PD Capacity` has no standalone
IOPS/throughput SKU — pairing it with a Hyperdisk-only performance fee on the same volume
isn't purchasable on real GCP). If you're mapping a `block_storage` row yourself (the
deterministic script didn't resolve it), use this corrected table, not "pd-balanced"/"pd-ssd"
as generic GCP concepts — always confirm the exact real SKU name via `find-sku.sh` before
finalizing (see Verification Methodology below).

| AWS Volume Type | GCP target | Notes |
|---|---|---|
| gp2 | Balanced PD Capacity | No companion IOPS/throughput fee row — classic Balanced PD is correct standalone |
| gp3 | **Hyperdisk Balanced Capacity** | Gets a companion provisioned-IOPS/throughput fee (Hyperdisk Balanced IOPS/Throughput) — capacity must stay in the same product family |
| io1 | Hyperdisk Balanced Capacity (or Extreme PD Capacity if provisioned IOPS &gt;160,000 or throughput &gt;2,400 MiB/s) | io1 caps at 64,000 IOPS/1,000 MB/s — always under Balanced's ceiling |
| io2 | Same as io1, EXCEPT io2 "Block Express" (up to 256,000 IOPS/4,000 MB/s) → Extreme PD Capacity | Check for "Block Express" in the row's own text before picking |
| st1 | Storage PD Capacity | Real catalog name — NOT "pd-standard", that string never matches any SKU |
| sc1 | Storage PD Capacity | Same as st1 |
| magnetic (standard) | Storage PD Capacity | Same as st1 |
| Aurora storage | Cloud SQL for {engine}: {Zonal/Regional} - Standard storage | Real name is "Standard storage"/"Low cost storage" — NOT "SSD storage"/"HDD storage", those never match any Cloud SQL SKU |
| EFS Standard | Filestore Capacity Basic SSD | A cheaper Filestore "Zonal" tier exists but has a different multi-component pricing shape (fixed instance fee + IOPS) this bill's GB-Mo-only data can't fully price — disclose, don't switch |
| EFS Standard-IA/One Zone-IA | Filestore Capacity Basic HDD | |

### Verification Methodology (applies to EVERY SKU you pick, not just storage)

This session found and fixed a long chain of real pricing bugs, all sharing the same root
cause: a plausible-looking SKU description string or unit assumption was never actually
checked against the real catalog data. Before finalizing ANY SKU choice:

1. **Verify the exact SKU description, not just a keyword match.** Run `find-sku.sh` with
   the specific service + keyword and read the FULL description text back. A description
   that merely *contains* your search term isn't automatically the right SKU — GCP catalog
   descriptions often have several variants of the same words (e.g. "Balanced PD Capacity"
   vs "Hyperdisk Balanced Capacity" vs "Regional Balanced PD Capacity" are three different,
   non-interoperable products; "Network Load Balancer Forwarding Rule" matched several
   **Proxy** NLB SKUs when AWS NLB is actually a **Passthrough** LB — a different, 2.8x
   pricier product family).
2. **Check the billing unit, not just the rate number.** If you're about to multiply a
   quantity by a rate, confirm what that rate is actually priced PER (per-hour? per-1000
   requests? per-mCPU? per-vCPU? per-month?). Two SKUs with similar-looking names can have
   completely different billing granularity (GKE Autopilot's "...mCPU Requests" SKUs are
   priced per-1000-mCPU; a differently-named sibling SKU was priced as a flat per-vCPU rate
   — applying the same "×1000" logic to both would have been a confirmed ~1000x error).
3. **Sanity-check the resulting price for plausibility**, not just internal consistency.
   Re-deriving the same number your own mapping logic already produces and calling that
   "confirmed" is circular — it proves nothing. Ask instead: is this a realistic real-world
   rate? (Would anyone plausibly pay $32/vCPU-hour for container orchestration? No — that's
   an immediate signal something is wrong, even before checking the catalog metadata.)
4. **Check whether a "cheaper" swap is actually the same product family.** A cheaper SKU
   that shares only some words with your target isn't necessarily a valid substitute —
   confirm it delivers the same capability/guarantee (e.g. a companion IOPS/throughput fee
   for the same resource must resolve against the same underlying product line as its
   capacity fee, not a different, non-interoperable one).
5. **Confirm the bill's actual unit basis before assuming a conversion is needed at all.**
   The same AWS charge can appear with total_usage in different units depending on bill
   format (CUR vs PDF vs flat-CSV) — e.g. a WAF WebACL fee's `total_usage` was in raw hours
   for one bill format and already in months for another; applying the same hours→month
   conversion to both silently produced a ~99.8% underprice for the already-month-denominated
   format. Read the row's own `operation`/`usage_type` text for the actual unit before
   picking a multiplier.

### Commitment/Discount rows — always ignore
Rows with mechanic_group='commitment_discount' MUST be set to strategy='ignore'.
These include: RIFee, SavingsPlanRecurringFee, EdpDiscount, BundledDiscount.
They represent amortized commitment costs already reflected in effective rates.
NEVER try to map these to GCP — it double-counts.

**Run by:** main agent dispatches one sub-agent per mechanic_group in parallel; all write to temp files; a merge script does one bulk INSERT.
**Reads:** `projection-audit/phase2_manifest.json` (written by Phase 1).
**Writes:** `projection-audit/mappings/<group>_mappings.json` per agent, then merged into `aws_li_to_gcp_li`.
**Returns to main:** `{group, mapped_count, unmapped_keys[]}` per agent.

## FIRST LINE OF THIS PHASE — write progress marker

Before any other work, write `progress.json` in the job working directory:

```python
import json
with open("progress.json", "w") as f:
    json.dump({"phase": 2, "phase_name": "Mapping", "last_activity": "Mapping AWS line items to GCP"}, f)
```

This is required — the UI reads it every 5 s and will show a blank screen for phases before the last if you skip it.

## Per-service prompt files

Every mechanic_group has a dedicated prompt file in `phases/02-mapping/`. Each sub-agent receives:
- `phases/02-mapping/_shared.md` — principles, output format, back-check rules (read first)
- `phases/02-mapping/<service>.md` — service-specific context extraction, dynamic candidate SQL, performance floor, availability check

| mechanic_group | Service file |
|---|---|
| `compute_breakdown` | `phases/02-mapping/compute.md` (GPU rows also read `gpu.md`) |
| `block_storage` | `phases/02-mapping/block-storage.md` |
| `managed_db` | `phases/02-mapping/managed-db.md` |
| `opensearch` | See OpenSearch section below (inline — complex dispatch logic) |
| `object_storage` | `phases/02-mapping/object-storage.md` |
| `data_transfer` | `phases/02-mapping/data-transfer.md` |
| `misc` | `phases/02-mapping/misc.md` |

**The core pattern in every service file:** query `gcp_sku_rates` for ALL candidates ordered by price → apply performance floor filter → verify availability via `find-sku.sh` / GCP Billing Catalog API → pick cheapest available. No hardcoded family tables.

---

## Dispatch — mechanic-group parallel agents

Phase 1 wrote `projection-audit/phase2_manifest.json`. Read it to get groups and their rows:

```python
import json
manifest = json.load(open("projection-audit/phase2_manifest.json"))
# manifest[group] = {"rows": [...], "row_count": N, "total_spend": X, "needs_llm": bool}
```

**Auto-handled groups (already done by orchestrator):**

`apply_static_mappings.py` handled `flat_hourly`, `object_storage`, and `per_request` — their mapping files already exist in `projection-audit/mappings/`. Do not re-run them.

| Group | Handler | Method |
|---|---|---|
| `commitment_discount` | `apply_commitment_ignores.py` | All rows → `strategy='ignore'` |
| `data_transfer` | `classify_transfer.py` (Phase 1) | Direction lookup, pre-filled |
| `block_storage` | inline (see table below) | Volume type → PD tier |
| `flat_hourly` | `apply_static_mappings.py` | ALB/NLB/NAT/EIP fixed SKU map |
| `object_storage` | `apply_static_mappings.py` | S3 class → GCS class |
| `per_request` | `apply_static_mappings.py` | Lambda/SQS/SNS/Kinesis fixed map |

**Block storage — use the dynamic candidate sweep in `phases/02-mapping/block-storage.md`.**

Do NOT use a hardcoded volume_type→SKU table here — that table previously fell out of sync and caused real pricing bugs (gp3 was incorrectly mapped to classic Balanced PD instead of Hyperdisk Balanced; io1/io2 to pd-ssd instead of Hyperdisk Balanced/Extreme). The correct approach is in `block-storage.md`: query `gcp_sku_rates` for all storage candidates, apply the volume_type performance floor, verify availability, and pick cheapest.

Write block_storage mappings to `projection-audit/mappings/block_storage_mappings.json` using the same JSON format as LLM agents (see Output contract below).

**Groups that spawn an LLM agent (in parallel):**

| mechanic_group | Rows contain | Curated prompt focus |
|---|---|---|
| `compute_breakdown` | EC2 Box/Spot/Reserved/Dedicated | Family already resolved by scripts; agent confirms SKU ID + unit_multiplier |
| `managed_db` | RDS, Aurora, ElastiCache, MemoryDB, DocumentDB | Engine → GCP service (MySQL→Cloud SQL, Postgres→AlloyDB, Redis→Memorystore) |
| `opensearch` | OpenSearch / Elasticsearch all row types | See OpenSearch agent section below |
| `misc` | Unclassified rows | Use `misc_annotation` per row — targeted guidance per service type |

Merge groups with <5 rows into `misc` before dispatching. Keep **5 max concurrent agents**.

### OpenSearch agent — structured row-type dispatch

All rows with `mechanic_group='opensearch'` come from Amazon OpenSearch Service or Amazon Elasticsearch Service. They include mixed row types: compute node hours, storage, IOPS, throughput, snapshots, and networking. Map each row by inspecting every available field (`usage_type`, `operation`, `unit`, `total_usage`, `instance_type`, `instance_ram_gb`, `volume_type`) before selecting a SKU. Never guess — if the row type or SKU cannot be determined from the fields, use `strategy='passthrough'`.

**Design constraint (non-negotiable):** AWS CUR is an infrastructure billing model, not an application model. Map to self-managed Compute Engine by default. Do NOT automatically map to Vertex AI Search, Cloud Logging, or Google Security Operations — those require workload context not present in the bill.

**Confidence cap: compute rows must not exceed `mapping_confidence=0.70`.** Multiple valid GCP targets exist for OpenSearch compute (Managed OpenSearch via Marketplace, Elastic Cloud, Vertex AI Search, or self-managed GCE). Include this note in every compute row's `projection_note`: `"architecture review recommended: multiple valid GCP targets exist (Managed OpenSearch via Marketplace, Elastic Cloud, Vertex AI Search, or self-managed GCE); self-managed GCE assumed"`.

#### Step 1 — Classify the row type

Inspect `usage_type` + `operation` to determine the row type:

| Signal in usage_type or operation | Row type |
|---|---|
| `ESInstance`, `BoxUsage`, `NodeUsage` + recognizable instance type | **compute** |
| `OCU`, `SearchOCU`, `IndexingOCU`, unit `OCU-Hrs`/`OCU-hours` (OpenSearch **Serverless**) | **ocu** |
| `GP2-Storage`, `GP3-Storage`, `VolumeUsage`, storage-like unit (GB-Mo) | **storage** |
| `PIOPS`, `GP3-PIOPS`, `ProvisionedIOPS` | **iops** |
| `GP3-Throughput`, `ProvisionedThroughput` | **throughput** |
| `SnapshotStorage`, `snapshot` | **snapshot** |
| `DataTransfer`, `data-transfer`, `transfer-out` | **networking** |
| None of the above / empty usage_type | **unknown** → passthrough |

#### Step 2 — Parse instance type for compute rows

The `.search` suffix on instance types is AWS-specific and must be stripped before looking up specs:

```
c7g.2xlarge.search  →  c7g.2xlarge   (ARM Graviton)
m5.xlarge.search    →  m5.xlarge     (x86)
r6g.4xlarge.search  →  r6g.4xlarge   (ARM Graviton)
ultrawarm1.medium.search → ultrawarm1.medium  (warm-tier, see below)
```

Instance type extraction priority:
1. `instance_type` column (if non-empty after stripping `.search`)
2. Regex from `usage_type`: pattern `ESInstance:<itype>` or `BoxUsage:<itype>` or `NodeUsage:<itype>`
3. If `instance_ram_gb` is populated, use it directly for the ram component

**vCPU count by size token:**

| Size | vCPU |
|---|---|
| large | 2 |
| xlarge | 4 |
| 2xlarge | 8 |
| 4xlarge | 16 |
| 8xlarge | 32 |
| 12xlarge | 48 |
| 16xlarge | 64 |
| 18xlarge | 72 |
| 24xlarge | 96 |

**RAM by family initial (GiB per vCPU):**

| Family prefix | GiB/vCPU | Examples |
|---|---|---|
| c | 2.0 | c5, c6i, c6g, c7g |
| m | 4.0 | m5, m6i, m6g, m7g |
| r | 8.0 | r5, r6i, r6g, r7g |
| i | 7.625 | i3 (NVMe storage; disk mapped separately) |

**ARM detection:** family name has `g` at position 2 (c7**g**, m6**g**, r7**g**, t4**g**). ARM → try C4A first; fall back to N2D AMD if C4A SKU absent in the target region.

**UltraWarm fixed specs:**

| Type | vCPU | RAM GiB |
|---|---|---|
| ultrawarm1.medium | 2 | 15 |
| ultrawarm1.large | 4 | 30 |
| ultrawarm1.xlarge | 8 | 60 |

UltraWarm maps to N2 (warm-tier approximation). Cap `mapping_confidence=0.50` for UltraWarm rows.

#### Step 3 — Select GCP SKU per row type

**Compute rows** — emit two components per row: `core` and `ram`.

| Architecture | Try first | Fallback |
|---|---|---|
| ARM Graviton | `C4A Arm Instance Core` / `C4A Arm Instance Ram` | `N2D AMD Instance Core` / `N2D AMD Instance Ram` |
| x86, sustained family (m5/r5/c5/etc.) | `N2 Instance Core` / `N2 Instance Ram` | `N2D AMD Instance Core` / `N2D AMD Instance Ram` |
| x86, **burstable** family (t2/t3/t3a) | Compare real per-region rates for `N2`, `N2D AMD`, **and `E2`** — pick the cheapest | — |
| UltraWarm | `N2 Instance Core` / `N2 Instance Ram` | — |

A burstable AWS source (t2/t3/t3a) may pick the cheapest GCP option including E2 — AWS CPU credits don't lower the AWS bill, so E2 is never a worse deal, same rule `map_compute_burstable()` in `apply_static_mappings.py` already applies to non-OpenSearch EC2. A **sustained** AWS source (m5/r5/c5/i3/etc.) must never land on E2 (burstable-only) — that would silently change the performance guarantee (CLAUDE.md §8). Only the size token's family prefix (not the whole row) determines this — check it before defaulting to N2.

Set `unit_multiplier` to vCPU count for `core`, RAM GiB for `ram`. Use `resolve_sku()` (already imported) to get the SKU ID. If SKU not found, set `strategy='passthrough'` for that component.

**OCU rows** (OpenSearch Serverless, billed in OCU-hours) — this is CUR-derivable infra evidence (a fixed compute-capacity unit), not workload intent, so reconstruct it rather than passthrough-copying AWS cost unchanged. AWS documents 1 OCU ≈ 6 vCPU + 12 GiB RAM. Emit two components per row, same as compute:

- `core`: `N2 Instance Core`, `unit_multiplier` = `total_usage` (OCU-hours) × 6
- `ram`: `N2 Instance Ram`, `unit_multiplier` = `total_usage` (OCU-hours) × 12

Cap `mapping_confidence=0.60` (lower than the 0.70 compute-row ceiling — the 6vCPU/12GiB ratio is AWS's own published approximation, not an exact per-node spec, and OCU billing has no Multi-AZ/replica-count signal). Include in the note: `"OpenSearch Serverless OCU-hours reconstructed as compute (1 OCU ≈ 6 vCPU + 12 GiB RAM per AWS documentation); architecture review recommended — OCU auto-scaling has no direct GCE equivalent"`. Never leave an OCU row as an undifferentiated passthrough that copies `aws_amortized_cost` into the On-Demand/1yr/3yr CUD columns unchanged — that presents "no CUD savings" as a finding when it's really just a missing mapping.

**Storage rows** → `pd-balanced` (Compute Engine `Storage PD Capacity`). `unit_multiplier` = GB-Mo from `total_usage`. `mapping_confidence=0.72`.

**IOPS rows** → `Hyperdisk Balanced IOPS`. `unit_multiplier` = IOPS count from `total_usage`. `mapping_confidence=0.72`.

**Throughput rows** → `Hyperdisk Balanced Throughput`. `unit_multiplier` = MiB/s from `total_usage`. `mapping_confidence=0.72`.

**Snapshot rows** → Compute Engine `Storage PD Snapshot`. `unit_multiplier` = GB-Mo. `mapping_confidence=0.72`.

**Networking rows** → Networking `Premium Network Egress`. `unit_multiplier` = GB from `total_usage`. `mapping_confidence=0.65`.

**Unknown / PDF bill rows** (empty `usage_type`, no instance type, no recognizable pattern) → `strategy='passthrough'`, `unit_multiplier=1.0`, `mapping_confidence=0.30`. Include in note: `"PDF bill row — instance type not available in CUR; passthrough at cost parity"`.

#### Step 4 — Write output

Write to `projection-audit/mappings/opensearch_mappings.json` using the standard mapping JSON format (same schema as all other group mapping files). One entry per component (compute rows produce two entries per source row).

---

### Misc agent — how to use `misc_annotation`

Every misc row in the manifest has a `misc_annotation` field injected by `classify_mechanics.py`. Read it before mapping the row:

```json
{
  "why": "no mechanic rule matched; product='Amazon EKS'",
  "service_hint": "Amazon EKS",
  "available_from_cur": ["usage_type", "operation", "unit", "region"],
  "not_available_cur_only": ["node_count", "cluster_mode"],
  "mapping_guidance": "Map Amazon EKS to its GCP equivalent. Fields ['node_count', 'cluster_mode'] are not in CUR — use service defaults and document assumptions."
}
```

Apply this protocol per row:

1. **`service_hint` is set** → you know the AWS service. Follow `mapping_guidance`. Fields in `not_available_cur_only` cannot be retrieved — pick conservative defaults (e.g. single-cluster, standard tier) and record each assumption in `mapping-notes.md` under `Assumed:`.
2. **`service_hint` is null** + `aws_amortized_cost < $10/mo` → `strategy='passthrough'`, `mapping_confidence=0.3`, note `"low-spend unrecognized service"`.
3. **`service_hint` is null** + `aws_amortized_cost ≥ $10/mo` → look up the product name in `data/services.json`. If found, map to the best GCP equivalent. If not found, `strategy='outlier_triage'` and add to `mapping-notes.md` with `why` text so Phase 5 can handle it.

Never leave a misc row unmapped without an explicit `strategy`. The valid strategies for misc rows are: `map`, `passthrough`, `outlier_triage`, `ignore`.

## Output contract — temp files, never direct DuckDB writes

**Every agent (and every script) writes to its own JSON file. No agent touches DuckDB.**

Output path: `projection-audit/mappings/<mechanic_group>_mappings.json`

Each file is a JSON array. Every element maps to one row in `aws_li_to_gcp_li`:

```json
[
  {
    "aws_li_key": "abc123",
    "gcp_service": "Compute Engine",
    "gcp_sku_id": "6F81-5844-456A",
    "gcp_sku_name": "N2D AMD Instance Core running in Americas",
    "component": "core",
    "strategy": "map",
    "unit_multiplier": 16,
    "gcp_region": "us-east4",
    "projection_note": "N2D AMD preferred over N2 Intel; 60/40 win",
    "mapping_confidence": 0.92
  }
]
```

All schema fields from `reference/schemas.md` for `aws_li_to_gcp_li` are valid. Omit fields you don't know — the merge script uses NULL for missing optional fields.

## Merge step — run by orchestrator, NOT by this agent

The orchestrator runs `merge_mappings.py` after this agent exits. Do NOT run it yourself. Phase 2 is complete when all three `_mappings.json` files exist in `projection-audit/mappings/`.

## Briefing each sub-agent

Each mapping sub-agent receives:
- This whole file (`phases/02-mapping.md`).
- Its rows from the manifest: `manifest["<mechanic_group>"]["rows"]` — embedded in the prompt, not fetched from DuckDB.
- Path to schema reference: `reference/schemas.md`.
- Path to catalog: `data/skus/`, `data/services.json`.
- Path to helper: `scripts/find-sku.sh`.
- Its output path: `projection-audit/mappings/<mechanic_group>_mappings.json`.
- Path to notes journal: `projection-audit/mapping-notes.md` (append-only).

The agent MUST NOT query DuckDB for its own rows — they are already in the manifest. The agent MAY query DuckDB or call `find-sku.sh` only for SKU rate lookups.

## Per-row reasoning — `operation` is the authoritative detail field

On AWS Cost Explorer Detail Reports (PDF or flat-CSV), `usage_type`
in `aws_li_catalog` carries the Cost-Explorer *rollup* string (e.g.
`"Amazon Elastic Compute Cloud running Linux/UNIX"`) — the same
value covers every Linux instance type in the region. The instance
type, disk class, data-transfer sub-type, and net unit price live
in `operation` (the verbatim AWS Description). Phase 1's dedup
tuple includes `operation`, so each distinct row in the bill becomes
its own catalog row — but you must read `operation` per-row to know
what you're actually mapping.

Concretely, **do not key Phase 2 mapping decisions on `usage_type`
alone.** When two catalog rows share `(product, usage_type, region)`
but differ in `operation`, they are different workloads and need
different mappings — for example `"On Demand Linux t3.micro Instance
Hour"` vs `"On Demand Linux m5.4xlarge Instance Hour"`. Treat
`operation` as the structural key for compute, EBS, data-transfer,
RDS, and S3 rows; `usage_type` is auxiliary metadata.

For raw CUR shape, `usage_type` already carries the granular
`BoxUsage:<instance-type>` form and the same discipline applies
trivially.

## Two principles that govern every choice

### 1. Bill is ground truth

The input bill is the only authoritative record of commercial
mechanisms on this workload, and the only authoritative record of
what the customer is buying. **Don't invent stories.** If you're
constructing a narrative to explain a discrepancy or justify a
deviation from defaults, treat that as strong evidence you're wrong.

| Visible — fair game | Invisible — don't invoke |
|---|---|
| Explicit discount lines (EDP, CK, Private Pricing, Volume) | Speculative reseller absorption ("CK probably absorbs this") |
| Credits and refunds | Speculative parent-account bundling (Shield Advanced, etc.) |
| SP-coverage offsets | "Probably free tier" without a cited published URL |
| RI-applied rows (`reserved instance applied` in description) | "Promo credit applied silently" |
| AWS description text (Multi-AZ, region, instance type) | Customer's "probable" Intel/ISA dependency, HA need, etc. |

**Two applications of the rule:**

- **Commercial-mechanism failures.** A row's cost is unexpectedly
  low/zero and no line item explains it → the cause is almost always
  a parsing or multiplier bug, not a hidden absorption. Fix the bug;
  don't write it into the report.
- **Technical-deviation failures.** You're picking a costlier GCP
  family / tier than the 60/40 default → the deviation needs evidence
  in the bill or customer docs, not a plausible-sounding story
  ("retain AVX-512 perf", "production-sized DB needs HA"). The bill
  tells you what the customer is paying for; match that.

### 2. Equivalence intent (price–performance, 60/40)

To prevent the mapping agent from making catastrophic "cheap-out" mistakes (e.g., mapping a memory-heavy database to a shared-core e2-micro), you must apply a **Two-Stage Filtering Pipeline**:

#### Stage 1: The Hard Boundary Filter (Zero Tolerance)
Before running any scoring or price comparison, eliminate incompatible candidates by applying these strict limits:
1. **The RAM/vCPU Ratio Floor:** Calculate the exact Gigabytes-per-vCPU ratio of the source AWS instance (e.g., `r5.xlarge` has 4 vCPUs and 32 GiB RAM, a ratio of 8:1). The target GCP instance **must have a ratio of ≥ source ratio** (e.g. `n2-highmem-4` or custom with ratio `≥8:1`). Disqualify any candidate family with a lower ratio.
2. **The Shared-Core / Burstable Block:** If the AWS `instance_type` does not start with `t` (meaning it is not a burstable instance type like `t3` or `t4g`), **you must explicitly exclude GCP's shared-core tiers (`e2-micro`, `e2-small`, `e2-medium`)** from the eligible candidate pool.

#### Stage 2: The 60/40 Optimization Within the Safe Pool
Only after filtering out the invalid, under-provisioned instances do you select the final SKU:
* Weight is **60% price / 40% performance** on the remaining safe, compatible candidates.
* The rule kicks in only when **both options are genuinely close** on the other axis:

- **Don't sacrifice cost for slight perf/availability gains.** Picking
  Cloud SQL Regional (HA) over Zonal because "it's more reliable"
  when the AWS row is Single-AZ is wrong — the customer isn't paying
  for HA today, don't project HA cost. Same for over-provisioning
  IOPS, upgrading to a faster disk tier, or picking a costlier compute
  family just because the benchmark is 5% better.
- **Don't sacrifice perf/availability for slight cost gains either.**
  Don't downgrade gp3 → Standard HDD, Cloud SQL Enterprise Plus →
  Enterprise, or Memorystore Standard → Basic just to save dollars
  when the AWS workload clearly needs the higher tier.
- **When both options are genuinely close**, pick the cheaper one.
  Example: AWS `m5.4xlarge` (Intel) → GCP `n2d-standard-16` (AMD) is
  fine even at ~5–10% lower per-thread perf — the families are peers,
  the cost win is real, the workload tolerates it.
- **A large performance regression on a perf-critical workload**
  (production databases, latency-bound services, high-IOPS storage)
  is never acceptable for cost reasons. Stay on the closer-perf
  family and note it.

Capture every non-obvious trade-off in `projection_note` and (more
importantly) in `mapping-notes.md` so the Review phase can challenge
it.

## Mapping checklist (apply to every catalog row)

For each row in your slice, INSERT 1+ rows into `aws_li_to_gcp_li`
following this checklist. Skim every row against every rule:

1. **`is_workload = FALSE` → `strategy = 'ignore'`**, all SKU/rate
   fields NULL. The AWS column still shows the cost; GCP cost = $0.
2. **Negative `aws_amortized_cost`** (credits, offsets that survived
   classification) → `strategy = 'ignore'`. Same shape as #1.
3. **Tax** — already dropped at the catalog stage; you should not see
   any tax row here.
4. **Region preserved per row.** Don't consolidate to a single GCP
   region; use the row's `gcp_region`. Only collapse if the user
   explicitly asks.
5. **One AWS LI → 1+ GCP rows.** `break_down` for compute (core+ram),
   databases (core+ram+storage+backup), accelerator instances
   (core+ram+accelerator), local-SSD families (core+ram+storage).
   Don't force a single-row `map` for compute.
6. **HA tier comes from deployment_option or description text, not from instance size.**
    For services with HA-tier pricing on GCP (Cloud SQL Regional vs
    Zonal, AlloyDB primary vs read pool, Memorystore Standard vs
    Basic):
    - Map to the HA / Regional tier **when deployment_option is 'Multi-AZ'** or when the AWS description contains "Multi-AZ", "Multi-Region", "HA", or equivalent.
    - Otherwise default to Zonal / Single-AZ, even for large instance
      sizes.
    - Never infer HA from "this is a production-sized DB". The bill
      tells you what the customer is actually paying for. A 48-vCPU
      `db.m6g.12xlarge` with `"reserved instance applied"` (and no
      Multi-AZ in the text) is Zonal.
7. **Licensing and BYOL routing rules.**
    Check the `license_model` column in `aws_li_catalog`:
    - If `license_model = 'Bring Your Own License'` or `Customer-provided`, map the workload to GCE/Cloud SQL BYOL instances to project compute-only charges and avoid double license fees.
    - If `license_model = 'License Included'`, map to GCP license-included SKUs.
8. **Workload Class Specific Routing Rules.**
    Read the `workload_class` column in `aws_li_catalog` and apply strict family gates:
    - **Burstable:** For burstable x86 instances (`t2`, `t3`, `t3a`), compare real per-region rates across **E2**, **N2**, and **N2D AMD** and pick the cheapest (§256) — AWS CPU credits are a performance mechanism, not a price discount, so a cheaper sustained x86 family is never a worse deal and the swap is always disclosed inline. Map burstable ARM instances (`t4g`) strictly to **Tau T2A** (`t2a-standard-*`) — never silently cross ARM/x86.
    - **ARM:** Map general-purpose/memory-optimized ARM workloads (`m6g`, `r6g`, etc.) strictly to **Tau T2A** (Tau ARM) to align with cost/performance parity. Only map to **C4A** (Axion) for highly-intensive compute workloads, and fall back to N2D only if ARM is unavailable in the target region.
    - **Memory-Optimized:** Enforce RAM-to-vCPU ratio $\ge 8:1$ (e.g. `n2-highmem-4` or custom shapes with $\ge 8:1$ memory ratio).
    - **GPU:** Map to `g2` (L4) or `a2` (A100) shapes, matching physical GPU counts and RAM exactly.
    - **Outlier:** If `workload_class = 'Outlier'` (e.g., bare-metal `u-` or `hpc-` instances), mark the row with `strategy = 'outlier_triage'`, assign no default SKU, and write its details to `outlier_instances.md`. Do not apply standard 60/40 heuristics.

### Strict Instance Family Mappings

When selecting a GCP machine family for AWS compute instances, adhere to this mapping directive:

| AWS Family | CPU Type / Arch | Target GCP Family | GCP SKU / Description Pattern |
|---|---|---|---|
| **t2, t3, t3a** | Burstable, x86 | **E2** | `E2 Instance Core/Ram` |
| **t4g** | Burstable, ARM | **T2A** | `T2A Instance Core/Ram` |
| **c5, c5a** | Compute-optimized, x86 | **N2D** | `N2D AMD Instance Core/Ram` |
| **c6i, c7i** | Compute-optimized, Intel | **C3 / C4** | `C3/C4 Dedicated Core/Ram` |
| **c6a, c7a** | Compute-optimized, AMD | **C3D / N2D** | `C3D/N2D AMD Instance Core/Ram` |
| **r5, r5a** | Memory-optimized, x86 | **N2 / N2D** | `N2/N2D AMD Instance Core/Ram` |
| **r6i, r7i** | Memory-optimized, Intel | **N4 / N2** | `N4/N2 Predefined Instance Core/Ram` |
| **r6a, r7a** | Memory-optimized, AMD | **N2D / C3D** | `N2D/C3D AMD Instance Core/Ram` |
| **r6g, r7g, r8g** | Memory-optimized, ARM | **T2A** | `T2A Instance Core/Ram` |
| **m5, m5a, m6i, m6a** | General-purpose, x86 | **N2D** | `N2D AMD Instance Core/Ram` |
| **m6g, m7g** | General-purpose, ARM | **T2A** | `T2A Instance Core/Ram` |

### Database Mapping (RDS/Aurora)

When mapping `managed_db` rows (RDS/Aurora), follow these rules to ensure a robust deployment match:
1. **Engine to GCP Service:**
   - **MySQL / PostgreSQL / SQL Server** -> **Cloud SQL** (use `SQLGen2InstancesCPU` / `SQLGen2InstancesRAM` / `SQLGen2InstancesPD-SSD` resource groups).
   - **Aurora PostgreSQL / RDS PostgreSQL** (when high performance/availability is required) -> **AlloyDB** (use `AlloyDB` resource group).
   - **ElastiCache Redis / Valkey** -> **Cloud Memorystore for Redis**.
   - **ElastiCache Memcached** -> **Cloud Memorystore for Memcached**.
2. **HA Architecture Mapping:**
   - Check the `deployment_option` or the AWS description/operation for `Multi-AZ` or `HA`.
   - **Multi-AZ** -> Map to GCP **Regional** (HA) SKUs (e.g. `SQLGen2InstancesCPU (Regional)`).
   - **Single-AZ** -> Map to GCP **Zonal** SKUs.
3. **Storage & IOPS Sizing:**
   - Database storage rows must be mapped to Cloud SQL storage (e.g., `SQLGen2InstancesPD-SSD` or regional equivalent).
   - For high-IOPS databases (e.g., matching AWS `io1`/`io2` or high provisioned IOPS), ensure the GCP Persistent Disk or Cloud SQL Storage tier is sized to meet or exceed the provisioned IOPS.
9. **Strict Region Descriptor Matching.**
    For standard network egress/data transfer and storage SKUs, the SKU description must match the target row's region display name (e.g. Mumbai -> `... from Mumbai`, Northern Virginia -> `... from Northern Virginia`). NEVER map to a Singapore SKU for egress originating in Mumbai or Virginia; the validation gates will instantly fail.
10. **Pick concrete `gcp_sku_id` after evaluating alternatives.** For
    each AWS LI, don't latch onto the first plausible SKU. Run a small
    evaluation:

   a. **Find the obvious candidate** by matching
      `category.serviceDisplayName` + `category.resourceGroup` +
      region + description in the bundled catalog
      (`data/skus/<service_id>.json.gz`). Component-affinity:
      `core` → vCPU/Core SKU, `ram` → RAM/Memory,
      `storage` → Storage/Disk, `accelerator` → GPU/TPU.

   b. **Surface ONE meaningful alternative** if any exists:
      - A **cheaper-but-equivalent** option (e.g. N2D-AMD vs N2-Intel
        for general-purpose compute; T2A-ARM if the AWS row is a
        Graviton family; gp3 → Hyperdisk Balanced when both fit).
      - A **higher-tier** option only if it's marginally pricier
        *and* the AWS row's description supports it (e.g. Cloud SQL
        Enterprise Plus vs Enterprise when the row already pays for
        HA via Multi-AZ).
      - Skip this step if the choice is unambiguous (S3 → Cloud
        Storage, KMS → Cloud KMS, Route 53 → Cloud DNS, etc.).

   c. **Score both via the 60/40 rule.** Cheaper option wins unless
      the costlier option's perf/availability advantage is **material
      AND matches what the AWS row is paying for** — see "Bill is
      ground truth" above. "Slight perf benefit" is not a reason to
      deviate; "AWS description literally says Multi-AZ / HA" is.

   d. **Append a note** to `projection-audit/mapping-notes.md`,
      keyed on `aws_li_key`. The file is **append-only** — each
      mapping sub-agent contributes its own entries; nobody rewrites
      others'. This is the working journal that gets reviewed in
      Phase 3 before outlier queries fire. Use entries to capture
      *anything you're not 100% sure about*: the alternative you
      considered, a unit-multiplier you derived from an ambiguous
      description, a back-check that came back degenerate, an AWS
      service you weren't sure mapped cleanly to GCP. `projection_note`
      in the mapping row itself stays terse — one sentence on why the
      picked SKU was picked.

      ```markdown
      ## <aws_li_key> — <short row description>
      - Picked: <gcp_sku_id> <sku-name>
      - Alt: <gcp_sku_id> <sku-name> (+X% cost, <perf delta>) —
        rejected per 60/40, <one-line reason>
      - Open: unit_multiplier=<X>, derived from Description
        "<phrase>"; back-check <pass | fail-by-Nx | degenerate>;
        <what to verify or how I'd want this challenged>
      ```

      `Alt` and `Open` lines are optional — only when there's
      something real to record. Obvious unambiguous rows (S3 → Cloud
      Storage at multiplier 1.0, back-check passes) don't need an
      entry at all.

   The point of this step isn't ceremony — it's to force one explicit
   comparison so the 60/40 rule actually fires instead of getting
   overridden by the first plausible-sounding rationale, *and* to
   surface uncertainties for review before outlier rationalization
   sets in.
8. **`unit_multiplier` is the conversion factor** so that
   `total_usage × unit_multiplier × rate_usd` yields GCP-billable
   cost. Common pairings:
   - `Hrs → h` at vCPU count (core) / RAM-GB (ram)
   - `GB-Mo → GiBy.mo` at 1.0
   - `Requests-1000 → Count-10000` at 0.1
   - `Requests → TiBy` at `avg_msg_bytes / 1024^4` (SQS → Pub/Sub)

   **For `break_down` compute and database rows, read vCPU and RAM from
   the catalog — do NOT infer from the operation string or model weights.**
   Phase 1 Step 7 wrote `instance_vcpus` and `instance_ram_gb` into
   `aws_li_catalog` exactly for this purpose. Use them:

   ```sql
   SELECT c.aws_li_key, c.operation, c.total_usage,
          c.instance_vcpus, c.instance_ram_gb, c.instance_arch
   FROM   aws_li_catalog c
   WHERE  c.aws_li_key = '<key>';
   -- Set unit_multiplier = c.instance_vcpus  for the 'core' row
   -- Set unit_multiplier = c.instance_ram_gb for the 'ram'  row
   ```

   If `instance_vcpus` is NULL (lookup miss flagged in Phase 1 anomalies),
   surface a `projection_note` that the multiplier was inferred, and flag
   it in `mapping-notes.md` under `Open:` so Phase 3 can challenge it.

   **Don't normalize EC2/RDS hours to 730 hr/mo.** `total_usage` from
   the bill is already the actual hours billed (744 for a 31-day
   month, 720 for 30-day, 672/696 for Feb). Re-normalizing
   systematically skews the projection.
9. **Back-check is mechanical, not judgment.** This is the predicate
   that catches the rationalization trap. Rule #9 is the ONLY rule
   that's allowed to be a hard predicate — when it fires, the
   conclusion is fixed; you may not talk around it. Apply both
   branches:

   **Branch A — `aws_amortized_cost > $1`: ratio test.**
   `gcp_projected_cost / aws_amortized_cost` must be within 3×
   (i.e. between 0.33 and 3.0). Outside → `unit_multiplier` is wrong,
   full stop. Not "AWS rate is RI / SP / EDP discounted" (that's a
   *visible* line item; outlier triage handles it separately). Here
   it's about the multiplier. Multiplier first, every time.

   **Branch B — `aws_amortized_cost ≤ $1` (the phantom-zero case):
   absolute test, NOT a ratio.**
   `gcp_projected_cost` must also be `≤ $10`. Larger →
   **`unit_multiplier` is wrong, full stop.** No exception allowed
   for:
   - "Free tier we don't see" — only valid if a free-tier line item
     is visible in the bill.
   - "Vendor / reseller absorbs this" — only valid if a discount
     line item explicitly absorbs it.
   - "AWS rounded to zero" — when AWS itself stays near zero, GCP
     should too.
   - "Promotional credit applied silently" — credits show as line
     items.
   - Any "the bill is unusual" narrative.

   No visible mechanism = no exception = flip the multiplier.

   **Default for `per-N` descriptions.** When the AWS Description
   contains `"per 1,000"`, `"per 10,000"`, `"million"`, `"thousand"`,
   `"<N>-hour"`, `"<N>-month"`, or any rate-denomination phrase, the
   default `unit_multiplier = 1.0`. `Usage Quantity` is raw count;
   the description names the *rate denomination* AWS uses to publish
   the price, not the unit of the quantity column. This applies
   uniformly to S3 ops, DynamoDB requests, WAF requests, NAT data
   processed, CloudFront requests, KMS API calls, SNS publishes,
   SQS messages, Lambda invocations, etc.

   **Forced two-interpretation compare.** Whenever you're tempted to
   set `unit_multiplier ≠ 1`, compute the projection under BOTH
   `m=1` and `m=N` explicitly. Pick the one that satisfies Branch A
   or B above. Don't pick on description-text alone.

   **What to do when Branch A or B fails:** Try the other multiplier
   interpretation per the forced two-interpretation compare. If
   neither satisfies the predicate, the unit genuinely doesn't
   reconcile (rare — gp3 MiBps-month overages, OpenSearch throughput
   tiers can do this) → switch to `passthrough` rather than guess.
   Never accept a Branch A / B failure as "the bill is unusual".
10. **`passthrough` is the LAST RESORT, not a comfortable fallback.**
    The whole point of this skill is to project AWS spend to a GCP
    equivalent. A passthrough row tells the customer nothing — its
    Diff is $0 by definition, neither a win nor a loss, just noise
    in the report. Every passthrough is a row where we couldn't do
    our job.

    **Services that may NEVER be passthrough — they always have a GCP
    target.** If a row's `product` matches any entry below, passthrough is
    **FORBIDDEN** — you MUST map it (run `find-sku.sh` to get the SKU).
    Passing these through is the single most common way this skill fails: on a
    real bill it can leave the *majority* of spend unprojected, which makes the
    whole report worthless.

    | AWS product (substring of `product`) | Mandatory GCP target — never passthrough |
    |---|---|
    | EC2 / Elastic Compute Cloud | Compute Engine (`break_down` core+ram) |
    | EBS / Elastic Block Store | Hyperdisk Balanced / Persistent Disk |
    | RDS / Aurora | Cloud SQL or AlloyDB |
    | ElastiCache | Cloud Memorystore (Redis/Memcached) |
    | S3 / Simple Storage Service | Cloud Storage |
    | Data Transfer / Bandwidth / egress | Compute Engine / Networking egress |
    | ELB / ALB / NLB / Load Balancing | Cloud Load Balancing |
    | Lambda | Cloud Run / Cloud Run Functions |
    | Route 53 | Cloud DNS |
    | KMS | Cloud KMS |
    | CloudWatch Logs / Metrics | Cloud Logging / Cloud Monitoring |

    For any row in that table, **none** of these are valid reasons to
    passthrough: "I'm not sure of the exact SKU", "the price ratio looks
    weird", "the AWS rate is RI/SP/PRC-discounted", "it's a small row". They
    are mapping problems to **solve**, not reasons to give up. If you genuinely
    cannot find the exact SKU, map to the closest one and set
    `confidence: low` — but **do not passthrough**.

    **Valid reasons to use `passthrough`:**

    - **No GCP equivalent exists at all.** AWS Support / Enterprise
      Support; AWS Config configuration-item recording (Cloud Asset
      Inventory is free, so just zero-out — that's not passthrough,
      it's `ignore`); AWS Marketplace third-party SaaS; niche
      AWS-only sub-features like S3 Intelligent-Tiering per-object
      monitoring fee (GCS Autoclass bundles tiering into the storage
      rate with no separate line item).
    - **Implied-rate back-check fails on Branch A/B/m=1/m=N** —
      genuinely irreconcilable unit. Rare.

    **Invalid reasons — DO NOT use passthrough for any of these:**

    - **"Price ratio is unfavorable / weird"** — wrong reason.
      Example seen in the wild: S3 Glacier Transition → GCS Class A
      ops, ratio ~0.33. That ratio is *information* — GCS is
      cheaper for this operation. Map it. Don't drop it because
      the math looks lopsided.
    - **"AWS rate looks anomalously low (PRC / RI / SP)"** — wrong.
      The AWS rate is post-discount and we use it as the comparison
      baseline; GCP at list might also be discountable by a
      negotiated GCP rate card we can't see. Show the apples-to-
      oranges gap as the diff; note it in `projection_note`.
    - **"I'm not 100% sure of the right GCP SKU"** — that's what
      `confidence: medium / low` is for, not passthrough.
    - **"It's a small line item, I'll save thinking time"** —
      cumulative passthroughs erode the report's value. Map it or
      mark it `confidence: provisional` for Phase 3 to scrutinize.

    If you find yourself reaching for passthrough, pause and ask:
    "Is there really NO GCP service that does roughly the same
    work?" If you can name a GCP service that does — Cloud Storage,
    Compute Engine, Cloud SQL, etc. — there's almost certainly a
    SKU. Use `find-sku.sh`. Map it.

    `projection_note` for passthroughs must explicitly state which
    of the two valid reasons applies. "No equivalent" or "unit
    irreconcilable" — anything else is suspect and Phase 3 will
    overturn it.

    **Passthrough budget — a hard self-check before you return.** A
    correctly-mapped bill has at most a handful of passthrough rows and
    **well under 5% of total AWS spend** in passthrough. Before signaling
    done, sum `aws_amortized_cost` of your passthrough rows and divide by your
    slice's total. If it exceeds 5% — or if *any* row from the
    never-passthrough table above is passthrough — **you are not finished**:
    the excess is mappable-service rows you gave up on. Go back and map them.
    (A run that passes through the majority of spend has failed, even if every
    individual passthrough "felt" justified.)
11. **`projection_note`**: one sentence per row. Capture the
    rationale ("baseline gp3 → Hyperdisk Balanced", "Multi-AZ → Cloud
    SQL Regional", "RI-applied: AWS rate is post-RI, compare GCP
    CUD", etc.). The reviewer reads these.

## Service-specific mapping guides

These services have non-obvious billing model differences that a
one-line checklist entry can't capture. Read the relevant section
before mapping any row in that category.

### DynamoDB → Cloud Bigtable / Cloud Firestore

DynamoDB bills on **provisioned or on-demand capacity units** (WCU, RCU)
plus storage GB-month, not on instance hours. GCP has no direct
WCU/RCU analogue. Map based on the workload pattern visible in the
bill:

| DynamoDB charge | GCP target | Mapping logic |
|---|---|---|
| Provisioned WCU / RCU (consistent traffic) | **Cloud Bigtable** node-hours | Map one Bigtable SSD node ≈ 10K QPS sustained; WCU and RCU are not interchangeable with node count — use `passthrough` and note "sizing requires workload analysis" unless the bill shows a clear capacity tier |
| On-Demand reads (DynamoDB `ReadRequestUnits`) | **Cloud Firestore** read ops | `unit_multiplier = 1.0`; Firestore bills per read op; back-check ratio |
| On-Demand writes (`WriteRequestUnits`) | **Cloud Firestore** write ops | same; separate SKU per write vs read |
| DynamoDB Streams | Cloud Dataflow / Pub/Sub | `passthrough` unless stream volume is visible; note in `projection_note` |
| DynamoDB storage (GB-month) | Cloud Bigtable or Firestore storage | `unit_multiplier = 1.0` (GB-month → GiBy.mo, adjust 1 GB = 0.931 GiBy if precise) |
| DynamoDB global table replicas | Bigtable multi-cluster replication surcharge | No direct SKU; use `passthrough` and note |
| DynamoDB backup / PITR | Bigtable backup GB-month | `unit_multiplier = 1.0` |

**Key rule:** do NOT blindly map DynamoDB WCU to Firestore write ops at
unit_multiplier=1. The billing denominations are different (one DynamoDB
WCU ≠ one Firestore write op in capacity). If you cannot find a
reconcilable unit, use `passthrough` and write a specific note explaining
which capacity concept has no equivalent — this is one of the few valid
`passthrough` uses.

---

### Lambda → Cloud Run / Cloud Run Functions

Lambda bills on **invocations + GB-seconds** (duration × memory). GCP
bills Cloud Run on **vCPU-seconds + GiB-seconds** separately.

| Lambda charge | GCP target | unit_multiplier |
|---|---|---|
| `Request` (invocation count) | Cloud Run Functions — invocations | `1.0` (1 invocation = 1 invocation) |
| `Duration` (GB-seconds) | Cloud Run Functions — compute (GiB-s) | `1.0` — Lambda GB-seconds and Cloud Run GiB-seconds are the same denomination; back-check passes |
| Lambda@Edge requests | Cloud Run at network edge or Cloud CDN | `passthrough` — no direct peer; note |
| Lambda@Edge duration | same | `passthrough` |
| Provisioned Concurrency | Cloud Run min-instances idle charge | Map to Cloud Run idle vCPU-seconds × memory ratio; flag as `confidence: low` |

**ARM (Graviton) Lambda:** if `operation` contains `"arm64"`, map to
Cloud Run on ARM or Cloud Run Functions 2nd gen (which bills the same
SKU). No multiplier adjustment needed; rate is the same as x86 for
Cloud Run Functions.

**Memory-to-vCPU conversion for Cloud Run (if mapping to standard Cloud Run,
not Functions):**
Cloud Run bills vCPU and memory separately. Lambda GB-seconds conflate
them. Split as:
- vCPU-seconds = GB-seconds × (lambda_vcpu_fraction) — Lambda allocates
  vCPUs proportionally to memory; at 1 GB → 0.5 vCPU (approx), 2 GB → 1 vCPU.
  For the projection, default to 0.5 vCPU per 1 GB memory unless the
  bill shows a specific function configuration.
- memory GiB-seconds = GB-seconds × 0.931 (GB → GiB)

If this split produces an A-branch back-check failure, fall back to
mapping Lambda GB-seconds → Cloud Run GiB-seconds at `unit_multiplier=1.0`
(treating GiB ≈ GB) and note the approximation.

---

### ARM (Graviton) routing decision tree

When `aws_li_catalog.instance_arch = 'arm64'` (set by Phase 1 from the
lookup table), follow this decision tree for the GCP family:

```
Is the target region available?
  ├─ C4A (Axion, ARM) available in region?
  │    └─ YES → map to C4A (best ARM-native match, GCP Axion)
  │    └─ NO  ↓
  ├─ T2A (Tau ARM) available in region?
  │    └─ YES → map to T2A (general-purpose ARM)
  │    └─ NO  ↓
  └─ Fall back to N2D (AMD x86) — note architecture change in projection_note
```

C4A regions (as of 2025): `us-central1`, `us-east4`, `europe-west4`,
`asia-southeast1`. T2A adds: `us-west1`, `europe-west1`, `asia-east1`,
`asia-northeast1`, `southamerica-east1`.

Write the architecture choice into `projection_note` on every Graviton
row. Never silently fall back to N2 (Intel x86) from an ARM instance
without a note. 

For Managed Databases (RDS/Aurora) running on Graviton (ARM): since C4A is exclusive to Cloud SQL Enterprise Plus, you must select the Enterprise Plus edition and document the reason in the `projection_note` (e.g., *"C4A ARM compatibility requires Enterprise Plus"*).

---

### Data Transfer / Egress matrix

Data-transfer rows (`DataTransfer` product or `Bandwidth` usage_type) need
the destination dimension — it determines which GCP egress tier applies.

| AWS transfer sub-type (from `operation`) | GCP SKU family | Notes |
|---|---|---|
| `"regional data transfer"` / inter-AZ | Compute Engine inter-zone egress (`InterzoneEgress`) | ~$0.01/GB; same-region cross-zone |
| `"inter-region"` within same continent | Compute Engine inter-region egress (`InterregionEgress`) | Rate varies by region pair |
| `"internet"` outbound to internet | Compute Engine internet egress (`InternetEgress`) | Tiered; first 1 TB/month cheaper |
| CloudFront → internet | Cloud CDN egress | Map if CDN row is separate; else treat as internet egress |
| `"Direct Connect"` / `"VPN"` | Cloud Interconnect / Cloud VPN | Separate SKUs; `passthrough` if Interconnect port cost isn't in bill |
| Cross-region `S3` replication | Cloud Storage multi-region replication fee | No direct SKU; `passthrough` with note |
| `"EKS NAT Gateway"` / NAT Gateway processed bytes | Cloud NAT processed bytes | `unit_multiplier = 1.0` (GB → GB) |
| Transfer within same AZ (free on AWS) | No GCP charge for same-zone | `strategy='ignore'` |

For internet egress: AWS and GCP both apply tiered pricing. Phase 4's
blended-rate computation handles this — just map to the correct SKU
and let Phase 4 compute the right rate from the actual GB volume.

## Finding SKUs — use the helper

84K SKUs is too many to grep through interactively. Use
`scripts/find-sku.sh`. It emits TSV rows of
`service<TAB>skuId<TAB>resource_group<TAB>usage_type<TAB>regions<TAB>description<TAB>rate_usd<TAB>unit`:

```bash
# Storage class for an EBS gp3 mapping in Singapore:
scripts/find-sku.sh --service "Compute Engine" --region asia-southeast1 \
                    --keyword "Hyperdisk Balanced Capacity" --usage-type OnDemand

# CUD-1yr core rate for N2D AMD in Singapore:
scripts/find-sku.sh --service "Compute Engine" --region asia-southeast1 \
                    --resource-group CPU --keyword "N2D AMD" \
                    --usage-type Commit1Yr

# Find anything matching a keyword across all 110 services:
scripts/find-sku.sh --keyword "external IP"
```

All flags optional. Without `--service` it scans every service
(~1 second). `--keyword` is a regex matched case-insensitively against
`description`. The price column is the first non-zero `tieredRate` —
the value to put in `gcp_sku_rates.rate_usd` later (Phase 4).

### Common AWS → GCP service-file landings

Saves you the "which file do I open" question:

| AWS feature | GCP service file (display name) |
|---|---|
| EC2 instance core/RAM, Spot, sole-tenancy | Compute Engine |
| EBS gp3 / io2 / sc1 / st1 | Compute Engine (Hyperdisk Balanced/Throughput/Extreme; Persistent Disk for legacy) |
| Snapshots | Compute Engine (resource_group `Storage`, "Snapshot") |
| Public IPv4 / EIP | Compute Engine (resource_group `IpAddress`) |
| NAT Gateway / Transit Gateway | Networking |
| Inter-AZ / inter-Region egress | Networking (look for `InterregionEgress`, `InterzoneEgress`) |
| Internet egress | Networking (look for `InternetEgress` + destination region in description) |
| ELB/ALB/NLB | Networking (Cloud Load Balancing entries) |
| Route 53 zones + queries | Cloud DNS |
| Shield/WAF | Networking (Cloud Armor entries — same parent as Networking) |
| S3 storage + ops | Cloud Storage |
| RDS / Aurora compute, RAM, storage | Cloud SQL or AlloyDB (resource_group `SQLGen2InstancesCPU` / `SQLGen2InstancesRAM` / `SQLGen2InstancesPD-SSD`) |
| ElastiCache Memcached | Cloud Memorystore for Memcached (NOT Redis Basic) |
| ElastiCache Redis / Valkey | Cloud Memorystore for Redis (cluster mode → Redis Cluster) |
| DynamoDB | Cloud Bigtable or Cloud Firestore |
| Kinesis / SNS / SQS | Cloud Pub/Sub |
| Lambda | Cloud Run Functions or Cloud Run |
| EKS | Kubernetes Engine |
| CloudWatch Logs / Metrics | Cloud Logging / Cloud Monitoring |
| KMS | Cloud Key Management Service (KMS) |
| Secrets Manager / SSM Parameter Store | Secret Manager |

When in doubt, run `find-sku.sh --keyword "<aws-feature-word>"`
unscoped first to see which service file owns it.

## Common pitfalls (re-read before mapping)

| Pitfall | Reality | What to do |
|---|---|---|
| In-use Public IPv4 is "free on GCP" | Charged at $0.005/h since 2020 (sku `C054-7F72-A02E`) | Map to Compute Engine `IpAddress` SKU; don't zero it out |
| ElastiCache Memcached → Redis | Memcached and Redis are different engines with different SKUs in Memorystore | Memcached → Cloud Memorystore for Memcached; Redis → Cloud Memorystore for Redis |
| AWS `Usage Quantity` for "per 1,000" lines | Raw count; the description names the rate denomination but the column is unit count | `unit_multiplier = 1.0`; back-check rule #9 |
| "Production-sized DB → Multi-AZ" | The bill says what the customer pays for; large size ≠ HA | Rule #6 — only HA when description literally says so |
| EC2 hours = 730/mo | Bill carries actual hours (720/744/672) | Rule #8 — don't normalize |
| `"reserved instance applied"` row | Real workload at RI-amortized rate, not a discount line | Phase 1 already classified this `is_workload=TRUE`, `pricing_model='Committed'`; compare GCP CUD, not OD in outliers |
| Windows OS premium | Customer may BYOL or pay GCP marketplace; depends on posture | Default to including Windows DC SKU; flag in `projection_note` so customer can request BYOL adjustment |
| AWS ARM (Graviton) → x86 | T2A is ARM, matches `c6g`/`m6g`/`r6g` shape | Map ARM-to-ARM, don't fall back to x86 N2 |

### ECS / Fargate → Cloud Run

Fargate bills on **vCPU-hours + GB-hours of memory**, separately from
ECS task-definition count. The `Fargate` product shows two charge types
in the bill: `vCPU` (per vCPU-second) and `Memory` (per GB-second).

| Fargate charge | GCP target | unit_multiplier |
|---|---|---|
| `AWS Fargate vCPU Hours:perCPU` | Cloud Run — vCPU-seconds | `3600.0` (hrs → seconds); Cloud Run bills per vCPU-second |
| `AWS Fargate GB Hours:perGB` | Cloud Run — memory GiB-seconds | `3600 × 0.931` (hrs → GiBy-seconds; 1 GB = 0.931 GiBy) |
| `AWS Fargate Windows vCPU/GB` | Cloud Run (no Windows); use standard Linux rate | flag in `projection_note` |

**ECS cluster fee:** ECS itself has no control-plane charge (unlike EKS).
If you see a `Amazon Elastic Container Service` line with a small flat charge,
check whether it's Fargate container insight or CloudWatch — map to the
appropriate monitoring SKU or `passthrough` if no GCP equivalent.

---

### EKS → GKE

EKS bills a **per-cluster, per-hour** control-plane fee (currently
$0.10/hr in most regions). GKE's control plane also has a per-cluster fee.

| EKS charge | GCP target | Notes |
|---|---|---|
| `Amazon Elastic Kubernetes Service:AmazonEKS` control-plane hours | GKE cluster management fee | `unit_multiplier = 1.0` (hr → hr); rate from GKE `ClusterManagement` SKU |
| EC2 worker nodes under EKS | Compute Engine (standard instance mapping) | Map normally via compute partition; EKS doesn't add a per-node surcharge |
| EKS Anywhere / Outposts | `passthrough` | No GCP equivalent for on-prem |

---
### MSK (Kafka) & OpenSearch / Elasticsearch → self-hosted GCE (sized to real footprint)

These have no managed GCP equivalent, so they run **self-hosted on Compute Engine**.
The instance detail is in the CUR's `operation` field (e.g. `"broker hour for
Kafka.t3.small"`, `"r6g.large.search instance hour"`), so the orchestrator has
ALREADY extracted the real footprint into each manifest row:
`instance_type`, `instance_vcpus`, `instance_ram_gb`, and `instance_count`
(= instance-hours ÷ hours-in-month = the **actual** node count).

For each such instance/broker line:
- `break_down` into `core` + `ram` components and map to Compute Engine exactly
  like an EC2 instance, using the row's extracted `instance_vcpus` / `instance_ram_gb`.
- Multiply by the row's `instance_count` — this is the real node count from the
  bill. **Do NOT invent a replication factor (no "3× brokers") or a disk size.**
- `projection_note`: *"<service> self-hosted on GCE: <count>× <machine> (from
  <instance_type>, <vcpus>vCPU/<ram>GB)."*

Storage sub-lines (`gp3`/`gp2` provisioned storage, `ESDomain` GB-Mo) map to
Persistent Disk via the block_storage rules; transfer lines via data_transfer.
Only if a row has NO extracted specs (opaque line, `instance_vcpus` null) fall
back to `strategy='map'` at cost parity and flag it low-confidence for review.

---

### Fargate-on-EKS vs plain Fargate

When the bill shows `Amazon Elastic Kubernetes Service` + `AWS Fargate`
charges together, the customer runs Fargate pods in EKS. Map:
- Fargate charges per the Fargate guide above.
- EKS control-plane hours per the EKS guide above.
These are separate line items; don't conflate them.

---

### AWS Glue → Cloud Dataflow / Cloud Data Fusion

Glue bills on **DPU-hours** (Data Processing Units, each = 4 vCPU + 16 GB).

| Glue charge | GCP target | unit_multiplier |
|---|---|---|
| Glue ETL job DPU-hours | Cloud Dataflow — vCPU-hours | `4.0` per DPU (1 DPU = 4 vCPUs); memory component → Dataflow memory GB-hours at `16.0` |
| Glue crawler DPU-hours | Cloud Data Catalog + Dataflow | `passthrough` with note — crawlers don't have a direct analog |
| Glue Data Catalog storage / requests | Cloud Data Catalog | No per-storage charge; `passthrough` or `ignore` depending on cost size |

---

---

### GPU / Accelerator instance mapping

When `workload_class = 'GPU'` in `aws_li_catalog`, use `strategy = 'break_down'` with components `core`, `ram`, **and** `accelerator`. Never map GPU instances to CPU-only families (N2D, N2, E2) — this is the single most common cause of large cost-projection errors.

#### AWS GPU family → GCP family

| AWS family | Accelerator | → GCP family | GCP GPU SKU keyword | Notes |
|---|---|---|---|---|
| p4d | 8× A100 40GB SXM | **A2** (a2-highgpu-8g) | `A2 Instance Core` | 96 vCPU, 1152 GB RAM |
| p4de | 8× A100 80GB SXM | **A2 Ultra** (a2-ultragpu-8g) | `A2 Ultra Instance Core` | 96 vCPU, 1152 GB RAM |
| p5, p5.48xlarge | 8× H100 80GB | **A3** (a3-highgpu-8g or a3-megagpu-8g) | `A3 Instance Core` | 192 vCPU, 2048 GB RAM |
| p5e, p5en | 8× H200 141GB | **A3 Ultra** (a3-ultragpu-8g) | `A3 Ultra Instance Core` | 192 vCPU, 2048 GB RAM |
| p3, p3dn | V100 16/32GB | **A2** (recommend migration path) | `A2 Instance Core` | V100 still on N1; A2 is recommended |
| p2 | K80 | **N1 custom GPU** | `N1 Predefined Instance Core` | GCP K80 (`nvidia-tesla-k80`) on N1 |
| g4dn | NVIDIA T4 16GB | **G2** (g2-standard-*) | `G2 Instance Core` | T4→L4 is recommended migration |
| g5 | NVIDIA A10G 24GB | **G2** (g2-standard-*) | `G2 Instance Core` | GCP has no A10G; L4 is successor |
| g6, gr6 | NVIDIA L4 24GB | **G2** (g2-standard-*) | `G2 Instance Core` | Direct L4 → G2 match |
| g6e | NVIDIA L40S 48GB | **G2** (g2-standard-*) | `G2 Instance Core` | L40S also maps to G2 family |
| g5g | T4G, Graviton2 | **T2A** (tau ARM) | `C4A Instance Core` / `T2A Instance Core` | ARM arch; use T2A/C4A |
| g3, g3s | NVIDIA M60 (legacy) | **N1 custom GPU** | `N1 Predefined Instance Core` | Legacy; M60 on N1 |
| g4ad | AMD Radeon V520 | **N1 custom GPU** | `N1 Predefined Instance Core` | GCP has no Radeon; use N1 |
| inf1 | AWS Inferentia | **G2** (g2-standard-*) | `G2 Instance Core` | L4 is GCP inference GPU |
| inf2 | AWS Inferentia2 | **G2** (g2-standard-*) | `G2 Instance Core` | Scale up: A2 for larger inf2.48xlarge |
| trn1, trn1n | AWS Trainium | **A2** (a2-highgpu-*) | `A2 Instance Core` | Trainium training → A2 A100 |
| trn2, trn2u | AWS Trainium2 | **A3** (a3-highgpu-8g) | `A3 Instance Core` | Trainium2 scale → A3 H100 |
| dl1 | Habana Gaudi | **A2** (a2-highgpu-*) | `A2 Instance Core` | GCP has no Gaudi; A2 is closest |
| f1, f2 | Xilinx FPGA | **N1 custom** (`passthrough` if no FPGA need) | — | GCP has no FPGA; if FPGA cost only, `passthrough` |

#### GPU unit_multiplier for `accelerator` component

For the `accelerator` component in a `break_down` row, look up the GPU SKU and use:
```
unit_multiplier = gpu_count  (number of GPUs per instance × instance_count)
```
The GPU SKU is priced per GPU-hour. Find it via:
```bash
scripts/find-sku.sh --service "Compute Engine" --region <gcp_region> --keyword "A2 GPU" --resource-group GPU
# e.g. "NVIDIA Tesla A100 running in Americas" — one SKU per GPU model per region
```

#### Memory-optimized instance mapping

| AWS family | GB/vCPU | → GCP family | GCP billing SKU |
|---|---|---|---|
| r5, r6i, r7i, r7iz | 8 GB/vCPU, Intel | **N2** (n2-highmem-*) | `N2 Predefined Instance Core/Ram` |
| r5a, r6a, r7a | 8 GB/vCPU, AMD | **N2D** (n2d-highmem-*) | `N2D AMD Instance Core/Ram` |
| r6g, r7g, r8g | 8 GB/vCPU, ARM | **T2A / C4A** | `C4A Instance Core/Ram` |
| x1, x1e | 15–30 GB/vCPU | **M1** (m1-megamem or m1-ultramem) | `Memory Optimized Instance Core/Ram` |
| x2idn, x2iedn, x2iezn | 16–32 GB/vCPU | **M3** (m3-megamem or m3-ultramem) | `Memory Optimized Instance Core/Ram` |
| x2gd | 16 GB/vCPU, ARM | **T2A** | `C4A Instance Core/Ram` |
| u-3tb1, u-6tb1 | 14–27 GB/vCPU, TB-scale | **M2** (m2-megamem or m2-ultramem) | `Memory Optimized Instance Core/Ram` + `Memory Optimized Upgrade Premium` |
| z1d | 8 GB/vCPU, 4 GHz | **C2** (compute-optimized) | `Compute optimized Core/Ram` |

**IMPORTANT — M-series SKU naming:** GCP billing SKUs for M1, M2, M3 all say `"Memory Optimized Instance Core running in [Region]"` — there is no "M1"/"M2"/"M3" literal in the description. The family is inferred from the machine type label in usage details. This is expected; the validator knows about it.

**IMPORTANT — N2 vs N2D:** r-family Intel (r5, r6i, r7i) must map to `n2-highmem-*` (N2 Intel), not `n2d-highmem-*` (N2D AMD). The billing SKU is `N2 Predefined Instance Core` for N2 and `N2D AMD Instance Core` for N2D — the validator enforces this.

## Coverage check

The orchestrator runs a deterministic gate after merge_mappings.py. You do not need to verify coverage yourself — write your three `_mappings.json` files and stop.
