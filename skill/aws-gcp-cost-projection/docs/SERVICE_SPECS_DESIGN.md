# Per-Service Charge-Type Specs — Design & Migration Plan

Status: **CUTOVER COMPLETE (Layer 1.5)**. 54 services specced in `data/service_specs.json`.
Pipeline classification runs via `spec_dispatch.py` (OWNS_CLASSIFICATION = True) through `classify_mechanics.py`.
Parity: 98.1% agree ($977,759), 0 differ, 0 spec gap, 0 ambiguous.
For the complete system blueprint across all layers (0–4), see [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md).

---

## Why this exists

`classify_mechanics.py` decides each bill row's **pricing mechanic** with a single
flat chain of ~37 global rules, first-match-wins. The chain mixes two different
questions:

1. **What service is this?** (identity)
2. **Given the service, what charge-type is this row and how is it priced?**

Because the mechanic is chosen by **global** pattern-matching, broad rules
(`block_storage`'s `usage_type ILIKE '%Storage%'`, `per_request`, `flat_hourly`)
are greedy — they grab rows from services they shouldn't, which is why
`block_storage` already carries 5 hard-coded exclusions (S3, DynamoDB, Lambda,
VPC, SageMaker). That accretion is the symptom of a leaky abstraction.

This change splits the two questions:

```
Stage 1  identity     canonical_service(product_code)   -> "ec2" / "rds" / ...   [DONE: Layer 1, product_code-first]
Stage 2  charge-type  service_specs[canonical]          -> which charge_type, which fields   [THIS]
Stage 3  mechanic     reusable pricing primitive         -> compute_breakdown / block_storage / ...   [UNCHANGED]
```

The **mechanic vocabulary is kept** (shared pricing math — EC2, EMR nodes, MSK
brokers all price compute the same way; EBS, RDS storage, MSK storage all price
GB-Mo the same way). What changes is **how the mechanic is chosen**: per-service
and explicit, instead of by global regex order. Cross-service ordering
fragility disappears — charge-types are only ordered *within* a service.

---

## What was explicitly decided

- **Keep the broad mechanic labels** (storage/compute/etc.) — they are the shared
  pricing primitives, not the problem. We re-evaluate the *set* once every service
  has a spec: the service-specific "mechanics" (opensearch/msk/elasticache/...)
  are really charge-types pointing at a shared mechanic, so the mechanic
  vocabulary should *shrink* to ~6-8 true primitives while the ~27 service rules
  become spec entries.
- **Kill the greedy global rules**, not the labels. Each becomes per-service
  charge-types + a thin generic fallback for the long tail.
- **DB readability: NOT one-field-per-row (EAV).** DuckDB is columnar; wide
  tables with explicit columns are faster AND more readable than EAV. Instead:
  - one row per **charge-type** (already ~true after dedup),
  - every relevant column populated or marked `not_available`,
  - two new columns: `charge_type` and `field_provenance`,
  - an optional **tall inspection VIEW** (`key, field, value, provenance`) for
    eyeballing — a view on top, never the primary store.
- **A thin generic fallback stays.** "Global for every service" is the uniform
  *structure* + the aspiration, seeded with high-spend services and grown by the
  enrichment loop. Services without a spec hit the legacy generic rules. Full
  coverage is the asymptote, not day one.

---

## Schema (see `data/service_specs.json` `_meta` for the authoritative copy)

```
<canonical_service>:
  display: str
  product_codes: [AWS ProductCode, ...]      # authoritative identity
  charge_types:                              # ordered; first match wins WITHIN the service
    - id: str
      description: str
      match: { <predicate>: ... }            # ALL present predicates must be true
      fallback_match: {...}                   # OR-ed alternative (PDF/flat bills: signal in operation/product)
      mechanic: <one of the mechanic vocabulary>
      fields:                                 # what full context for this charge-type requires
        - { name, source: cur|table|api|llm, required: bool }
      source_rule: str                        # traceability to the classify_mechanics rule it came from
      notes: str
```

Match predicate vocabulary and mechanic/field-source enums are documented in the
registry `_meta`. All regex are case-insensitive. `unit_hourly:true` mirrors
`_is_hourly_unit` (accepts blank unit for PDF/flat bills).

---

## Field completeness & provenance

Every charge-type lists **all** fields it needs for full context, even ones a
given bill omits. `field_state.py` (already exists) stamps each as
`present_in_cur` / `needs_api` / `not_available`. The `source` hint drives the
enrichment order below. The realised source of each value is recorded in
`field_provenance` so a billed fact and an inferred value never look identical.

---

## Enrichment loop (the "better per CUR" mechanism)

A **separate** stage after classification (keeps classification deterministic and
auditable). For each required field still missing:

1. **Deterministic tables first** — `ec2-instance-types.json`,
   `rds-instance-types.json`, `instance-specs.json`, catalog. (facts)
2. **Live API / catalog sweep** — `gcp_sweep.py` / AWS spec. (facts)
3. **LLM inference** — only when 1 & 2 miss.

Three non-negotiable rules (or enrichment becomes the "fabricated t3.medium" bug
at scale):

1. **Provenance + confidence on every enriched field.** `source: cur|table|api|llm`.
2. **Never overwrite a `cur` value.** Enrichment fills blanks only.
3. **Cache to a persistent registry** (`data/enriched_specs.json`), keyed by the
   stable thing (service + instance_type/sku). *This cache is the accumulation* —
   the next CUR hits the cache, not the LLM. High-confidence cache entries get
   promoted into the deterministic tables over time.

This matches the repo's existing patterns: product_code 3-tier resolution +
agy-backfill (identity), and "sweep the real catalog/API first, then fix".

---

## Migration plan (low-regression; do NOT rewrite-and-pray)

The current rules encode dozens of hard-won bug fixes. We carry every one
forward and diff behavior before anything goes live.

1. **[done] Schema + EC2/RDS seed** — each charge_type cites its `source_rule`.
   Field names grounded in the AWS CUR data dictionary (`cur_column` per field).
2. **[done] Dispatch engine** — `scripts/spec_dispatch.py`: `canonical -> spec ->
   first matching charge_type -> (mechanic, charge_type, fields, field_state)`.
   Global exclusion rules (negative_cost/commitment_discount/non_workload) reused
   from classify_mechanics as a pre-filter; spec-less services fall back to the
   legacy classifier. `classify_via_specs()` wires it for parity/cutover.
3. **[done] Parity self-test** — `tests/test_spec_dispatch.py`: 16 EC2/RDS
   charge-type cases, each asserts `classify_via_specs(row) ==
   classify_mechanics.classify(row)` AND the expected charge_type. 32/32 green;
   existing classify_mechanics suite unaffected.
4. **Backfill the remaining services** into specs from the existing rules
   (ranked by spend — see below). Preserves the bug-scars as spec entries.
5. **Shadow-mode diff on real bills** — run `classify_via_specs` alongside old
   `classify_mechanics` on every available job DB; assert identical
   `mechanic_group` for every row. (Needs the macOS job DBs — not on the Windows
   dev box.) Investigate every diff (carried-over bug or regression).
5. **Cut over** `classify_mechanics` to spec dispatch once diffs are zero or
   explained. Add `charge_type` + `field_provenance` columns.
6. **Wire `field_state.py`** to the spec `fields` list.
7. **Enrichment stage** (tables -> api -> llm, provenance + persistent cache).
8. **Tall inspection view** for DB readability.

Downstream contract: `mechanic_group` output is **unchanged**, so
`apply_static_mappings.py` / `family_mapper.py` need no edits.

---

## Known nuance: the global `per_request` rule sits mid-chain

In classify_mechanics, `per_request` (unit in Requests/Count/Lambda-GB-Second) runs
at ~line 690 — BEFORE the tail service rules (quicksight, inspector, kinesis,
redshift, athena, emr, glue, …). So a tail service's Count/Requests-unit row is
claimed by `per_request` first, UNLESS the service is in per_request's exclusion
list (Cognito, DynamoDB, SES, GuardDuty, SecurityHub). Services that can emit such
rows therefore carry a leading `*_requests` charge_type (→ per_request) in their
spec: done for `quicksight`, `inspector`, `kinesis`. The same applies to the broad
`block_storage`/`data_transfer` rules preceding tail services — tail-service
storage/transfer sub-rows may be claimed by those broad rules first. Single
`*_all` specs are a simplification; the shadow-diff milestone must split them
where real bills show divergence. This is the #1 thing shadow-diff validates.

## Intended registry-vs-classify divergences (NEW-4)

The registry deliberately splits greedy service rules into ordered charge_types, yielding
egress rows to `data_transfer` and storage rows to `block_storage`, keeping only core instance
rows under the service mechanic. This routes those sub-rows to deterministic shared pricers
rather than falling through to LLM/misc.

Current intended divergences:
- **`opensearch`**:
  - `opensearch_data_transfer`: Network egress/ingress rows (e.g. `DataTransfer`, `Out-Bytes`, `In-Bytes`)
    route to `data_transfer` (was swallowed as `opensearch` by `opensearch_all`).
  - `opensearch_storage`: Storage, snapshot, and IOPS rows (e.g. `Storage`, `Snapshot`, `IOPS`, `GB-Mo`)
    route to `block_storage` (was swallowed as `opensearch` by `opensearch_all`).
  - `opensearch_compute`: Core instance rows route to `opensearch`.

## Service seeding order (evidence: AGENTS.md global audit, 75 job DBs by spend)

Done (35 services): **ec2, rds** (schema v2, full field CUR-grounding), **s3,
datatransfer, opensearch, redshift, elasticache, msk, emr, athena, glue, kinesis,
quicksight, shield, dynamodb, efs, fsx, guardduty, securityhub, inspector, xray,
cloudwatch, mwaa, route53, eks**, and the tail batch **lambda, elb, sqs, sns,
apigateway, directconnect, globalaccelerator, waf, memorydb, documentdb**. All
faithful (each charge_type cites source_rule); parity self-test
`tests/test_spec_dispatch.py` = 64 cases, 128/128 pytest assertions green.

Also added (43 total) WITH CUR-grounded fields, mechanics = test-driven faithful
classify() output: **cognito, ses, sagemaker, dms** (-> misc; dms has no BoxUsage
signal so it is misc, not compute_breakdown; service_map corrects the target),
**backup** (storage -> block_storage via the generic Storage rule; other -> misc),
**cloudtrail** (Count/Requests events -> per_request; blank -> misc), **vpc**
(TransitGateway hours -> flat_hourly, bytes -> data_transfer, endpoints -> misc),
**ssm** (ssm_parameter_store mechanic, BUT canonical_service('AWS Systems Manager')
is None today so this spec is DOCUMENTATION-ONLY — it dispatches via the legacy
product-match rule until a 'systems manager'->'ssm' alias is added to
data/aws-aliases.json). Parity self-test now 75 cases, 150/150 pytest green.

Misc-routed services carry fields for context even though their mechanic is `misc`;
field provenance + the service_map target are documented per charge_type. When a
service_map decision later moves into the registry, only the charge_type's
`mechanic` changes — the fields stay.

Next, highest-spend first (spend figures are the cross-bill aggregate from the
global_sku_audit):

| # | Service | ~Aggregate spend | Notes from the audit |
|---|---------|------------------|----------------------|
| 1 | EC2 (compute, Linux/Windows/Graviton/GPU) | $4.8M+ | SP/RI-netting is the dominant error source (separate decision) |
| 2 | RDS / Aurora (incl Postgres/MySQL/MariaDB/Aurora) | $600K+ | storage-vs-instance unit gate is the key correctness pin |
| 3 | EBS (Elastic Block Store) | $215K | NULL-cost + occasional S3-mislabel |
| 4 | Marketplace/Fortinet/Prisma (non_workload) | $1.8M | large but non-workload; ignore path |
| 5 | Savings Plans / Compute SP (commitment) | $265K | NULL gcp_service by design (commitment rows) |
| 6 | S3 (object storage) | $57K+ | glacier/IA storage-class pinning |
| 7 | Data Transfer / Bandwidth | $170K | direction classification (classify_transfer) |
| 8 | OpenSearch | $70K | 70% confidence cap; compute+storage+iops |
| 9 | Redshift | $98K | node-hours -> BQ slots formula |
| 10 | ELB (ALB/NLB) | $58K | flat_hourly + LCU data_transfer |
| 11 | ElastiCache | $57K | node RAM -> Memorystore GiB-h |
| 12 | NAT Gateway | $41K | hours (flat_hourly) + data (data_transfer) |
| 13 | DMS, MSK, Lambda, CloudWatch, CloudTrail, GuardDuty, Route53, VPC, SageMaker | tail | each becomes one spec |

(Re-run `scripts/` global audit against live job DBs to refresh this ranking
when bills change.)

---

## PARKED (future, not now) — generic field-resolution + provenance

Missing-field handling (derive vCPU/RAM from a table, blank-column fallbacks like
Multi-AZ→Regional, region-from-usage_type) already works for the big services
(EC2/RDS/ElastiCache/Redshift/EMR), but it is **hand-built per service and scattered**
across ingest.py / family_mapper.py / apply_static_mappings.py. Goal (deferred): a
single declarative resolver driven by each spec's `fields[].source` so
`source:"table"` backfills uniformly for EVERY service — turning EC2's special-casing
into the general case — plus one `field_provenance` stamp surfaced in the report.

Decision (2026-10-11): **do NOT build this now.** It duplicates working code, risks
regressing hard-won fallbacks (e.g. the blank-column Multi-AZ fallback), and the spec
registry isn't wired into the pipeline yet. Correct sequence: wire the registry in +
shadow-diff vs classify_mechanics on real bills FIRST; then the field-resolver becomes
mostly consolidation + a provenance surface, done faithfully with shadow-diff too.
Optional later per-field knob: `on_missing: derive|enrich|llm|passthrough|default:<x>`.
