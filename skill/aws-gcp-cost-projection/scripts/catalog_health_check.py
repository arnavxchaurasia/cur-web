#!/usr/bin/env python3
"""
catalog_health_check.py — Standalone GCP SKU catalog health report.

Checks that every service in data/services.json has a non-empty SKU file in
data/skus/, reports catalog age, and optionally triggers a refresh.

Usage:
    python3 scripts/catalog_health_check.py [--refresh] [--skill-dir PATH]

Options:
    --refresh    Fetch missing/stale SKU files from the GCP Cloud Billing API
                 (requires GOOGLE_CLOUD_API_KEY/GCP_API_KEY env var; the
                 `gcloud auth print-access-token` fallback is opt-in only via
                 AGY_ALLOW_GCLOUD_LIVE_FETCH=1 -- it can hang indefinitely on
                 Windows even with an active gcloud session, confirmed real,
                 so it is never attempted automatically)
    --skill-dir  Path to the skill root (default: parent of this script's dir)

Exit codes:
    0  All services have SKU files and catalog is fresh (<7 days old)
    1  Missing SKU files or catalog is stale (>7 days old)
    2  Configuration error (missing services.json, etc.)
"""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STALE_DAYS = 7


# ---------------------------------------------------------------------------
# GCP auth
# ---------------------------------------------------------------------------

def _gcp_token():
    """Return (kind, value) auth token, or None.

    CONFIRMED REAL BUG: the `gcloud` fallback below used to run
    unconditionally with no timeout on the subprocess call at all — and this
    function is invoked by internal/jobs/catalog_refresher.go's background
    refresher (server startup + every 24h) via `cmd.Run()`, which ALSO has no
    timeout/context at the Go level. Reproduced directly on Windows: `gcloud
    auth print-access-token` hung past 120s even with a valid, already-active
    `gcloud auth` session (`gcloud.cmd` is a batch shim whose spawned child
    process can keep the pipe open past whatever timeout the parent
    subprocess call requests, so a bare `timeout=` kwarg alone would not
    reliably bound it either — same bug class apply_static_mappings.py's own
    `_gcp_token_uncached()` already documents and works around there). With
    no bound at either layer, this could hang the catalog-refresh goroutine
    (and leak a zombie subprocess) indefinitely on every periodic tick.
    Fixed the same way: opt-in only via AGY_ALLOW_GCLOUD_LIVE_FETCH=1, not
    attempted by default; GOOGLE_CLOUD_API_KEY/GCP_API_KEY (a plain HTTP call
    with a real bounded timeout, no subprocess) remains the safe default path.
    """
    key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if key:
        return ("key", key)
    if os.environ.get("AGY_ALLOW_GCLOUD_LIVE_FETCH") != "1":
        return None
    for candidate in ["gcloud", r"C:\Users\Public\google-cloud-sdk\bin\gcloud.cmd"]:
        resolved = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if not resolved:
            continue
        try:
            tok = subprocess.check_output(
                [resolved, "auth", "print-access-token"],
                text=True, stderr=subprocess.DEVNULL, timeout=10,
            ).strip()
            if tok:
                return ("bearer", tok)
        except Exception:
            continue
    return None


def _gcp_get(url, token_info):
    kind, value = token_info
    if kind == "key":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={value}"
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {value}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch_service_skus(service_id, token_info):
    skus, page_token = [], ""
    while True:
        url = f"https://cloudbilling.googleapis.com/v1/services/{service_id}/skus?pageSize=5000"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = _gcp_get(url, token_info)
        skus.extend(data.get("skus", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return skus


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GCP SKU catalog health check")
    parser.add_argument("--refresh", action="store_true", help="Fetch missing SKUs from GCP API")
    parser.add_argument("--skill-dir", default=None)
    args = parser.parse_args()

    skill_dir = args.skill_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir  = os.path.join(skill_dir, "data")
    sku_dir   = os.path.join(data_dir, "skus")
    meta_path = os.path.join(data_dir, "CATALOG_META.json")
    svc_path  = os.path.join(data_dir, "services.json")

    print("=" * 60)
    print("GCP SKU Catalog Health Check")
    print("=" * 60)

    # ── 1. services.json ──────────────────────────────────────────
    if not os.path.exists(svc_path):
        print(f"ERROR: services.json not found at {svc_path}")
        sys.exit(2)

    services = json.load(open(svc_path))
    print(f"\nServices in allow-list : {len(services)}")

    # ── 2. Catalog age ────────────────────────────────────────────
    age_days = None
    is_stale = True
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path))
            fetched_at = meta.get("fetched_at", "")
            if fetched_at:
                ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - ts).days
                is_stale = age_days >= STALE_DAYS
                status = "STALE" if is_stale else "fresh"
                print(f"Catalog age            : {age_days} day(s) [{status} — threshold {STALE_DAYS}d]")
                print(f"Last fetched           : {fetched_at}")
                print(f"SKU count              : {meta.get('sku_count', '?')}")
        except Exception as e:
            print(f"WARNING: could not read CATALOG_META.json: {e}")
    else:
        print("CATALOG_META.json      : MISSING — catalog has never been fetched")

    # ── 3. Per-service SKU file check ─────────────────────────────
    print(f"\n{'Service':<45} {'SKU file':<10} {'SKUs':>6}")
    print("-" * 65)

    missing = []
    empty   = []
    total_skus = 0

    for svc in services:
        name  = svc["displayName"]
        sid   = svc["serviceId"]
        path  = os.path.join(sku_dir, f"{sid}.json.gz")

        if not os.path.exists(path):
            missing.append(svc)
            print(f"  {name:<43} MISSING")
            continue

        try:
            skus = json.load(gzip.open(path, "rt"))
            count = len(skus)
        except Exception:
            empty.append(svc)
            print(f"  {name:<43} CORRUPT")
            continue

        if count == 0:
            empty.append(svc)
            print(f"  {name:<43} EMPTY      {count:>6}")
        else:
            total_skus += count
            print(f"  {name:<43} OK         {count:>6}")

    # ── 4. Summary ────────────────────────────────────────────────
    print("-" * 65)
    ok_count = len(services) - len(missing) - len(empty)
    print(f"\nSummary:")
    print(f"  OK      : {ok_count}/{len(services)}")
    print(f"  Missing : {len(missing)}")
    print(f"  Empty   : {len(empty)}")
    print(f"  Stale   : {'YES (>7 days)' if is_stale else 'NO'}")

    problems = missing + empty

    # ── 5. Refresh if requested ───────────────────────────────────
    if args.refresh and problems:
        token_info = _gcp_token()
        if not token_info:
            print("\nWARNING: No GCP credentials found.")
            print("  Set GOOGLE_CLOUD_API_KEY or run `gcloud auth login`.")
        else:
            print(f"\nRefreshing {len(problems)} service(s) from GCP Cloud Billing API...")
            for svc in problems:
                name = svc["displayName"]
                sid  = svc["serviceId"]
                print(f"  Fetching {name}...", end="", flush=True)
                try:
                    skus = _fetch_service_skus(sid, token_info)
                    path = os.path.join(sku_dir, f"{sid}.json.gz")
                    with gzip.open(path, "wt") as f:
                        json.dump(skus, f)
                    print(f" {len(skus)} SKUs saved")
                except Exception as e:
                    print(f" FAILED: {e}")

    if is_stale and not args.refresh:
        print(f"\nHINT: Run with --refresh to fetch fresh SKUs from the GCP API.")

    exit_code = 1 if (missing or empty or is_stale) else 0
    print(f"\nExit code: {exit_code}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
