#!/usr/bin/env python3
from __future__ import annotations
"""
apply_static_mappings.py — Deterministic mappings for flat_hourly, object_storage,
per_request, block_storage, and data_transfer groups. Zero LLM tokens spent.

Usage:
    python3 apply_static_mappings.py <projection.duckdb>

Writes one <group>_mappings.json per handled group into projection-audit/mappings/.
These groups map by fixed rules (volume type, storage class, transfer direction),
so they are resolved here rather than by the LLM — deterministic and variance-free.
"""

import gzip, json, os, re, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from egress_rates import EGRESS_SKUS, cdn_egress_rate
from config_loader import load_data_config as _cfg

_inst_cfg = _cfg("instance-specs")
_gcp_cfg  = _cfg("gcp-model-config")
_svc_cfg  = _cfg("service-classification")


def _safe_path(base: str, *parts: str) -> str:
    """Resolve path and verify it stays within base (path traversal guard)."""
    p = os.path.realpath(os.path.join(base, *parts))
    if not p.startswith(os.path.realpath(base) + os.sep) and p != os.path.realpath(base):
        raise ValueError(f"Path escapes base directory: {p}")
    return p

GCS_NEARLINE            = "Nearline Storage"
GCS_STANDARD            = "Standard Storage"
GCS_ARCHIVE             = "Archive Storage"
GCS_COLDLINE            = "Coldline Storage"
GCP_COMPUTE_ENGINE      = "Compute Engine"

# CUD discount multipliers — single source of truth for the last-resort
# fallback used by both apply_rates.py and validate_fix.py when
# data/cud_pct.json (the real source of truth both scripts load first) is
# unavailable. Previously hand-copied into each file separately — harmless
# while the two copies matched, but nothing enforced that; a future edit to
# one (e.g. an updated AlloyDB rate) could silently drift from the other,
# producing inconsistent 1yr/3yr discounts between the pricing pass and the
# validator meant to catch pricing errors.
def _load_cud_fallback() -> dict:
    raw = _cfg("cud_pct")
    if raw:
        return {k: tuple(v) for k, v in raw.items()}
    return {
        GCP_COMPUTE_ENGINE: (0.63, 0.45),
        "Compute Engine Memory Optimized": (0.59, 0.30),
        "Cloud SQL": (0.75, 0.48),
        "Cloud Spanner": (0.75, 0.60),
        "Cloud Bigtable": (0.75, 0.60),
        "Memorystore": (0.75, 0.48),
        "Cloud Memorystore": (0.75, 0.48),
        "Cloud Memorystore for Redis": (0.75, 0.48),
        "Cloud Memorystore for Memcached": (0.75, 0.48),
        "AlloyDB": (0.75, 0.60),
        "Cloud Run": (0.83, 0.67),
        "DEFAULT": (0.75, 0.60),
    }

CUD_PCT_FALLBACK = _load_cud_fallback()

PUBSUB_MESSAGE_DELIVERY = "Pub/Sub Message Delivery"
GCP_PUBSUB              = "Pub/Sub"
GCP_BALANCED_PD         = "Balanced PD Capacity"
GCP_HYPERDISK_BALANCED  = "Hyperdisk Balanced Capacity"  # real catalog name, confirmed
# cheaper than classic Balanced PD Capacity in every region checked (e.g. $0.08 vs
# $0.10/GiBy.mo in us-east4) — used for gp3/io1/io2 capacity specifically, since those
# volume types also get a provisioned-IOPS/throughput fee priced against Hyperdisk
# Balanced IOPS/Throughput SKUs (a genuinely separate, non-interoperable product line
# from classic PD — classic Balanced PD has no standalone IOPS SKU at all, confirmed
# via find-sku.sh). Pairing classic-PD capacity with Hyperdisk-only performance
# add-ons on the same disk isn't purchasable on real GCP; Hyperdisk Balanced Capacity
# keeps the whole volume within one real, coherent product family.
GCP_STANDARD_PD         = "Storage PD Capacity"  # real catalog name (confirmed via find-sku.sh);
# the old "Standard Persistent Disk Capacity" string never matched any real SKU, so
# resolve_sku() always returned None here and every st1/sc1/standard/magnetic EBS row
# silently fell through to apply_rates.py's downstream word-overlap lazy-fill resolver
# instead — which then mismatched onto an unrelated, pricier "Hyperdisk Balanced Storage
# Pools Standard Capacity" SKU purely because it shared the generic words "Standard"/
# "Capacity", producing a real ~1.73x overprice on EBS Magnetic/st1/sc1 rows.
GCP_CLOUD_STORAGE       = "Cloud Storage"
GCP_CLOUD_SQL           = "Cloud SQL"
GCP_MEMORYSTORE         = "Cloud Memorystore for Redis"
GCP_MEMORYSTORE_MEMCACHED = "Cloud Memorystore for Memcached"
GCP_FILESTORE_HDD       = "Filestore Capacity Basic HDD"
GCP_DATAPROC            = "Dataproc"

# Static mappers (map_object_storage, map_fsx, map_per_request, ...) deliberately
# passthrough certain charge types PERMANENTLY — e.g. S3 Early-Delete penalties,
# Glacier retrieval fees, Object Lambda, FSx ONTAP/OpenZFS — because there is no
# real GCP equivalent, not because nobody has mapped them yet. They stamp this
# intent into projection_note using this consistent phrasing (grep the mappers
# for "no G.*equivalent" to see all the call sites). Single source of truth so
# every downstream gate/repair script (auto_review.py, ensure_catalog_coverage.py,
# validate_fix.py) honors the same intentional-passthrough rows identically —
# a repair pass that doesn't check this will "fix" an intentional decision into
# a wrong, invented SKU (observed: S3 Glacier Early-Delete → ~50x under-projection,
# CloudWatch Log Storage → ~16x over-projection).
INTENTIONAL_PASSTHROUGH_NOTE_ILIKE = _svc_cfg.get("intentional_passthrough_note_ilike", [
    "%no GCS equivalent%",
    "%no GCP equivalent%",
    "%no direct GCP equivalent%",
])


def intentional_passthrough_exclude_clause(column):
    """SQL AND-clause excluding rows whose projection_note marks them as an
    intentional, permanent no-GCP-equivalent passthrough. `column` should be
    the fully-qualified projection_note column reference, e.g. 'm.projection_note'."""
    return " AND ".join(
        f"COALESCE({column}, '') NOT ILIKE '{p}'" for p in INTENTIONAL_PASSTHROUGH_NOTE_ILIKE
    )


class SKUMeta(str):
    """str subclass returned by resolve_sku().
    Existing callers that do `if sku:` or `entry["gcp_sku_id"] = sku` continue
    to work unchanged. New callers access .unit and .resource_group.
    """
    def __new__(cls, sku_id, unit=None, resource_group=None):
        obj = str.__new__(cls, sku_id or "")
        obj.unit = unit
        obj.resource_group = resource_group
        return obj

    def __bool__(self):
        return bool(str.__str__(self))

    @property
    def sku_id(self):
        return str.__str__(self) or None

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

# object_storage: S3 storage class → GCS storage class SKU family.
# SKU names must use current GCS catalog vocabulary ("Standard Storage <region>").
# The legacy name "Regional Storage" word-matched an Archive Storage SKU
# ($0.0015/GB vs Standard's $0.023/GB — a 15x underprojection on bill3).
# ---------------------------------------------------------------------------
# S3 → GCS routing
#
# Two-pass approach:
#   Pass 1: usage_type structural codes (authoritative, always present in CUR exports)
#   Pass 2: blob fallback for PDF/summary bills where usage_type is empty/generic
#   Pass 3 (in main): LLM for truly unknown rows
#
# Pass 1 — keyed on usage_type substrings (region prefix is stripped by lower()).
# None  = no GCP equivalent, passthrough at AWS cost.
# str   = GCS storage class name to map to.
# Order matters: more specific patterns must precede shorter ones they overlap with.
# ---------------------------------------------------------------------------
_S3_USAGE_TYPE_ROUTING = [
    # Intelligent-Tiering storage tiers (most specific first — must precede catch-alls)
    ("timedstorage-int-fa-bytehrs",       GCS_STANDARD),   # IT frequent access → Standard
    ("timedstorage-int-ia-bytehrs",       GCS_NEARLINE),   # IT infrequent access → Nearline
    ("timedstorage-int-aa-bytehrs",       GCS_COLDLINE),   # IT Archive Instant Access → Coldline ($0.005/GB)
    ("timedstorage-int-daa-bytehrs",      GCS_ARCHIVE),    # IT Deep Archive Access → Archive ($0.002/GB)
    # Glacier variants
    ("timedstorage-deeparchivebytehrs",   GCS_ARCHIVE),    # Glacier Deep Archive (compact form)
    ("timedstorage-glacierdeeparchive",   GCS_ARCHIVE),    # Glacier Deep Archive (expanded form)
    ("timedstorage-gda-bytehrs",          GCS_ARCHIVE),    # GDA alias
    ("timedstorage-glacierbytehrs",       GCS_COLDLINE),   # Glacier Flexible
    ("timedstorage-gir-bytehrs",          GCS_COLDLINE),   # Glacier Instant Retrieval
    # IA variants
    ("timedstorage-sia-bytehrs",          GCS_NEARLINE),   # Standard-IA
    ("timedstorage-zia-bytehrs",          GCS_NEARLINE),   # One Zone-IA
    # Standard / RRS (catch-all last within timedStorage family)
    ("timedstorage-rrs-bytehrs",          GCS_STANDARD),   # Reduced Redundancy
    ("timedstorage-bytehrs",              GCS_STANDARD),   # Standard (also catches staging/glacier-staging)
    # Bare "TimedStorage" (no class suffix, older CUR format) is handled by the blob
    # fallback via operation-text keywords ("storage in Standard", etc.) — no catch-all
    # here because "timedstorage" as substring would prematurely match deeper keys.

    # IT monitoring fee: GCS Autoclass has no per-object monitoring charge — $0 on GCP
    ("monitoring-automation-int",         "ignore"),
    # S3 Storage Analytics: per-object analysis report; no GCS equivalent — passthrough
    ("storageanalytics",                  None),
    # Retrieval fees: GCS Nearline/Coldline/Archive have no explicit retrieval charge
    # (cost difference is baked into lower storage rates). Passthrough at AWS cost.
    ("retrieval",                         None),   # ZIA/SIA/Glacier retrieval — passthrough
    ("int-ret",                           None),   # IT retrieval — passthrough
    ("ret-bytehrs",                       None),   # Glacier retrieval — passthrough
    ("earlydelete",                       None),   # Early delete penalty
    ("obj-lambda",                        None),   # S3 Object Lambda (no GCP equivalent)
]

# Region prefix codes embedded in product/usage_type (e.g. "APS3-", "USE1-", "EUW1-")
_S3_REGION_PREFIX_RE = re.compile(r'^[a-z]{2,4}\d?-', re.IGNORECASE)

def _s3_extract_code_from_product(product: str) -> str:
    """For PDF/simplified CUR where usage_type is empty, the AWS usage_type code is
    often embedded as a suffix in the product field:
      'Amazon Simple Storage Service APS3-TimedStorage-ByteHrs' → 'timedstorage-bytehrs'
    Returns the extracted code (lowercased, region-prefix stripped), or ''.
    """
    # Strip the S3 service name prefix
    code = re.sub(r'^amazon\s+simple\s+storage\s+service\s*', '', product.strip(), flags=re.IGNORECASE)
    # Strip leading region prefix (APS3-, USE1-, EU-, etc.)
    code = _S3_REGION_PREFIX_RE.sub('', code)
    return code.lower().strip()


def _s3_route_usage_type(usage_type_lower: str):
    """Return GCS class name, None (passthrough), or sentinel 'unknown'."""
    for key, gcs_class in _S3_USAGE_TYPE_ROUTING:
        if key in usage_type_lower:
            return gcs_class  # str or None
    return "unknown"

# Pass 2 — blob fallback for PDF/summary bills where usage_type is unpopulated.
# Keyed on human-readable substrings that appear in operation/product/description.
_S3_BLOB_FALLBACK_MAP = {
    "glacier deep archive": GCS_ARCHIVE,
    "glacierdeeparchive":   GCS_ARCHIVE,
    "glacier instant":      GCS_COLDLINE,
    "glacierinstant":       GCS_COLDLINE,
    "glacier flexible":     GCS_COLDLINE,
    "glacierflexible":      GCS_COLDLINE,
    "archive instant":      GCS_COLDLINE,
    "standard-ia":          GCS_NEARLINE,
    "standardia":           GCS_NEARLINE,
    "onezone-ia":           GCS_NEARLINE,
    "intelligent":          GCS_STANDARD,   # IT frequent access is the dominant tier
    "standard":             GCS_STANDARD,
    # Old-format Glacier (operation says "Amazon Glacier" — pre-2021 naming for Glacier Flexible)
    "amazon glacier":       GCS_COLDLINE,
    # GCS Autoclass has no per-object monitoring fee → $0 on GCP
    "per 1,000 objects":         "ignore",
    "per 1000 objects":          "ignore",
    "monitoring and automation": "ignore",
    "monitoringautomation":      "ignore",
    # Plain "S3 Storage" operation text (very flat CUR format)
    "s3 storage":                GCS_STANDARD,
    # No GCS equivalent — passthrough at AWS cost
    "glacier requests":     None,   # Glacier transition/lifecycle requests
    "early delete":         None,
    "earlydelete":          None,
}

def _s3_route_blob(blob: str):
    """Blob fallback: longest key first to avoid 'standard' matching 'standard-ia'."""
    for key in sorted(_S3_BLOB_FALLBACK_MAP, key=len, reverse=True):
        if key in blob:
            return _S3_BLOB_FALLBACK_MAP[key]
    return "unknown"

# flat_hourly: usage_type/product/operation fragment → (GCP service, SKU description pattern, unit_multiplier)
# SKU IDs are resolved at runtime from the bundled GCP catalog — no hardcoding.
# sku_desc_pattern: regex matched against sku["description"] in the catalog file.
FLAT_HOURLY_MAP = [
    # ALB maps to Regional External Application Load Balancer Forwarding Rule Minimum
    (r"LoadBalancerUsage.*application|ALB|Application LoadBalancer",
     "Networking", r"Regional External Application Load Balancer Forwarding Rule Minimum", 1.0),
    # "Network Load Balancer Forwarding Rule Minimum" (the old pattern here)
    # never matches any real SKU verbatim — it's a SUBSTRING of several
    # Proxy NLB variants ("Regional External/Internal Proxy Network Load
    # Balancer Forwarding Rule Minimum...", confirmed via find-sku.sh), and
    # resolve_sku()'s substring search picked one of those by accident. AWS
    # NLB is a Layer-4 PASSTHROUGH load balancer — Proxy NLB is a genuinely
    # different, non-interoperable GCP product family (like the EBS
    # gp3/Hyperdisk mixing bug) and was also ~2.8x pricier ($0.028/h Proxy
    # vs $0.01/h Passthrough). Real correct target confirmed in catalog:
    # "Global External Passthrough Network Load Balancer Forwarding Rule".
    (r"LoadBalancerUsage.*network|NLB|Elastic Load Balancing - Network|Network LoadBalancer-hour",
     "Networking", r"Global External Passthrough Network Load Balancer Forwarding Rule", 1.0),
    # NOTE: AWS WAF WebACL/Rule fixed fees are handled by a DEDICATED branch
    # at the top of map_flat_hourly() instead of a plain table entry here —
    # different bill formats express this same charge with genuinely
    # different total_usage units (CUR "WebACL-Hour" in raw hours; PDF-format
    # "WebACLV2"/operation text like "web ACL created (prorated hourly)
    # (3.978 Month)" already gives total_usage in MONTHS) requiring different
    # unit_multiplier per format — a single fixed-multiplier tuple can't
    # express that. Confirmed real: a first attempt at a fixed-multiplier
    # entry here matched neither format's actual text at all (patterns were
    # narrower than the real bill phrasing), silently fell through to the
    # vague "Other Hourly Charge" default, and landed on an unrelated Static
    # IP Charge SKU — a ~99.8% underprice on a real customer job.
    # Bare "LoadBalancerUsage" is the CUR-format Classic-ELB token. PDF bills
    # instead spell this out as "Elastic Load Balancing - Classic" (usage_type)
    # / "LoadBalancer-hour (or partial hour)" (operation) with no Application/
    # Network qualifier — since those qualified variants are already claimed by
    # the two rules above, a bare "LoadBalancer-hour" or "- Classic" reaching
    # this point is unambiguously Classic ELB. Without this, PDF Classic-ELB
    # rows fell through to the generic Compute Engine "Other Hourly Charge"
    # default instead of pricing against a load-balancer SKU at all.
    # NB: the GCP catalog's actual SKU description for this legacy/generic
    # forwarding-rule charge is "Cloud Load Balancer Forwarding Rule Minimum" —
    # no "Classic" ever appears in a real catalog description. The previous
    # pattern here never matched anything, so every AWS Classic-ELB row fell
    # back to passthrough with a "no rate available" note despite a real,
    # priced GCP equivalent existing (confirmed: SKU BD15-114E-9273, $0.028/hr
    # in asia-southeast1).
    (r"LoadBalancerUsage|Elastic Load Balancing - Classic|LoadBalancer-hour",
     "Cloud Load Balancing", r"Cloud Load Balancer Forwarding Rule Minimum", 1.0),
    # Cloud NAT gateway-hours: AWS bills a flat $0.045/hr per gateway regardless of
    # instance count behind it. "Cloud Nat Gateway Uptime" ($0.0014/hr) is GCP's
    # per-VM-instance charge, not a per-gateway fee, and was a ~32x mismatch.
    # "Private Nat Gateway Uptime" ($0.045/hr) is the flat per-gateway SKU — exact parity.
    (r"NatGateway-Hours|NatGateway",
     "Networking", r"Private Nat Gateway Uptime", 1.0),
    # Transit Gateway → Cloud VPN Tunnel ($0.05/hr vs NCC Spoke $0.10/hr).
    # Cloud VPN is the functional equivalent for inter-VPC/on-prem connectivity
    # and costs 2x less than NCC Spoke Hours (the previous mapping).
    (r"TransitGateway-Hours|TransitGateway|TGW",
     "Cloud VPN", r"Cloud VPN Tunnel", 1.0),
    # EKS cluster management fee $0.10/hr → GKE cluster management fee $0.10/hr (parity).
    # Route to "Zonal Kubernetes Clusters" SKU; Regional EKS clusters are rare in CUR.
    (r"AmazonEKS|EKS.*Hours|EKSCluster|Elastic Container Service for Kubernetes",
     "Kubernetes Engine", r"Zonal Kubernetes Clusters", 1.0),
    # VPC idle/allocated (but not in-use) public IPv4: CUR usage_type is
    # "PublicIPv4:IdleAddress" or operation is "AllocateAddressVPC".
    # Flat-CSV/PDF bills write "Idle public IPv4 address" in the operation field.
    # GCP equivalent is "Static IP Charge" ($0.010/hr in most regions).
    # MUST come before the in-use rule: both "Idle" and "InUse" operation texts
    # contain the substring "public IPv4 address", so in-use would match idle
    # rows first if ordered the other way.
    (r"PublicIPv4.*Idle|IdleAddress|AllocateAddress|[Ii]dle.*[Pp]ublic.*[Ii][Pp][Vv]4|[Ii]dle.*[Ii][Pp][Vv]4",
     GCP_COMPUTE_ENGINE, r"Static IP Charge", 1.0),
    # VPC in-use public IPv4: CUR usage_type is "PublicIPv4:InUseAddress";
    # older/simplified CUR may say "In-use public IPv4" or "ElasticIP".
    (r"In-use public IPv4|public IPv4 address|ElasticIP|EIP|PublicIPv4.*InUse|InUseAddress",
     GCP_COMPUTE_ENGINE, r"External IP Charge on a Standard VM", 1.0),
    (r"VPN",
     "Cloud VPN", r"Cloud VPN Tunnel", 1.0),
    # Real catalog description is "Cloud Interconnect - <bandwidth> Dedicated
    # circuit", billed under Compute Engine (not a separate "Cloud
    # Interconnect" billing service, despite the user-facing product name) —
    # AWS DirectConnect line items don't reliably state a bandwidth tier in
    # usage_type, so this assumes the 10Gbps baseline tier (the smallest/most
    # common Dedicated Interconnect port). The previous pattern ("Dedicated
    # Interconnect" under service "Cloud Interconnect") never matched any real
    # SKU at all — wrong description AND wrong service.
    (r"DirectConnect|DX|HostedConnection",
     GCP_COMPUTE_ENGINE, r"Cloud Interconnect - 10Gbps Dedicated circuit", 1.0),
    # Global Accelerator / CloudFront (see PER_REQUEST_MAP below) intentionally
    # do NOT map to a resolved SKU. GCP has no per-GB "Cloud CDN cache egress"
    # charge the way AWS bills CloudFront/GA egress — Cloud CDN's only
    # CDN-specific line items are "Cache Fill" (origin-to-cache pull, the
    # opposite direction) and a subscription-based "Media CDN Cache Egress
    # Subscription" commitment product, neither of which is a fair pay-as-you-go
    # equivalent. Cache-served bytes actually bill as standard internet egress
    # in GCP's real pricing model, but guessing a specific egress tier/SKU here
    # risks a confidently wrong number; passthrough is the honest answer until
    # this needs a real architecture-review flag like OpenSearch/managed-DB do.
    (r"GlobalAccelerator|Global Accelerator",
     None, None, 1.0),
]
FLAT_HOURLY_DEFAULT_SERVICE = GCP_COMPUTE_ENGINE
FLAT_HOURLY_DEFAULT_DESC    = r"Other Hourly Charge"

# per_request: product/usage_type → GCP service + SKU family + unit_multiplier
# ORDER MATTERS — first match wins. Lambda GB-Second must precede generic Lambda
# so compute-time rows get CPU Allocation Time, not invocation pricing.
PER_REQUEST_MAP = [
    # Two separate, still-real bugs found and fixed here:
    # 1. Wrong GCP service entirely: AWS Lambda's real GCP-native equivalent is
    #    the "Cloud Run Functions" service, NOT plain "Cloud Run" (a different,
    #    sibling service in the catalog for container-based Cloud Run
    #    services/jobs) — confirmed via find-sku.sh: "Cloud Run" has no
    #    Lambda-relevant SKUs at all, so both rows below always resolved to
    #    nothing under the old service name.
    # 2. Real catalog descriptions are "Cloud Run functions Memory
    #    (Request-based billing)" and "Cloud Run Functions Invocations" — the
    #    old "Services CPU (Instance-based billing)"/"Cloud Run Requests"
    #    patterns never matched any real SKU description either, on top of
    #    the wrong service. Confirmed live: a PDF-format bill's Lambda rows
    #    landed in mechanic_group='misc' (pricing_unit didn't match the
    #    classify rule's CUR-format-only "Requests"/"Lambda-GB-Second" tokens)
    #    and got correctly priced by the Phase 2 LLM's own find-sku.sh search
    #    — but any CUR-format bill, where Lambda rows DO route through this
    #    deterministic per_request path, would have silently gotten zero SKU
    #    found from both of the old entries.
    (r"Lambda.*GB.Second|Lambda-GB-Second",
                               "Cloud Run Functions", "Cloud Run functions Memory", 1.0),
    (r"Lambda",                "Cloud Run Functions", "Cloud Run Functions Invocations", 1.0),
    # "SimpleQueue"/"SimpleNotification" (no space) never matched the real AWS
    # product name ("Amazon Simple Queue Service", with a space) — confirmed
    # real: these two rules never fired for standard CUR/PDF product text,
    # silently leaving every SQS/SNS row as an unpriced passthrough instead of
    # mapping to Pub/Sub. `\s*` allows the space AWS's real product name has.
    (r"SQS|Simple\s*Queue",    GCP_PUBSUB,         PUBSUB_MESSAGE_DELIVERY,              1.0),
    (r"SNS|Simple\s*Notification", GCP_PUBSUB,     PUBSUB_MESSAGE_DELIVERY,              1.0),
    (r"Kinesis",               GCP_PUBSUB,         PUBSUB_MESSAGE_DELIVERY,              1.0),
    (r"ApiGateway|API Gateway","Cloud Endpoints", "API Gateway Requests",          1.0),
    (r"StepFunctions|Step Functions",
                               "Workflows",       "Workflow Steps",                1.0),
    (r"EventBridge|EventBus",  "Eventarc",        "Eventarc Events",               1.0),
    # CloudFront intentionally has NO entry here — see the Global Accelerator
    # comment in FLAT_HOURLY_MAP above for why. Falling through to this
    # function's "no known GCP equivalent" branch gives an honest passthrough
    # instead of a confidently wrong "Cache Egress" SKU that doesn't exist.
    (r"WAF",                   "Cloud Armor",     "Cloud Armor Requests",          1.0),
    (r"Rekognition",           "Cloud Vision",    "Vision API Requests",           1.0),
    (r"Comprehend",            "Natural Language API", "NL API Requests",          1.0),
    (r"Translate",             "Cloud Translation",   "Translation Characters",    1.0),
    (r"Polly",                 "Text-to-Speech",  "TTS Characters",                1.0),
    (r"Transcribe",            "Speech-to-Text",  "STT Audio",                     1.0),
]

# block_storage: EBS volume_type → GCP Persistent Disk tier (desc_pattern searched
# in the Compute Engine catalog). RDS/managed-db storage rows that landed here map
# to Cloud SQL storage instead (branched on product in map_block_storage).
EBS_VOLUME_MAP = {
    # gp2 maps to Hyperdisk Balanced ($0.083/GB-Mo) for cost comparison; even
    # though gp2 bundles IOPS, Hyperdisk Balanced is the realistic GCP landing
    # zone for migrated workloads and is cheaper than Balanced PD ($0.120/GB-Mo).
    "gp2": GCP_HYPERDISK_BALANCED,
    # gp3 DOES get a separate provisioned-IOPS/throughput fee (priced against
    # Hyperdisk Balanced IOPS/Throughput elsewhere in this function) — must
    # stay on Hyperdisk Balanced Capacity for the same volume, not classic
    # Balanced PD (a different, non-interoperable product family with no
    # IOPS SKU of its own).
    "gp3": GCP_HYPERDISK_BALANCED,
    "io1": "Extreme PD Capacity",
    "io2": "Extreme PD Capacity",
    "st1": GCP_STANDARD_PD,
    "sc1": GCP_STANDARD_PD,
    "standard": GCP_STANDARD_PD,
    "magnetic": GCP_STANDARD_PD,
}
EBS_DEFAULT_DESC = GCP_HYPERDISK_BALANCED

# data_transfer: direction inferred from usage_type. Ingress is free on GCP → ignore.
# Inter-zone / inter-region / internet egress each map to their egress SKU family.
def _transfer_target(usage_type, operation):
    """Classify AWS data-transfer direction. Returns (strategy, direction, note).
    direction is a key into EGRESS_SKUS (or None for ingress). Order matters.

    Calibrated against real CUR usage types:
      - '...-In-Bytes'                          → ingress, free on GCP → ignore
      - '<REG>-<REG>-AWS-Out-Bytes'             → inter-region egress
      - '...Regional...' / intra-AZ / Bandwidth → inter-zone egress
      - other '...-Out-Bytes' to internet       → internet egress
    """
    ut = f"{usage_type or ''} {operation or ''}".lower()
    if re.search(r"in-bytes|datatransfer-in|\bin\b.*byte", ut):
        return ("ignore", None, "ingress is free on GCP")
    # VPC Peering traffic is private intra-AWS-network traffic, not public
    # internet egress — AWS's own $0.01/GB rate on these rows matches its
    # same-region VPC-peering rate (its cross-region peering rate is higher
    # and would carry a region-pair code, caught by the inter-region check
    # below). Falling through to the "internet" default here was pricing it
    # at the public internet-egress rate (~12x higher), comparing the wrong
    # traffic class entirely.
    if "vpcpeering" in ut:
        return ("map", "interzone", "VPC Peering — private inter-VPC traffic, not internet egress")
    # Region-to-region code pair (e.g. "aps1-apn1-aws-out-bytes") → inter-region.
    if re.search(r"\b[a-z]{2,4}\d?-[a-z]{2,4}\d?-aws-out", ut) or "inter-region" in ut or "interregion" in ut:
        return ("map", "interregion", "inter-region egress")
    # 'regional'/intra-AZ = same-region cross-zone traffic → inter-zone.
    # NOTE: bare "bandwidth" is NOT a locality signal — AWS uses it as the generic
    # usage_type/product category label across internet-out, region-to-region, AND
    # actual regional/AZ transfer rows alike (confirmed across job DBs). The real
    # regional/AZ rows already contain the literal word "regional" in their operation
    # text, so matching on "regional" alone still catches them without the false
    # positives bare "bandwidth" was causing on internet-egress and inter-region rows.
    if "regional" in ut or "intra" in ut or re.search(r"az.*az", ut):
        return ("map", "interzone", "inter-zone (same-region) egress")
    return ("map", "internet", "internet egress")


SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR  = os.path.join(SKILL_DIR, "data")
# Global SKU cache shared across all jobs — built once by prefetch_skus.py, appended on miss
RESOLVED_SKUS_FILE = os.path.join(DATA_DIR, "resolved_skus.json")

with open(os.path.join(DATA_DIR, "ec2-instance-types.json")) as _f:
    _EC2_TYPES_FOR_VCPU = json.load(_f)


_SERVICES_CACHE = None


def _load_services():
    """Return {displayName: serviceId} from the bundled services.json.
    Cached — this file is read on nearly every SKU resolution call."""
    global _SERVICES_CACHE
    if _SERVICES_CACHE is not None:
        return _SERVICES_CACHE
    path = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        _SERVICES_CACHE = {s["displayName"]: s["serviceId"] for s in json.load(f)}
    return _SERVICES_CACHE


_SKU_FILE_CACHE = {}


def _load_sku_file(sku_file):
    """Return the parsed SKU list for a bundled catalog file, cached by path.

    Every cost-comparison sweep (cheapest_in_scope, cheapest_gpu_in_scope) and
    every resolve_sku() call re-opened and re-json.load()'d the SAME catalog
    file from scratch — confirmed real cost: cheapest_in_scope() alone calls
    _family_hourly_rate() up to ~40 times per compute row (20 candidate
    families x core+ram), each a fresh 0.3s parse of the 31,728-SKU Compute
    Engine catalog — over a third of a real job's total measured runtime was
    traced to this exact pattern. The parsed content never changes within a
    single pipeline run, so caching by file path is always safe.
    """
    if sku_file not in _SKU_FILE_CACHE:
        with gzip.open(sku_file, "rt") as f:
            _SKU_FILE_CACHE[sku_file] = json.load(f)
    return _SKU_FILE_CACHE[sku_file]


def _no_rate_suffix(sku_id, gcp_region):
    """Return "" if sku_id resolved, else an explicit caveat to append to a
    fixed projection_note string. A row can end up strategy='passthrough'
    purely because resolve_sku() found no SKU for its target service/region —
    but a note like "Redshift RA3 ManagedStorage -> BigQuery Active Storage"
    reads identically whether or not that actually happened, so a customer has
    no way to tell "this was priced" from "this was carried through unpriced
    because no rate exists." Every fixed (non-conditional) projection_note in
    this file must go through this so silently-unpriced rows are never
    described the same way as successfully-priced ones."""
    if sku_id:
        return ""
    return f" — no GCP rate found in {gcp_region}; AWS cost carried through unpriced, NOT a GCP cost estimate"


