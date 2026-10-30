#!/usr/bin/env python3
from __future__ import annotations
"""
classify_mechanics.py — stamp each row in aws_li_catalog with mechanic_group.

Usage:
    python3 classify_mechanics.py <projection.duckdb>

Rules applied in order (first match wins):
    negative_cost, commitment_discount, cloudwatch, non_workload,
    compute_windows, compute_burstable, compute_arm, opensearch,
    compute_breakdown, rds_extended_support, managed_db, efs, fsx,
    block_storage, data_transfer, xray, flat_hourly, per_request,
    elasticache, msk, guardduty, inspector, mwaa, ssm_parameter_store,
    dynamodb_storage, shield, quicksight, redshift, athena, kinesis,
    emr, glue, per_request (route53), per_request (waf), flat_hourly (waf),
    object_storage, marketplace_thirdparty, misc.

Order is load-bearing — see per-rule comments for 'must come before X' constraints.

Exits 0 on success. Prints WARNING if misc > 15% of total aws_amortized_cost.
"""

import functools
import json
import sys
import re
import os
import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg
try:
    from aws_normalizer import canonical_service
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    canonical_service = lambda *_a: None
try:
    from diagnostics import diag, WARN
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    def diag(*a, **k): pass
    WARN = "warn"

try:
    from refresh_store import get_trusted_mapping, load_refresh_store
except (ImportError, ModuleNotFoundError):
    def get_trusted_mapping(*_a, **_k): return None
    def load_refresh_store(*_a, **_k): return {"mappings": {}}

_svc_cfg = _cfg("service-classification")

# Groups resolved deterministically by apply_commitment_ignores.py /
# apply_static_mappings.py — the LLM must NOT re-touch them, or it could
# overwrite a stable deterministic mapping with a run-to-run-varying guess.
DETERMINISTIC_GROUPS = frozenset({
    "commitment_discount", "negative_cost",
    "flat_hourly", "object_storage", "per_request",
    "block_storage", "data_transfer", "non_workload", "cloudwatch",
    "guardduty", "inspector", "marketplace_thirdparty", "quicksight", "redshift",
    "athena", "kinesis", "efs", "xray", "fsx", "emr", "elasticache", "msk",
    "rds_extended_support", "glue", "shield", "dynamodb_storage", "mwaa", "ssm_parameter_store",
    "compute_windows", "compute_arm", "compute_burstable",
})

AWS_RDS = "Relational Database"
AWS_S3 = "Simple Storage"
AWS_SES = "Simple Email"
AWS_SECURITY_HUB = "Security Hub"


def _safe_path(base: str, *parts: str) -> str:
    """Resolve path and verify it stays within base (LLM path traversal guard)."""
    resolved = os.path.realpath(os.path.join(base, *parts))
    base_real = os.path.realpath(base)
    if resolved != base_real and not resolved.startswith(base_real + os.sep):
        raise ValueError(f"Path escapes base directory: {resolved}")
    return resolved

# ---------------------------------------------------------------------------
# Rule definitions — each rule is (group_name, test_fn).
# test_fn(row: dict) -> bool
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _compiled(pattern: str):
    return re.compile(pattern, re.IGNORECASE)

def _norm(s: str | None) -> str:
    return re.sub(r'\s+', ' ', (s or '').replace('\xa0', ' ')).strip()

def _ilike(value: str | None, pattern: str) -> bool:
    """Case-insensitive substring match (SQL ILIKE '%pattern%' semantics)."""
    if value is None:
        return False
    return pattern.lower() in _norm(value).lower()

def _re(value: str | None, pattern: str) -> bool:
    if value is None:
        return False
    return bool(_compiled(pattern).search(_norm(value)))

# AWS's own `pricing/unit` field spells "hourly" inconsistently across services
# and bill formats: "Hrs" (most EC2 rows), lowercase "hours", and — confirmed
# real on a live customer bill — plain "Hours" (title case) for EKS cluster
# management-hours ("APS3-AmazonEKS-Hours:perCluster", pricing_unit="Hours").
# managed_db's own unit check (below) already normalizes to lowercase for
# exactly this reason; compute_windows/compute_burstable/compute_arm/
# compute_breakdown/flat_hourly did NOT, so a case variant they'd never seen
# (like "Hours") silently failed their gate and fell through to misc/LLM —
# same "fix one path, miss the sibling path" bug class documented elsewhere in
# this file. One shared, case-insensitive helper so this can't drift again.
#
# "accelerator-hours" is AWS Global Accelerator's own service-specific unit
# name for its per-hour fixed fee ("Global-Accelerator-fixed-fee", confirmed
# real: $99.00 = 3,960 Accelerator-Hours x $0.025/hr across regions on a live
# bill) — added explicitly, by evidence, rather than matching any unit string
# ending in "hours" generically: a blanket suffix match would also swallow
# genuinely different quantities like "GB-Hours" (a GB x hour product, e.g.
# Lambda-style memory-seconds billing), which is NOT a simple hourly count and
# must NOT go through this hourly-rate gate.
_HOURLY_UNITS = frozenset({"hrs", "hours", "hour", "hr", "accelerator-hours", ""})


def _is_hourly_unit(unit: str | None) -> bool:
    return (unit or "").strip().lower() in _HOURLY_UNITS

# Accelerator / specialized-silicon families: AI inference/training chips
# (Inferentia inf*, Trainium trn*, Habana dl*) and GPU families (p*, g*, vt*).
# These must NOT be mapped to a general-purpose CPU VM (N2D) — that hides the
# architectural change and mis-prices badly. They are routed to misc for a
# passthrough + manual-review verdict instead.
def _is_accelerator(row) -> bool:
    txt = f"{row.get('instance_type') or ''} {row.get('operation') or ''} {row.get('usage_type') or ''}".lower()
    if "inferentia" in txt or "trainium" in txt:
        return True
    return bool(re.search(r'\b(inf\d|trn\d|dl\d|p[2-5]|g[3-6]|vt\d)[a-z0-9]*\.', txt))

_EC2_TYPES_DATA = _cfg("ec2-instance-types")

def _has_instance_type(text: str | None) -> bool:
    if not text:
        return False
    # Oracle lookup against authoritative ec2-instance-types table first
    for m in re.finditer(r'[a-zA-Z0-9_\-\.:]+', text):
        tok = m.group(0).split(":")[-1].strip(".,;:()")
        clean = re.sub(r'^(?:db\.|cache\.|kafka\.)', '', tok, flags=re.IGNORECASE).lower().strip()
        if clean and not clean.startswith("_") and clean in _EC2_TYPES_DATA:
            return True
    # Fallback to regex for uncataloged or brand-new types
    return bool(re.search(r'\b(?:db\.|cache\.|kafka\.)?[a-z]+\d[a-z0-9-]*\.(nano|micro|small|medium|large|metal|\d*xlarge)\b', text, re.IGNORECASE))

# Set once per job by main() before classify() is ever called (module-level,
# same pattern as _FAMILY_RATE_CACHE-style caches elsewhere in this pipeline):
# whether this bill has ANY line_item_type='DiscountedUsage' row anywhere.
#
# apply_commitment_ignores.py's whole justification for treating RIFee/
# SavingsPlanRecurringFee/pricing_model=Reserved rows as ignorable is
# "amortized costs already reflected in effective rates [of a companion
# DiscountedUsage row]". Confirmed real on a live customer bill (job
# 6a561187, 27 commitment_discount rows totaling $60,717.47 — RIFee for
# AmazonRedshift $3,735.65 alone) that this bill has ZERO DiscountedUsage
# rows anywhere: there is no companion row for these RIFee charges to be
# "already reflected" in. The usage_type on these rows ("HeavyUsage:
# ra3.4xlarge", "HeavyUsage:m5a.4xlarge", ...) and their total_usage
# (1440.0 hrs = 2 nodes x 720 hrs/month, a real node-hours quantity, not a
# discount-adjustment number) confirm these ARE the sole, real record of
# Reserved-covered compute/DB usage on this bill — blanket-ignoring them
# silently zeroed $60K+ of real spend in this one job, with it and its real
# GCP-equivalent cost never appearing in the report at all. When the bill
# genuinely has no DiscountedUsage rows, these rows must fall through to
# their normal service-specific classification instead (compute_breakdown/
# managed_db/redshift/etc. already correctly derive instance/node type from
# usage_type regardless of the HeavyUsage/BoxUsage prefix — confirmed via
# family_mapper.py's _resolve_instance_type(), which isn't anchored to any
# specific prefix literal).


