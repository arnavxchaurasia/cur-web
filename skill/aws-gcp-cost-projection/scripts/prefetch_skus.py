#!/usr/bin/env python3
"""
prefetch_skus.py — ONE-TIME setup: pre-fetch all GCP SKU IDs and AWS instance pricing
into the skill's global cache (data/resolved_skus.json, data/aws_instance_prices.json).

Run manually once after installing or updating the skill:
    SKILL_DIR=/path/to/skill python3 scripts/prefetch_skus.py [--region asia-southeast1] [--force]

Options:
    --region REGION   GCP region to fetch SKUs for (default: asia-southeast1)
    --force           Re-fetch even if this region is already in the cache

After this runs, resolve_sku() in apply_static_mappings.py uses the cache for instant
lookups and only calls the live API when it encounters a genuinely new SKU not in the cache.
Do NOT add this script to pre_llm_scripts — it does not run per job.
"""

import gzip, json, os, re, subprocess, sys, urllib.request, urllib.parse

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import duckdb

SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR  = os.path.join(SKILL_DIR, "data")

# Global persistent caches — shared across all jobs, appended to never overwritten
RESOLVED_SKUS_FILE = os.path.join(DATA_DIR, "resolved_skus.json")
AWS_PRICES_FILE    = os.path.join(DATA_DIR, "aws_instance_prices.json")

# GCP services we always need regardless of bill content — loaded from config
# so adding a new core service requires only a data/gcp-model-config.json edit.
def _load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

_gcp_model_config = _load_json(os.path.join(DATA_DIR, "gcp-model-config.json"))
CORE_GCP_SERVICES = _gcp_model_config.get("prefetch_core_gcp_services", [
    "Compute Engine", "Cloud SQL", "Cloud Storage", "Networking",
    "Cloud Memorystore for Memcached", "Cloud Memorystore for Redis",
    "Cloud Memorystore", "Artifact Registry",
])

# AWS Pricing API base — publicly accessible, no credentials required
AWS_PRICING_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _append_json(path, new_entries):
    """Append only the keys that are not yet in the file — never overwrites existing entries."""
    existing = _load_json(path, {})
    added = 0
    for k, v in new_entries.items():
        if k not in existing:
            existing[k] = v
            added += 1
    if added:
        _save_json(path, existing)
    return added


# ---------------------------------------------------------------------------
# GCP token
# ---------------------------------------------------------------------------

def _gcp_token():
    """Return a Bearer token. Tries API key env var, then gcloud."""
    api_key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if api_key:
        return ("key", api_key)
    try:
        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if tok:
            return ("bearer", tok)
    except Exception:
        pass
    return None


def _gcp_get(url, token_info):
    if token_info is None:
        return None
    kind, value = token_info
    if kind == "key":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={value}"
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {value}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  GCP fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# GCP SKU prefetch
# ---------------------------------------------------------------------------

def _services_map():
    path = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {s["displayName"]: s["serviceId"] for s in json.load(f)}


def _bundled_sku_lookup(service_id, desc_pattern, gcp_region):
    """Search the bundled (possibly stale) catalog as fast fallback."""
    sku_file = os.path.join(DATA_DIR, "skus", f"{service_id}.json.gz")
    if not os.path.exists(sku_file):
        return None
    with gzip.open(sku_file, "rt") as f:
        skus = json.load(f)
    for sku in skus:
        if not re.search(desc_pattern, sku.get("description", ""), re.IGNORECASE):
            continue
        geo = sku.get("geoTaxonomy", {})
        if geo.get("type") == "GLOBAL" or gcp_region in sku.get("serviceRegions", []):
            return sku["skuId"]
    return None


# Mirrors apply_static_mappings.py's _KNOWN_PHANTOM_AVAILABILITY — confirmed
# (family, region) pairs where the Cloud Billing Catalog/API returns a real,
# priced SKU that Compute Engine will not actually let you deploy (N4D has a
# priced "N4D Instance Core/Ram running in Delhi" SKU for asia-south2, but
# the GCP Console's own VM-creation wizard rejects N4D there). This script is
# standalone (no import from apply_static_mappings.py, deliberately, so it
# can run without the rest of the pipeline's dependencies) so the list is
# duplicated here rather than shared — keep the two in sync when adding a
# new confirmed case. Without this, a `--force` re-fetch would call the live
# Billing API (which genuinely returns this SKU, phantom pricing and all)
# and write the wrong entry straight back into resolved_skus.json, undoing
# any manual cleanup with no code change required on the reading side.
# Note: data/gcp-model-config.json tracks phantom availability by pattern label + region.
# This list uses lowercase SKU description prefix — a different matching strategy.
# Both must be kept in sync. Update both when adding a new confirmed phantom case.
_KNOWN_PHANTOM_AVAILABILITY = {
    ("n4d instance core running in delhi", "asia-south2"),
    ("n4d instance ram running in delhi", "asia-south2"),
}