_FAMILY_RATE_CACHE = {}


def _family_hourly_rate(gcp_service, desc_pattern, gcp_region):
    """Return the USD unit rate for the first exact-region (or GLOBAL) SKU
    matching desc_pattern, or None if no such SKU exists in this region.

    Used for mapping-TIME cost comparisons between candidate GCP machine
    families (e.g. burstable E2 vs sustained-performance N4D for the same
    vCPU/RAM spec). apply_rates.py's rate table (gcp_sku_rates) doesn't exist
    yet at this point in the pipeline — Phase 2 (this file) runs before
    Phase 4 (apply_rates.py) — so this reads the raw bundled SKU JSON
    directly rather than depending on that later stage.

    Result-cached by (service, desc_pattern, region): a cost sweep re-checks
    the same ~20 candidate families for every compute row, and many rows in
    the same job share the same region — without this, each call re-scans
    the entire ~31,728-SKU catalog list with a regex match per SKU, even
    though the underlying file is already cached. Caching the resolved rate
    turns repeat lookups (extremely common — same region, same family) into
    a dict hit instead of a fresh O(n) scan.
    """
    cache_key = (gcp_service, desc_pattern, gcp_region)
    if cache_key in _FAMILY_RATE_CACHE:
        return _FAMILY_RATE_CACHE[cache_key]

    rate = _family_hourly_rate_uncached(gcp_service, desc_pattern, gcp_region)
    _FAMILY_RATE_CACHE[cache_key] = rate
    return rate


# Confirmed real, reviewable exceptions where the Cloud Billing Catalog lists
# a priced SKU for a (family, region) pair that Compute Engine will not
# actually let you deploy — the Billing Catalog and the real machine-type-
# availability check are two different GCP systems that don't always agree,
# and nothing in this pipeline can tell them apart from billing data alone
# (no live Compute Engine "regions/machineTypes" cross-check is wired in).
# Confirmed case: N4D has a real, priced "N4D Instance Core/Ram running in
# Delhi" SKU (present in both the 2026-04-30 and 2026-07-27 catalog
# snapshots) for asia-south2 — but the actual GCP Console VM-creation wizard
# rejects N4D there ("'N4D' isn't available in asia-south2", confirmed via
# screenshot from a customer's own console session, 2026-07-24). Any sweep
# that would otherwise price N4D in this region must treat it as unavailable,
# same as if the catalog had no SKU for it at all — this is a manually
# curated list, not something derivable from the catalog itself, so add an
# entry here (with the evidence that proved it, same as this one) whenever a
# new phantom-availability case is confirmed. Never remove an entry just
# because the catalog later adds/keeps pricing for it — the catalog's
# presence is exactly what's proven unreliable here.
_KNOWN_PHANTOM_AVAILABILITY = {
    tuple(pair) for pair in _gcp_cfg.get("known_phantom_availability", [
        ["N4D Instance Core", "asia-south2"],
        ["N4D Instance Ram", "asia-south2"],
    ])
}


def _family_hourly_rate_uncached(gcp_service, desc_pattern, gcp_region):
    if (desc_pattern, gcp_region) in _KNOWN_PHANTOM_AVAILABILITY:
        return None
    services = _load_services()
    service_id = services.get(gcp_service)
    if not service_id:
        return None
    sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz")
    if not os.path.exists(sku_file):
        return None
    skus = _load_sku_file(sku_file)
    for sku in skus:
        desc = sku.get("description", "")
        if not re.search(desc_pattern, desc, re.IGNORECASE):
            continue
        desc_lower = desc.lower()
        # Reserved/Commitment/DWS-attached variants are a different commercial
        # mechanism (paid upfront/committed, not incremental on-demand) and can
        # carry a genuinely $0 or non-representative per-hour rate — confirmed
        # live: "Reserved Nvidia Tesla A100 80GB GPU in Virginia" substring-
        # matches a plain GPU desc_pattern and prices at exactly $0/hr, which
        # would make it look like the cheapest option in any sweep that hits it.
        if any(q in desc_lower for q in ("preemptible", "reserved", "commitment", "dws defined duration")):
            continue
        geo = sku.get("geoTaxonomy", {})
        regions = sku.get("serviceRegions", [])
        if geo.get("type") != "GLOBAL" and gcp_region not in regions:
            continue
        pricing = sku.get("pricingInfo", [])
        if not pricing:
            continue
        rate = pricing[0].get("pricingExpression", {}).get("tieredRates", [{}])[0].get("unitPrice", {})
        try:
            return int(rate.get("units", 0)) + rate.get("nanos", 0) / 1e9
        except (TypeError, ValueError):
            continue
    return None


# Single source of truth for every general-purpose Compute Engine family this
# pipeline is willing to consider as a cost-optimization candidate. Verified
# directly against the bundled catalog (data/skus/*.json.gz), not guessed.
#
# This replaces the old per-default hardcoded alternatives dict, which was a
# recurring bug pattern: E2's list only checked N4D and missed that N2D AMD/
# T2D AMD are frequently cheaper still (asia-south1: N2D AMD $0.018151/vCPU
# vs N4D $0.022124/vCPU); a later t4g-specific fix only checked C4A against
# that same incomplete E2 sweep and could pick something MORE expensive than
# what was already there. Both were "fixed" by hand-editing one list at a
# time — exactly the niche, doesn't-generalize approach to avoid. Adding a
# family here ONCE makes it a candidate at every call site automatically;
# there is no per-caller list left to fall out of sync.
#
# tier: 'burstable' (AWS CPU-credit-style pricing) or 'sustained' (flat-rate,
# no credit model). arch: 'x86' or 'arm'. workload: 'general' | 'compute' |
# 'memory' — GCP's own product-line split (N-series vs C-series vs M-series),
# mirroring AWS's own m/c/r family split. This axis exists because of a real
# bug found in family_map.json: its "compute" workload correctly targets C3
# (compute-optimized) for modern-gen Intel sources, but its *pre-gen-6
# fallback* was hardcoded to "N2" — a GENERAL-purpose family — for every
# workload uniformly, silently crossing from compute-optimized into
# general-purpose for older c4/c5 instances (confirmed real: C2D AMD is
# ~48.6% cheaper than N2 for the same vCPU/RAM in Mumbai, and is the
# same-tier match — crossing to N2 was never validated as safe). Per the
# Performance-Tier Safety Rule, a compute-optimized AWS source must stay
# within the compute-optimized workload pool, same as sustained can't cross
# into burstable. Every family below is verified against the real bundled
# catalog (data/skus/*.json.gz) — not a hand-picked shortlist. Deliberately
# excluded: A2/A3/A3Plus/A3Ultra/G2/G4/H3/H4D (GPU/HPC — different class
# entirely) and Spot/Preemptible variants (different commercial mechanism,
# handled elsewhere).
# tier/arch/workload classification for every REAL family the catalog scan
# below discovers. This table is NOT the candidate list — it cannot add or
# remove a candidate on its own, it only labels one once the scan has found
# it. GCP's SKU description text has no machine-readable field for "is this
# burstable" or "is this compute-optimized", so *some* small manual mapping
# from family name to (tier, workload) is unavoidable — but unlike the old
# design, an unclassified-but-real family is never silently dropped: it's
# surfaced as a warning (see _discover_gp_families) so a newly-added GCP
# family gets noticed and classified, not invisibly missing from every sweep.
_GP_FAMILY_TIER         = _gcp_cfg.get("gp_family_tier",         {"E2": "burstable", "T2A Arm": "burstable"})
_GP_FAMILY_ARCH         = _gcp_cfg.get("gp_family_arch",         {"T2A Arm": "arm", "C4A Arm": "arm", "N4A": "arm"})
_GP_FAMILY_WORKLOAD     = _gcp_cfg.get("gp_family_workload",     {
    "E2": "general", "N1 Predefined": "general", "N2": "general",
    "N2D AMD": "general", "T2D AMD": "general", "N4": "general",
    "N4D": "general", "N4A": "general", "T2A Arm": "general",
    "C2D AMD": "compute", "C3": "compute", "C3D": "compute", "C4": "compute",
    "C4D": "compute", "C4N": "compute", "C4A Arm": "compute",
    "M3 Memory-optimized": "memory", "M4": "memory", "M4Ultramem224": "memory",
    "Z3": "storage",
})
_GP_FAMILY_NETWORK_TIER = _gcp_cfg.get("gp_family_network_tier", {"E2": "standard", "T2A Arm": "standard"})

# Structural noise patterns in the catalog's "<Family> Instance Core/Ram"
# descriptions that are NOT selectable families in their own right — Sole
# Tenancy/Custom/Custom Extended pricing variants of a family already
# discovered under its plain name, CUD/overcommit premium line items, and
# generic unlabeled "Compute optimized"/"Memory-optimized"/"Confidential
# Computing"/"Flex"/"Sole Tenancy" entries with no specific family attached.
# GPU/accelerator families (A2/A3/A3Plus/A3Ultra/G2/G4/H3/H4D) are excluded
# here deliberately — they belong to the separate GPU price-sweep
# (_discover_gpu_families below), not this general/compute/memory/storage pool,
# since GPU selection needs a different safety floor (GPU-class adequacy, not
# vCPU/RAM tier) than a simple cheapest-of-equivalent-CPU-VMs comparison.
_GP_FAMILY_NOISE_RE = re.compile(
    r"Sole Tenancy|Custom|Committed Use Discount Premium|Overcommit Premium|"
    r"Memory Optimized Upgrade Premium|Security Command Center|"
    r"DWS Calendar Mode|Organization Level|^Confidential Computing|^Flex$|"
    r"^Spot Preemptible|^Compute optimized$|^Memory-optimized$",
    re.IGNORECASE,
)
_GPU_FAMILY_NAMES = {"A2", "A3", "A3Plus", "A3Ultra", "G2", "G4", "H3", "H4D"}

_GP_FAMILIES_CACHE = None


def _discover_gp_families():
    """Scan the REAL bundled Compute Engine catalog for every distinct
    "<Family> Instance Core"/"<Family> Instance Ram" pair and return the
    candidate list dynamically — this is the actual fix for the pattern this
    file used to repeat: a hand-typed family list that quietly went stale
    whenever GCP added a new family (confirmed real misses this session: Z3
    storage-optimized was never in the old hand-typed list at all). The
    candidate SET now comes from the catalog itself; only the tier/arch/
    workload label per family is a (small, necessary) manual lookup.

    A family whose name isn't in the classification tables above is still
    surfaced — with a loud warning, not silently dropped — so a future GCP
    catalog update that introduces a genuinely new family gets noticed
    immediately instead of quietly missing from every cost comparison.
    """
    global _GP_FAMILIES_CACHE
    if _GP_FAMILIES_CACHE is not None:
        return _GP_FAMILIES_CACHE

    families = {}
    try:
        services = _load_services()
        service_id = services.get(GCP_COMPUTE_ENGINE)
        sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz") if service_id else None
        if sku_file and os.path.exists(sku_file):
            skus = _load_sku_file(sku_file)
            # Real descriptions always carry a trailing region suffix, e.g.
            # "E2 Instance Core running in Paris" — anchoring to end-of-string
            # right after Core/Ram (no trailing text allowed) matched nothing
            # at all against the real catalog; must allow anything after.
            pat = re.compile(r"^(.*?)\s+Instance (Core|Ram)\b")
            for sku in skus:
                desc = (sku.get("description") or "").strip()
                m = pat.match(desc)
                if not m:
                    continue
                fam = m.group(1).strip()
                if fam in _GPU_FAMILY_NAMES or _GP_FAMILY_NOISE_RE.search(fam):
                    continue
                families.setdefault(fam, set()).add(m.group(2))
    except Exception as e:
        print(f"WARNING: _discover_gp_families catalog scan failed ({e}); "
              f"falling back to empty candidate set — cost sweeps will find nothing", file=sys.stderr)

    result = []
    for fam, comps in families.items():
        if comps != {"Core", "Ram"}:
            continue  # need both halves priced to be a usable candidate
        workload = _GP_FAMILY_WORKLOAD.get(fam)
        if workload is None:
            print(f"WARNING: catalog family {fam!r} has no tier/workload classification — "
                  f"add it to _GP_FAMILY_TIER/_GP_FAMILY_ARCH/_GP_FAMILY_WORKLOAD or it will "
                  f"never be considered in any cost sweep", file=sys.stderr)
            continue
        tier = _GP_FAMILY_TIER.get(fam, "sustained")
        arch = _GP_FAMILY_ARCH.get(fam, "x86")
        network_tier = _GP_FAMILY_NETWORK_TIER.get(fam, "high")
        # Use the catalog's own family name verbatim as the label — no case
        # transformation. Callers must pass this exact string as
        # `default_label` (e.g. "C4A Arm", not "C4A ARM") for the
        # switched-family comparison to work correctly.
        label = fam
        result.append((label, f"{fam} Instance Core", f"{fam} Instance Ram", arch, tier, workload, network_tier))

    _GP_FAMILIES_CACHE = result
    return result


class _GPFamiliesProxy:
    """Lazy list-like wrapper so `_GP_FAMILIES` still works as a plain
    iterable everywhere it's referenced, without forcing the catalog scan
    at import time (some environments — tests, offline dev — may not have
    the bundled catalog available yet when this module is first imported)."""
    def __iter__(self):
        return iter(_discover_gp_families())

    def __len__(self):
        return len(_discover_gp_families())


_GP_FAMILIES = _GPFamiliesProxy()


def _gp_family_by_label(label):
    for f in _discover_gp_families():
        if f[0] == label:
            return f
    return None


def _arm_workloads():
    """Every workload tag present among currently-discovered arch='arm'
    families (e.g. {'general', 'compute', 'memory'} today from N4A/T2A Arm/
    C4A Arm) — derived from the catalog scan itself, not a hand-maintained
    tuple. GCP's ARM lineup doesn't split by workload as granularly as x86
    does, so a Graviton row's cheapest_in_scope() sweep should consider every
    ARM family regardless of which workload tag it happens to carry — this
    keeps that scope self-updating if a new ARM family with a different
    workload tag is ever added to the catalog, instead of silently excluding
    it because a hardcoded tuple here wasn't also updated."""
    return tuple({f[5] for f in _discover_gp_families() if f[3] == "arm"})


def cheapest_in_scope(default_label, vcpu, ram_gib, region, archs, tiers, workloads=("general",), min_network_tier="standard"):
    """Price every family in `_GP_FAMILIES` whose (arch, tier, workload) is
    allowed by `archs`/`tiers`/`workloads` for this region/spec, and return
    the genuinely cheapest. `archs`/`tiers` define what's SAFE to compare per
    the Performance-Tier Safety Rule (CLAUDE.md §8) — e.g. a sustained AWS
    source passes tiers=['sustained'] only, so a cheaper-but-burstable GCP
    family can never be silently substituted; a burstable AWS source may pass
    tiers=['burstable','sustained'] since that direction is always a pure win.
    `workloads` applies the same rule across GCP's product-line split
    (general/compute/memory) — a compute-optimized AWS source (c4/c5/c6g/...)
    passes workloads=['compute'] only, so it can never silently cross into a
    cheaper general-purpose family; defaults to ['general'] since that's what
    every pre-existing caller in this file compares against.
    `min_network_tier` applies the same rule to GCP's networking axis — an
    AWS source placed in an EFA-enabled cluster for HPC/tightly-coupled
    distributed training must pass "high" so a standard-tier family (E2/T2A)
    can never be silently substituted even if cheaper; defaults to
    "standard" (no restriction) since that's what every pre-existing caller
    already implicitly allowed.
    Which SPECIFIC families exist to consider is entirely driven by
    `_GP_FAMILIES` — callers never hardcode a candidate list.
    Returns (label, core_desc, ram_desc, switched, reason) — same shape as
    the old cheapest_family(), so callers only need to change what they pass
    in, not how they use what comes back. reason is 'cheaper' (default was
    priced here but something else undercut it) or 'unavailable' (default has
    no rate in this region at all) — kept distinct because a fallback picked
    for lack of any alternative isn't a cost optimization and must not read
    like one."""
    _NETWORK_RANK = {"standard": 0, "high": 1}
    min_rank = _NETWORK_RANK.get(min_network_tier, 0)

    default_entry = _gp_family_by_label(default_label)
    default_rate = None
    if default_entry:
        _, dcore, dram, _, _, _, _ = default_entry
        dcr = _family_hourly_rate(GCP_COMPUTE_ENGINE, dcore, region)
        drr = _family_hourly_rate(GCP_COMPUTE_ENGINE, dram, region)
        if dcr is not None and drr is not None:
            default_rate = vcpu * dcr + ram_gib * drr

    priced = []
    for label, core_desc, ram_desc, arch, tier, workload, network_tier in _GP_FAMILIES:
        if arch not in archs or tier not in tiers or workload not in workloads:
            continue
        if _NETWORK_RANK.get(network_tier, 0) < min_rank:
            continue
        core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, core_desc, region)
        ram_rate  = _family_hourly_rate(GCP_COMPUTE_ENGINE, ram_desc, region)
        if core_rate is None or ram_rate is None:
            continue
        priced.append((vcpu * core_rate + ram_gib * ram_rate, label, core_desc, ram_desc))

    if not priced:
        dcore = default_entry[1] if default_entry else None
        dram  = default_entry[2] if default_entry else None
        return default_label, dcore, dram, False, None

    priced.sort(key=lambda x: x[0])
    _, best_label, best_core, best_ram = priced[0]
    switched = best_label != default_label
    reason = None
    if switched:
        reason = "cheaper" if default_rate is not None else "unavailable"
    return best_label, best_core, best_ram, switched, reason


def cheapest_family(default_label, default_core_desc, default_ram_desc,
                     vcpu, ram_gib, region):
    """Deprecated shim over `cheapest_in_scope()` — kept only so any
    not-yet-migrated caller keeps working during the transition. New callers
    should call `cheapest_in_scope()` directly with explicit `archs`/`tiers`
    instead of relying on this function's old default-derived tier guess."""
    entry = _gp_family_by_label(default_label)
    if entry:
        tiers = ["burstable", "sustained"] if entry[4] == "burstable" else ["sustained"]
        archs = [entry[3]]
    else:
        tiers, archs = ["sustained"], ["x86"]
    return cheapest_in_scope(default_label, vcpu, ram_gib, region, archs, tiers)


# ---------------------------------------------------------------------------
# GPU family/model discovery (mirrors _discover_gp_families() above)
# ---------------------------------------------------------------------------
#
# Which GCP GPU models exist and what they cost is discovered from the real
# catalog, same as compute families. Only two things can't be read off the
# catalog's own text and stay as small manual tables: which GCE machine
# family a given GPU model attaches to (GCP's product catalog fact, not
# derivable from SKU text), and a same-or-better capability ordering
# (VRAM + a training/inference class rank) so a cheaper GPU can never be
# silently substituted for a worse one — the GPU equivalent of CLAUDE.md
# §8's Performance-Tier Safety Rule.
_GPU_MODEL_PAIRED_FAMILY = _gcp_cfg.get("gpu_model_paired_family", {
    "T4": "N1 Predefined", "P100": "N1 Predefined", "P4": "N1 Predefined",
    "V100": "N1 Predefined", "L4": "G2", "A100": "A2", "H100": "A3",
})
_GPU_VRAM_GB_FALLBACK = _gcp_cfg.get("gpu_vram_gb_fallback", {
    "T4": 16, "L4": 24, "P100": 16, "P4": 8, "V100": 16, "A100": 40,
})
_GPU_CLASS_RANK = _gcp_cfg.get("gpu_class_rank", {
    "P4": 0, "T4": 1, "P100": 2, "V100": 3, "L4": 3, "A100": 4, "H100": 5,
})

_GPU_MODELS_CACHE = None


def _discover_gpu_models():
    """Scan the real bundled Compute Engine catalog for every attachable
    Nvidia GPU accelerator SKU (e.g. "Nvidia Tesla T4 GPU running in ..."),
    filtering out the AI Platform/Vertex "Batch Prediction"/"Online
    Prediction" managed-service variants (different product, not a raw
    Compute Engine attachment) and Preemptible/Sole-Tenancy/Commitment
    variants (different commercial mechanism, not a comparable candidate).

    Returns a list of (model, vram_gb, desc_pattern, paired_family, class_rank).
    A (model, vram_gb) pair is a distinct candidate — e.g. A100 40GB and
    A100 80GB are different real SKUs at different prices, not the same row.
    """
    global _GPU_MODELS_CACHE
    if _GPU_MODELS_CACHE is not None:
        return _GPU_MODELS_CACHE

    pat = re.compile(r"^Nvidia\s+(?:Tesla\s+)?([A-Za-z0-9]+)(?:\s+(\d+)GB)?\s+GPU\b")
    found = {}
    try:
        services = _load_services()
        service_id = services.get(GCP_COMPUTE_ENGINE)
        sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz") if service_id else None
        if sku_file and os.path.exists(sku_file):
            skus = _load_sku_file(sku_file)
            for sku in skus:
                desc = (sku.get("description") or "").strip()
                m = pat.match(desc)
                if not m:
                    continue
                desc_lower = desc.lower()
                if any(q in desc_lower for q in ("preemptible", "sole tenancy", "commitment")):
                    continue
                model = m.group(1).upper()
                vram = int(m.group(2)) if m.group(2) else _GPU_VRAM_GB_FALLBACK.get(model)
                paired = _GPU_MODEL_PAIRED_FAMILY.get(model)
                class_rank = _GPU_CLASS_RANK.get(model)
                if paired is None or class_rank is None or vram is None:
                    print(f"WARNING: catalog GPU model {model!r} has no paired_family/class_rank/"
                          f"vram classification — add it to _GPU_MODEL_PAIRED_FAMILY/_GPU_VRAM_GB_"
                          f"FALLBACK/_GPU_CLASS_RANK or it will never be considered in any GPU sweep",
                          file=sys.stderr)
                    continue
                key = (model, vram)
                # desc_pattern must match the real catalog text without hardcoding a full
                # literal string — mechanically derived from the model token this scan found.
                gb_part = rf"\s+{vram}GB" if m.group(2) else ""
                desc_pattern = rf"^Nvidia\s+(?:Tesla\s+)?{re.escape(model)}{gb_part}\s+GPU\b"
                # m.group(0) is the exact matched prefix ("Nvidia Tesla A100 80GB GPU") —
                # real catalog text, not a hand-typed display string.
                display_desc = m.group(0)
                if key not in found:
                    found[key] = (model, vram, desc_pattern, paired, class_rank, display_desc)
    except Exception as e:
        print(f"WARNING: _discover_gpu_models catalog scan failed ({e}); falling back to empty "
              f"candidate set — GPU cost sweeps will find nothing", file=sys.stderr)

    _GPU_MODELS_CACHE = list(found.values())
    return _GPU_MODELS_CACHE


def cheapest_gpu_in_scope(default_model, default_vram_gb, required_count, vcpu, ram_gib, region):
    """Price every real GPU model discovered in the catalog that meets or
    exceeds `default_model`'s class rank and VRAM, at the given GPU count,
    combined with its paired machine family's core+RAM cost for this
    row's vcpu/ram — and return the genuinely cheapest. Mirrors
    `cheapest_in_scope()`'s shape but for the GPU axis: never substitutes a
    lower-class or lower-VRAM GPU even if cheaper, only ever equal-or-better
    (same Performance-Tier Safety Rule as CLAUDE.md §8, applied to GPU class
    instead of compute tier).

    Returns (model, vram_gb, core_desc, ram_desc, gpu_desc, gpu_display_desc, switched, reason).
    gpu_desc is the regex pattern for resolve_sku(); gpu_display_desc is the
    clean, real catalog text for customer-facing SKU name/notes.
    """
    models = _discover_gpu_models()
    default_rank = _GPU_CLASS_RANK.get(default_model, 0)

    def _paired_core_ram_descs(family):
        # A2/A3/G2 are deliberately excluded from _discover_gp_families()'s
        # general pool (they're GPU-paired families, not standalone general-
        # purpose candidates) — build their Core/Ram desc strings directly,
        # same convention _discover_gp_families() itself uses internally.
        return f"{family} Instance Core", f"{family} Instance Ram"

    default_entry = next((m for m in models if m[0] == default_model and m[1] == default_vram_gb), None)
    default_rate = None
    if default_entry:
        _, _, ddesc, dpaired, _, _ = default_entry
        dcore_desc, dram_desc = _paired_core_ram_descs(dpaired)
        dgpu_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ddesc, region)
        dcr = _family_hourly_rate(GCP_COMPUTE_ENGINE, dcore_desc, region)
        drr = _family_hourly_rate(GCP_COMPUTE_ENGINE, dram_desc, region)
        if dgpu_rate is not None and dcr is not None and drr is not None:
            default_rate = vcpu * dcr + ram_gib * drr + required_count * dgpu_rate

    priced = []
    for model, vram, desc_pattern, paired, class_rank, display_desc in models:
        if class_rank < default_rank or vram < default_vram_gb:
            continue
        core_desc, ram_desc = _paired_core_ram_descs(paired)
        gpu_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, desc_pattern, region)
        core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, core_desc, region)
        ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ram_desc, region)
        if gpu_rate is None or core_rate is None or ram_rate is None:
            continue
        total = vcpu * core_rate + ram_gib * ram_rate + required_count * gpu_rate
        priced.append((total, model, vram, core_desc, ram_desc, desc_pattern, display_desc))

    if not priced:
        dcore_desc, dram_desc = _paired_core_ram_descs(default_entry[3]) if default_entry else (None, None)
        return (default_model, default_vram_gb, dcore_desc, dram_desc,
                default_entry[2] if default_entry else None,
                default_entry[5] if default_entry else None, False, None)

    priced.sort(key=lambda x: x[0])
    _, best_model, best_vram, best_core, best_ram, best_gpu_desc, best_display_desc = priced[0]
    switched = (best_model, best_vram) != (default_model, default_vram_gb)
    reason = None
    if switched:
        reason = "cheaper" if default_rate is not None else "unavailable"
    return best_model, best_vram, best_core, best_ram, best_gpu_desc, best_display_desc, switched, reason


def _sku_meta(sku):
    """Extract (sku_id, unit, resource_group) from a catalog SKU dict."""
    unit = None
    pricing = sku.get("pricingInfo", [])
    if pricing:
        expr = pricing[0].get("pricingExpression", {})
        unit = expr.get("usageUnit")
    resource_group = sku.get("category", {}).get("resourceGroup")
    return sku["skuId"], unit, resource_group


def lookup_sku_in_catalog(gcp_service, desc_pattern, gcp_region):
    """
    Search the bundled GCP catalog for the first SKU whose description matches
    desc_pattern (regex) and whose serviceRegions include gcp_region (or is GLOBAL).
    Returns (sku_id, unit, resource_group) tuple, or (None, None, None).
    """
    # See _KNOWN_PHANTOM_AVAILABILITY above _family_hourly_rate_uncached: some
    # (family, region) pairs have a real, priced billing-catalog SKU that
    # Compute Engine won't actually deploy — this direct SKU-ID resolution
    # path must refuse them too, not just the cost-comparison sweep, or a
    # caller that resolves a SKU without going through cheapest_in_scope
    # first would still get handed the phantom SKU ID.
    if (desc_pattern, gcp_region) in _KNOWN_PHANTOM_AVAILABILITY:
        return None, None, None
    # Some user-facing GCP service names differ from the billing catalog display name.
    _CATALOG_SERVICE_ALIASES = {
        "Cloud VPN": "Networking",
        "Cloud DNS": "Networking",
        "Cloud NAT": "Networking",
        "Cloud Load Balancing": "Networking",
    }
    services = _load_services()
    service_id = services.get(gcp_service) or services.get(_CATALOG_SERVICE_ALIASES.get(gcp_service, ""))
    if not service_id:
        return None, None, None

    sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz")
    if not os.path.exists(sku_file):
        return None, None, None

    skus = _load_sku_file(sku_file)

    _CONTINENT = {
        "asia": ["asia-east1","asia-east2","asia-northeast1","asia-northeast2","asia-northeast3",
                 "asia-south1","asia-south2","asia-southeast1","asia-southeast2"],
        "europe": ["europe-west1","europe-west2","europe-west3","europe-west4","europe-west6",
                   "europe-north1","europe-central2","europe-southwest1"],
        "us":     ["us-central1","us-east1","us-east4","us-east5","us-south1","us-west1","us-west2","us-west3","us-west4"],
    }

    def _exact_region_match(sku):
        geo = sku.get("geoTaxonomy", {})
        if geo.get("type") == "GLOBAL":
            return True
        regions = sku.get("serviceRegions", [])
        return bool(gcp_region and gcp_region in regions)

    def _continent_region_match(sku):
        # Some families (T2A, newer/niche SKUs) are only sold in a subset of a
        # continent's regions. Used ONLY as a fallback when no exact-region SKU
        # exists at all — an earlier version of this check treated exact-region
        # and same-continent matches as equally valid, so a same-continent SKU
        # (e.g. Standard Storage Hong Kong) could be picked over the correct
        # exact-region one (Standard Storage Mumbai) purely by list order. Never
        # let continent-fallback outrank an exact match.
        regions = sku.get("serviceRegions", [])
        for continent_regions in _CONTINENT.values():
            if gcp_region in continent_regions and any(r in continent_regions for r in regions):
                return True
        return False

    # Qualifiers that make a SKU a more-specific or differently-priced variant.
    # When desc_pattern doesn't mention them, these variants must be excluded —
    # "Nearline Storage" must not resolve to "Autoclass Nearline Storage" or
    # "Nearline Storage Dual-region". The same rule applies to all GCP services.
    # "regional" (cross-ZONE replicated Persistent Disk, ~2x zonal price) must be
    # excluded the same way as the others: AWS EBS/gp3 is a zonal construct with
    # no built-in cross-zone replication, so a bare "Balanced PD Capacity" search
    # was non-deterministically matching "Regional Balanced PD Capacity in
    # Mumbai" ($0.24/GiB-mo) instead of the correct zonal "Balanced PD Capacity
    # in Mumbai" ($0.12/GiB-mo) — a confirmed ~2x/2.6x over-projection on every
    # gp2/gp3 EBS row. Callers that DO want the regional tier (Cloud SQL HA,
    # Filestore Enterprise) already write "Regional" into their own desc_pattern,
    # so they're unaffected — _has_noise() only excludes a qualifier absent from
    # the caller's own pattern.
    _NOISE_QUALIFIERS = ["autoclass", "early delete", "dual-region", "multi-region", "regional",
                         "asynchronous replication protection", "confidential mode"]

    def _has_noise(sku_desc):
        desc_lower = sku_desc.lower()
        pattern_lower = desc_pattern.lower()
        return any(q in desc_lower and q not in pattern_lower for q in _NOISE_QUALIFIERS)

    desc_matches = [sku for sku in skus
                    if re.search(desc_pattern, sku.get("description", ""), re.IGNORECASE)]

    def _select(candidates):
        # Pass 1: non-Preemptible, non-noise (the ideal match)
        for sku in candidates:
            desc = sku.get("description", "").lower()
            if "preemptible" not in desc and not _has_noise(sku.get("description", "")):
                return _sku_meta(sku)
        # Pass 2: non-Preemptible but allow noise (e.g. if only Autoclass SKU exists)
        for sku in candidates:
            if "preemptible" not in sku.get("description", "").lower():
                return _sku_meta(sku)
        # Pass 3: accept Preemptible as last resort
        for sku in candidates:
            return _sku_meta(sku)
        return None

    # Exact-region (or GLOBAL) candidates always take priority. Only fall back
    # to same-continent candidates when NO exact-region SKU exists at all —
    # otherwise a continent-mate could win purely by catalog list order even
    # when the correct exact-region SKU is present later in the list.
    exact = [sku for sku in desc_matches if _exact_region_match(sku)]
    result = _select(exact)
    if result:
        return result

    continent = [sku for sku in desc_matches if _continent_region_match(sku)]
    result = _select(continent)
    if result:
        return result

    return None, None, None


