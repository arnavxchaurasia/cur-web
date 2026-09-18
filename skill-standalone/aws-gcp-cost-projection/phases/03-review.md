# Phase 3 — Review

**Run by:** one **fresh** sub-agent — not one of the mapping agents.
**Reads:** `review_flags.md` + `review_candidates.json` (written by `auto_review.py` before this phase).
**Writes:** `review_fixes.json` — the script applies all DB changes; the agent never touches the DB directly for flagged rows.
**Do NOT:** re-run Phase 2 scripts, do broad `SELECT *` queries, or write SQL UPDATEs for rows listed in `review_flags.md`.

---

## How it works

`auto_review.py` already ran. It detected two categories of problems and wrote:
- `review_flags.md` — human-readable, one section per flagged row with a pre-computed candidate where available
- `review_candidates.json` — machine-readable candidates for `apply_review_fixes.py` to consume

Your job: read every flagged row in `review_flags.md` and write `review_fixes.json` — a JSON array of decisions. Then the script applies them.

---

## Validation checklist — MANDATORY before writing any override

Read `data/validation-rules.json` at the start of this phase. Every `override` decision must pass all BLOCK-severity rules before you write it to `review_fixes.json`. Do not skip rules because a candidate "looks right" — run each check explicitly.

### Rule summary (full detail in `data/validation-rules.json`)

| Rule ID | Check | How |
|---|---|---|
| `no_hallucinated_sku_format` | gcp_sku_id matches `[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}` | Visual check — never invent an ID |
| `sku_exists_in_region` | SKU exists AND covers the row's `gcp_region` | `bash scripts/find-sku.sh "<gcp_service>" "<gcp_region>"` |
| `service_sku_consistency` | SKU's service (from find-sku.sh output) matches `gcp_service` you write | find-sku.sh output shows the owning service |
| `no_intentional_passthrough_override` | Row's `projection_note` does NOT contain "no GCP equivalent" / "no GCS equivalent" / "no direct GCP equivalent" | Read the note in `review_flags.md` |
| `tier_safety_sustained_to_burstable` | Non-t-family EC2 source (m5, c5, r6g, …) must NOT map to E2 or T2A Arm SKU | Check usage_type + gcp_sku_name |
| `multiplier_positive` | unit_multiplier > 0 | Arithmetic check |
| `multiplier_dimensional_correctness` | `core` rows use vCPU count; `ram` rows use RAM GiB | Cross-reference `instance_vcpus`/`instance_ram_gb` in `aws_li_catalog` |
| `multiplier_sanity_bounds` | vCPU ≤ 512, RAM ≤ 8192 GiB (WARN, not BLOCK) | If exceeded, verify against spec and note "Verified against instance spec" in reason |

### GCP/AWS API knowledge sources

When you need to verify a SKU's existence, region coverage, or pricing, consult:
- **GCP Billing Catalog API:** `https://cloud.google.com/billing/docs/reference/rest/v1/services.skus/list` — lists all real SKU IDs by service and region
- **GCP Compute Engine pricing:** `https://cloud.google.com/compute/vm-instance-pricing`
- **GCP Cloud SQL pricing:** `https://cloud.google.com/sql/pricing`
- **GCP Memorystore pricing:** `https://cloud.google.com/memorystore/docs/redis/pricing`
- **GCP BigQuery pricing:** `https://cloud.google.com/bigquery/pricing`
- **AWS EC2 instance specs:** `https://aws.amazon.com/ec2/instance-types/`
- **AWS ElastiCache node specs:** `https://aws.amazon.com/elasticache/pricing/`

The full list with all API URLs is in `data/validation-rules.json` under `gcp_api_knowledge_sources`.

**If any BLOCK rule fails for an override → use `veto` instead, document the failure in `reason`.**

---

## Step 1 — Write `review_fixes.json`

For **every** flagged row in `review_flags.md`, emit one entry:

```json
[
  {
    "aws_li_key": "abc123",
    "decision": "confirm",
    "reason": "candidate looks correct — EC2 m5.xlarge → N2 Custom Core"
  },
  {
    "aws_li_key": "def456",
    "decision": "override",
    "gcp_sku_id": "XXXX-YYYY-ZZZZ",
    "gcp_sku_name": "N2 Instance Core running in us-east4",
    "gcp_service": "Compute Engine",
    "reason": "candidate SKU was wrong region — found correct one via find-sku.sh"
  },
  {
    "aws_li_key": "ghi789",
    "decision": "veto",
    "reason": "AWS Support charge — no GCP equivalent, passthrough is correct"
  }
]
```

**Decision rules:**

| Decision | When to use |
|---|---|
| `confirm` | Confidence is `HIGH` and the pre-computed candidate looks correct — just accept it |
| `override` | Confidence is `LOW` (no candidate found) or the candidate is wrong — provide your own `gcp_sku_id` + `gcp_sku_name`, or a corrected `unit_multiplier` + `component` |
| `veto` | Row is a genuine passthrough (AWS Support, Marketplace, no real GCP equivalent) — document why in `reason` |

**Flag types and how to handle each:**

