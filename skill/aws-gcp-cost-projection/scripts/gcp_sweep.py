#!/usr/bin/env python3
from __future__ import annotations
"""
gcp_sweep.py — Live GCP sweep: pick the cheapest real, available Compute Engine
machine type that meets or beats a given workload spec.

DESIGN NOTE (as of writing this file): this script has NOT been exercised
end-to-end — there are no live GCP credentials available in this environment.
It is written to run for real once credentials exist; treat any output before
then as unverified. Verify against `gcloud compute machine-types list` and the
Cloud Billing Catalog console before trusting a projection built on it.

What it does, given a workload's essential fields (vCPU, RAM GiB, architecture,
region, storage GiB, optional IOPS/throughput):

  1. Queries the Cloud Billing Catalog API for Compute Engine SKUs and derives
     a candidate machine-type list with an on-demand hourly price for the
     target region (same API prefetch_skus.py / refresh-catalog.sh already use
     — see those for the established auth + pagination pattern this reuses).
  2. Confirms each candidate is actually orderable in the target region/zone
     via the Compute Engine `machineTypes.list` API (a SKU existing in the
     Catalog does not guarantee the family is available in every region/zone
     — apply_static_mappings.py's N4D-in-asia-south2 case is exactly this
     failure mode; see _N4D_UNAVAILABLE_REGIONS there).
  3. Filters to same-or-better architecture family: an ARM (Graviton) source
     workload only considers GCP ARM families (Axion C4A, Tau T2A, N4A) —
     x86 is never silently substituted for an ARM source unless literally no
     ARM candidate meets the vCPU/RAM spec in that region. An x86 source may
     consider both (a cheaper ARM candidate is a legitimate suggestion when
     going x86->ARM, unlike the reverse).
  4. Filters to vCPU >= requested and RAM_GiB >= requested — never proposes a
     smaller/weaker instance than the source workload needs.
  5. Ranks the surviving candidates by on-demand hourly price ascending and
     returns the cheapest.

Usage:
    python3 gcp_sweep.py --vcpu 8 --ram 32 --arch arm --region asia-south1 \\
        [--storage-gb 500] [--iops 3000] [--throughput-mbps 250] [--zone asia-south1-a]

Auth: set GOOGLE_CLOUD_API_KEY (or GCP_API_KEY), or run `gcloud auth login` /
`gcloud auth application-default login` first (this script shells out to
`gcloud auth print-access-token`, same as scripts/catalog_health_check.py and
scripts/refresh-catalog.sh). Without either, it exits with a clear error
instead of a stack trace — it never silently returns a fabricated price.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

COMPUTE_ENGINE_SERVICE_ID = "6F81-5844-456A"  # data/services.json: displayName == "Compute Engine"
BILLING_API = "https://cloudbilling.googleapis.com/v1"
COMPUTE_API = "https://compute.googleapis.com/compute/v1"

# ARM (Graviton-equivalent) GCP machine-type family prefixes. Kept in sync
# with _GP_FAMILY_ARCH in apply_static_mappings.py (T2A Arm / C4A Arm / N4A).
ARM_FAMILY_PREFIXES = ("t2a-", "c4a-", "n4a-")


class GcpSweepError(RuntimeError):
    """Raised for any condition that should stop the sweep with a clear message
    (auth missing, no candidates, API failure) rather than a bare traceback."""


# ---------------------------------------------------------------------------
# Auth — identical pattern to scripts/catalog_health_check.py and
# scripts/prefetch_skus.py: prefer an API key, fall back to `gcloud` ADC token.
# ---------------------------------------------------------------------------

def _gcp_token():
    key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if key:
        return ("key", key)
    for candidate in ["gcloud", r"C:\Users\Public\google-cloud-sdk\bin\gcloud.cmd"]:
        try:
            tok = subprocess.check_output(
                [candidate, "auth", "print-access-token"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if tok:
                return ("bearer", tok)
        except Exception:
            continue
    return None


def _require_token():
    token_info = _gcp_token()
    if not token_info:
        raise GcpSweepError(
            "No GCP credentials found. Set GOOGLE_CLOUD_API_KEY (or GCP_API_KEY) "
            "or run `gcloud auth login` (ADC: `gcloud auth application-default login`) "
            "before running gcp_sweep.py. See scripts/refresh-catalog.sh for the same "
            "auth requirement used by the catalog refresh job."
        )
    return token_info


def _gcp_get(url, token_info):
    kind, value = token_info
    if kind == "key":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={value}"
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {value}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GcpSweepError(f"GCP API request failed ({e.code}) for {url}: {body}") from e
    except urllib.error.URLError as e:
        raise GcpSweepError(f"GCP API request failed for {url}: {e.reason}") from e


# ---------------------------------------------------------------------------
# Step 1 — Cloud Billing Catalog: Compute Engine on-demand SKUs for the region
# ---------------------------------------------------------------------------

def fetch_compute_engine_skus(region: str, token_info) -> list[dict]:
    """Return raw Compute Engine SKU dicts whose serviceRegions include `region`.

    Same pagination pattern as prefetch_skus.py / catalog_health_check.py's
    _fetch_service_skus, scoped to one region via the API's own filter param
    to avoid pulling the entire (very large) global SKU list.
    """
    skus, page_token = [], ""
    while True:
        url = (f"{BILLING_API}/services/{COMPUTE_ENGINE_SERVICE_ID}/skus"
               f"?pageSize=5000")
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = _gcp_get(url, token_info)
        for sku in data.get("skus", []):
            if region in sku.get("serviceRegions", []):
                skus.append(sku)
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return skus


def _sku_hourly_rate_usd(sku: dict) -> float | None:
    """Extract a $/unit-hour rate from a Cloud Billing Catalog SKU's
    pricingInfo. Returns None if the SKU has no usable tiered rate (e.g. it's
    a commitment/discount SKU, not on-demand)."""
    try:
        pe = sku["pricingInfo"][0]["pricingExpression"]
        tiers = pe.get("tieredRates", [])
        if not tiers:
            return None
        unit_price = tiers[0]["unitPrice"]
        units = int(unit_price.get("units", 0))
        nanos = unit_price.get("nanos", 0)
        return units + nanos / 1e9
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _family_from_sku_description(desc: str) -> str | None:
    """Best-effort machine family token from a SKU description like
    'N2D AMD Instance Core running in Mumbai' -> 'n2d'. Instance-core/instance-
    ram SKUs come in pairs per family; we key off the leading family name."""
    desc_low = desc.lower()
    for token in ("t2a", "c4a", "n4a", "n4d", "n4", "n2d", "n2", "c4d", "c4",
                  "c2d", "c2", "t2d", "e2", "m3", "m2", "m1"):
        if desc_low.startswith(token + " ") or f" {token} " in desc_low:
            return token
    return None


def build_candidate_price_table(skus: list[dict]) -> dict[str, float]:
    """From raw SKUs, build {family: hourly_usd_per_vcpu_and_ram_combined}.

    Real Compute Engine pricing is Instance Core ($/vCPU-hr) + Instance Ram
    ($/GiB-hr) as separate SKUs per family; this collapses them into a
    per-family {"core": $, "ram": $} pair so callers can price an exact
    vCPU/RAM shape instead of assuming a fixed predefined machine size.
    """
    table: dict[str, dict[str, float]] = {}
    for sku in skus:
        desc = sku.get("description", "")
        family = _family_from_sku_description(desc)
        if not family:
            continue
        rate = _sku_hourly_rate_usd(sku)
        if rate is None:
            continue
        desc_low = desc.lower()
        slot = table.setdefault(family, {})
        if "ram running" in desc_low or "instance ram" in desc_low:
            slot["ram"] = rate
        elif "core running" in desc_low or "instance core" in desc_low:
            slot["core"] = rate
    return {k: v for k, v in table.items() if "core" in v and "ram" in v}


# ---------------------------------------------------------------------------
# Step 2 — Compute Engine machineTypes.list: confirm real availability per zone
# ---------------------------------------------------------------------------

def list_zone_machine_types(project: str, zone: str, token_info) -> list[dict]:
    """Return machineTypes.list results for one zone. A SKU existing in the
    Catalog API does NOT guarantee the family is orderable in every zone —
    this is the authoritative "can I actually launch this" check.

    Requires a GCP project id with the Compute Engine API enabled (any project
    the caller's credentials can read machine-type metadata in works — this is
    a read-only, no-billing-impact call).
    """
    machine_types, page_token = [], ""
    while True:
        url = f"{COMPUTE_API}/projects/{project}/zones/{zone}/machineTypes?maxResults=500"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = _gcp_get(url, token_info)
        machine_types.extend(data.get("items", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return machine_types


def _machine_type_family(name: str) -> str:
    # e.g. "n2d-standard-8" -> "n2d", "c4a-highmem-4" -> "c4a"
    return name.split("-", 1)[0]


def _is_arm_family(family: str) -> bool:
    return f"{family}-" in ARM_FAMILY_PREFIXES


# ---------------------------------------------------------------------------
# Step 3-5 — filter by arch/spec, rank by price, return the cheapest
# ---------------------------------------------------------------------------

def sweep(
    vcpu: int,
    ram_gb: float,
    arch: str,
    region: str,
    project: str,
    zone: str | None = None,
    storage_gb: float | None = None,
    iops: int | None = None,
    throughput_mbps: int | None = None,
) -> dict:
    """Find the cheapest real, available GCP Compute Engine machine type that
    meets or exceeds (vcpu, ram_gb) in `region`, honoring the arch constraint.

    Returns a dict describing the winning candidate and the reasoning trail
    (candidates considered, why others were excluded) so the caller can audit
    the decision rather than trust a bare number.
    """
    arch = arch.lower()
    if arch not in ("x86", "arm"):
        raise GcpSweepError(f"arch must be 'x86' or 'arm', got {arch!r}")

    zone = zone or f"{region}-a"
    token_info = _require_token()

    skus = fetch_compute_engine_skus(region, token_info)
    if not skus:
        raise GcpSweepError(
            f"No Compute Engine SKUs found for region {region!r} — check the "
            f"region code and that Cloud Billing Catalog access is working."
        )
    price_table = build_candidate_price_table(skus)
    if not price_table:
        raise GcpSweepError(
            f"Could not derive per-vCPU/per-GiB rates from Compute Engine SKUs "
            f"for region {region!r} (SKU description format may have changed — "
            f"see _family_from_sku_description)."
        )

    available = list_zone_machine_types(project, zone, token_info)
    available_families = {_machine_type_family(mt["name"]) for mt in available}

    candidates = []
    excluded = []
    for family, rates in price_table.items():
        is_arm = _is_arm_family(family)
        if arch == "arm" and not is_arm:
            excluded.append((family, "not an ARM family; source workload is ARM"))
            continue
        if family not in available_families:
            excluded.append((family, f"not orderable in zone {zone} per machineTypes.list"))
            continue
        hourly = rates["core"] * vcpu + rates["ram"] * ram_gb
        candidates.append({
            "family": family,
            "arch": "arm" if is_arm else "x86",
            "vcpu": vcpu,
            "ram_gb": ram_gb,
            "hourly_usd": round(hourly, 6),
            "core_rate_usd_per_vcpu_hr": rates["core"],
            "ram_rate_usd_per_gib_hr": rates["ram"],
        })

    if not candidates and arch == "arm":
        # Never silently substitute x86 for an ARM source — only fall back
        # when explicitly no ARM candidate exists, and say so loudly.
        for family, rates in price_table.items():
            if family not in available_families:
                excluded.append((family, f"not orderable in zone {zone}"))
                continue
            hourly = rates["core"] * vcpu + rates["ram"] * ram_gb
            candidates.append({
                "family": family,
                "arch": "x86",
                "vcpu": vcpu,
                "ram_gb": ram_gb,
                "hourly_usd": round(hourly, 6),
                "core_rate_usd_per_vcpu_hr": rates["core"],
                "ram_rate_usd_per_gib_hr": rates["ram"],
                "fallback_note": "No ARM (C4A/T2A/N4A) family available in this region/zone "
                                 "meeting the spec — falling back to x86. Verify before relying on this.",
            })

    if not candidates:
        raise GcpSweepError(
            f"No available Compute Engine family in {zone} meets vCPU>={vcpu}, "
            f"RAM>={ram_gb} GiB for arch={arch}. Excluded: {excluded}"
        )

    candidates.sort(key=lambda c: c["hourly_usd"])
    winner = candidates[0]

    result = {
        "winner": winner,
        "candidates_considered": candidates,
        "excluded": excluded,
        "region": region,
        "zone": zone,
        "requested": {"vcpu": vcpu, "ram_gb": ram_gb, "arch": arch,
                      "storage_gb": storage_gb, "iops": iops, "throughput_mbps": throughput_mbps},
    }
    if storage_gb is not None:
        # Storage/IOPS/throughput sizing is a Persistent Disk / Hyperdisk decision
        # independent of machine-type choice — left for the caller's existing
        # block_storage pricing path (map_block_storage in apply_static_mappings.py)
        # rather than duplicated here.
        result["storage_note"] = (
            "Storage/IOPS/throughput sizing is not resolved by this sweep — "
            "route storage_gb/iops/throughput_mbps through the existing "
            "block_storage static mapper for a Hyperdisk/PD SKU instead."
        )
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Find the cheapest available GCP Compute Engine machine type "
                    "meeting or exceeding a given vCPU/RAM/arch spec in a region."
    )
    parser.add_argument("--vcpu", type=int, required=True, help="Minimum vCPU count")
    parser.add_argument("--ram", type=float, required=True, help="Minimum RAM in GiB")
    parser.add_argument("--arch", choices=["x86", "arm"], required=True,
                        help="Source workload architecture (arm = Graviton-equivalent)")
    parser.add_argument("--region", required=True, help="GCP region, e.g. asia-south1")
    parser.add_argument("--zone", default=None, help="GCP zone (default: <region>-a)")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                        help="GCP project id for machineTypes.list availability check "
                             "(default: $GOOGLE_CLOUD_PROJECT)")
    parser.add_argument("--storage-gb", type=float, default=None, help="Storage size in GiB (advisory only)")
    parser.add_argument("--iops", type=int, default=None, help="Provisioned IOPS (advisory only)")
    parser.add_argument("--throughput-mbps", type=int, default=None, help="Provisioned throughput MB/s (advisory only)")
    args = parser.parse_args()

    if not args.project:
        print("ERROR: --project (or $GOOGLE_CLOUD_PROJECT) is required for the "
              "machineTypes.list availability check.", file=sys.stderr)
        sys.exit(2)

    try:
        result = sweep(
            vcpu=args.vcpu, ram_gb=args.ram, arch=args.arch, region=args.region,
            project=args.project, zone=args.zone, storage_gb=args.storage_gb,
            iops=args.iops, throughput_mbps=args.throughput_mbps,
        )
    except GcpSweepError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