RULES = [
    (
        # Negative-cost rows are credits, refunds, RI/SP negations — no GCP equivalent.
        # Must be first so no service rule claims them before they're excluded.
        "negative_cost",
        lambda r: (r.get("aws_amortized_cost") or 0) < 0,
    ),
    (
        # commitment_discount must come before any service rule (managed_db, compute_breakdown,
        # etc.) — RI fee rows have unit=Hrs and product=RDS/ElastiCache which would otherwise
        # match service rules first and get incorrectly sent to the LLM for mapping.
        "commitment_discount",
        lambda r: (
            (
                r.get("_bill_has_discounted_usage", True)
                and (
                    r["line_item_type"] in ("RIFee", "SavingsPlanRecurringFee")
                    or r["pricing_model"] in ("Reserved", "SavingsPlan")
                )
            )
            # EdpDiscount is always a pure rate-adjustment mechanism, never a
            # usage-quantity carrier of its own — safe to ignore unconditionally
            # (the near-universal negative-cost case is already caught earlier
            # by the "negative_cost" rule above; a rare positive EdpDiscount
            # row carries no instance/node-hours to price either way).
            or r["line_item_type"] == "EdpDiscount"
            # Synthetic commitment-summary products ("Savings Plans for Compute
            # usage", "Compute Savings Plans", a distinct "Discounts" product)
            # carry no per-service instance/node identity to price against a
            # GCP SKU at all — genuinely unconditional, not gated on
            # _BILL_HAS_DISCOUNTED_USAGE.
            or _ilike(r["product"], "Savings Plans")
            or _ilike(r["product"], "Discounts")
            or _ilike(r["product"], "CK Discounts")
        ),
    ),
    (
        "cloudwatch",
        lambda r: (
            _ilike(r["product"], "CloudWatch")
            or _ilike(r["product"], "AmazonCloudWatch")
            or r.get("canonical") == "cloudwatch"
        ),
    ),
    (
        "non_workload",
        lambda r: (
            _ilike(r["product"], "Marketplace")
            or _ilike(r["product"], "Support")
            or _ilike(r["product"], "AWS Support")
            or _ilike(r["product"], "AWSMarketplace")
            or _ilike(r["product"], "AWSSupport")
            or _ilike(r["product"], "AWSMP")
            or _ilike(r["operation"], "Marketplace")
            or _ilike(r["operation"], "AWS Support")
            # Third-party bandwidth resellers have no GCP equivalent — no "Amazon"/"AWS"
            # prefix means it's a marketplace product, not a native AWS service.
            # Exception: PDF/flat-CSV bills use product="Bandwidth" for native EC2
            # internet egress too. Those rows describe themselves as "data transfer out"
            # in the operation/description field — route them to data_transfer instead.
            or (r.get("product", "").strip() == "Bandwidth"
                and not _ilike(r.get("product", ""), "Amazon")
                and not _ilike(r.get("product", ""), "AWS")
                and not _re(r.get("operation", ""), r"data transfer (out|in)|per gb.{0,30}transfer|transfer.{0,30}per gb"))
            # EC2 burstable CPU credits (t2/t3/t4g CPUCredits) have no GCP equivalent
            # (E2 has no burst-credit billing model). Ignore rather than mismap.
            # Checked against `product` too, not just `usage_type` — confirmed
            # real bug, same class as every other PDF-bill field-location miss
            # this session: PDF bills carry "T3ACPUCredits" in `product`
            # ("Amazon Elastic Compute Cloud T3ACPUCredits") with `usage_type`
            # left empty, so the usage_type-only check silently never matched
            # on PDF bills, sending these rows to the LLM-only `misc` bucket
            # instead of this deterministic $0 classification — despite this
            # being a slam-dunk, always-correct case with zero judgment call
            # involved (no GCP burstable family has a credit-billing concept
            # at all, ever, regardless of customer specifics).
            or _re(r.get("usage_type", ""), r"CPUCredits?")
            or _re(r.get("product", ""), r"CPUCredits?")
            # ingest.py's materiality filter re-inserts the sum of dropped
            # low-materiality rows as this exact synthetic row so the bill
            # total still reconciles — route it to passthrough at cost parity,
            # never to the LLM (it isn't a real AWS service to map).
            or r.get("product") == "Ingestion Materiality Adjustment"
        ),
    ),
    (
        # Windows EC2 instance-hours — handled by a dedicated static mapper that
        # emits core + RAM + Windows Server license components deterministically.
        # Must come before compute_breakdown so Windows rows don't fall to the LLM.
        "compute_windows",
        lambda r: (
            (
                _ilike(r["product"], "Elastic Compute")
                or _ilike(r["product"], "EC2")
                or _ilike(r["product"], "Compute Cloud")
                or r.get("canonical") == "ec2"
            )
            and (
                _re(r["usage_type"], r"running Windows")
                or _re(r["operation"], r"(?i)Windows")
            )
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and _is_hourly_unit(r["unit"])
            and not _is_accelerator(r)
        ),
    ),
    (
        # Burstable EC2 instance-hours: ALL t-family (t2, t3, t3a, t4g).
        # Maps to E2 (GCP's burstable equivalent) regardless of architecture.
        # t4g is ARM but E2 is available everywhere and is the correct cost
        # equivalent for burstable workloads; must come before compute_arm so
        # t4g rows land here instead of the full-performance C4A/N2D mapper.
        "compute_burstable",
        lambda r: (
            (
                _ilike(r["product"], "Elastic Compute")
                or _ilike(r["product"], "EC2")
                or _ilike(r["product"], "Compute Cloud")
                or r.get("canonical") == "ec2"
            )
            and (
                # HeavyUsage: (alongside BoxUsage:) is AWS's own usage_type
                # prefix for a Reserved Instance's recurring per-hour fee
                # (RIFee line item). Without it, a Reserved t-family instance's
                # HeavyUsage row skips this rule (correctly, since it needs
                # E2/burstable treatment, not compute_breakdown's general-
                # purpose family sweep) and falls through all the way to the
                # generic compute_breakdown rule below — same bug class as
                # the compute_breakdown HeavyUsage fix, just for burstable.
                _re(r.get("usage_type", ""), r"(?:BoxUsage|HeavyUsage):t[234][ag]?\.")
                # PDF/flat-CSV bills leave usage_type blank; fall back to the
                # operation field which carries the instance type as a substring
                # (e.g. "... t2.xlarge instance ...").
                or (not r.get("usage_type")
                    and _re(r.get("operation", ""),
                            r"t[234][ag]?\.(nano|micro|small|medium|large|\d*xlarge)"))
            )
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and _is_hourly_unit(r["unit"])
            and not _re(r.get("usage_type", ""), r"running Windows")
            and not _re(r.get("operation", ""), r"(?i)Windows")
        ),
    ),
    (
        # Graviton (ARM) EC2 instance-hours. The static mapper tries C4A first;
        # falls back to N2D AMD when C4A is absent in the region — catalog-driven,
        # no hardcoded region lists. Must come before compute_breakdown.
        "compute_arm",
        lambda r: (
            (
                _ilike(r["product"], "Elastic Compute")
                or _ilike(r["product"], "EC2")
                or _ilike(r["product"], "Compute Cloud")
                or r.get("canonical") == "ec2"
            )
            # Graviton families all contain 'g' in the family name (t4g, c6g, m7g…).
            # HeavyUsage: (a Reserved Instance's recurring per-hour RIFee, alongside
            # on-demand BoxUsage:) needs the same recognition here as the burstable
            # rule above and compute_breakdown below, or a Reserved Graviton
            # instance's fee row falls through to the general-purpose x86 family
            # sweep instead of the correct C4A/N2D ARM path.
            and _re(r.get("usage_type", ""), r"(?:BoxUsage|HeavyUsage):[a-z]\d[a-z0-9]*g[a-z0-9]*\.")
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and _is_hourly_unit(r["unit"])
            and not _is_accelerator(r)
            and not _re(r.get("usage_type", ""), r"running Windows")
            and not _re(r.get("operation", ""), r"(?i)Windows")
        ),
    ),
    (
        # OpenSearch / Elasticsearch — all row types (compute, storage, IOPS,
        # snapshots, networking) go to a single dedicated static handler.
        # Must come before compute_breakdown so that OpenSearch compute rows
        # (which have a parseable instance type in usage_type) don't bleed
        # into EC2 routes and get mapped without the 70% confidence cap.
        "opensearch",
        lambda r: (
            _ilike(r["product"], "OpenSearch")
            or _ilike(r["product"], "Elasticsearch")
            or r.get("canonical") == "opensearch"
        ),
    ),
    (
        # Accelerator/GPU instances ARE included here (unlike compute_windows
        # and compute_arm above, which genuinely have no GPU handling and must
        # keep excluding them) — family_mapper.py, which processes every
        # compute_breakdown row, already branches on is_gpu to call
        # map_gpu_row() (A2/A3/G2/etc., via family_map.json's gpu_profiles)
        # instead of map_gce_row() for exactly this case. The old exclusion
        # here predated that branch and became a real bug once it existed:
        # every GPU-family EC2 row (g4dn, g5, p3, p4d, ...) was silently
        # falling to the misc/LLM path — which had no GPU-aware guidance and
        # was just passing them through at 1:1 AWS cost — instead of ever
        # reaching the deterministic GPU mapper that already existed to
        # handle them correctly. Confirmed live: a real bill's g4dn.2xlarge/
        # g5.2xlarge rows landed in mechanic_group='misc' and were passed
        # through unmapped rather than priced against A2/G2.
        "compute_breakdown",
        lambda r: (
            (
                _ilike(r["product"], "Elastic Compute")
                or _ilike(r["product"], "EC2")
                or _ilike(r["product"], "Compute Cloud")
                or r.get("canonical") == "ec2"
                or _has_instance_type(r["operation"])
                or _has_instance_type(r["usage_type"])
            )
            and not (
                _ilike(r["product"], "RDS")
                or _ilike(r["product"], "Relational Database")
                or _ilike(r["product"], "Aurora")
                or _ilike(r["product"], "ElastiCache")
                or _ilike(r["product"], "DocumentDB")
                or _ilike(r["product"], "MemoryDB")
                # Policy confirmation (P-1): Neptune, QLDB, and Timestream instances have no direct
                # managed equivalent on GCP. Excluding them from compute_breakdown prevents pricing
                # them as standard generic GCE VMs; they route to misc -> passthrough at AWS cost.
                or _ilike(r["product"], "Neptune")
                or _ilike(r["product"], "QLDB")
                or _ilike(r["product"], "Timestream")
                or r.get("canonical") in ("neptune", "qldb", "timestream")
                # EMR's own "BoxUsage:<type>" lines are EMR's per-instance
                # SURCHARGE (confirmed real: 23-33% of the same instance's EC2
                # rate in the raw bill, e.g. m8g.4xlarge $0.1795/h on EMR vs
                # $0.5413/h on EC2) — the VM itself bills separately as
                # AmazonEC2. The `emr` rule (below) routes these to map_emr's
                # Dataproc-premium formula, but this rule's
                # _has_instance_type(usage_type) clause matched them first and
                # priced the surcharge as a whole GCE VM: five EMR rows,
                # AWS $912.12 vs GCP $3,938.96 reported, ~$856 once routed
                # correctly. Excluding the product here lets `emr` claim them.
                or _ilike(r["product"], "Elastic MapReduce")
                or _ilike(r["product"], "ElasticMapReduce")
                or _ilike(r["product"], "AmazonEMR")
                or _ilike(r["product"], "Amazon EMR")
                or _ilike(r["product"], "SageMaker")
                or r.get("canonical") == "sagemaker"
            )
            # "Instance-hour" (hyphen) is the PDF-ingestion format
            # ("c7gd.4xlarge Linux/UNIX Spot Instance-hour in ..."); "Instance Hour"
            # (space) is the CUR format. Without both, PDF-format Spot/On-Demand
            # rows fall through to misc/LLM instead of getting deterministic
            # core+RAM (and, for Spot, Preemptible-rate) pricing.
            # A third real phrasing (EDP/private-rate-card discounted bills):
            # "([EC2 PRC] EC2 Discount @ 35.00%) $0.029120 per On Demand Linux
            # t3.medium Instance (672 Hrs)" — "Instance" and "Hrs" are separated
            # by a parenthetical quantity, so neither "Instance Hour" nor
            # "Instance-hour" appears as a contiguous phrase. Confirmed real: 23
            # EC2 rows in one bill fell through to misc/LLM entirely because of
            # this. "per On Demand" is specific enough to only appear on a
            # genuine on-demand instance-hour line.
            # A fourth real phrasing: "HeavyUsage:<type>" — AWS's own usage_type
            # for a Reserved Instance's recurring per-hour fee (RIFee line item;
            # e.g. "APS3-HeavyUsage:m5a.4xlarge"). Only reachable at all once the
            # bill genuinely has no DiscountedUsage rows to double-count against
            # (see _BILL_HAS_DISCOUNTED_USAGE above) — in that case this usage_type
            # is the SOLE record of real RI-covered compute-hours on the bill, and
            # needs the exact same deterministic core+RAM pricing as an on-demand
            # BoxUsage row, not misc/LLM. Confirmed real: $4,862.88 of EC2 RI
            # HeavyUsage spend fell to misc purely because this phrasing wasn't
            # recognized, even after fixing the classify_mechanics/commitment_
            # discount routing that used to zero it out entirely.
            and (_re(r["usage_type"], r"BoxUsage|SpotUsage|ReservedInstances|running Linux|HeavyUsage")
                 or _re(r["operation"], r"Instance[\s-]hour|hourly fee per Linux/UNIX|per On Demand \w+.*Instance"))
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and _is_hourly_unit(r["unit"])
        ),
    ),
    (
        # RDS Extended Support (post-EOL engine-version surcharge, e.g.
        # "ExtendedSupport:Yr1-Yr2:MySQL8.0") must come before managed_db/
        # block_storage — its unit ("vCPU-hour") and product ("Amazon
        # Relational Database Service") would otherwise match managed_db's
        # generic RDS check and get routed to the LLM's instance-sizing path,
        # or (if the unit doesn't look hourly enough) fall to block_storage's
        # storage-SKU path — neither is correct for what is actually a flat
        # per-vCPU surcharge with its own dedicated GCP pricing model.
        # map_rds_extended_support() decides map-vs-ignore per engine version
        # against the real Cloud SQL Extended Support SKU catalog (verified:
        # GCP publishes "Cloud SQL for MySQL: ... Extended support vCPU"
        # SKUs for MySQL 5.6/5.7 and PostgreSQL 9.6-13 in every region,
        # including asia-south1 — but none for MySQL 8.0 as of this catalog
        # snapshot, so 8.0 rows genuinely have no GCP charge to map to today).
        "rds_extended_support",
        lambda r: _re(r.get("usage_type", ""), r"ExtendedSupport:"),
    ),
    (
        # TACTICAL: migrate into service_specs.json <svc> charge_type (see WORKORDER_generic_mechanic_overclaim.md)
        # Aurora/RDS per-I/O request rows (ServerlessV2IOOp, StorageIOUsage, ReplicatedWriteIO, etc.)
        # must route to block_storage deterministically so they are not treated as ACU compute capacity
        # by managed_db. Excludes capacity storage (GB-Mo) like IO-OptimizedStorage.
        "block_storage",
        lambda r: (
            (
                _ilike(r["product"], "RDS")
                or _ilike(r["product"], "Relational Database")
                or _ilike(r["product"], "Aurora")
                or r.get("canonical") in ("rds", "aurora")
            )
            and _re(
                f"{r.get('usage_type', '')} {r.get('operation', '')}",
                r"I/?O.?Op(?!timized\s*ACU)|ServerlessV2IOOp|StorageIOUsage|IO-?Optimized.*(Request|Usage)|per.?IO|ReplicatedWriteIO",
            )
            and not _re(f"{r.get('usage_type', '')} {r.get('operation', '')}", r"IO-?OptimizedStorage")
            and r.get("unit") != "GB-Mo"
        ),
    ),
    (
        # managed_db handles INSTANCE rows (billing unit = Hrs/hours) for the LLM.
        # Storage/IO/backup rows (unit = GB-Mo, None, or non-hourly) are routed to
        # block_storage instead — map_block_storage handles managed-DB storage
        # deterministically and picks the correct Cloud SQL storage SKU.
        # Sending non-Hrs Aurora/RDS rows to the LLM caused it to assign vCPU SKUs
        # to GB-Mo storage rows, inflating cost by 10,000x (millions of GB × $/hr).
        "managed_db",
        lambda r: (
            (
                _ilike(r["product"], "RDS")
                or _ilike(r["product"], "Relational Database")
                or _ilike(r["product"], "Aurora")
                or _ilike(r["product"], "DocumentDB")
                or _ilike(r["product"], "MemoryDB")
                or r.get("canonical") in ("rds", "aurora", "documentdb", "memorydb")
            )
            # Aurora Serverless v2 bills in ACU-Hrs; include alongside instance Hrs.
            # ElastiCache excluded — has its own static elasticache handler below.
            # DiscountedUsage/SavingsPlanCoveredUsage rows are RI/SP-covered instance
            # hours — their pricing_unit can be NULL but they are still instance rows.
            # Unit compared lowercase and includes the singular "ACU-Hr" (AWS's own
            # Parquet/CUR-2.0 exports use the singular form; the plural "ACU-Hrs" came
            # from CSV-format bills only) — confirmed real: a genuine Aurora Serverless
            # v2 I/O-Optimized row ($6.8K/mo, unit="ACU-Hr") fell through this exact-match
            # check to the block_storage catch-all below and got priced as GB-Mo storage
            # instead of ACU compute capacity.
            and (
                (r.get("unit") or "").lower() in ("hrs", "hours", "hour", "hr", "acu-hrs", "acu-hours", "acu-hr")
                or r.get("line_item_type") in ("DiscountedUsage", "SavingsPlanCoveredUsage")
                # PDF/flat-CSV bills leave pricing_unit blank. Catch InstanceUsage and
                # RDS Proxy rows by usage_type pattern so they don't fall to block_storage
                # where they'd be priced as $/GiBy.mo × hours (10-20x inflation).
                or _re(r.get("usage_type", ""), r"InstanceUsage:|RDS:Proxy")
                # Aurora Serverless V2 rows: usage_type="...Aurora:ServerlessV2Usage" (PDF)
                # or "...Aurora:ServerlessV2IOOptimizedUsage" (I/O-Optimized storage mode,
                # Parquet/CUR-2.0) — match the "Aurora:ServerlessV2" prefix alone (not the
                # full "...Usage" suffix) so every variant is caught, not just the plain one.
                # Excludes ServerlessV2IOOp which is handled by the block_storage I/O guard above.
                or (_re(r.get("usage_type", ""), r"Aurora:ServerlessV2") and not _re(r.get("usage_type", ""), r"ServerlessV2IOOp"))
            )
        ),
    ),
    (
        # EFS → Filestore. Must come before block_storage because EFS rows have
        # "Storage" in usage_type and would match the generic Storage rule.
        "efs",
        lambda r: (
            _ilike(r["product"], "Elastic File System")
            or _ilike(r["product"], "AmazonEFS")
            or r.get("canonical") == "efs"
        ),
    ),
    (
        # FSx variants → Filestore or passthrough. Must come before block_storage
        # because FSx rows have "Storage" in usage_type and would otherwise match
        # the generic block_storage storage rule.
        "fsx",
        lambda r: (
            _ilike(r["product"], "FSx")
            or _ilike(r["product"], "AmazonFSx")
            or _ilike(r["product"], "Amazon FSx")
            or r.get("canonical") == "fsx"
        ),
    ),
    (
        "block_storage",
        lambda r: (
            # MSK (Kafka) broker storage is ordinary disk billed in GB-Mo — price
            # it as storage, not as a Kafka broker-hour. CUR carries usage_type
            # "Kafka.Storage..." (caught by the generic "Storage" branch below),
            # but PDF/flat bills leave usage_type blank and put the signal in
            # operation ("$0.12 per GB-Mo for storage ...") — without this those
            # rows fall to misc. Broker-hour rows (unit=Hrs, "broker hour" in
            # operation) do NOT match here and flow on to the msk rule.
            (r.get("canonical") == "msk"
             and ((r.get("unit") or "").lower() in ("gb-mo", "gb month", "gb-month")
                  or _re(f"{r.get('usage_type', '')} {r.get('operation', '')}", r"storage")))
            or
            # Confirmed real: rows with product="Amazon Elastic Block Store"
            # whose operation text unambiguously describes an S3-ONLY storage
            # class ("One Zone-Infrequent Access", "Glacier", etc. — EBS has
            # no such concept at all) — the product field itself was
            # genuinely mislabeled somewhere upstream (source bill or PDF
            # extraction), but operation is more specific, granular evidence
            # than product here. Trusting the mislabeled product routed a
            # real S3 storage row into block_storage's Balanced-PD-Capacity
            # default, a ~11x category-error overprice (compute-disk pricing
            # applied to archival object storage). Same defensive pattern as
            # the S3/DynamoDB/Lambda exclusions already below for the generic
            # "Storage" catch — extend it to the "Elastic Block" product
            # match itself, not just the fallback branch.
            (_ilike(r["product"], "Elastic Block")
             and not _re(r["operation"], r"Infrequent Access|Glacier|Intelligent-Tiering|Deep Archive"))
            or _re(r["usage_type"], r"EBS:Volume|EBS:Snapshot|gp2|gp3|io1|io2|sc1|st1")
            or _re(r["usage_type"], r"Storage")
            or _re(r["operation"], r"GP3-Storage|Provisioned GP3 storage")
            # PDF-flat-bill EBS rows: product is genuinely "Elastic Compute Cloud"
            # (not "Elastic Block [Store]") and usage_type is a bare "EBS" with no
            # volume-type substring at all — the real detail ("GB-month of General
            # Purpose SSD (gp2) provisioned storage...", "GB-Month of snapshot data
            # stored...", "GB-month of Provisioned IOPS SSD (io1)...") lives only in
            # `operation`. The GP3-specific check above already covered gp3; this
            # extends the same operation-text check to every other volume type and
            # to snapshots, so these don't silently fall to the LLM-handled misc
            # bucket just because the PDF format didn't populate usage_type.
            or _re(r["operation"], r"\((?:gp2|gp3|io1|io2|sc1|st1)\)\s+provisioned storage|"
                                    r"snapshot data stored|"
                                    r"provisioned (?:IOPS|MiBps)-month of (?:gp2|gp3|io1|io2)")
            # Managed-DB storage/IO/backup rows (non-Hrs billing unit) — these fell
            # through managed_db's Hrs filter and must be caught here for deterministic
            # Cloud SQL storage SKU mapping rather than going to the LLM as misc.
            or (
                (
                    _ilike(r["product"], "RDS") or _ilike(r["product"], "Relational Database")
                    or _ilike(r["product"], "Aurora") or _ilike(r["product"], "DocumentDB")
                    or _ilike(r["product"], "MemoryDB")
                    # ElastiCache excluded — its own handler (elasticache) manages all rows
                    # including node-hours with empty pricing_unit (PDF bills).
                )
                and (
                    (r.get("pricing_unit") or "").lower() not in ("hrs", "hours", "hr", "hour", "")
                    # Blank pricing_unit alone used to be treated the same as "Hrs"
                    # (assumed instance-hour row) — confirmed real: a genuine RDS
                    # Multi-AZ GP3 IOPS row ("$0.046 per IOPS-month of provisioned
                    # GP3 IOPS...") with blank pricing_unit (PDF format) fell through
                    # to misc instead of the deterministic mapper, and the LLM priced
                    # it as strategy='map' instead of the correct 'ignore' (Cloud SQL
                    # bundles IOPS into storage price) — flagged by the pipeline's own
                    # Phase 6 sanity check. When pricing_unit itself is blank/unhelpful,
                    # fall back to a positive non-hourly signal in operation/usage_type
                    # text (GB-Mo/IOPS-Mo/storage/snapshot/backup) instead of defaulting
                    # to "assume it's an hourly row".
                    or (
                        not (r.get("pricing_unit") or "").strip()
                        and _re(f"{r.get('usage_type','')} {r.get('operation','')}",
                                r"iops-mo|gb-mo|storage|snapshot|backup")
                    )
                )
            )
        ),
    ),
    (
        "data_transfer",
        lambda r: (
            _ilike(r["product"], "DataTransfer")
            or _ilike(r["product"], "Data Transfer")
            or r.get("canonical") == "datatransfer"
            # PDF/flat bills use product="Bandwidth" for native EC2 internet egress.
            # Only route here when the operation describes a real data-transfer charge.
            or (r.get("product", "").strip() == "Bandwidth"
                and _re(r.get("operation", ""), r"data transfer (out|in)|per gb.{0,30}transfer|transfer.{0,30}per gb"))
            or _re(r["usage_type"], r"DataTransfer|Data Transfer|NatGateway-Bytes|TransitGateway-Bytes|LCUUsage|LoadBalancer-Bytes")
            or _re(r["operation"], r"TransitGateway-Bytes")
            # LCU (Load Balancer Capacity Unit) data-processing charge. CUR format
            # uses usage_type "LCUUsage" (caught above); PDF format instead phrases
            # this in `operation` as "...capacity unit-hour... (N LCU-Hrs)" — a
            # different token ("LCU-Hrs", hyphenated) the CUR-oriented regex above
            # doesn't match. Without this, PDF-format LCU rows fall through to misc
            # -> LLM, which (observed) re-uses the base per-hour forwarding-rule SKU
            # instead of the data-processing SKU, inflating cost 3-5x.
            or _re(r["operation"], r"LCU-Hrs?|capacity unit-hour")
            # Spreadsheet-export meters: "Out-Bytes" and "In-Bytes" suffixes signal
            # inter-region / internet egress/ingress — map to data_transfer.
            # PDF-ingested rows (e.g. VPC Peering) carry this suffix in `product`
            # ("...APS3-VpcPeering-In-Bytes") rather than usage_type, since PDF
            # bills leave usage_type blank — check both fields.
            or _re(r["usage_type"], r"Out-Bytes|In-Bytes|AWS-Out-Bytes|AWS-In-Bytes")
            or _re(r["product"], r"VpcPeering.*(?:Out-Bytes|In-Bytes)")
            # PDF bills: usage_type is empty; detect NatGateway by product name.
            # Only route to data_transfer when usage_type is blank — CUR bills
            # have "NatGateway-Hours" in usage_type and stay in flat_hourly.
            or (_ilike(r["product"], "NatGateway")
                and not r.get("usage_type"))
            # PDF ingestion for CloudFront bills groups per-request rows AND the
            # sibling "Bandwidth" sub-section's per-GB egress rows under the SAME
            # product label (confirmed real: "Amazon CloudFront IN-Requests-Tier2-
            # HTTPS" was used as `product` for both the genuine per-request charge
            # AND its region's per-GB data-transfer-out rows — the raw PDF has them
            # under separate "Any"/"Bandwidth" headers, but the ingest step loses
            # that grouping). The "Requests" substring in that shared label would
            # otherwise route these egress rows into per_request, where
            # CloudFront's per-request ignore branch zeroes them out as "no fair
            # per-request GCP charge" — wrong, since these rows are real billable
            # egress, not a request charge. `operation` is the authoritative signal:
            # a genuine per-GB data-transfer-out charge always states so explicitly,
            # regardless of what the mislabeled product field says.
            or (_ilike(r.get("product", ""), "CloudFront")
                and _re(r.get("operation", ""),
                        r"per gb.{0,40}data transfer (out|in)|data transfer (out|in).{0,40}per gb"))
        ),
    ),
    (
        # X-Ray → Cloud Trace. Must come before per_request because X-Ray rows
        # use unit=Count which the per_request rule also catches.
        "xray",
        lambda r: (
            _ilike(r["product"], "X-Ray")
            or _ilike(r["product"], "AmazonXRay")
            or _ilike(r["product"], "XRay")
            or r.get("canonical") == "xray"
        ),
    ),
    (
        "flat_hourly",
        lambda r: (
            (
                _re(r["usage_type"], r"LoadBalancerUsage|NatGateway-Hours|ElasticIP|IPAddress"
                                     r"|TransitGateway-Hours|DirectConnect|HostedConnection"
                                     r"|GlobalAccelerator|PublicIPv4|VPN-Connections|VPNConnection")
                or _re(r["operation"], r"LoadBalancer|public IPv4 address|TransitGateway-Hours"
                                       r"|Transit Gateway|DirectConnect|Global Accelerator"
                                       r"|CreateVpnConnection|VPN")
                or _ilike(r["product"], "Direct Connect")
                or _ilike(r["product"], "AmazonDirectConnect")
                or _ilike(r["product"], "GlobalAccelerator")
                or _ilike(r["product"], "Global Accelerator")
                or r.get("canonical") in ("directconnect", "globalaccelerator", "eks")
                # EKS cluster management fee: $0.10/hr → GKE cluster mgmt fee (exact parity)
                or _ilike(r["product"], "Elastic Container Service for Kubernetes")
                or _ilike(r["product"], "AmazonEKS")
                or _re(r["usage_type"], r"AmazonEKS|EKS.*Hours|EKSCluster")
                # AWS WAF WebACL-Hour/Rule-Hour fixed fees — confirmed these had
                # ZERO static coverage before: WAF's per-request rows correctly
                # route to per_request (unit=Requests), but the WebACL/Rule
                # fixed-fee rows (unit=Hrs) matched nothing here and fell all
                # the way to misc/LLM with no deterministic safety net at all,
                # unlike every other hourly service in this rule.
                or _re(r["usage_type"], r"WebACL-Hour|Rule-Hour")
                or _re(r["operation"], r"Web ACL|WAF Rule")
            )
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and _is_hourly_unit(r["unit"])
            # Fargate rows use Hrs but are caught by per_request above
            and not _re(r.get("product", ""), r"[Ff]argate")
            and not _re(r.get("usage_type", ""), r"[Ff]argate")
        ),
    ),
    (
        # TACTICAL: migrate into service_specs.json <svc> charge_type (see WORKORDER_generic_mechanic_overclaim.md)
        # AWS WAF request rows (RequestV2, Requests, Captcha, Challenge) and rules (WebACLV2, RuleV2)
        # must route to per_request dedicated WAF handling (yielding Cloud Armor passthrough at AWS cost parity),
        # not generic per_request mapping to 7.5e-07 zero-cost SKUs.
        "per_request",
        lambda r: (
            (_re(r.get("product", ""), r"AWS WAF|WAF") or r.get("canonical") == "waf")
            and _re(
                f"{r.get('usage_type', '')} {r.get('operation', '')} {r.get('unit', '')}",
                r"RequestV2|Requests|Captcha|Challenge|WebACLV2|RuleV2|Web ACL|WAF Rule",
            )
        ),
    ),
    (
        # TACTICAL: migrate into service_specs.json <svc> charge_type (see WORKORDER_generic_mechanic_overclaim.md)
        # S3 request rows (Requests-Tier1, Requests-Tier2, etc.) must route to dedicated S3 request
        # passthrough handling at cost parity rather than being mapped to GCS Class A/B SKUs with
        # incompatible $/1k vs $/10k unit models (~770x underestimation).
        "per_request",
        lambda r: (
            (r.get("canonical") == "s3" or _ilike(r.get("product", ""), "Simple Storage") or _re(r.get("product", ""), r"\bS3\b"))
            and _re(r.get("usage_type", ""), r"Requests-Tier")
        ),
    ),
    (
        # Architecturally complex services (Cognito, DynamoDB, SES) are excluded
        # here — they fall through to misc so the LLM gets a personalized prompt.
        # GuardDuty and Security Hub are caught by the guardduty rule above and
        # never reach this rule.
        "per_request",
        lambda r: (
            (
                r["unit"] in ("Requests", "Lambda-GB-Second", "Count")
                or _re(r["usage_type"], r"Requests|Invocations")
                # PDF-format bills for request-billed services (SQS/SNS) leave usage_type
                # blank entirely and carry the request-tier signal only in `product`
                # ("Amazon Simple Queue Service APS3-Requests-Tier1") — confirmed real:
                # this silently routed every SQS/SNS row to misc/LLM instead of the
                # deterministic Pub/Sub mapper. Checking `product` too (not just
                # usage_type) closes that gap without the broader risk of matching
                # arbitrary free-text "requests" mentions in `operation`.
                or _re(r["product"], r"Requests|Invocations")
                # Lambda GB-Second rows: CUR sets unit="Lambda-GB-Second" (caught above).
                # Flat-CSV/PDF bills set unit="" and embed the signal only in product name
                # ("AWS Lambda APS3-Lambda-GB-Second-ARM" / "Lambda-GB-Second-x86").
                # Without this, flat-CSV Lambda compute rows fall to misc → LLM with no
                # real GCP price, silently dropping Cloud Run savings.
                or _re(r.get("product"), r"Lambda[- ]GB[- ]Second")
                # Fargate vCPU-Hours and GB-Hours: static mapper converts to GKE Autopilot.
                # Unit is "Hrs" so the flat_hourly rule would otherwise catch these first.
                # Catches both "AWS Fargate" product rows and ECS rows where usage_type
                # includes "Fargate" (e.g. "APS3-Fargate-vCPU-Hours:perCPU"), AND
                # PDF bills where product = "Amazon Elastic Container Service APS3-Fargate-..."
                # and usage_type is empty — _re on product handles all three forms.
                or _ilike(r["product"], "AWS Fargate")
                or _re(r.get("usage_type"), r"[Ff]argate")
                or _re(r.get("product"), r"[Ff]argate")
                # API Gateway: pricing model is per-call, map to Cloud Endpoints.
                # Must be caught here so it doesn't fall to misc → LLM misroute.
                or _ilike(r["product"], "Amazon API Gateway")
                or _ilike(r["product"], "AmazonApiGateway")
                or r.get("canonical") == "apigateway"
                # S3 Intelligent-Tiering, lifecycle, and miscellaneous S3 charges
                # with blank usage_type / unit fall here rather than to misc.
                # map_per_request() handles them as Cloud Storage passthroughs.
                # Bare substring "s3" is unsafe (matches region codes like
                # "APS3" in unrelated products) — word-boundary match "S3" as
                # a whole token, or the full "Simple Storage" phrase.
                or (
                    (_re(r["product"], r"\bS3\b") or _ilike(r["product"], "Simple Storage"))
                    and not r["unit"] in ("GB-Mo", "GB Month", "GB-Month")
                    and not _re(r["usage_type"], r"TimedStorage|ByteHrs")
                    # Storage-class rows (Glacier IR/Flexible/Deep Archive,
                    # Intelligent-Tiering) with blank usage_type/unit — the PDF/
                    # simplified-CUR gap this whole branch exists for — must still
                    # fall through to object_storage rather than land here, or the
                    # storage GB-month gets classified (and therefore priced) as an
                    # unidentifiable per-request charge instead of a GCS storage
                    # class, while a request line for the exact same storage class
                    # correctly reaches object_storage's operation check. Only
                    # exclude when operation names a storage class WITHOUT also
                    # naming a request concept, so genuine Glacier/Intelligent-
                    # Tiering REQUEST rows still land here. NB: match "Request"
                    # only, not "Retrieval" — "Glacier Instant Retrieval" is the
                    # storage class's own proper name and contains "Retrieval"
                    # even on pure-storage rows; using it here would exclude
                    # every GIR storage row right back into misclassification.
                    and not (
                        _re(r["operation"], r"Infrequent Access|Glacier|Intelligent-Tiering|Deep Archive")
                        and not _re(r["operation"], r"Request|Data Returned|Select")
                    )
                )
            )
            and not any(
                _ilike(r["product"], p) for p in (
                    "Cognito", "DynamoDB", "Simple Email",
                    "GuardDuty", "Security Hub", "WAF",
                )
            )
            and r.get("canonical") != "waf"
            and not (
                (r.get("canonical") == "s3" or _ilike(r.get("product", ""), "Simple Storage") or _re(r.get("product", ""), r"\bS3\b"))
                and _re(r.get("usage_type", ""), r"Requests-Tier")
            )
        ),
    ),
    (
        # ElastiCache → Cloud Memorystore for Redis. Static mapper converts
        # node-hours to GiBy.h using node RAM from instance_type/operation text.
        "elasticache",
        lambda r: (
            _ilike(r["product"], "ElastiCache")
            or _ilike(r["product"], "AmazonElastiCache")
            or r.get("canonical") == "elasticache"
        ),
    ),
    (
        # MSK broker-hours → static GCE equivalent mapper (deterministic vCPU/RAM lookup).
        # Storage sub-lines (Kafka.Storage.*) stay in block_storage; data-transfer stays
        # in data_transfer. Only broker compute-hours land here.
        "msk",
        lambda r: (
            (
                _ilike(r["product"], "Managed Streaming for Apache Kafka")
                or _ilike(r["product"], "AmazonMSK")
                or _ilike(r["product"], "MSK")
                or r.get("canonical") == "msk"
            )
            # Broker-hours (compute) only — storage GB-Mo rows are caught earlier
            # by block_storage. CUR carries the broker signal in usage_type
            # ("Kafka.t3..."); PDF/flat bills leave usage_type blank and carry it
            # in operation ("... per broker hour for Kafka.t3.small ..."), so
            # check both or PDF broker rows fall to misc.
            and (
                _re(r.get("usage_type", ""), r"Kafka\.[a-z]")
                or _re(r.get("operation", ""), r"broker[\s-]hour|Kafka\.[a-z]")
            )
            # PDF/flat-CSV bills leave the unit blank — _is_hourly_unit accepts ""
            # alongside the explicit hour units, while still excluding GB-Mo
            # storage rows (which belong to block_storage, matched above).
            and _is_hourly_unit(r["unit"])
        ),
    ),
    (
        # GuardDuty and Security Hub → static passthrough to Security Command Center.
        # Excluded from per_request because their pricing model (per-GB-analyzed /
        # per-asset) is incompatible with GCP equivalents; static mapper handles them
        # with the correct gcp_service label and an explanatory note.
        "guardduty",
        lambda r: (
            _ilike(r["product"], "GuardDuty")
            or _ilike(r["product"], "AmazonGuardDuty")
            or _ilike(r["product"], "Security Hub")
            or _ilike(r["product"], "SecurityHub")
            or r.get("canonical") in ("guardduty", "securityhub")
        ),
    ),
    (
        # Amazon Inspector (EC2/ECR/Lambda vulnerability scanning) → static
        # passthrough to Security Command Center Premium. Same reasoning as the
        # guardduty rule directly above: Inspector bills per-resource-scanned
        # (e.g. Inspector-ECR-ImageScanning, Inspector-EC2-Scanning) while SCC
        # Premium bills per-asset-under-management/mo — incompatible units, so
        # a real rate conversion would be invented precision. Kept as its own
        # group (rather than folded into "guardduty") so the static mapper can
        # carry an Inspector-specific note and label.
        "inspector",
        lambda r: (
            _ilike(r["product"], "Inspector")
            or _ilike(r["product"], "AmazonInspector")
            or r.get("canonical") == "inspector"
        ),
    ),
    (
        # MWAA (Managed Workflows for Apache Airflow) → Cloud Composer, GCP's
        # own managed-Airflow service — a genuine 1:1 product match, unlike
        # most "no equivalent" passthrough cases. Kept passthrough for now
        # (not a formula mapper like Glue/MSK) because AWS bills per
        # Environment-class and Worker-class hourly rate (mw1.micro/small/
        # medium/large), while Cloud Composer bills per raw vCPU-hour + GiB-
        # hour + storage — converting the two requires AWS's own published
        # vCPU/RAM-per-class spec table, which isn't independently verifiable
        # from this offline catalog the way Glue's DPU spec or MSK's MCU spec
        # were (both AWS-documented, single well-known numbers). See
        # map_mwaa() for the honest-passthrough note instead of a guessed
        # conversion table.
        "mwaa",
        lambda r: (
            _ilike(r["product"], "Managed Workflows for Apache Airflow")
            or _ilike(r["product"], "AmazonMWAA")
            or _ilike(r["product"], "MWAA")
            or r.get("canonical") == "mwaa"
        ),
    ),
    (
        # AWS Systems Manager Parameter Store (usage_type "PS-Param-..."/
        # "PS-Advanced-Param-...") -> Secret Manager, the architecturally
        # closest GCP service for API-accessed config/secret values. Scoped
        # to Parameter Store usage_types only — other Systems Manager
        # sub-services (Automation, Session Manager, Patch Manager, Inventory,
        # etc.) have no comparable GCP equivalent at all and correctly stay in
        # misc/LLM. Swept the local catalog before writing this: it has NO
        # "Secret Manager" service entry whatsoever (unlike, say, BigQuery or
        # Cloud SQL, which are richly represented) — that reads as a gap in
        # this catalog snapshot, not evidence Secret Manager isn't priced
        # (unlike API Gateway, where sibling network-egress SKUs under the
        # same service DID exist, proving the catalog actively tracks that
        # service and simply has no per-call SKU for it). So this stays an
        # honest passthrough with the correct label rather than a resolved
        # SKU — see map_ssm_parameter_store() for the full reasoning.
        "ssm_parameter_store",
        lambda r: (
            (_ilike(r["product"], "Systems Manager") or _ilike(r["product"], "AWSSystemsManager")
             or r.get("canonical") in ("systemsmanager", "ssm"))
            and _re(r.get("usage_type", ""), r"PS-(?:Advanced-)?Param")
        ),
    ),
    (
        # DynamoDB STORAGE rows only (TimedStorage-ByteHrs, TimedPITRStorage-
        # ByteHrs) — a plain capacity charge with a real, directly comparable
        # Firestore storage SKU (same GiB-mo unit, same free-tier shape), NOT
        # workload-dependent like RCU/WCU throughput. The pricing matrix's
        # documented "DynamoDB -> PASS, workload-dependent" call is specifically
        # about read/write request units (Firestore/Bigtable charge per-
        # operation, not per-provisioned-capacity — genuinely needs judgment
        # on the customer's access pattern) — it was never meant to cover
        # storage, which has no such ambiguity. RCU/WCU rows (usage_type
        # ReadRequestUnits/WriteRequestUnits) deliberately fall through to
        # misc/LLM unchanged, per that matrix design.
        "dynamodb_storage",
        lambda r: (
            (_ilike(r["product"], "DynamoDB") or _ilike(r["product"], "AmazonDynamoDB") or r.get("canonical") == "dynamodb")
            and _re(r.get("usage_type", ""), r"TimedStorage-ByteHrs|TimedPITRStorage-ByteHrs")
        ),
    ),
    (
        # AWS Shield Advanced's flat $3,000/mo subscription fee → Cloud Armor
        # Enterprise's own Annual Subscription SKU (real, dynamically priced —
        # see map_shield()). Scoped to the Monthly-Fee/Subscription row only,
        # so it doesn't take the Shield-tagged data-transfer rows (a different
        # usage_type) away from the data_transfer rule, which already prices
        # those correctly as ordinary egress.
        "shield",
        lambda r: (
            (_ilike(r["product"], "Shield") or _ilike(r["product"], "AWSShield") or r.get("canonical") == "shield")
            and _re(r.get("usage_type", ""), r"Monthly-?Fee")
        ),
    ),
    (
        # QuickSight → Looker Studio Pro. Unlike GuardDuty/Inspector, QuickSight's
        # CUR usage_type strings directly encode role + edition (Author vs Reader,
        # Standard vs Enterprise/Pro) and the row quantity IS the per-user or
        # per-session count (standard, documented AWS billing behavior — not an
        # inference). That's a real, defensible per-unit conversion, so QuickSight
        # gets its own deterministic static mapper (map_quicksight) instead of a
        # blanket passthrough.
        "quicksight",
        lambda r: (
            _ilike(r["product"], "QuickSight")
            or _ilike(r["product"], "AmazonQuickSight")
            or r.get("canonical") == "quicksight"
        ),
    ),
    (
        # Redshift → BigQuery static mapper. Node hours are converted to BQ slot-hours
        # using the slot-per-node table in apply_static_mappings.py. Backup/snapshot
        # rows pass through. Falls through to misc only if instance type is unrecognized.
        "redshift",
        lambda r: (
            _ilike(r["product"], "Redshift")
            or _ilike(r["product"], "AmazonRedshift")
            or r.get("canonical") == "redshift"
        ),
    ),
    (
        # Athena → BigQuery on-demand. Athena bills per-TB-scanned; BigQuery on-demand
        # uses the same unit ($6.25/TB). Static mapper handles the conversion directly.
        "athena",
        lambda r: (
            _ilike(r["product"], "Athena")
            or _ilike(r["product"], "AmazonAthena")
            or r.get("canonical") == "athena"
        ),
    ),
    (
        # Kinesis shard-hours → Pub/Sub throughput. Kinesis data-volume rows (Requests
        # unit) are already handled by per_request → Pub/Sub Message Delivery.
        # This catches hourly shard billing (unit=Hrs) that falls through per_request.
        "kinesis",
        lambda r: (
            (
                _ilike(r["product"], "Kinesis")
                or _ilike(r["product"], "AmazonKinesis")
                or r.get("canonical") == "kinesis"
            )
            and (_is_hourly_unit(r.get("unit")) or (r.get("unit") or "").lower() in ("shard-hrs", "shardhours"))
        ),
    ),
    (
        # EMR management fee rows — caught before misc so they get a deterministic
        # Dataproc mapping rather than an LLM guess. The underlying EC2 cost for
        # EMR worker nodes comes through compute_breakdown (different product line).
        "emr",
        lambda r: (
            _ilike(r["product"], "Elastic MapReduce")
            or _ilike(r["product"], "ElasticMapReduce")
            or _ilike(r["product"], "AmazonEMR")
            or _ilike(r["product"], "Amazon EMR")
            or r.get("canonical") == "emr"
        ),
    ),
    (
        # AWS Glue ETL/Crawler DPU-hours → Dataproc-style deterministic FORMULA
        # mapping, same class as the EMR rule above. Only claims rows that
        # actually carry a DPU-hour usage figure — per the pricing matrix's
        # documented design, Glue rows with no DPU count in the CUR (Catalog-
        # Storage, Catalog-Request — a different, non-compute charge shape)
        # still fall through to misc/LLM, since there's nothing deterministic
        # to convert there.
        #
        # Checked against BOTH pricing_unit AND usage_type, not pricing_unit
        # alone — confirmed real in this exact bill: the genuine positive-cost
        # ETL/Crawler DPU-hour row has pricing_unit="DPU-Hour", but AWS's own
        # negation/credit entry for that identical usage_type
        # ("APS3-ETL-DPU-Hour"/"APS3-Crawler-DPU-Hour") carries pricing_unit=
        # "Hrs" instead — same class of unit-field inconsistency already
        # worked around elsewhere in this file (RDS IOPS, Aurora Serverless
        # PDF rows, WAF LCU). The negative row is harmless here (negative_cost
        # claims it first, above), but it proves a positive-cost row with
        # unit="Hrs" for this exact usage_type is a real shape AWS emits, not
        # a hypothetical — so usage_type is checked as a fallback rather than
        # trusting pricing_unit alone to always say "DPU-Hour".
        "glue",
        lambda r: (
            (_ilike(r["product"], "Glue") or _ilike(r["product"], "AWSGlue") or r.get("canonical") == "glue")
            and (
                _re(r.get("unit", ""), r"DPU-?Hour")
                or _re(r.get("usage_type", ""), r"DPU-?Hour")
            )
        ),
    ),
    (
        # Route 53 DNS query charges → Cloud DNS. PDF-format bills set unit=None
        # so this must match by product name, not unit. Route 53 and Cloud DNS
        # are at cost parity ($0.40/M for first 1B queries, $0.20/M thereafter).
        #
        # "AWS Route 53 Application Recovery Controller" (ARC) is a DIFFERENT
        # product that only shares the "Route 53" brand name — a multi-region
        # failover control-plane (billed as e.g. "Global-Cluster-Hours", an
        # HOURLY cluster fee, pricing_unit="hours"), not a per-DNS-query
        # charge at all. Confirmed real: a live bill's ARC row ($1,237.50,
        # 495 cluster-hours) was matched here anyway (bare "Route 53"
        # substring), then priced as if 495 were a DNS QUERY count against
        # Cloud DNS's tiered $0.40/M-query rate — 495 queries rounds to
        # $0.0002, reported as GCP cost = $0.00 -> a fabricated "100% savings"
        # that was actually just a unit mismatch, not a real comparison.
        # Excluded here so it falls through to its own dedicated service_map
        # rule (honest passthrough, no invented DNS pricing) instead.
        "per_request",
        lambda r: (
            (
                _ilike(r["product"], "Route 53")
                or _re(r.get("usage_type", ""), r"Route53|DNS-Queries")
                or r.get("canonical") == "route53"
            )
            and not _ilike(r["product"], "Application Recovery Controller")
        ),
    ),
    (
        # AWS KMS. Without this rule KMS rows fall through to `misc` and are
        # priced by the LLM, which has consistently used GCP's $1.00/mo HSM
        # rate for ordinary software-backed keys — correct-looking parity that
        # over-projects 16.7x, since the software tier is $0.06/mo.
        # map_per_request() splits key-months from operations and picks the
        # software / HSM / external SKU from the row itself.
        "per_request",
        lambda r: (
            _re(r.get("product", ""), r"Key Management Service|\bKMS\b")
            or r.get("canonical") == "kms"
        ),
    ),
    (
        # AWS WAF per-request charges (standard WAF, BotControl, AntiDDoS-Request,
        # BotControl-Targeted-Request). PDF bills leave unit=None so we can't
        # rely on unit="Requests" — catch by product name before misc fallback.
        # The existing Cloud Armor mapping logic in map_per_request handles these.
        "per_request",
        lambda r: (
            (_re(r.get("product", ""), r"AWS WAF") or r.get("canonical") == "waf")
            and not _re(r.get("usage_type", "") + r.get("operation", ""),
                        r"WebACL-Hour|Rule-Hour|Web ACL|WAF Rule|\bMonth\b")
        ),
    ),
    (
        # AWS WAF flat monthly fees (BotControl per-month, AntiDDoS per-month).
        # PDF bills leave unit=None; matched by product + "Month" in operation.
        "flat_hourly",
        lambda r: (
            (_re(r.get("product", ""), r"AWS WAF") or r.get("canonical") == "waf")
            and _re(r.get("operation", ""), r"\bMonth\b")
        ),
    ),
    (
        "object_storage",
        lambda r: (
            (
                (
                    # Bare substring "s3" is NOT safe: region codes like "APS3"
                    # (Asia Pacific region 3) appear in dozens of unrelated
                    # products' names (CloudTrail, EMR, QuickSight, Redshift, SQS,
                    # VPC, DAX, Data Transfer, ...) and all contain "s3" as a
                    # substring. Some CUR exports do legitimately shorten the
                    # product name to bare "Amazon S3" though, so word-boundary
                    # match "S3" as a whole token, not a substring.
                    (_re(r["product"], r"\bS3\b") or _ilike(r["product"], "Simple Storage")
                     or r.get("canonical") == "s3")
                    and not _ilike(r["product"], "Lambda")
                )
                # Confirmed real: a row with product="Amazon Elastic Block
                # Store" (genuinely mislabeled somewhere upstream) had an
                # operation string unambiguously describing an S3-ONLY
                # storage class ("One Zone-Infrequent Access" — not a real
                # EBS concept at all). operation is more specific evidence
                # than a mislabeled product field — trust it here the same
                # way block_storage's own exclusion for this case does.
                or _re(r["operation"], r"Infrequent Access|Glacier|Intelligent-Tiering|Deep Archive")
            )
            # unit=GB-Mo reliably identifies a STORAGE row (requests/retrieval use
            # count/GB units). The usage_type check is kept as an OR so CSV bills
            # still match, but PDF bills (blank usage_type) route on unit alone —
            # otherwise S3 storage falls to the LLM and gets invented multipliers.
            and (
                (r["unit"] in ("GB-Mo", "GB Month", "GB-Month")
                 and not _re(r["operation"], r"Request|Retrieval|Data Returned|Select"))
                or _re(r["usage_type"], r"TimedStorage|ByteHrs")
                # PDF/simplified-CUR bills leave both unit and usage_type blank —
                # for those, operation naming a storage class (and not a request)
                # is the only signal available at all. Without this, a blank-
                # field Glacier/Intelligent-Tiering/Deep-Archive STORAGE row can't
                # satisfy either branch above and gets excluded from per_request's
                # blank-usage_type catch-all for the identical reason, landing
                # nowhere and going unpriced. Symmetric with the exclusion added
                # to that per_request branch. NB: match "Request" only, not
                # "Retrieval" — "Glacier Instant Retrieval" is the storage
                # class's own proper name and contains "Retrieval" even on
                # pure-storage rows.
                or (
                    _re(r["operation"], r"Infrequent Access|Glacier|Intelligent-Tiering|Deep Archive")
                    and not _re(r["operation"], r"Request|Data Returned|Select")
                )
            )
        ),
    ),
    (
        # AWS Marketplace third-party SaaS, billed through AWS but not an
        # AWS-owned service (Kiro today; any future vendor billed the same
        # way tomorrow) — no GCP equivalent exists or could exist, since it
        # isn't Google's product to offer. Deliberately last in RULES: every
        # real AWS service is checked by a more specific rule above first, so
        # this only ever catches a product name that (a) isn't AWS-branded
        # ("Amazon "/"AWS " prefix — the same signal ingest.py's PDF header
        # guard trusts to tell a real AWS section from a stray line) and (b)
        # wasn't recognized by any earlier rule. That combination is a
        # reliable third-party-SaaS signal, not a guess: a genuine AWS
        # service that's merely missing its own dedicated rule would still
        # carry the "Amazon "/"AWS " prefix and so would NOT match here — it
        # falls through to misc instead, same as before this rule existed,
        # so this can't accidentally swallow an unclassified real AWS
        # service into an incorrect "no GCP equivalent" passthrough.
        #
        # The `\b` boundary check above is unreliable for a raw AWS
        # ProductCode with no space between the "Amazon"/"AWS" prefix and the
        # abbreviation that follows it (e.g. "AmazonVPC", "awskms" — both real,
        # native AWS services, confirmed from a live customer bill): there is
        # no word-boundary between two adjacent word characters regardless of
        # case-folding, so the regex alone misreads them as third-party and
        # routes real workload spend (VPC Endpoints, KMS keys) into a $0/
        # "no GCP equivalent" bucket that should never apply to them. Before
        # trusting the prefix regex, check canonical_service() (Layer 1,
        # aws_normalizer.py) — its alias table already recognizes these exact
        # raw codes ("amazonvpc"->"vpc", "awskms"->"kms", etc.) independent of
        # spacing/casing, so a row it identifies as a real AWS service is
        # never misrouted here even when the regex would have missed it.
        "marketplace_thirdparty",
        lambda r: (
            bool(r["product"])
            and not re.match(r'^(amazon|aws)\s*', r["product"], re.IGNORECASE)
            and canonical_service(r["product"], r.get("product_code")) is None
        ),
    ),
]