def _gcp_token():
    """Return (kind, value) auth token, or None."""
    import subprocess
    api_key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if api_key:
        return ("key", api_key)
    try:
        import urllib.request as _req
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if tok:
            return ("bearer", tok)
    except Exception:
        pass
    return None


def _live_sku_fetch(gcp_service, desc_pattern, gcp_region):
    """Last-resort live fetch from GCP Cloud Billing Catalog API. Append result to cache."""
    import urllib.request, urllib.parse
    services_file = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(services_file):
        return None
    with open(services_file) as f:
        svc_map = {s["displayName"]: s["serviceId"] for s in json.load(f)}
    svc_id = svc_map.get(gcp_service)
    if not svc_id:
        return None

    token_info = _gcp_token()
    if not token_info:
        return None

    kind, value = token_info
    page_token = ""
    while True:
        url = f"https://cloudbilling.googleapis.com/v1/services/{svc_id}/skus?pageSize=5000"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        if kind == "key":
            url += f"&key={value}"
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {value}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"  live fetch failed: {e}")
            return None

        for sku in data.get("skus", []):
            if not re.search(desc_pattern, sku.get("description", ""), re.IGNORECASE):
                continue
            geo = sku.get("geoTaxonomy", {})
            if geo.get("type") == "GLOBAL" or gcp_region in sku.get("serviceRegions", []):
                return sku["skuId"]

        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return None


_RUNTIME_SKU_CACHE = {}


def resolve_sku(gcp_service, desc_pattern, gcp_region):
    """
    Return the GCP SKU ID for (service, desc_pattern, region).

    Resolution order:
      1. This-process runtime cache — no scan, instant, never persisted
      2. Prefetched global cache (data/resolved_skus.json) — read-only here;
         only prefetch_skus.py (a deliberate, reviewable maintainer step run
         BEFORE jobs execute) ever writes to this file
      3. Bundled catalog (data/skus/*.json.gz) — scan once, cache result for
         the remainder of this process only
      4. Live GCP Billing API — only for genuinely new SKUs not in catalog
         (requires GOOGLE_CLOUD_API_KEY env var or gcloud auth)

    A live job never writes back to the shared disk cache — see the runtime
    cache write below for why.
    """
    # Must be checked BEFORE the resolved_skus.json cache below — that cache
    # was prefetched before _KNOWN_PHANTOM_AVAILABILITY existed and already
    # contains a real, cached SKU ID for the N4D/asia-south2 case (confirmed:
    # resolved_skus.json has "Compute Engine|N4D Instance Core|asia-south2"
    # resolved to a real sku_id from an earlier prefetch run). A cache hit
    # returns immediately at step 2 below, so putting this check only in
    # lookup_sku_in_catalog()/_family_hourly_rate_uncached (step 3, the
    # catalog-scan fallback) never actually runs for an already-cached key —
    # the exact bug this guard exists to prevent would still slip through
    # resolve_sku() itself, which is what actually assigns the SKU ID used in
    # the final report, not just the cost-comparison sweep.
    if (desc_pattern, gcp_region) in _KNOWN_PHANTOM_AVAILABILITY:
        return SKUMeta(None, None, None)

    catalog_version = _catalog_version()
    key = f"{gcp_service}|{desc_pattern}|{gcp_region or ''}"

    if key in _RUNTIME_SKU_CACHE:
        v = _RUNTIME_SKU_CACHE[key]
        if v.get("sku_id") is not None or v.get("catalog_version") == catalog_version:
            return SKUMeta(v.get("sku_id"), v.get("unit"), v.get("resource_group"))

    cache = {}
    if os.path.exists(RESOLVED_SKUS_FILE):
        try:
            with open(RESOLVED_SKUS_FILE) as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    if key in cache:
        v = cache[key]
        cached_sku_id = v.get("sku_id") if isinstance(v, dict) else v
        if cached_sku_id is not None:
            # A real hit — GCP essentially never changes a SKU ID for an
            # existing product between refreshes, always trust this.
            if isinstance(v, dict):
                return SKUMeta(v.get("sku_id"), v.get("unit"), v.get("resource_group"))
            return SKUMeta(v)
        # A cached MISS. Only trust it if it was recorded against the current
        # catalog version — otherwise a refresh that added the SKU/region would
        # be masked forever by a stale negative (this cache is checked before
        # ever consulting the catalog again). Old entries with no version tag
        # predate this fix; treat them as stale so they get one fresh re-check.
        # Re-checking a real miss still costs one live-API round-trip (a
        # `gcloud auth print-access-token` subprocess + HTTP call) — expensive
        # if done on every call, which is why misses ARE re-cached below, just
        # version-tagged instead of trusted unconditionally forever.
        if isinstance(v, dict) and v.get("catalog_version") == catalog_version:
            return SKUMeta(None, None, None)

    # Not in cache (or cached miss is stale for this catalog version): try
    # bundled catalog.
    sku_id, unit, resource_group = lookup_sku_in_catalog(gcp_service, desc_pattern, gcp_region)

    if sku_id is None:
        # Genuinely missing — trigger live fetch for this new SKU only
        print(f"  SKU not in bundled catalog, trying live API: {gcp_service} / {desc_pattern!r}")
        raw = _live_sku_fetch(gcp_service, desc_pattern, gcp_region)
        sku_id = raw
        unit = None
        resource_group = None

    if sku_id:
        print(f"  resolved SKU: {gcp_service} / {desc_pattern!r} → {sku_id}")
    else:
        print(f"  WARNING: no SKU found for {gcp_service} / {desc_pattern!r} in {gcp_region}")

    # In-memory only — never write back to the shared, cross-job disk cache
    # during a live job run. Two separate real bugs this session (a stale
    # negative result surviving a catalog refresh, and a stale WRONG positive
    # result surviving a matching-logic fix) both stemmed from a live job
    # silently writing a result into resolved_skus.json that then poisoned
    # every future job forever. Pre-populating that shared cache is now a
    # deliberate, reviewable maintainer step (prefetch_skus.py, run BEFORE
    # jobs execute) — resolve_sku() still reads it (respects whatever was
    # deliberately prefetched) but a live job's own lookups are cached only
    # for the remainder of THIS process, so repeat lookups within one run
    # stay fast without any risk of leaking into other jobs.
    _RUNTIME_SKU_CACHE[key] = {
        "sku_id": sku_id, "unit": unit, "resource_group": resource_group,
        "catalog_version": catalog_version if sku_id is None else None,
    }

    return SKUMeta(sku_id, unit, resource_group)


def _strict_resolve_sku(gcp_service, desc_pattern, gcp_region):
    """Like resolve_sku(), but refuses lookup_sku_in_catalog()'s continent-
    fallback substitution — only returns a SKU if _family_hourly_rate()
    independently confirms an EXACT-region rate exists first.

    Confirmed real bug this prevents: cheapest_in_scope()/_family_hourly_rate()
    decide "is this family available here" using STRICT exact-region-or-
    GLOBAL matching (no continent fallback). But a plain resolve_sku() call
    on the family/region it picks goes through lookup_sku_in_catalog(),
    which DOES fall back to a same-continent SKU when no exact-region one
    exists — a fallback meant for genuinely continent-restricted niche SKUs
    (T2A, newer families), not for "this general-purpose family isn't sold
    here at all." The two disagreeing meant a row cheapest_in_scope()
    correctly determined had NO real GCP family available in-region could
    still resolve_sku() its way to a DIFFERENT region's SKU/price with zero
    disclosure — confirmed live: an m6g row in asia-south2 (Delhi), where
    neither C4A Arm nor N4A has any rate, still resolved to C4A Arm's
    Taiwan (asia-east1) SKU/price via continent fallback, silently pricing
    a Delhi customer's bill off Taiwan rates.

    Use this (not resolve_sku() directly) for any core/ram/GPU SKU that was
    just chosen by cheapest_in_scope()/cheapest_gpu_in_scope() — anywhere
    else, plain resolve_sku() and its continent-fallback are unaffected."""
    if _family_hourly_rate(gcp_service, desc_pattern, gcp_region) is None:
        return None
    return resolve_sku(gcp_service, desc_pattern, gcp_region)


def _catalog_version():
    """Cached fetched_at timestamp from data/CATALOG_META.json, or '' if absent.
    Used to version-tag cached SKU-resolution misses so they invalidate on
    the next catalog refresh instead of staying wrong forever."""
    global _CATALOG_VERSION_CACHE
    if _CATALOG_VERSION_CACHE is None:
        meta_path = os.path.join(DATA_DIR, "CATALOG_META.json")
        try:
            with open(meta_path) as f:
                _CATALOG_VERSION_CACHE = json.load(f).get("fetched_at", "")
        except Exception:
            _CATALOG_VERSION_CACHE = ""
    return _CATALOG_VERSION_CACHE


_CATALOG_VERSION_CACHE = None


def _match(usage_type, product, table, operation=None):
    combined = f"{usage_type or ''} {product or ''} {operation or ''}"
    for pattern, *values in table:
        if re.search(pattern, combined, re.IGNORECASE):
            return values
    return None


def map_object_storage(rows):
    """Map S3 storage rows to GCS.

    Three-pass routing (most reliable first):
      Pass 1: usage_type structural codes — authoritative AWS billing keys
      Pass 2: blob fallback — for PDF/summary bills with unpopulated usage_type
      Pass 3: LLM — truly unknown usage_type; injected into misc by main()

    Returns (mapped_rows, llm_rows). main() writes mapped_rows to the mappings
    file and injects llm_rows into the manifest misc group for Phase 2 LLM.
    """
    mapped = []
    llm_rows = []

    for r in rows:
        gcp_region = r.get("gcp_region")
        usage_type_lower = (r.get("usage_type") or "").lower()

        # ── Pass 1: structured usage_type code lookup ────────────────────────
        result = _s3_route_usage_type(usage_type_lower)

        if result != "unknown":
            if result == "ignore":
                # Fee that GCP doesn't charge (e.g. IT per-object monitoring — GCS Autoclass is free)
                mapped.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       None,
                    "component":          "storage",
                    "strategy":           "ignore",
                    "unit_multiplier":    1.0,
                    "gcp_region":         gcp_region,
                    "projection_note":    (f"S3 fee with no GCS equivalent — $0 on GCP "
                                          f"(usage_type={r.get('usage_type')!r})"),
                    "mapping_confidence": 0.90,
                })
            elif result is None:
                # Known fee type with no GCP equivalent — passthrough at AWS cost
                mapped.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       None,
                    "component":          "storage",
                    "strategy":           "passthrough",
                    "unit_multiplier":    1.0,
                    "gcp_region":         gcp_region,
                    "projection_note":    (f"S3 fee with no GCS equivalent "
                                          f"(usage_type={r.get('usage_type')!r}) — "
                                          f"passthrough at cost parity"),
                    "mapping_confidence": 0.40,
                })
            else:
                # Known storage class — resolve SKU directly to avoid word-overlap errors
                sku_id = resolve_sku(GCP_CLOUD_STORAGE, result, gcp_region)
                entry = {
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       result,
                    "component":          "storage",
                    "strategy":           "map",
                    "unit_multiplier":    1.0 / 1.024,  # AWS decimal GB-Mo → GCP binary GiBy.mo
                    "gcp_region":         gcp_region,
                    "projection_note":    f"S3 usage_type routing → {result}",
                    "mapping_confidence": 0.95,
                }
                if sku_id:
                    entry["gcp_sku_id"] = sku_id
                    entry["gcp_sku_unit"] = sku_id.unit
                mapped.append(entry)
            continue

        # ── Pass 1.5: extract usage_type code from product (PDF/simplified CUR) ──
        # PDF bills often leave usage_type empty but embed the AWS code in the product
        # field: "Amazon Simple Storage Service APS3-TimedStorage-INT-DAA-ByteHrs".
        # Stripping the service name and region prefix recovers the routing key.
        if not usage_type_lower.strip():
            extracted = _s3_extract_code_from_product(r.get("product") or "")
            if extracted:
                result = _s3_route_usage_type(extracted)
                if result != "unknown":
                    # Emit using the same logic as pass 1 (reuse the block below by
                    # jumping to the pass-1 emit path via a synthetic continue).
                    _r = result  # capture before overwrite
                    if _r == "ignore":
                        mapped.append({
                            "aws_li_key": r["aws_li_key"], "gcp_service": GCP_CLOUD_STORAGE,
                            "gcp_sku_name": None, "component": "storage", "strategy": "ignore",
                            "unit_multiplier": 1.0, "gcp_region": gcp_region,
                            "projection_note": f"S3 fee — $0 on GCP (product code: {extracted!r})",
                            "mapping_confidence": 0.90,
                        })
                    elif _r is None:
                        mapped.append({
                            "aws_li_key": r["aws_li_key"], "gcp_service": GCP_CLOUD_STORAGE,
                            "gcp_sku_name": None, "component": "storage", "strategy": "passthrough",
                            "unit_multiplier": 1.0, "gcp_region": gcp_region,
                            "projection_note": f"S3 fee with no GCS equivalent (product code: {extracted!r}) — passthrough",
                            "mapping_confidence": 0.40,
                        })
                    else:
                        sku_id = resolve_sku(GCP_CLOUD_STORAGE, _r, gcp_region)
                        entry = {
                            "aws_li_key": r["aws_li_key"], "gcp_service": GCP_CLOUD_STORAGE,
                            "gcp_sku_name": _r, "component": "storage", "strategy": "map",
                            "unit_multiplier": 1.0 / 1.024, "gcp_region": gcp_region,  # AWS decimal GB → GCP GiBy
                            "projection_note": f"S3 product-code routing → {_r} (extracted from product)",
                            "mapping_confidence": 0.90,
                        }
                        if sku_id:
                            entry["gcp_sku_id"] = sku_id
                            entry["gcp_sku_unit"] = sku_id.unit
                        mapped.append(entry)
                    continue

        # ── Pass 2: blob fallback for PDF/summary bills ──────────────────────
        blob = (f"{usage_type_lower} {(r.get('operation') or '').lower()} "
                f"{(r.get('product') or '').lower()}")
        result_blob = _s3_route_blob(blob)

        if result_blob != "unknown":
            if result_blob == "ignore":
                mapped.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       None,
                    "component":          "storage",
                    "strategy":           "ignore",
                    "unit_multiplier":    1.0,
                    "gcp_region":         gcp_region,
                    "projection_note":    "S3 fee with no GCS equivalent — $0 on GCP (blob match)",
                    "mapping_confidence": 0.85,
                })
            elif result_blob is None:
                mapped.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       None,
                    "component":          "storage",
                    "strategy":           "passthrough",
                    "unit_multiplier":    1.0,
                    "gcp_region":         gcp_region,
                    "projection_note":    "S3 fee with no GCS equivalent (blob match) — passthrough at cost parity",
                    "mapping_confidence": 0.40,
                })
            else:
                sku_id = resolve_sku(GCP_CLOUD_STORAGE, result_blob, gcp_region)
                entry = {
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_STORAGE,
                    "gcp_sku_name":       result_blob,
                    "component":          "storage",
                    "strategy":           "map",
                    "unit_multiplier":    1.0 / 1.024,  # AWS decimal GB-Mo → GCP binary GiBy.mo
                    "gcp_region":         gcp_region,
                    "projection_note":    f"S3 blob fallback → {result_blob}",
                    "mapping_confidence": 0.75,
                }
                if sku_id:
                    entry["gcp_sku_id"] = sku_id
                    entry["gcp_sku_unit"] = sku_id.unit
                mapped.append(entry)
            continue

        # ── Pass 3: truly unknown — send to LLM ─────────────────────────────
        llm_rows.append(r)

    return mapped, llm_rows


# Some flat_hourly mappings are genuine 1:1 rate parity (verified against the
# catalog — e.g. NAT Gateway, Transit Gateway) and deserve the default high
# confidence + a plain note. Others are acknowledged approximations because
# the AWS and GCP billing models don't line up dimension-for-dimension:
#   - ALB/NLB LCU-hours: AWS's LCU meters new-connections + active-connections +
#     bandwidth + rule-evaluations as ONE blended unit; GCP prices forwarding
#     rules and data processing as separate line items.
#   - CloudFront -> Cloud CDN: cache egress cost depends heavily on cache-hit
#     ratio and destination geography, neither of which the CUR/PDF bill
#     exposes.
# Cross-checked against an independent reference report on the same bill: this
# approximation already lands on the same dollar figure the reference does
# (ALB/NLB LCU and CloudFront rows matched to the cent) — so this is NOT a
# confidence problem, the estimate is empirically accurate. What the reference
# report does better is explain the estimate inline (exact formula, alternate
# rate considered, computed ratio) instead of a bare "mapped to X". Match that
# transparency here: keep confidence as-is, richen the note.
# Keyed by a substring of the resolved sku_name (post regex-strip), so this
# doesn't require restructuring FLAT_HOURLY_MAP's tuple shape.
_FLAT_HOURLY_APPROXIMATE_NOTE = {
    "Application Load Balancer Forwarding Rule Minimum":
        " [estimate: ALB LCU-hours bundle connections+bandwidth+rule-evals into one AWS meter; "
        "GCP prices forwarding rules and data processing separately — this SKU covers the base "
        "forwarding-rule charge only, not the full LCU bundle]",
    "Passthrough Network Load Balancer Forwarding Rule":
        " [estimate: NLB LCU-hours bundle connections+bandwidth into one AWS meter; "
        "GCP prices forwarding rules and data processing separately — this SKU covers the base "
        "forwarding-rule charge only, not the full LCU bundle]",
}


def map_flat_hourly(rows):
    out = []
    for r in rows:
        # AWS WAF WebACL/Rule fixed fees — dedicated branch (not a plain
        # FLAT_HOURLY_MAP entry) because total_usage's real unit varies by
        # bill format: CUR-format "WebACL-Hour"/"Rule-Hour" usage_type tokens
        # give total_usage in raw HOURS; PDF-format bills (product like
        # "APS3-WebACLV2", operation like "web ACL created (prorated hourly)
        # (3.978 Month)") give total_usage ALREADY IN MONTHS. Applying the
        # same hours→month conversion to both would be wrong for the latter
        # (confirmed real: total_usage=3.978 for a $19.89 AWS row exactly
        # matches the "(3.978 Month)" figure in the operation text — it's
        # already month-denominated, not hours). Detect which format this
        # row is and pick the multiplier accordingly.
        waf_blob = f"{r.get('product') or ''} {r.get('usage_type') or ''} {r.get('operation') or ''}"
        is_webacl = bool(re.search(r"WebACLV2|WebACL-Hour|Web ACL", waf_blob, re.IGNORECASE))
        is_waf_rule = bool(re.search(r"\bRuleV2\b|Rule-Hour|WAF Rule", waf_blob, re.IGNORECASE)) and "waf" in waf_blob.lower()
        if is_webacl or is_waf_rule:
            already_in_months = bool(re.search(r"\bMonth\b", waf_blob, re.IGNORECASE))
            desc = "Networking Cloud Armor Policy" if is_webacl else "Networking Cloud Armor Rule"
            mult = 1.0 if already_in_months else (1.0 / 730.0)
            gcp_region = r.get("gcp_region") or "global"
            sku_id = resolve_sku("Networking", desc, gcp_region)
            out.append({
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      "Networking",
                "gcp_sku_id":       sku_id if sku_id else None,
                "gcp_sku_name":     desc,
                "component":        "hourly",
                "strategy":         "map" if sku_id else "passthrough",
                "unit_multiplier":  mult,
                "gcp_region":       gcp_region,
                "projection_note":  (f"AWS WAF {'WebACL' if is_webacl else 'Rule'} fixed fee → {desc} "
                                     f"(real GCP rate is flat $/month; total_usage detected as "
                                     f"{'already month-denominated' if already_in_months else 'raw hours, converted via /730'})"),
                "mapping_confidence": 0.80,
            })
            continue

        match = _match(r.get("usage_type"), r.get("product"), FLAT_HOURLY_MAP, r.get("operation"))
        if match:
            service, desc_pattern, mult = match
        else:
            service, desc_pattern, mult = FLAT_HOURLY_DEFAULT_SERVICE, FLAT_HOURLY_DEFAULT_DESC, 1.0

        # (service=None, desc=None) marks a deliberate "no honest SKU exists"
        # entry (see Global Accelerator above) — passthrough rather than guess.
        if service is None and desc_pattern is None:
            out.append({
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      None,
                "gcp_sku_name":     None,
                "component":        "hourly",
                "strategy":         "passthrough",
                "unit_multiplier":  1.0,
                "gcp_region":       r.get("gcp_region"),
                "projection_note":  ("No fair per-GB GCP equivalent: Cloud CDN's only CDN-specific "
                                     "charges are Cache Fill (origin-pull, opposite direction) and a "
                                     "subscription-based Media CDN product — cache-served egress "
                                     "actually bills as standard internet egress but guessing a "
                                     "specific tier/SKU risks a confidently wrong number"),
                "mapping_confidence": 0.90,
            })
            continue

        gcp_region = r.get("gcp_region")
        # PDF/flat-CSV bills often land gcp_region='global'. For services whose SKUs
        # are region-specific (Cloud VPN, Transit Gateway), fall back to parsing the
        # region prefix from usage_type (e.g. "APS5-TransitGateway-Hours" → asia-south2).
        if not gcp_region or gcp_region == "global":
            ut = r.get("usage_type") or ""
            m = _UT_PREFIX_RE.match(ut)
            if m:
                gcp_region = _UT_PREFIX_TO_GCP.get(m.group(1).lower(), gcp_region)

        sku_id = resolve_sku(service, desc_pattern, gcp_region)

        # Use the matched description pattern as a human-readable name (strip regex chars)
        sku_name = re.sub(r'[\\^$.*+?()[\]{}|]', '', desc_pattern).strip()

        caveat = next((note for key, note in _FLAT_HOURLY_APPROXIMATE_NOTE.items() if key in sku_name), "")

        # Direct Connect / Transit Gateway: CUR genuinely doesn't carry the
        # signal needed to pick correctly here (true port bandwidth, true
        # traffic topology) — silently assuming one number papers over a real
        # evidence gap. Same precedent as OpenSearch's 70% confidence ceiling
        # (CLAUDE.md §6): cap confidence and say plainly what's unknown,
        # rather than a silent point estimate that reads as more certain than
        # it is. Checked against the row's own text, not the resolved
        # sku_name, since "Cloud VPN Tunnel" also legitimately matches plain
        # VPN rows that have no such uncertainty.
        combined = f"{r.get('usage_type') or ''} {r.get('product') or ''} {r.get('operation') or ''}"
        confidence = 0.90
        review_note = ""
        if re.search(r"DirectConnect|HostedConnection", combined, re.IGNORECASE):
            confidence = 0.55
            review_note = (" [architecture review recommended: AWS DirectConnect line items don't "
                            "reliably state the actual port bandwidth (1/10/50/100Gbps) in CUR — "
                            "this assumes the 10Gbps baseline tier; a 1Gbps port would be "
                            "over-projected ~10x, a 100Gbps port under-projected ~10x — verify the "
                            "real port speed with customer before finalizing]")
        elif re.search(r"TransitGateway|TGW", combined, re.IGNORECASE):
            confidence = 0.60
            review_note = (" [architecture review recommended: Cloud VPN Tunnel is assumed without "
                            "checking whether Dedicated Interconnect or plain VPC Peering would be "
                            "the cheaper same-or-better fit for this traffic's actual topology — CUR "
                            "alone doesn't indicate on-prem vs intra-region vs cross-region traffic "
                            "patterns; verify with customer before finalizing]")

        entry = {
            "aws_li_key":       r["aws_li_key"],
            "gcp_service":      service,
            "gcp_sku_name":     sku_name,
            "component":        "hourly",
            "strategy":         "map",
            "unit_multiplier":  mult,
            "gcp_region":       gcp_region,
            "projection_note":  f"flat_hourly lookup → {sku_name}{caveat}{review_note}",
            "mapping_confidence": confidence,
        }
        if sku_id:
            entry["gcp_sku_id"] = sku_id
            entry["gcp_sku_unit"] = sku_id.unit
        out.append(entry)
    return out


