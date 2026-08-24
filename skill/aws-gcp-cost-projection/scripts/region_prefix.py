#!/usr/bin/env python3
"""
Shared AWS usage_type region-prefix decoder.

PDF/flat-CSV bills often omit a dedicated Region column; the region is instead
embedded as a prefix on usage_type (e.g. "APS3-EBS:VolumeUsage.gp3" → Mumbai,
"APS5-Lambda-GB-Second-ARM" → Delhi/Hyderabad). This used to be decoded inside
individual mapper functions (map_block_storage, the MSK mapper, family_mapper's
EC2 path) — anything NOT covered by one of those (Lambda/Cloud Run rows, S3
storage rows, ...) silently kept gcp_region='global', which then breaks
description-based SKU resolution downstream: most GCP catalog SKU descriptions
embed a literal region name ("Standard Storage Hong Kong"), so there's often no
'global' variant to match against at all — the row's rate lookup fails outright.

Call decode_region_prefix() once, universally, in ingest.py's Step 5 (region
assignment) so every row gets this applied regardless of which mapper it later
routes through, instead of requiring each mapper to remember to call it itself.
"""
import re

# Prefix meanings (confirmed against real multi-region flat-CSV/PDF bills):
#   APS1=ap-southeast-1(SG), APS2=ap-southeast-2(SYD), APS3=ap-south-1(MUM),
#   APS4=ap-southeast-4(MEL), APS5=ap-south-2(DEL), APN1=ap-northeast-1(TYO),
#   USE1=us-east-1, USE2=us-east-2, USW1=us-west-1, USW2=us-west-2,
#   EUW1=eu-west-1, EUW2=eu-west-2, EUC1=eu-central-1
UT_PREFIX_TO_GCP: dict[str, str] = {
    "aps1": "asia-southeast1",           # ap-southeast-1 → Singapore
    "aps2": "australia-southeast1",      # ap-southeast-2 → Sydney
    "aps3": "asia-south1",               # ap-south-1     → Mumbai
    "aps4": "australia-southeast2",      # ap-southeast-4 → Melbourne
    "aps5": "asia-south2",               # ap-south-2     → Delhi/Hyderabad
    "aps6": "asia-southeast2",           # ap-southeast-3 → Jakarta
    "apn1": "asia-northeast1",           # ap-northeast-1 → Tokyo
    "apn2": "asia-northeast2",           # ap-northeast-2 → Seoul
    "apn3": "asia-northeast3",           # ap-northeast-3 → Osaka
    "use1": "us-east4",                  # us-east-1      → N. Virginia
    "use2": "us-east4",                  # us-east-2      → Ohio
    "usw1": "us-west1",                  # us-west-1      → N. California
    "usw2": "us-west2",                  # us-west-2      → Oregon
    "usw3": "us-west2",                  # us-west-3      → Salt Lake City
    "usw4": "us-west1",                  # us-west-4      → Las Vegas
    "euw1": "europe-west1",              # eu-west-1      → Ireland
    "euw2": "europe-west2",              # eu-west-2      → London
    "euw3": "europe-west3",              # eu-west-3      → Paris
    "euw4": "europe-west4",              # eu-west-4      → Netherlands
    "eun1": "europe-north1",             # eu-north-1     → Stockholm
    "euc1": "europe-west3",              # eu-central-1   → Frankfurt
    "euc2": "europe-west3",              # eu-central-2   → Zurich
    "cac1": "northamerica-northeast1",   # ca-central-1   → Montreal
    "sae1": "southamerica-east1",        # sa-east-1      → São Paulo
    "mec1": "me-central1",              # me-central-1   → UAE
    "mes1": "me-west1",                  # me-south-1     → Bahrain
}
UT_PREFIX_RE = re.compile(r"^([a-z]{2,4}\d{1,2})-", re.IGNORECASE)


def decode_region_prefix(usage_type, fallback=None):
    """Return the GCP region embedded in usage_type's prefix, or fallback if
    usage_type has no recognized prefix (e.g. "APS3-EBS:..." → "asia-south1")."""
    m = UT_PREFIX_RE.match(usage_type or "")
    if not m:
        return fallback
    return UT_PREFIX_TO_GCP.get(m.group(1).lower(), fallback)