def classify_long_tail_with_trace(row: dict) -> tuple[str, str]:
    """Evaluate long-tail rules for services not yet specced in data/service_specs.json."""
    for idx, (group, test) in enumerate(RULES):
        try:
            if test(row):
                return group, f"{group}#{idx}"
        except Exception as exc:
            print(f"classify_mechanics: rule {group}#{idx} raised {exc!r}", file=sys.stderr)
            continue
    return "misc", "misc"


def _legacy_classify_with_trace(row: dict) -> tuple[str, str]:
    return classify_long_tail_with_trace(row)


def _legacy_classify(row: dict) -> str:
    return classify_long_tail_with_trace(row)[0]


def classify_with_trace(row: dict) -> tuple[str, str]:
    """Authoritative classification: routed through spec_dispatch (Layer 1.5)."""
    try:
        import spec_dispatch as _sd
    except ImportError:
        # spec_dispatch genuinely unavailable (e.g. standalone import) — the
        # legacy long-tail is the only engine present. Safe: no spec exists to
        # bypass here.
        return classify_long_tail_with_trace(row)
    try:
        return _sd.classify_via_specs_with_trace(row)
    except Exception as _exc:
        # spec_dispatch raised on THIS row. Do NOT fall through to the long-tail:
        # block_storage's cross-service exclusions were removed at cutover, so a
        # specced row (Lambda/VPC/SageMaker ...) hitting the generic "Storage"
        # rule would silently resurrect the 61x misroute. Route to misc loudly
        # instead, tagged so it is countable in mechanic_rule.
        print(f"classify_mechanics: spec_dispatch raised on "
              f"{row.get('product')!r}/{row.get('usage_type')!r}: {_exc}",
              file=sys.stderr)
        return "misc", "spec_error"