def map_per_request(rows):
    out = []
    for r in rows:
        product = (r.get("product") or "").lower()
        gcp_region = r.get("gcp_region")

        # Fargate vCPU-Hours / GB-Hours → GKE Autopilot pod resources.
        # Fargate is per-task (vCPU + memory billed separately). GKE Autopilot is the
        # closest equivalent: per-pod mCPU-hour + GiBy.h (no cluster management fee).
        # unit_multiplier=1000 converts vCPU → mCPU (GKE Autopilot bills in mCPU).
        ut_lower = (r.get("usage_type") or "").lower()
        # PDF bills embed usage info in product (e.g. "... APS3-Fargate-GB-Hours");
        # CUR bills put it in usage_type. Combine both for detection.
        fargate_blob = product + " " + ut_lower
        if "fargate" in fargate_blob:
            is_arm = "arm" in fargate_blob
            is_memory = "gb-hours" in fargate_blob or "memory" in fargate_blob
            component = "memory" if is_memory else "vcpu"

            # GKE Autopilot has FOUR real compute classes, each separately
            # priced (confirmed via find-sku.sh): General Purpose, Balanced,
            # Scale-Out x86, and Scale-Out Arm. The old code hardcoded a
            # single class ("Autopilot Pod mCPU Requests" for x86, "Autopilot
            # Arm Pod CPU" for arm) and never compared against the others —
            # so even after adding the self-managed-Compute-Engine disclosure,
            # ECS rows still weren't getting the "sweep every real GCP option"
            # treatment the rest of this codebase applies. Fixed to sweep all
            # real classes for the row's own architecture and pick whichever
            # is genuinely cheapest per-region.
            #
            # CRITICAL unit-mismatch bug found while building this sweep:
            # "Autopilot Arm Pod CPU" (the OLD hardcoded ARM target) has a
            # DIFFERENT real billing granularity than every other class.
            # Checked the raw catalog SKU JSON directly (pricingExpression.
            # displayQuantity): every "...Pod mCPU Requests"-named class
            # (General Purpose x86, Balanced, Scale-Out x86, Scale-Out Arm)
            # is displayQuantity=1000 (price is per 1000 mCPU = per vCPU,
            # confirmed empirically consistent with this pipeline's existing
            # mult=1000 vCPU→mCPU conversion). "Autopilot Arm Pod CPU" is
            # displayQuantity=1 (its listed rate, $0.03216/h in one region, is
            # ALREADY a flat per-vCPU-hour price) — applying the same
            # mult=1000 to it would have silently produced a ~1000x overprice
            # ($32.16/vCPU-hr — implausible on its face, more than any GPU
            # instance rate) for any real ARM Fargate row in a region where
            # this specific SKU resolves (us-central1, us-east1, europe-west4,
            # us-west1, us-east7, asia-southeast4 all have it). Never caught
            # before now because no job audited today happened to have an ARM
            # Fargate row in one of those specific regions — pure luck, not
            # correctness. Fixed by excluding this one mismatched-unit class
            # from the sweep entirely (Scale-Out Arm remains as the correctly-
            # metered native-ARM option) rather than risk reintroducing this
            # bug via a per-candidate mult that's easy to get wrong again.
            #
            # All memory-side SKUs (checked the same way) are consistently
            # displayQuantity=1 regardless of class or architecture, so
            # mult=1.0 for memory is correct across the board — no equivalent
            # exclusion needed there.
            _AUTOPILOT_CPU_CLASSES = [
                ("General Purpose", "Autopilot Pod mCPU Requests", ("x86",)),
                ("Balanced", "Autopilot Balanced Pod mCPU Requests", ("x86",)),
                ("Scale-Out x86", "Autopilot Scale-Out x86 Pod mCPU Requests", ("x86",)),
                ("Scale-Out Arm", "Autopilot Scale-Out Arm Pod mCPU Requests", ("arm",)),
            ]
            _AUTOPILOT_MEM_CLASSES = [
                ("General Purpose", "Autopilot Pod Memory Requests", ("x86",)),
                ("Balanced", "Autopilot Balanced Pod Memory Requests", ("x86",)),
                ("Scale-Out x86", "Autopilot Scale-Out x86 Pod Memory Requests", ("x86",)),
                ("General Purpose Arm", "Autopilot Arm Pod Memory", ("arm",)),
                ("Scale-Out Arm", "Autopilot Scale-Out Arm Pod Memory Requests", ("arm",)),
            ]
            classes = _AUTOPILOT_MEM_CLASSES if is_memory else _AUTOPILOT_CPU_CLASSES
            mult = 1.0 / 1.024 if is_memory else 1000.0  # AWS GB-Hours → GCP GiBy.h (1 GiB = 1.024 GB)
            want_arch = "arm" if is_arm else "x86"

            best_label, best_desc, best_rate = None, None, None
            for label, desc, archs in classes:
                if want_arch not in archs:
                    continue
                rate = _family_hourly_rate("Kubernetes Engine", desc, gcp_region)
                if rate is None:
                    continue
                if best_rate is None or rate < best_rate:
                    best_label, best_desc, best_rate = label, desc, rate

            if best_desc:
                autopilot_desc = best_desc
                # _strict_resolve_sku, not resolve_sku — best_desc was chosen by
                # a strict exact-region _family_hourly_rate() sweep above; a
                # plain resolve_sku() could still continent-fallback to a
                # different region's SKU/price for this same class.
                autopilot_sku_id = _strict_resolve_sku("Kubernetes Engine", best_desc, gcp_region)
                fallback_note = ""
            elif is_arm:
                # No real ARM class available in this region at all — fall
                # back to the cheapest x86 class instead (same "try native,
                # fall back" pattern used everywhere else for this crossing).
                for label, desc, archs in classes:
                    if "x86" not in archs:
                        continue
                    rate = _family_hourly_rate("Kubernetes Engine", desc, gcp_region)
                    if rate is None:
                        continue
                    if best_rate is None or rate < best_rate:
                        best_label, best_desc, best_rate = label, desc, rate
                autopilot_desc = best_desc
                autopilot_sku_id = _strict_resolve_sku("Kubernetes Engine", best_desc, gcp_region) if best_desc else None
                fallback_note = (f" (no Arm Autopilot class available in {gcp_region} — x86 "
                                 f"{best_label or ''} pricing used instead; real cost may differ)")
            else:
                autopilot_desc = None
                autopilot_sku_id = None
                fallback_note = ""

            # GKE Autopilot's rate is per-mCPU (component="vcpu") or per-GiBy
            # (component="memory"); `mult` (1000 for vcpu, 1 for memory) is
            # the SAME conversion already used to turn AWS's vCPU quantity
            # into Autopilot's mCPU billing unit — applying it here puts
            # Autopilot's rate into $/real-vCPU-hr or $/real-GiB-hr, the same
            # unit Cloud Run and Compute Engine are naturally priced in, so
            # all three can be compared on equal footing below.
            autopilot_rate = None
            if autopilot_sku_id:
                _raw = _family_hourly_rate("Kubernetes Engine", autopilot_desc, gcp_region)
                autopilot_rate = _raw * mult if _raw is not None else None

            # Cloud Run Services (instance-based billing, always-on) — the
            # OTHER real fully-managed, no-node-management GCP target for
            # ECS Fargate (confirmed real gap: this function only ever
            # considered GKE Autopilot, never compared it against Cloud Run,
            # even though both are the same operational tier — Google runs
            # the compute either way, no cluster/node to manage — so per the
            # Performance-Tier Safety Rule this comparison is always safe to
            # auto-pick the cheaper of the two, unlike the Compute Engine
            # comparison below which DOES cross into self-managed and must
            # stay disclosure-only). Cloud Run has no separate ARM SKU
            # tier — same rate applies regardless of the AWS source's
            # architecture. Catalog rate is $/vCPU-second or $/GiB-second
            # (not $/hr like every other candidate here), hence the *3600.
            # cr_desc is the regex pattern for resolve_sku(); cr_display_desc
            # is the clean, real catalog text (no escaping) for the
            # customer-facing gcp_sku_name/note — same convention as
            # cheapest_gpu_in_scope()'s gpu_desc/gpu_display_desc split.
            is_vcpu = component == "vcpu"
            cr_desc = (r"Services CPU \(Instance-based billing\)" if is_vcpu
                       else r"Services Memory \(Instance-based billing\)")
            cr_display_desc = ("Services CPU (Instance-based billing)" if is_vcpu
                                else "Services Memory (Instance-based billing)")
            cr_rate = None
            cr_sku_id = _strict_resolve_sku("Cloud Run", cr_desc, gcp_region)
            if cr_sku_id:
                _cr_raw = _family_hourly_rate("Cloud Run", cr_desc, gcp_region)
                cr_rate = _cr_raw * 3600 if _cr_raw is not None else None

            # Pick the cheaper of whichever managed candidates actually
            # priced in this exact region — never picks Compute Engine here,
            # that stays a disclosed-only option below.
            managed_candidates = []
            if autopilot_rate is not None and autopilot_sku_id:
                managed_candidates.append(
                    ("Kubernetes Engine", autopilot_desc, autopilot_sku_id, autopilot_rate, mult,
                     f"GKE Autopilot ({best_label})"))
            if cr_rate is not None and cr_sku_id:
                managed_candidates.append(
                    ("Cloud Run", cr_display_desc, cr_sku_id, cr_rate, 1.0,
                     "Cloud Run Services (always-on, instance-based billing)"))
            managed_candidates.sort(key=lambda c: c[3])

            if managed_candidates:
                gcp_service, sku_desc, sku_id, chosen_rate, mult, chosen_label = managed_candidates[0]
                platform_note = ""
                if len(managed_candidates) > 1:
                    _, _, _, other_rate, _, other_label = managed_candidates[1]
                    if other_rate > chosen_rate:
                        pct = round((1 - chosen_rate / other_rate) * 100)
                        platform_note = (f" [cost-tier: {chosen_label} chosen over {other_label} — "
                                         f"~{pct}% cheaper here, same operational tier — Google runs the "
                                         "compute either way, no cluster/node to manage on either side]")

                # GKE Autopilot/Cloud Run's managed-platform premium over raw
                # self-managed Compute Engine was never disclosed as a cost
                # tradeoff before — confirmed real, substantial gap via real
                # catalog rates: Autopilot Pod mCPU Requests ($0.0534/vCPU-hr
                # in asia-south1) is itself ~25% pricier than AWS Fargate
                # ($0.04256/vCPU-hr), and raw Compute Engine (N2D AMD,
                # $0.018151/vCPU-hr) is under half of AWS Fargate's own rate.
                # A bare Compute Engine VM or GKE Standard node pool is
                # self-managed — a real operational tradeoff, disclosed here
                # rather than silently switched to, since a customer who
                # chose Fargate may have specifically wanted to avoid
                # managing nodes.
                ce_arch = ("arm",) if is_arm else ("x86",)
                ce_default = "C4A Arm" if is_arm else "N2D AMD"
                vcpu_arg, ram_arg = (1.0, 0.0) if component == "vcpu" else (0.0, 1.0)
                ce_label, ce_core, ce_ram, _, _ = cheapest_in_scope(
                    ce_default, vcpu_arg, ram_arg, gcp_region, archs=ce_arch, tiers=("sustained",),
                    workloads=_arm_workloads() if is_arm else ("general",))
                ce_desc = ce_core if component == "vcpu" else ce_ram
                ce_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ce_desc, gcp_region) if ce_desc else None
                self_managed_note = ""
                if ce_rate is not None and ce_rate < chosen_rate:
                    pct = round((1 - ce_rate / chosen_rate) * 100)
                    self_managed_note = (f" [architecture review recommended: self-managed GKE Standard "
                                         f"node pools ({ce_label}, ~${ce_rate:.5f} vs {chosen_label}'s "
                                         f"~${chosen_rate:.5f} per unit here) would be ~{pct}% cheaper "
                                         "for this component, but shifts node provisioning/scaling/patching "
                                         "onto the customer — not switched automatically since Fargate's "
                                         "own appeal is avoiding that; confirm with customer whether "
                                         "self-managed nodes are acceptable]")
                note = f"Fargate → {chosen_label}; {sku_desc} (unit_multiplier={mult}){fallback_note}{platform_note}{self_managed_note}"
                confidence = 0.78 if not fallback_note else 0.6
            else:
                # Neither Autopilot SKU (Arm or x86) resolved anywhere — not even
                # via resolve_sku's own live-API fallback. Last resort: price it
                # as a raw, self-managed Compute Engine VM instead of giving up.
                # Compute Engine core/RAM rates are the most universally-available
                # SKUs in the entire catalog — they exist in nearly every region,
                # unlike Autopilot's narrower SKU set — so this is a genuinely
                # useful fallback rather than an unpriced passthrough.
                #
                # UNLIKE the ARM->x86 Autopilot swap above, this is NOT a quiet
                # same-tier cost-tier note: Autopilot is a managed product (Google
                # runs node provisioning/scaling); a bare Compute Engine VM is
                # self-managed (the customer would run their own Kubernetes/
                # container layer on it). That's a real operational-model change,
                # not just a cheaper price for the same guarantee — per this
                # project's performance-tier rule (CLAUDE.md §8), a cross-tier
                # substitution like this must always be flagged loudly, never
                # silently swapped in the way a same-tier swap is.
                gce_desc = "C4A Arm Instance Core" if (is_arm and component == "vcpu") else \
                           "C4A Arm Instance Ram" if is_arm else \
                           None
                # _strict_resolve_sku (not resolve_sku) — a plain resolve_sku()
                # call here would go through lookup_sku_in_catalog()'s continent
                # fallback and could silently substitute a different region's
                # C4A Arm SKU/price instead of correctly falling through to the
                # N2D AMD branch below (same bug class as the Delhi->Taiwan
                # case fixed elsewhere in this file).
                fallback_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, gce_desc, gcp_region) if is_arm else None
                if not fallback_sku:
                    # C4A ARM either isn't the source architecture or isn't sold
                    # in this region (confirmed to happen for real — C4A is
                    # unavailable in asia-south2/Delhi in this catalog, same gap
                    # every other ARM mapper in this file already falls back
                    # from). Chain into the same sustained-family comparison
                    # used everywhere else rather than dead-ending here.
                    vcpu_arg, ram_arg = (1.0, 0.0) if component == "vcpu" else (0.0, 1.0)
                    _label, core_desc, ram_desc, _sw, _reason = cheapest_in_scope(
                        "N2D AMD", vcpu_arg, ram_arg, gcp_region, archs=("x86",), tiers=("sustained",))
                    gce_desc = core_desc if component == "vcpu" else ram_desc
                    fallback_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, gce_desc, gcp_region)

                if fallback_sku:
                    gcp_service = GCP_COMPUTE_ENGINE
                    sku_id = fallback_sku
                    sku_desc = gce_desc
                    mult = 1.0 / 1.024 if is_memory else 1.0  # AWS GB-Hours → GCP GiBy.h; vCPU-hours match 1:1
                    note = (f"⚠ SERVICE MODEL CHANGE: no GKE Autopilot pod pricing (Arm or x86) available "
                            f"in {gcp_region} — falling back to self-managed Compute Engine {gce_desc} as "
                            f"the nearest priced equivalent. This is NOT the same operational model as "
                            f"Fargate/Autopilot (no managed node provisioning/autoscaling) — verify this "
                            f"fits before finalizing.")
                    confidence = 0.45
                else:
                    # Be explicit that this is an UNPRICED passthrough, not a real
                    # GKE Autopilot cost estimate — the previous note here read
                    # "Fargate -> GKE Autopilot pod capacity" regardless of whether
                    # resolve_sku actually found a rate, which misrepresented a raw
                    # AWS-cost carry-forward as if it were a priced GCP estimate.
                    note = (f"Fargate → GKE Autopilot: no Autopilot pod SKU (Arm or x86) found in "
                            f"{gcp_region}, and no Compute Engine fallback rate either — AWS cost "
                            f"carried through unpriced, NOT a GCP cost estimate")
                    confidence = 0.3

            strategy = "map" if sku_id else "passthrough"
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        gcp_service,
                "gcp_sku_id":         sku_id,
                "gcp_sku_name":       sku_desc if sku_id else None,
                "component":          component,
                "strategy":           strategy,
                "unit_multiplier":    mult,
                "gcp_region":         gcp_region,
                "projection_note":    note,
                "mapping_confidence": confidence,
            })
            continue

        # S3 per-request charges → GCS Class A / Class B Operations.
        # S3 Tier 1 (PUT/COPY/POST/LIST) = $0.005/1,000 ≈ GCS Class A $0.005/1,000
        # S3 Tier 2 (GET/HEAD/others)    = $0.004/10,000 ≈ GCS Class B $0.004/10,000
        # Unit is raw request count on both sides; unit_multiplier=1.0.
        # Lifecycle, replication, and bucket-level charges have no GCS equivalent — passthrough.
        # NB: do NOT use bare "s3" — it false-matches region codes like "APS3"
        # in a product name (same lesson already documented in outlier_gate.py's
        # _STORAGE_OK). Confirmed real: an SQS row ("Amazon Simple Queue Service
        # APS3-Requests-Tier1") was silently misrouted here and priced as GCS
        # Class A Operations instead of Pub/Sub, because "APS3" contains "s3".
        # AWS's real S3 product name always contains "simple storage" anyway,
        # so the bare "s3" check added collision risk with no real coverage gain.
        if "simple storage" in product:
            ut = (r.get("usage_type") or "").lower()
            op = (r.get("operation") or "").lower()
            blob = ut + " " + op

            if "storagelens" in blob or "generalpurposebuckets" in blob:
                # Storage Lens and bucket-count fees: $0 on GCP
                strat, sku_desc, note, conf = (
                    "ignore", None,
                    "S3 Storage Lens / bucket fee — no GCS equivalent; $0 on GCP", 0.90
                )
            elif re.search(r"lifecycle|transition|replicat|replication", blob):
                # Lifecycle transitions and replication requests: no unit-compatible GCS SKU
                strat, sku_desc, note, conf = (
                    "passthrough", None,
                    "S3 lifecycle/replication request — no GCS equivalent; passthrough at cost parity", 0.45
                )
            elif re.search(r"tier1|put|copy|post|list", blob):
                # Tier 1 (write) → GCS Class A Operations (same per-1000 price)
                strat, sku_desc, note, conf = (
                    "map", "Regional Standard Class A Operations",
                    "S3 Tier1 requests → GCS Class A Operations ($0.005/1k, parity)", 0.88
                )
            elif re.search(r"tier2|get|head|select", blob):
                # Tier 2 (read) → GCS Class B Operations (same per-10k price)
                strat, sku_desc, note, conf = (
                    "map", "Regional Standard Class B Operations",
                    "S3 Tier2 requests → GCS Class B Operations ($0.004/10k, parity)", 0.88
                )
            else:
                strat, sku_desc, note, conf = (
                    "passthrough", None,
                    "S3 per-request charge — tier unknown; passthrough at cost parity", 0.40
                )

            sku_id = resolve_sku(GCP_CLOUD_STORAGE, sku_desc, gcp_region) if sku_desc else None
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_CLOUD_STORAGE,
                "gcp_sku_id":         sku_id if sku_id else None,
                "gcp_sku_name":       sku_desc,
                "component":          "requests",
                "strategy":           strat if sku_id or strat != "map" else "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    note,
                "mapping_confidence": conf,
            })
            continue
        else:
            match = _match(r.get("usage_type"), r.get("product"), PER_REQUEST_MAP)
            if match:
                service, sku_name, mult = match
                strategy, confidence = "map", 0.85
                note = f"per_request lookup → {sku_name}"
                if service == "Cloud Armor":
                    # AWS WAF's Bot Control and Fraud Control managed rule
                    # groups are billed as their own, separately-named CUR
                    # line items (confirmed real AWS billing behavior, not
                    # workload-intent guessing — this is CUR-derivable infra
                    # evidence per this project's own evidence-vs-intent
                    # philosophy). Cloud Armor's equivalent bot/fraud
                    # protection lives only in the Enterprise tier, which
                    # carries a substantial fixed cost on top ($200-3000/mo
                    # enrollment, confirmed via find-sku.sh) that Standard
                    # doesn't have. Never silently assume either tier —
                    # Standard would under-price a customer who genuinely
                    # needs these features; Enterprise would over-price one
                    # who doesn't. Disclose only when the real evidence
                    # (the AWS bill mentioning these specific AWS feature
                    # names) is actually present.
                    waf_blob = f"{r.get('product') or ''} {r.get('usage_type') or ''} {r.get('operation') or ''}".lower()
                    if re.search(r"botcontrol|bot control|fraudcontrol|fraud control", waf_blob):
                        note += (" [architecture review recommended: this bill shows AWS WAF Bot "
                                 "Control/Fraud Control usage — the equivalent Cloud Armor bot/fraud "
                                 "protection requires the Enterprise tier, which carries a substantial "
                                 "fixed enrollment cost ($200-3000/mo) not modeled here; verify actual "
                                 "tier requirements with customer before finalizing]")
                        confidence = 0.5
            else:
                # No known GCP equivalent — passthrough at cost parity rather than
                # silently mapping to Cloud Run (which would be wrong for most unmatched
                # services like GuardDuty, Security Hub, unrecognized ML services, etc.).
                service, sku_name, mult = None, None, 1.0
                strategy, confidence = "passthrough", 0.40
                note = (f"per_request: no GCP equivalent found for "
                        f"product={r.get('product')!r} — passthrough at cost parity")
        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        service,
            "gcp_sku_name":       sku_name,
            "component":          "requests",
            "strategy":           strategy,
            "unit_multiplier":    mult,
            "gcp_region":         r.get("gcp_region"),
            "projection_note":    note,
            "mapping_confidence": confidence,
        })
    return out


def map_block_storage(rows):
    """EBS volumes → Persistent Disk; RDS/managed-db storage → Cloud SQL storage."""
    out = []
    for r in rows:
        product = (r.get("product") or "").lower()
        ut = (r.get("usage_type") or "").lower()
        op = (r.get("operation") or "").lower()
        vol = (r.get("volume_type") or "").lower().strip()
        gcp_region = r.get("gcp_region")

        # Infer gcp_region from usage_type prefix when ingest left it as 'global'/blank.
        # PDF/flat-CSV bills omit the aws_region column; the usage_type prefix encodes it
        # (e.g. "APS3-EBS:VolumeUsage.gp3" → asia-south1 / Mumbai).
        if not gcp_region or gcp_region == "global":
            m = _UT_PREFIX_RE.match(r.get("usage_type") or "")
            if m:
                gcp_region = _UT_PREFIX_TO_GCP.get(m.group(1).lower(), gcp_region)

        # Safety guard: RDS InstanceUsage and RDS Proxy hour rows end up in block_storage
        # when pricing_unit is blank (managed_db rule requires a unit in ('Hrs','hours',...)).
        # Pricing them as storage (total_usage_hours × $/GiBy.mo) produces 10-20x errors.
        # Detect and passthrough these so they're at least honest rather than wildly wrong.
        is_managed_db_hours = (
            re.search(r"instanceusage|rds:proxy", ut, re.IGNORECASE)
            and any(k in product for k in ("rds", "relational", "aurora"))
        )
        if is_managed_db_hours:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud SQL",
                "gcp_sku_name":       None,
                "component":          "storage",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    ("RDS instance/proxy hour row in block_storage (blank pricing_unit) — "
                                       "would mis-price as $/GiBy.mo; passthrough at cost parity"),
                "mapping_confidence": 0.65,
            })
            continue
        # PDF/summary bills leave the product/volumetype column blank, so fall
        # back to inferring the volume type and snapshot flag from the free text
        # (usage_type + operation + product description).
        blob = f"{ut} {op} {product}"
        if not vol:
            mvol = re.search(r'\b(gp3|gp2|io2|io1|st1|sc1|standard|magnetic)\b', blob)
            if mvol:
                vol = mvol.group(1)
        is_snapshot = ("snapshot" in ut) or ("snapshot" in op) or ("snapshot" in blob)
        # gp3/io2 provisioned-IOPS and throughput (MiBps) fees have no separate
        # GCP charge — pd-balanced bundles performance with capacity. Pricing
        # them per-GB against a capacity SKU produced nonsense rows; drop them
        # with an explicit note instead.
        # VolumeP-IOPS usage_type signals a provisioned-IOPS row in the billing API.
        # The IOPS row's pricing_unit may be "GB-Mo" (not "IOPS-Mo") in PDF/flat CSV
        # bills, so we need to detect it from the usage_type pattern.
        # RDS's own native identifiers for this charge are "StorageIOPS" /
        # "Storage IOPS - million I/O requests" — neither contains "provisioned"
        # or "iops-mo", so the original regex (written for EBS's VolumeP-IOPS
        # phrasing) silently missed every RDS-native IOPS row, letting it fall
        # through to a generic storage-capacity mapping that mis-prices it.
        # Small-dollar rows like this never cross outlier_gate.py's hard
        # thresholds (ratio>50x AND >$25, or abs>$10k), so nothing downstream
        # caught it either — this regex is the only real gate for this class
        # of row and it has to match every phrasing AWS actually uses.
        # Aurora's own native identifier for this same per-I/O-request charge is
        # "StorageIOUsage" (distinct from RDS's "StorageIOPS") — confirmed still
        # missing a real customer bill (Aquilai.io, APS5-Aurora:StorageIOUsage,
        # $39.81 AWS row silently priced as $0.18 GB-mo of SSD storage capacity
        # instead of being ignored like every other RDS/Aurora per-IO variant).
        # "provisioned.?iops" is meant to catch genuine IOPS-RATE billing phrases
        # ("provisioned IOPS-month of gp3", "VolumeP-IOPS.piops") — but io1/io2's
        # own AWS product name is literally "Provisioned IOPS SSD", so a plain
        # CAPACITY row ("GB-month of Provisioned IOPS SSD (io1) provisioned
        # storage") false-matched and got misrouted to the IOPS-rate branch,
        # pricing a capacity charge as a passthrough IOPS fee instead of the
        # correct capacity SKU. Confirmed real: an io1 GB-month row in a PDF
        # bill hit this exact false match. "...IOPS SSD" is always the volume
        # type's product name, never how a genuine rate charge is phrased, so
        # excluding that specific sequence disambiguates safely.
        #
        # Same bug class, different volume type: st1's own AWS product name is
        # "Throughput Optimized HDD" — the bare "throughput" check below would
        # equally false-match a plain st1 CAPACITY row ("GB-month of Throughput
        # Optimized HDD (st1) provisioned storage") and misroute it to the
        # IOPS/throughput branch instead of Standard PD capacity. "Throughput
        # Optimized HDD" is always the volume type's product name, never how a
        # genuine throughput-RATE charge is phrased (those say "provisioned
        # throughput" or "MiBps"), so exclude that specific sequence too.
        is_iops_or_throughput = re.search(
            r"iops-mo|provisioned.?iops(?!\s*ssd)|storageiops|storage.?iops|storageiousage|"
            r"mibps|throughput(?!\s*optimized)|volumep.iops|volumep-iops|million i.?o requests",
            blob, re.IGNORECASE)
        if is_iops_or_throughput:
            is_managed_db = any(k in product for k in
                                ("rds", "relational", "aurora", "documentdb", "memorydb", "elasticache"))
            gp3_like = "gp3" in blob.lower() or "gp2" in blob.lower()
            
            if is_managed_db:
                out.append({
                    "aws_li_key":       r["aws_li_key"],
                    "gcp_service":      GCP_CLOUD_SQL,
                    "gcp_sku_name":     None,
                    "component":        "storage",
                    "strategy":         "ignore",
                    "unit_multiplier":  1.0,
                    "gcp_region":       gcp_region,
                    "projection_note":  "RDS Provisioned IOPS/throughput fee — performance scaling is included in Cloud SQL storage tier",
                    "mapping_confidence": 0.90,
                })
            else:
                # gp3 provisioned IOPS: map to Hyperdisk Balanced IOPS (same unit: IOPS-month).
                # Rate parity: AWS $0.006/IOPS-mo ≈ GCP $0.005–0.008/IOPS-mo by region.
                # Previously ignored on the assumption pd-balanced bundles IOPS — Hyperdisk
                # Balanced is the gp3 equivalent and *does* charge IOPS separately.
                is_iops = re.search(r"iops", blob, re.IGNORECASE) and not re.search(r"mibps|throughput", blob, re.IGNORECASE)
                # Hyperdisk Balanced has a real, published absolute ceiling regardless
                # of disk capacity (160,000 IOPS / 2,400 MiB/s) — a provisioned value
                # above the ceiling for a SINGLE volume is escalated to Hyperdisk
                # Extreme (already the target used for io1/io2 below) instead of
                # silently under-provisioning a real performance shortfall.
                #
                # This ceiling check must never fire for gp3, though: AWS itself caps
                # gp3 provisioned IOPS at 16,000 per volume (a hard AWS limit, well
                # under Hyperdisk Balanced's 160,000 per-volume ceiling), so a real
                # single gp3 volume can never legitimately need Extreme. `total_usage`
                # here is IOPS-Mo — this pipeline ingests flat/deduplicated Cost
                # Explorer exports, not per-resource CUR, so a >160,000 total is an
                # ACCOUNT-WIDE SUM across many volumes, not one disk's rate. Comparing
                # that aggregate against a per-disk ceiling was a category error —
                # confirmed real (a genuine account total of 227,326 IOPS-Mo escalated
                # a linear, correctly-priceable gp3 aggregate to a passthrough-priced
                # Extreme SKU, ~14x more expensive than the real Hyperdisk Balanced
                # rate for the same total IOPS). Hyperdisk Balanced's rate is linear
                # per IOPS regardless of how many disks the total spans, so gp3 always
                # prices correctly against it. io1/io2 (AWS max 64,000/volume, still
                # under Balanced's ceiling) use a separate existing Extreme path below
                # and are unaffected by this gp3-only exclusion.
                _HYPERDISK_BALANCED_MAX_IOPS = 160000
                _HYPERDISK_BALANCED_MAX_MIBPS = 2400
                provisioned = r.get("total_usage") or 0
                if (not gp3_like) and is_iops and provisioned > _HYPERDISK_BALANCED_MAX_IOPS:
                    out.append({
                        "aws_li_key":       r["aws_li_key"],
                        "gcp_service":      GCP_COMPUTE_ENGINE,
                        "gcp_sku_name":     "Extreme PD IOPS",
                        "component":        "iops",
                        "strategy":         "passthrough",
                        "unit_multiplier":  1.0,
                        "gcp_region":       gcp_region,
                        "projection_note":  (f"EBS gp3 provisioned IOPS ({provisioned:.0f}) exceeds Hyperdisk "
                                             f"Balanced's published ceiling ({_HYPERDISK_BALANCED_MAX_IOPS} IOPS) — "
                                             "escalated to Hyperdisk Extreme rather than silently under-provisioning; "
                                             "passthrough at cost parity (no direct Extreme IOPS rate match)"),
                        "mapping_confidence": 0.55,
                    })
                elif gp3_like and is_iops:
                    iops_sku = resolve_sku(GCP_COMPUTE_ENGINE,
                                          "Hyperdisk Balanced IOPS", gcp_region)
                    out.append({
                        "aws_li_key":       r["aws_li_key"],
                        "gcp_service":      GCP_COMPUTE_ENGINE,
                        "gcp_sku_id":       iops_sku,
                        "gcp_sku_name":     "Hyperdisk Balanced IOPS",
                        "component":        "iops",
                        "strategy":         "map" if iops_sku else "passthrough",
                        "unit_multiplier":  1.0,
                        "gcp_region":       gcp_region,
                        "projection_note":  ("EBS gp3 provisioned IOPS → Hyperdisk Balanced IOPS "
                                             f"($0.006/IOPS-mo parity); region={gcp_region}"),
                        "mapping_confidence": 0.88,
                    })
                elif gp3_like and provisioned * 1024.0 > _HYPERDISK_BALANCED_MAX_MIBPS:
                    out.append({
                        "aws_li_key":       r["aws_li_key"],
                        "gcp_service":      GCP_COMPUTE_ENGINE,
                        "gcp_sku_name":     "Extreme PD Throughput",
                        "component":        "throughput",
                        "strategy":         "passthrough",
                        "unit_multiplier":  1024.0,
                        "gcp_region":       gcp_region,
                        "projection_note":  (f"EBS gp3 provisioned throughput ({provisioned:.0f} MiBps) exceeds "
                                             f"Hyperdisk Balanced's published ceiling ({_HYPERDISK_BALANCED_MAX_MIBPS} MiBps) — "
                                             "escalated to Hyperdisk Extreme rather than silently under-provisioning; "
                                             "passthrough at cost parity (no direct Extreme throughput rate match)"),
                        "mapping_confidence": 0.55,
                    })
                elif gp3_like:
                    # gp3 provisioned throughput → Hyperdisk Balanced Throughput.
                    # AWS CUR stores throughput in GiBps-month (total_usage=1.038 for
                    # 1.038 GiBps-month). GCP bills at $/MiBps-month. The multiplier
                    # must be 1024 (GiBps→MiBps) so that:
                    #   1.038 GiBps × 1024 × $0.049/MiBps = $52.08  (vs AWS $51.02)
                    # unit_multiplier=1.0 would compute 1.038 × $0.049 = $0.05 — wrong.
                    tp_sku = resolve_sku(GCP_COMPUTE_ENGINE,
                                        "Hyperdisk Balanced Throughput", gcp_region)
                    out.append({
                        "aws_li_key":       r["aws_li_key"],
                        "gcp_service":      GCP_COMPUTE_ENGINE,
                        "gcp_sku_id":       tp_sku,
                        "gcp_sku_name":     "Hyperdisk Balanced Throughput",
                        "component":        "throughput",
                        "strategy":         "map" if tp_sku else "passthrough",
                        "unit_multiplier":  1024.0,
                        "gcp_region":       gcp_region,
                        "projection_note":  ("EBS gp3 provisioned throughput → Hyperdisk Balanced Throughput "
                                             "(AWS CUR stores GiBps-month; ×1024 converts to MiBps-month for GCP rate)"),
                        "mapping_confidence": 0.92,
                    })
                else:
                    # io1/io2 provisioned IOPS/throughput: no direct Hyperdisk Extreme rate match
                    out.append({
                        "aws_li_key":       r["aws_li_key"],
                        "gcp_service":      GCP_COMPUTE_ENGINE,
                        "gcp_sku_name":     "Extreme PD IOPS",
                        "component":        "storage",
                        "strategy":         "passthrough",
                        "unit_multiplier":  1.0,
                        "gcp_region":       gcp_region,
                        "projection_note":  "EBS io1/io2 provisioned IOPS/throughput fee — maps to PD Extreme; passthrough at cost parity",
                        "mapping_confidence": 0.60,
                    })
            continue
        is_managed_db = any(k in product for k in
                            ("rds", "relational", "aurora", "documentdb", "memorydb", "elasticache"))

        # Aurora/RDS per-I/O request charges: billed as "N million I/O requests".
        # Cloud SQL includes I/O in the storage price — no separate per-I/O charge.
        # These rows MUST be ignored: total_usage is an I/O count (e.g. 22,477,180
        # IOs), but storage SKU rates are in $/GiBy.mo — multiplying them inflates
        # cost by >1,000,000x (22M IOs × $0.41/GiBy.mo = $9M from a $5 AWS row).
        if is_managed_db and re.search(r"i/o request|million i/o|million io|\bio request", blob, re.IGNORECASE):
            out.append({
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      GCP_CLOUD_SQL,
                "gcp_sku_name":     None,
                "component":        "storage",
                "strategy":         "ignore",
                "unit_multiplier":  1.0,
                "gcp_region":       gcp_region,
                "projection_note":  "Aurora/RDS per-I/O request fee — Cloud SQL storage pricing includes I/O; no separate per-I/O charge on GCP",
                "mapping_confidence": 0.95,
            })
            continue

        if is_managed_db:
            service = GCP_CLOUD_SQL
            if "backup" in ut or "backup" in op:
                tier_desc = "Backups"
            elif vol in ("st1", "sc1", "standard", "magnetic"):
                # "HDD storage" never matches any real Cloud SQL SKU name at
                # all (confirmed via find-sku.sh — Cloud SQL storage tiers
                # are literally named "Standard storage"/"Low cost storage",
                # not "SSD"/"HDD") — every non-backup managed-DB storage row
                # was silently falling through resolve_sku()'s exact-pattern
                # match and landing on apply_rates.py's word-overlap fallback
                # instead, the same fragile fallback path that already
                # produced the confirmed Backups mis-price bug (a real
                # storage row priced against an unrelated compute SKU). The
                # fallback happens to score correctly most of the time for
                # storage specifically (shared "storage"/tier words usually
                # beat vCPU/RAM SKUs), but it's unverified and one catalog
                # change away from silently reproducing that bug for the
                # majority of RDS/Aurora storage rows.
                tier_desc = "Low cost storage"
            else:
                # gp2/gp3 SSD → Hyperdisk Balanced Capacity (Cloud SQL Enterprise):
                # newer storage type with guaranteed IOPS+throughput (same model as gp3),
                # and cheaper than legacy Standard storage ($0.161 vs $0.238/GiBy.mo).
                tier_desc = "Enterprise Storage Hyperdisk Balanced Capacity"

            # Engine + Multi-AZ→Regional tier disambiguation (mirrors
            # apply_rates.py::fix_managed_db_storage_rows() — that function only
            # fires when a row was first mis-mapped to a vCPU/RAM SKU; rows that
            # come straight through this primary branch need the same engine/tier
            # logic or resolve_sku() non-deterministically picks whichever Cloud
            # SQL "SSD storage" SKU sorts first for the region (wrong engine
            # and/or a silent HA-tier downgrade, which CLAUDE.md §8 forbids).
            # deployment_option is the real CUR column (mirrors family_mapper.py's
            # map_db_row is_ha check); operation carries the same signal as free
            # text on PDF/flat-CSV bills where deployment_option is blank. Same for
            # database_engine — product is always the generic AWS service name
            # ("Amazon Relational Database Service"), never the engine; the engine
            # only appears in database_engine or the operation free text.
            is_multi_az = ((r.get("deployment_option") or "").strip().lower() == "multi-az"
                           or "multi-az" in ut or "multi-az" in op)
            ha_tier = "Regional" if is_multi_az else "Zonal"
            engine = (r.get("database_engine") or "").strip().lower()
            engine_blob = f"{engine} {op}"
            engine_note = ""
            if tier_desc == "Backups":
                # Real catalog has exactly ONE flat "Cloud SQL: Backups in
                # <region>" SKU (confirmed via find-sku.sh) — no per-engine or
                # per-tier (Zonal/Regional) variant exists at all. Building the
                # desc pattern as "Cloud SQL: Zonal - Backups" (like every
                # other tier_desc here) added a spurious "Zonal -" that never
                # matches any real SKU at the initial resolve_sku() call, so
                # gcp_sku_id stayed NULL and the row fell to the downstream
                # lazy-fill word-overlap resolver (apply_rates.py) — which
                # then picked an unrelated vCPU-hour compute SKU purely
                # because its description ALSO happened to contain "Zonal -",
                # scoring higher than the real (tier-less) Backups SKU on raw
                # word overlap despite missing the actual "backups" concept
                # entirely. Confirmed real: a $26.71 AWS backup-storage row
                # got priced against a $0.0648/h compute rate.
                desc = "Cloud SQL: Backups"
            elif "postgresql" in engine_blob or "aurora" in engine_blob:
                desc = f"Cloud SQL for PostgreSQL: {ha_tier} - {tier_desc}"
            elif "mariadb" in engine_blob:
                desc = f"Cloud SQL for MySQL: {ha_tier} - {tier_desc}"
                engine_note = " [engine substitution: Cloud SQL has no native MariaDB offering; priced as MySQL]"
            elif "mysql" in engine_blob:
                desc = f"Cloud SQL for MySQL: {ha_tier} - {tier_desc}"
            else:
                desc = f"Cloud SQL: {ha_tier} - {tier_desc}"
            note = (f"managed-db storage ({vol or 'default'}) → {desc}"
                    + (" (Multi-AZ→Regional tier)" if is_multi_az else "") + engine_note)
        else:
            service = GCP_COMPUTE_ENGINE
            if is_snapshot:
                # EBS snapshots are instant-restore (volumes clone from them immediately,
                # data streams in background) — GCP Instant Snapshot has the same property.
                # Standard PD Instant Snapshot ($0.044/GiBy.mo) vs Storage PD Snapshot
                # ($0.061/GiBy.mo): cheaper AND better product match.
                desc = "Standard PD Instant Snapshot data storage"
            elif vol in ("io1", "io2") and "block express" not in blob:
                # io1 and standard io2 (non-"Block Express") cap at 64,000
                # IOPS / 1,000 MB/s per volume — both well under Hyperdisk
                # Balanced's published per-volume ceiling (160,000 IOPS /
                # 2,400 MiB/s, the same constant this function's IOPS-fee
                # branch already uses to decide when Balanced can't keep up).
                # The old EBS_VOLUME_MAP hardcoded io1/io2 capacity straight
                # to Extreme PD Capacity ($0.125/GiBy.mo) unconditionally,
                # even though the overwhelming majority of real io1/io2
                # volumes fit safely under Balanced's ceiling and Balanced PD
                # Capacity ($0.10/GiBy.mo) is genuinely cheaper at the same
                # guarantee. Only io2 "Block Express" (AWS's own distinct
                # billing term, up to 256,000 IOPS / 4,000 MB/s) can actually
                # exceed the Balanced ceiling — keep that one case on Extreme.
                # Target is Hyperdisk Balanced Capacity, not classic Balanced
                # PD — a same-region io1/io2 volume under this ceiling can
                # still carry a provisioned-IOPS fee row (priced against
                # Extreme PD IOPS elsewhere in this function); classic
                # Balanced PD has no IOPS SKU of its own, so staying within
                # the Hyperdisk product family keeps capacity+performance on
                # one real, purchasable disk type.
                desc = GCP_HYPERDISK_BALANCED
            else:
                desc = EBS_VOLUME_MAP.get(vol, EBS_DEFAULT_DESC)
            note = f"EBS {vol or 'volume'}{' snapshot' if is_snapshot else ''} → {desc}"
            if vol in ("io1", "io2") and desc == GCP_HYPERDISK_BALANCED:
                note += (" (io1/io2 standard tier caps at 64,000 IOPS/1,000 MB/s, within "
                         "Hyperdisk Balanced's per-volume ceiling — cheaper than Extreme "
                         "at the same performance guarantee)")
            if desc == GCP_HYPERDISK_BALANCED:
                note += (" [Hyperdisk disks require specific compatible machine "
                         "series/zones in GCP, unlike classic Persistent Disk's broader "
                         "attachment support — verify VM type compatibility with customer]")

        sku_id = resolve_sku(service, desc, gcp_region)
        entry = {
            "aws_li_key":       r["aws_li_key"],
            "gcp_service":      service,
            "gcp_sku_name":     desc,
            "component":        "storage",
            "strategy":         "map",
            "unit_multiplier":  1.0,
            "gcp_region":       gcp_region,
            "projection_note":  note,
            "mapping_confidence": 0.90,
        }
        if sku_id:
            entry["gcp_sku_id"] = sku_id
            entry["gcp_sku_unit"] = sku_id.unit
        out.append(entry)
    return out