| Flag type | What it means | Action |
|---|---|---|
| **Passthrough requiring review** | strategy=passthrough, but no "no GCP equivalent" stamp — may have a real mapping | `confirm` pre-computed candidate, or `override`/`veto` |
| **Spec violation** | break_down row with wrong unit_multiplier vs. instance spec | `confirm` HIGH-confidence candidate (correct vCPU/RAM from spec) |
| **No SKU** | strategy=map/break_down but gcp_sku_id is NULL — rate-fill will produce $0 | `override` with correct gcp_sku_id found via `find-sku.sh`, or `veto` if should be passthrough |
| **Zero multiplier** | unit_multiplier = 0 or NULL — GCP cost = 0 regardless of rate | `override` with the correct numeric multiplier (vCPUs for core, RAM GiB for ram) |

For **spec violations**, all candidates are `HIGH` — `confirm` them unless the spec values look wrong. Do not invent multiplier values; correct values come from `instance_vcpus`/`instance_ram_gb` in `aws_li_catalog`.

For **LOW confidence passthrough rows** and **no-SKU rows**, look up the correct SKU:
```bash
bash scripts/find-sku.sh "<gcp_service>" "<region>"
```
Then use `override` with the found `gcp_sku_id`. If genuinely no GCP equivalent exists, `veto` and explain.

---

## Step 2 — Apply the fixes

Once `review_fixes.json` is written, run:

```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - << 'PYEOF'
# paste the apply_review_fixes.py code block (from "Embedded Scripts" section below) here
PYEOF
```

The script reads `review_fixes.json` + `review_candidates.json` and applies DB changes. It runs `llm_override_guard.py` as a final format gate (SKU ID regex, multiplier sign/type) — business-logic rules are your responsibility to enforce at Step 1 via the validation checklist above. Out-of-scope overrides (rows not in `review_candidates.json`) are rejected automatically.

---

## Step 3 — `Alt:` and `Open:` lines in `mapping-notes.md` (secondary, direct SQL)

Only do this after Step 2 is complete. These are NOT handled by the script pipeline — fix them via direct SQL UPDATE.

### `Alt:` lines — was the trade-off honest?
For each `## <aws_li_key>` entry with an `Alt:` line:
- Did the picked SKU win the 60/40 trade-off honestly?
- If the rationale has no line-item anchor from the bill, **demote to the alternative**.
- UPDATE `aws_li_to_gcp_li` directly.

### `Open:` lines — resolve the uncertainty
A `fail-by-Nx` or `degenerate` back-check is a real signal. Cross-reference other rows in the bill to confirm or fix the unit_multiplier. Fix with a direct UPDATE.

Append a brief justification to `mapping-notes.md` under `## Review findings`.

---

## Returning to main

One paragraph. Concrete numbers. Example:

> Confirmed 6 HIGH-confidence candidates, overrode 2 LOW-confidence passthroughs (found correct SKUs via find-sku.sh), vetoed 1 (AWS Support — no GCP equivalent). `apply_review_fixes.py` applied 8 fixes. Fixed 1 Alt: demotion and 1 Open: multiplier via direct SQL. Ready for Phase 4.


---

## Embedded Scripts

### scripts/auto_review.py