def classify(row: dict) -> str:
    return classify_with_trace(row)[0]


# Canonical field coverage per service keyword — used to annotate misc rows.
# Maps a product-name fragment → (service_label, available_fields, missing_fields).
# missing_fields = fields that are NOT_AVAILABLE_CUR_ONLY for this service type,
# so the misc agent knows what to assume rather than hallucinate.
def _load_misc_hints() -> list[tuple[str, str, list[str], list[str]]]:
    hints = _svc_cfg.get("misc_service_hints", [])
    return [
        (h["key"], h["product_name"], h["fields"], h.get("extra_fields", {}))
        for h in hints
    ]

_MISC_SERVICE_HINTS: list[tuple] = _load_misc_hints()


_PASSTHROUGH_SERVICES = frozenset(_svc_cfg.get("passthrough_services", [
    "Simple Email", "SES", "Inferentia", "Trainium"
]))

_SELF_HOST_ON_GCE = _svc_cfg.get("self_host_on_gce", [
    "OpenSearch", "Elasticsearch",
    "Managed Streaming for Apache Kafka", "MSK", "Kafka",
])

# Sizing-marker patterns that indicate we can derive a meaningful GCP mapping
# from the operation/description field even without CUR-level detail.
_SIZING_RE = re.compile(
    r'\b[a-z][0-9][a-z]?\.[0-9]*x?large\b'
    r'|\b\d+(\.\d+)?\s*(DPU|vCPU|GB|TB|PB)\b'
    r'|\b\d+\s*(node|worker|broker|shard|cluster)\b',
    re.IGNORECASE
)