_CF_BW_RE = re.compile(r'^([A-Z]{2,3})-DataTransfer-Out-Bytes$', re.IGNORECASE)
# CloudFront destination prefix → CDN bucket key in CDN_EGRESS_TIERS.
# Covers all documented CloudFront destination codes; unknown codes fall back to "apac".
_CF_DEST_TO_BUCKET = {
    "US": "americas", "EU": "emea",  "IN": "apac",  "AP": "apac",
    "AU": "apac_au",  "SA": "americas", "ME": "emea", "AF": "emea",
    "CN": "china",
}


def map_data_transfer(rows):
    """Classify transfer direction deterministically and pin the canonical egress
    SKU + rate (no fuzzy catalog matching — see egress_rates.py). Ingress → ignore."""
    # Pre-pass: sum CloudFront bandwidth GB per destination bucket so tier selection
    # uses total monthly volume rather than each individual tier-band row's volume.
    _cf_bucket_totals = {}
    for _r in rows:
        if "cloudfront" not in (_r.get("product") or "").lower():
            continue
        _m = _CF_BW_RE.match(_r.get("usage_type") or "")
        if not _m:
            continue
        _bucket = _CF_DEST_TO_BUCKET.get(_m.group(1).upper(), "apac")
        _cf_bucket_totals[_bucket] = (
            _cf_bucket_totals.get(_bucket, 0.0) + float(_r.get("total_usage") or 0)
        )

    out = []
    for r in rows:
        ut = f"{r.get('usage_type') or ''} {r.get('operation') or ''}".lower()
        product_lower = (r.get("product") or "").lower()

        # CUR bills have "NatGateway-Bytes" / "NatGateway-Hours" in usage_type.
        # Flat-CSV/PDF bills have blank usage_type; the GB/hours signal lives in
        # the operation field. Route bytes → Cloud NAT Data Processing and hours
        # → Private Nat Gateway Uptime, matching the FLAT_HOURLY_MAP entry.
        op_lower = (r.get("operation") or "").lower()
        _nat_bytes = (
            "natgateway-bytes" in ut
            or ("natgateway" in product_lower
                and ("per gb" in op_lower or "data processed" in op_lower
                     or "gb data" in op_lower))
        )
        _nat_hours = (
            "natgateway" in product_lower
            and not _nat_bytes
            and "hour" in op_lower
        )

        if "natgateway" in product_lower and not _nat_bytes and not _nat_hours:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud NAT",
                "gcp_sku_name":       None,
                "component":          "nat",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         r.get("gcp_region"),
                "projection_note":    ("NAT Gateway (PDF bill — usage_type blank, operation "
                                       "gives no GB/hours signal; cannot distinguish "
                                       "gateway-hours from data-processed); passthrough"),
                "mapping_confidence": 0.60,
            })
            continue

        if _nat_hours:
            # Flat-CSV/PDF NatGateway hourly fee → Private Nat Gateway Uptime ($0.045/hr)
            service = "Networking"
            desc_pattern = "Private Nat Gateway Uptime"
            sku_id = resolve_sku(service, desc_pattern, r.get("gcp_region"))
            entry = {
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      "Cloud NAT",
                "gcp_sku_name":     "Private Nat Gateway Uptime",
                "component":        "nat",
                "strategy":         "map",
                "unit_multiplier":  1.0,
                "gcp_region":       r.get("gcp_region"),
                "projection_note":  "NAT Gateway hourly fee → Private NAT Gateway Uptime",
                "mapping_confidence": 0.90,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
            out.append(entry)
            continue

        if _nat_bytes:
            # GCP service is "Networking" in billing catalog (not "Cloud NAT").
            service = "Networking"
            desc_pattern = "Cloud Nat Data Processing"
            sku_id = resolve_sku(service, desc_pattern, r.get("gcp_region"))
            entry = {
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      "Cloud NAT",
                "gcp_sku_name":     "Cloud Nat Data Processing",
                "component":        "transfer",
                "strategy":         "map",
                "unit_multiplier":  1.0,
                "gcp_region":       r.get("gcp_region"),
                "projection_note":  "NAT Gateway processed bytes → Cloud NAT Data Processing",
                "mapping_confidence": 0.90,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
            out.append(entry)
            continue
            
        if "transitgateway-bytes" in ut or "tgw-bytes" in ut:
            out.append({
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      "VPC Network",
                "gcp_sku_name":     None,
                "component":        "transfer",
                "strategy":         "ignore",
                "unit_multiplier":  1.0,
                "gcp_region":       r.get("gcp_region"),
                "projection_note":  "Transit Gateway data processing fee — VPC network spoke traffic has no transit fee on GCP; only standard egress applies",
                "mapping_confidence": 0.90,
            })
            continue

        # "lcu"/"loadbalancer-bytes" catches CUR-format usage_type tokens; PDF bills
        # spell this out in `operation` as "...capacity unit-hour (or partial hour)"
        # with no "lcu" substring anywhere. Without this, the row fell through to
        # the generic egress default below, which priced an LCU-hour COUNT at the
        # per-GB internet-egress rate — a unit mismatch that inflated ALB/NLB LCU
        # rows ~13-15x (classify_mechanics.py routes these here correctly already;
        # this was the second half of that fix that got missed the first time).
        #
        # That earlier fix corrected WHICH $/GB SKU gets targeted but never
        # addressed the deeper unit mismatch: total_usage here is a raw LCU-Hrs
        # COUNT (AWS's own composite meter — new-connections + active-connections
        # + bandwidth + rule-evaluations bundled into ONE normalized unit), not a
        # GB quantity. The real catalog SKU (confirmed via find-sku.sh) is
        # genuinely priced $/GiBy — multiplying a non-GB count by that rate at
        # unit_multiplier=1.0 conflates two different units with no basis for the
        # 1:1 ratio, same bug shape as the confirmed Redshift RI and Hyperdisk
        # Throughput unit-mismatch bugs (see CLAUDE.md history). Per that same
        # principle: since there's no real per-row GB-processed figure to derive
        # an honest multiplier from, this stays an honest passthrough rather than
        # presenting a "map" result with fabricated precision. The sibling
        # map_flat_hourly() ALB/NLB path already does this correctly for the
        # separate flat per-hour LCU forwarding-rule charge, with a disclosed
        # "[estimate: ...]" caveat — mirror that honesty here for the
        # data-processing component too.
        if "lcu" in ut or "loadbalancer-bytes" in ut or "capacity unit-hour" in ut:
            entry = {
                "aws_li_key":       r["aws_li_key"],
                "gcp_service":      "Networking",
                "gcp_sku_id":       None,
                "gcp_sku_name":     None,
                "component":        "transfer",
                "strategy":         "passthrough",
                "unit_multiplier":  1.0,
                "gcp_region":       r.get("gcp_region"),
                "projection_note":  ("Load Balancer Capacity Units (LCU-Hrs) → GCP data processing is billed "
                                     "$/GiBy, but LCU-Hrs is AWS's own composite meter (connections+bandwidth+"
                                     "rule-evals bundled) with no direct GB figure in the bill to convert from — "
                                     "passthrough at AWS cost pending real bandwidth data"),
                "mapping_confidence": 0.50,
            }
            out.append(entry)
            continue

        # CloudFront bandwidth → Cloud CDN Cache Egress.
        # usage_type format: <DEST>-DataTransfer-Out-Bytes (e.g. IN-DataTransfer-Out-Bytes)
        # Tier is selected dynamically from total monthly GB per destination bucket
        # (summed in the pre-pass above) so multiple AWS tier-band rows for the same
        # destination all land in the correct single GCP tier — not hardcoded to any band.
        _cf_m = _CF_BW_RE.match(r.get("usage_type") or "")
        if "cloudfront" in product_lower and _cf_m:
            _bucket = _CF_DEST_TO_BUCKET.get(_cf_m.group(1).upper(), "apac")
            _total_gb = _cf_bucket_totals.get(_bucket, float(r.get("total_usage") or 0))
            _sku_id, _sku_name, _rate = cdn_egress_rate(_bucket, _total_gb)
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud CDN",
                "gcp_sku_id":         _sku_id,
                "gcp_sku_name":       _sku_name,
                "component":          "transfer",
                "strategy":           "map",
                "unit_multiplier":    1.0,
                "gcp_region":         r.get("gcp_region"),
                "projection_note":    (
                    f"CloudFront cache egress → {_sku_name} "
                    f"(total monthly {_bucket} volume: {_total_gb:,.0f} GB → "
                    f"${_rate:.4f}/GB tier; verify current GCP CDN rates at "
                    f"cloud.google.com/cdn/pricing — Media CDN excluded: "
                    f"requires Google account-team approval)"
                ),
                "mapping_confidence": 0.80,
            })
            continue

        strategy, direction, note = _transfer_target(
            r.get("usage_type"), f"{r.get('operation') or ''} {r.get('product') or ''}"
        )
        entry = {
            "aws_li_key":       r["aws_li_key"],
            "gcp_service":      GCP_COMPUTE_ENGINE,
            "component":        "transfer",
            "strategy":         strategy,
            "unit_multiplier":  1.0,
            "gcp_region":       r.get("gcp_region"),
            "projection_note":  f"data_transfer: {note}",
            "mapping_confidence": 0.85,
        }
        if strategy == "map" and direction in EGRESS_SKUS:
            sku_id, sku_name, _rate = EGRESS_SKUS[direction]
            entry["gcp_sku_id"] = sku_id
            # EGRESS_SKUS stores plain strings (synthetic IDs), no .unit attribute
            entry["gcp_sku_unit"] = getattr(sku_id, "unit", None)
            entry["gcp_sku_name"] = sku_name
        else:
            entry["gcp_sku_name"] = None
        out.append(entry)
    return out


def map_non_workload(rows):
    out = []
    for r in rows:
        product = (r.get("product") or "").lower()
        desc = (r.get("operation") or "").lower()
        if "marketplace" in product or "marketplace" in desc:
            gcp_service = "AWS Marketplace (Passthrough)"
            note = "AWS Marketplace subscription — passthrough at cost parity; not core workload"
        elif "support" in product or "support" in desc:
            gcp_service = "AWS Support (Passthrough)"
            note = "AWS Support plan — passthrough at cost parity; not core workload"
        else:
            gcp_service = "AWS Non-Workload (Passthrough)"
            note = "AWS Non-workload item — passthrough at cost parity"
            
        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        gcp_service,
            "gcp_sku_id":         None,
            "gcp_sku_name":       None,
            "component":          "passthrough",
            "strategy":           "passthrough",
            "unit_multiplier":    1.0,
            "gcp_region":         r.get("gcp_region"),
            "projection_note":    note,
            "mapping_confidence": 1.0,
        })
    return out


def map_commitment_discount(rows):
    """RI fees, Savings Plan recurring fees, and EdpDiscount rows → ignore ($0).

    These rows are AWS commitment/amortization artifacts:
      - RIFee / SavingsPlanRecurringFee: the upfront/recurring commitment charge;
        the actual usage is captured in DiscountedUsage / SavingsPlanCoveredUsage
        rows at the amortized rate. Counting both double-bills the instance cost.
      - EdpDiscount: AWS negotiated discount; GCP equivalent is committed-use
        discounts (CUDs) applied directly to instance pricing — no separate line.
      - Compute Savings Plans: amortized plan fee already reflected in
        SavingsPlanCoveredUsage instance rows at their blended rate.

    All these rows must be ignored so they don't inflate the AWS baseline or
    cause the passthrough total to exceed the 5% guard.
    """
    out = []
    for r in rows:
        product = (r.get("product") or "Unknown")
        line_type = (r.get("line_item_type") or "")
        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        "N/A (commitment amortization)",
            "gcp_sku_id":         None,
            "gcp_sku_name":       None,
            "component":          "commitment",
            "strategy":           "ignore",
            "unit_multiplier":    1.0,
            "gcp_region":         r.get("gcp_region"),
            "projection_note":    (f"AWS commitment artifact ({line_type}: {product}) — "
                                   "already reflected in amortized instance costs; ignore to avoid double-billing"),
            "mapping_confidence": 1.0,
        })
    return out


def map_guardduty(rows):
    """GuardDuty and Security Hub → Security Command Center passthrough.

    Both services are priced on incompatible models (GuardDuty: $/GB-analyzed;
    SCC Premium: $/asset/mo) so a rate-based mapping is not feasible. We carry
    the AWS cost as an honest passthrough with the correct GCP service label
    so the report says "Security Command Center" rather than "Unmapped".
    """
    out = []
    for r in rows:
        product = (r.get("product") or "").lower()
        if "security hub" in product or "securityhub" in product:
            note = ("AWS Security Hub → Security Command Center Standard; "
                    "pricing model differs (per-finding vs per-asset/mo); passthrough at cost parity")
        else:
            note = ("Amazon GuardDuty → Security Command Center Premium; "
                    "pricing model differs (per-GB-analyzed vs per-asset/mo); passthrough at cost parity")
        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        "Security Command Center",
            "gcp_sku_id":         None,
            "gcp_sku_name":       None,
            "component":          "security",
            "strategy":           "passthrough",
            "unit_multiplier":    1.0,
            "gcp_region":         r.get("gcp_region"),
            "projection_note":    note,
            "mapping_confidence": 0.60,
        })
    return out


# ---------------------------------------------------------------------------
# Managed DB (RDS/Aurora instance-hours) → Cloud SQL instance SKU
# ---------------------------------------------------------------------------
# RDS instance type → (vCPU, RAM_GiB).  Only instance-hour rows land here;
# storage/IO/snapshot rows are handled by map_block_storage.
_RDS_SPECS: dict[str, tuple[int, float]] = {
    # T-family (burstable)
    "db.t2.micro": (1, 1.0),   "db.t2.small": (1, 2.0),   "db.t2.medium": (2, 4.0),
    "db.t2.large": (2, 8.0),   "db.t2.xlarge": (4, 16.0),  "db.t2.2xlarge": (8, 32.0),
    "db.t3.micro": (2, 1.0),   "db.t3.small": (2, 2.0),   "db.t3.medium": (2, 4.0),
    "db.t3.large": (2, 8.0),   "db.t3.xlarge": (4, 16.0),  "db.t3.2xlarge": (8, 32.0),
    "db.t4g.micro": (2, 1.0),  "db.t4g.small": (2, 2.0),  "db.t4g.medium": (2, 4.0),
    "db.t4g.large": (2, 8.0),  "db.t4g.xlarge": (4, 16.0), "db.t4g.2xlarge": (8, 32.0),
    # M-family (general purpose)
    "db.m5.large": (2, 8.0),   "db.m5.xlarge": (4, 16.0),  "db.m5.2xlarge": (8, 32.0),
    "db.m5.4xlarge": (16, 64.0),"db.m5.8xlarge": (32, 128.0),"db.m5.12xlarge": (48, 192.0),
    "db.m5.16xlarge": (64, 256.0),"db.m5.24xlarge": (96, 384.0),
    "db.m6g.large": (2, 8.0),  "db.m6g.xlarge": (4, 16.0), "db.m6g.2xlarge": (8, 32.0),
    "db.m6g.4xlarge": (16, 64.0),"db.m6g.8xlarge": (32, 128.0),"db.m6g.12xlarge": (48, 192.0),
    "db.m6g.16xlarge": (64, 256.0),
    "db.m6i.large": (2, 8.0),  "db.m6i.xlarge": (4, 16.0), "db.m6i.2xlarge": (8, 32.0),
    "db.m6i.4xlarge": (16, 64.0),"db.m6i.8xlarge": (32, 128.0),"db.m6i.12xlarge": (48, 192.0),
    "db.m6i.16xlarge": (64, 256.0),"db.m6i.24xlarge": (96, 384.0),"db.m6i.32xlarge": (128, 512.0),
    "db.m7g.large": (2, 8.0),  "db.m7g.xlarge": (4, 16.0), "db.m7g.2xlarge": (8, 32.0),
    "db.m7g.4xlarge": (16, 64.0),"db.m7g.8xlarge": (32, 128.0),"db.m7g.12xlarge": (48, 192.0),
    "db.m7g.16xlarge": (64, 256.0),
    # R-family (memory optimized)
    "db.r5.large": (2, 16.0),  "db.r5.xlarge": (4, 32.0),  "db.r5.2xlarge": (8, 64.0),
    "db.r5.4xlarge": (16, 128.0),"db.r5.8xlarge": (32, 256.0),"db.r5.12xlarge": (48, 384.0),
    "db.r5.16xlarge": (64, 512.0),"db.r5.24xlarge": (96, 768.0),
    "db.r6g.large": (2, 16.0), "db.r6g.xlarge": (4, 32.0), "db.r6g.2xlarge": (8, 64.0),
    "db.r6g.4xlarge": (16, 128.0),"db.r6g.8xlarge": (32, 256.0),"db.r6g.12xlarge": (48, 384.0),
    "db.r6g.16xlarge": (64, 512.0),
    "db.r6i.large": (2, 16.0), "db.r6i.xlarge": (4, 32.0), "db.r6i.2xlarge": (8, 64.0),
    "db.r6i.4xlarge": (16, 128.0),"db.r6i.8xlarge": (32, 256.0),"db.r6i.12xlarge": (48, 384.0),
    "db.r6i.16xlarge": (64, 512.0),"db.r6i.24xlarge": (96, 768.0),"db.r6i.32xlarge": (128, 1024.0),
    "db.r7g.large": (2, 16.0), "db.r7g.xlarge": (4, 32.0), "db.r7g.2xlarge": (8, 64.0),
    "db.r7g.4xlarge": (16, 128.0),"db.r7g.8xlarge": (32, 256.0),"db.r7g.12xlarge": (48, 384.0),
    "db.r7g.16xlarge": (64, 512.0),
    # X-family (extreme memory)
    "db.x2g.large": (4, 32.0), "db.x2g.xlarge": (8, 64.0), "db.x2g.2xlarge": (16, 128.0),
    "db.x2g.4xlarge": (32, 256.0),"db.x2g.8xlarge": (64, 512.0),"db.x2g.12xlarge": (96, 768.0),
    "db.x2g.16xlarge": (128, 1024.0),
}

_DB_ITYPE_RE = re.compile(r"InstanceUsage:(db\.[a-z0-9]+\.[a-z0-9]+)", re.IGNORECASE)
# ACU = Aurora Capacity Unit: 1 ACU ≈ 2 vCPU + 4 GiB RAM
# Flat-CSV/PDF bills write "Serverless v2" (space + lowercase v) in the
# operation field rather than the CUR token "ServerlessV2". ":ACU" covers
# CUR usage_type like "ServerlessV2:ACU"; "Capacity Unit hour" covers the
# PDF operation text "per Aurora Capacity Unit hour running Aurora PostgreSQL
# Serverless v2". Both forms must match so the row reaches map_managed_db()
# instead of falling silently to misc/LLM with no GCP price.
_ACU_RE = re.compile(
    r"ServerlessV2|Serverless\s+v2|:ACU|Capacity\s+Unit\s+hour",
    re.IGNORECASE,
)


def map_managed_db(rows: list[dict]) -> list[dict]:
    """Aurora Serverless v2 ACU-hours → Cloud SQL bundled instance SKU.

    ONLY Aurora Serverless (ACU-billed, no traditional instance type to parse)
    rows are handled here. Normal RDS/Aurora instance-hour rows are handled
    exclusively by family_mapper.py's map_db_row(), which emits separate core+RAM
    components AND correctly distinguishes Single-AZ (Zonal) from Multi-AZ
    (Regional) pricing — this function used to ALSO emit a third, overlapping
    bundled "instance" SKU for the same rows (family_mapper.py runs after this
    script in the pipeline and both outputs get merged), silently billing
    compute twice on every RDS row. Storage/IO/backup rows are handled
    separately by map_block_storage regardless.
    """
    out = []
    for r in rows:
        ut = r.get("usage_type") or ""
        region = r.get("gcp_region") or "global"

        # Aurora Serverless v2 ACU-hours: 1 ACU ≈ 2 vCPU + 4 GiB → "2 vCPU + 7.5 GB RAM"
        # classify_mechanics.py routes ACU-billed rows here via pricing_unit
        # (ACU-Hrs/ACU-hours) alone on PDF bills with blank usage_type — check
        # unit and operation too, or such a row reaches this function, fails
        # the usage_type-only check, and silently gets no mapping at all
        # (family_mapper.py only handles instance-typed rows, not ACU billing).
        acu_blob = f"{ut} {r.get('operation') or ''}"
        if _ACU_RE.search(acu_blob) or (r.get("unit") or "").lower() in ("acu-hrs", "acu-hours"):
            product_blob = f"{r.get('product') or ''} {r.get('operation') or ''}".lower()
            # Aurora Serverless v2 has no bundled Cloud SQL SKU for PostgreSQL —
            # the real catalog only has unbundled per-vCPU/per-GB PostgreSQL SKUs
            # (confirmed via find-sku.sh), unlike MySQL's bundled-instance sizes.
            # Forcing Postgres through _cloud_sql_sku_for() (MySQL-only bundles)
            # silently mispriced/mislabeled it as MySQL.
            if "postgres" in product_blob:
                core_desc = "Cloud SQL for PostgreSQL: Zonal - vCPU"
                ram_desc = "Cloud SQL for PostgreSQL: Zonal - RAM"
                core_sku = resolve_sku(GCP_CLOUD_SQL, core_desc, region)
                ram_sku = resolve_sku(GCP_CLOUD_SQL, ram_desc, region)
                base_note = "Aurora Serverless v2 ACU-hrs → Cloud SQL for PostgreSQL unbundled vCPU+RAM (1 ACU ≈ 2 vCPU + 4 GiB)"
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_SQL,
                    "gcp_sku_id":         str(core_sku) if core_sku else None,
                    "gcp_sku_name":       core_desc,
                    "component":          "core",
                    "strategy":           "map" if core_sku else "passthrough",
                    "unit_multiplier":    2.0,
                    "gcp_region":         region,
                    "projection_note":    base_note,
                    "mapping_confidence": 0.65,
                })
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_SQL,
                    "gcp_sku_id":         str(ram_sku) if ram_sku else None,
                    "gcp_sku_name":       ram_desc,
                    "component":          "ram",
                    "strategy":           "map" if ram_sku else "passthrough",
                    "unit_multiplier":    4.0,
                    "gcp_region":         region,
                    "projection_note":    base_note,
                    "mapping_confidence": 0.65,
                })
            else:
                # Previously bin-packed into the fixed legacy bundled-instance
                # size table (_cloud_sql_sku_for), which picks the SMALLEST
                # bundle >= the requested vCPU/RAM — for a 1-ACU (2vCPU+4GiB)
                # row, no exact-fit bundle exists, so it over-provisioned to
                # the next size up ("Zonal - 2 vCPU + 7.5GB RAM", $0.1351/h).
                # The unbundled per-unit MySQL SKUs (confirmed real via
                # resolve_sku, same SKUs the family_mapper.py instance-hour
                # path already uses) give an exact-fit 2 vCPU + 4 GiB at
                # $0.1106/h — cheaper AND not over-provisioned, at the same
                # Cloud SQL Zonal guarantee. Matches the treatment the
                # PostgreSQL branch above already gets; MySQL's bundled-size
                # code path just never received the same fix.
                engine_note = ""
                if "mariadb" in product_blob:
                    engine_note = " [engine substitution: Cloud SQL has no native MariaDB offering; priced as MySQL]"
                core_desc = "Cloud SQL for MySQL: Zonal - vCPU"
                ram_desc = "Cloud SQL for MySQL: Zonal - RAM"
                core_sku = resolve_sku(GCP_CLOUD_SQL, core_desc, region)
                ram_sku = resolve_sku(GCP_CLOUD_SQL, ram_desc, region)
                base_note = "Aurora Serverless v2 ACU-hrs → Cloud SQL for MySQL unbundled vCPU+RAM (1 ACU ≈ 2 vCPU + 4 GiB)" + engine_note
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_SQL,
                    "gcp_sku_id":         str(core_sku) if core_sku else None,
                    "gcp_sku_name":       core_desc,
                    "component":          "core",
                    "strategy":           "map" if core_sku else "passthrough",
                    "unit_multiplier":    2.0,
                    "gcp_region":         region,
                    "projection_note":    base_note,
                    "mapping_confidence": 0.65,
                })
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_CLOUD_SQL,
                    "gcp_sku_id":         str(ram_sku) if ram_sku else None,
                    "gcp_sku_name":       ram_desc,
                    "component":          "ram",
                    "strategy":           "map" if ram_sku else "passthrough",
                    "unit_multiplier":    4.0,
                    "gcp_region":         region,
                    "projection_note":    base_note,
                    "mapping_confidence": 0.65,
                })
        # Non-ACU rows: leave entirely to family_mapper.py (do not append here —
        # any row family_mapper.py can't parse falls through to the LLM, the same
        # safety net compute_breakdown already relies on).

    return out


_ELASTICACHE_RAM_GIB = _inst_cfg.get("elasticache_ram_gib", {})

