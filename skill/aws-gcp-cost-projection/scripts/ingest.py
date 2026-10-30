#!/usr/bin/env python3
"""
ingest.py — Phase 1. SINGLE RESPONSIBILITY: EXTRACTION.

Turn any supported bill format into a faithful, normalized catalog of line
items. This is the extraction layer; it does not resolve canonical identity,
classify charge-type, or map to GCP.

OWNS (its whole job):
  1. Format detection + loading — CSV/TSV (+gz/zst), Parquet, JSON, Excel, PDF,
     ZIP; agy fallback (normalized_bill.csv) for unrecognized shapes.
  2. Dedup into aws_li_catalog — one row per distinct line-item type, stable
     aws_li_key (md5 of the dedup tuple).
  3. Faithful field extraction — product, product_code (raw CUR
     lineItem/ProductCode), usage_type, operation, region->gcp_region,
     pricing_model, line_item_type, is_workload, total_usage, cost,
     license/OS/engine/deployment/volume, pricing_unit, billing_format.
  4. Structured enrichment derivable from the row — instance_type, vcpus,
     ram_gb, arch, instance_count, billing_days, effective_unit_rate.
  5. Bill integrity — PDF total reconciliation, materiality filter.

OUTPUT GUARANTEE: a fully-populated aws_li_catalog carrying the raw facts +
product_code (when the format has one) + structured fields where derivable.
It promises identity RAW MATERIAL (product/product_code), not RESOLVED identity.

EXPLICITLY NOT ingest's job (owned by later layers, keep them out of here):
  - canonical_service resolution   -> aws_normalizer.py / resolve_unknown_product_codes.py
  - mechanic_group (charge-type)   -> classify_mechanics.py
  - any SKU / GCP pricing decision  -> apply_static_mappings.py / family_mapper.py

(The only identity call here is _canonical_service(), used solely as a PDF
section-header parsing heuristic — an extraction aid, not catalog identity.)
"""
import duckdb
import os
import sys
import glob
import json
import re
import hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_utils import get_logger
from region_prefix import decode_region_prefix
from aws_normalizer import canonical_service as _canonical_service
from config_loader import load_data_config as _cfg
from config_loader import load_assumptions as _load_assumptions


def _od_divisor(disc: float) -> float:
    """(1 - discount) with the same guard projection_view uses: a discount >= 1
    or a non-positive divisor would blow up the reconstruction, so clamp to 1.0
    (no reconstruction) in those degenerate cases."""
    d = (1.0 - disc) if disc < 1.0 else 1.0
    return d if d > 0 else 1.0


def apply_od_basis(conn, assumptions: dict | None = None) -> None:
    """Stamp aws_od_cost, sp_ri_discount_pct, od_basis on every aws_li_catalog row
    (WORKORDER_od_vs_od_sp_ri). Single source of truth for the SP/RI/Spot discount
    rates: the SAME assumptions projection_view reads (config_loader.load_assumptions),
    so an operator override is honoured here instead of being silently overridden by
    a hardcoded default that then wins the view's COALESCE(aws_od_cost, ...).

    Basis vocabulary:
      public_od            - real publicOnDemandCost, or genuine on-demand usage.
      reconstructed_default- no public OD; reconstructed from a PORTFOLIO-DEFAULT
                             rate (not a per-SP/RI resolved rate). An estimate, and
                             labelled so it is distinguishable and countable.
      amortized_fallback   - no public OD and no applicable rate; aws_od_cost is the
                             billed amortized cost, untouched. No rate invented.
    """
    a = assumptions or _load_assumptions() or {}
    spot_div = _od_divisor(float(a.get("aws_spot_discount_pct", 0.70)))
    ri1_div = _od_divisor(float(a.get("aws_ri_1yr_discount_pct", 0.38)))
    ri3_div = _od_divisor(float(a.get("aws_ri_3yr_discount_pct", 0.52)))
    spot_pct = float(a.get("aws_spot_discount_pct", 0.70))
    ri1_pct = float(a.get("aws_ri_1yr_discount_pct", 0.38))
    ri3_pct = float(a.get("aws_ri_3yr_discount_pct", 0.52))

    _commit_match = (
        "LOWER(COALESCE(pricing_model, '')) IN ('committed', 'reserved')\n"
        "             OR LOWER(COALESCE(line_item_type, '')) = 'discountedusage'\n"
        "             OR LOWER(COALESCE(line_item_type, '')) = 'savingsplancoveredusage'\n"
        "             OR COALESCE(usage_type, '') ILIKE '%reserved%'\n"
        "             OR COALESCE(operation, '') ILIKE '%reserved%'\n"
        "             OR COALESCE(usage_type, '') ILIKE '%savings%plan%'\n"
        "             OR COALESCE(operation, '') ILIKE '%savings%plan%'"
    )
    _spot_match = (
        "LOWER(COALESCE(pricing_model, '')) = 'spot'\n"
        "             OR COALESCE(usage_type, '') ILIKE '%spot%'\n"
        "             OR COALESCE(operation, '') ILIKE '%spot%'"
    )
    _is_3yr = ("REGEXP_MATCHES(LOWER(COALESCE(usage_type, '') || ' ' || COALESCE(operation, '')), "
               "'\\b(3-year|36\\s*mo|36\\s*month)\\b|3\\s+yr|3\\s+year|3yr')")

    conn.execute(f"""
        UPDATE aws_li_catalog
        SET
            aws_od_cost = CASE
                WHEN public_on_demand_cost IS NOT NULL AND public_on_demand_cost > 0
                    THEN public_on_demand_cost
                WHEN LOWER(COALESCE(pricing_model, '')) = 'ondemand' AND LOWER(COALESCE(line_item_type, '')) = 'usage'
                    THEN aws_amortized_cost
                WHEN {_spot_match}
                    THEN CASE WHEN aws_amortized_cost > 0 THEN aws_amortized_cost / {spot_div} ELSE aws_amortized_cost END
                WHEN {_commit_match}
                    THEN CASE WHEN aws_amortized_cost > 0
                              THEN aws_amortized_cost / CASE WHEN {_is_3yr} THEN {ri3_div} ELSE {ri1_div} END
                              ELSE aws_amortized_cost END
                ELSE aws_amortized_cost
            END,
            sp_ri_discount_pct = CASE
                WHEN public_on_demand_cost IS NOT NULL AND public_on_demand_cost > aws_amortized_cost AND public_on_demand_cost > 0
                    THEN (public_on_demand_cost - aws_amortized_cost) / public_on_demand_cost
                WHEN LOWER(COALESCE(pricing_model, '')) = 'ondemand' AND LOWER(COALESCE(line_item_type, '')) = 'usage'
                    THEN 0.0
                WHEN {_spot_match}
                    THEN {spot_pct}
                WHEN {_commit_match}
                    THEN CASE WHEN {_is_3yr} THEN {ri3_pct} ELSE {ri1_pct} END
                ELSE 0.0
            END,
            od_basis = CASE
                WHEN public_on_demand_cost IS NOT NULL AND public_on_demand_cost > 0
                    THEN 'public_od'
                WHEN LOWER(COALESCE(pricing_model, '')) = 'ondemand' AND LOWER(COALESCE(line_item_type, '')) = 'usage'
                    THEN 'public_od'
                WHEN {_spot_match}
                    THEN 'reconstructed_default'
                WHEN {_commit_match}
                    THEN 'reconstructed_default'
                ELSE 'amortized_fallback'
            END
    """)
try:
    from diagnostics import diag, WARN
except ImportError:
    def diag(*a, **k): pass
    WARN = "warn"
from currency_utils import (
    detect_currency_and_rate,
    save_currency_info,
    load_currency_info,
)

log = get_logger("ingest")

_WL_CFG = _cfg("workload_classification")
_WL_TYPES = set(_WL_CFG.get("workload_line_item_types", ["Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"]))
# Substring patterns (safe for multi-word phrases + concatenated product-code tokens).
_NONWL_PATTERNS = _WL_CFG.get("nonworkload_patterns", [])
# Word-boundary patterns (short words that would false-match inside longer words,
# e.g. tax->syntax, credit->accredited). Matched with \b...\b, case-insensitive.
_NONWL_WORD_PATTERNS = _WL_CFG.get("nonworkload_word_patterns", [])
_NONWL_WORD_RES = [re.compile(r'\b' + wp + r'\b', re.IGNORECASE) for wp in _NONWL_WORD_PATTERNS]

def _is_workload_py(line_item_type, *text_fields):
    """Python-path verdict (PDF / usage-units parsers)."""
    blob = " ".join((t or "") for t in text_fields).lower()
    if any(p in blob for p in _NONWL_PATTERNS):
        return False
    if any(rx.search(blob) for rx in _NONWL_WORD_RES):
        return False
    if line_item_type and line_item_type not in _WL_TYPES:
        return False          # authoritative enum says non-consumption
    return True

def classify_charge(*, line_item_type_raw=None, product="", usage_type="",
                    operation="", cost=0.0, category=None):
    """Return (charge_category, line_item_type, is_workload).

    charge_category follows FOCUS: 'Usage' | 'Purchase' | 'Credit' | 'Tax'.
    One definition, used by every ingest branch.
    When a raw line_item_type is given (authoritative from CUR), trust it.
    """
    cost_val = float(cost or 0.0)
    cat_str = str(category or "")
    prod_str = str(product or "")
    ut_str = str(usage_type or "")
    op_str = str(operation or "")
    blob = f"{prod_str} {ut_str} {op_str} {cat_str}".lower()

    if line_item_type_raw and str(line_item_type_raw).strip():
        lit = str(line_item_type_raw).strip()
    else:
        if cost_val < 0:
            if "covered by" in blob and "savings plan" in blob:
                lit = "SavingsPlanCoveredUsage"
            else:
                lit = "Credit"
        elif "savings plan" in blob and any(w in blob for w in ("recurring", "hourly commitment", "commitment", "upfront")):
            lit = "SavingsPlanRecurringFee"
        elif "savings plans" in cat_str.lower() or "savings plan" in cat_str.lower():
            lit = "SavingsPlanRecurringFee"
        elif "reserved instance fee" in blob or "rifee" in blob:
            lit = "RIFee"
        elif "reserved instance applied" in blob or "reserved instances" in blob or "reserved instance" in blob or "discountedusage" in blob:
            lit = "DiscountedUsage"
        elif prod_str.strip().lower() == "tax" or ut_str.strip().lower() == "tax" or op_str.strip().lower() == "tax":
            lit = "Tax"
        else:
            lit = "Usage"

    if lit in ("SavingsPlanRecurringFee", "RIFee", "Fee", "Purchase"):
        charge_category = "Purchase"
    elif cost_val < 0 or lit in ("Credit", "Refund"):
        charge_category = "Credit"
    elif lit == "SavingsPlanCoveredUsage" and cost_val < 0:
        charge_category = "Credit"
    elif lit == "Tax" or "tax" in blob:
        charge_category = "Tax"
    else:
        charge_category = "Usage"

    is_workload = (charge_category == "Usage"
                   and lit in _WL_TYPES
                   and _is_workload_py(lit, prod_str, ut_str, op_str, cat_str))
    return (charge_category, lit, is_workload)


def _sql_classify_charge(cost_expr, product_expr, usage_type_expr, op_expr="''", category_expr="''", raw_lit_expr=None):
    """Build single-source-of-truth SQL CASE expressions for (line_item_type, is_workload).

    Generates the exact SQL counterpart of classify_charge() to guarantee no divergence
    between Python and SQL branches.
    """
    if raw_lit_expr and raw_lit_expr != "NULL":
        lit_case = f"COALESCE(NULLIF({raw_lit_expr}, ''), 'Usage')"
    else:
        lit_case = f"""(
            CASE
                WHEN {cost_expr} < 0 AND ({op_expr} ILIKE '%covered by%savings plan%' OR {usage_type_expr} ILIKE '%covered by%savings plan%' OR {product_expr} ILIKE '%covered by%savings plan%' OR {category_expr} ILIKE '%covered by%savings plan%')
                    THEN 'SavingsPlanCoveredUsage'
                WHEN {cost_expr} < 0
                    THEN 'Credit'
                WHEN ({category_expr} ILIKE '%savings plan%' OR {product_expr} ILIKE '%savings plan%' OR {usage_type_expr} ILIKE '%savings plan%' OR {op_expr} ILIKE '%savings plan%')
                     AND ({category_expr} ILIKE '%recurring%' OR {category_expr} ILIKE '%hourly commitment%' OR {category_expr} ILIKE '%upfront%'
                          OR {product_expr} ILIKE '%recurring%' OR {product_expr} ILIKE '%hourly commitment%' OR {product_expr} ILIKE '%upfront%'
                          OR {usage_type_expr} ILIKE '%recurring%' OR {usage_type_expr} ILIKE '%hourly commitment%' OR {usage_type_expr} ILIKE '%upfront%'
                          OR {op_expr} ILIKE '%recurring%' OR {op_expr} ILIKE '%hourly commitment%' OR {op_expr} ILIKE '%upfront%')
                    THEN 'SavingsPlanRecurringFee'
                WHEN {category_expr} ILIKE '%savings plans%' OR {category_expr} ILIKE '%savings plan%'
                     OR {product_expr} ILIKE '%savings plans%' OR {product_expr} ILIKE '%savings plan%'
                    THEN 'SavingsPlanRecurringFee'
                WHEN {usage_type_expr} ILIKE '%rifee%' OR {op_expr} ILIKE '%rifee%'
                     OR {usage_type_expr} ILIKE '%reserved instance fee%' OR {op_expr} ILIKE '%reserved instance fee%'
                    THEN 'RIFee'
                WHEN {usage_type_expr} ILIKE '%reserved instance applied%' OR {usage_type_expr} ILIKE '%reserved instances%' OR {usage_type_expr} ILIKE '%reserved instance%' OR {usage_type_expr} ILIKE '%discountedusage%'
                     OR {op_expr} ILIKE '%reserved instance applied%' OR {op_expr} ILIKE '%reserved instances%' OR {op_expr} ILIKE '%reserved instance%' OR {op_expr} ILIKE '%discountedusage%'
                     OR {product_expr} ILIKE '%reserved instance applied%' OR {product_expr} ILIKE '%reserved instances%' OR {product_expr} ILIKE '%reserved instance%' OR {product_expr} ILIKE '%discountedusage%'
                    THEN 'DiscountedUsage'
                WHEN {product_expr} = 'Tax' OR {usage_type_expr} = 'Tax' OR {op_expr} = 'Tax'
                    THEN 'Tax'
                ELSE 'Usage'
            END
        )"""

    non_wl = _nonworkload_sql(product_expr, usage_type_expr, op_expr, category_expr)
    is_wl_case = f"""(
        CASE
            WHEN {cost_expr} < 0 THEN FALSE
            WHEN {lit_case} NOT IN ('Usage', 'DiscountedUsage', 'SavingsPlanCoveredUsage') THEN FALSE
            WHEN {non_wl} THEN FALSE
            ELSE TRUE
        END
    )"""
    return lit_case, is_wl_case


def _nonworkload_sql(*col_exprs):
    """Build a SQL boolean that is TRUE when the row is NON-workload, from the
    pattern list — so the spreadsheet/flat-CSV SQL CASEs stay in sync with the JSON.
    Substring patterns use ILIKE; short word patterns use word-boundary regexp so
    e.g. 'tax' does not match 'syntax'."""
    ors = []
    for col in col_exprs:
        for p in _NONWL_PATTERNS:
            ors.append(f"{col} ILIKE '%{p}%'")
        for wp in _NONWL_WORD_PATTERNS:
            ors.append(f"regexp_matches({col}, '\\b{wp}\\b', 'i')")
    return "(" + " OR ".join(ors) + ")" if ors else "(FALSE)"


def _sql_num(col_expr: str) -> str:
    """SQL fragment that robustly parses a possibly-messy numeric cell to DOUBLE.

    Real exports carry accounting negatives "(150.00)", currency symbols, thousands
    separators, and junk cells (N/A, -, blank). A bare CAST(... AS DOUBLE) throws a
    ConversionException on any of these and aborts the WHOLE ingest with no
    failure.txt (Finding Q). This cleans the cell (accounting parens -> minus, strip
    everything but digits/./-) and uses TRY_CAST so one bad cell can never crash the
    run — it just resolves to 0.0.
    """
    return (
        "COALESCE(TRY_CAST(NULLIF(REGEXP_REPLACE("
        f"REPLACE(REPLACE(TRIM(CAST({col_expr} AS VARCHAR)), '(', '-'), ')', ''), "
        "'[^0-9.+-]', '', 'g'), '') AS DOUBLE), 0.0)"
    )