def _is_phantom_availability(desc_lower, gcp_region):
    return any(desc_lower.startswith(prefix) and gcp_region == region
               for prefix, region in _KNOWN_PHANTOM_AVAILABILITY)


def fetch_gcp_skus_for_region(gcp_region, services_needed, token_info):
    """
    For each service, page through the Cloud Billing Catalog API and collect
    all SKUs that serve gcp_region. Returns {desc_lower: sku_id}.
    Uses bundled catalog as fallback when API is unavailable.
    """
    svc_map = _services_map()
    new_entries = {}

    for svc_name in services_needed:
        svc_id = svc_map.get(svc_name)
        if not svc_id:
            print(f"  [GCP] service not in services.json: {svc_name}")
            continue

        skus = []
        if token_info:
            page_token = ""
            while True:
                url = f"https://cloudbilling.googleapis.com/v1/services/{svc_id}/skus?pageSize=5000"
                if page_token:
                    url += f"&pageToken={urllib.parse.quote(page_token)}"
                data = _gcp_get(url, token_info)
                if not data:
                    break
                skus.extend(data.get("skus", []))
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
            print(f"  [GCP] {svc_name}: {len(skus)} SKUs fetched from API")
        else:
            # Use bundled catalog
            sku_file = os.path.join(DATA_DIR, "skus", f"{svc_id}.json.gz")
            if os.path.exists(sku_file):
                with gzip.open(sku_file, "rt") as f:
                    skus = json.load(f)
                print(f"  [GCP] {svc_name}: {len(skus)} SKUs from bundled catalog")

        for sku in skus:
            geo = sku.get("geoTaxonomy", {})
            in_region = (
                geo.get("type") == "GLOBAL"
                or gcp_region in sku.get("serviceRegions", [])
            )
            if not in_region:
                continue
            desc = sku.get("description", "")
            desc_lower = desc.lower()
            if _is_phantom_availability(desc_lower, gcp_region):
                print(f"  [GCP] skipping known-phantom-availability SKU: {desc!r} in {gcp_region}")
                continue
            key = f"gcp|{svc_name}|{desc_lower}|{gcp_region}"
            new_entries[key] = sku["skuId"]

    return new_entries


# ---------------------------------------------------------------------------
# AWS instance pricing prefetch
# ---------------------------------------------------------------------------

def _aws_region_url_code(aws_region):
    """Map AWS region to the pricing URL code."""
    mapping = {
        "ap-southeast-1": "ap-southeast-1",
        "us-east-1": "us-east-1",
        "us-east-2": "us-east-2",
        "us-west-1": "us-west-1",
        "us-west-2": "us-west-2",
        "eu-west-1": "eu-west-1",
        "eu-central-1": "eu-central-1",
        "ap-northeast-1": "ap-northeast-1",
        "ap-south-1": "ap-south-1",
    }
    return mapping.get(aws_region, aws_region)


def fetch_aws_instance_prices(aws_region, instance_types):
    """
    Fetch On-Demand hourly prices from the AWS Pricing API for the given instance types.
    The AWS EC2 pricing index is publicly accessible — no credentials needed.
    Appends to aws_instance_prices.json (only new instance types).
    """
    existing = _load_json(AWS_PRICES_FILE, {})
    needed = [it for it in instance_types if it and it not in existing]
    if not needed:
        print(f"  [AWS] all {len(instance_types)} instance types already cached")
        return 0

    region_code = _aws_region_url_code(aws_region)
    url = f"{AWS_PRICING_BASE}/AmazonEC2/current/{region_code}/index.json"
    print(f"  [AWS EC2] fetching pricing index for {region_code} ...")
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [AWS EC2] fetch failed: {e}")
        return 0

    products = data.get("products", {})
    terms    = data.get("terms", {}).get("OnDemand", {})
    new_prices = {}

    for sku_hash, product in products.items():
        attrs = product.get("attributes", {})
        itype = attrs.get("instanceType", "")
        if itype not in needed:
            continue
        if attrs.get("operatingSystem", "") != "Linux":
            continue
        if attrs.get("tenancy", "Shared") != "Shared":
            continue
        od_terms = terms.get(sku_hash, {})
        for _, term_data in od_terms.items():
            for _, dim in term_data.get("priceDimensions", {}).items():
                price = float(dim.get("pricePerUnit", {}).get("USD", 0))
                if price > 0:
                    new_prices[itype] = {"od_hourly_usd": price, "region": region_code}
                    break

    added = _append_json(AWS_PRICES_FILE, new_prices)
    print(f"  [AWS EC2] {len(new_prices)} prices found, {added} new entries cached")
    return added