# Regex to extract a bare node type from the operation text when instance_type is null.
# Matches patterns like "M5.large", "m6g.xlarge", "R5.large", "T4G Medium", "T3 Small", etc.
_EC_NODE_RE = re.compile(
    r"\b(t[234]g?)\s*(micro|small|medium)"        # T-family with optional space
    r"|\b(m[567]g?)\.(large|xlarge|[248]xlarge|12xlarge|16xlarge|24xlarge)"  # M-family
    r"|\b(r[567]g?)\.(large|xlarge|[248]xlarge|12xlarge|16xlarge|24xlarge)"  # R-family
    , re.IGNORECASE
)


def _elasticache_ram(r: dict) -> float | None:
    """Return RAM in GiB for an ElastiCache row, or None if unknown."""
    # instance_ram_gb is populated by ingest.py for rows that have instance_type
    ram = r.get("instance_ram_gb")
    if ram:
        return float(ram)
    itype = (r.get("instance_type") or "").lower().strip()
    if itype in _ELASTICACHE_RAM_GIB:
        return _ELASTICACHE_RAM_GIB[itype]
    # Try extracting node type from usage_type: "APS5-NodeUsage:cache.r7g.large"
    ut = r.get("usage_type") or ""
    ut_match = re.search(r"NodeUsage:(cache\.\S+)", ut, re.IGNORECASE)
    if ut_match:
        key = ut_match.group(1).lower()
        if key in _ELASTICACHE_RAM_GIB:
            return _ELASTICACHE_RAM_GIB[key]
    # Fall back to parsing the operation text (PDF bills often lack instance_type)
    op = r.get("operation") or ""
    m = _EC_NODE_RE.search(op)
    if m:
        # Reconstruct a canonical cache.family.size key
        if m.group(1):   # T-family: "T4G Medium" → "cache.t4g.medium"
            key = f"cache.{m.group(1).lower()}.{m.group(2).lower()}"
        elif m.group(3): # M-family
            key = f"cache.{m.group(3).lower()}.{m.group(4).lower()}"
        else:            # R-family
            key = f"cache.{m.group(5).lower()}.{m.group(6).lower()}"
        return _ELASTICACHE_RAM_GIB.get(key)
    return None


# Real Memorystore for Redis capacity-tier maximum GiB per tier name. The
# catalog's own SKU description ("Redis Capacity Basic M1 <region>") never
# encodes the GiB boundary — only Google's product documentation does — so
# this small table is unavoidable, same as the compute tier/workload tables.
# What IS discovered dynamically is which tier names actually exist in the
# catalog (below) — this replaces a RAM-band table that referenced M10/M20,
# which don't exist anywhere in the real Memorystore catalog (confirmed: the
# real tiers are M1 through M5 only), meaning every node that used to land in
# the "M10"/"M20" band was priced against a nonexistent SKU, and nodes that
# should have landed in M2/M3/M4 were always overcharged as M5.
_MEMORYSTORE_TIER_MAX_GIB = _gcp_cfg.get("memorystore_tier_max_gib", {"M1": 4, "M2": 10, "M3": 35, "M4": 100, "M5": 300})

_MEMORYSTORE_TIERS_CACHE = None


def _discover_memorystore_tiers():
    """Scan the real bundled Memorystore catalog for every distinct
    "Redis Capacity {Basic,Standard} M*" tier that actually exists.
    Returns a list of (cache_tier, size_tier, max_gib) sorted by max_gib.
    """
    global _MEMORYSTORE_TIERS_CACHE
    if _MEMORYSTORE_TIERS_CACHE is not None:
        return _MEMORYSTORE_TIERS_CACHE

    pat = re.compile(r"^Redis Capacity (Basic|Standard) (M\d+)\b")
    found = set()
    try:
        services = _load_services()
        service_id = services.get(GCP_MEMORYSTORE)
        sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz") if service_id else None
        if sku_file and os.path.exists(sku_file):
            skus = _load_sku_file(sku_file)
            for sku in skus:
                desc = (sku.get("description") or "").strip()
                m = pat.match(desc)
                if not m:
                    continue
                found.add((m.group(1), m.group(2)))
    except Exception as e:
        print(f"WARNING: _discover_memorystore_tiers catalog scan failed ({e}); falling back to "
              f"empty candidate set — ElastiCache sizing will find nothing", file=sys.stderr)

    result = []
    for cache_tier, size_tier in found:
        max_gib = _MEMORYSTORE_TIER_MAX_GIB.get(size_tier)
        if max_gib is None:
            print(f"WARNING: catalog Memorystore tier {size_tier!r} has no GiB classification — "
                  f"add it to _MEMORYSTORE_TIER_MAX_GIB or it will never be considered when sizing "
                  f"ElastiCache rows", file=sys.stderr)
            continue
        result.append((cache_tier, size_tier, max_gib))
    result.sort(key=lambda x: x[2])
    _MEMORYSTORE_TIERS_CACHE = result
    return result


def _cheapest_memorystore_tier(cache_tier, ram_gib, region):
    """Pick the cheapest real Memorystore tier at the given guarantee tier
    (Basic/Standard — never crossed, that's the HA guarantee already
    correctly detected via deployment_option) whose capacity covers ram_gib.
    Returns (sku_name, size_tier) or (None, None) if no real tier covers it."""
    for tier, size_tier, max_gib in _discover_memorystore_tiers():
        if tier == cache_tier and max_gib >= ram_gib:
            return f"Redis Capacity {cache_tier} {size_tier}", size_tier
    return None, None


def map_elasticache(rows: list) -> list:
    """ElastiCache for Redis/Memcached → Cloud Memorystore for Redis.

    Billing model: ElastiCache charges per node-hour; Memorystore charges per
    GiBy.h of capacity.  unit_multiplier = RAM_GiB converts node-hours into
    GiBy.h so the catalog rate applies directly.

    Tier: `deployment_option` (the same CUR column already used for RDS
    Multi-AZ detection — populated generically by ingest.py from
    product/deploymentOption, not an RDS-only field) is checked directly.
    "Multi-AZ" -> Standard HA tier (replicated, automatic failover);
    anything else -> Basic (single-instance). This used to always default
    to Basic regardless of the source's actual replication topology — a
    real Multi-AZ/replication-group source would silently get priced
    against a non-HA target, understating cost for the guarantee the
    customer is actually paying AWS for. When deployment_option itself is
    blank (some CUR formats omit it for ElastiCache specifically), we still
    can't tell — that residual uncertainty is why the confidence ceiling
    stays at 0.80 rather than reaching 1.0, not because the check isn't
    attempted.
    """
    out = []
    for r in rows:
        region = r.get("gcp_region")

        # Memcached is a genuinely different GCP product (Cloud Memorystore for
        # Memcached), billed per-vCPU-core-hour rather than Redis's per-GiB-hour
        # — engine text only appears in `operation` free text (product is always
        # the generic "Amazon ElastiCache for X" or blank), never in a dedicated
        # column, so check operation/product directly.
        engine_blob = f"{r.get('product') or ''} {r.get('operation') or ''}".lower()
        if "memcached" in engine_blob:
            itype = (r.get("instance_type") or "").lower()
            ec2_key = itype[len("cache."):] if itype.startswith("cache.") else itype
            # ec2-instance-types.json uses "vcpus" (plural) — "vcpu" was the old wrong key
            vcpus = (_EC2_TYPES_FOR_VCPU.get(ec2_key) or {}).get("vcpus")
            ram_gib = _ELASTICACHE_RAM_GIB.get(itype) or _ELASTICACHE_RAM_GIB.get("cache." + ec2_key)
            if not vcpus:
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_MEMORYSTORE_MEMCACHED,
                    "gcp_sku_id":         None,
                    "gcp_sku_name":       "Custom Core M1",
                    "component":          "cache",
                    "strategy":           "passthrough",
                    "unit_multiplier":    1.0,
                    "gcp_region":         region,
                    "projection_note":    ("ElastiCache for Memcached → Memorystore for Memcached; "
                                           "node vCPU count unknown (instance_type missing/unrecognized) "
                                           "— passthrough at cost parity"),
                    "mapping_confidence": 0.50,
                })
                continue
            core_sku = resolve_sku(GCP_MEMORYSTORE_MEMCACHED, "Custom Core M1", region)
            # Memorystore Memcached bills per-vCPU-hour (Custom Core) AND per-GiBy-hour (Custom RAM M1)
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_MEMORYSTORE_MEMCACHED,
                "gcp_sku_id":         str(core_sku) if core_sku else None,
                "gcp_sku_name":       "Custom Core M1",
                "component":          "cache_cpu",
                "strategy":           "map" if core_sku else "passthrough",
                "unit_multiplier":    float(vcpus),
                "gcp_region":         region,
                "projection_note":    (f"ElastiCache for Memcached → Memorystore for Memcached "
                                       f"Custom Core; node vCPU={vcpus} → unit_multiplier={vcpus} (core-hours)"),
                "mapping_confidence": 0.75,
            })
            if ram_gib:
                ram_sku = resolve_sku(GCP_MEMORYSTORE_MEMCACHED, "Custom RAM M1", region)
                out.append({
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_MEMORYSTORE_MEMCACHED,
                    "gcp_sku_id":         str(ram_sku) if ram_sku else None,
                    "gcp_sku_name":       "Custom RAM M1",
                    "component":          "cache_ram",
                    "strategy":           "map" if ram_sku else "passthrough",
                    "unit_multiplier":    float(ram_gib),
                    "gcp_region":         region,
                    "projection_note":    (f"ElastiCache for Memcached → Memorystore for Memcached "
                                           f"Custom RAM; node RAM={ram_gib} GiB → unit_multiplier={ram_gib} (GiBy-hours)"),
                    "mapping_confidence": 0.75,
                })
            continue

        ram = _elasticache_ram(r)
        deployment_raw = (r.get("deployment_option") or "").strip().lower()
        is_ha = deployment_raw == "multi-az"
        cache_tier = "Standard" if is_ha else "Basic"
        # deployment_option itself absent (not just "Single-AZ") means CUR gave
        # no topology signal at all for this row — genuine ambiguity, same
        # category as OpenSearch's "no application-context in CUR" situation,
        # so it gets the same 70% confidence ceiling CLAUDE.md §6 already
        # established for that case, rather than a higher confidence that
        # implies we actually know the topology.
        has_deployment_signal = bool(deployment_raw)

        if ram is None:
            # Can't determine node size — passthrough with correct GCP label
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_MEMORYSTORE,
                "gcp_sku_id":         None,
                "gcp_sku_name":       f"Redis Capacity {cache_tier} M1",
                "component":          "cache",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region,
                "projection_note":    ("ElastiCache → Memorystore for Redis; node RAM unknown "
                                       "(instance_type missing from CUR) — passthrough at cost parity"),
                "mapping_confidence": 0.50,
            })
            continue

        # Pick the cheapest real Memorystore capacity tier (discovered from
        # the actual catalog, not a hand-typed RAM-band table) that covers
        # this node's RAM, never crossing the Basic/Standard guarantee tier.
        sku_name, size_tier = _cheapest_memorystore_tier(cache_tier, ram, region)
        if sku_name is None:
            # No real tier covers this much RAM — largest known tier is the
            # honest ceiling; the note below already discloses the shortfall.
            tiers = _discover_memorystore_tiers()
            largest = max((t for t in tiers if t[0] == cache_tier), key=lambda t: t[2], default=None)
            size_tier = largest[1] if largest else "M1"
            sku_name = f"Redis Capacity {cache_tier} {size_tier}"
        sku_id = resolve_sku(GCP_MEMORYSTORE, sku_name, region)
        strategy = "map" if sku_id else "passthrough"
        if is_ha:
            note = (f"ElastiCache Multi-AZ → Memorystore for Redis Standard HA ({sku_name}); "
                    f"node RAM={ram:.2f} GiB → unit_multiplier={ram:.2f} (GiBy.h); "
                    f"deployment_option=Multi-AZ — replicated/failover guarantee preserved, "
                    f"not silently downgraded to Basic")
        elif has_deployment_signal:
            note = (f"ElastiCache → Memorystore for Redis Basic ({sku_name}); "
                    f"node RAM={ram:.2f} GiB → unit_multiplier={ram:.2f} (GiBy.h); "
                    f"deployment_option confirms single-instance (not Multi-AZ)")
        else:
            note = (f"ElastiCache → Memorystore for Redis Basic ({sku_name}); "
                    f"node RAM={ram:.2f} GiB → unit_multiplier={ram:.2f} (GiBy.h); "
                    f"deployment_option not present in this CUR export — cannot confirm "
                    f"single-instance vs Multi-AZ/Cluster mode; Standard HA tier would cost "
                    f"~1.3-1.9x if this is actually HA. [architecture review recommended: "
                    f"verify ElastiCache replication topology with customer]")
        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        GCP_MEMORYSTORE,
            "gcp_sku_id":         sku_id,
            "gcp_sku_name":       sku_name,
            "component":          "cache",
            "strategy":           strategy,
            "unit_multiplier":    ram,
            "gcp_region":         region,
            "projection_note":    note,
            # Real deployment_option signal (HA or not) → 0.85, evidence-based.
            # No signal at all → same 70% ceiling CLAUDE.md §6 uses for
            # OpenSearch's identical "no application context in CUR" case.
            "mapping_confidence": 0.85 if has_deployment_signal else 0.70,
        })
    return out


# Redshift node-type → BigQuery Standard Edition slot-hour conversion table.
# Edit data/instance-specs.json → redshift_slot_map to add new node types.
_REDSHIFT_SLOT_MAP = _inst_cfg.get("redshift_slot_map", {
    "dc2.large": 500, "dc2.8xlarge": 4000,
    "ds2.xlarge": 500, "ds2.8xlarge": 4000,
    "ra3.large": 250, "ra3.xlplus": 500,
    "ra3.4xlarge": 1500, "ra3.16xlarge": 6000,
})
# ra3.large (AWS's smaller, cheaper entry-level RA3 tier, added after the
# original xlplus/4xlarge/16xlarge lineup) was missing entirely — confirmed
# real: a customer bill billing "$0.618 per Redshift RA3.LARGE Compute
# Node-hour" fell through to unpriced passthrough. Unlike the other slot
# counts (sourced from AWS/BigQuery's own published migration-guide capacity
# recommendations), ra3.large has no equivalent official guidance yet — 250
# is estimated from AWS's own published spec (2 vCPU/16GiB, roughly half of
# ra3.xlplus's 4 vCPU/32GiB/500-slot recommendation), not a sourced figure.
_REDSHIFT_SLOT_ESTIMATED = {"ra3.large"}
# Both of these were wrong against the real bundled catalog — confirmed live:
# "Standard Edition Slot Hour" matches nothing at all (the real per-region SKU
# is "BigQuery Standard Edition for <City> (<region>)", and it's billed under
# a SEPARATE service, "BigQuery Reservation API", not the base "BigQuery"
# service at all — every Redshift node-hour row silently failed to resolve a
# rate in every region, not just one). "Active Storage" was also wrong — the
# real storage SKU is "Active Logical Storage".
GCP_BIGQUERY_RESERVATION = "BigQuery Reservation API"
_BQ_SLOT_SKU      = "BigQuery Standard Edition for"
_BQ_STORAGE_SKU   = "Active Logical Storage"


def map_redshift(rows):
    """Redshift → BigQuery deterministic mapping.

    Node-hours: converted to BigQuery Standard Edition slot-hours using
    _REDSHIFT_SLOT_MAP (slots per node). RA3 ManagedStorage → BQ active storage.
    Serverless RPU → BQ slot-hours at 128 slots/RPU. Backups → passthrough.
    Unknown instance types → passthrough with a note.
    """
    out = []
    for r in rows:
        ut  = (r.get("usage_type") or "").lower()
        op  = (r.get("operation")  or "").lower()
        gcp_region = r.get("gcp_region")
        blob = f"{ut} {op} {(r.get('product') or '').lower()}"

        # Backup and snapshot rows → passthrough (no BQ equivalent charge)
        if "backup" in blob or "snapshot" in blob:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "BigQuery",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "backup",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "Redshift backup/snapshot — no BigQuery equivalent charge; passthrough at cost parity",
                "mapping_confidence": 0.70,
            })
            continue

        # RA3 ManagedStorage → BigQuery active storage ($0.02/GB-mo).
        # AWS usage_type can be "APS3-RMS:ra3.4xlarge" where "RMS" = Redshift
        # Managed Storage; also matches "ManagedStorage" literal and "rms"
        # abbreviation. PDF-format bills carry "RMS" in `product` instead of
        # `usage_type` (confirmed real: product="Amazon Redshift APS3-RMS:
        # ra3.4xlarge" with usage_type blank) — checking `ut` alone missed
        # this, letting a GB-Mo storage row fall through to the node-hour/
        # slot-hour conversion path below instead of the correct storage SKU.
        if "managedstorage" in ut or "managed storage" in blob or re.search(r'\brms\b', blob):
            sku_id = resolve_sku("BigQuery", _BQ_STORAGE_SKU, gcp_region)
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "BigQuery",
                "gcp_sku_name":       _BQ_STORAGE_SKU,
                "component":          "storage",
                "strategy":           "map" if sku_id else "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "Redshift RA3 ManagedStorage → BigQuery Active Storage" + _no_rate_suffix(sku_id, gcp_region),
                "mapping_confidence": 0.80,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
            out.append(entry)
            continue

        # Serverless RPU hours → BigQuery slot-hours (1 RPU ≈ 128 BQ slots)
        if "serverless" in blob or re.search(r"\brpu\b", ut):
            sku_id = resolve_sku(GCP_BIGQUERY_RESERVATION, _BQ_SLOT_SKU, gcp_region)
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_BIGQUERY_RESERVATION,
                "gcp_sku_name":       _BQ_SLOT_SKU,
                "component":          "compute",
                "strategy":           "map" if sku_id else "passthrough",
                "unit_multiplier":    128.0,
                "gcp_region":         gcp_region,
                "projection_note":    "Redshift Serverless RPU → BigQuery slot-hours (1 RPU ≈ 128 BQ Standard slots)" + _no_rate_suffix(sku_id, gcp_region),
                "mapping_confidence": 0.65,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
            out.append(entry)
            continue

        # Node-hour rows: match instance type in usage_type or operation
        slots = None
        matched_family = None
        for family, slot_count in _REDSHIFT_SLOT_MAP.items():
            if family in ut or family in op:
                slots = slot_count
                matched_family = family
                break

        if slots:
            # BigQuery's committed-use pricing ("Flat Rate Annual/Monthly") is a
            # genuinely SEPARATE SKU from on-demand "Standard Edition" — not a
            # different pricing_type of the same SKU the way most other GCP
            # services work. That means the generic OnDemand-vs-Commit1Yr
            # comparison in projection_view.py's gcp_projection VIEW (which
            # looks up a Commit1Yr rate for the SAME sku_id) can never bridge
            # these two — it would silently keep comparing a Reserved AWS row
            # against GCP's on-demand rate regardless of pricing_model. This
            # row's own commitment status has to pick the correct SKU directly.
            is_reserved = r.get("pricing_model") == "Committed"

            if is_reserved:
                # BigQuery Flat Rate Annual's real catalog SKU is a bare $/mo
                # rate with displayQuantity=1 and no "per slot" qualifier in
                # its usageUnit/description — there is no reliable way to
                # tell from catalog metadata whether that $/mo figure is
                # per-slot or per some larger commitment block (GCP flat-rate
                # commitments are actually sold in blocks, historically ~100
                # slots minimum). Confirmed real: multiplying by the raw slot
                # count (unit_multiplier=1500) produced an ~11,796x blowup
                # that outlier_gate.py correctly hard-clamped to passthrough
                # — but a later Phase-5 triage pass then "fixed" it by
                # reverse-engineering a slots/hours-per-month scaling factor
                # that merely got the ratio under the gate's 50x threshold
                # (landing at a still-16x overprice), not a genuinely correct
                # price. Rather than repeat that mistake with a different
                # invented multiplier, this row stays an honest passthrough:
                # we don't have enough evidence from the catalog to compute a
                # defensible slot-to-price ratio, and CLAUDE.md's own
                # principle (never claim precision the CUR/catalog evidence
                # doesn't support) applies here as much as anywhere else.
                note = (f"Redshift {matched_family} Reserved Instance node-hour — AWS's RI term/"
                        "payment option has no reliable equivalent in GCP's BigQuery Flat Rate "
                        "commitment SKU from catalog metadata alone (the SKU exposes a bare $/mo "
                        "rate with no per-slot unit breakdown, and real flat-rate commitments are "
                        "sold in blocks, not per-slot); passthrough at AWS cost pending a manual "
                        "commitment-tier sizing review with customer")
                entry = {
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_BIGQUERY_RESERVATION,
                    "gcp_sku_id":         None,
                    "gcp_sku_name":       None,
                    "component":          "compute",
                    "strategy":           "passthrough",
                    "unit_multiplier":    1.0,
                    "gcp_region":         gcp_region,
                    "projection_note":    note,
                    "mapping_confidence": 0.50,
                }
                out.append(entry)
            else:
                sku_id = resolve_sku(GCP_BIGQUERY_RESERVATION, _BQ_SLOT_SKU, gcp_region)
                note = f"Redshift {matched_family} node-hour → BigQuery {slots} Standard slot-hours"
                if matched_family in _REDSHIFT_SLOT_ESTIMATED:
                    note += (f" [architecture review recommended: {slots} slots for {matched_family} is "
                             "an estimate derived from AWS's published node spec (no official AWS/BigQuery "
                             "migration-guide capacity recommendation exists for this node type yet) — "
                             "verify actual workload sizing with customer before finalizing]")
                entry = {
                    "aws_li_key":         r["aws_li_key"],
                    "gcp_service":        GCP_BIGQUERY_RESERVATION,
                    "gcp_sku_name":       _BQ_SLOT_SKU,
                    "component":          "compute",
                    "strategy":           "map" if sku_id else "passthrough",
                    "unit_multiplier":    float(slots),
                    "gcp_region":         gcp_region,
                    "projection_note":    note + _no_rate_suffix(sku_id, gcp_region),
                    "mapping_confidence": 0.55 if matched_family in _REDSHIFT_SLOT_ESTIMATED else 0.70,
                }
                if sku_id:
                    entry["gcp_sku_id"] = sku_id
                    entry["gcp_sku_unit"] = sku_id.unit
                out.append(entry)
        else:
            # Unrecognized Redshift row → passthrough with note
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "BigQuery",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "compute",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    f"Redshift row unrecognized (usage_type={r.get('usage_type')!r}) — passthrough at cost parity",
                "mapping_confidence": 0.40,
            })
    return out


def map_cloudwatch(rows):
    out = []
    for r in rows:
        ut = (r.get("usage_type") or "").lower()
        op = (r.get("operation") or "").lower()
        prod = (r.get("product") or "").lower()
        gcp_region = r.get("gcp_region")
        p_unit = (r.get("unit") or "").lower()

        # Dashboard charges: GCP Cloud Monitoring has no per-dashboard fee.
        # DashboardHour rows map to $0 (ignore) — NOT passthrough — so they
        # don't carry the AWS cost through and over-project.
        # PDF bills have empty usage_type with the charge info embedded in the
        # product field ("AmazonCloudWatch DashboardHour") — check all three.
        dash_blob = f"{prod} {ut} {op}"
        is_dashboard = "dashboard" in dash_blob

        # Only map log DATA VOLUME rows to Cloud Logging — those where the billing
        # unit is GB/GiB (ingested bytes). Every other CloudWatch row (API calls,
        # queries, metric periods, alarms, dashboard refreshes) is priced in a count
        # unit incompatible with Cloud Logging's $/GiBy.mo rate; multiplying call
        # counts by a GiBy rate inflates by 100x+. Passthrough at AWS cost parity
        # is the safe default until a proper per-unit pricer exists.
        is_log_volume = (
            ("logbytes" in ut or "datascanned" in ut or "logstorage" in ut
             or "logingest" in ut or "logingestion" in ut)
            or (("log" in ut or "log" in op) and p_unit in ("gb", "gib", "gb-mo", "giby.mo"))
        )

        if is_dashboard:
            # GCP Cloud Monitoring has no dashboard charge; $0 on GCP.
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Monitoring",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "dashboard",
                "strategy":           "ignore",
                "unit_multiplier":    0.0,
                "gcp_region":         gcp_region,
                "projection_note":    "CloudWatch DashboardHour → $0 on GCP (Cloud Monitoring includes dashboards free)",
                "mapping_confidence": 0.95,
            }
            out.append(entry)
            continue
        elif is_log_volume:
            # CloudWatch Logs ingest = $0.50/GiB. Cloud Logging ingest = $0.50/GiB.
            # Rate parity → passthrough at AWS cost gives the correct GCP estimate.
            # The bundled catalog has only Log Storage (retention) SKUs, not the ingestion
            # SKU, so resolve_sku returns the wrong rate. Passthrough is more accurate.
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Logging",
                "gcp_sku_id":         None,
                "gcp_sku_name":       "Log Storage",
                "component":          "logs",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "CloudWatch Logs data volume → Cloud Logging storage/ingestion parity ($0.50/GiB on both, passthrough = parity)",
                "mapping_confidence": 0.90,
            }
        else:
            # API calls, queries, metric periods, alarms, dashboards — count-based units
            # don't translate to Cloud Monitoring/Logging volume rates. Passthrough.
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Monitoring",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "monitoring",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "CloudWatch metric/API/query charge — unit-incompatible with Cloud Monitoring rates; passthrough at AWS cost parity",
                "mapping_confidence": 0.70,
            }
        out.append(entry)
    return out


def map_athena(rows):
    """Athena → BigQuery on-demand.

    Athena bills per-TB-scanned. BigQuery on-demand is also $/TB-analyzed
    (same unit, directly comparable). We map at unit_multiplier=1.0 — the
    total_usage (bytes or GB/TB scanned) maps to BigQuery Analysis pricing.
    If bytes_scanned is not in the usage unit, passthrough with note.
    """
    _BQ_ANALYSIS_SKU = "Analysis"
    out = []
    for r in rows:
        ut  = (r.get("usage_type") or "").lower()
        gcp_region = r.get("gcp_region")

        # Passthrough for non-data-scanned rows (CTAS, DDL, cancelled, limits)
        if re.search(r"data.?scanned|bytes.?scanned|tb.?scanned", ut):
            sku_id = resolve_sku("BigQuery", _BQ_ANALYSIS_SKU, gcp_region)
            # Athena bills in TB; BigQuery Analysis SKU is also /TB → multiplier=1.0
            # Unit conversion note: if CUR usage_type shows bytes, the projection
            # framework uses total_usage directly against the rate — verify unit matches.
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "BigQuery",
                "gcp_sku_name":       _BQ_ANALYSIS_SKU,
                "component":          "analysis",
                "strategy":           "map" if sku_id else "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "Athena data-scanned → BigQuery Analysis ($6.25/TB on-demand; first 1 TB/mo free)" + _no_rate_suffix(sku_id, gcp_region),
                "mapping_confidence": 0.85,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
            out.append(entry)
        else:
            # Other Athena charges (DDL, cancelled queries, DML metadata) → passthrough
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "BigQuery",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "analysis",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    f"Athena row (usage_type={r.get('usage_type')!r}) not data-scanned charge — passthrough at cost parity",
                "mapping_confidence": 0.50,
            })
    return out


def map_kinesis(rows):
    """Kinesis shard-hours → Pub/Sub throughput (hourly billing only).

    Kinesis Shard-Hours are the per-shard reservation fee. GCP Pub/Sub does not
    have a shard model — it's throughput-based. A shard supports 1 MB/s write and
    2 MB/s read. We map shard-hours → Pub/Sub message delivery as an approximation:
    1 shard × 1 hr ≈ 3.6 GB throughput capacity (1 MB/s × 3600 s). This is an
    over-estimate (many shards run < full utilization) — passthrough is also honest.
    Strategy: passthrough with Pub/Sub label, because unit models differ too much
    for an accurate rate-based projection without utilization data.
    """
    out = []
    for r in rows:
        ut  = (r.get("usage_type") or "").lower()
        gcp_region = r.get("gcp_region")

        if re.search(r"shard.?hr|shardhours|extended.?retention", ut):
            note = "Kinesis shard-hours → Pub/Sub (no shard model on GCP; throughput-based billing differs fundamentally); passthrough at cost parity"
        else:
            note = f"Kinesis hourly charge (usage_type={r.get('usage_type')!r}) → Pub/Sub passthrough"

        out.append({
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        GCP_PUBSUB,
            "gcp_sku_id":         None,
            "gcp_sku_name":       None,
            "component":          "messaging",
            "strategy":           "passthrough",
            "unit_multiplier":    1.0,
            "gcp_region":         gcp_region,
            "projection_note":    note,
            "mapping_confidence": 0.55,
        })
    return out


# EFS storage class → Filestore tier
_EFS_STORAGE_MAP = {
    # Standard (infrequent access) and Intelligent-Tiering → Filestore Basic HDD.
    # unit_multiplier was previously 0.90 (an unsourced "one-zone discount" with
    # no catalog basis — Filestore Basic HDD is a single flat-rate product with
    # no one-zone variant) — fabricated-precision bug, same class as other
    # unsourced conversion factors fixed today; removed.
    "standardia":      ("Cloud Filestore", GCP_FILESTORE_HDD,                   1.0 / 1.024),  # AWS decimal GB-Mo → GCP GiBy.mo
    "standard-ia":     ("Cloud Filestore", GCP_FILESTORE_HDD,                   1.0 / 1.024),
    "ia":              ("Cloud Filestore", GCP_FILESTORE_HDD,                   1.0 / 1.024),
    # Standard (frequent access) → Filestore Basic SSD (closer performance profile)
    "standard":        ("Cloud Filestore", "Filestore Capacity Basic SSD",       1.0 / 1.024),
}
_EFS_DEFAULT_STORAGE = ("Cloud Filestore", "Filestore Capacity Basic SSD", 1.0 / 1.024)