_SIZING_SENSITIVE = frozenset(_svc_cfg.get("sizing_sensitive", ["EKS", "ECS", "Glue"]))


def _info_completeness(row: dict) -> str:
    """Return 'full', 'partial', or 'minimal' based on available sizing data.

    full    — instance_type/vcpus present, or usage_type has CUR-style granularity
    partial — operation or usage_type contains a concrete sizing marker
    minimal — only product name + cost; no sizing signal present
    """
    if row.get("instance_type") or row.get("instance_vcpus"):
        return "full"
    ut = row.get("usage_type") or ""
    if len(ut) > 10:  # CUR: "APS3-BoxUsage:m6g.2xlarge"; flat CSV: "" or "EC2"
        return "full"
    combined = f"{ut} {row.get('operation') or ''}"
    if _SIZING_RE.search(combined):
        return "partial"
    return "minimal"


# NOTE: tailored branches are dead (lowercase-key mismatch) — superseded by llm_guidance.build_guidance; retire when wired.
def _misc_reason(row: dict) -> str:
    """Produce a structured reason dict for a misc-classified row."""
    product = row.get("product") or ""
    usage_type = row.get("usage_type") or ""
    unit = row.get("unit") or ""

    # DATA GAP: the service IS known (canonical resolved from product_code/name),
    # but the bill row carries no charge-type signal at all — unit AND operation
    # blank, and usage_type absent or a rolled-up "<region>-Usage:NA" placeholder.
    # We know WHICH service, not WHAT KIND of charge, so no mechanic can price it.
    # Flag it explicitly (data_gap=True) so the job run reports the service and the
    # missing fields instead of a silent, unexplained misc.
    canonical = row.get("canonical")
    operation = row.get("operation") or ""
    cost = float(row.get("aws_amortized_cost") or 0.0)
    _ut = usage_type.strip().lower()
    degenerate_ut = (not _ut) or _ut.endswith(":na")
    _it = (row.get("instance_type") or "").strip()
    if (canonical or cost > 0) and not unit.strip() and not operation.strip() and degenerate_ut and not _it:
        if canonical:
            why_text = f"service identified as '{canonical}' but the bill row has no charge-type detail (unit, operation blank; usage_type={usage_type!r})"
            svc_hint = canonical
            guidance_text = (
                f"Identity is known ({canonical}) but this is an aggregated/rolled-up bill line "
                f"with no unit, operation, or granular usage_type, so the charge TYPE "
                f"(instance-hours vs storage vs request) cannot be determined from the source "
                f"bill. Passthrough at cost parity, mapping_confidence=0.3, and note in "
                f"mapping-notes.md that the source bill lacked line-item detail for this "
                f"{canonical} charge."
            )
        else:
            why_text = f"${cost:,.2f} could not be classified — source bill lacked line-item detail (unit, operation, usage_type, instance_type blank)"
            svc_hint = product or "Unclassified Charge"
            guidance_text = (
                f"Source bill lacked line-item detail for this charge (${cost:,.2f}). "
                f"Must passthrough at cost parity — never guess a non-zero GCP SKU."
            )
        return json.dumps({
            "why": why_text,
            "service_hint": svc_hint,
            "data_gap": True,
            "strategy": "passthrough",
            "recommended_strategy": "passthrough",
            "confidence_max": 0.2,
            "missing_fields": ["unit", "operation", "usage_type", "instance_type"],
            "mapping_guidance": guidance_text,
        })

    # Accelerator / specialized silicon (Inferentia, Trainium, GPU) → NEVER a
    # CPU VM. No like-for-like GCP equivalent (GCP uses TPUs / different GPUs),
    # so this needs a human architecture decision. Passthrough at cost parity
    # and flag for manual review — mapping to N2D would be actively misleading.
    if _is_accelerator(row):
        return json.dumps({
            "why": f"AWS accelerator/GPU instance ({row.get('instance_type') or product!r}) — no CPU-VM equivalent",
            "service_hint": "Accelerator / specialized silicon",
            "recommended_strategy": "passthrough",
            "manual_review": True,
            "mapping_guidance": (
                "This is an AI accelerator (Inferentia/Trainium) or GPU instance. Do NOT map "
                "it to a general-purpose CPU VM (N2D) — that hides the architectural change. "
                "Set strategy='passthrough' (cost parity) and mapping_confidence=0.3, and note "
                "'requires manual review: GCP TPU/GPU or accelerator service — not a like-for-like "
                "CPU mapping' in mapping-notes.md."
            ),
        })

    # No-managed-equivalent compute services → self-hosted GCE, sized to the
    # ACTUAL extracted footprint. Never fabricate replication or disk.
    for frag in _SELF_HOST_ON_GCE:
        if frag.lower() in product.lower() or frag.lower() in usage_type.lower():
            has_specs = bool(row.get("instance_vcpus"))
            return json.dumps({
                "why": f"no managed GCP equivalent for product={product!r}; self-host on GCE",
                "service_hint": f"{product} → self-hosted on Compute Engine",
                "recommended_strategy": "break_down" if has_specs else "map",
                "extracted_footprint": {
                    "instance_type": row.get("instance_type"),
                    "instance_vcpus": row.get("instance_vcpus"),
                    "instance_ram_gb": row.get("instance_ram_gb"),
                    "instance_count": row.get("instance_count"),
                },
                "mapping_guidance": (
                    "Map to self-hosted Compute Engine using the EXTRACTED footprint above "
                    "(instance_type/vcpus/ram × instance_count = the real node count from "
                    "instance-hours). break_down into core+ram components exactly like an EC2 "
                    "instance. instance_count IS the actual node count — do NOT invent a "
                    "replication factor or disk size. Storage sub-lines map to Persistent Disk "
                    "(block_storage); the instance line maps to GCE compute."
                ),
            })

    completeness = _info_completeness(row)
    actually_available = [
        f for f in ("instance_type", "instance_vcpus", "instance_ram_gb",
                    "usage_type", "operation", "unit")
        if row.get(f)
    ]

    # Try to match a known service hint
    for fragment, service_label, _, missing in _MISC_SERVICE_HINTS:
        if fragment.lower() in product.lower() or fragment.lower() in usage_type.lower():
            passthrough_only = fragment in _PASSTHROUGH_SERVICES
            sizing_sensitive = fragment in _SIZING_SENSITIVE

            if passthrough_only:
                rec_strategy = "passthrough"
                guidance = (
                    f"{service_label} has no GCP managed equivalent — passthrough at cost "
                    f"parity. Set mapping_confidence=0.3 and note in mapping-notes.md that "
                    f"no GCP comparison is possible for this service."
                )
            elif fragment == "Cognito":
                rec_strategy = "map"
                guidance = (
                    "Map Amazon Cognito to GCP Identity Platform. IMPORTANT: Cognito is "
                    "MAU-priced (Monthly Active Users) but CUR bills it per authentication "
                    "request — you cannot derive MAU count directly. Estimate: "
                    "total_usage (request count) ÷ 20 ≈ MAU (assuming 20 auth events/user/mo). "
                    "Identity Platform pricing: $0.0055/MAU above 10k free tier. "
                    "Document the MAU estimation assumption and set mapping_confidence=0.45."
                )
            elif fragment == "DynamoDB":
                rec_strategy = "map"
                guidance = (
                    "Map Amazon DynamoDB to one of: Firestore (document/key-value, ops-based "
                    "pricing), Bigtable (high-throughput wide-column, node-based pricing), or "
                    "Spanner (strongly consistent relational, node-based). "
                    "Decision rule — check operation field: "
                    "'GetItem/PutItem/Query/Scan' → Firestore (most common); "
                    "'BatchWrite/high-volume analytical' → Bigtable; "
                    "'Transactional/ACID' → Spanner. "
                    "RCU maps to Firestore read ops (1 RCU ≈ 1 document read); "
                    "WCU maps to Firestore write ops (1 WCU ≈ 1 document write). "
                    "Fields rcu/wcu/table_mode are NOT in CUR — infer from total_usage and unit. "
                    "Set mapping_confidence=0.5 and document the target service choice."
                )
            elif sizing_sensitive and completeness == "minimal":
                rec_strategy = "passthrough"
                guidance = (
                    f"{service_label}: sizing fields ({missing}) are absent and no sizing "
                    f"markers found in operation/usage_type — passthrough at cost parity is "
                    f"safer than fabricating a cluster size. Set mapping_confidence=0.3 and "
                    f"note the assumption in mapping-notes.md."
                )
            elif sizing_sensitive and completeness == "partial":
                rec_strategy = "map"
                guidance = (
                    f"Map {service_label} to its GCP equivalent using the sizing signal in "
                    f"operation/usage_type. Fields {missing} are not available — document "
                    f"any cluster-size assumptions and set mapping_confidence ≤ 0.5."
                )
            else:
                rec_strategy = "map"
                guidance = (
                    f"Map {service_label} to its GCP equivalent. "
                    f"Fields {missing} are not in CUR — use service defaults and document assumptions."
                )
            return json.dumps({
                "why": f"no mechanic rule matched; product={product!r}",
                "service_hint": service_label,
                "information_completeness": completeness,
                "actually_available": actually_available,
                "not_available_cur_only": missing,
                "recommended_strategy": rec_strategy,
                "mapping_guidance": guidance,
            })

    # Unknown service — provide generic annotation
    return json.dumps({
        "why": f"unrecognized product={product!r} usage_type={usage_type!r} unit={unit!r}",
        "service_hint": None,
        "information_completeness": completeness,
        "actually_available": actually_available,
        "not_available_cur_only": [],
        "recommended_strategy": "passthrough" if completeness == "minimal" else "map",
        "mapping_guidance": (
            "Service is unrecognized. Map by spend share: if aws_amortized_cost < $10/mo "
            "set strategy='passthrough'. Otherwise find the closest GCP equivalent by product "
            "name and usage_type, document your reasoning in mapping-notes.md, and set "
            "mapping_confidence ≤ 0.6."
        ),
    })