```python
# Source: originally scripts/auto_review.py
#!/usr/bin/env python3
"""
auto_review.py — Phase 3 suggestion engine. NEVER modifies the database.

Detects mapping issues and pre-computes candidate fixes:
  - Illegal passthroughs: core services (EC2/RDS/S3/etc.) marked passthrough
  - Spec violations: break_down rows with wrong unit_multipliers vs instance spec

Writes:
  review_flags.md         — human-readable report with candidates (LLM input)
  review_candidates.json  — machine-readable candidates (for apply_review_fixes.py)
"""
import duckdb
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

# ── Inlined: config_loader ────────────────────────────────────────────────────
import logging as _logging_cfg

def _load_data_config(name):
    """Load data/<name>.json from SKILL_DIR/data/. Returns {} on failure."""
    path = os.path.join(SKILL_DIR, "data", f"{name}.json")
    try:
        import json as _j
        with open(path, encoding="utf-8") as f:
            return _j.load(f)
    except Exception as _e:
        _logging_cfg.getLogger("config_loader").warning("config_loader: %s: %s", path, _e)
        return {}
_cfg = _load_data_config
# ── End config_loader ─────────────────────────────────────────────────────────

# ── Inlined: apply_static_mappings (resolve_sku + dependencies) ───────────────
import gzip, re as _re_asm

DATA_DIR = os.path.join(SKILL_DIR, "data")
RESOLVED_SKUS_FILE = os.path.join(DATA_DIR, "resolved_skus.json")

_gcp_cfg_asm = _cfg("gcp-model-config")
_svc_cfg_asm = _cfg("service-classification")

INTENTIONAL_PASSTHROUGH_NOTE_ILIKE = _svc_cfg_asm.get("intentional_passthrough_note_ilike", [
    "%no GCS equivalent%",
    "%no GCP equivalent%",
    "%no direct GCP equivalent%",
])

def intentional_passthrough_exclude_clause(column):
    return " AND ".join(
        f"COALESCE({column}, '') NOT ILIKE '{p}'" for p in INTENTIONAL_PASSTHROUGH_NOTE_ILIKE
    )

class SKUMeta(str):
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

_KNOWN_PHANTOM_AVAILABILITY = {
    tuple(pair) for pair in _gcp_cfg_asm.get("known_phantom_availability", [
        ["N4D Instance Core", "asia-south2"],
        ["N4D Instance Ram", "asia-south2"],
    ])
}

_SERVICES_CACHE = None
def _load_services():
    global _SERVICES_CACHE
    if _SERVICES_CACHE is not None:
        return _SERVICES_CACHE
    path = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        import json as _j
        _SERVICES_CACHE = {s["displayName"]: s["serviceId"] for s in _j.load(f)}
    return _SERVICES_CACHE

_SKU_FILE_CACHE = {}
def _load_sku_file(sku_file):
    if sku_file not in _SKU_FILE_CACHE:
        with gzip.open(sku_file, "rt") as f:
            import json as _j
            _SKU_FILE_CACHE[sku_file] = _j.load(f)
    return _SKU_FILE_CACHE[sku_file]

def _sku_meta(sku):
    unit = None
    pricing = sku.get("pricingInfo", [])
    if pricing:
        unit = pricing[0].get("pricingExpression", {}).get("usageUnit")
    resource_group = sku.get("category", {}).get("resourceGroup")
    return sku["skuId"], unit, resource_group

_FAMILY_RATE_CACHE = {}
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
    for sku in _load_sku_file(sku_file):
        desc = sku.get("description", "")
        if not _re_asm.search(desc_pattern, desc, _re_asm.IGNORECASE):
            continue
        dl = desc.lower()
        if any(q in dl for q in ("preemptible", "reserved", "commitment", "dws defined duration")):
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

def _family_hourly_rate(gcp_service, desc_pattern, gcp_region):
    cache_key = (gcp_service, desc_pattern, gcp_region)
    if cache_key in _FAMILY_RATE_CACHE:
        return _FAMILY_RATE_CACHE[cache_key]
    rate = _family_hourly_rate_uncached(gcp_service, desc_pattern, gcp_region)
    _FAMILY_RATE_CACHE[cache_key] = rate
    return rate

def _gcp_token():
    import subprocess
    api_key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GCP_API_KEY")
    if api_key:
        return ("key", api_key)
    try:
        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        if tok:
            return ("bearer", tok)
    except Exception:
        pass
    return None

def _live_sku_fetch(gcp_service, desc_pattern, gcp_region):
    import urllib.request, urllib.parse, json as _j
    services_file = os.path.join(DATA_DIR, "services.json")
    if not os.path.exists(services_file):
        return None
    with open(services_file) as f:
        svc_map = {s["displayName"]: s["serviceId"] for s in _j.load(f)}
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
                data = _j.loads(r.read())
        except Exception as e:
            print(f"  live fetch failed: {e}")
            return None
        for sku in data.get("skus", []):
            if not _re_asm.search(desc_pattern, sku.get("description", ""), _re_asm.IGNORECASE):
                continue
            geo = sku.get("geoTaxonomy", {})
            if geo.get("type") == "GLOBAL" or gcp_region in sku.get("serviceRegions", []):
                return sku["skuId"]
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return None

def lookup_sku_in_catalog(gcp_service, desc_pattern, gcp_region):
    if (desc_pattern, gcp_region) in _KNOWN_PHANTOM_AVAILABILITY:
        return None, None, None
    _CATALOG_SERVICE_ALIASES = {
        "Cloud VPN": "Networking", "Cloud DNS": "Networking",
        "Cloud NAT": "Networking", "Cloud Load Balancing": "Networking",
        "Cloud Armor": "Networking",
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
        "us": ["us-central1","us-east1","us-east4","us-east5","us-south1",
               "us-west1","us-west2","us-west3","us-west4"],
    }
    def _exact_region_match(sku):
        geo = sku.get("geoTaxonomy", {})
        if geo.get("type") == "GLOBAL":
            return True
        return bool(gcp_region and gcp_region in sku.get("serviceRegions", []))
    def _continent_region_match(sku):
        regions = sku.get("serviceRegions", [])
        for creg in _CONTINENT.values():
            if gcp_region in creg and any(r in creg for r in regions):
                return True
        return False
    _NOISE_QUALIFIERS = ["autoclass", "early delete", "dual-region", "multi-region", "regional",
                         "asynchronous replication protection", "confidential mode"]
    def _has_noise(sku_desc):
        dl = sku_desc.lower()
        pl = desc_pattern.lower()
        return any(q in dl and q not in pl for q in _NOISE_QUALIFIERS)
    desc_matches = [sku for sku in skus
                    if _re_asm.search(desc_pattern, sku.get("description", ""), _re_asm.IGNORECASE)]
    def _select(candidates):
        for sku in candidates:
            dl = sku.get("description", "").lower()
            if "preemptible" not in dl and not _has_noise(sku.get("description", "")):
                return _sku_meta(sku)
        for sku in candidates:
            if "preemptible" not in sku.get("description", "").lower():
                return _sku_meta(sku)
        for sku in candidates:
            return _sku_meta(sku)
        return None
    exact = [sku for sku in desc_matches if _exact_region_match(sku)]
    result = _select(exact)
    if result:
        return result
    continent = [sku for sku in desc_matches if _continent_region_match(sku)]
    result = _select(continent)
    if result:
        return result
    return None, None, None

_RUNTIME_SKU_CACHE = {}
_CATALOG_VERSION_CACHE = None

def _catalog_version():
    global _CATALOG_VERSION_CACHE
    if _CATALOG_VERSION_CACHE is None:
        meta_path = os.path.join(DATA_DIR, "CATALOG_META.json")
        try:
            import json as _j
            with open(meta_path) as f:
                _CATALOG_VERSION_CACHE = _j.load(f).get("fetched_at", "")
        except Exception:
            _CATALOG_VERSION_CACHE = ""
    return _CATALOG_VERSION_CACHE

def resolve_sku(gcp_service, desc_pattern, gcp_region):
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
            import json as _j
            with open(RESOLVED_SKUS_FILE) as f:
                cache = _j.load(f)
        except Exception:
            cache = {}
    if key in cache:
        v = cache[key]
        cached_sku_id = v.get("sku_id") if isinstance(v, dict) else v
        if cached_sku_id is not None:
            if isinstance(v, dict):
                return SKUMeta(v.get("sku_id"), v.get("unit"), v.get("resource_group"))
            return SKUMeta(v)
        if isinstance(v, dict) and v.get("catalog_version") == catalog_version:
            return SKUMeta(None, None, None)
    sku_id, unit, resource_group = lookup_sku_in_catalog(gcp_service, desc_pattern, gcp_region)
    if sku_id is None:
        print(f"  SKU not in bundled catalog, trying live API: {gcp_service} / {desc_pattern!r}")
        raw = _live_sku_fetch(gcp_service, desc_pattern, gcp_region)
        sku_id, unit, resource_group = raw, None, None
    if sku_id:
        print(f"  resolved SKU: {gcp_service} / {desc_pattern!r} -> {sku_id}")
    else:
        print(f"  WARNING: no SKU found for {gcp_service} / {desc_pattern!r} in {gcp_region}")
    _RUNTIME_SKU_CACHE[key] = {
        "sku_id": sku_id, "unit": unit, "resource_group": resource_group,
        "catalog_version": catalog_version if sku_id is None else None,
    }
    return SKUMeta(sku_id, unit, resource_group)

def _strict_resolve_sku(gcp_service, desc_pattern, gcp_region):
    """Like resolve_sku() but refuses continent-fallback substitution."""
    if _family_hourly_rate(gcp_service, desc_pattern, gcp_region) is None:
        return None
    return resolve_sku(gcp_service, desc_pattern, gcp_region)
# ── End apply_static_mappings ─────────────────────────────────────────────────
DB_PATH         = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
FLAGS_FILE      = os.path.join(JOB_DIR, "review_flags.md")
CANDIDATES_FILE = os.path.join(JOB_DIR, "review_candidates.json")

# Materiality threshold loaded from data/review-config.json.
# Passthrough rows below this cost are not worth the LLM's time.
# Increase to reduce review volume; decrease to catch more edge cases.
_PASSTHROUGH_MATERIALITY_USD = _cfg("review-config").get("passthrough_materiality_usd", 5.0)


def main():
    if not os.path.exists(DB_PATH):
        print("Database not found.")
        sys.exit(0)

    conn = duckdb.connect(DB_PATH)

    tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
    if "aws_li_to_gcp_li" not in tables:
        print("ERROR: aws_li_to_gcp_li table missing — Phase 2 did not complete.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 1. Detect candidate passthroughs for agent review                   #
    # All workload passthrough rows above the materiality threshold are   #
    # flagged — no hardcoded service list. The agent decides which are    #
    # legitimate (AWS Support, Marketplace, truly no GCP equivalent) and  #
    # which need a real mapping. Rows that static mappers intentionally   #
    # stamped "no GCP/GCS equivalent" are excluded automatically.        #
    # ------------------------------------------------------------------ #

    note_exclude = intentional_passthrough_exclude_clause("m.projection_note")
    illegal_rows = conn.execute(f"""
        SELECT c.aws_li_key, c.product, c.usage_type, c.operation,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region,
               m.gcp_service, m.gcp_sku_name, m.gcp_sku_id, m.projection_note
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy = 'passthrough'
          AND c.is_workload
          AND c.aws_amortized_cost >= {_PASSTHROUGH_MATERIALITY_USD}
          AND {note_exclude}
        ORDER BY c.aws_amortized_cost DESC
    """).fetchall()

    # ------------------------------------------------------------------ #
    # 2. Detect spec violations (break_down multiplier wrong)              #
    # ------------------------------------------------------------------ #

    SKILL_DIR_PATH = os.environ.get("SKILL_DIR", "")
    CATALOG_DB = os.path.join(SKILL_DIR_PATH, "data", "catalog.duckdb")
    spec_violations = []

    if os.path.exists(CATALOG_DB):
        try:
            conn.execute(f"ATTACH '{CATALOG_DB}' AS catalog (READ_ONLY)")
            spec_violations = conn.execute("""
                WITH gcp_caps AS (
                    SELECT
                        aws_li_key,
                        MAX(CASE WHEN component = 'core' THEN unit_multiplier ELSE 0.0 END) AS gcp_vcpu,
                        MAX(CASE WHEN component = 'ram'  THEN unit_multiplier ELSE 0.0 END) AS gcp_ram,
                        MAX(CASE WHEN component = 'core' THEN gcp_sku_id ELSE NULL END) AS core_sku
                    FROM aws_li_to_gcp_li
                    WHERE strategy IN ('map', 'break_down')
                    GROUP BY aws_li_key
                )
                SELECT
                    c.aws_li_key, c.instance_type,
                    c.instance_vcpus, c.instance_ram_gb,
                    g.gcp_vcpu, g.gcp_ram,
                    cat.description, c.gcp_region,
                    m.gcp_service, m.gcp_sku_name
                FROM aws_li_catalog c
                JOIN gcp_caps g USING (aws_li_key)
                JOIN catalog.skus cat ON cat.sku_id = g.core_sku
                JOIN aws_li_to_gcp_li m
                  ON m.aws_li_key = c.aws_li_key AND m.component = 'core'
                -- shared-core SKUs are identified dynamically from the catalog:
                -- GCP marks burstable/shared-core instances with "shared" in the
                -- description or via the resource_group field — no hardcoded name list.
                WHERE c.instance_vcpus IS NOT NULL AND c.instance_ram_gb IS NOT NULL
                  AND (
                    ((g.gcp_ram / g.gcp_vcpu) < (c.instance_ram_gb / c.instance_vcpus) AND g.gcp_vcpu > 0)
                    OR
                    (c.instance_type NOT LIKE 't%' AND (
                        cat.description ILIKE '%shared-core%'
                        OR cat.description ILIKE '%shared core%'
                        OR cat.resource_group ILIKE '%SharedCore%'
                        OR cat.resource_group ILIKE '%Micro%'
                    ))
                  )
            """).fetchall()
            conn.execute("DETACH catalog")
        except Exception as e:
            print(f"Warning: catalog spec check skipped: {e}")

    # ------------------------------------------------------------------ #
    # 3. Detect mapped rows with no SKU (rate-fill will produce $0)        #
    # ------------------------------------------------------------------ #
    no_sku_rows = conn.execute("""
        SELECT c.aws_li_key, c.product, c.usage_type,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region, m.gcp_service, m.gcp_sku_name, m.component, m.strategy
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy IN ('map', 'break_down')
          AND (m.gcp_sku_id IS NULL OR m.gcp_sku_id = '')
          AND c.is_workload
          AND c.aws_amortized_cost >= {threshold}
        ORDER BY c.aws_amortized_cost DESC
    """.format(threshold=_PASSTHROUGH_MATERIALITY_USD)).fetchall()

    # ------------------------------------------------------------------ #
    # 4. Detect zero / null unit_multiplier on mapped rows (silent $0)     #
    # ------------------------------------------------------------------ #
    zero_mult_rows = conn.execute("""
        SELECT c.aws_li_key, c.product, c.usage_type,
               ROUND(c.aws_amortized_cost, 2) AS cost,
               c.gcp_region, m.gcp_service, m.gcp_sku_name,
               m.component, m.unit_multiplier, m.strategy
        FROM aws_li_catalog c JOIN aws_li_to_gcp_li m USING(aws_li_key)
        WHERE m.strategy IN ('map', 'break_down')
          AND (m.unit_multiplier IS NULL OR m.unit_multiplier <= 0)
          AND c.is_workload
          AND c.aws_amortized_cost >= {threshold}
        ORDER BY c.aws_amortized_cost DESC
    """.format(threshold=_PASSTHROUGH_MATERIALITY_USD)).fetchall()

    conn.close()

    # ------------------------------------------------------------------ #
    # 5. Compute candidates                                                #
    # ------------------------------------------------------------------ #

    candidates = {}

    # Illegal passthrough candidates: try resolve_sku
    for row in illegal_rows:
        key = row[0]
        gcp_service  = row[6] or ""
        gcp_sku_name = row[7] or ""
        region       = row[5] or "us-central1"

        candidate  = None
        confidence = "NONE"

        if gcp_service and gcp_sku_name:
            try:
                # _strict_resolve_sku, not resolve_sku — a row a static mapper
                # already correctly left as passthrough because
                # cheapest_in_scope()/_family_hourly_rate() found no rate in
                # this EXACT region (e.g. an ARM EC2 row with no GCP ARM
                # family available in-region) still carries its intended
                # gcp_service/gcp_sku_name, so it matches this "illegal
                # passthrough" heuristic by name alone. Plain resolve_sku()
                # would then continent-fallback to a DIFFERENT region's SKU
                # and "fix" this row back into a silently wrong-region price
                # — undoing that mapper's correct decision. Confirmed real:
                # this is exactly what happened for an ARM row in Delhi
                # resolving to Taiwan's C4A Arm SKU/price.
                sku_id = _strict_resolve_sku(gcp_service, gcp_sku_name, region)
                if sku_id:
                    candidate = {
                        "action": "set_sku_and_map",
                        "gcp_sku_id": sku_id,
                        "gcp_sku_name": gcp_sku_name,
                        "gcp_service": gcp_service,
                    }
                    confidence = "HIGH"
                else:
                    confidence = "LOW"
            except Exception:
                confidence = "LOW"

        candidates[key] = {
            "type": "illegal_passthrough",
            "confidence": confidence,
            "candidate": candidate,
        }

    # Spec violation candidates: correct multipliers from instance spec
    for row in spec_violations:
        key = row[0]
        instance_vcpus  = row[2]
        instance_ram_gb = row[3]
        candidates[key] = {
            "type": "spec_violation",
            "confidence": "HIGH",
            "candidate": {
                "action": "fix_multipliers",
                "core_multiplier": float(instance_vcpus) if instance_vcpus is not None else None,
                "ram_multiplier":  float(instance_ram_gb) if instance_ram_gb is not None else None,
            },
        }

    # No-SKU rows: no pre-computed candidate — agent must supply gcp_sku_id via override
    for row in no_sku_rows:
        key = row[0]
        if key not in candidates:  # spec_violation or passthrough may already cover this key
            candidates[key] = {
                "type": "no_sku",
                "confidence": "LOW",
                "candidate": None,
            }

    # Zero/null multiplier rows: no pre-computed candidate — agent must supply correct value
    for row in zero_mult_rows:
        key = row[0]
        if key not in candidates:
            candidates[key] = {
                "type": "zero_multiplier",
                "confidence": "LOW",
                "candidate": None,
            }

    # ------------------------------------------------------------------ #
    # 4. Write review_candidates.json                                      #
    # ------------------------------------------------------------------ #

    with open(CANDIDATES_FILE, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)

    # ------------------------------------------------------------------ #
    # 5. Write review_flags.md                                             #
    # ------------------------------------------------------------------ #

    total_flags = len(illegal_rows) + len(spec_violations) + len(no_sku_rows) + len(zero_mult_rows)

    with open(FLAGS_FILE, "w", encoding="utf-8") as f:
        f.write("# Phase 3 Review Flags\n\n")
        f.write(f"Total flags: **{total_flags}** "
                f"({len(illegal_rows)} passthrough, {len(spec_violations)} spec violations, "
                f"{len(no_sku_rows)} no-SKU, {len(zero_mult_rows)} zero-multiplier)\n\n")

        if total_flags == 0:
            f.write("No issues detected. Return `[]` (empty array).\n")
            print("auto_review: 0 flags — no issues.")
            return

        f.write("Return `review_fixes.json` — a JSON array:\n")
        f.write('```json\n[{"aws_li_key": "...", "decision": "confirm|override|veto",\n'
                '  "gcp_sku_id": "...", "gcp_sku_name": "...",\n'
                '  "unit_multiplier": 4.0, "component": "core", "reason": "..."}]\n```\n\n')
        f.write("- `confirm`: apply the pre-computed candidate as-is\n")
        f.write("- `override`: supply your own values\n")
        f.write("- `veto`: skip (document why in reason)\n\n")
        f.write("---\n\n")

        if illegal_rows:
            f.write("## Passthrough Rows Requiring Review\n\n")
            f.write("These workload rows are set to passthrough but were not intentionally stamped "
                    "by a static mapper as having no GCP equivalent. For each row: confirm a "
                    "pre-computed candidate, supply your own SKU via override, or veto if it is "
                    "genuinely a valid passthrough (AWS Support, Marketplace, no GCP equivalent).\n\n")
            for row in illegal_rows:
                key = row[0]
                cand_info = candidates.get(key, {})
                conf = cand_info.get("confidence", "NONE")
                cand = cand_info.get("candidate")

                f.write(f"### `{key}` — Confidence: `{conf}`\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **Operation**: {row[3]}\n")
                f.write(f"- **AWS cost**: ${row[4]}\n")
                f.write(f"- **Current gcp_service**: {row[6]!r}\n")
                f.write(f"- **Current gcp_sku_name**: {row[7]!r}\n")
                if row[9]:
                    f.write(f"- **projection_note**: {row[9]}\n")

                if cand:
                    f.write(f"\n**Candidate** (`{conf}`): "
                            f"set gcp_sku_id=`{cand['gcp_sku_id']}`, "
                            f"gcp_sku_name=`{cand['gcp_sku_name']}`, strategy=`map`\n\n")
                else:
                    f.write("\n**No candidate found** — provide gcp_sku_id and gcp_sku_name in override.\n\n")

        if spec_violations:
            f.write("---\n\n")
            f.write("## Spec Violations\n\n")
            f.write("These break_down rows have wrong unit_multipliers. "
                    "Correct values (HIGH confidence) are from the instance spec.\n\n")
            for row in spec_violations:
                key = row[0]
                f.write(f"### `{key}` — Confidence: `HIGH`\n\n")
                f.write(f"- **Instance**: {row[1]}\n")
                f.write(f"- **AWS spec**: {row[2]} vCPU, {row[3]} GB RAM\n")
                f.write(f"- **Current GCP mapping**: {row[4]} vCPU, {row[5]} GB RAM\n")
                f.write(f"- **SKU description**: {row[6]}\n")
                f.write(f"\n**Candidate**: set core unit_multiplier={row[2]}, "
                        f"ram unit_multiplier={row[3]}\n\n")

        if no_sku_rows:
            f.write("---\n\n")
            f.write("## Mapped Rows With No SKU (silent $0 risk)\n\n")
            f.write("These rows have strategy='map'/'break_down' but no gcp_sku_id. "
                    "Rate-fill cannot price them — they will produce $0 GCP cost. "
                    "For each row: use `override` with the correct gcp_sku_id, or `veto` "
                    "if the row should be passthrough.\n\n")
            for row in no_sku_rows:
                key = row[0]
                f.write(f"### `{key}` — No SKU\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **AWS cost**: ${row[3]}\n")
                f.write(f"- **Region**: {row[4]}\n")
                f.write(f"- **gcp_service**: {row[5]!r}\n")
                f.write(f"- **gcp_sku_name**: {row[6]!r}\n")
                f.write(f"- **component**: {row[7]}, **strategy**: {row[8]}\n")
                f.write("\n**Action required**: override with correct gcp_sku_id, or veto.\n\n")

        if zero_mult_rows:
            f.write("---\n\n")
            f.write("## Zero / Null unit_multiplier (silent $0 risk)\n\n")
            f.write("These mapped rows have unit_multiplier = 0 or NULL. "
                    "GCP cost = usage × multiplier × rate, so a zero multiplier produces $0 regardless of rate. "
                    "For each row: use `override` with the correct unit_multiplier value.\n\n")
            for row in zero_mult_rows:
                key = row[0]
                f.write(f"### `{key}` — Zero Multiplier\n\n")
                f.write(f"- **Product**: {row[1]}\n")
                f.write(f"- **Usage type**: {row[2]}\n")
                f.write(f"- **AWS cost**: ${row[3]}\n")
                f.write(f"- **Region**: {row[4]}\n")
                f.write(f"- **gcp_service**: {row[5]!r}, **gcp_sku_name**: {row[6]!r}\n")
                f.write(f"- **component**: {row[7]}, **current unit_multiplier**: {row[8]}\n")
                f.write("\n**Action required**: override with correct unit_multiplier.\n\n")

    print(f"auto_review: {len(illegal_rows)} passthrough, {len(spec_violations)} spec violations, "
          f"{len(no_sku_rows)} no-SKU, {len(zero_mult_rows)} zero-multiplier. "
          f"Total {total_flags} flags.")


if __name__ == "__main__":
    main()

```

### scripts/apply_review_fixes.py

```python
# Source: originally scripts/apply_review_fixes.py
#!/usr/bin/env python3
"""
apply_review_fixes.py — Phase 3 single application point.

Reads:
  review_fixes.json       — LLM output (confirm / override / veto per aws_li_key)
  review_candidates.json  — auto_review.py pre-computed candidates

For each fix:
  confirm  → apply auto_review's pre-validated candidate
  override → apply LLM's values (schema-validated before DB write)
  veto     → leave row unchanged (log reason)

All DB writes happen here — auto_review.py and the LLM never touch the DB.
"""
import duckdb
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

# ── Inlined: config_loader ────────────────────────────────────────────────────
import logging as _logging_cfg2

def _load_data_config2(name):
    path = os.path.join(SKILL_DIR, "data", f"{name}.json")
    try:
        import json as _j
        with open(path, encoding="utf-8") as f:
            return _j.load(f)
    except Exception as _e:
        _logging_cfg2.getLogger("config_loader").warning("config_loader: %s: %s", path, _e)
        return {}
# ── End config_loader ─────────────────────────────────────────────────────────

# ── Inlined: llm_override_guard ───────────────────────────────────────────────
import re as _re_guard

_review_cfg = _load_data_config2("review-config")
_MAX_MULT_VCPU   = _review_cfg.get("max_unit_multiplier_vcpu",   512)
_MAX_MULT_RAM_GB = _review_cfg.get("max_unit_multiplier_ram_gb", 8192)
_SKU_ID_RE = _re_guard.compile(r'^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$')

def validate_override(conn, aws_li_key, component, fix):
    """Final format/schema gate before writing an LLM override to the DB.
    Returns (ok: bool, reason: str).
    """
    gcp_sku_id = fix.get("gcp_sku_id")
    unit_mult  = fix.get("unit_multiplier")
    if gcp_sku_id and not _SKU_ID_RE.match(str(gcp_sku_id)):
        return False, (
            f"gcp_sku_id {gcp_sku_id!r} does not match the GCP SKU ID format "
            f"(expected XXXX-XXXX-XXXX hex). This is not a real SKU ID."
        )
    if unit_mult is not None:
        try:
            mult_f = float(unit_mult)
        except (TypeError, ValueError):
            return False, f"unit_multiplier {unit_mult!r} is not a number"
        if mult_f <= 0:
            return False, (
                f"unit_multiplier must be > 0 (got {mult_f}). "
                f"A zero or negative multiplier produces $0 GCP cost."
            )
        comp = (component or "core").lower()
        limit = _MAX_MULT_RAM_GB if comp == "ram" else _MAX_MULT_VCPU
        if mult_f > limit:
            return False, (
                f"unit_multiplier {mult_f} exceeds the sanity limit of {limit} "
                f"for component {comp!r}. Update data/review-config.json if correct."
            )
    return True, ""
# ── End llm_override_guard ────────────────────────────────────────────────────

DB_PATH         = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
FIXES_FILE      = os.path.join(JOB_DIR, "review_fixes.json")
CANDIDATES_FILE = os.path.join(JOB_DIR, "review_candidates.json")
NOTES_FILE      = os.path.join(JOB_DIR, "mapping-notes.md")


def apply_candidate(conn, key: str, cand: dict, notes: list):
    action = cand.get("action")

    if action == "set_sku_and_map":
        conn.execute("""
            UPDATE aws_li_to_gcp_li
            SET gcp_sku_id = ?, gcp_sku_name = ?, gcp_service = ?, strategy = 'map'
            WHERE aws_li_key = ?
        """, [cand["gcp_sku_id"], cand["gcp_sku_name"], cand.get("gcp_service"), key])
        notes.append(f"- `{key}`: confirmed — SKU {cand['gcp_sku_id']} ({cand['gcp_sku_name']}), strategy=map")

    elif action == "fix_multipliers":
        if cand.get("core_multiplier") is not None:
            conn.execute("""
                UPDATE aws_li_to_gcp_li SET unit_multiplier = ?
                WHERE aws_li_key = ? AND component = 'core'
            """, [cand["core_multiplier"], key])
        if cand.get("ram_multiplier") is not None:
            conn.execute("""
                UPDATE aws_li_to_gcp_li SET unit_multiplier = ?
                WHERE aws_li_key = ? AND component = 'ram'
            """, [cand["ram_multiplier"], key])
        notes.append(f"- `{key}`: confirmed spec fix — "
                     f"core={cand.get('core_multiplier')}, ram={cand.get('ram_multiplier')}")
    else:
        notes.append(f"- `{key}`: unknown candidate action '{action}' — skipped")


def apply_override(conn, key: str, fix: dict, notes: list):
    gcp_sku_id   = fix.get("gcp_sku_id")
    gcp_sku_name = fix.get("gcp_sku_name")
    unit_mult    = fix.get("unit_multiplier")
    component    = fix.get("component")  # Default to None to keep backward compatibility
    gcp_service  = fix.get("gcp_service")
    reason       = fix.get("reason", "")

    if gcp_sku_id and gcp_sku_name:
        updates = {"gcp_sku_id": gcp_sku_id, "gcp_sku_name": gcp_sku_name, "strategy": "map"}
        if gcp_service:
            updates["gcp_service"] = gcp_service
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        if component is not None:
            values = list(updates.values()) + [key, component]
            conn.execute(f"UPDATE aws_li_to_gcp_li SET {set_clause} WHERE aws_li_key = ? AND component = ?", values)
            notes.append(f"- `{key}`: override — SKU {gcp_sku_id} ({gcp_sku_name}) [{component}] — {reason}")
        else:
            values = list(updates.values()) + [key]
            conn.execute(f"UPDATE aws_li_to_gcp_li SET {set_clause} WHERE aws_li_key = ?", values)
            notes.append(f"- `{key}`: override — SKU {gcp_sku_id} ({gcp_sku_name}) — {reason}")

    if unit_mult is not None:
        comp_mult = component if component is not None else "core"
        conn.execute("""
            UPDATE aws_li_to_gcp_li SET unit_multiplier = ?
            WHERE aws_li_key = ? AND component = ?
        """, [float(unit_mult), key, comp_mult])
        notes.append(f"- `{key}`: override multiplier — {unit_mult} ({comp_mult}) — {reason}")


def main():
    if not os.path.exists(FIXES_FILE):
        print("review_fixes.json not found — LLM may not have written it (possibly 0 flags). Skipping.")
        sys.exit(0)

    try:
        with open(FIXES_FILE) as f:
            fixes = json.load(f)
    except Exception as e:
        print(f"WARNING: Could not parse review_fixes.json: {e} — skipping review fixes.", file=sys.stderr)
        sys.exit(0)

    if not isinstance(fixes, list):
        print("WARNING: review_fixes.json must be a JSON array — skipping review fixes.", file=sys.stderr)
        sys.exit(0)

    candidates = {}
    if os.path.exists(CANDIDATES_FILE):
        try:
            with open(CANDIDATES_FILE) as f:
                candidates = json.load(f)
        except Exception:
            pass

    conn = duckdb.connect(DB_PATH)
    notes = []
    applied = vetoed = errors = 0

    for fix in fixes:
        key      = fix.get("aws_li_key")
        decision = (fix.get("decision") or "").lower()
        reason   = fix.get("reason", "")

        if not key or decision not in ("confirm", "override", "veto"):
            print(f"WARNING: Skipping malformed fix entry: {fix}")
            errors += 1
            continue

        if decision == "veto":
            vetoed += 1
            notes.append(f"- `{key}`: vetoed — {reason}")
            continue

        if decision == "confirm":
            cand_info = candidates.get(key, {})
            cand = cand_info.get("candidate")
            if not cand:
                print(f"WARNING: confirm for {key} but no pre-computed candidate — treating as veto")
                vetoed += 1
                continue
            try:
                apply_candidate(conn, key, cand, notes)
                applied += 1
            except Exception as e:
                print(f"ERROR applying candidate for {key}: {e}", file=sys.stderr)
                errors += 1

        elif decision == "override":
            # auto_review.py's review_candidates.json is the authoritative
            # flagged-row list. "confirm" already implicitly requires the key to
            # have a real candidate there, but "override" applied the LLM's own
            # fields with no such check — letting the LLM freelance a fix on any
            # row it liked. Observed in practice: the LLM invented its own
            # "violation" on a row that was never flagged (an intentional,
            # correct S3 Glacier Early-Delete passthrough) and overrode it to a
            # wrong SKU, producing a real ~50x under-projection outside any
            # review that was actually asked for.
            if candidates and key not in candidates:
                print(f"WARNING: {key} was never flagged by auto_review.py (not "
                      f"in review_candidates.json) — ignoring out-of-scope "
                      f"override, row left untouched.")
                errors += 1
                continue
            has_sku  = fix.get("gcp_sku_id") and fix.get("gcp_sku_name")
            has_mult = fix.get("unit_multiplier") is not None
            if not has_sku and not has_mult:
                print(f"WARNING: override for {key} has no actionable fields — skipping")
                errors += 1
                continue
            # Content-check the override itself — the scope check above only
            # verifies the ROW was allowed to be touched, not that the VALUES
            # being written are real (a hallucinated gcp_sku_id) or safe (un-
            # ignoring an intentionally-protected passthrough, or silently
            # crossing from a sustained AWS source onto a burstable GCP family).
            ok, why = validate_override(conn, key, fix.get("component"), fix)
            if not ok:
                print(f"WARNING: override for {key} rejected — {why}")
                errors += 1
                continue
            try:
                apply_override(conn, key, fix, notes)
                applied += 1
            except Exception as e:
                print(f"ERROR applying override for {key}: {e}", file=sys.stderr)
                errors += 1

    conn.close()

    with open(NOTES_FILE, "a") as f:
        if notes:
            f.write("\n\n## Phase 3 Review Fixes\n\n")
            for note in notes:
                f.write(note + "\n")

    print(f"apply_review_fixes: applied={applied} vetoed={vetoed} errors={errors}")


if __name__ == "__main__":
    main()

```