def fetch_aws_rds_prices(aws_region, db_instance_types):
    """Fetch RDS On-Demand prices from the AWS Pricing API."""
    existing = _load_json(AWS_PRICES_FILE, {})
    needed = [it for it in db_instance_types if it and f"rds:{it}" not in existing]
    if not needed:
        return 0

    region_code = _aws_region_url_code(aws_region)
    url = f"{AWS_PRICING_BASE}/AmazonRDS/current/{region_code}/index.json"
    print(f"  [AWS RDS] fetching pricing index for {region_code} ...")
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [AWS RDS] fetch failed: {e}")
        return 0

    products = data.get("products", {})
    terms    = data.get("terms", {}).get("OnDemand", {})
    new_prices = {}

    for sku_hash, product in products.items():
        attrs = product.get("attributes", {})
        itype = attrs.get("instanceType", "")
        if itype not in needed:
            continue
        engine = attrs.get("databaseEngine", "")
        deploy = attrs.get("deploymentOption", "Single-AZ")
        od_terms = terms.get(sku_hash, {})
        for _, term_data in od_terms.items():
            for _, dim in term_data.get("priceDimensions", {}).items():
                price = float(dim.get("pricePerUnit", {}).get("USD", 0))
                if price > 0:
                    cache_key = f"rds:{itype}:{engine}:{deploy}"
                    new_prices[cache_key] = {"od_hourly_usd": price, "region": region_code}
                    break

    added = _append_json(AWS_PRICES_FILE, new_prices)
    print(f"  [AWS RDS] {len(new_prices)} prices found, {added} new entries cached")
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="One-time GCP/AWS SKU prefetch into global cache")
    parser.add_argument("--region", default="asia-southeast1",
                        help="GCP region to fetch SKUs for (default: asia-southeast1)")
    parser.add_argument("--aws-region", default="ap-southeast-1",
                        help="AWS region for instance pricing (default: ap-southeast-1)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if region already in cache")
    args = parser.parse_args()

    gcp_region = args.region
    aws_region = args.aws_region
    services_needed = CORE_GCP_SERVICES[:]

    print(f"One-time SKU prefetch: gcp={gcp_region}, aws={aws_region}")
    print(f"Cache files: {RESOLVED_SKUS_FILE}")
    print(f"             {AWS_PRICES_FILE}")

    # ------------------------------------------------------------------
    # 1. GCP SKUs for all core services × region
    # ------------------------------------------------------------------
    print("\n[1/3] GCP SKU prefetch")
    existing_cache = _load_json(RESOLVED_SKUS_FILE, {})
    region_cached = any(
        k.startswith("gcp|Compute Engine|") and k.endswith(f"|{gcp_region}")
        for k in existing_cache
    )
    if region_cached and not args.force:
        print(f"  Region {gcp_region} already in cache — use --force to re-fetch")
        added_gcp = 0
    else:
        token_info = _gcp_token()
        print(f"  Auth: {token_info[0] if token_info else 'none — bundled catalog only'}")
        gcp_entries = fetch_gcp_skus_for_region(gcp_region, services_needed, token_info)
        added_gcp = _append_json(RESOLVED_SKUS_FILE, gcp_entries)
        print(f"  → {len(gcp_entries)} region-matched SKUs, {added_gcp} new entries cached")

    # ------------------------------------------------------------------
    # 2. AWS EC2 On-Demand prices for common instance families
    # ------------------------------------------------------------------
    print("\n[2/3] AWS EC2 instance price prefetch")
    # Read what's already in the static ec2-instance-types.json
    ec2_static = _load_json(os.path.join(DATA_DIR, "ec2-instance-types.json"), {})
    # Fetch prices for all known instance types (fills aws_instance_prices.json)
    ec2_types = [k for k in ec2_static if not k.startswith("_")]
    fetch_aws_instance_prices(aws_region, ec2_types)

    # ------------------------------------------------------------------
    # 3. AWS RDS On-Demand prices for common DB instance types
    # ------------------------------------------------------------------
    print("\n[3/3] AWS RDS instance price prefetch")
    rds_static = _load_json(os.path.join(DATA_DIR, "rds-instance-types.json"), {})
    rds_types = [k for k in rds_static if not k.startswith("_") and k.startswith("db.")]
    fetch_aws_rds_prices(aws_region, rds_types)

    total_gcp = len(_load_json(RESOLVED_SKUS_FILE, {}))
    total_aws = len(_load_json(AWS_PRICES_FILE, {}))
    print(f"\nDone. Cache totals: {total_gcp} GCP SKUs, {total_aws} AWS prices")


if __name__ == "__main__":
    main()