def annotate_misc_field_state(row: dict) -> dict[str, str]:
    """Annotate field states for a misc row in phase2_manifest.json (FIX 18).
    Classifies each column as 'present_in_cur', 'needs_api', 'not_available', or 'not_in_bill'.
    """
    try:
        from field_state import FIELD_REGISTRY, FieldSource
    except Exception:
        FIELD_REGISTRY = {}
        FieldSource = None

    field_state = {}
    for col, val in row.items():
        if col in ("aws_li_key", "mechanic_group", "misc_annotation", "field_state"):
            continue
        if val is not None:
            field_state[col] = "present_in_cur"
        else:
            fdef = FIELD_REGISTRY.get(col)
            if fdef and FieldSource:
                if fdef.source == FieldSource.NOT_AVAILABLE_CUR_ONLY:
                    field_state[col] = "not_available"
                elif fdef.source in (FieldSource.AWS_CATALOG, FieldSource.GCP_CATALOG):
                    field_state[col] = "needs_api"
                else:
                    field_state[col] = "not_in_bill"
            else:
                field_state[col] = "not_in_bill"
    return field_state


def check_manifest_integrity(
    manifest_groups: set[str] | dict[str, list],
    llm_groups: set[str],
    handled_groups: set[str] | frozenset[str],
) -> list[str]:
    """Assert every group present in the manifest is either in llm_groups or in handled_groups (FIX 20).

    Returns a list of warning/error messages for any unhandled groups.
    """
    groups = manifest_groups.keys() if isinstance(manifest_groups, dict) else manifest_groups
    unhandled = []
    for g in sorted(groups):
        if g not in llm_groups and g not in handled_groups:
            spend_str = ""
            if isinstance(manifest_groups, dict) and g in manifest_groups:
                spend = sum(r.get("aws_amortized_cost") or 0.0 for r in manifest_groups[g])
                rows_count = len(manifest_groups[g])
                spend_str = f" ({rows_count} rows, ${spend:,.2f} spend)"
            unhandled.append(
                f"Unhandled mechanic_group '{g}'{spend_str}: excluded from LLM manifest but has no deterministic handler"
            )
    return unhandled


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    con = duckdb.connect(db_path)

    # --- Check table exists ---
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "aws_li_catalog" not in tables:
        print("ERROR: table aws_li_catalog not found in database.", file=sys.stderr)
        sys.exit(1)

    # --- Add columns if missing ---
    existing_cols = {
        r[1].lower()
        for r in con.execute("PRAGMA table_info('aws_li_catalog')").fetchall()
    }
    if "mechanic_group" not in existing_cols:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN mechanic_group TEXT")
        existing_cols.add("mechanic_group")
        print("Added column mechanic_group to aws_li_catalog.")
    if "mechanic_rule" not in existing_cols:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN mechanic_rule TEXT")
        existing_cols.add("mechanic_rule")
        print("Added column mechanic_rule to aws_li_catalog.")

    # --- Load rows ---
    # billing_format / product_code may be absent in DBs ingested before those
    # columns were added — fall back to NULL so classification still runs.
    bf_expr = "billing_format" if "billing_format" in existing_cols else "NULL AS billing_format"
    pc_expr = "product_code" if "product_code" in existing_cols else "NULL AS product_code"
    rows = con.execute(
        f"""
        SELECT
            aws_li_key,
            product,
            usage_type,
            operation,
            pricing_unit AS unit,
            line_item_type,
            pricing_model,
            aws_amortized_cost,
            instance_type,
            instance_vcpus,
            instance_ram_gb,
            instance_count,
            {bf_expr},
            {pc_expr}
        FROM aws_li_catalog
        """
    ).fetchall()

    col_names = [
        "aws_li_key", "product", "usage_type", "operation",
        "unit", "line_item_type", "pricing_model", "aws_amortized_cost",
        "instance_type", "instance_vcpus", "instance_ram_gb", "instance_count",
        "billing_format", "product_code",
    ]

    # --- Determine once, bill-wide, whether ignoring RIFee/SavingsPlanRecurringFee/
    # Reserved-pricing rows is actually safe (see commitment_discount rule docstring) ---
    bill_has_discounted = con.execute(
        "SELECT count(*) FROM aws_li_catalog WHERE line_item_type = 'DiscountedUsage'"
    ).fetchone()[0] > 0
    if not bill_has_discounted:
        print("classify_mechanics: no DiscountedUsage rows found in this bill — "
              "RIFee/SavingsPlanRecurringFee/Reserved-pricing rows will be classified "
              "and priced as real usage instead of ignored as commitment-discount noise.")

    # --- Classify ---
    try:
        from llm_guidance import build_guidance
        from spec_dispatch import dispatch as _spec_dispatch
        _USE_SPEC_GUIDANCE = True
    except Exception as _exc:
        print(f"classify_mechanics: build_guidance unavailable ({_exc!r}); "
              f"falling back to _misc_reason", file=sys.stderr)
        _USE_SPEC_GUIDANCE = False

    updates: list[tuple[str, str, str]] = []
    misc_reasons: dict[str, str] = {}   # aws_li_key → why it landed in misc
    data_gaps: list[tuple[str, str, float, str]] = []  # (canonical, product, cost, aws_li_key) — known service, no charge-type detail
    for raw in rows:
        row = dict(zip(col_names, raw))
        row["_bill_has_discounted_usage"] = bill_has_discounted
        # Authoritative service identity (product_code-first, name fallback),
        # consumed by the rules below instead of each rule re-sniffing the
        # free-text product name. Identity is resolved once, here.
        row["canonical"] = canonical_service(row.get("product"), row.get("product_code"))
        group, rule_label = classify_with_trace(row)
        updates.append((group, rule_label, row["aws_li_key"]))
        if group == "misc":
            if _USE_SPEC_GUIDANCE:
                reason = json.dumps(build_guidance(row, _spec_dispatch(row)), default=str)
            else:
                reason = _misc_reason(row)
            misc_reasons[row["aws_li_key"]] = reason
            try:
                if json.loads(reason).get("data_gap"):
                    data_gaps.append((row.get("canonical") or "?", row.get("product") or "",
                                      float(row.get("aws_amortized_cost") or 0),
                                      row.get("aws_li_key") or ""))
            except (ValueError, TypeError):
                pass

    # Validate mechanic_group against known rule groups
    known_groups = {group for group, _ in RULES} | {"misc"}
    written_groups = {g for g, _, _ in updates}
    unknown_groups = written_groups - known_groups
    if unknown_groups:
        print(f"WARNING: classify_mechanics assigned unknown mechanic_group(s): {sorted(unknown_groups)}", file=sys.stderr)

    # Bulk update
    con.executemany(
        "UPDATE aws_li_catalog SET mechanic_group = ?, mechanic_rule = ? WHERE aws_li_key = ?",
        updates,
    )
    con.commit()

    # --- Breakdown report ---
    stats = con.execute(
        """
        SELECT
            mechanic_group,
            COUNT(*) AS row_count,
            COALESCE(SUM(aws_amortized_cost), 0) AS group_spend
        FROM aws_li_catalog
        GROUP BY mechanic_group
        ORDER BY group_spend DESC
        """
    ).fetchall()

    total_spend = sum(r[2] for r in stats)
    total_rows = sum(r[1] for r in stats)

    print(f"\n{'mechanic_group':<25} {'rows':>8}  {'% rows':>8}  {'spend_usd':>14}  {'% spend':>8}")
    print("-" * 70)
    misc_spend = 0.0
    misc_rows_count = 0
    misc_rows: list[dict] = []
    for group, row_count, group_spend in stats:
        pct_rows = 100.0 * row_count / total_rows if total_rows else 0.0
        pct_spend = 100.0 * group_spend / total_spend if total_spend else 0.0
        print(f"{group:<25} {row_count:>8}  {pct_rows:>7.1f}%  {group_spend:>14,.2f}  {pct_spend:>7.1f}%")
        if group == "misc":
            misc_spend = group_spend
            misc_rows_count = row_count

    print("-" * 70)
    print(f"{'TOTAL':<25} {total_rows:>8}  {'100.0%':>8}  {total_spend:>14,.2f}  {'100.0%':>8}")

    # Diag: misc-spend rows with no deterministic mapping
    if misc_rows_count > 0:
        diag(WARN, "unmapped_misc_spend", "classify_mechanics.py",
             f"{misc_rows_count} row(s) landed in misc with no deterministic mapping",
             rows=misc_rows_count, usd=misc_spend,
             detail={"mechanic_group": "misc", "pct_of_spend": round(100.0 * misc_spend / total_spend, 2) if total_spend else 0.0})

    # --- Data-gap report: known service, but the bill lacks charge-type detail ---
    # These rows are NOT a classification failure — the source bill rolled them up
    # with no unit/operation/granular usage_type, so no mechanic can price them.
    # Surface them in the job run (and a file the report can read) so the operator
    # sees exactly which spend could not be projected and why.
    if data_gaps:
        gap_total = sum(c for _, _, c, _ in data_gaps)
        print(f"\nDATA GAPS: {len(data_gaps)} row(s), ${gap_total:,.2f} — service identified "
              f"but the bill has no charge-type detail (unit/operation/usage_type missing):")
        for canon, prod, cost, _key in sorted(data_gaps, key=lambda x: -x[2]):
            print(f"  ${cost:>12,.2f}  {canon:<16} {prod[:50]}")
        try:
            gap_path = _safe_path(os.path.dirname(db_path), "data_gaps.json")
            with open(gap_path, "w", encoding="utf-8") as _gf:
                json.dump(
                    # aws_li_key is included so downstream writers (ensure_full_coverage.py)
                    # can de-duplicate exactly by key instead of a (product, cost) heuristic.
                    {"count": len(data_gaps), "total_cost": round(gap_total, 2),
                     "rows": [{"aws_li_key": k, "canonical": c, "product": p, "cost": round(co, 2)}
                              for c, p, co, k in sorted(data_gaps, key=lambda x: -x[2])]},
                    _gf, indent=2,
                )
        except Exception as _e:  # pragma: no cover - best-effort reporting
            print(f"  (could not write data_gaps.json: {_e})")

    # --- Gate: misc > 15% of total spend ---
    misc_pct = 100.0 * misc_spend / total_spend if total_spend else 0.0
    if misc_pct > 15.0:
        print(f"\nWARNING: misc group is {misc_pct:.1f}% of total spend (threshold: 15%).")
        print("Misc rows:")
        misc_detail = con.execute(
            """
            SELECT aws_li_key, product, usage_type, operation,
                   line_item_type, pricing_model, aws_amortized_cost
            FROM aws_li_catalog
            WHERE mechanic_group = 'misc'
            ORDER BY aws_amortized_cost DESC
            """
        ).fetchall()
        header = f"  {'aws_li_key':<34} {'product':<30} {'usage_type':<35} {'operation':<35} {'line_item_type':<25} {'pricing_model':<15} {'amortized_cost':>14}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in misc_detail:
            print(
                f"  {str(r[0]):<34} {str(r[1] or ''):<30} {str(r[2] or ''):<35} "
                f"{str(r[3] or ''):<35} {str(r[4] or ''):<25} {str(r[5] or ''):<15} {r[6]:>14,.2f}"
            )

    # --- Emit phase2_manifest.json alongside the DB ---
    import collections  # os is already imported at module scope
    manifest: dict[str, list] = collections.defaultdict(list)

    # FIX 19: Build manifest SELECT column expressions dynamically from existing_cols
    manifest_col_specs = [
        ("aws_li_key", "aws_li_key"),
        ("mechanic_group", "mechanic_group"),
        ("product", "product"),
        ("usage_type", "usage_type"),
        ("operation", "operation"),
        ("pricing_unit", "unit"),
        ("line_item_type", "line_item_type"),
        ("pricing_model", "pricing_model"),
        ("aws_amortized_cost", "aws_amortized_cost"),
        ("instance_type", "instance_type"),
        ("instance_vcpus", "instance_vcpus"),
        ("instance_ram_gb", "instance_ram_gb"),
        ("instance_arch", "instance_arch"),
        ("workload_class", "workload_class"),
        ("billing_days", "billing_days"),
        ("instance_count", "instance_count"),
        ("aws_effective_unit_rate", "aws_effective_unit_rate"),
        ("aws_region", "region"),
        ("gcp_region", "gcp_region"),
        ("is_workload", "is_workload"),
        ("operating_system", "operating_system"),
        ("license_model", "license_model"),
        ("database_engine", "database_engine"),
        ("deployment_option", "deployment_option"),
    ]
    manifest_select_exprs = [
        (f"{db_col} AS {alias}" if db_col != alias else db_col)
        if db_col in existing_cols
        else f"NULL AS {alias}"
        for db_col, alias in manifest_col_specs
    ]
    full_cols = [alias for _, alias in manifest_col_specs]
    manifest_select_sql = ",\n            ".join(manifest_select_exprs)

    row_data_full = con.execute(
        f"""
        SELECT
            {manifest_select_sql}
        FROM aws_li_catalog
        ORDER BY mechanic_group, aws_amortized_cost DESC
        """
    ).fetchall()

    for raw in row_data_full:
        row = dict(zip(full_cols, raw))
        if row["mechanic_group"] == "misc" and row["aws_li_key"] in misc_reasons:
            row["misc_annotation"] = json.loads(misc_reasons[row["aws_li_key"]])
        manifest[row["mechanic_group"]].append(row)

    # Groups resolved deterministically by apply_commitment_ignores.py /
    # apply_static_mappings.py — the LLM must NOT re-touch them, or it could
    # overwrite a stable deterministic mapping with a run-to-run-varying guess.
    skip_groups = set(DETERMINISTIC_GROUPS)
    llm_groups = {g for g in manifest if g not in skip_groups}
    static_groups = {g for g in manifest if g in skip_groups}

    # FIX 20: Integrity check — ensure no group drops silently
    integrity_errors = check_manifest_integrity(manifest, llm_groups, DETERMINISTIC_GROUPS)
    if integrity_errors:
        for err in integrity_errors:
            print(f"CRITICAL WARNING (unhandled group): {err}", file=sys.stderr)

    def _drop_null_cols(rows: list) -> list:
        """Remove columns that are None for every row in the group (T3 token optimization)."""
        if not rows:
            return rows
        all_keys = set(rows[0].keys())
        null_cols = {k for k in all_keys if all(r.get(k) is None for r in rows)}
        if not null_cols:
            return rows
        return [{k: v for k, v in r.items() if k not in null_cols} for r in rows]

    # FIX 18: For misc rows, do not drop null columns; attach per-row field_state instead.
    manifest_out = {}
    for g, rows in manifest.items():
        if g not in llm_groups:
            continue
        if g == "misc":
            processed_rows = []
            refresh_mapped = []
            # Load the refresh store ONCE for the whole misc bucket; each row does
            # up to 4 lookups, so re-reading the file per lookup would be O(4N)
            # disk reads. get_trusted_mapping takes the preloaded store.
            _rstore = load_refresh_store()
            for r in rows:
                trusted = None
                canon = r.get("canonical")
                if not canon and r.get("product"):
                    canon = canonical_service(r.get("product"), r.get("product_code"))
                if canon:
                    trusted = get_trusted_mapping(canon, store=_rstore)
                if not trusted and r.get("product"):
                    clean_prod = r.get("product", "").lower().replace("amazon ", "").replace("aws ", "").strip()
                    trusted = get_trusted_mapping(clean_prod, store=_rstore)
                if not trusted and r.get("product"):
                    trusted = get_trusted_mapping(r.get("product"), store=_rstore)
                if not trusted and r.get("product_code"):
                    trusted = get_trusted_mapping(r.get("product_code"), store=_rstore)

                if trusted:
                    refresh_mapped.append({
                        "aws_li_key": r["aws_li_key"],
                        "gcp_service": trusted.get("gcp_service", "Manual Review"),
                        "gcp_sku_name": trusted.get("gcp_sku_name", ""),
                        "gcp_sku_id": trusted.get("gcp_sku_id", ""),
                        "strategy": trusted.get("strategy", "map"),
                        "component": None,
                        "unit_multiplier": trusted.get("unit_multiplier", 1.0),
                        "gcp_region": r.get("gcp_region"),
                        "projection_note": f"[refresh_store: trusted fact, {trusted.get('confirmation_count', 1)} confirmation(s)]",
                        "mapping_confidence": 1.0,
                        "is_workload": True,
                        "break_down": False,
                        "decided_by": "refresh_store",
                    })
                else:
                    r_copy = dict(r)
                    r_copy["field_state"] = annotate_misc_field_state(r_copy)
                    processed_rows.append(r_copy)

            if refresh_mapped:
                out_mappings_dir = os.path.join(os.path.dirname(db_path), "mappings")
                os.makedirs(out_mappings_dir, exist_ok=True)
                rf_path = os.path.join(out_mappings_dir, "refresh_mappings.json")
                try:
                    with open(rf_path, "w", encoding="utf-8") as f:
                        json.dump(refresh_mapped, f, indent=2)
                    print(f"refresh_store: applied {len(refresh_mapped)} trusted mapping(s) -> {rf_path}")
                except Exception as _e:
                    print(f"refresh_store: failed to write {rf_path}: {_e}", file=sys.stderr)
        else:
            processed_rows = _drop_null_cols(rows)

        manifest_out[g] = {
            "rows": processed_rows,
            "row_count": len(processed_rows),
            "total_spend": sum(r.get("aws_amortized_cost") or 0 for r in processed_rows),
            "needs_llm": len(processed_rows) > 0,
        }

    # Add output_dir so the LLM knows exactly where to write mapping files.
    # Use a relative path — absolute paths cause the LLM to write outside the job dir.
    manifest_out["_meta"] = {
        "output_dir": "projection-audit/mappings",
        "db_path": "projection-audit/projection.duckdb",
        "skip_groups": sorted(static_groups),
    }

    manifest_path = os.path.join(os.path.dirname(db_path), "phase2_manifest.json")
    # Also create the output dir now so the LLM doesn't have to
    os.makedirs(os.path.join(os.path.dirname(db_path), "mappings"), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest_out, fh, indent=2, default=str)
    print(f"\nWrote {manifest_path}  ({len(manifest_out)} groups)")

    con.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