def map_efs(rows):
    """EFS → Filestore mapping.

    Standard storage → Filestore Basic SSD ($0.20/GB-mo).
    Infrequent Access → Filestore Basic HDD ($0.10/GB-mo).
    Provisioned Throughput (MB/s-month) → passthrough (Filestore includes throughput
    in capacity price; no separate throughput charge exists to map to).
    Data access / I/O requests → passthrough (GCP has no per-access EFS equivalent).
    """
    out = []
    for r in rows:
        ut  = (r.get("usage_type") or "").lower()
        op  = (r.get("operation") or "").lower()
        gcp_region = r.get("gcp_region")
        blob = f"{ut} {op}"

        # Provisioned Throughput (MB/s-month) → passthrough
        if re.search(r"provisioned.?throughput|throughput.?capacity", blob):
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Filestore",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "storage",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "EFS Provisioned Throughput — Filestore includes throughput in capacity price; no separate charge to map",
                "mapping_confidence": 0.75,
            })
            continue

        # AWS Backup for EFS ("warm backup storage") is a distinct, much cheaper
        # charge type ($0.055/GB-mo) than primary EFS storage — routing it through
        # the same Basic SSD/HDD Filestore capacity SKU (~$0.20-0.30/GB-mo) as
        # primary storage was a ~6.5x over-projection. GCP has no direct backup-
        # tier equivalent at this price point, so passthrough at cost parity.
        if re.search(r"backup.?storage|warm.?backup", blob):
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Filestore",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "backup",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "EFS backup storage — no GCP equivalent at this price tier; passthrough at cost parity",
                "mapping_confidence": 0.60,
            })
            continue

        # Data access requests / IO charges → passthrough
        if re.search(r"data.?access|io.?request|meteredthroughput", blob):
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Filestore",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "storage",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "EFS data-access / I/O request charge — no direct Filestore equivalent; passthrough at cost parity",
                "mapping_confidence": 0.60,
            })
            continue

        # Storage rows — detect access tier
        service, sku_name, mult = _EFS_DEFAULT_STORAGE
        for key, val in _EFS_STORAGE_MAP.items():
            if key in blob:
                service, sku_name, mult = val
                break

        sku_id = resolve_sku(service, sku_name, gcp_region)
        # A cheaper-looking "Filestore Instance Capacity Zonal" tier exists
        # ($0.12/GiBy.mo vs Basic SSD's $0.30/GiBy.mo, confirmed via
        # find-sku.sh) but it is NOT a like-for-like capacity swap — Zonal is
        # a genuinely different, multi-component pricing shape: a fixed
        # ~$20/mo "Filestore Instance Count" fee PLUS a separate per-IOPS
        # charge on top of the cheaper capacity rate, vs Basic SSD's single
        # flat $/GiBy.mo rate covering everything. AWS EFS bills (General
        # Purpose / non-Provisioned-Throughput) typically only report GB-Mo
        # storage usage, with no discrete IOPS-provisioning figure to price
        # that component from — silently switching would either omit the
        # fixed+IOPS components (understating cost) or require evidence this
        # pipeline doesn't have. Per this project's evidence-vs-fabrication
        # principle, disclose it as an architecture-review option instead of
        # silently substituting.
        zonal_note = (" [architecture review recommended: GCP Filestore Zonal tier has a lower "
                      "per-GB rate ($0.12 vs $0.30/GiBy.mo here) but a different multi-component "
                      "pricing shape (fixed instance fee + separate IOPS charge) that this bill's "
                      "GB-Mo-only usage data can't fully price — verify total Zonal cost with "
                      "customer before switching]") if sku_name == "Filestore Capacity Basic SSD" else ""
        entry = {
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        service,
            "gcp_sku_name":       sku_name,
            "component":          "storage",
            "strategy":           "map" if sku_id else "passthrough",
            "unit_multiplier":    mult,
            "gcp_region":         gcp_region,
            "projection_note":    f"EFS storage → {sku_name}" + zonal_note + _no_rate_suffix(sku_id, gcp_region),
            "mapping_confidence": 0.75,
        }
        if sku_id:
            entry["gcp_sku_id"] = sku_id
            entry["gcp_sku_unit"] = sku_id.unit
        out.append(entry)
    return out


def map_fsx(rows):
    """FSx variants → Filestore or passthrough.

    FSx for Lustre → Filestore High Scale (closest performance tier).
    FSx for Windows → Filestore Enterprise (SMB-compatible).
    FSx for NetApp ONTAP, OpenZFS → passthrough (no direct GCP equivalent).
    Backup/snapshot rows → passthrough.
    """
    out = []
    for r in rows:
        product = (r.get("product") or "").lower()
        ut      = (r.get("usage_type") or "").lower()
        gcp_region = r.get("gcp_region")
        blob = f"{product} {ut}"

        # Backup rows → passthrough regardless of FSx type
        if "backup" in blob or "snapshot" in blob:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Filestore",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "storage",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    "FSx backup — no Filestore equivalent; passthrough at cost parity",
                "mapping_confidence": 0.70,
            })
            continue

        if "lustre" in blob:
            sku_name = "Filestore Capacity Zonal and High Scale"
            note = "FSx for Lustre → Filestore High Scale SSD (high-throughput parallel workloads)"
            confidence = 0.65
        elif "windows" in blob:
            sku_name = "Filestore Capacity Regional and Enterprise"
            note = "FSx for Windows → Filestore Enterprise (SMB-compatible; Kerberos/AD auth differs)"
            confidence = 0.65
        else:
            # NetApp ONTAP, OpenZFS — no direct GCP equivalent
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Cloud Filestore",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "storage",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    f"FSx ({r.get('product')!r}) — no direct GCP equivalent; passthrough at cost parity",
                "mapping_confidence": 0.50,
            })
            continue

        sku_id = resolve_sku("Cloud Filestore", sku_name, gcp_region)
        entry = {
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        "Cloud Filestore",
            "gcp_sku_name":       sku_name,
            "component":          "storage",
            "strategy":           "map" if sku_id else "passthrough",
            "unit_multiplier":    1.0,
            "gcp_region":         gcp_region,
            "projection_note":    note + _no_rate_suffix(sku_id, gcp_region),
            "mapping_confidence": confidence,
        }
        if sku_id:
            entry["gcp_sku_id"] = sku_id
            entry["gcp_sku_unit"] = sku_id.unit
        out.append(entry)
    return out


def map_xray(rows):
    """X-Ray → Cloud Trace.

    X-Ray: $5.00/million traces (first 100k free). Cloud Trace: $0.20/million spans.
    Unit models are compatible (count-based), though GCP spans are more granular than
    AWS traces. We map at unit_multiplier=1.0 and flag the rate difference — the GCP
    price is dramatically lower ($0.20 vs $5.00/M), so passthrough would overstate cost.
    """
    # "Trace Ingestion" never matched any real catalog SKU (confirmed via
    # find-sku.sh) — resolve_sku always returned None, so every X-Ray row
    # silently fell to strategy='passthrough' (AWS-cost parity) instead of
    # applying the real, much cheaper Cloud Trace rate this function's own
    # docstring says it should. Real (only) SKU is "Spans ingested",
    # $0.20/M spans.
    _TRACE_SKU = "Spans ingested"
    out = []
    for r in rows:
        gcp_region = r.get("gcp_region")
        sku_id = resolve_sku("Cloud Trace", _TRACE_SKU, gcp_region)
        entry = {
            "aws_li_key":         r["aws_li_key"],
            "gcp_service":        "Cloud Trace",
            "gcp_sku_name":       _TRACE_SKU,
            "component":          "tracing",
            "strategy":           "map" if sku_id else "passthrough",
            "unit_multiplier":    1.0,
            "gcp_region":         gcp_region,
            "projection_note":    "AWS X-Ray → Cloud Trace ($0.20/M spans vs $5.00/M traces on AWS — GCP significantly cheaper)" + _no_rate_suffix(sku_id, gcp_region),
            "mapping_confidence": 0.75,
        }
        if sku_id:
            entry["gcp_sku_id"] = sku_id
            entry["gcp_sku_unit"] = sku_id.unit
        out.append(entry)
    return out


# EMR instance type → vCPU count. Edit data/instance-specs.json → emr_vcpu_map to add types.
_EMR_VCPU_MAP = _inst_cfg.get("emr_vcpu_map", {})


def _emr_vcpus(usage_type, instance_vcpus):
    """Extract vCPU count for an EMR management fee row."""
    if instance_vcpus:
        try:
            return int(instance_vcpus)
        except (TypeError, ValueError):
            pass
    if not usage_type:
        return None
    # usage_type pattern: "m5.xlarge-EMR-CORE" or "USE2-m5.2xlarge-EMR-MASTER"
    m = re.search(r'([a-z][0-9][a-z0-9]*\.[a-z0-9]+)-EMR', usage_type, re.IGNORECASE)
    if m:
        return _EMR_VCPU_MAP.get(m.group(1).lower())
    return None


def map_emr(rows):
    """EMR management fee → Cloud Dataproc Premium.

    EMR charges a per-node-hour management fee on top of the underlying EC2 cost.
    The EC2 cost is captured separately by compute_breakdown. This mapper converts
    the EMR management premium to the Dataproc cluster service charge.

    Dataproc Premium: $0.01/vCPU-hr. We multiply the node-hours by vCPU count
    to get vCPU-hours, then apply the Dataproc Premium SKU rate.
    When the instance type is unknown, passthrough at cost parity with Dataproc label.
    """
    # "Dataproc Premium" under service GCP_DATAPROC ("Dataproc", the Dataproc
    # Serverless Batch billing service) never matched any real catalog SKU —
    # confirmed via find-sku.sh: the real cluster-mode management-fee SKU
    # ("Licensing Fee for Google Cloud Dataproc (CPU cost)", $0.01/h — matches
    # this function's own documented rate) is billed under service
    # "Compute Engine", resource_group "Dataproc", not the "Dataproc" service
    # at all. Both the service name AND the desc pattern were wrong, so
    # resolve_sku always returned None and every EMR management-fee row
    # silently fell to strategy='passthrough' (AWS-cost parity) instead of
    # the vCPU-scaled Dataproc rate this function claims to apply.
    _DATAPROC_PREMIUM_SERVICE = GCP_COMPUTE_ENGINE
    _DATAPROC_PREMIUM_SKU = r"Licensing Fee for Google Cloud Dataproc \(CPU cost\)"
    out = []
    for r in rows:
        ut = r.get("usage_type") or ""
        gcp_region = r.get("gcp_region")
        blob = f"{ut} {r.get('operation') or ''} {r.get('product') or ''}".lower()

        # Spot/preemptible rows and storage-only rows → passthrough
        if re.search(r"spot|storage|backup", blob):
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_DATAPROC,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "management",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    f"EMR spot/storage row — passthrough at cost parity (usage_type={ut!r})",
                "mapping_confidence": 0.55,
            })
            continue

        vcpus = _emr_vcpus(ut, r.get("instance_vcpus"))
        if vcpus:
            sku_id = resolve_sku(_DATAPROC_PREMIUM_SERVICE, _DATAPROC_PREMIUM_SKU, gcp_region)
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        _DATAPROC_PREMIUM_SERVICE,
                "gcp_sku_name":       "Licensing Fee for Google Cloud Dataproc (CPU cost)",
                "component":          "management",
                "strategy":           "map" if sku_id else "passthrough",
                "unit_multiplier":    float(vcpus),
                "gcp_region":         gcp_region,
                "projection_note":    f"EMR management fee → Dataproc Premium ({vcpus} vCPU × $0.01/hr per node-hour)" + _no_rate_suffix(sku_id, gcp_region),
                "mapping_confidence": 0.70,
            }
            if sku_id:
                entry["gcp_sku_id"] = sku_id
                entry["gcp_sku_unit"] = sku_id.unit
        else:
            entry = {
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_DATAPROC,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "management",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         gcp_region,
                "projection_note":    f"EMR management fee — vCPU count unknown for usage_type={ut!r}; passthrough at cost parity",
                "mapping_confidence": 0.45,
            }
        out.append(entry)
    return out


# MSK broker instance type → (vCPU, RAM_GiB). ARM (Graviton/m7g) and x86 (m5/t3) families.
# Map Kafka broker hours to self-hosted GCE using the same core+ram breakdown as EC2.
_MSK_VCPU_RAM: dict[str, tuple[int, float]] = {
    # t3 family (burstable, small brokers)
    "t3.small":   (2,  2.0),  "t3.medium":  (2,  4.0),
    # m5 / m5.large family (x86)
    "m5.large":   (2,  8.0),  "m5.xlarge":  (4, 16.0),  "m5.2xlarge": (8, 32.0),
    "m5.4xlarge": (16, 64.0), "m5.12xlarge":(48,192.0), "m5.24xlarge":(96,384.0),
    # m7g family (Graviton3, ARM)
    "m7g.large":  (2,  8.0),  "m7g.xlarge": (4, 16.0),  "m7g.2xlarge":(8, 32.0),
    "m7g.4xlarge":(16, 64.0), "m7g.8xlarge":(32,128.0), "m7g.12xlarge":(48,192.0),
    "m7g.16xlarge":(64,256.0),
    # m6g family (Graviton2)
    "m6g.large":  (2,  8.0),  "m6g.xlarge": (4, 16.0),  "m6g.2xlarge":(8, 32.0),
    "m6g.4xlarge":(16, 64.0), "m6g.8xlarge":(32,128.0), "m6g.12xlarge":(48,192.0),
    "m6g.16xlarge":(64,256.0),
    # r5 / r7g (memory-optimised, larger brokers)
    "r5.large":   (2, 16.0),  "r5.xlarge":  (4, 32.0),  "r5.2xlarge": (8, 64.0),
    "r5.4xlarge": (16,128.0), "r5.8xlarge": (32,256.0),
    "r7g.large":  (2, 16.0),  "r7g.xlarge": (4, 32.0),  "r7g.2xlarge":(8, 64.0),
    "r7g.4xlarge":(16,128.0), "r7g.8xlarge":(32,256.0),
}

# Region prefix embedded in AWS usage_type (e.g. "APS5-Kafka.t3.small") → GCP region.
# ingest.py now decodes this universally for every row at ingestion time (Step 5,
# region assignment) — before any mapper runs — so by the time rows reach this
# file gcp_region should already be resolved. These per-mapper calls remain as
# defense-in-depth for any row whose gcp_region gets reset to 'global' later in
# the pipeline (e.g. an LLM-provided override). Shared with ingest.py via
# region_prefix.py so the prefix table has exactly one definition.
from region_prefix import UT_PREFIX_TO_GCP as _UT_PREFIX_TO_GCP, UT_PREFIX_RE as _UT_PREFIX_RE
_MSK_UT_PREFIX_TO_GCP = _UT_PREFIX_TO_GCP  # backward-compat alias

_MSK_UT_INSTANCE_RE = re.compile(r"Kafka\.([a-z][0-9a-z]+\.[0-9]*x?(?:small|medium|large))", re.IGNORECASE)
_MSK_UT_PREFIX_RE   = _UT_PREFIX_RE  # same regex, kept for backward compat


def map_msk(rows: list) -> list:
    """MSK broker-hours → self-hosted Kafka on GCE (core + ram breakdown).

    Deterministic alternative to the LLM compute_breakdown path.  Parses the
    Kafka instance type from usage_type (e.g. 'APS5-Kafka.t3.small'), looks up
    vCPU/RAM from _MSK_VCPU_RAM, and emits the same core+ram component pair that
    the EC2 compute_breakdown mapper would produce.  Falls back to passthrough with
    a Compute Engine label when the instance type is not in the table.
    """
    out = []
    for r in rows:
        ut     = r.get("usage_type") or ""
        region = r.get("gcp_region") or ""

        # Derive GCP region from usage_type prefix when ingest left it as 'global' or blank.
        if not region or region == "global":
            m = _MSK_UT_PREFIX_RE.match(ut)
            if m:
                region = _MSK_UT_PREFIX_TO_GCP.get(m.group(1).lower(), region)

        # Extract instance type: "APS5-Kafka.t3.small" → "t3.small"
        m = _MSK_UT_INSTANCE_RE.search(ut)
        itype = m.group(1).lower() if m else None
        specs = _MSK_VCPU_RAM.get(itype) if itype else None

        if specs is None:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Compute Engine",
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "broker",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region or r.get("gcp_region"),
                "projection_note":    (f"MSK broker {itype or ut!r} not in instance table — "
                                       "passthrough at cost parity with Compute Engine label"),
                "mapping_confidence": 0.40,
            })
            continue

        vcpu, ram_gib = specs
        is_arm_burstable = itype.split(".")[0] == "t4g"
        is_arm_sustained = itype.split(".")[0] in ("m7g", "m6g", "r7g", "c7g", "c6g")
        is_arm = is_arm_burstable or is_arm_sustained
        # t3 is AWS's burstable broker family; m5/r5/m6g/m7g/r7g are sustained.
        # The old code resolved both through a single "N2 Instance Core|E2
        # Instance Core" regex-OR pattern — whichever the catalog scan hit
        # first won, non-deterministically, regardless of whether the source
        # broker was actually burstable. A sustained r5/m5 broker could land
        # on E2 (a real performance-tier downgrade) purely by catalog list
        # order. Branch explicitly instead, same as map_compute_windows.
        is_burstable = itype.split(".")[0] in ("t3", "t2")
        cost_tag = ""

        if is_arm:
            # Was hardcoded to "C4A Arm Instance Core/Ram" directly for EVERY
            # ARM broker (t4g included, lumped in with sustained m6g/m7g/r6g/
            # r7g/c6g/c7g under the same branch) — no cheapest_in_scope()
            # sweep, no workload-tier awareness, and t4g (burstable ARM)
            # incorrectly forced onto C4A (sustained ARM) instead of its own
            # native burstable family T2A Arm. Same bug class already fixed
            # in map_compute_arm()/map_compute_burstable() — fixed the same
            # way here: sweep real ARM candidates for the correct tier, fall
            # back to same-tier x86 only if no ARM family is available in
            # region, and disclose (never silently switch) a cheaper x86
            # alternative when a real ARM SKU was found.
            arm_default = "T2A Arm" if is_arm_burstable else "C4A Arm"
            arm_tiers = ("burstable", "sustained") if is_arm_burstable else ("sustained",)
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                arm_default, vcpu, ram_gib, region, archs=("arm",), tiers=arm_tiers,
                workloads=_arm_workloads())
            # _strict_resolve_sku — see its docstring: plain resolve_sku()
            # could continent-fallback to a different region's SKU/price for
            # an ARM family cheapest_in_scope() already confirmed unavailable
            # here, which would make `found_arm` wrongly True below.
            cpu_sku = _strict_resolve_sku("Compute Engine", cpu_desc, region) if cpu_desc else None
            ram_sku = _strict_resolve_sku("Compute Engine", ram_desc, region) if ram_desc else None
            found_arm = bool(cpu_sku and ram_sku)
            if found_arm:
                if switched:
                    cost_tag = (f" [cost-tier: {family_label} cheaper here, same tier as {arm_default}]" if reason == "cheaper"
                                else f" [cost-tier: {arm_default} unavailable in region — {family_label} used instead, same tier]")
            else:
                # BUG (found during a repo-wide audit after fixing the same
                # class of issue in family_mapper.py's map_gce_row() and
                # apply_static_mappings.py's map_compute_arm()): this used to
                # silently REPRICE the row onto x86 (E2/N2D AMD) here — even
                # for is_arm_burstable, where the tier crossing (burstable->
                # sustained) is safe but the ARCHITECTURE crossing (ARM->x86)
                # is not, same reasoning as every other ARM/x86 boundary in
                # this file. "No ARM family available here" is a customer
                # decision, never a silent substitution — passthrough +
                # disclosure only.
                cpu_sku = ram_sku = None
                x86_default = "E2" if is_arm_burstable else "N2D AMD"
                x86_tiers = ("burstable", "sustained") if is_arm_burstable else ("sustained",)
                family_label = "no GCP rate found in region"
                x86_label, x86_core, x86_ram, _, _ = cheapest_in_scope(
                    x86_default, vcpu, ram_gib, region, archs=("x86",), tiers=x86_tiers)
                x86_core_rate = _family_hourly_rate("Compute Engine", x86_core, region)
                x86_ram_rate = _family_hourly_rate("Compute Engine", x86_ram, region)
                if x86_core_rate is not None and x86_ram_rate is not None:
                    x86_total = vcpu * x86_core_rate + ram_gib * x86_ram_rate
                    cost_tag = (f" [architecture review recommended: no ARM family available in this "
                                f"region; x86 {x86_label} would cost ~${x86_total:.4f}/hr here — not "
                                "switched automatically since Graviton/ARM binaries aren't x86-compatible "
                                "without a rebuild; confirm with customer whether x86 is viable, or "
                                "whether a different region is acceptable]")

            if found_arm:
                x86_label, x86_core, x86_ram, _, _ = cheapest_in_scope(
                    "E2" if is_arm_burstable else "N2D AMD", vcpu, ram_gib, region,
                    archs=("x86",), tiers=("burstable", "sustained") if is_arm_burstable else ("sustained",))
                arm_core_rate = _family_hourly_rate("Compute Engine", cpu_desc, region)
                arm_ram_rate = _family_hourly_rate("Compute Engine", ram_desc, region)
                x86_core_rate = _family_hourly_rate("Compute Engine", x86_core, region)
                x86_ram_rate = _family_hourly_rate("Compute Engine", x86_ram, region)
                if None not in (arm_core_rate, arm_ram_rate, x86_core_rate, x86_ram_rate):
                    arm_total = vcpu * arm_core_rate + ram_gib * arm_ram_rate
                    x86_total = vcpu * x86_core_rate + ram_gib * x86_ram_rate
                    if x86_total < arm_total:
                        pct = round((1 - x86_total / arm_total) * 100)
                        cost_tag += (f" [architecture review recommended: x86 {x86_label} would be "
                                     f"~{pct}% cheaper here than {family_label} — not switched automatically "
                                     "since Graviton/ARM binaries aren't x86-compatible without a rebuild; "
                                     "confirm with customer whether x86 is viable]")
        elif is_burstable:
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "E2", vcpu, ram_gib, region, archs=("x86",), tiers=("burstable", "sustained"))
            cpu_sku = _strict_resolve_sku("Compute Engine", cpu_desc, region)
            ram_sku = _strict_resolve_sku("Compute Engine", ram_desc, region)
            if switched:
                cost_tag = (f" [cost-tier: {family_label} cheaper than E2 here, same/better perf — AWS credits don't lower price]" if reason == "cheaper"
                            else f" [cost-tier: E2 unavailable in region — {family_label} used instead, same/better perf]")
        else:
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "N2", vcpu, ram_gib, region, archs=("x86",), tiers=("sustained",))
            cpu_sku = _strict_resolve_sku("Compute Engine", cpu_desc, region)
            ram_sku = _strict_resolve_sku("Compute Engine", ram_desc, region)
            if switched:
                cost_tag = (f" [cost-tier: {family_label} cheaper here, same tier as default]" if reason == "cheaper"
                            else f" [cost-tier: default unavailable in region — {family_label} used instead, same tier]")

        for comp, sku, mult, desc in (
            ("core", cpu_sku, float(vcpu),    cpu_desc),
            ("ram",  ram_sku, float(ram_gib), ram_desc),
        ):
            strategy = "map" if sku else "passthrough"
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        "Compute Engine",
                "gcp_sku_id":         sku if sku else None,
                "gcp_sku_name":       desc,
                "component":          comp,
                "strategy":           strategy,
                "unit_multiplier":    mult,
                "gcp_region":         region or r.get("gcp_region"),
                "projection_note":    (f"MSK {itype} broker → GCE self-hosted Kafka; "
                                       f"{vcpu} vCPU / {ram_gib} GiB RAM; "
                                       f"component={comp}{cost_tag}"),
                "mapping_confidence": 0.72,
            })
    return out


# ---------------------------------------------------------------------------
# ARM (Graviton) EC2 static mapper
# ---------------------------------------------------------------------------
# Formula-driven — no per-instance-type dict. vCPU derived from AWS standard
# size names; RAM derived from the family's GiB-per-vCPU ratio. C4A is tried
# first via the catalog; if the SKU doesn't exist in the target region, N2D AMD
# is used instead. No hardcoded region lists — availability is read from the
# catalog's serviceRegions field on every resolve_sku call.

# Standard AWS size → vCPU count (same across all Graviton families)
_ARM_SIZE_VCPU: dict[str, int] = {
    "nano": 2, "micro": 2, "small": 2, "medium": 2, "large": 2,
    "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "8xlarge": 32,
    "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "32xlarge": 128,
    "48xlarge": 192, "metal": 64,
}
# "medium" is 2 vCPU only for t4g (part of its irregular burstable sizing
# schedule, along with nano/micro/small below "large" — all genuinely 2 vCPU
# for t4g). Every other Graviton family (m6g/m7g/c6g/c7g/r6g/r7g/etc.) is
# genuinely 1 vCPU at "medium" — confirmed real: an m7g.medium row was priced
# at 2 vCPU (double the real spec) until an LLM outlier-fix caught it by hand.
# nano/micro/small aren't offered at all outside t4g, so they're never queried
# for another family in practice — only "medium" needed this override.
_ARM_SIZE_VCPU_NON_T4G_OVERRIDE: dict[str, int] = _inst_cfg.get("arm_size_vcpu_non_t4g_override", {"medium": 1})
_ARM_RAM_RATIO: dict[str, float]                 = _inst_cfg.get("arm_ram_ratio",                 {"c": 2.0, "m": 4.0, "r": 8.0, "x": 16.0, "i": 4.0, "h": 4.0})
_T4G_SIZE_RAM: dict[str, float]                  = _inst_cfg.get("t4g_size_ram",                  {"nano": 0.5, "micro": 1.0, "small": 2.0, "medium": 4.0, "large": 8.0, "xlarge": 16.0, "2xlarge": 32.0})

# Matches the Graviton family + size from usage_type (e.g. APS5-BoxUsage:m8g.2xlarge)
_ARM_UT_RE = re.compile(
    r'BoxUsage:([a-z]\d[a-z0-9]*g[a-z0-9]*)\.([a-z0-9]+)',
    re.IGNORECASE,
)


def _arm_specs(family: str, size: str):
    """Return (vcpu, ram_gib) or None when the combination is unknown."""
    fam = family.lower()
    is_t4g = fam.startswith("t") and fam.endswith("g")
    if is_t4g:
        vcpu = _ARM_SIZE_VCPU.get(size)
    else:
        vcpu = _ARM_SIZE_VCPU_NON_T4G_OVERRIDE.get(size, _ARM_SIZE_VCPU.get(size))
    if vcpu is None:
        return None
    if is_t4g:
        ram = _T4G_SIZE_RAM.get(size)
        return (vcpu, ram) if ram is not None else None
    ratio = _ARM_RAM_RATIO.get(fam[0])
    if ratio is None:
        return None
    return vcpu, float(vcpu) * ratio


# ---------------------------------------------------------------------------
# Burstable EC2 static mapper (t2/t3/t3a/t4g)
# ---------------------------------------------------------------------------
# t2/t3/t3a default to E2; t4g (ARM/Graviton) defaults to its own native
# burstable family, T2A Arm — NOT silently cross-architected to E2/x86 (a
# genuine binary-compatibility boundary, same principle applied everywhere
# else in this file). x86 is only ever surfaced as a disclosed option when
# genuinely cheaper, never switched to silently. Neither GCP family has a
# burst-credit billing model (the AWS CPUCredits rows are already ignored by
# the non_workload rule) — that's a pricing-model fact, not license to treat
# architecture as freely interchangeable.
# Specs: AWS t-family vCPU and RAM counts per instance size. Edit data/instance-specs.json to add types.
_BURSTABLE_EC2_SPECS: dict[str, tuple[int, float]] = {
    k: tuple(v) for k, v in _inst_cfg.get("burstable_ec2_specs", {}).items()
}

_BURST_ITYPE_RE = re.compile(
    r'\b(t[234][ag]?\.(?:[0-9]+x)?(?:xlarge|large|medium|small|micro|nano))\b',
    re.IGNORECASE,
)


def map_opensearch(rows: list[dict]) -> list[dict]:
    """OpenSearch rows are handled by the Phase 2 LLM sub-agent with structured guidance.

    This function is intentionally empty — it writes an empty mappings file so the
    orchestrator sees the group as 'processed', while the rows remain in the Phase 2
    manifest for the dedicated opensearch LLM agent to handle.
    """
    return []


def map_compute_burstable(rows: list[dict]) -> list[dict]:
    """Map t-family (t2/t3/t3a/t4g) EC2 hours to the cheaper of E2 (GCP's
    burstable-class family) or a sustained-performance alternative (N4D),
    compared on REAL per-region rates via cheapest_family().

    AWS CPU credits are a performance-availability mechanism, not a pricing
    discount — the AWS bill never gets cheaper for staying within baseline
    CPU, so there is no scenario where E2 is a strictly better deal than a
    cheaper sustained family meeting the same vCPU/RAM spec. Defaulting to E2
    unconditionally missed real savings: for an 8vCPU/32GB spec this session
    confirmed N4D undercuts E2 by ~20% in asia-south1 while costing ~9% MORE
    in asia-south2 — genuinely region-dependent, which is exactly why this
    must be a real rate comparison rather than a fixed family choice either
    way. Unit multipliers: vcpu count for core SKU, ram_gib for ram SKU.
    """
    out = []
    for r in rows:
        ut     = r.get("usage_type") or ""
        itype  = (r.get("instance_type") or "").lower().strip()
        region = r.get("gcp_region") or "global"

        # Prefer instance_type column; fall back to regex parse of usage_type
        if itype not in _BURSTABLE_EC2_SPECS:
            m = _BURST_ITYPE_RE.search(ut)
            itype = m.group(1).lower() if m else ""

        specs = _BURSTABLE_EC2_SPECS.get(itype)
        if specs is None:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "instance",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region,
                "projection_note":    f"Burstable EC2 {itype or ut[:40]!r}: size not in table — passthrough",
                "mapping_confidence": 0.30,
            })
            continue

        vcpu, ram_gib = specs
        # This is the ACTUAL, primary handler for burstable EC2 rows —
        # classify_mechanics.py routes t2/t3/t3a/t4g rows here BEFORE they
        # ever reach family_mapper.py's map_gce_row(), which only sees
        # burstable rows as a rarely-hit safety net. Confirmed real, severe
        # bug: this function's cheapest_in_scope() call was hardcoded to
        # archs=("x86",) regardless of the SOURCE instance's own
        # architecture — every real t4g (Graviton/ARM) burstable row was
        # being SILENTLY force-mapped onto an x86 GCP family (E2/N2D/etc.),
        # never even trying the native ARM (T2A) target first, with zero
        # disclosure that an architecture crossing happened at all. Fixed to
        # default to the row's own native architecture (never silently
        # cross it), then disclose (never silently switch) if the OTHER
        # architecture would be cheaper — same principle as the
        # architecture-review disclosures elsewhere in this codebase: ARM vs
        # x86 is a real binary-compatibility boundary a customer must
        # confirm, not something safe to swap into on cost alone.
        is_arm_source = itype.split(".")[0] == "t4g"
        native_default = "T2A Arm" if is_arm_source else "E2"
        native_archs = ("arm",) if is_arm_source else ("x86",)
        family_label, core_desc, ram_desc, switched, reason = cheapest_in_scope(
            native_default, vcpu, ram_gib, region, archs=native_archs, tiers=("burstable", "sustained"))
        # _strict_resolve_sku, not resolve_sku — see its docstring: a plain
        # resolve_sku() call could continent-fallback to a different region's
        # SKU/price for a family cheapest_in_scope() already confirmed (via
        # strict exact-region matching) has no rate here at all.
        cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, core_desc, region)
        ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region)

        if switched and reason == "cheaper":
            note = (f"{itype} → {family_label} ({vcpu} vCPU / {ram_gib:.1f} GiB)"
                    f" [cost-tier: {family_label} cheaper than {native_default} here, same/better perf — "
                    f"AWS credits don't lower price]")
        elif switched:
            note = (f"{itype} → {family_label} ({vcpu} vCPU / {ram_gib:.1f} GiB)"
                    f" [cost-tier: {native_default} unavailable in region — {family_label} used instead, "
                    f"same/better perf]")
        else:
            note = f"{itype} → {native_default} (burstable); {vcpu} vCPU / {ram_gib:.1f} GiB"

        # Disclose (never silently switch) the OTHER architecture if it
        # would be cheaper for this same spec — mirrors the disclosure
        # already applied for this exact crossing in family_mapper.py's
        # map_gce_row(), kept in sync here since this is the function real
        # burstable rows actually reach.
        other_archs = ("x86",) if is_arm_source else ("arm",)
        other_default = "E2" if is_arm_source else "T2A Arm"
        other_label = _gp_family_by_label(other_default)
        if other_label:
            _, ocore, oram, _, _, _, _ = other_label
            ocore_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ocore, region)
            oram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, oram, region)
            fcore_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, core_desc, region)
            fram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ram_desc, region)
            if None not in (ocore_rate, oram_rate, fcore_rate, fram_rate):
                other_total = vcpu * ocore_rate + ram_gib * oram_rate
                chosen_total = vcpu * fcore_rate + ram_gib * fram_rate
                if other_total < chosen_total:
                    pct = round((1 - other_total / chosen_total) * 100)
                    from_arch, to_arch = ("ARM", "x86") if is_arm_source else ("x86", "ARM")
                    note += (f" [architecture review recommended: {to_arch} {other_default} would be "
                             f"~{pct}% cheaper here than {family_label} — not switched automatically "
                             f"since {from_arch} binaries aren't {to_arch}-compatible without a rebuild; "
                             "confirm with customer whether this architecture is viable]")

        for comp, sku, mult, desc in (
            ("core", cpu_sku, float(vcpu),    core_desc),
            ("ram",  ram_sku, float(ram_gib), ram_desc),
        ):
            strategy = "map" if sku else "passthrough"
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         sku if sku else None,
                "gcp_sku_name":       desc if sku else f"{family_label} {comp}",
                "component":          comp,
                "strategy":           strategy,
                "unit_multiplier":    mult,
                "gcp_region":         region,
                "projection_note":    note,
                "mapping_confidence": 0.85,
            })
    return out