JOB_DIR = os.getcwd()
DB_PATH = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
# SKILL_DIR is set by the Go spawner (internal/jobs/spawner.go, skillEnv).
# Fall back to the path derived from this file, exactly as apply_rates.py and
# apply_static_mappings.py already do. Without the fallback an unset SKILL_DIR
# made DATA_DIR the relative path "data", resolved against JOB_DIR — so the
# instance-spec tables loaded as {} and EVERY instance came out with NULL
# vcpus/ram_gb. Silently, with the bill still reconciling: the failure looked
# like a pipeline that could not identify common instance types.
SKILL_DIR = os.environ.get("SKILL_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SKILL_DIR, "data")

REGION_MAP = {
    "us-east-1": "us-east4",
    "us-east-2": "us-east4",
    "us-west-1": "us-west2",
    "us-west-2": "us-west1",
    "ca-central-1": "northamerica-northeast1",
    "ca-west-1": "northamerica-northeast2",
    "sa-east-1": "southamerica-east1",
    "eu-west-1": "europe-west1",
    "eu-west-2": "europe-west2",
    "eu-west-3": "europe-west9",
    "eu-central-1": "europe-west3",
    "eu-central-2": "europe-west4",
    "eu-north-1": "europe-north1",
    "eu-south-1": "europe-west8",
    "eu-south-2": "europe-southwest1",
    "ap-east-1": "asia-east2",
    "ap-southeast-1": "asia-southeast1",
    "ap-southeast-2": "australia-southeast1",
    "ap-southeast-3": "asia-southeast2",
    "ap-southeast-4": "australia-southeast2",
    "ap-south-1": "asia-south1",
    "ap-south-2": "asia-south2",
    "ap-northeast-1": "asia-northeast1",
    "ap-northeast-2": "asia-northeast3",
    "ap-northeast-3": "asia-northeast2",
    "me-central-1": "me-central1",
    "me-south-1": "me-west1",
    "af-south-1": "africa-south1",
    "il-central-1": "me-central2",
    "mx-central-1": "northamerica-south1",
    "us-gov-east-1": "us-east4",
    "us-gov-west-1": "us-west1"
}

DESCRIPTIVE_REGION_MAP = {
    # Confirmed real bug: this table only covered ~12 of AWS's ~30+ real
    # regions. A row with aws_region="Asia Pacific (Hyderabad)" (ap-south-2)
    # matched neither REGION_MAP (code-keyed) nor this table, silently fell
    # back to "global", and downstream SKU resolution then mismatched onto an
    # unrelated region's SKU (Melbourne) that happened to price identically —
    # correct by coincidence, not by design, and would silently diverge the
    # moment GCP regionally differentiates that rate. Every display name
    # below is matched to the SAME GCP target already in REGION_MAP for that
    # AWS region code, so the two tables can never disagree.
    "us east (n. virginia)": "us-east4",
    "us east (ohio)": "us-east4",
    "us west (n. california)": "us-west2",
    "us west (oregon)": "us-west1",
    "canada (central)": "northamerica-northeast1",
    "canada west (calgary)": "northamerica-northeast2",
    "south america (sao paulo)": "southamerica-east1",
    "europe (ireland)": "europe-west1",
    "europe (london)": "europe-west2",
    "europe (paris)": "europe-west9",
    "europe (frankfurt)": "europe-west3",
    "europe (zurich)": "europe-west4",
    "europe (stockholm)": "europe-north1",
    "europe (milan)": "europe-west8",
    "europe (spain)": "europe-southwest1",
    "asia pacific (hong kong)": "asia-east2",
    "asia pacific (singapore)": "asia-southeast1",
    "asia pacific (sydney)": "australia-southeast1",
    "asia pacific (jakarta)": "asia-southeast2",
    "asia pacific (melbourne)": "australia-southeast2",
    "asia pacific (mumbai)": "asia-south1",
    "asia pacific (hyderabad)": "asia-south2",
    "asia pacific (tokyo)": "asia-northeast1",
    "asia pacific (seoul)": "asia-northeast3",
    "asia pacific (osaka)": "asia-northeast2",
    "middle east (uae)": "me-central1",
    "middle east (bahrain)": "me-west1",
    "africa (cape town)": "africa-south1",
    "israel (tel aviv)": "me-central2",
    "mexico (central)": "northamerica-south1",
    "aws govcloud (us-east)": "us-east4",
    "aws govcloud (us-west)": "us-west1",
    "global": "global"
}


def _drop_raw(conn):
    try:
        conn.execute("DROP VIEW IF EXISTS aws_raw")
    except Exception:
        pass
    try:
        conn.execute("DROP TABLE IF EXISTS aws_raw")
    except Exception:
        pass


def _detect_foreign_currency(cols, conn):
    """Detect bill currency and FX rate (H-6). Returns currency info dict."""
    cl = {c.lower(): c for c in cols}
    cur_cols = [cl[k] for k in (
        "line_item_currency_code", "lineitem/currencycode", "pricing_currency",
        "pricing/currency", "currency", "bill/billingcurrency", "bill_billing_currency"
    ) if k in cl]
    col_vals = []
    for col in cur_cols:
        try:
            vals = conn.execute(
                f'SELECT DISTINCT UPPER(TRIM(CAST("{col}" AS VARCHAR))) FROM aws_raw'
            ).fetchall()
            col_vals.extend(v[0] for v in vals if v[0] not in (None, ""))
        except Exception:
            continue

    sample_text = ""
    for col in ("description", "lineitem/lineitemdescription", "item_description", "service"):
        if col in cl:
            try:
                rows = conn.execute(f'SELECT "{cl[col]}" FROM aws_raw LIMIT 20').fetchall()
                sample_text += " " + " ".join(str(r[0] or "") for r in rows)
            except Exception:
                pass

    codes = {c for c in col_vals if c not in ("USD", "US DOLLAR")}
    if len(codes) > 1:
        # Mixed foreign currencies — can't convert sensibly; refuse.
        _drop_raw(conn)
        _fail_direct(
            f"This bill mixes multiple non-USD currencies ({', '.join(sorted(codes))}). "
            "Please upload a single-currency bill.")

    return detect_currency_and_rate(raw_text=sample_text, column_values=col_vals)


def _reject_non_aws(cols, conn):
    """Detect clearly non-AWS files and fail early with a useful message."""
    cols_lower = {c.lower() for c in cols}
    # Azure billing exports (usage-based / EA portal / cost-management CSV).
    # Broadened signatures beyond the EA ones.
    _azure_cols = ("subscriptionid", "subscription id", "subscriptionname", "meterid",
                   "metercategory", "metersubcategory", "meterregion", "unitofmeasure",
                   "billingaccountid", "billingaccountname", "chargestart", "effectiveprice",
                   "resourcegroup", "resourcegroupname", "costinbillingcurrency",
                   "billingcurrency", "partnumber", "consumedservice")
    # Exact column matches (EA/cost-management exports).
    _azure_hit = any(k in cols_lower for k in _azure_cols)
    # Substring matches — Azure usage CSVs use descriptive phrase headers AWS
    # never uses (confirmed real: "Part number and service name", "Net unit price
    # currency", "Amount used currency"). "part number" alone is decisive; the
    # price/amount-currency phrasing is an Azure tell when it co-occurs with usage.
    _cols_blob = " | ".join(cols_lower)
    if "part number" in _cols_blob:
        _azure_hit = True
    if ("net unit price" in _cols_blob or "amount used" in _cols_blob) and "metric" in _cols_blob:
        _azure_hit = True
    if _azure_hit:
        _drop_raw(conn)
        _fail_direct("This looks like an Azure billing export, not an AWS bill. "
                     "This tool projects AWS costs to GCP - please upload an AWS "
                     "Cost & Usage Report (CUR), Cost Explorer CSV, or estimated-bill PDF.")
    # Some Azure/other exports put a key-value preamble BEFORE the real header,
    # so the provider markers land in CELL VALUES, not column names (confirmed on
    # a real Azure usage export: columns were ['Cloud account', '<name>'] and the
    # first column's values were 'Subscription ID', 'Part number and service name',
    # ...). Scan the first column's leading values for strong, AWS-absent markers.
    try:
        first = cols[0]
        vals = conn.execute(
            f'SELECT LOWER(CAST("{first}" AS VARCHAR)) FROM aws_raw LIMIT 60'
        ).fetchall()
        blob = " ".join(v[0] for v in vals if v and v[0])
        azure_markers = sum(m in blob for m in
                            ("subscription id", "part number", "meter", "resource group",
                             "billing period", "cost center"))
        if "subscription id" in blob and azure_markers >= 2:
            _drop_raw(conn)
            _fail_direct("This looks like an Azure (or other non-AWS) billing export with a "
                         "key-value preamble, not an AWS bill. This tool projects AWS costs "
                         "to GCP - please upload an AWS Cost & Usage Report (CUR), Cost "
                         "Explorer CSV, or estimated-bill PDF.")
    except Exception:
        pass  # detection is best-effort; never let it crash ingestion
    # VMware vCenter RVTools export
    if any(k in cols_lower for k in ("vm", "powerstate", "vcpu", "provisionedmb",
                                      "inusemb", "datacenter")):
        col_sample = [c for c in cols if c.lower() in ("vm", "powerstate", "vcpu")]
        if len(col_sample) >= 2:
            _drop_raw(conn)
            _fail_direct("This looks like a VMware/vCenter RVTools export, not an AWS bill. "
                         "Please upload an AWS Cost & Usage Report.")
    # OCI billing
    if any(k in cols_lower for k in ("tenancyname", "subscriptionid", "sku/partnumber",
                                      "product/compartmentname")):
        _drop_raw(conn)
        _fail_direct("This looks like an Oracle Cloud (OCI) billing export, not an AWS bill.")
    # GCP billing
    if any(k in cols_lower for k in ("billing_account_id", "service.description",
                                      "sku.description", "usage.amount_in_pricing_units")):
        _drop_raw(conn)
        _fail_direct("This looks like a GCP billing export, not an AWS bill.")


def _fail_direct(msg):
    """Write failure.txt and exit without attempting LLM fallback."""
    try:
        with open(os.path.join(JOB_DIR, "failure.txt"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception as fe:
        sys.stderr.write(f"Failed to write failure.txt: {fe}\n")
    log.error(f"INGEST FAILURE: {msg}")
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(1)


def _fail(msg):
    """Write a clean structural-failure reason. Attempts LLM normalization first;
    if that produces normalized_bill.csv, re-execs ingest.py so the retry path
    picks it up. Falls through to failure.txt only if LLM is unavailable or fails."""
    normalized_path = os.path.join(JOB_DIR, "projection-audit", "normalized_bill.csv")
    if not os.path.exists(normalized_path):
        _try_llm_normalization(msg)
        if os.path.exists(normalized_path):
            log.info("LLM normalization succeeded — re-running ingest with normalized_bill.csv")
            sys.stdout.flush()
            sys.stderr.flush()
            os.execv(sys.executable, [sys.executable] + sys.argv)
            # execv replaces the process; line below never reached
    try:
        with open(os.path.join(JOB_DIR, "failure.txt"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception as fe:
        sys.stderr.write(f"Failed to write failure.txt: {fe}\n")
    log.error(f"INGEST FAILURE: {msg}")
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(1)


def check_faithfulness(cat_rows, cat_total, reference_total, rows_before_materiality=None):
    """Faithfulness invariants for the extraction layer (universal, all formats).

    Returns an error string if the catalog is not a faithful representation of the
    bill, else None. Pure/typed so it is unit-testable without running ingestion.

    Invariants:
      EMPTY_BILL        — zero priceable rows survived extraction.
      NON_FINITE_TOTAL  — a cost column parsed to NaN/inf.
      COST_RECONCILE    — catalog total drifted from the pre-materiality reference
                          (dedup preserves cost; materiality deletes then re-adds
                          the same sum), so no spend may be lost or invented.
    """
    import math
    if cat_rows == 0:
        # Distinguish "nothing parsed" from "everything parsed and was then
        # removed". The old message blamed the file layout in both cases, so a
        # free-tier-only account was told its bill was malformed.
        if rows_before_materiality:
            return ("Bill parsed successfully but every line item was $0, so nothing remains to "
                    "project. This usually means a free-tier-only account or a period with no "
                    "chargeable usage.")
        return ("Empty bill: no line items were extracted into the catalog. The input "
                "parsed but produced zero priceable rows (all rows were tax/blank, or "
                "the layout was not recognized). Check the input file.")
    if not math.isfinite(cat_total):
        return (f"Catalog cost total is not finite ({cat_total!r}) — a cost column "
                f"parsed to NaN/inf. Refusing to emit a corrupt catalog.")
    tol = max(0.01, abs(reference_total) * 1e-6)
    drift = abs(cat_total - reference_total)
    if drift > tol:
        return (f"Cost reconciliation failed: catalog total ${cat_total:,.2f} does not "
                f"match the extracted bill total ${reference_total:,.2f} "
                f"(drift ${drift:,.2f} > ${tol:,.2f}). The dedup/materiality pipeline "
                f"lost or added spend — this would corrupt every downstream total.")
    return None


def _try_llm_normalization(fail_reason):
    """Run prepare_ingest_fallback.py to dump a preview, then call `agy` with a
    one-shot normalization prompt. Writes normalized_bill.csv if successful.
    Silently no-ops if `agy` is not on PATH (dev mode without preflight)."""
    import subprocess, shutil
    agy_bin = shutil.which("agy")
    if not agy_bin:
        log.debug("agy not on PATH — skipping LLM normalization fallback")
        return

    skill_dir = os.environ.get("SKILL_DIR", "")
    fallback_script = os.path.join(skill_dir, "scripts", "prepare_ingest_fallback.py")
    manifest_path = os.path.join(JOB_DIR, "projection-audit", "ingest_fallback_manifest.md")
    normalized_path = os.path.join(JOB_DIR, "projection-audit", "normalized_bill.csv")

    # Step 1: dump a readable preview of the raw file
    try:
        subprocess.run(
            [sys.executable, fallback_script],
            cwd=JOB_DIR, timeout=60, check=True,
            env={**os.environ, "SKILL_DIR": skill_dir},
        )
    except Exception as e:
        log.warning(f"prepare_ingest_fallback failed: {e}")
        return

    if not os.path.exists(manifest_path):
        log.debug("No manifest produced — skipping LLM normalization")
        return

    with open(manifest_path) as f:
        manifest = f.read()

    prompt = f"""You are normalizing an AWS billing export that the deterministic parser could not recognize.

Failure reason: {fail_reason}

Raw file preview (first ~500 rows):
{manifest[:12000]}

Your task: write the file `{normalized_path}` as a CSV with EXACTLY these columns:
  Service, Region, Custom Usage Type, Description, Usage Quantity, Cost ($)

Rules:
- Map every charge to its AWS service name (e.g. "Amazon EC2", "Amazon S3")
- Region: use the AWS region code (e.g. "us-east-1") or display name (e.g. "US East (N. Virginia)") — leave blank if unknown
- Custom Usage Type: the usage type string if available, else blank
- Description: the line item description
- Usage Quantity: numeric quantity (no units), 0 if unknown
- Cost ($): numeric USD cost. Negative for credits/discounts.
- Skip tax lines, header rows, and blank rows
- Do NOT include markdown fences — write raw CSV only

Write the CSV now."""

    try:
        result = subprocess.run(
            [agy_bin, "--print-only", "--model", os.environ.get("AGY_MODEL", "gemini-3.8-flash")],
            input=prompt, text=True, capture_output=True,
            cwd=JOB_DIR, timeout=120,
        )
        output = result.stdout.strip()
        if output and "Service" in output.split("\n")[0] if output else False:
            with open(normalized_path, "w", encoding="utf-8") as f:
                f.write(output)
            log.info(f"LLM wrote normalized_bill.csv ({len(output.splitlines())} lines)")
        else:
            log.warning("LLM output did not look like a valid CSV — skipping")
            if result.stderr:
                log.debug(f"agy stderr: {result.stderr[:500]}")
    except Exception as e:
        log.warning(f"LLM normalization call failed: {e}")


def _detect_unit_price_outliers(conn, col_map, job_dir=None):
    """Detect line items priced inconsistently with other lines on the same meter.

    Runs against raw rows in aws_raw before dedup/aggregation collapses per-line prices.
    Identifies intra-meter price discrepancies (e.g. 1000x typo in rate) without altering
    any cost, usage, or catalog row. Reports via diagnostics.py (WARN) and data_gaps.json.
    """
    import statistics

    def c(names):
        for n in names:
            clean_n = n.lower().replace("/", "_").replace(" ", "_")
            if clean_n in col_map:
                return f'"{col_map[clean_n]}"'
        return "NULL"

    # Identify candidate columns
    meter_col = c([
        'lineitem/usagetype', 'usagetype',
        'meter',
        'custom_usage_type', 'usage_type',
        'usage_(with_units)',
    ])
    cost_col = c([
        'lineitem/unblendedcost', 'unblendedcost',
        'cost_($)', 'cost', 'amount_in_usd', 'cost_(usd)'
    ])
    usage_col = c([
        'lineitem/usageamount', 'usageamount',
        'consumed',
        'usage_quantity', 'usage'
    ])
    src_row_col = c(['_src_row'])
    res_col = c(['lineitem/resourceid', 'resourceid', 'resource'])
    acct_col = c(['lineitem/usageaccountid', 'usageaccountid', 'account_id', 'account_reference'])

    if meter_col == "NULL" or cost_col == "NULL" or usage_col == "NULL" or src_row_col == "NULL":
        return

    q = f"""
        SELECT
            {src_row_col} AS src_row,
            {meter_col} AS meter,
            {_sql_num(cost_col)} AS cost,
            {_sql_num(usage_col)} AS usage,
            {res_col} AS resource,
            {acct_col} AS account_id
        FROM aws_raw
        WHERE {meter_col} IS NOT NULL AND TRIM(CAST({meter_col} AS VARCHAR)) != ''
    """
    try:
        cur = conn.cursor()
        cur.execute(q)
    except Exception as e:
        log.debug(f"Outlier detection query failed: {e}")
        return

    by_meter = {}
    CHUNK_SIZE = 5000
    try:
        while True:
            chunk = cur.fetchmany(CHUNK_SIZE)
            if not chunk:
                break
            for r in chunk:
                src_row = r[0]
                m = str(r[1] or '').strip()
                c_val = r[2]
                u_val = r[3]
                res = r[4]
                acct = r[5]
                if not m or c_val is None or u_val is None:
                    continue
                try:
                    c_float = float(c_val)
                    u_float = float(u_val)
                except (ValueError, TypeError):
                    continue

                # Guard 1: positive-cost only
                if u_float > 0 and c_float > 0:
                    meter_list = by_meter.setdefault(m, [])
                    # Bound memory per meter (keep up to 1000 sample rows)
                    if len(meter_list) < 1000:
                        price = c_float / u_float
                        meter_list.append((src_row, c_float, u_float, price, res, acct))
    except Exception as e:
        log.debug(f"Outlier row streaming encountered error: {e}")
    finally:
        try:
            cur.close()
        except Exception:
            pass

    flagged = []
    for m, rows in by_meter.items():
        # Guard 2: need >= 3 rows for a reference
        if len(rows) < 3:
            continue
        prices = [x[3] for x in rows]
        reference = statistics.median(prices)
        # Guard 3: reference must be real
        if reference <= 1e-9:
            continue

        for src_row, c_float, u_float, price, res, acct in rows:
            factor = max(price / reference, reference / price)
            # Guard 4 & 5: factor >= 100 and cost > $50
            if factor >= 100 and c_float > 50:
                flagged.append((src_row, m, res, acct, u_float, c_float, price, reference, factor, len(rows) - 1))

    if not flagged:
        return

    # Sort largest cost first
    flagged.sort(key=lambda x: -x[5])
    top_r, top_m, top_res, top_acct, top_q, top_c, top_p, top_ref, top_f, top_peers = flagged[0]
    file_line = int(top_r) + 1 if top_r is not None else 0

    top = {
        "file_line": file_line,
        "meter": top_m,
        "price": top_p,
        "reference": top_ref,
        "peers": top_peers,
        "factor": top_f,
        "cost": top_c,
    }

    n = len(flagged)
    usd = sum(x[5] for x in flagged)

    diag_lines = []
    for r, m, res, acct, q, c, p, ref, f, peers in flagged[:20]:
        line_item = {
            "src_row": r,
            "file_line": int(r) + 1 if r is not None else 0,
            "meter": m,
            "usage": q,
            "cost": round(c, 2),
            "unit_price": p,
            "reference_price": ref,
            "factor": round(f, 1),
        }
        if res is not None and str(res).strip() and str(res).strip().lower() != 'none':
            line_item["resource"] = str(res)
        if acct is not None and str(acct).strip() and str(acct).strip().lower() != 'none':
            line_item["account_id"] = str(acct)
        diag_lines.append(line_item)

    diag(
        WARN, "unit_price_outlier", "ingest.py",
        f"{n} line item(s) priced inconsistently with other lines on the same "
        f"meter. Largest: file line {top['file_line']}, meter "
        f"{top['meter']}, unit price {top['price']:.6g} vs {top['reference']:.6g} "
        f"on {top['peers']} other line(s) of that meter ({top['factor']:.0f}x), "
        f"cost ${top['cost']:,.2f}. Figures are reported as billed and have not "
        f"been adjusted.",
        rows=n, usd=usd, job_dir=job_dir,
        detail={"lines": diag_lines}
    )

    # Sync into data_gaps.json (read-modify-write)
    target_job_dir = job_dir or JOB_DIR or os.getcwd()
    candidate_paths = [
        os.path.join(target_job_dir, "data_gaps.json"),
        os.path.join(target_job_dir, "projection-audit", "data_gaps.json"),
    ]
    # If one of the files already exists, use that one; otherwise write to data_gaps.json in target_job_dir
    target_paths = [p for p in candidate_paths if os.path.exists(p)]
    if not target_paths:
        target_paths = [candidate_paths[0]]

    for gap_json_path in target_paths:
        try:
            os.makedirs(os.path.dirname(gap_json_path), exist_ok=True)
            existing_data = {}
            if os.path.exists(gap_json_path):
                with open(gap_json_path, "r", encoding="utf-8") as _gf:
                    try:
                        existing_data = json.load(_gf)
                    except Exception:
                        existing_data = {}

            if not isinstance(existing_data, dict):
                existing_data = {"count": 0, "total_cost": 0.0, "items": []}

            items = existing_data.get("items", [])
            if not isinstance(items, list):
                items = []

            existing_locators = {
                (it.get("file_line"), it.get("meter")) for it in items if isinstance(it, dict)
            }

            for line_info in diag_lines:
                loc = (line_info.get("file_line"), line_info.get("meter"))
                if loc not in existing_locators:
                    items.append({
                        "file_line": line_info.get("file_line"),
                        "meter": line_info.get("meter"),
                        "unit_price": line_info.get("unit_price"),
                        "reference_price": line_info.get("reference_price"),
                        "factor": line_info.get("factor"),
                        "cost": line_info.get("cost"),
                        "reason": f"priced {line_info.get('factor'):.0f}x the other lines of that meter; reported as billed",
                    })
                    existing_locators.add(loc)

            existing_data["items"] = items
            # Maintain active count / total_cost if items are the primary list
            existing_data["count"] = len(items) + len(existing_data.get("rows", []))
            item_cost = sum(float(it.get("cost") or it.get("aws_cost") or 0.0) for it in items if isinstance(it, dict))
            row_cost = sum(float(r.get("cost") or 0.0) for r in existing_data.get("rows", []) if isinstance(r, dict) and r.get("resolution") != "ignore")
            existing_data["total_cost"] = round(item_cost + row_cost, 2)

            with open(gap_json_path, "w", encoding="utf-8") as _gf:
                json.dump(existing_data, _gf, indent=2)
        except Exception as _gwe:
            log.debug(f"Could not update data_gaps.json with unit_price_outlier: {_gwe}")


def _load_excel(conn, path):
    """Excel → aws_raw via DuckDB excel extension, spatial fallback, then openpyxl."""
    for setup, query in (
        ("INSTALL excel; LOAD excel;", f"SELECT row_number() OVER () AS _src_row, * FROM read_xlsx('{path}', all_varchar=true)"),
        ("INSTALL spatial; LOAD spatial;", f"SELECT row_number() OVER () AS _src_row, * FROM st_read('{path}')"),
    ):
        try:
            conn.execute(setup)
            conn.execute(f"CREATE TABLE aws_raw AS {query}")
            # The spatial (GDAL) reader will "succeed" on a plain .xlsx but stamp a
            # junk OGC_FID/geom column instead of the bill's real columns — if we
            # accepted that, the working openpyxl reader below would never run and
            # the bill would die as "unrecognized format". Reject such a result so
            # the loop falls through to openpyxl.
            _cols = {c[1].lower() for c in conn.execute("PRAGMA table_info('aws_raw')").fetchall()}
            if "ogc_fid" in _cols or "geom" in _cols or "geometry" in _cols or len(_cols) < 2:
                _drop_raw(conn)
                continue
            return
        except Exception:
            _drop_raw(conn)

    # openpyxl fallback — handles files DuckDB's readers reject. Load rows DIRECTLY
    # into DuckDB (parameterized INSERT), NOT via a temp-CSV round-trip: writing a
    # 29k-row sheet back out as CSV and re-parsing it is fragile (cells with commas/
    # quotes/newlines make DuckDB's CSV parser reach "an invalid state"), which
    # previously sank real .xlsx bills. A direct insert has no CSV parsing to break.
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb.active
        row_iter = ws.iter_rows(values_only=True)
        header = next(row_iter, None)
        if not header:
            raise ValueError("empty worksheet")
        # Clean + de-duplicate column names (DuckDB rejects blank/duplicate columns).
        names, seen = ["_src_row"], {}
        for i, h in enumerate(header):
            nm = (str(h).strip() if h is not None else "") or f"col{i}"
            if nm in seen:
                seen[nm] += 1
                nm = f"{nm}_{seen[nm]}"
            else:
                seen[nm] = 0
            names.append(nm)
        width = len(names) - 1
        cols_ddl = '"_src_row" BIGINT, ' + ", ".join(f'"{n}" VARCHAR' for n in names[1:])
        _drop_raw(conn)
        conn.execute(f"CREATE TABLE aws_raw ({cols_ddl})")
        placeholders = ",".join("?" for _ in names)
        batch, n = [], 0
        for row_idx, row in enumerate(row_iter, start=1):
            vals = list(row) + [None] * width  # pad ragged rows
            batch.append([row_idx] + ["" if v is None else str(v) for v in vals[:width]])
            if len(batch) >= 5000:
                conn.executemany(f"INSERT INTO aws_raw VALUES ({placeholders})", batch)
                n += len(batch); batch = []
        if batch:
            conn.executemany(f"INSERT INTO aws_raw VALUES ({placeholders})", batch)
            n += len(batch)
        wb.close()
        if n == 0:
            raise ValueError("worksheet has a header but no data rows")
        log.info(f"Excel: loaded via openpyxl direct insert ({n} rows, {width} cols)")
        return
    except Exception as e:
        _drop_raw(conn)
        log.warning(f"Excel: openpyxl fallback failed: {e}")

    _fail("Could not read the Excel file. Re-export the bill as CSV or Parquet and upload that.")


# AWS region display-name fragments — used to tell a REGION sub-header apart from
# a SERVICE sub-header in the flat text of an AWS estimated-bill PDF.
_PDF_REGION_RE = re.compile(
    r'^(asia pacific|us east|us west|eu |europe|canada|south america|middle east|'
    r'africa|global|israel|sa[- ]east|ap[- ]|us[- ])', re.I)
# A line-item row: "<description> <qty> <unit> USD <amount>". The unit keeps it
# distinct from service/region subtotal lines ("<name> USD <amount>").
_PDF_ITEM_RE = re.compile(
    r'^(.*?)\s+([\d,]+(?:\.\d+)?)\s+([A-Za-z][A-Za-z0-9\-/]*)\s+USD\s+([\d,]+(?:\.\d+)?)\s*$')
# A subtotal/header row: "<name> USD <amount>" with no usage qty/unit.
_PDF_HDR_RE = re.compile(r'^(.+?)\s+USD\s+[\d,]+(?:\.\d+)?\s*$')
# Subtotal lines like "Amazon Internet Monitor (769 Internet Monitor)" start with
# "Amazon"/"AWS" just like real section headers, but carry an embedded parenthetical
# quantity — reject those so they don't get mistaken for headers.
_PDF_QUANTITY_PAREN_RE = re.compile(r'\(\d[\d,]*\s+[A-Za-z]')

# The PDF prints its own post-discount subtotal(s) as "Total pre-tax USD <amt>"
# (one per billing entity). Summing line items gives a GROSS figure (net of the
# PRC rate but gross of the Enterprise Discount Program + credits, which appear
# as parenthetical lines the item pattern can't sum) — ~27% high. We read the
# stated pre-tax total and book the gap as a single reconciliation row rather
# than parsing the EDP lines (which recap at several granularities → double-count).
_PDF_PRETAX_RE = re.compile(r'total\s+pre-?tax\s+USD\s+([\d,]+\.\d{2})', re.I)

# AWS PDFs group EBS storage/IOPS lines under the EC2 billing section, so the
# cur_service header ends up wrong (e.g. "T3ACPUCredits"). Override it when the
# description clearly describes block-storage pricing.
_PDF_STORAGE_DESC_RE = re.compile(
    r'(gp[23]|io[12]|sc1|st1|magnetic)\s*(provisioned|storage|-storage)|'
    r'GB-month\s*(of\s*)?.*storage|snapshot\s*data\s*stored|'
    r'IOPS-month|MiBps-month|throughput.*month',
    re.IGNORECASE)

# S3 object-storage class terms. These lines ("… Intelligent-Tiering Archive …
# GB-month of storage") ALSO match the EBS storage regex below and were wrongly
# relabeled "Amazon Elastic Block Store" — there is no EBS Intelligent-Tiering /
# Glacier. They are S3. Some PDFs spell "One Zone-IA" out in full as
# "One Zone-Infrequent Access" — match both forms.
_PDF_S3_CLASS_RE = re.compile(
    r'intelligent[- ]?tiering|glacier|standard[- ]?ia|'
    r'one[- ]?zone[- ]?(?:ia|infrequent\s*access)|'
    r'deep archive|reduced\s*redundancy', re.IGNORECASE)

# RDS/Aurora storage, IOPS, and backup lines ALSO match the generic EBS storage
# regex below ("GB-month of provisioned GP3 storage running PostgreSQL",
# "Multi-AZ deployments") — these are Cloud SQL storage, not Persistent Disk.
# "Multi-AZ" is RDS-specific terminology (EC2/EBS has no such concept); engine
# names appear directly in "running <engine>" storage/backup lines. "backup
# storage" (as opposed to EBS's own "snapshot storage"/"snapshot data stored"
# wording) is itself an RDS-only billing term even when the engine name is
# missing from the line.
_PDF_RDS_STORAGE_RE = re.compile(
    r'multi-az|running\s*(postgresql|mysql|mariadb|oracle|sql\s*server|aurora)|'
    r'backup\s*storage',
    re.IGNORECASE)

# EFS backup/storage lines are explicitly labeled "for EFS" in the description.
_PDF_EFS_RE = re.compile(r'for\s*efs\b', re.IGNORECASE)

# Any AWS-Marketplace-billed third-party SaaS (Kiro, or any future vendor
# billed the same way) never passes the header-validation guard below — its
# section header doesn't start with "Amazon "/"AWS " and isn't in the AWS
# alias table, both correctly, since it isn't an AWS-owned service — so
# cur_service stays stuck on whichever real AWS section preceded it in the
# PDF, and every one of that vendor's line items silently inherits that
# unrelated service name (confirmed real: Kiro credit/subscription rows
# labeled "Elastic Compute Cloud T3ACPUCredits"/"KMS ...-Requests" on
# customer reports — cost correct, service label wrong). Unlike the
# EBS/S3/RDS/EFS cases above, the fix can't be "trust the header" (there is
# no valid AWS header to trust for a non-AWS product) — the item's own
# description is the only reliable signal, and AWS Marketplace's per-unit
# line format always names the billed product directly: "$<rate> per <unit>
# for <ProductName> in <region> (<qty> <unit>)". Extract <ProductName>
# generically from that shape instead of hardcoding one vendor's name, so
# the next third-party product billed the same way (any AWS Marketplace
# SaaS) is caught automatically rather than needing its own one-off regex.
#
# The extracted token must look like a real product identifier, not an
# ordinary English phrase from an AWS-native description — AWS's own
# descriptions never use "for <ProductName> in <region>" with a bare
# CamelCase/hyphenated single token there (they say things like "for
# instance usage" or "for EFS"). Requiring internal capitalisation or a
# hyphen (KiroEnterprise-Credits, Some-Vendor-Pro, DataDogAPM, ...) is what
# tells a real marketplace SKU name apart from ordinary description prose,
# without hand-listing vendors.
_PDF_MARKETPLACE_ITEM_RE = re.compile(
    r'\bfor\s+([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\s+in\s+[A-Za-z]')
_PDF_LOOKS_LIKE_PRODUCT_NAME_RE = re.compile(r'-|[a-z][A-Z]')
# AWS's own per-resource-scanned/per-rule billing (Inspector: "for
# EC2-Scanning in ...", "for ECR-ImageScanning in ..."; WAF: "for
# AMR-BotControl-Targeted-Request in ...") is genuine AWS-native phrasing
# that happens to structurally match "for <hyphenated-token> in <region>"
# just as well as a real marketplace vendor name does — hyphen-or-CamelCase
# alone can't tell them apart. Confirmed real, twice, with two DIFFERENT
# AWS abbreviations (EC2, then AMR): each time an Inspector/WAF row matched
# this regex, its already-correct cur_service ("Amazon Inspector"/"AWS
# WAF") got overwritten with the bogus product name, which then failed
# every downstream service-specific detection and fell through to
# marketplace_thirdparty passthrough — losing the real mapping (SCC
# Premium, Cloud Armor Bot Control) entirely.
#
# A hand-listed prefix exclusion (EC2-, ECR-, ...) is exactly the kind of
# vendor list this whole function was designed to avoid needing — it just
# missed AMR- the first time, and will keep missing the next AWS
# abbreviation (WAF, ACM, NAT, ...) the same way. The real structural tell:
# AWS's own internal resource-type abbreviations are short (2-5 letters),
# ALL-CAPS, acronym-shaped tokens (EC2, ECR, AMR, WAF, ACM) — a genuine
# third-party vendor/product name is never spelled that way (KiroEnterprise,
# DataDogAPM, Some-Vendor-Pro all mix case within the token). Excluding an
# ALL-CAPS shape generically covers every AWS abbreviation without
# hand-listing them one at a time.
#
# Checked against EVERY hyphen-separated segment, not just the first one —
# confirmed real: "Global-AMR-AntiDDoS" (AWS WAF's flat monthly Anti-DDoS
# fee) has the abbreviation ("AMR") in the MIDDLE segment, with an ordinary
# mixed-case word ("Global") first. A first-segment-only check let this one
# through the exact same false-positive path as EC2-Scanning and
# AMR-BotControl-Targeted-Request before it — same root cause, just shifted
# one position over, which is exactly why checking only the first segment
# was never going to generalize.
_PDF_AWS_ABBREV_SEGMENT_RE = re.compile(r'^[A-Z0-9]{2,5}$')

def _pdf_marketplace_product(desc):
    """Return the third-party product name embedded in a Marketplace-style
    line-item description, or None if this doesn't look like one."""
    m = _PDF_MARKETPLACE_ITEM_RE.search(desc)
    if not m:
        return None
    name = m.group(1)
    if not _PDF_LOOKS_LIKE_PRODUCT_NAME_RE.search(name):
        return None
    if any(_PDF_AWS_ABBREV_SEGMENT_RE.match(seg) for seg in name.split("-")):
        return None
    return name

def _pdf_canonical_service(cur_service, desc):
    """Return the correct AWS service name for a PDF line item.
    EBS storage/IOPS descriptions bleed into the EC2 section header — remap them,
    but S3/RDS/EFS storage-class lines must NOT be caught by that (they price
    against a different GCP target than Persistent Disk)."""
    marketplace_product = _pdf_marketplace_product(desc)
    if marketplace_product:
        return marketplace_product
    if _PDF_S3_CLASS_RE.search(desc):
        return "Amazon Simple Storage Service"
    # EFS check must precede the RDS "backup storage" catch-all: "warm backup
    # storage for EFS" matches both, and the explicit "for EFS" is the more
    # specific signal.
    if _PDF_EFS_RE.search(desc):
        return "Amazon Elastic File System"
    if _PDF_RDS_STORAGE_RE.search(desc):
        return "Amazon Relational Database Service"
    if _PDF_STORAGE_DESC_RE.search(desc):
        return "Amazon Elastic Block Store"
    return cur_service


def _load_pdf(conn, path):
    """AWS estimated-bill PDF → aws_raw in the simplified-bill schema
    (Service, Region, Custom Usage Type, Description, Usage Quantity, Cost).

    Parses text lines (pdfplumber table detection fails on these border-less
    PDFs), tracking the current Service / Region sub-headers and attaching them
    to each line item. NOTE: amounts are GROSS (pre Savings-Plan/RI discount) and
    a PDF reconciles less precisely than a CSV/Parquet CUR — good for a
    directional projection, but CUR is preferred for the exact figure."""
    try:
        import pdfplumber
    except Exception:
        _fail("This is a PDF but the PDF text-extraction library isn't installed. "
              "Export the AWS Cost & Usage Report as CSV or Parquet and upload that instead.")
        return
    import csv as _csv

    items = []            # (service, region, custom_usage_type, description, qty, cost, billing_month)
    cur_service, cur_region, cur_month = "", "", ""
    stated_pretax = 0.0   # sum of the PDF's own "Total pre-tax USD X" lines
    _PDF_DATE_RE = re.compile(r'\b(20\d\d)[-/](0[1-9]|1[0-2])\b')
    _PDF_MONTH_YEAR_RE = re.compile(
        r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[,\s]+(20\d\d)\b',
        re.IGNORECASE,
    )
    _PDF_MONTH_MAP = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
        'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'
    }
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").split("\n"):
                    ln = raw.strip()
                    if not ln:
                        continue
                    dm = _PDF_DATE_RE.search(ln)
                    if dm:
                        cur_month = f"{dm.group(1)}-{dm.group(2)}"
                    else:
                        mym = _PDF_MONTH_YEAR_RE.search(ln)
                        if mym:
                            mn = _PDF_MONTH_MAP.get(mym.group(1).lower()[:3], "01")
                            cur_month = f"{mym.group(2)}-{mn}"
                    # The PDF states its own post-discount pre-tax total(s). We
                    # trust that instead of parsing the parenthetical EDP/credit
                    # lines (which recap at several granularities and double-count).
                    tot = _PDF_PRETAX_RE.search(ln)
                    if tot:
                        stated_pretax += float(tot.group(1).replace(",", ""))
                        continue
                    m = _PDF_ITEM_RE.match(ln)
                    if m:
                        desc, qty, unit, amt = m.groups()
                        # Skip Savings-Plan/RI "covered by" lines (parenthesized
                        # amount) — they double-count usage already charged.
                        if "covered by" in desc.lower() or desc.rstrip().endswith("("):
                            continue
                        svc = _pdf_canonical_service(cur_service, desc.strip())
                        items.append((svc, cur_region, "",
                                      f"{desc.strip()} ({qty} {unit})",
                                      qty.replace(",", ""), amt.replace(",", ""), cur_month))
                        continue
                    h = _PDF_HDR_RE.match(ln)
                    if h:
                        name = h.group(1).strip()
                        if _PDF_REGION_RE.match(name):
                            cur_region = name
                        elif len(name) > 3 and "total" not in name.lower():
                            # Only trust this as a genuine service-section header if
                            # it actually looks like an AWS product name (starts with
                            # "Amazon "/"AWS ", or resolves via the same alias table
                            # every other mapper trusts) — otherwise a stray line that
                            # happens to structurally match "<text> USD <amount>"
                            # (a wrapped header, footnote, or mis-extracted fragment)
                            # would silently become cur_service and get carried onto
                            # every following item line until a real header is next
                            # matched, splicing one section's name onto another's
                            # description (confirmed real: caused a service-name/
                            # description mismatch on customer reports). Leaving
                            # cur_service unchanged when a candidate fails this check
                            # is strictly safer than accepting a bad one.
                            if (re.match(r'^(amazon|aws)\s', name, re.IGNORECASE) or _canonical_service(name)) \
                                    and not _PDF_QUANTITY_PAREN_RE.search(name):
                                cur_service = name
                            else:
                                log.warning(
                                    f"PDF header candidate {name!r} doesn't look like a real "
                                    f"AWS service name — ignoring it, keeping cur_service={cur_service!r}"
                                )
    except Exception as e:
        _fail(f"Could not parse the PDF ({e}). Export the CUR as CSV/Parquet instead.")
        return

    if len(items) < 5:
        _fail("Couldn't extract line items from this PDF (it may be summary-only). "
              "Export the AWS Cost & Usage Report as CSV or Parquet for an accurate projection.")
        return

    # The PDF text layout prints most charges as TWO adjacent lines: a bare
    # summary ("Amazon Route (53 DNS-Queries)") followed by the actual rate
    # breakdown ("$0.40 per 1,000,000 queries... (638,201,715 Queries)") —
    # both carrying the SAME dollar amount. Both match _PDF_ITEM_RE and were
    # being kept as two separate line items, double-counting the charge.
    # The rate line always contains " per " (the summary line never does) and
    # carries the real usage detail classify_mechanics.py/apply_static_mappings.py
    # key off of, so drop the summary line and keep the rate line.
    deduped = []
    i = 0
    while i < len(items):
        cur = items[i]
        if i + 1 < len(items):
            nxt = items[i + 1]
            same_charge = (cur[0] == nxt[0] and cur[1] == nxt[1]
                           and cur[5] == nxt[5] and float(cur[5] or 0) > 0)
            cur_is_summary = " per " not in cur[3].lower()
            nxt_is_detail = " per " in nxt[3].lower()
            if same_charge and cur_is_summary and nxt_is_detail:
                i += 1  # skip the summary line, keep nxt (the detail line) next iteration
                continue
        deduped.append(cur)
        i += 1
    if len(deduped) < len(items):
        log.info(f"PDF: deduped {len(items) - len(deduped)} summary/detail line pair(s) "
              f"(same charge printed twice by the PDF layout)")
    items = deduped

    # Reconcile the gross line-item sum down to the bill's own stated pre-tax
    # total via a single Enterprise Discount Program row (classified is_workload
    # =FALSE, ignore on GCP — GCP doesn't inherit AWS's negotiated discount).
    # Only when the gap is material (>2%), so clean bills are untouched.
    gross = sum(float(it[5]) for it in items)
    if stated_pretax > 0 and gross - stated_pretax > 0.02 * gross:
        adj = round(stated_pretax - gross, 2)
        items.append(("Enterprise Discount Program", "", "",
                      f"Enterprise Discount Program & credits "
                      f"(reconcile gross USD {gross:,.2f} to stated pre-tax USD {stated_pretax:,.2f})",
                      "0", f"{adj}", cur_month))
        log.info(f"PDF: reconciled gross ${gross:,.2f} -> stated pre-tax ${stated_pretax:,.2f} "
              f"(discount adjustment ${adj:,.2f})")

    tmp_csv = os.path.join(JOB_DIR, "input_from_pdf.csv")
    with open(tmp_csv, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["Service", "Region", "Custom Usage Type", "Description", "Usage Quantity", "Cost ($)", "Billing Month"])
        w.writerows(items)
    log.info(f"PDF: extracted {len(items)} line items")
    conn.execute(f"CREATE TABLE aws_raw AS SELECT row_number() OVER () AS _src_row, * FROM read_csv_auto('{tmp_csv}', ALL_VARCHAR=TRUE, header=TRUE)")


def _read_into_aws_raw(conn, path):
    """Load one data file into aws_raw, dispatching by extension. DuckDB's
    read_csv_auto transparently handles .gz / .zst and delimiter detection.
    Creating aws_raw as a VIEW streams large CSVs on-demand and eliminates the
    multi-gigabyte memory footprint that causes OOM kills on 512MB containers."""
    p = path.lower()
    sql_path = path.replace("\\", "/")
    _drop_raw(conn)
    if p.endswith(".parquet"):
        conn.execute(f"CREATE VIEW aws_raw AS SELECT row_number() OVER () AS _src_row, * FROM read_parquet('{sql_path}')")
    elif p.endswith((".json", ".jsonl", ".ndjson")):
        conn.execute(f"CREATE VIEW aws_raw AS SELECT row_number() OVER () AS _src_row, * FROM read_json_auto('{sql_path}')")
    elif p.endswith((".xlsx", ".xlsm", ".xls")):
        _load_excel(conn, path)
    elif p.endswith(".pdf"):
        _load_pdf(conn, path)
    else:  # .csv .tsv .txt and .gz/.zst variants of them
        # [TEMPORARY: Render 512MB deployment constraint]
        # Use streaming read_csv_auto with compression fallback.
        try:
            conn.execute(f"CREATE VIEW aws_raw AS SELECT row_number() OVER () AS _src_row, * FROM read_csv_auto('{sql_path}', ALL_VARCHAR=TRUE)")
        except Exception as e:
            if p.endswith(".gz"):
                conn.execute(f"CREATE VIEW aws_raw AS SELECT row_number() OVER () AS _src_row, * FROM read_csv('{sql_path}', compression='gzip', ALL_VARCHAR=TRUE, header=TRUE)")
            else:
                raise


def load_raw(conn, input_file):
    """Format-aware entry point. Handles CSV/TSV (+gzip/zstd), Parquet, JSON,
    Excel, PDF, and ZIP archives containing any of those."""
    if input_file.lower().endswith(".zip"):
        import zipfile, tempfile
        dest = tempfile.mkdtemp(prefix="cur_zip_")
        try:
            with zipfile.ZipFile(input_file) as z:
                z.extractall(dest)
        except Exception as e:
            _fail(f"Could not open the ZIP archive ({e}).")
            return
        DATA_EXT = (".parquet", ".csv", ".tsv", ".txt", ".json", ".jsonl",
                    ".ndjson", ".gz", ".zst", ".xlsx", ".xls")
        cands = []
        for root, _dirs, files in os.walk(dest):
            for fn in files:
                if fn.startswith(".") or fn.startswith("__MACOSX"):
                    continue
                if fn.lower().endswith(DATA_EXT):
                    fp = os.path.join(root, fn)
                    cands.append((os.path.getsize(fp), fp))
        if not cands:
            _fail("The ZIP archive contains no recognizable data file (CSV / Parquet / JSON / Excel).")
            return
        # Largest data file is the bill; manifests/metadata are small.
        cands.sort(reverse=True)
        _read_into_aws_raw(conn, cands[0][1])
    else:
        _read_into_aws_raw(conn, input_file)


def main():
    if not os.path.exists(os.path.dirname(DB_PATH)):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = duckdb.connect(DB_PATH)
    # [TEMPORARY: Render 512MB Free Tier deployment constraint]
    # Bound DuckDB threads and memory aggressively so the container does not get killed by Linux OOM killer (exit code 137).
    # preserve_insertion_order=false significantly cuts memory overhead during bulk table creations and aggregations.
    try:
        conn.execute("PRAGMA threads=1")
        conn.execute("PRAGMA preserve_insertion_order=false")
        conn.execute("PRAGMA max_memory='250MB'")
        temp_dir = os.path.join(JOB_DIR, "projection-audit", "duckdb_temp")
        os.makedirs(temp_dir, exist_ok=True)
        conn.execute(f"PRAGMA temp_directory='{temp_dir.replace('\\', '/')}'")
    except Exception as pragma_err:
        log.warning(f"DuckDB memory pragma configuration warning: {pragma_err}")
    
    # 1. Inspect input
    # If an LLM-normalized fallback CSV exists (written by the Phase 1 retry
    # path after the deterministic parsers failed to recognize the raw file),
    # prefer it — it already matches the flat-CSV schema this file understands
    # natively, so no separate loader is needed.
    normalized_fallback = os.path.join(JOB_DIR, "projection-audit", "normalized_bill.csv")
    if os.path.exists(normalized_fallback):
        inputs = [normalized_fallback]
    else:
        inputs = glob.glob(os.path.join(JOB_DIR, "input.*"))
    if not inputs:
        msg = "No input file found."
        with open(os.path.join(JOB_DIR, "failure.txt"), "w", encoding="utf-8") as f:
            f.write(msg)
        log.error(f"INGEST FAILURE: {msg}")
        sys.exit(1)
    input_file = inputs[0]

    # 2. Load aws_raw — format-aware loader (CSV/TSV, gzip/zstd, Parquet, JSON,
    #    Excel, PDF, and ZIP archives of any of those).
    _drop_raw(conn)
    load_raw(conn, input_file)
        
    cols = [c[1] for c in conn.execute("PRAGMA table_info(aws_raw)").fetchall()]

    # Detect non-AWS files early with a clear message
    _reject_non_aws(cols, conn)
    # Detect bill currency and FX rate (H-6); saved to DB & currency.json
    _currency_info = _detect_foreign_currency(cols, conn)
    save_currency_info(JOB_DIR, _currency_info, conn)

    # Parquet data-lake CUR uses underscores (line_item_product_code) while the
    # CSV CUR uses slashes (lineItem/ProductCode). Detect both.
    is_raw_cur = (
        "lineItem/LineItemType" in cols or "LineItemType" in cols
        or "line_item_line_item_type" in cols
    )

    # Normalize col names to make queries easier
    col_map = {}
    for c_name in cols:
        col_map[c_name.lower().replace("/", "_").replace(" ", "_")] = c_name

    # For parquet underscore-style CUR, add slash-normalized aliases so the
    # existing c() calls work unchanged (line_item_product_code → lineitem_productcode).
    if "line_item_line_item_type" in cols:
        _UNDERSCORE_TO_SLASH = {
            "line_item_line_item_type":   "lineitem_lineitemtype",
            "line_item_product_code":     "lineitem_productcode",
            "line_item_usage_type":       "lineitem_usagetype",
            "line_item_operation":        "lineitem_operation",
            "line_item_usage_amount":     "lineitem_usageamount",
            "line_item_unblended_cost":   "lineitem_unblendedcost",
            "line_item_blended_cost":     "lineitem_blendedcost",
            "line_item_net_unblended_cost": "lineitem_netunblendedcost",
            "pricing_public_on_demand_cost": "pricing_publicondemandcost",
            "product_region_code":        "product_region",
            "pricing_term":               "pricing_term",
            "pricing_unit":               "pricing_unit",
            "product_license_model":      "product_licensemodel",
            "product_operating_system":   "product_operatingsystem",
            "product_database_engine":    "product_databaseengine",
            "product_deployment_option":  "product_deploymentoption",
            "product_volume_type":        "product_volumetype",
            "line_item_usage_account_id": "lineitem_usageaccountid",
        }
        for underscore_key, slash_alias in _UNDERSCORE_TO_SLASH.items():
            normalized_key = underscore_key.lower().replace("/", "_").replace(" ", "_")
            if normalized_key in col_map and slash_alias not in col_map:
                col_map[slash_alias] = col_map[normalized_key]
        # Region: parquet uses product_region_code; CSV uses product/region
        if "product_region_code" in cols and "lineitem_region" not in col_map:
            col_map["lineitem_region"] = "product_region_code"
        log.info("Detected parquet data-lake CUR format (underscore columns)")

    # Helper to find exact col name
    def c(names):
        for n in names:
            clean_n = n.lower().replace("/", "_").replace(" ", "_")
            if clean_n in col_map:
                return f'"{col_map[clean_n]}"'
        return "NULL"

    # Detect unit price outliers on raw rows before dedup collapses individual lines
    _detect_unit_price_outliers(conn, col_map, JOB_DIR)
        
    # 3. Create schema tables
    # (Schema is defined in reference/schemas.md but we just create aws_li_catalog here)
    conn.execute("DROP TABLE IF EXISTS aws_li_catalog")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aws_li_catalog (
            aws_li_key VARCHAR PRIMARY KEY,
            product VARCHAR,
            usage_type VARCHAR,
            operation VARCHAR,
            aws_region VARCHAR,
            gcp_region VARCHAR,
            pricing_model VARCHAR,
            line_item_type VARCHAR,
            is_workload BOOLEAN,
            total_usage DOUBLE,
            aws_amortized_cost DOUBLE,
            projection_note VARCHAR,
            instance_type VARCHAR,
            instance_vcpus INTEGER,
            instance_ram_gb DOUBLE,
            instance_arch VARCHAR,
            instance_count DOUBLE,
            billing_days INTEGER,
            aws_effective_unit_rate DOUBLE,
            license_model VARCHAR,
            operating_system VARCHAR,
            database_engine VARCHAR,
            deployment_option VARCHAR,
            volume_type VARCHAR,
            pricing_unit VARCHAR,
            workload_class VARCHAR,
            billing_format VARCHAR,
            product_code VARCHAR,
            billing_month VARCHAR,
            aws_amortized_cost_native DOUBLE,
            currency VARCHAR,
            public_on_demand_cost DOUBLE,
            aws_od_cost DOUBLE,
            sp_ri_discount_pct DOUBLE,
            od_basis VARCHAR
        )
    """)
    # product_code (= CUR lineItem/ProductCode) is the authoritative stable key
    # aws_normalizer.py keys canonical_service on. Guard for DBs created before
    # this column existed (fresh jobs get it from the CREATE above).
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS product_code VARCHAR")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS billing_month VARCHAR")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS aws_amortized_cost_native DOUBLE")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS currency VARCHAR")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS public_on_demand_cost DOUBLE")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS aws_od_cost DOUBLE")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS sp_ri_discount_pct DOUBLE")
    conn.execute("ALTER TABLE aws_li_catalog ADD COLUMN IF NOT EXISTS od_basis VARCHAR")

    # Reconcile sum helper
    if is_raw_cur:
        cost_col = c(['lineitem/unblendedcost', 'unblendedcost'])
        if cost_col == "NULL": cost_col = c(['lineitem/blendedcost'])
        # GROSS basis (Finding B, policy-decided): workload is compared at AWS
        # on-demand/list cost (pre SP/RI/EDP discount) vs GCP on-demand. The gross
        # figure lives in pricing/publicOnDemandCost. Discount/commitment rows keep
        # their actual (unblended) amount and are surfaced separately. If the gross
        # column is absent (thin CUR export), fall back to unblended (gross==net).
        gross_col = c(['pricing/publicondemandcost', 'publicondemandcost'])
        if gross_col == "NULL":
            gross_col = cost_col
        # A row is projectable workload iff it is real usage AND not a marketplace/
        # support third-party line. SavingsPlanCoveredUsage is now workload — under
        # gross basis it is real usage at list price (previously excluded as $0 net).
        _raw_lit = c(['lineitem/lineitemtype', 'lineitemtype'])
        _pc = c(['lineitem/productcode', 'productcode'])
        _cur_lit, _workload_expr = _sql_classify_charge(
            _sql_num(cost_col),
            _pc,
            c(['lineitem/usagetype', 'usagetype']),
            c(['lineitem/operation', 'operation']),
            category_expr="''",
            raw_lit_expr=_raw_lit,
        )
    else:
        cost_col = c(['cost_($)', 'cost'])

    # Build SQL to group and classify
    if is_raw_cur:
        sql = f"""
            WITH _raw_flat AS (
                SELECT
                    -- Prefer a human-readable name (e.g. "Amazon Simple Storage
                    -- Service") over lineItem/ProductCode (AWS's short machine
                    -- code, e.g. "AmazonS3"). Every downstream classifier
                    -- (classify_mechanics.py, aws_normalizer.py, render_report.py's
                    -- pill_type) matches on friendly-name substrings/word-
                    -- boundaries ("Simple Storage", "\bS3\b", ...), which a bare
                    -- code like "AmazonS3"/"AmazonRDS" never satisfies (no word
                    -- boundary between the "n" of "Amazon" and the code that
                    -- follows it once lowercased). Confirmed real: this silently
                    -- sent every raw-CUR S3/RDS/EC2 row to misc -> passthrough
                    -- instead of the deterministic static mappers.
                    --
                    -- Two shapes of friendly name exist across real bills:
                    --   1. A flat product/ProductName column (some CUR exports).
                    --   2. A bare `product` column holding the ENTIRE product
                    --      struct as a JSON string (confirmed real on a live
                    --      customer bill -- product_name nested inside product's
                    --      JSON, e.g. Amazon Elastic File System, alongside
                    --      storage_class/access_type -- with product_servicecode
                    --      carrying the raw "AmazonEFS" code separately). Case 1
                    --      never fires for this bill shape since no
                    --      product/ProductName-style column exists at all --
                    --      only the JSON-in-`product` form does.
                    -- TRY() guards case 2 against a `product` column that isn't
                    -- JSON (older/other bill formats) or is NULL/absent — both
                    -- resolve to NULL and fall through to the code, same as before.
                    COALESCE(
                        NULLIF({c(['product/productname', 'productname'])}, ''),
                        NULLIF(TRY(json_extract_string({c(['product'])}, '$.product_name')), ''),
                        {c(['lineitem/productcode', 'productcode'])},
                        ''
                    ) as product,
                    COALESCE({c(['lineitem/usagetype', 'usagetype'])}, '') as usage_type,
                    COALESCE({c(['lineitem/operation', 'operation'])}, '') as operation,
                    -- Region fallback chain (Finding K): AWS leaves product_region_code
                    -- blank on data-transfer rows and carries the region in
                    -- product_from_region_code / product_location instead; ingest read
                    -- only the primary column, silently dropping those to 'global'.
                    -- Fall back through the other real region columns (both region
                    -- CODES and display NAMES map downstream), excluding SP 'Any'.
                    COALESCE(
                        NULLIF({c(['product/region', 'region'])}, ''),
                        NULLIF({c(['product_from_region_code', 'product/fromregioncode'])}, ''),
                        NULLIF({c(['product_location', 'product/location'])}, ''),
                        NULLIF(NULLIF({c(['savings_plan_region', 'savingsplan/region'])}, ''), 'Any'),
                        ''
                    ) as aws_region,
                    COALESCE({c(['pricing/term', 'term'])}, 'OnDemand') as pricing_model,
                    COALESCE({c(['lineitem/lineitemtype', 'lineitemtype'])}, '') as line_item_type,
                    CASE WHEN {_workload_expr} THEN TRUE ELSE FALSE END as is_workload,
                    {_sql_num(c(['lineitem/usageamount', 'usageamount']))} as row_usage,
                    -- GROSS basis: workload rows carry on-demand/list cost
                    -- (publicOnDemandCost); discount/commitment rows keep their
                    -- actual (unblended) amount for the separate savings section.
                    CASE WHEN {_workload_expr}
                         THEN {_sql_num(gross_col)}
                         ELSE {_sql_num(cost_col)}
                    END as row_cost,
                    CASE WHEN {c(['pricing/publicondemandcost', 'publicondemandcost'])} != 'NULL'
                         THEN {_sql_num(c(['pricing/publicondemandcost', 'publicondemandcost']))}
                         ELSE NULL
                    END as row_public_od,
                    COALESCE({c(['product/licensemodel', 'licensemodel'])}, '') as license_model,
                    COALESCE({c(['product/operatingsystem', 'operatingsystem'])}, '') as operating_system,
                    COALESCE({c(['product/databaseengine', 'databaseengine'])}, '') as database_engine,
                    COALESCE({c(['product/deploymentoption', 'deploymentoption'])}, '') as deployment_option,
                    COALESCE({c(['product/volumetype', 'volumetype'])}, '') as volume_type,
                    COALESCE({c(['pricing/unit', 'unit'])}, '') as pricing_unit,
                    -- Authoritative stable key: the raw CUR service code
                    -- (lineItem/ProductCode), e.g. "AmazonEC2". Already read above
                    -- for the product-name fallback and is_workload; now persisted
                    -- as its own column so aws_normalizer.py can key on it.
                    COALESCE({c(['lineitem/productcode', 'productcode'])}, '') as product_code,
                    -- Billing month (Finding N): without it in the dedup key, a
                    -- multi-month CUR collapses each line item across months, so one
                    -- instance running 2 months looks like 2 instances (instance_count
                    -- doubles, billing_days defaults wrong). Truncate to YYYY-MM so
                    -- each month is its own catalog row. Blank on single-period/Hive
                    -- reads degrades to today's behavior.
                    COALESCE(
                        strftime(COALESCE(try_cast({c(['bill/billingperiodstartdate', 'bill_billing_period_start_date'])} AS DATE), try_strptime(CAST({c(['bill/billingperiodstartdate', 'bill_billing_period_start_date'])} AS VARCHAR), '%m/%d/%Y'), try_strptime(CAST({c(['bill/billingperiodstartdate', 'bill_billing_period_start_date'])} AS VARCHAR), '%m/%d/%y')), '%Y-%m'),
                        strftime(COALESCE(try_cast({c(['lineitem/usagestartdate', 'line_item_usage_start_date'])} AS DATE), try_strptime(CAST({c(['lineitem/usagestartdate', 'line_item_usage_start_date'])} AS VARCHAR), '%m/%d/%Y'), try_strptime(CAST({c(['lineitem/usagestartdate', 'line_item_usage_start_date'])} AS VARCHAR), '%m/%d/%y')), '%Y-%m'),
                        LEFT(CAST({c(['bill/billingperiodstartdate', 'bill_billing_period_start_date'])} AS VARCHAR), 7),
                        LEFT(CAST({c(['lineitem/usagestartdate', 'line_item_usage_start_date'])} AS VARCHAR), 7),
                        ''
                    ) as billing_month
                FROM aws_raw
                WHERE {c(['lineitem/lineitemtype', 'lineitemtype'])} != 'Tax'
            )
            INSERT INTO aws_li_catalog (
                aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                public_on_demand_cost,
                license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit,
                product_code, billing_month
            )
            SELECT
                md5(product || usage_type || operation || aws_region || pricing_model || line_item_type || CAST(is_workload AS VARCHAR) || license_model || operating_system || database_engine || deployment_option || volume_type || pricing_unit || billing_month) as key,
                product, usage_type, operation, aws_region, NULL as gcp_region, pricing_model, line_item_type, is_workload,
                SUM(row_usage) as total_usage,
                SUM(row_cost) as aws_amortized_cost,
                SUM(row_public_od) as public_on_demand_cost,
                license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit,
                -- product_code is functionally constant within a dedup group (it is
                -- 1:1 with product); carried via max() so it is NOT part of the md5
                -- dedup key (the md5 dedup key includes billing_month to keep months distinct).
                max(product_code) as product_code,
                billing_month
            FROM _raw_flat
            GROUP BY product, usage_type, operation, aws_region, pricing_model, line_item_type, is_workload,
                     license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit,
                     billing_month
        """
        conn.execute(sql)

        # Implicit on-demand/private-rate discount: workload rows now carry GROSS
        # (publicOnDemandCost), but AWS actually charged the (lower) unblended rate.
        # That per-row gap has no line item of its own, so without it the catalog
        # would not reconcile to the real invoice. Re-add the gap as one explicit
        # discount row (is_workload=FALSE) so: gross_workload + all discounts =
        # actual paid. Only meaningful when a distinct gross column exists.
        if gross_col != cost_col:
            gap = conn.execute(
                f"SELECT COALESCE(SUM({_sql_num(gross_col)} - {_sql_num(cost_col)}), 0) "
                f"FROM aws_raw WHERE {_raw_lit} != 'Tax' AND {_workload_expr}"
            ).fetchone()[0] or 0.0
            if abs(gap) > 0.005:
                import hashlib as _hl
                disc_key = _hl.sha256(b"aws-ondemand-rate-discount").hexdigest()
                conn.execute(
                    """
                    INSERT INTO aws_li_catalog (
                        aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                        pricing_model, line_item_type, is_workload, total_usage,
                        aws_amortized_cost, projection_note
                    ) VALUES (?, 'AWS On-Demand/Private-Rate Discount', 'Discount', 'Discount',
                              'global', 'global', 'OnDemand', 'Discount', false, 1, ?, ?)
                    """,
                    [disc_key, -float(gap),
                     "Negotiated/private-rate discount baked into unblended rates "
                     "(gross publicOnDemandCost minus actual unblended across workload "
                     "rows) — surfaced here so AWS gross reconciles to the actual invoice."],
                )
    else:
        # ── Cloud-billing spreadsheet export format ────────────────────────────
        # Columns: Category, Subcategory, Meter, Consumed, Cost, Unit Price,
        #          Currency, Account ID, [Account Reference,] Location, Resource
        # [Cloud Account] optional leading column. No "lineItem/" prefix.
        # Detection: "Category" + "Meter" + "Cost" all present.
        if c(['category']) != 'NULL' and c(['meter']) != 'NULL' and c(['cost']) != 'NULL' and c(['service']) == 'NULL':
            cat_col  = c(['category'])
            sub_col  = c(['subcategory'])
            meter_col = c(['meter'])
            consumed_col = c(['consumed'])
            cost_col_raw = c(['cost'])
            loc_col  = c(['location'])
            date_col = c(['billingperiodstartdate', 'bill_billing_period_start_date', 'date', 'month', 'period', 'billing_period', 'start_date', 'usage_start_date', 'billing_month'])

            _sheet_lit, _sheet_is_wl = _sql_classify_charge(
                f"TRY_CAST({cost_col_raw} AS DOUBLE)",
                cat_col,
                meter_col,
                op_expr=f"COALESCE({sub_col if sub_col != 'NULL' else "''"}, '')",
                category_expr=cat_col,
            )

            sql = f"""
                INSERT INTO aws_li_catalog (
                    aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                    pricing_model, line_item_type, is_workload,
                    total_usage, aws_amortized_cost, pricing_unit, billing_month
                )
                WITH _raw AS (
                    SELECT
                        COALESCE({cat_col},   '')       AS product,
                        COALESCE({meter_col}, '')       AS usage_type,
                        COALESCE({sub_col if sub_col != 'NULL' else "''"},  '') AS operation,
                        -- Strip availability-zone suffix (e.g. ap-south-1a → ap-south-1)
                        regexp_replace(
                            COALESCE({loc_col if loc_col != 'NULL' else "''"}, ''),
                            '([0-9])[a-z]$', '\\1'
                        )                              AS aws_region,
                        CASE
                            WHEN {meter_col} ILIKE '%spot%' THEN 'Spot'
                            WHEN {meter_col} ILIKE '%reserved%' THEN 'Committed'
                            ELSE 'OnDemand'
                        END                            AS pricing_model,
                        {_sheet_lit}                   AS line_item_type,
                        {_sheet_is_wl}                 AS is_workload,
                        COALESCE(TRY_CAST({consumed_col if consumed_col != 'NULL' else '0'} AS DOUBLE), 0) AS total_usage,
                        COALESCE(TRY_CAST({cost_col_raw} AS DOUBLE), 0) AS row_cost,
                        -- Infer pricing unit from meter name.
                        -- Order matters: more specific patterns first.
                        CASE
                            WHEN {meter_col} ILIKE '%Lambda-GB-Second%'   THEN 'Lambda-GB-Second'
                            -- MSK broker storage is per GB-Mo
                            WHEN {meter_col} ILIKE '%Kafka.Storage%'      THEN 'GB-Mo'
                            -- EBS volumes and snapshots are per GB-Mo
                            WHEN {meter_col} ILIKE '%EBS:%'
                              OR {meter_col} ILIKE '%VolumeUsage%'
                              OR {meter_col} ILIKE '%SnapshotUsage%'      THEN 'GB-Mo'
                            -- GB-Hour meters (Fargate, ECS) → GB-Mo label
                            WHEN {meter_col} ILIKE '%-GB-Hour%'           THEN 'GB-Mo'
                            -- Hourly meters: instances, brokers, LBs, gateways
                            WHEN {meter_col} ILIKE '%-Hours%'
                              OR {meter_col} ILIKE '%:per%'
                              OR {meter_col} ILIKE '%BoxUsage%'
                              OR {meter_col} ILIKE '%SpotUsage%'
                              OR {meter_col} ILIKE '%RunBroker%'
                              OR {meter_col} ILIKE '%LoadBalancerUsage%'
                              OR {meter_col} ILIKE '%NatGateway-Hour%'
                              OR {meter_col} ILIKE '%TransitGateway-Hour%'
                              OR {meter_col} ILIKE '%Kafka.%'             THEN 'Hrs'
                            WHEN {meter_col} ILIKE '%-Bytes%'
                              OR {meter_col} ILIKE '%-Out-Bytes%'         THEN 'GB'
                            WHEN {meter_col} ILIKE '%Request%'            THEN 'Requests'
                            WHEN {meter_col} ILIKE '%LCU%'               THEN 'LCU-Hrs'
                            WHEN {meter_col} ILIKE '%Storage%'
                              OR {meter_col} ILIKE '%ByteHrs%'
                              OR {meter_col} ILIKE '%TimedStorage%'       THEN 'GB-Mo'
                            ELSE ''
                        END                            AS pricing_unit,
                        COALESCE(
                            strftime(COALESCE(try_cast({date_col} AS DATE), try_strptime(CAST({date_col} AS VARCHAR), '%m/%d/%Y'), try_strptime(CAST({date_col} AS VARCHAR), '%m/%d/%y')), '%Y-%m'),
                            LEFT(CAST({date_col} AS VARCHAR), 7),
                            ''
                        )                              AS billing_month
                    FROM aws_raw
                    WHERE COALESCE(TRY_CAST({cost_col_raw} AS DOUBLE), 0) != 0
                )
                SELECT
                    md5(product || usage_type || operation || aws_region || pricing_model || line_item_type || CAST(is_workload AS VARCHAR) || billing_month) AS aws_li_key,
                    product, usage_type, operation, aws_region,
                    NULL AS gcp_region,
                    pricing_model, line_item_type, is_workload,
                    SUM(total_usage)  AS total_usage,
                    SUM(row_cost)     AS aws_amortized_cost,
                    pricing_unit,
                    billing_month
                FROM _raw
                GROUP BY product, usage_type, operation, aws_region,
                         pricing_model, line_item_type, is_workload, pricing_unit, billing_month
            """
            conn.execute(sql)
            log.info("Loaded spreadsheet-export format (Category/Meter/Cost columns)")

        # Check if custom semicolon-separated usage column exists in aws_raw
        elif 'usage_(with_units)' in col_map:
            import hashlib
            raw_rows = conn.execute("SELECT * FROM aws_raw").fetchall()
            col_names = [col[0] for col in conn.description]
            
            month_idx = col_names.index(col_map["month"])
            service_idx = col_names.index(col_map["service"])
            usage_idx = col_names.index(col_map["usage_(with_units)"])
            cost_idx = col_names.index(col_map["cost_(usd)"])
            
            aggregated = {}
            for row in raw_rows:
                month = row[month_idx]
                b_month = str(month or '')[:7]
                service = row[service_idx]
                usage_str = row[usage_idx]
                cost_str = row[cost_idx]
                
                if not service or service.lower() in ('tax/other', 'tax'):
                    row_cost = 0.0
                    if cost_str:
                        try:
                            row_cost = float(str(cost_str).replace(",", "").replace("$", "").strip())
                        except ValueError:
                            pass
                    group_key = ("Tax/Other", "Tax", "Tax", "global", "OnDemand", "Tax", False, None, "", b_month)
                    if group_key not in aggregated:
                        aggregated[group_key] = {"total_usage": 1.0, "row_cost": 0.0}
                    aggregated[group_key]["row_cost"] += row_cost
                    continue
                
                row_cost = 0.0
                if cost_str:
                    try:
                        row_cost = float(str(cost_str).replace(",", "").replace("$", "").strip())
                    except ValueError:
                        pass
                
                usages = []
                if usage_str:
                    parts = [p.strip() for p in str(usage_str).split(";") if p.strip()]
                    for part in parts:
                        m = re.match(r'([\d,\.]+)\s*(.*)', part)
                        if m:
                            try:
                                val = float(m.group(1).replace(",", ""))
                            except ValueError:
                                val = 0.0
                            unit = m.group(2).strip()
                            usages.append((val, unit))
                
                if not usages:
                    continue
                    
                # FAITHFUL REPRESENTATION - the bill is the only source of
                # truth. This aggregated export gives ONE total cost per
                # service plus a combined usage string, with NO per-component
                # cost and NO instance type. Splitting the cost by assumed
                # ratios, or stamping a default instance type (t3.medium),
                # would invent data the bill does not contain. So emit the row
                # exactly as presented - one row per service at its real cost,
                # raw usage string preserved - as a passthrough that cannot be
                # projected per-resource without fabricating data.
                usage_clean = "; ".join(f"{v:g} {u}".strip() for v, u in usages)
                note = ("Aggregated bill row: source gives only a per-service "
                        f"total cost with combined usage ('{usage_clean[:160]}') "
                        "and no per-component cost or instance type. Presented "
                        "as-is (passthrough); not projectable per-resource "
                        "without fabricating data.")
                is_wl = _is_workload_py(None, service, usage_clean)
                group_key = (service, "AggregatedUsage", usage_clean, "global",
                             "OnDemand", "Usage", is_wl, note, "", b_month)
                if group_key not in aggregated:
                    aggregated[group_key] = {"total_usage": 0.0, "row_cost": 0.0}
                aggregated[group_key]["row_cost"] += row_cost

            for gk, val in aggregated.items():
                prod, ut, op, aws_reg, pm, lit, is_wl, proj_note, p_unit, b_month = gk
                # Faithful cost: the real per-service total from the bill
                # (do not gate on total_usage, which is 0 for aggregated rows).
                cost = val["row_cost"]
                
                # Must hash ALL fields of group_key, not a subset — otherwise two
                # distinct aggregation buckets that differ only in a field left out
                # here (e.g. projection_note or pricing_unit) collide onto the same
                # aws_li_key and the INSERT below throws a primary-key violation.
                key_str = f"{prod}{ut}{op}{aws_reg}{pm}{lit}{str(is_wl)}{proj_note}{p_unit}{b_month}"
                aws_li_key = hashlib.md5(key_str.encode('utf-8')).hexdigest()
                
                conn.execute("""
                    INSERT INTO aws_li_catalog (
                        aws_li_key, product, usage_type, operation, aws_region, gcp_region, 
                        pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                        projection_note, pricing_unit, billing_month
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (aws_li_key, prod, ut, op, aws_reg, pm, lit, is_wl, val["total_usage"], cost, proj_note, p_unit, b_month))
            log.info("Loaded custom flat CSV with semicolon-separated Usage (with Units)")

        # Check if "service" column exists in aws_raw
        elif c(['service']) == 'NULL':
            # Run the Python-based ingestion/mapping for the 4-column flat CSV
            import hashlib
            cur_raw = conn.cursor()
            cur_raw.execute("SELECT * FROM aws_raw")
            # Fetch column names
            col_names = [col[0] for col in cur_raw.description]
            # Region is genuinely optional on this format — some AWS export
            # variants (e.g. a Cost Explorer summary grouped by charge
            # description only) never include a region column at all. Rows
            # without a resolvable region already fall back to "global"
            # elsewhere in this pipeline (region mapping, rate lookup), so
            # requiring the column to exist at all was over-strict: it
            # rejected a genuine, correctly-shaped AWS bill outright instead
            # of just leaving region blank for it.
            _required = ["Description", "Usage Quantity", "Amount in USD"]
            _missing = [c for c in _required if c not in col_names]
            if _missing:
                # _fail (not raise SystemExit): attempts the LLM-normalization
                # fallback for odd-but-valid layouts, and ALWAYS writes failure.txt
                # so the Go watcher's readFailureReason has a reason to surface.
                _fail(
                    "Unrecognized bill format: this file doesn't match AWS CUR, the AWS "
                    "PDF-export layout, the Category/Meter/Cost spreadsheet layout, or the "
                    f"flat-CSV layout (missing column(s): {', '.join(_missing)}). "
                    f"Actual columns found: {col_names}. "
                    "This tool projects AWS billing data to GCP - verify the uploaded file "
                    "is an AWS billing export, not another cloud provider's."
                )
            desc_idx = col_names.index("Description")
            region_idx = col_names.index("Region") if "Region" in col_names else None
            usage_idx = col_names.index("Usage Quantity")
            cost_idx = col_names.index("Amount in USD")
            date_idx = None
            for _candidate in ("billing_month", "billingmonth", "month", "date", "period", "billing_period", "billingperiodstartdate", "usagestartdate"):
                for idx, col in enumerate(col_names):
                    if col.lower().replace("/", "_").replace(" ", "_") == _candidate.replace(" ", "_"):
                        date_idx = idx
                        break
                if date_idx is not None:
                    break

            # Temporary storage to aggregate rows by the same key
            aggregated = {}
            CHUNK_SIZE = 5000

            while True:
                chunk = cur_raw.fetchmany(CHUNK_SIZE)
                if not chunk:
                    break
                for row in chunk:
                    desc = row[desc_idx]
                    region = row[region_idx] if region_idx is not None else ""
                    b_month = str(row[date_idx] or '')[:7] if date_idx is not None else ""
                    usage_str = row[usage_idx]
                    cost_str = row[cost_idx]
                
                if not desc or desc.lower() == 'total tax' or 'tax' in desc.lower():
                    continue
                
                # Parse usage quantity and unit
                total_usage = 0.0
                pricing_unit = ""
                if usage_str:
                    m = re.match(r'([\d,\.]+)\s*(.*)', str(usage_str).strip())
                    if m:
                        try:
                            total_usage = float(m.group(1).replace(",", ""))
                        except ValueError:
                            total_usage = 0.0
                        pricing_unit = m.group(2).strip()
                        
                # Parse cost
                row_cost = 0.0
                if cost_str:
                    try:
                        row_cost = float(str(cost_str).replace(",", "").replace("$", "").strip())
                    except ValueError:
                        row_cost = 0.0
                        
                # Map product and usage_type
                desc_lower = desc.lower()
                # "reserved instance applied" (an RI-discount-adjustment line item in
                # some bill formats) is a narrower phrase than this bill format's own
                # product-catalog naming, which plainly says "... Reserved Instances"
                # (e.g. "Amazon Redshift Node Usage Reserved Instances", "Amazon
                # Elastic Compute Cloud running Linux/UNIX Reserved Instances") —
                # confirmed real: this narrower check missed every genuinely-Reserved
                # row across EC2/RDS/Redshift in one bill, silently defaulting
                # pricing_model to "OnDemand" for all of them. Since these rows'
                # amortized cost already reflects the real reserved/discounted rate
                # (not a separate fee-adjustment line), this is exactly the same
                # "Committed"/"DiscountedUsage" case, not a distinct one — downstream
                # (projection_view.py) relies on this pair to compare GCP's committed
                # rate against AWS's committed rate instead of GCP's full on-demand
                # rate against an already-discounted AWS price (a real, structural
                # 2-10x over-projection when this pairing is missed).
                is_reserved = "reserved instance applied" in desc_lower or "reserved instances" in desc_lower
                pricing_model = "OnDemand"
                if "spot" in desc_lower:
                    pricing_model = "Spot"
                elif is_reserved:
                    pricing_model = "Committed"
                
                product = ""
                usage_type = ""
                
                # EMR's per-node surcharge is frequently described in free-text
                # bills as "... EC2 instance usage <type> (EMR) ..." — if the EC2
                # branch below matched first, the row gets permanently relabeled
                # as plain Elastic Compute Cloud and all EMR identity is lost
                # before classify_mechanics.py / map_emr() ever see it, so it
                # never gets priced via the real Dataproc Premium SKU and instead
                # falls through to a generic Compute Engine VM mapping (mirrors
                # the same EC2/EMR exclusion already applied in
                # classify_mechanics.py's compute_breakdown rule).
                if ("elastic compute" in desc_lower or desc_lower.startswith("ec2") or "natgateway" in desc_lower) \
                        and "emr" not in desc_lower:
                    product = "Amazon Elastic Compute Cloud"
                    if "natgateway" in desc_lower:
                        if "hour" in desc_lower:
                            usage_type = "NatGateway-Hours"
                        else:
                            usage_type = "NatGateway-Bytes"
                    elif "t4g cpu credits" in desc_lower:
                        usage_type = "T4G CPU Credits"
                    else:
                        inst_match = re.search(r'\b([a-z0-9]+\.[a-z0-9]+)\b', desc_lower)
                        if inst_match:
                            itype = inst_match.group(1)
                            prefix = "SpotUsage:" if pricing_model == "Spot" else "BoxUsage:"
                            usage_type = prefix + itype
                        else:
                            usage_type = "BoxUsage"
                elif "cloudtrail" in desc_lower:
                    product = "AWS CloudTrail"
                    usage_type = "CloudTrail"
                elif "cloudwatch" in desc_lower:
                    product = "AmazonCloudWatch"
                    if "alarm" in desc_lower:
                        usage_type = "AlarmThreshold"
                    else:
                        usage_type = "Metrics"
                elif "cost explorer" in desc_lower:
                    product = "AWS Cost Explorer"
                    usage_type = "CostExplorer"
                elif "dax" in desc_lower:
                    product = "Amazon DynamoDB"
                    usage_type = "DAX"
                elif "dms" in desc_lower:
                    product = "AWS Database Migration Service"
                    usage_type = "DMS"
                elif "data transfer" in desc_lower:
                    product = "AWS Data Transfer"
                    usage_type = "DataTransfer"
                elif "directory service" in desc_lower:
                    product = "AWS Directory Service"
                    usage_type = "DirectoryService"
                elif "dynamodb" in desc_lower:
                    product = "Amazon DynamoDB"
                    usage_type = "DynamoDB"
                elif "ebs" in desc_lower:
                    product = "Amazon Elastic Block Store"
                    if "snapshot" in desc_lower:
                        usage_type = "EBS:Snapshot"
                    else:
                        for vt in ["gp2", "gp3", "io1", "io2", "st1", "sc1"]:
                            if vt in desc_lower:
                                usage_type = f"EBS:VolumeUsage:{vt}"
                                break
                        if not usage_type:
                            usage_type = "EBS:VolumeUsage"
                elif "ecr" in desc_lower:
                    product = "Amazon EC2 Container Registry"
                    usage_type = "ECR"
                elif "efs" in desc_lower:
                    product = "Amazon Elastic File System"
                    usage_type = "EFS"
                elif "eks" in desc_lower:
                    product = "Amazon Elastic Kubernetes Service"
                    usage_type = "EKS"
                elif "emr" in desc_lower:
                    product = "Amazon Elastic MapReduce"
                    # Preserve the instance type so apply_static_mappings.py's
                    # _emr_vcpus() can resolve a real vCPU count for the
                    # Dataproc Premium ($/vCPU-hr) formula. The flat literal
                    # "EMR" (no instance type) used here previously matched
                    # neither of _emr_vcpus()'s regexes, so every such row
                    # silently fell to the "vCPU unknown" passthrough branch
                    # instead of computing the real formula-based price.
                    # "BoxUsage:<type>" matches the fallback pattern
                    # _emr_vcpus() already supports for raw-CUR-style EMR rows.
                    inst_match = re.search(r'\b([a-z0-9]+\.[a-z0-9]+)\b', desc_lower)
                    usage_type = f"BoxUsage:{inst_match.group(1)}" if inst_match else "EMR"
                elif "elasticache" in desc_lower or "valkey" in desc_lower:
                    product = "Amazon ElastiCache"
                    usage_type = "ElastiCache"
                elif "glue" in desc_lower:
                    product = "AWS Glue"
                    usage_type = "Glue"
                elif "kms" in desc_lower:
                    product = "AWS Key Management Service"
                    usage_type = "KMS"
                elif "lambda" in desc_lower:
                    product = "AWS Lambda"
                    if "compute" in desc_lower:
                        usage_type = "Lambda-GB-Second"
                    else:
                        usage_type = "Lambda"
                elif "nlb" in desc_lower:
                    product = "Elastic Load Balancing"
                    usage_type = "LoadBalancerUsage"
                elif "neptune" in desc_lower:
                    product = "Amazon Neptune"
                    usage_type = "Neptune"
                elif "quicksight" in desc_lower:
                    product = "Amazon QuickSight"
                    usage_type = "QuickSight"
                elif "redshift" in desc_lower:
                    product = "Amazon Redshift"
                    usage_type = "Redshift"
                elif "rekognition" in desc_lower:
                    product = "Amazon Rekognition"
                    usage_type = "Rekognition"
                elif "route 53" in desc_lower or "route53" in desc_lower:
                    product = "Amazon Route 53"
                    usage_type = "Route53"
                # Bare "s3"/"ses" substring checks would false-match region-code
                # prefixes ("APS3" contains "s3") and common English words ("ses"
                # in "licenses") — confirmed real for "s3" (an SQS row's own
                # description contained "APS3" and was misclassified as S3
                # elsewhere in this pipeline before this fix); "ses" in "licenses"
                # is the exact same documented risk aws_normalizer.py already
                # guards against for "ecs"/"ses". AWS's real product names always
                # contain the full "simple storage"/"simple email" phrase, so
                # checking that alone is both sufficient and safe.
                elif "simple storage" in desc_lower:
                    product = "Amazon Simple Storage Service"
                    usage_type = "S3"
                elif "simple email" in desc_lower:
                    product = "Amazon Simple Email Service"
                    usage_type = "SES"
                elif "sqs" in desc_lower or "simple queue" in desc_lower:
                    product = "Amazon Simple Queue Service"
                    usage_type = "SQS"
                elif "secrets manager" in desc_lower:
                    product = "AWS Secrets Manager"
                    usage_type = "SecretsManager"
                elif "security hub" in desc_lower:
                    product = "AWS Security Hub"
                    usage_type = "SecurityHub"
                elif "vpc" in desc_lower:
                    product = "Amazon Elastic Compute Cloud"
                    if "endpoint" in desc_lower:
                        usage_type = "VPCEndpoint-Hours"
                    elif "peering" in desc_lower:
                        usage_type = "DataTransfer-Peering-Bytes"
                    elif "transit gateway" in desc_lower:
                        usage_type = "TransitGateway-Hours"
                    elif "vpn" in desc_lower:
                        usage_type = "VPN-Hours"
                    elif "public ipv4" in desc_lower:
                        usage_type = "IPAddress-Hours"
                    else:
                        usage_type = "VPC"
                else:
                    # Unknown service: do NOT silently relabel to "Other" and
                    # passthrough — that hides the row and loses its identity. Keep
                    # the bill's real description verbatim as the product so
                    # canonical_service / the Phase-2 LLM can still resolve it, and
                    # flag it (below) so it surfaces for review. Surface, don't bury.
                    product = desc.strip() or "Unknown service"
                    usage_type = "UnrecognizedService"

                _ch_cat, line_item_type, is_workload = classify_charge(
                    product=product,
                    usage_type=usage_type,
                    operation=desc,
                    cost=row_cost,
                )
                    
                projection_note = None
                if usage_type == "UnrecognizedService":
                    projection_note = ("Unrecognized service in 4-column bill: no keyword "
                                       "matched, so the bill's description is kept verbatim as "
                                       "the service (not bucketed as 'Other') for resolution/"
                                       "review downstream.")
                    usage_type = ""
                if "reserved instance applied" in desc_lower:
                    projection_note = 'AWS rate is RI-amortized; compare GCP CUD, not OD'

                group_key = (product, usage_type, desc, region, pricing_model, line_item_type, is_workload, projection_note, pricing_unit, b_month)
                if group_key not in aggregated:
                    aggregated[group_key] = {"total_usage": 0.0, "row_cost": 0.0}
                aggregated[group_key]["total_usage"] += total_usage
                aggregated[group_key]["row_cost"] += row_cost
                
            # Insert aggregated rows in batches
            insert_rows = []
            for gk, val in aggregated.items():
                prod, ut, op, aws_reg, pm, lit, is_wl, proj_note, p_unit, b_month = gk
                key_str = f"{prod}{ut}{op}{aws_reg}{pm}{lit}{str(is_wl)}{proj_note}{p_unit}{b_month}"
                aws_li_key = hashlib.md5(key_str.encode('utf-8')).hexdigest()
                insert_rows.append((aws_li_key, prod, ut, op, aws_reg, pm, lit, is_wl, val["total_usage"], val["row_cost"], proj_note, p_unit, b_month))

            if insert_rows:
                conn.executemany("""
                    INSERT INTO aws_li_catalog (
                        aws_li_key, product, usage_type, operation, aws_region, gcp_region, 
                        pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                        projection_note, pricing_unit, billing_month
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """, insert_rows)
        else:
            _inv_lit, _inv_is_wl = _sql_classify_charge(
                _sql_num(cost_col),
                c(['service']),
                c(['custom_usage_type']),
                op_expr=c(['description']),
                category_expr="''",
            )

            # CTE pre-computes all CASE expressions from raw columns so the outer
            # GROUP BY only sees already-derived aliases — avoids DuckDB strict-mode
            # "column must appear in GROUP BY" errors on unaggregated column refs.
            sql = f"""
                WITH _flat AS (
                    SELECT
                        COALESCE({c(['service'])}, '')          AS product,
                        COALESCE({c(['custom_usage_type'])}, '') AS usage_type,
                        COALESCE({c(['description'])}, '')       AS operation,
                        COALESCE({c(['region'])}, '')             AS aws_region,
                        COALESCE(
                            LEFT(CAST({c(['billing_month', 'billingmonth', 'bill/billingperiodstartdate', 'bill_billing_period_start_date', 'date', 'month', 'period', 'billing_period', 'start_date', 'usage_start_date'])} AS VARCHAR), 7),
                            ''
                        ) AS billing_month,
                        CASE
                            WHEN {c(['service'])} ILIKE '%Spot%' OR {c(['description'])} ILIKE '%Spot Instance%' THEN 'Spot'
                            WHEN {c(['description'])} ILIKE '%reserved instance applied%'
                              OR {c(['description'])} ILIKE '%reserved instances%'
                              OR {c(['service'])} ILIKE '%reserved instances%' THEN 'Committed'
                            ELSE 'OnDemand'
                        END AS pricing_model,
                        {_inv_lit}   AS line_item_type,
                        {_inv_is_wl} AS is_workload,
                        {_sql_num(c(['usage_quantity']))} AS row_usage,
                        {_sql_num(cost_col)}              AS row_cost,
                        CASE WHEN {c(['description'])} ILIKE '%reserved instance applied%'
                              OR {c(['description'])} ILIKE '%reserved instances%'
                              OR {c(['service'])} ILIKE '%reserved instances%'
                             THEN 'AWS rate is RI-amortized; compare GCP CUD, not OD' ELSE NULL END AS projection_note
                    FROM aws_raw
                    WHERE {c(['service'])} != 'Tax'
                      AND {c(['service'])} NOT ILIKE '%Tax%'
                      -- Bare '%Tax%' also matches "pre-tax"/"post-tax", which isn't a tax
                      -- charge — it's the substring the PDF's own "Total pre-tax USD X"
                      -- line uses, and the exact phrase _load_pdf()'s own gross->stated
                      -- reconciliation row (see PDF_PRETAX_RE / the "Enterprise Discount
                      -- Program & credits (reconcile gross USD ... to stated pre-tax USD
                      -- ...)" row above) puts in its own description. That row was being
                      -- silently deleted by this exact filter — the reconciliation adjustment
                      -- it carries (bringing the gross line-item sum down to the bill's
                      -- real stated total) never reached aws_li_catalog, so AWS Total
                      -- stayed at the inflated gross figure instead of the bill's real one.
                      AND ({c(['description'])} NOT ILIKE '%Tax%'
                           OR {c(['description'])} ILIKE '%pre-tax%'
                           OR {c(['description'])} ILIKE '%post-tax%')
                      AND COALESCE({c(['service'])}, '') != ''
                      AND NOT regexp_matches(COALESCE({c(['description'])}, ''), '^Amazon [A-Za-z0-9 ]+\\([0-9]+ [A-Za-z]+\\)$')
                )
                INSERT INTO aws_li_catalog (aws_li_key, product, usage_type, operation, aws_region, gcp_region, pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost, projection_note, billing_month)
                SELECT
                    md5(product || usage_type || operation || aws_region ||
                        CASE WHEN operation ILIKE '%reserved instance applied%' THEN 'Committed' ELSE 'OnDemand' END ||
                        line_item_type ||
                        CAST(is_workload AS VARCHAR) ||
                        billing_month
                    ) AS aws_li_key,
                    product, usage_type, operation, aws_region,
                    NULL AS gcp_region,
                    pricing_model, line_item_type, is_workload,
                    SUM(row_usage)  AS total_usage,
                    SUM(row_cost)   AS aws_amortized_cost,
                    projection_note,
                    billing_month
                FROM _flat
                GROUP BY product, usage_type, operation, aws_region, pricing_model, line_item_type, is_workload, projection_note, billing_month
            """
            conn.execute(sql)

    # 4. Free raw table/view and disk cache once loaded into aws_li_catalog
    _drop_raw(conn)

    # 4b. Multi-currency (Directive 1): normalize aws_amortized_cost to USD internally,
    # and store native bill amounts in aws_amortized_cost_native with the currency code.
    # Everything downstream reads aws_amortized_cost in USD and needs no FX knowledge.
    # The final report converts from USD into the bill's currency at display time.
    curr_code = (_currency_info.get("currency") or "USD").upper() if _currency_info else "USD"
    fx_rate = float(_currency_info.get("fx_rate", 1.0)) if _currency_info else 1.0

    # Initialize native cost and currency for all rows
    conn.execute("UPDATE aws_li_catalog SET aws_amortized_cost_native = aws_amortized_cost, currency = ?", [curr_code])

    if curr_code != "USD" and fx_rate > 0:
        # Convert native currency to USD in aws_amortized_cost and public_on_demand_cost
        conn.execute("UPDATE aws_li_catalog SET aws_amortized_cost = aws_amortized_cost / ?", [fx_rate])
        conn.execute("UPDATE aws_li_catalog SET public_on_demand_cost = public_on_demand_cost / ? WHERE public_on_demand_cost IS NOT NULL", [fx_rate])
        # Update currency_info to mark that aws_cost_currency in the DB is now USD
        _currency_info["aws_cost_currency"] = "USD"
        _currency_info["aws_fx_rate"] = fx_rate
        save_currency_info(JOB_DIR, _currency_info, conn)
        log.info(
            f"Multi-currency (Directive 1): Normalized bill costs from {curr_code} to USD "
            f"at rate {fx_rate:g} ({_currency_info.get('label', '')}). Native amounts stored in "
            f"aws_amortized_cost_native; internal pipeline runs purely in USD."
        )

    # 4c. OD-vs-OD Comparison (WORKORDER_od_vs_od_sp_ri). See apply_od_basis().
    apply_od_basis(conn)

    # 5. Map Regions
    catalog_rows = conn.execute("SELECT aws_li_key, aws_region, usage_type FROM aws_li_catalog").fetchall()
    region_updates = []
    for row in catalog_rows:
        key = row[0]
        aws_r = row[1]
        aws_r_clean = aws_r.strip().lower() if aws_r else ""
        gcp_r = REGION_MAP.get(aws_r_clean) or DESCRIPTIVE_REGION_MAP.get(aws_r_clean, "global")
        if gcp_r == "global":
            gcp_r = decode_region_prefix(row[2], gcp_r)
        region_updates.append((gcp_r, key))

    if region_updates:
        conn.executemany("UPDATE aws_li_catalog SET gcp_region = ? WHERE aws_li_key = ?", region_updates)
            
    # 6. Reconcile - let orchestrate.go verification gate handle failure, but check
    # In raw CUR, sometimes totals have slight float differences, we'll ignore for script unless it's huge
    
    # 7. Enrich instances
    ec2_path = os.path.join(DATA_DIR, "ec2-instance-types.json")
    rds_path = os.path.join(DATA_DIR, "rds-instance-types.json")
    
    ec2_table = {}
    if os.path.exists(ec2_path):
        with open(ec2_path) as f: ec2_table = json.load(f)
        
    rds_table = {}
    if os.path.exists(rds_path):
        with open(rds_path) as f: rds_table = json.load(f)
        
    def infer_billing_days(rows):
        candidates = [r[1] for r in rows if r[1] and 670 <= r[1] <= 745]
        if candidates:
            return round(max(candidates) / 24)
        return 30
        
    all_cat = conn.execute("""
        SELECT aws_li_key, total_usage, product, operation, aws_amortized_cost, usage_type,
               license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit
        FROM aws_li_catalog
    """).fetchall()
    b_days = infer_billing_days(all_cat)
    
    unresolved_specs = []
    resolved_updates = []

    for r in all_cat:
        key, total_usage, product, operation, aws_cost, usage_type, lic, os_name, db_eng, deploy_opt, vol_t, p_unit = r
        itype = None
        op = operation or ""
        ut = usage_type or ""

        # Aggregated passthrough rows (coarse "Usage (with Units)" export) carry
        # only real bill data — service, total cost, raw usage string. The bill
        # states no OS / license / deployment / instance type / per-component unit
        # for them, so skip all inference here: stamping defaults (Linux, License
        # Included, Single-AZ, a guessed pricing_unit) would invent data the bill
        # does not contain. The bill is the only source of truth.
        if usage_type == "AggregatedUsage":
            continue

        # 1. Infer OS
        if not os_name:
            if "windows" in op.lower() or "windows" in ut.lower():
                os_name = "Windows"
            elif "rhel" in op.lower() or "rhel" in ut.lower():
                os_name = "RHEL"
            elif "suse" in op.lower() or "suse" in ut.lower():
                os_name = "SUSE"
            else:
                os_name = "Linux"
                
        # 2. Infer DB Engine
        if not db_eng:
            if "postgres" in op.lower() or "postgres" in ut.lower():
                db_eng = "PostgreSQL"
            elif "mysql" in op.lower() or "mysql" in ut.lower():
                db_eng = "MySQL"
            elif "oracle" in op.lower() or "oracle" in ut.lower():
                db_eng = "Oracle"
            elif "sql server" in op.lower() or "sql server" in ut.lower() or "sqlserver" in op.lower() or "sqlserver" in ut.lower():
                db_eng = "SQL Server"
            elif "mariadb" in op.lower() or "mariadb" in ut.lower():
                db_eng = "MariaDB"
                
        # 3. Infer Deployment Option (Multi-AZ)
        if not deploy_opt:
            if "multi-az" in op.lower() or "multiaz" in op.lower() or "multi-az" in ut.lower() or "multiaz" in ut.lower():
                deploy_opt = "Multi-AZ"
            else:
                deploy_opt = "Single-AZ"
                
        # 4. Infer License Model
        if not lic:
            if "byol" in op.lower() or "byol" in ut.lower() or "bring your own license" in op.lower() or "bring your own license" in ut.lower() or "customer-provided" in op.lower():
                lic = "Bring Your Own License"
            else:
                lic = "License Included"
                
        # 5. Infer Volume Type
        if not vol_t:
            for vt in ["gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"]:
                if vt in op.lower() or vt in ut.lower():
                    vol_t = vt
                    break
                    
        # 6. Infer Pricing Unit
        if not p_unit:
            if "hour" in op.lower() or "hour" in ut.lower() or "boxusage" in ut.lower() or "instancehour" in ut.lower():
                p_unit = "Hrs"
            elif "gb" in op.lower() or "gb" in ut.lower() or "byte" in op.lower() or "byte" in ut.lower() or "storage" in op.lower():
                p_unit = "GB-Mo"
                
        # Update inferred info back to database
        conn.execute("""
            UPDATE aws_li_catalog
            SET license_model = ?, operating_system = ?, database_engine = ?,
                deployment_option = ?, volume_type = ?, pricing_unit = ?
            WHERE aws_li_key = ?
        """, (lic, os_name, db_eng, deploy_opt, vol_t, p_unit, key))
        LEGACY_SPECS = {
            "c3.2xlarge": { "vcpus": 8, "ram_gb": 15.0, "arch": "x86_64" },
            "c4.xlarge": { "vcpus": 4, "ram_gb": 7.5, "arch": "x86_64" },
            "c4.2xlarge": { "vcpus": 8, "ram_gb": 15.0, "arch": "x86_64" },
            "c4.8xlarge": { "vcpus": 36, "ram_gb": 60.0, "arch": "x86_64" },
            "a1.2xlarge": { "vcpus": 8, "ram_gb": 16.0, "arch": "arm64" },
            "c7g.2xlarge.search": { "vcpus": 8, "ram_gb": 16.0, "arch": "arm64" },
            "kafka.t3.small": { "vcpus": 2, "ram_gb": 2.0, "arch": "x86_64" },
            "db.t4g.xlarge": { "vcpus": 4, "ram_gb": 16.0, "arch": "arm64" }
        }
        
        # Instance type lives in `operation` on PDF/flat bills but in `usage_type`
        # on raw CUR (operation="RunInstances", usage_type="...BoxUsage:g4ad.8xlarge")
        # — search BOTH. Keying only on `operation` left instance_vcpus/ram_gb/
        # workload_class/instance_count NULL for every compute row on CUR bills.
        itext = f"{op} {ut}"
        if "RDS" in product or "Aurora" in product or "Relational Database" in product:
            m = re.search(r'(db\.[a-z0-9]+\.[a-z0-9]+)', itext, re.IGNORECASE)
            if m: itype = m.group(1).lower()
        elif "ElastiCache" in product:
            m = re.search(r'(cache\.[a-z0-9]+\.[a-z0-9]+)', itext, re.IGNORECASE)
            if m:
                itype = m.group(1).lower()
            else:
                m2 = re.search(r'([A-Z][0-9][A-Za-z0-9]*)\s+(Micro|Small|Medium|Large|XLarge|[0-9]+XLarge)\s+Cache',
                               itext, re.IGNORECASE)
                if m2:
                    fam  = m2.group(1).lower()
                    size = m2.group(2).lower()
                    itype = f"cache.{fam}.{size}"
        elif "OpenSearch" in product or "Elasticsearch" in product:
            m = re.search(r'([a-z0-9]+\.[a-z0-9]+\.search)', itext, re.IGNORECASE)
            if m: itype = m.group(1).lower()
        elif "Managed Streaming for Apache Kafka" in product or "MSK" in product:
            if "storage" in ut.lower() or "runvolume" in op.lower():
                pass  # Storage meter, not an instance type (WORKORDER_small_corrections Item 2a)
            else:
                m = re.search(r'(kafka\.[a-z0-9]+\.[a-z0-9]+)', itext, re.IGNORECASE)
                if m: itype = m.group(1).lower()
        elif "Elastic Compute Cloud" in product or "EC2" in product:
            # Match the instance-type SHAPE (family+gen+size, e.g. "t2.xlarge",
            # "c7gd.4xlarge") anywhere in the operation string, rather than
            # assuming it immediately precedes "Instance Hour" — PDF-format Spot
            # rows read "c7gd.4xlarge Linux/UNIX Spot Instance-hour in ..." (other
            # words + a hyphen in between), which the old positional regex missed
            # entirely, leaving instance_type NULL and losing deterministic
            # core+RAM (and Spot-discount) pricing for every Spot row.
            # After a "<region>-BoxUsage:" (or Spot/Heavy) prefix the rest of the
            # token IS the instance type — grab it whole so accelerator families
            # (inf1, trn1) and hyphenated "-flex" shapes (c8i-flex, m7i-flex) are
            # captured too, not just the strict family+gen+size pattern.
            m = re.search(r'(?:BoxUsage|SpotUsage|HeavyUsage):([a-z0-9][a-z0-9.\-]*\.[a-z0-9]+)', itext, re.IGNORECASE)
            if m:
                itype = m.group(1).lower()
            else:
                m = re.search(r'\b([a-z][a-z0-9\-]*\.[a-z0-9]+)\b', itext, re.IGNORECASE)
                if m: itype = m.group(1).lower()
                
        if not itype: continue

        # Managed services (OpenSearch/MSK) decorate a standard EC2 type with a
        # service suffix/prefix — "c7g.2xlarge.search", "kafka.t3.small". Strip
        # the decoration to the BASE EC2 type so the full 357-entry ec2 table
        # resolves specs for ANY instance, not just a hardcoded few.
        base_itype = itype
        if base_itype.endswith(".search"):
            base_itype = base_itype[: -len(".search")]
        if base_itype.startswith("kafka."):
            base_itype = base_itype[len("kafka.") :]

        table = rds_table if base_itype.startswith(("db.", "cache.")) else ec2_table
        spec = table.get(base_itype)
        if not spec:
            # Retry with expanded abbreviated size (e.g. db.m7g.xl -> db.m7g.xlarge, m7g.xl -> m7g.xlarge)
            expanded_base = re.sub(r'(\d*)xl$', r'\g<1>xlarge', base_itype)
            if expanded_base != base_itype:
                spec = table.get(expanded_base)

        if not spec:
            # EC2 fallback: strip db./cache. prefix if present (Item 2c)
            ec2_lookup_name = base_itype
            if ec2_lookup_name.startswith(("db.", "cache.")):
                ec2_lookup_name = re.sub(r'^(db\.|cache\.)', '', ec2_lookup_name)
            spec = ec2_table.get(ec2_lookup_name)
            if not spec:
                expanded_ec2 = re.sub(r'(\d*)xl$', r'\g<1>xlarge', ec2_lookup_name)
                if expanded_ec2 != ec2_lookup_name:
                    spec = ec2_table.get(expanded_ec2)

        if not spec:
            # Fall back to legacy static spec lookup (decorated or base name)
            spec = LEGACY_SPECS.get(itype) or LEGACY_SPECS.get(base_itype)
            
        if not spec:
            unresolved_specs.append((itype, float(aws_cost or 0.0), key))
            continue
            
        # Classify workload
        it = itype.lower()
        ram = spec.get("ram_gb", 0)
        w_class = "General-Purpose"

        if ram > 1536 or it.startswith(("u-", "hpc-")):
            w_class = "Outlier"
        elif it.startswith(("inf", "trn", "dl1", "dl2", "f1", "f2", "vt1")):
            w_class = "GPU"
        elif it.startswith(("g", "p")) and not it.startswith(("gd", "pd", "gp", "gl")):
            if len(it) > 1 and it[1].isdigit():
                w_class = "GPU"
        elif it.startswith("t"):
            w_class = "Burstable"
        elif spec.get("arch") == "arm64" or "graviton" in it or (len(it) > 2 and it[2] == 'g'):
            w_class = "ARM"
        elif it.startswith(("r", "x", "z")):
            w_class = "Memory-Optimized"
        elif it.startswith("c"):
            w_class = "Compute-Optimized"
            
        instance_count = round(total_usage / (b_days * 24), 4) if b_days else None
        rate = (aws_cost / total_usage) if (total_usage and total_usage > 0) else None
        resolved_updates.append((itype, spec.get("vcpus"), spec.get("ram_gb"), spec.get("arch"), b_days, instance_count, rate, w_class, key))

    if unresolved_specs:
        conn.executemany("UPDATE aws_li_catalog SET instance_type = ? WHERE aws_li_key = ?", [(t, k) for t, _, k in unresolved_specs])
    if resolved_updates:
        conn.executemany("""
            UPDATE aws_li_catalog 
            SET instance_type = ?, instance_vcpus = ?, instance_ram_gb = ?, instance_arch = ?,
                billing_days = ?, instance_count = ?, aws_effective_unit_rate = ?, workload_class = ?
            WHERE aws_li_key = ?
        """, resolved_updates)

    if unresolved_specs:
        unspec_usd = sum(cost for _, cost, _ in unresolved_specs)
        examples = sorted(list({t for t, _, _ in unresolved_specs}))[:20]
        diag(WARN, "instance_spec_unresolved", "ingest.py",
             f"{len(unresolved_specs)} row(s) had unresolved instance specs: {', '.join(examples)}",
             rows=len(unresolved_specs), usd=unspec_usd, phase=1,
             detail={"example_instance_types": examples})

    # Stamp billing_format on every row so downstream scripts know what
    # information was available without re-detecting it from column presence.
    fmt = "raw_cur" if is_raw_cur else "flat_csv"
    conn.execute("UPDATE aws_li_catalog SET billing_format = ?", (fmt,))

    # Faithfulness reference: the freshly-built catalog total (ex-tax) BEFORE the
    # materiality filter. Dedup is a cost-preserving GROUP BY SUM, so this equals
    # the faithful sum of every non-tax line item on the bill. The materiality
    # filter below deletes low-value rows and re-adds their sum as one row, so the
    # final catalog total must still equal this — asserted before "Ingest complete".
    _bill_reference_total = conn.execute(
        "SELECT COALESCE(SUM(aws_amortized_cost), 0.0) FROM aws_li_catalog"
    ).fetchone()[0]

    # ── Materiality filter ────────────────────────────────────────────────────
    # 1. Drop zero-cost rows. Negative costs are credits/refunds — always kept.
    zero_dropped = conn.execute(
        "SELECT COUNT(*) FROM aws_li_catalog WHERE aws_amortized_cost = 0"
    ).fetchone()[0]
    if zero_dropped:
        conn.execute("DELETE FROM aws_li_catalog WHERE aws_amortized_cost = 0")

    # 2. Drop low-materiality positive rows whose cumulative sum (sorted
    #    cheapest-first) stays within 1% of total positive spend. Greedily
    #    removing the cheapest rows guarantees total dropped cost < 1% of bill.
    #    Rows like $0.10–$0.50 that individually look trivial but whose sum
    #    is still < 1% are removed here; anything whose inclusion would push
    #    the running total past the 1% cap is kept.
    pre_filter = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(aws_amortized_cost) FILTER (WHERE aws_amortized_cost > 0), 0)"
        " FROM aws_li_catalog"
    ).fetchone()
    total_rows_pre, total_positive = pre_filter

    low_mat_dropped, low_mat_cost = 0, 0.0
    if total_positive > 0:
        # Identify rows to drop: cumulative sum (ASC) ≤ 1% of total positive spend.
        to_drop = conn.execute("""
            WITH low_mat AS (
                SELECT aws_li_key, aws_amortized_cost,
                       SUM(aws_amortized_cost) OVER (
                           ORDER BY aws_amortized_cost ASC
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cum_sum
                FROM aws_li_catalog
                WHERE aws_amortized_cost > 0
            )
            SELECT aws_li_key, aws_amortized_cost
            FROM low_mat
            WHERE cum_sum <= ?
        """, [total_positive * 0.01]).fetchall()

        if to_drop:
            low_mat_dropped = len(to_drop)
            low_mat_cost = sum(r[1] for r in to_drop)
            drop_keys = [r[0] for r in to_drop]
            # Use a temp table to avoid large IN-list for bills with many tiny rows.
            conn.execute(
                "CREATE TEMP TABLE _mat_drop AS SELECT unnest(?) AS k", [drop_keys]
            )
            conn.execute(
                "DELETE FROM aws_li_catalog WHERE aws_li_key IN (SELECT k FROM _mat_drop)"
            )
            conn.execute("DROP TABLE _mat_drop")

            # Reconcile: these rows are genuinely deleted from the catalog, so
            # their cost would otherwise silently vanish from every downstream
            # total (report totals, the Phase-4 reconciliation gate) — up to 1%
            # of the bill on a large one. Re-insert their sum as a single
            # synthetic line item rather than a bare log line, so the true bill
            # total is always recoverable from the catalog. classify_mechanics.py
            # routes this exact product name to non_workload -> passthrough at
            # cost parity (never invents a GCP price for it).
            import hashlib
            recon_key = hashlib.sha256(b"ingestion-materiality-adjustment").hexdigest()
            mat_bmonth_row = conn.execute("SELECT billing_month FROM aws_li_catalog WHERE billing_month IS NOT NULL AND billing_month != '' LIMIT 1").fetchone()
            mat_bmonth = mat_bmonth_row[0] if mat_bmonth_row else ''
            conn.execute(
                """
                INSERT INTO aws_li_catalog (
                    aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                    pricing_model, line_item_type, is_workload, total_usage,
                    aws_amortized_cost, projection_note, billing_format, billing_month
                ) VALUES (?, 'Ingestion Materiality Adjustment', 'Aggregate', 'Aggregate',
                          'global', 'global', 'OnDemand', 'Usage', false, 1, ?, ?, ?, ?)
                """,
                [recon_key, low_mat_cost,
                 f"Sum of {low_mat_dropped} line item(s) dropped by the ingestion "
                 f"materiality filter (each individually < 1% of bill cumulative) — "
                 f"re-added as one row so the bill total always reconciles.",
                 fmt, mat_bmonth],
            )

    total_dropped = zero_dropped + low_mat_dropped
    if total_dropped:
        pct = (low_mat_cost / total_positive * 100) if total_positive else 0
        log.info(
            f"Materiality filter: dropped {zero_dropped} zero-cost rows + "
            f"{low_mat_dropped} low-materiality rows "
            f"(${low_mat_cost:.2f} = {pct:.2f}% of bill); credits/refunds retained "
            f"as a single reconciliation row"
        )
        # Diag: materiality filter dropped rows
        if low_mat_dropped > 0:
            diag(WARN, "materiality_dropped", "ingest.py",
                 f"{low_mat_dropped} low-materiality row(s) dropped by ingestion materiality filter",
                 rows=low_mat_dropped, usd=low_mat_cost,
                 detail={"rows_dropped": low_mat_dropped,
                         "usd_dropped": low_mat_cost,
                         "pct_of_workload": round(pct, 2)})

    # ── Faithfulness invariants (typed, universal) ──────────────────────────────
    # These are the extraction layer's output guarantees. A violation means the
    # pipeline silently lost or invented spend — fail loudly rather than hand a
    # wrong bill to the mapping phase. _fail_direct writes failure.txt and exits
    # (no LLM retry): an empty or non-reconciling catalog is not a format the LLM
    # fallback can rescue.
    cat_rows, cat_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(aws_amortized_cost), 0.0) FROM aws_li_catalog"
    ).fetchone()

    # total_rows_pre is counted AFTER the zero-cost delete, so an all-zero bill
    # reads 0 there; add zero_dropped back to get "rows that actually parsed".
    err = check_faithfulness(cat_rows, cat_total, _bill_reference_total,
                             rows_before_materiality=(total_rows_pre or 0) + (zero_dropped or 0))
    if err:
        _fail_direct(err)

    log.info(
        f"Faithfulness OK: {cat_rows} catalog rows, total ${cat_total:,.2f} "
        f"reconciles to extracted bill total ${_bill_reference_total:,.2f}"
    )
    log.info(f"Ingest complete. billing_format={fmt!r}")
    try:
        conn.close()
    except Exception:
        pass

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"INGEST UNCAUGHT EXCEPTION: {tb}")
        sys.stderr.write(f"INGEST EXCEPTION: {tb}\n")
        sys.stderr.flush()
        _fail_direct(f"Ingestion failed with exception: {e}\n\nTraceback:\n{tb[-1200:]}")