def map_compute_arm(rows: list[dict]) -> list[dict]:
    """
    Static mapper for Graviton (ARM) EC2 instance-hours.

    For each row:
      1. Parse family (e.g. m8g) and size (e.g. 2xlarge) from usage_type.
      2. Derive vCPU and RAM GiB from formula (no per-instance lookup table).
      3. Sweep real ARM general-purpose families (C4A Arm, N4A) for the
         target GCP region/spec and use the cheapest one actually priced
         there. If NO ARM family has a rate in this region at all, the row
         goes to passthrough with a disclosed (never auto-applied) x86 cost
         estimate — ARM vs x86 is an instruction-set boundary, not a cost
         optimization, so it's always a customer decision, never a silent
         substitution. Region availability comes from the catalog — no
         hardcoded lists (except a small, explicit exceptions table in
         _KNOWN_PHANTOM_AVAILABILITY for confirmed cases where the catalog's
         own region listing is itself wrong).
      4. Emit core + ram components (strategy='map') or passthrough on failure.
    """
    out = []
    for r in rows:
        ut     = r.get("usage_type") or ""
        region = r.get("gcp_region") or "global"

        m = _ARM_UT_RE.search(ut)
        if not m:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "instance",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region,
                "projection_note":    f"ARM EC2 usage_type not parseable: {ut[:60]}",
                "mapping_confidence": 0.30,
            })
            continue

        family = m.group(1).lower()
        size   = m.group(2).lower()
        specs  = _arm_specs(family, size)
        itype  = f"{family}.{size}"

        if specs is None:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "instance",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region,
                "projection_note":    f"ARM EC2 {itype}: size/family not in formula tables — passthrough",
                "mapping_confidence": 0.30,
            })
            continue

        vcpu, ram_gib = specs

        # t4g is BURSTABLE (AWS CPU-credit family), unlike m7g/c7g/r7g/etc
        # which are sustained-performance Graviton. Per the Performance-Tier
        # Safety Rule, a burstable source may move to ANY cheaper GCP option
        # — including crossing out of the "ARM-native" default — because AWS
        # never charges less for staying within baseline; there is no
        # performance guarantee being silently downgraded. Every candidate
        # family comes from the single `_GP_FAMILIES` registry (not a local
        # hardcoded list) so a family added there later is automatically
        # considered here too, with no risk of this branch's list quietly
        # falling out of sync with everyone else's.
        if family == "t4g":
            # BUG (found during a repo-wide audit after fixing the same class
            # of issue in family_mapper.py's map_gce_row()): this used to
            # sweep archs=("x86","arm") TOGETHER in one call — if an x86
            # family won the sweep, it got silently priced and used, no
            # disclosure at all. Burstable-vs-sustained is a performance-tier
            # axis (safe to cross freely per the Performance-Tier Safety
            # Rule — AWS CPU credits never make staying idle cheaper), but
            # ARM-vs-x86 is a SEPARATE, orthogonal axis (instruction-set
            # compatibility) that's never safe to cross silently regardless
            # of tier — same reasoning already applied to every other
            # ARM/x86 boundary in this file and in family_mapper.py's
            # is_burstable ARM handling. Fixed to sweep ARM only, then
            # disclose (never auto-switch) a cheaper x86 alternative.
            fam_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "C4A Arm", vcpu, ram_gib, region,
                archs=("arm",), tiers=("burstable", "sustained"))
            cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, cpu_desc, region) if cpu_desc else None
            ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region) if ram_desc else None
            cost_tag = ""
            if cpu_sku and ram_sku:
                gcp_family = fam_label
                if switched:
                    cost_tag = (" [cost-tier: t4g burstable — AWS CPU credits don't lower AWS price, "
                                "so cheapest available ARM family is a pure win, no performance downside]")
                x86_label, x86_core, x86_ram, _, _ = cheapest_in_scope(
                    "N2D AMD", vcpu, ram_gib, region, archs=("x86",), tiers=("burstable", "sustained"))
                arm_core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, cpu_desc, region)
                arm_ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ram_desc, region)
                x86_core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_core, region)
                x86_ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_ram, region)
                if None not in (arm_core_rate, arm_ram_rate, x86_core_rate, x86_ram_rate):
                    arm_total = vcpu * arm_core_rate + ram_gib * arm_ram_rate
                    x86_total = vcpu * x86_core_rate + ram_gib * x86_ram_rate
                    if x86_total < arm_total:
                        pct = round((1 - x86_total / arm_total) * 100)
                        cost_tag += (f" [architecture review recommended: x86 {x86_label} would be "
                                     f"~{pct}% cheaper here than {fam_label} — not switched automatically "
                                     "since Graviton/ARM binaries aren't x86-compatible without a rebuild; "
                                     "confirm with customer whether x86 is viable]")
            else:
                cpu_sku = ram_sku = None
                gcp_family = "no GCP rate found in region"
        else:
            # Was hardcoded to always try "C4A Arm Instance Core/Ram" directly,
            # with NO cheapest_in_scope() sweep and NO workload-tier awareness at
            # all — a memory-oriented Graviton source (r6g/r7g) or general-purpose
            # one (m6g/m7g) was force-mapped onto C4A regardless of whether N4A
            # (GCP's other real general-purpose ARM family) might be available/
            # cheaper, and never disclosed a cheaper x86 sustained alternative
            # either (unlike every other compute mapper in this file). Fixed to
            # do a real sweep — archs=("arm",) so a sustained Graviton source
            # never silently downgrades to T2A (burstable ARM, now correctly
            # tagged in _GP_FAMILY_TIER) — and to disclose (never silently
            # switch) a cheaper x86 alternative, matching the same
            # architecture-review pattern used everywhere else for this exact
            # crossing.
            #
            # workloads uses _arm_workloads() (every workload tag actually
            # discovered among arch='arm' families — currently general/
            # compute/memory from N4A/T2A Arm/C4A Arm) rather than this
            # family's own AWS-side letter (m=general/c=compute/r=memory),
            # because GCP's ARM lineup doesn't split by workload as granularly
            # as x86 does — C4A Arm and N4A are both legitimate real
            # candidates for any Graviton workload category regardless of
            # which single tag each carries; letting real per-region
            # availability/price decide is more useful than a workload filter
            # that would exclude a perfectly valid, cheaper option.
            fam_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "C4A Arm", vcpu, ram_gib, region, archs=("arm",), tiers=("sustained",),
                workloads=_arm_workloads())
            cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, cpu_desc, region) if cpu_desc else None
            ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region) if ram_desc else None
            cost_tag = ""
            found_arm = bool(cpu_sku and ram_sku)
            if found_arm:
                gcp_family = fam_label
                if switched:
                    cost_tag = (f" [cost-tier: {fam_label} cheaper here, same tier as C4A Arm]" if reason == "cheaper"
                                else f" [cost-tier: C4A Arm unavailable in region — {fam_label} used instead, same tier]")
            else:
                # BUG (found during a repo-wide audit after fixing the same
                # class of issue in family_mapper.py's map_gce_row(), the
                # Delhi/asia-south2 case): when no ARM general-purpose family
                # (C4A Arm or N4A) had any rate in this region at all, this
                # used to silently REPRICE the row as x86 N2D AMD — a real
                # architecture-crossing violation identical to the one
                # already fixed elsewhere, just in a different function. "No
                # ARM family available here" is not a cost optimization to
                # act on, it's a customer decision (different region, or
                # accept the x86 rebuild) — passthrough + disclosure only,
                # matching every other ARM/x86 boundary in this file.
                cpu_sku = ram_sku = None
                gcp_family = "no GCP rate found in region"
                x86_label, x86_core, x86_ram, _, _ = cheapest_in_scope(
                    "N2D AMD", vcpu, ram_gib, region, archs=("x86",), tiers=("sustained",))
                x86_core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_core, region)
                x86_ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_ram, region)
                if x86_core_rate is not None and x86_ram_rate is not None:
                    x86_total = vcpu * x86_core_rate + ram_gib * x86_ram_rate
                    cost_tag = (f" [architecture review recommended: no ARM family available in this "
                                f"region; x86 {x86_label} would cost ~${x86_total:.4f}/hr here — not "
                                "switched automatically since Graviton/ARM binaries aren't x86-compatible "
                                "without a rebuild; confirm with customer whether x86 is viable, or "
                                "whether a different region is acceptable]")

            # Disclose (never silently switch) a cheaper x86 alternative when a
            # real ARM SKU WAS found above — mirrors the disclosure already
            # applied for this exact crossing in family_mapper.py's
            # map_gce_row() and apply_static_mappings.py's
            # map_compute_burstable().
            if found_arm:
                x86_label, x86_core, x86_ram, x86_switched, _ = cheapest_in_scope(
                    "N2D AMD", vcpu, ram_gib, region, archs=("x86",), tiers=("sustained",))
                arm_core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, cpu_desc, region)
                arm_ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, ram_desc, region)
                x86_core_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_core, region)
                x86_ram_rate = _family_hourly_rate(GCP_COMPUTE_ENGINE, x86_ram, region)
                if None not in (arm_core_rate, arm_ram_rate, x86_core_rate, x86_ram_rate):
                    arm_total = vcpu * arm_core_rate + ram_gib * arm_ram_rate
                    x86_total = vcpu * x86_core_rate + ram_gib * x86_ram_rate
                    if x86_total < arm_total:
                        pct = round((1 - x86_total / arm_total) * 100)
                        cost_tag += (f" [architecture review recommended: x86 {x86_label} would be "
                                     f"~{pct}% cheaper here than {fam_label} — not switched automatically "
                                     "since Graviton/ARM binaries aren't x86-compatible without a rebuild; "
                                     "confirm with customer whether x86 is viable]")

        for comp, sku, mult, desc in (
            ("core", cpu_sku, float(vcpu),    cpu_desc),
            ("ram",  ram_sku, float(ram_gib), ram_desc),
        ):
            strategy = "map" if sku else "passthrough"
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         sku if sku else None,
                "gcp_sku_name":       desc if sku else f"{gcp_family} {comp}",
                "component":          comp,
                "strategy":           strategy,
                "unit_multiplier":    mult,
                "gcp_region":         region,
                "projection_note":    (
                    f"{itype} → {gcp_family}; {vcpu} vCPU / {ram_gib:.1f} GiB RAM{cost_tag}"
                ),
                "mapping_confidence": 0.82,
            })
    return out


# ---------------------------------------------------------------------------
# Windows EC2 static mapper
# ---------------------------------------------------------------------------
# EC2 instance type → (vCPU, RAM_GiB). Covers all instance types commonly
# seen in Windows-licensed EC2 billing. Missing types fall to passthrough.
_WIN_EC2_SPECS: dict[str, tuple[int, float]] = {
    # t2 family
    "t2.nano": (1, 0.5), "t2.micro": (1, 1.0), "t2.small": (1, 2.0),
    "t2.medium": (2, 4.0), "t2.large": (2, 8.0), "t2.xlarge": (4, 16.0),
    "t2.2xlarge": (8, 32.0),
    # t3 family
    "t3.nano": (2, 0.5), "t3.micro": (2, 1.0), "t3.small": (2, 2.0),
    "t3.medium": (2, 4.0), "t3.large": (2, 8.0), "t3.xlarge": (4, 16.0),
    "t3.2xlarge": (8, 32.0),
    # t3a family
    "t3a.nano": (2, 0.5), "t3a.micro": (2, 1.0), "t3a.small": (2, 2.0),
    "t3a.medium": (2, 4.0), "t3a.large": (2, 8.0), "t3a.xlarge": (4, 16.0),
    "t3a.2xlarge": (8, 32.0),
    # m4 family
    "m4.large": (2, 8.0), "m4.xlarge": (4, 16.0), "m4.2xlarge": (8, 32.0),
    "m4.4xlarge": (16, 64.0), "m4.10xlarge": (40, 160.0), "m4.16xlarge": (64, 256.0),
    # m5 family
    "m5.large": (2, 8.0), "m5.xlarge": (4, 16.0), "m5.2xlarge": (8, 32.0),
    "m5.4xlarge": (16, 64.0), "m5.8xlarge": (32, 128.0), "m5.12xlarge": (48, 192.0),
    "m5.16xlarge": (64, 256.0), "m5.24xlarge": (96, 384.0),
    # m5a family
    "m5a.large": (2, 8.0), "m5a.xlarge": (4, 16.0), "m5a.2xlarge": (8, 32.0),
    "m5a.4xlarge": (16, 64.0), "m5a.12xlarge": (48, 192.0), "m5a.24xlarge": (96, 384.0),
    # m6i family
    "m6i.large": (2, 8.0), "m6i.xlarge": (4, 16.0), "m6i.2xlarge": (8, 32.0),
    "m6i.4xlarge": (16, 64.0), "m6i.8xlarge": (32, 128.0), "m6i.12xlarge": (48, 192.0),
    "m6i.16xlarge": (64, 256.0), "m6i.24xlarge": (96, 384.0), "m6i.32xlarge": (128, 512.0),
    # c4 family
    "c4.large": (2, 3.75), "c4.xlarge": (4, 7.5), "c4.2xlarge": (8, 15.0),
    "c4.4xlarge": (16, 30.0), "c4.8xlarge": (36, 60.0),
    # c5 family
    "c5.large": (2, 4.0), "c5.xlarge": (4, 8.0), "c5.2xlarge": (8, 16.0),
    "c5.4xlarge": (16, 32.0), "c5.9xlarge": (36, 72.0), "c5.18xlarge": (72, 144.0),
    # c5a family
    "c5a.large": (2, 4.0), "c5a.xlarge": (4, 8.0), "c5a.2xlarge": (8, 16.0),
    "c5a.4xlarge": (16, 32.0), "c5a.8xlarge": (32, 64.0), "c5a.12xlarge": (48, 96.0),
    # r5 family
    "r5.large": (2, 16.0), "r5.xlarge": (4, 32.0), "r5.2xlarge": (8, 64.0),
    "r5.4xlarge": (16, 128.0), "r5.8xlarge": (32, 256.0), "r5.12xlarge": (48, 384.0),
    # r5a family (AMD)
    "r5a.large": (2, 16.0), "r5a.xlarge": (4, 32.0), "r5a.2xlarge": (8, 64.0),
    "r5a.4xlarge": (16, 128.0), "r5a.8xlarge": (32, 256.0), "r5a.12xlarge": (48, 384.0),
    "r5a.16xlarge": (64, 512.0), "r5a.24xlarge": (96, 768.0),
    # r6i family
    "r6i.large": (2, 16.0), "r6i.xlarge": (4, 32.0), "r6i.2xlarge": (8, 64.0),
    "r6i.4xlarge": (16, 128.0), "r6i.8xlarge": (32, 256.0), "r6i.12xlarge": (48, 384.0),
}

# Windows Server license SKU. Previously hardcoded to Datacenter Edition
# ("9597-C24E-C305", "...Datacenter Edition (CPU cost)", scaled per-vCPU) —
# but AWS's generic "License Included" Windows product (no "Datacenter"
# anywhere in the product/operation text) is Standard edition, not
# Datacenter (Datacenter is a distinct, pricier AWS SKU/BYOL scenario, always
# named explicitly when it applies). Confirmed via find-sku.sh: the real
# Standard Edition SKU ("Licensing Fee for Windows Server 2019 Standard
# Edition on VM") is billed FLAT per VM-hour ("on VM"), not per-vCPU like
# Datacenter's "(CPU cost)" variant — using the wrong edition AND scaling it
# by vCPU count double-counted the gap, confirmed real: a 2-vCPU t2.medium
# Windows row that should cost ~$0.046/h flat for licensing was being priced
# at $0.046 × 2 = $0.092/h, roughly doubling the license component alone.
#
# Hardcoding the "2019" year has no cost-accuracy impact regardless of the
# AWS bill's actual Windows version: confirmed via find-sku.sh that every
# Standard Edition "on VM" SKU (2008, 2008 R2, 2012, 2012 R2, 2016, 2019,
# 2022) is priced identically at $0.046/h flat — GCP doesn't price this
# license by version.
#
# A genuinely cheaper option — BYOL (Bring Your Own License), $0.00/h — also
# exists in the real catalog, but is NEVER silently assumed here: CUR data
# alone can't confirm whether the customer holds an eligible portable license
# (Microsoft Software Assurance / License Mobility) to bring, and AWS's
# "License Included" billing model specifically means they're paying
# per-use through AWS, not that they lack one. Same evidence-vs-fabrication
# principle as elsewhere in this file — disclosed as a customer-verifiable
# option (see note below), never silently substituted.
_WIN_LICENSE_SKU = "1991-54F9-2129"
_WIN_BYOL_NOTE = (" [if you hold an eligible portable Windows Server license via Microsoft "
                  "Software Assurance/License Mobility, GCP's BYOL licensing option is $0/h — "
                  "verify eligibility with customer, not inferable from AWS billing data alone]")

# SQL Server license SKUs on Compute Engine (flat per-VM-hour, not per-vCPU).
# GCP uses two tiers: 1–4 vCPU and >4 vCPU. Source: GCP billing catalog.
# Standard edition: 2019 SKUs used as the canonical year-independent proxy
# (GCP prices Standard identically across 2014/2016/2017/2019/2022/2025).
_SQL_STD_SKU_LE4  = "6B90-18D5-E700"   # SQL Server 2019 Standard on VM, 1-4 vCPU
_SQL_STD_SKU_GT4  = "F685-956C-2D64"   # SQL Server 2019 Standard on VM, >4 vCPU
_SQL_ENT_SKU_LE4  = "0BA1-9A4F-3F6A"   # SQL Server 2017 Enterprise on VM, 1-4 vCPU
_SQL_ENT_SKU_GT4  = "0810-E21D-8709"   # SQL Server 2019 Enterprise on VM, >4 vCPU
_SQL_WEB_SKU_LE4  = None               # Web edition: not supported on standard VMs
_SQL_WEB_SKU_GT4  = None

_WIN_ITYPE_RE = re.compile(
    r'\b([a-z][0-9][a-z0-9]*\.(?:[0-9]+x)?(?:xlarge|large|medium|micro|small|nano|metal))\b',
    re.IGNORECASE,
)


def _detect_sql_server_sku(product: str, operation: str, vcpu: int) -> tuple[str | None, str]:
    """Return (sku_id, edition_label) for a SQL Server license component, or (None, '')."""
    blob = f"{product} {operation}".lower()
    if "sql server" not in blob and "sql std" not in blob and "sql ent" not in blob:
        return None, ""
    gt4 = vcpu > 4
    if "enterprise" in blob:
        sku = _SQL_ENT_SKU_GT4 if gt4 else _SQL_ENT_SKU_LE4
        label = "Enterprise"
    else:
        sku = _SQL_STD_SKU_GT4 if gt4 else _SQL_STD_SKU_LE4
        label = "Standard"
    vcpu_tier = f"{'more than' if gt4 else '1 to'} 4 vCPU"
    return sku, f"SQL Server 2019 {label} Edition license ({vcpu_tier})"


def map_compute_windows(rows: list[dict]) -> list[dict]:
    """
    Static mapper for Windows EC2 instance-hours.

    Emits three or four components per row:
      core       — E2/N2D vCPU-hours (same as Linux compute_breakdown)
      ram        — E2/N2D RAM GiB-hours
      license    — Windows Server 2019 Standard Edition license ($0.046/VM-h flat)
      sql_license — SQL Server license when product/operation mentions SQL Server

    The license components use global SKUs so gcp_region does not affect the rate.
    For instance types not in _WIN_EC2_SPECS, falls back to passthrough.
    """
    out: list[dict] = []
    for r in rows:
        op = r.get("operation") or ""
        ut = r.get("usage_type") or ""
        region = r.get("gcp_region") or ""

        # Derive region from usage_type prefix when blank/global
        if not region or region == "global":
            m = _UT_PREFIX_RE.match(ut)
            if m:
                region = _UT_PREFIX_TO_GCP.get(m.group(1).lower(), region)

        # Extract instance type from operation ("On Demand Windows t2.xlarge Instance Hour")
        m = _WIN_ITYPE_RE.search(op) or _WIN_ITYPE_RE.search(ut)
        itype = m.group(1).lower() if m else None
        specs = _WIN_EC2_SPECS.get(itype) if itype else None

        if specs is None:
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         None,
                "gcp_sku_name":       None,
                "component":          "instance",
                "strategy":           "passthrough",
                "unit_multiplier":    1.0,
                "gcp_region":         region or r.get("gcp_region"),
                "projection_note":    (f"Windows EC2 {itype or repr(op[:60])} — instance type not in "
                                       "table; passthrough at cost parity (includes OS license)"),
                "mapping_confidence": 0.40,
            })
            continue

        vcpu, ram_gib = specs
        is_arm = itype.split(".")[0] in ("t4g", "m6g", "c6g", "r6g", "m7g", "c7g")
        # t2/t3/t3a are AWS's burstable families; everything else in
        # _WIN_EC2_SPECS (m4/m5/m5a/m6i/c4/c5/c5a/r5/r6i) is sustained-
        # performance. These MUST branch separately: defaulting every non-ARM
        # row to E2 regardless of source family (as this used to) mapped
        # genuinely sustained AWS instances — which never burst on AWS either
        # — onto GCP's burstable family, a real performance-tier downgrade,
        # not just a missed cost optimization. Only the burstable branch may
        # ever land on E2; the sustained branch stays within the sustained
        # tier (N2D/N4D/T2D) so a "cheaper" pick can never mean "lower tier."
        is_burstable = itype.split(".")[0] in ("t2", "t3", "t3a")
        switched = False
        if is_arm:
            # Confirmed dead code today (AWS doesn't sell Windows on
            # Graviton) but fixed for consistency anyway, and because this
            # branch had its own real bug: it previously fell back to
            # "C4A Instance Core"/"C4A Instance Ram" (missing "Arm"), which
            # never resolves to any SKU — the fallback silently failed every
            # time regardless of region. Now uses the same registry-driven
            # sweep as every other ARM branch: T2A ARM's default preference is
            # preserved via `default_label`, but if it's unavailable ANY
            # sustained ARM family in `_GP_FAMILIES` is a valid same-tier pick.
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "T2A Arm", vcpu, ram_gib, region, archs=("arm",), tiers=("sustained",))
            # _strict_resolve_sku everywhere below — see its docstring: avoids
            # resolve_sku()'s continent-fallback silently substituting a
            # different region's SKU/price for a family cheapest_in_scope()
            # already confirmed has no rate in THIS region.
            cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, cpu_desc, region)
            ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region)
        elif is_burstable:
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "E2", vcpu, ram_gib, region, archs=("x86",), tiers=("burstable", "sustained"))
            cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, cpu_desc, region)
            ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region)
        else:
            family_label, cpu_desc, ram_desc, switched, reason = cheapest_in_scope(
                "N2D AMD", vcpu, ram_gib, region, archs=("x86",), tiers=("sustained",))
            cpu_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, cpu_desc, region)
            ram_sku = _strict_resolve_sku(GCP_COMPUTE_ENGINE, ram_desc, region)

        cost_tier_note = ""
        if switched:
            cost_tier_note = (f" [cost-tier: {family_label} cheaper here, same tier as default]" if reason == "cheaper"
                              else f" [cost-tier: default unavailable in region — {family_label} used instead, same tier]")

        sql_sku, sql_desc = _detect_sql_server_sku(
            r.get("product") or "", op, vcpu)
        sql_suffix = f" + SQL Server {sql_desc.split()[2]} license" if sql_sku else ""
        note_base = (f"Windows EC2 {itype} ({vcpu} vCPU, {ram_gib} GiB) → "
                     f"{family_label} core+RAM + Windows Server Standard license "
                     f"($0.046/VM-h flat){sql_suffix}{cost_tier_note}")

        components = [
            ("core",        cpu_sku,           float(vcpu),    cpu_desc),
            ("ram",         ram_sku,           float(ram_gib), ram_desc),
            ("license",     _WIN_LICENSE_SKU,  1.0,            "Windows Server 2019 Standard Edition license"),
        ]
        if sql_sku:
            components.append(("sql_license", sql_sku, 1.0, sql_desc))

        for comp, sku, mult, desc in components:
            strategy = "map" if sku else "passthrough"
            out.append({
                "aws_li_key":         r["aws_li_key"],
                "gcp_service":        GCP_COMPUTE_ENGINE,
                "gcp_sku_id":         sku,
                "gcp_sku_name":       desc,
                "component":          comp,
                "strategy":           strategy,
                "unit_multiplier":    mult,
                "gcp_region":         region or r.get("gcp_region"),
                "projection_note":    note_base + (_WIN_BYOL_NOTE if comp == "license" else ""),
                "mapping_confidence": 0.82,
            })
    return out


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
        sys.exit(1)

    db_path = sys.argv[1]
    print(f"SKU cache: {RESOLVED_SKUS_FILE}")
    con = duckdb.connect(db_path)

    rows = con.execute("""
        SELECT aws_li_key, mechanic_group, product, usage_type, pricing_unit AS unit,
               aws_amortized_cost, aws_region AS region, gcp_region, operation, volume_type,
               total_usage, instance_type, instance_ram_gb, deployment_option, database_engine,
               pricing_model
        FROM aws_li_catalog
        WHERE mechanic_group IN
              ('flat_hourly', 'object_storage', 'per_request', 'block_storage', 'data_transfer',
               'non_workload', 'cloudwatch', 'guardduty', 'redshift', 'athena', 'kinesis', 'efs',
               'xray', 'fsx', 'emr', 'elasticache', 'msk', 'compute_windows', 'compute_arm',
               'compute_burstable', 'managed_db', 'opensearch', 'commitment_discount')
    """).fetchall()
    con.close()

    cols = ["aws_li_key", "mechanic_group", "product", "usage_type", "unit",
            "aws_amortized_cost", "region", "gcp_region", "operation", "volume_type",
            "total_usage", "instance_type", "instance_ram_gb", "deployment_option", "database_engine",
            "pricing_model"]
    by_group: dict[str, list] = {g: [] for g in
                                 ("flat_hourly", "object_storage", "per_request",
                                  "block_storage", "data_transfer", "non_workload", "cloudwatch", "msk",
                                  "guardduty", "redshift", "athena", "kinesis", "efs", "xray",
                                  "fsx", "emr", "elasticache", "compute_windows", "compute_arm",
                                  "compute_burstable", "managed_db", "opensearch", "commitment_discount")}
    for raw in rows:
        r = dict(zip(cols, raw))
        by_group[r["mechanic_group"]].append(r)

    out_dir = _safe_path(os.path.dirname(db_path), "mappings")
    os.makedirs(out_dir, exist_ok=True)

    handlers = {
        "flat_hourly":    map_flat_hourly,
        "object_storage": map_object_storage,
        "per_request":    map_per_request,
        "block_storage":  map_block_storage,
        "data_transfer":  map_data_transfer,
        "non_workload":   map_non_workload,
        "cloudwatch":     map_cloudwatch,
        "guardduty":      map_guardduty,
        "redshift":       map_redshift,
        "athena":         map_athena,
        "kinesis":        map_kinesis,
        "efs":            map_efs,
        "xray":           map_xray,
        "fsx":            map_fsx,
        "emr":            map_emr,
        "elasticache":         map_elasticache,
        "msk":                 map_msk,
        "compute_windows":     map_compute_windows,
        "compute_arm":         map_compute_arm,
        "compute_burstable":   map_compute_burstable,
        "commitment_discount": map_commitment_discount,
        "managed_db":          map_managed_db,
        "opensearch":          map_opensearch,
    }
    all_llm_rows: list[dict] = []

    for group, handler in handlers.items():
        try:
            result = handler(by_group[group])
        except Exception as e:
            # A mapper failure must never stop report generation. Log it and emit
            # an empty mapping file so downstream phases see the group as handled.
            print(f"WARNING: {group} mapper raised {type(e).__name__}: {e} — skipping group, rows will be passthrough", file=sys.stderr)
            result = []

        # Handlers that have LLM fallback return (mapped, llm_rows).
        # Legacy handlers return a plain list — treat as (result, []).
        if isinstance(result, tuple):
            mappings, llm_rows = result
            all_llm_rows.extend(llm_rows)
        else:
            mappings = result

        path = _safe_path(out_dir, f"{group}_mappings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mappings, f, indent=2)
        llm_note = f" (+{len(llm_rows)} → LLM)" if isinstance(result, tuple) and llm_rows else ""
        print(f"{group}: {len(mappings)} rows → {path}{llm_note}")

    # Inject unknown rows from static mappers into the manifest misc group so the
    # Phase 2 LLM handles them with full context instead of them being silently lost.
    if all_llm_rows:
        manifest_path = _safe_path(os.path.dirname(db_path), "phase2_manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            misc = manifest.setdefault("misc", {"row_count": 0, "rows": []})
            for r in all_llm_rows:
                misc["rows"].append({
                    "aws_li_key":          r["aws_li_key"],
                    "product":             r.get("product"),
                    "usage_type":          r.get("usage_type"),
                    "operation":           r.get("operation"),
                    "gcp_region":          r.get("gcp_region"),
                    "aws_amortized_cost":  r.get("aws_amortized_cost"),
                    "mechanic_group":      r.get("mechanic_group"),
                    "_injected_from_static": True,
                })
            misc["row_count"] = len(misc["rows"])
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            print(f"\nInjected {len(all_llm_rows)} unknown static-mapper row(s) into misc for LLM.")
        else:
            print(f"\nWARNING: {len(all_llm_rows)} unknown row(s) could not be injected "
                  f"— phase2_manifest.json not found at {manifest_path}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never exit 1 — a partial mapping is always better than no report.
        print(f"FATAL in apply_static_mappings: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(0)
