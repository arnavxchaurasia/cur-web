# Phase 1 — Ingestion

**Run by:** main agent. Mechanical script phase, no judgment calls.
**Reads:** the input bill file path.
**Writes:** `aws_raw`, `aws_li_catalog` in `projection-audit/projection.duckdb`.

## Execution

> **Before running anything, read [GUARDRAILS.md](../GUARDRAILS.md).**

All ingestion logic is embedded in this file. Write the embedded `ingest.py` code (see below) to a temp file and run it. No external scripts directory is needed.

```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - << 'PYEOF'
# paste the ingest.py code block (from "Embedded Scripts" section below) here
PYEOF
```

Check `failure.txt` after running the script. If the file exists and has content, it means the input bill was structurally unprocessable. In that case, stop and let the orchestrator handle the exit.

## Step 2 — Classify rows and produce the Phase 2 dispatch manifest

Once ingestion succeeds, run the mechanic-group classifier immediately. Write the embedded `classify_mechanics.py` code (see below) to a temp file and run it:

```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
export JOB_DIR="/path/to/job/directory"
python3 - << 'PYEOF'
# paste the classify_mechanics.py code block (from "Embedded Scripts" section below) here
PYEOF
```

This stamps a `mechanic_group` column on every row in `aws_li_catalog` and writes `projection-audit/phase2_manifest.json` — the file Phase 2 reads to dispatch parallel agents. The manifest lists every row (with its full field set) grouped by mechanic, so each Phase 2 agent only receives rows relevant to its specialty.

If the script prints a `WARNING: misc group is X% of total spend` line, note it but do not stop — Phase 5 will triage misc rows. If the script exits non-zero for any other reason, treat it as a fatal ingestion error.

The phase is complete once both scripts exit 0 and `projection-audit/phase2_manifest.json` exists.


---

## Embedded Scripts

### scripts/ingest.py

```python
# Source: originally scripts/ingest.py
#!/usr/bin/env python3
import duckdb
import os
import sys
import glob
import json
import re
import hashlib
SKILL_DIR = os.environ.get("SKILL_DIR", os.path.dirname(os.path.abspath(__file__)))
JOB_DIR = os.environ.get("JOB_DIR", os.getcwd())

# ── Inlined: log_utils ────────────────────────────────────────────────────────
import logging, traceback as _tb

_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"
_log_configured = False

def get_logger(name, job_dir=None):
    global _log_configured
    if not _log_configured:
        _configure_logging(job_dir or JOB_DIR)
        _log_configured = True
    return logging.getLogger(name)

def _configure_logging(jd):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    log_dir = os.path.join(jd, "projection-audit")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(log_dir, "debug.log"), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

def fatal(log, message, *, phase=None, exc=None, job_dir=None):
    jd = job_dir or JOB_DIR
    full_msg = f"{message}: {exc}" if exc else message
    log.critical(full_msg)
    if exc:
        log.debug(_tb.format_exc())
    pp = os.path.join(jd, "progress.json")
    try:
        import json as _j
        ex = {}
        if os.path.exists(pp):
            with open(pp) as f:
                ex = _j.load(f)
    except Exception:
        ex = {}
    ex.update({"status": "FAILED", "error": full_msg})
    if phase is not None:
        ex["phase"] = phase
    try:
        with open(pp, "w", encoding="utf-8") as f:
            _j.dump(ex, f, indent=2)
    except Exception as e:
        log.error(f"Could not write failure to progress.json: {e}")
    sys.exit(1)
# ── End log_utils ─────────────────────────────────────────────────────────────

# ── Inlined: region_prefix ────────────────────────────────────────────────────
import re as _re_rp

UT_PREFIX_TO_GCP: dict = {
    "aps1": "asia-southeast1",           # ap-southeast-1 → Singapore
    "aps2": "australia-southeast1",      # ap-southeast-2 → Sydney
    "aps3": "asia-south1",               # ap-south-1     → Mumbai
    "aps4": "australia-southeast2",      # ap-southeast-4 → Melbourne
    "aps5": "asia-south2",               # ap-south-2     → Delhi/Hyderabad
    "aps6": "asia-southeast2",           # ap-southeast-3 → Jakarta
    "apn1": "asia-northeast1",           # ap-northeast-1 → Tokyo
    "apn2": "asia-northeast3",           # ap-northeast-2 → Seoul
    "apn3": "asia-northeast2",           # ap-northeast-3 → Osaka
    "use1": "us-east4",                  # us-east-1      → N. Virginia
    "use2": "us-east4",                  # us-east-2      → Ohio
    "usw1": "us-west2",                  # us-west-1      → N. California
    "usw2": "us-west1",                  # us-west-2      → Oregon
    "usw3": "us-west2",                  # us-west-3      → Salt Lake City
    "usw4": "us-west1",                  # us-west-4      → Las Vegas
    "euw1": "europe-west1",              # eu-west-1      → Ireland
    "euw2": "europe-west2",              # eu-west-2      → London
    "euw3": "europe-west9",              # eu-west-3      → Paris
    "euw4": "europe-west4",              # eu-west-4      → Netherlands
    "eun1": "europe-north1",             # eu-north-1     → Stockholm
    "euc1": "europe-west3",              # eu-central-1   → Frankfurt
    "euc2": "europe-west4",              # eu-central-2   → Zurich
    "cac1": "northamerica-northeast1",   # ca-central-1   → Montreal
    "sae1": "southamerica-east1",        # sa-east-1      → Sao Paulo
    "mec1": "me-central1",              # me-central-1   → UAE
    "mes1": "me-west1",                  # me-south-1     → Bahrain
}
UT_PREFIX_RE = _re_rp.compile(r"^([a-z]{2,4}\d{1,2})-", _re_rp.IGNORECASE)

def decode_region_prefix(usage_type, fallback=None):
    """Return GCP region from usage_type prefix (e.g. APS3-EBS:... → asia-south1)."""
    m = UT_PREFIX_RE.match(usage_type or "")
    if not m:
        return fallback
    return UT_PREFIX_TO_GCP.get(m.group(1).lower(), fallback)
# ── End region_prefix ─────────────────────────────────────────────────────────

log = get_logger("ingest")

DB_PATH = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
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


def _reject_non_aws(cols, conn):
    """Detect clearly non-AWS files and fail early with a useful message."""
    cols_lower = {c.lower() for c in cols}
    # Azure billing exports (usage-based or EA portal)
    if any(k in cols_lower for k in ("subscriptionid", "subscription id", "meterid",
                                      "billingaccountid", "billingaccountname",
                                      "chargestart", "effectiveprice")):
        conn.execute("DROP TABLE IF EXISTS aws_raw")
        _fail_direct("This looks like an Azure billing export, not an AWS bill. "
                     "This tool projects AWS costs to GCP — please upload an AWS "
                     "Cost & Usage Report (CUR), Cost Explorer CSV, or estimated-bill PDF.")
    # VMware vCenter RVTools export
    if any(k in cols_lower for k in ("vm", "powerstate", "vcpu", "provisionedmb",
                                      "inusemb", "datacenter")):
        col_sample = [c for c in cols if c.lower() in ("vm", "powerstate", "vcpu")]
        if len(col_sample) >= 2:
            conn.execute("DROP TABLE IF EXISTS aws_raw")
            _fail_direct("This looks like a VMware/vCenter RVTools export, not an AWS bill. "
                         "Please upload an AWS Cost & Usage Report.")
    # OCI billing
    if any(k in cols_lower for k in ("tenancyname", "subscriptionid", "sku/partnumber",
                                      "product/compartmentname")):
        conn.execute("DROP TABLE IF EXISTS aws_raw")
        _fail_direct("This looks like an Oracle Cloud (OCI) billing export, not an AWS bill.")
    # GCP billing
    if any(k in cols_lower for k in ("billing_account_id", "service.description",
                                      "sku.description", "usage.amount_in_pricing_units")):
        conn.execute("DROP TABLE IF EXISTS aws_raw")
        _fail_direct("This looks like a GCP billing export, not an AWS bill.")


def _fail_direct(msg):
    """Write failure.txt and exit without attempting LLM fallback."""
    with open(os.path.join(JOB_DIR, "failure.txt"), "w") as f:
        f.write(msg)
    log.error(f"INGEST FAILURE: {msg}")
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
            os.execv(sys.executable, [sys.executable] + sys.argv)
            # execv replaces the process; line below never reached
    with open(os.path.join(JOB_DIR, "failure.txt"), "w") as f:
        f.write(msg)
    log.error(f"INGEST FAILURE: {msg}")
    sys.exit(1)


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
            [agy_bin, "--print-only", "--model", os.environ.get("AGY_MODEL", "gemini-3.5-flash")],
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


def _load_excel(conn, path):
    """Excel → aws_raw via DuckDB excel extension, spatial fallback, then openpyxl."""
    for setup, query in (
        ("INSTALL excel; LOAD excel;", f"SELECT * FROM read_xlsx('{path}', all_varchar=true)"),
        ("INSTALL spatial; LOAD spatial;", f"SELECT * FROM st_read('{path}')"),
    ):
        try:
            conn.execute(setup)
            conn.execute(f"CREATE TABLE aws_raw AS {query}")
            return
        except Exception:
            conn.execute("DROP TABLE IF EXISTS aws_raw")

    # openpyxl fallback — handles files with corrupt styles.xml that DuckDB rejects
    try:
        import openpyxl, csv as _csv, tempfile
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False,
                                          newline="", encoding="utf-8")
        w = _csv.writer(tmp)
        for row in ws.iter_rows(values_only=True):
            w.writerow(["" if v is None else str(v) for v in row])
        tmp.close()
        conn.execute(f"CREATE TABLE aws_raw AS SELECT * FROM read_csv_auto('{tmp.name}', ALL_VARCHAR=TRUE, header=TRUE)")
        log.info(f"Excel: loaded via openpyxl fallback ({ws.max_row} rows)")
        return
    except Exception as e:
        conn.execute("DROP TABLE IF EXISTS aws_raw")
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

def _pdf_canonical_service(cur_service, desc):
    """Return the correct AWS service name for a PDF line item.
    EBS storage/IOPS descriptions bleed into the EC2 section header — remap them,
    but S3/RDS/EFS storage-class lines must NOT be caught by that (they price
    against a different GCP target than Persistent Disk)."""
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

    items = []            # (service, region, description, qty, cost)
    cur_service, cur_region = "", ""
    stated_pretax = 0.0   # sum of the PDF's own "Total pre-tax USD X" lines
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for raw in (page.extract_text() or "").split("\n"):
                    ln = raw.strip()
                    if not ln:
                        continue
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
                                      qty.replace(",", ""), amt.replace(",", "")))
                        continue
                    h = _PDF_HDR_RE.match(ln)
                    if h:
                        name = h.group(1).strip()
                        if _PDF_REGION_RE.match(name):
                            cur_region = name
                        elif len(name) > 3 and "total" not in name.lower():
                            cur_service = name
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
                      "0", f"{adj}"))
        log.info(f"PDF: reconciled gross ${gross:,.2f} -> stated pre-tax ${stated_pretax:,.2f} "
              f"(discount adjustment ${adj:,.2f})")

    tmp_csv = os.path.join(JOB_DIR, "input_from_pdf.csv")
    with open(tmp_csv, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["Service", "Region", "Custom Usage Type", "Description", "Usage Quantity", "Cost ($)"])
        w.writerows(items)
    log.info(f"PDF: extracted {len(items)} line items")
    conn.execute(f"CREATE TABLE aws_raw AS SELECT * FROM read_csv_auto('{tmp_csv}', ALL_VARCHAR=TRUE, header=TRUE)")


def _read_into_aws_raw(conn, path):
    """Load one data file into aws_raw, dispatching by extension. DuckDB's
    read_csv_auto transparently handles .gz / .zst and delimiter detection."""
    p = path.lower()
    if p.endswith(".parquet"):
        conn.execute(f"CREATE TABLE aws_raw AS SELECT * FROM read_parquet('{path}')")
    elif p.endswith((".json", ".jsonl", ".ndjson")):
        conn.execute(f"CREATE TABLE aws_raw AS SELECT * FROM read_json_auto('{path}')")
    elif p.endswith((".xlsx", ".xlsm", ".xls")):
        _load_excel(conn, path)
    elif p.endswith(".pdf"):
        _load_pdf(conn, path)
    else:  # .csv .tsv .txt and .gz/.zst variants of them
        conn.execute(f"CREATE TABLE aws_raw AS SELECT * FROM read_csv_auto('{path}', ALL_VARCHAR=TRUE)")


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
        with open(os.path.join(JOB_DIR, "failure.txt"), "w") as f:
            f.write(msg)
        log.error(f"INGEST FAILURE: {msg}")
        sys.exit(1)
    input_file = inputs[0]

    # 2. Load aws_raw — format-aware loader (CSV/TSV, gzip/zstd, Parquet, JSON,
    #    Excel, PDF, and ZIP archives of any of those).
    conn.execute("DROP TABLE IF EXISTS aws_raw")
    load_raw(conn, input_file)
        
    cols = [c[1] for c in conn.execute("PRAGMA table_info(aws_raw)").fetchall()]

    # Detect non-AWS files early with a clear message
    _reject_non_aws(cols, conn)

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
            billing_format VARCHAR
        )
    """)

    # Reconcile sum helper
    if is_raw_cur:
        cost_col = c(['lineitem/unblendedcost', 'unblendedcost'])
        if cost_col == "NULL": cost_col = c(['lineitem/blendedcost'])
    else:
        cost_col = c(['cost_($)', 'cost'])

    # Build SQL to group and classify
    if is_raw_cur:
        sql = f"""
            WITH _raw_flat AS (
                SELECT
                    COALESCE({c(['lineitem/productcode', 'productcode'])}, '') as product,
                    COALESCE({c(['lineitem/usagetype', 'usagetype'])}, '') as usage_type,
                    COALESCE({c(['lineitem/operation', 'operation'])}, '') as operation,
                    COALESCE({c(['product/region', 'region'])}, '') as aws_region,
                    COALESCE({c(['pricing/term', 'term'])}, 'OnDemand') as pricing_model,
                    COALESCE({c(['lineitem/lineitemtype', 'lineitemtype'])}, '') as line_item_type,
                    CASE 
                        WHEN {c(['lineitem/lineitemtype', 'lineitemtype'])} IN ('Tax','RIFee','SavingsPlanUpfrontFee','SavingsPlanRecurringFee','SavingsPlanNegation','SavingsPlanCoveredUsage','Refund','Credit','EdpDiscount','PrivateRateDiscount','BundledDiscount') THEN FALSE
                        WHEN {c(['lineitem/productcode', 'productcode'])} IN ('AWSMarketplace', 'AWSSupport', 'Support')
                          OR {c(['lineitem/productcode', 'productcode'])} ILIKE '%Marketplace%'
                          OR {c(['lineitem/productcode', 'productcode'])} ILIKE '%AWSMP%'
                          OR {c(['lineitem/productcode', 'productcode'])} ILIKE '%Support%'
                          OR {c(['lineitem/lineitemtype', 'lineitemtype'])} ILIKE '%Marketplace%' THEN FALSE
                        WHEN {c(['lineitem/lineitemtype', 'lineitemtype'])} IN ('Usage','DiscountedUsage') THEN TRUE
                        ELSE FALSE
                    END as is_workload,
                    CAST(COALESCE({c(['lineitem/usageamount', 'usageamount'])}, '0') AS DOUBLE) as row_usage,
                    CAST(COALESCE({cost_col}, '0') AS DOUBLE) as row_cost,
                    COALESCE({c(['product/licensemodel', 'licensemodel'])}, '') as license_model,
                    COALESCE({c(['product/operatingsystem', 'operatingsystem'])}, '') as operating_system,
                    COALESCE({c(['product/databaseengine', 'databaseengine'])}, '') as database_engine,
                    COALESCE({c(['product/deploymentoption', 'deploymentoption'])}, '') as deployment_option,
                    COALESCE({c(['product/volumetype', 'volumetype'])}, '') as volume_type,
                    COALESCE({c(['pricing/unit', 'unit'])}, '') as pricing_unit
                FROM aws_raw
                WHERE {c(['lineitem/lineitemtype', 'lineitemtype'])} != 'Tax'
            )
            INSERT INTO aws_li_catalog (
                aws_li_key, product, usage_type, operation, aws_region, gcp_region, 
                pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit
            )
            SELECT 
                md5(product || usage_type || operation || aws_region || pricing_model || line_item_type || CAST(is_workload AS VARCHAR) || license_model || operating_system || database_engine || deployment_option || volume_type || pricing_unit) as key,
                product, usage_type, operation, aws_region, NULL as gcp_region, pricing_model, line_item_type, is_workload,
                SUM(row_usage) as total_usage,
                SUM(row_cost) as aws_amortized_cost,
                license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit
            FROM _raw_flat
            GROUP BY product, usage_type, operation, aws_region, pricing_model, line_item_type, is_workload,
                     license_model, operating_system, database_engine, deployment_option, volume_type, pricing_unit
        """
        conn.execute(sql)
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

            sql = f"""
                INSERT INTO aws_li_catalog (
                    aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                    pricing_model, line_item_type, is_workload,
                    total_usage, aws_amortized_cost, pricing_unit
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
                        CASE
                            WHEN TRY_CAST({cost_col_raw} AS DOUBLE) < 0 THEN 'Credit'
                            WHEN {cat_col} ILIKE '%Savings Plans%'        THEN 'SavingsPlanRecurringFee'
                            WHEN {meter_col} ILIKE '%reserved%' OR {meter_col} ILIKE '%DiscountedUsage%'
                                THEN 'DiscountedUsage'
                            ELSE 'Usage'
                        END                            AS line_item_type,
                        CASE
                            WHEN {cat_col} ILIKE '%support%'
                              OR {cat_col} ILIKE '%marketplace%'
                              OR {cat_col} ILIKE '%AWSMP%'
                              OR {cat_col} ILIKE '%tax%'
                              OR {cat_col} ILIKE '%Savings Plans%'
                              OR {cat_col} ILIKE '%Registrar%'
                              THEN FALSE
                            ELSE TRUE
                        END                            AS is_workload,
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
                        END                            AS pricing_unit
                    FROM aws_raw
                    WHERE COALESCE(TRY_CAST({cost_col_raw} AS DOUBLE), 0) != 0
                )
                SELECT
                    md5(product || usage_type || operation || aws_region || pricing_model || line_item_type || CAST(is_workload AS VARCHAR)) AS aws_li_key,
                    product, usage_type, operation, aws_region,
                    NULL AS gcp_region,
                    pricing_model, line_item_type, is_workload,
                    SUM(total_usage)  AS total_usage,
                    SUM(row_cost)     AS aws_amortized_cost,
                    pricing_unit
                FROM _raw
                GROUP BY product, usage_type, operation, aws_region,
                         pricing_model, line_item_type, is_workload, pricing_unit
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
                    aws_li_key = hashlib.md5(f"Tax/OtherTax{month}".encode('utf-8')).hexdigest()
                    group_key = ("Tax/Other", "Tax", "Tax", "global", "OnDemand", "Tax", False, None, "")
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
                    
                total_weight = 0.0
                weights = []
                for val, unit in usages:
                    u_lower = unit.lower()
                    weight = 1.0
                    if service == "Amazon EC2":
                        if "hr" in u_lower: weight = 8.5
                        elif "gb" in u_lower: weight = 1.5
                    elif service == "Amazon RDS":
                        if "acu" in u_lower: weight = 8.0
                        elif "gb" in u_lower: weight = 1.5
                        elif "io" in u_lower: weight = 0.5
                    elif service == "Amazon S3":
                        if "hr" in u_lower: weight = 4.0
                        elif "gb" in u_lower: weight = 4.0
                        else: weight = 0.2
                    elif service == "Amazon VPC":
                        if "hr" in u_lower: weight = 8.5
                        elif "gb" in u_lower: weight = 1.5
                    elif service == "AWS KMS":
                        if "key" in u_lower: weight = 9.0
                        elif "request" in u_lower: weight = 1.0
                    elif service == "Amazon Route 53":
                        if "zone" in u_lower: weight = 6.0
                        elif "quer" in u_lower: weight = 4.0
                    weights.append(weight)
                    total_weight += weight
                    
                for i, (val, unit) in enumerate(usages):
                    w = weights[i] / total_weight if total_weight > 0 else (1.0 / len(usages))
                    allocated_cost = row_cost * w
                    
                    prod_mapped = service
                    ut_mapped = unit
                    op_mapped = unit
                    p_unit = unit
                    is_wl = True
                    pm = "OnDemand"
                    lit = "Usage"
                    proj_note = None
                    
                    if "gb-month" in unit.lower() or "gb-mo" in unit.lower():
                        p_unit = "GB-Mo"
                    elif "hr" in unit.lower():
                        p_unit = "Hrs"
                    elif "request" in unit.lower():
                        p_unit = "Requests"
                    elif "quer" in unit.lower():
                        p_unit = "Queries"
                        
                    if service == "Amazon EC2":
                        prod_mapped = "Amazon Elastic Compute Cloud"
                        if "hr" in unit.lower():
                            ut_mapped = "BoxUsage:t3.medium"
                            op_mapped = "BoxUsage:t3.medium"
                            p_unit = "Hrs"
                        elif "gb" in unit.lower():
                            prod_mapped = "Amazon Elastic Block Store"
                            ut_mapped = "EBS:VolumeUsage.gp3"
                            op_mapped = "gp3 volume storage"
                            p_unit = "GB-Mo"
                    elif service == "Amazon RDS":
                        if "acu" in unit.lower():
                            prod_mapped = "Amazon Aurora"
                            ut_mapped = "ACU-Hrs"
                            op_mapped = "Aurora Serverless v2"
                            p_unit = "ACU-Hrs"
                        elif "gb" in unit.lower():
                            prod_mapped = "Amazon Relational Database Service"
                            ut_mapped = "RDS:GP2-Storage"
                            op_mapped = "GP2 Storage"
                            p_unit = "GB-Mo"
                        elif "io" in unit.lower():
                            prod_mapped = "Amazon Relational Database Service"
                            ut_mapped = "RDS:StorageIOPS"
                            op_mapped = "Storage IOPS - million I/O requests"
                            p_unit = "IOs"
                    elif service == "Amazon S3":
                        if "gb" in unit.lower() or "gb-month" in unit.lower():
                            prod_mapped = "Amazon Simple Storage Service"
                            ut_mapped = "TimedStorage-ByteHrs"
                            op_mapped = "StandardStorage"
                            p_unit = "GB-Mo"
                        elif "secret" in unit.lower():
                            prod_mapped = "AWS Secrets Manager"
                            ut_mapped = "SecretsManager"
                            op_mapped = "Secrets"
                            p_unit = "Secrets"
                        elif "request" in unit.lower():
                            prod_mapped = "Amazon Simple Storage Service"
                            ut_mapped = "APIRequests"
                            op_mapped = "PutRequests"
                            p_unit = "Requests"
                        elif "acu" in unit.lower():
                            prod_mapped = "Amazon Aurora"
                            ut_mapped = "ACU-Hrs"
                            op_mapped = "Aurora Serverless v2"
                            p_unit = "ACU-Hrs"
                        elif "obj" in unit.lower():
                            prod_mapped = "Amazon Simple Storage Service"
                            ut_mapped = "Obj-Month"
                            op_mapped = "Object Storage"
                            p_unit = "GB-Mo"
                        elif "hr" in unit.lower():
                            prod_mapped = "Amazon Elastic Compute Cloud"
                            ut_mapped = "BoxUsage:t3.medium"
                            op_mapped = "BoxUsage:t3.medium"
                            p_unit = "Hrs"
                        elif "lambda-gb" in unit.lower():
                            prod_mapped = "AWS Lambda"
                            ut_mapped = "Lambda-GB-Second"
                            op_mapped = "Lambda-GB-Second"
                            p_unit = "GB-Mo"
                        elif "invoc" in unit.lower():
                            prod_mapped = "AWS Lambda"
                            ut_mapped = "Lambda"
                            op_mapped = "Invocations"
                            p_unit = "Requests"
                        elif "page" in unit.lower():
                            prod_mapped = "Other"
                            ut_mapped = "Pages"
                            op_mapped = "Pages"
                            p_unit = "Pages"
                    elif service == "Amazon VPC":
                        prod_mapped = "Amazon Elastic Compute Cloud"
                        if "gb" in unit.lower():
                            ut_mapped = "NatGateway-Bytes"
                            op_mapped = "NatGateway-Bytes"
                            p_unit = "GB"
                        elif "hr" in unit.lower():
                            ut_mapped = "NatGateway-Hours"
                            op_mapped = "NatGateway-Hours"
                            p_unit = "Hrs"
                    elif service == "Amazon EKS":
                        prod_mapped = "Amazon Elastic Container Service for Kubernetes"
                        ut_mapped = "EKS-Hours"
                        op_mapped = "EKS-Hours"
                        p_unit = "Hrs"
                    elif service == "Amazon Bedrock":
                        prod_mapped = "Amazon Bedrock"
                        ut_mapped = "Bedrock"
                        op_mapped = "Bedrock"
                        p_unit = "Units"
                    elif service == "AWS KMS":
                        prod_mapped = "AWS Key Management Service"
                        if "key" in unit.lower():
                            ut_mapped = "KMS"
                            op_mapped = "Keys"
                            p_unit = "Keys"
                        elif "request" in unit.lower():
                            ut_mapped = "KMS"
                            op_mapped = "Requests"
                            p_unit = "Requests"
                    elif service == "Amazon Route 53":
                        prod_mapped = "Amazon Route 53"
                        if "zone" in unit.lower():
                            ut_mapped = "Route53-HostedZone"
                            op_mapped = "HostedZone"
                            p_unit = "HostedZones"
                        elif "quer" in unit.lower():
                            ut_mapped = "Route53-Queries"
                            op_mapped = "Queries"
                            p_unit = "Queries"
                    elif service == "Elastic Load Balancing":
                        prod_mapped = "Elastic Load Balancing"
                        ut_mapped = "LoadBalancerUsage"
                        op_mapped = "LCU-Hrs"
                        p_unit = "LCU-Hrs"
                    elif service == "USE1-APIRequest":
                        prod_mapped = "Other"
                        ut_mapped = "APIRequest"
                        op_mapped = "Requests"
                        p_unit = "Requests"
                    
                    group_key = (prod_mapped, ut_mapped, op_mapped, "global", pm, lit, is_wl, proj_note, p_unit)
                    if group_key not in aggregated:
                        aggregated[group_key] = {"total_usage": 0.0, "row_cost": 0.0}
                    aggregated[group_key]["total_usage"] += val
                    aggregated[group_key]["row_cost"] += allocated_cost

            for gk, val in aggregated.items():
                prod, ut, op, aws_reg, pm, lit, is_wl, proj_note, p_unit = gk
                cost = max(val["row_cost"], 0.001) if val["total_usage"] > 0 else 0.0
                
                # Must hash ALL fields of group_key, not a subset — otherwise two
                # distinct aggregation buckets that differ only in a field left out
                # here (e.g. projection_note or pricing_unit) collide onto the same
                # aws_li_key and the INSERT below throws a primary-key violation.
                key_str = f"{prod}{ut}{op}{aws_reg}{pm}{lit}{str(is_wl)}{proj_note}{p_unit}"
                aws_li_key = hashlib.md5(key_str.encode('utf-8')).hexdigest()
                
                conn.execute("""
                    INSERT INTO aws_li_catalog (
                        aws_li_key, product, usage_type, operation, aws_region, gcp_region, 
                        pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                        projection_note, pricing_unit
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """, (aws_li_key, prod, ut, op, aws_reg, pm, lit, is_wl, val["total_usage"], cost, proj_note, p_unit))
            log.info("Loaded custom flat CSV with semicolon-separated Usage (with Units)")

        # Check if "service" column exists in aws_raw
        elif c(['service']) == 'NULL':
            # Run the Python-based ingestion/mapping for the 4-column flat CSV
            import hashlib
            raw_rows = conn.execute("SELECT * FROM aws_raw").fetchall()
            # Fetch column names
            col_names = [col[0] for col in conn.description]
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
                raise SystemExit(
                    "Unrecognized bill format: this file doesn't match AWS CUR, the AWS "
                    "PDF-export layout, the Category/Meter/Cost spreadsheet layout, or the "
                    f"flat-CSV layout (missing column(s): {', '.join(_missing)}). "
                    f"Actual columns found: {col_names}. "
                    "This tool projects AWS billing data to GCP — verify the uploaded file "
                    "is an AWS billing export, not another cloud provider's."
                )
            desc_idx = col_names.index("Description")
            region_idx = col_names.index("Region") if "Region" in col_names else None
            usage_idx = col_names.index("Usage Quantity")
            cost_idx = col_names.index("Amount in USD")

            # Temporary storage to aggregate rows by the same key
            aggregated = {}

            for row in raw_rows:
                desc = row[desc_idx]
                region = row[region_idx] if region_idx is not None else ""
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

                line_item_type = "DiscountedUsage" if is_reserved else "Usage"
                
                product = ""
                usage_type = ""
                
                if "elastic compute" in desc_lower or desc_lower.startswith("ec2") or "natgateway" in desc_lower:
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
                    usage_type = "EMR"
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
                    product = "Other"
                    usage_type = "Other"
                    
                is_workload = True
                if any(k in desc_lower for k in [
                    "covered by compute savings plans",
                    "covered by ec2 instance savings plans",
                    "covered by reserved instances",
                    "committed", "upfront", "no upfront fee",
                    "recurring monthly fee", "edp discount",
                    "enterprise discount program", "private pricing discount",
                    "private rate", "solution provider", "bundled discount",
                    "refund", "credit", "marketplace", "awsmp", "support",
                    "aws support", "tax", "late fee", "ocb late fee"
                ]) or any(k in product.lower() for k in ["marketplace", "awsmp", "support", "tax"]):
                    is_workload = False
                    
                projection_note = None
                if "reserved instance applied" in desc_lower:
                    projection_note = 'AWS rate is RI-amortized; compare GCP CUD, not OD'
                    
                group_key = (product, usage_type, desc, region, pricing_model, line_item_type, is_workload, projection_note, pricing_unit)
                if group_key not in aggregated:
                    aggregated[group_key] = {"total_usage": 0.0, "row_cost": 0.0}
                aggregated[group_key]["total_usage"] += total_usage
                aggregated[group_key]["row_cost"] += row_cost
                
            # Insert aggregated rows
            for gk, val in aggregated.items():
                prod, ut, op, aws_reg, pm, lit, is_wl, proj_note, p_unit = gk
                # Must hash ALL fields of group_key, not a subset — otherwise two
                # distinct aggregation buckets that differ only in a field left out
                # here (e.g. projection_note or pricing_unit) collide onto the same
                # aws_li_key and the INSERT below throws a primary-key violation.
                key_str = f"{prod}{ut}{op}{aws_reg}{pm}{lit}{str(is_wl)}{proj_note}{p_unit}"
                aws_li_key = hashlib.md5(key_str.encode('utf-8')).hexdigest()
                
                conn.execute("""
                    INSERT INTO aws_li_catalog (
                        aws_li_key, product, usage_type, operation, aws_region, gcp_region, 
                        pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost,
                        projection_note, pricing_unit
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """, (aws_li_key, prod, ut, op, aws_reg, pm, lit, is_wl, val["total_usage"], val["row_cost"], proj_note, p_unit))
        else:
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
                        CASE
                            WHEN {c(['service'])} ILIKE '%Spot%' OR {c(['description'])} ILIKE '%Spot Instance%' THEN 'Spot'
                            WHEN {c(['description'])} ILIKE '%reserved instance applied%'
                              OR {c(['description'])} ILIKE '%reserved instances%'
                              OR {c(['service'])} ILIKE '%reserved instances%' THEN 'Committed'
                            ELSE 'OnDemand'
                        END AS pricing_model,
                        CASE WHEN {c(['description'])} ILIKE '%reserved instance applied%'
                              OR {c(['description'])} ILIKE '%reserved instances%'
                              OR {c(['service'])} ILIKE '%reserved instances%'
                             THEN 'DiscountedUsage' ELSE 'Usage' END AS line_item_type,
                        CASE
                            WHEN {c(['description'])} ILIKE '%covered by Compute Savings Plans%'
                              OR {c(['description'])} ILIKE '%covered by EC2 Instance Savings Plans%'
                              OR {c(['description'])} ILIKE '%covered by Reserved Instances%'
                              OR {c(['description'])} ILIKE '%committed%upfront%'
                              OR {c(['description'])} ILIKE '%No upfront fee%'
                              OR {c(['description'])} ILIKE '%Recurring monthly fee%'
                              OR {c(['description'])} ILIKE '%EDP Discount%'
                              OR {c(['description'])} ILIKE '%Enterprise Discount Program%'
                              OR {c(['service'])} ILIKE '%Enterprise Discount Program%'
                              OR {c(['description'])} ILIKE '%Private Pricing Discount%'
                              OR {c(['description'])} ILIKE '%Private Rate%'
                              OR {c(['description'])} ILIKE '%Solution Provider%'
                              OR {c(['description'])} ILIKE '%Bundled Discount%'
                              OR {c(['description'])} ILIKE '%Refund%'
                              OR {c(['description'])} ILIKE '%Credit%'
                              OR {c(['service'])} ILIKE '%Marketplace%'
                              OR {c(['service'])} ILIKE '%AWSMP%'
                              OR {c(['service'])} ILIKE '%Support%'
                              OR {c(['description'])} ILIKE '%Marketplace%'
                              OR {c(['description'])} ILIKE '%AWS Support%' THEN FALSE
                            ELSE TRUE
                        END AS is_workload,
                        CAST(REPLACE(COALESCE({c(['usage_quantity'])}, '0'), ',', '') AS DOUBLE) AS row_usage,
                        CAST(REPLACE(COALESCE({cost_col}, '0'), ',', '')              AS DOUBLE) AS row_cost,
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
                INSERT INTO aws_li_catalog (aws_li_key, product, usage_type, operation, aws_region, gcp_region, pricing_model, line_item_type, is_workload, total_usage, aws_amortized_cost, projection_note)
                SELECT
                    md5(product || usage_type || operation || aws_region ||
                        CASE WHEN operation ILIKE '%reserved instance applied%' THEN 'Committed' ELSE 'OnDemand' END ||
                        line_item_type ||
                        CAST(is_workload AS VARCHAR)
                    ) AS aws_li_key,
                    product, usage_type, operation, aws_region,
                    NULL AS gcp_region,
                    pricing_model, line_item_type, is_workload,
                    SUM(row_usage)  AS total_usage,
                    SUM(row_cost)   AS aws_amortized_cost,
                    projection_note
                FROM _flat
                GROUP BY product, usage_type, operation, aws_region, pricing_model, line_item_type, is_workload, projection_note
            """
            conn.execute(sql)
    
    # 5. Map Regions
    catalog_rows = conn.execute("SELECT aws_li_key, aws_region, usage_type FROM aws_li_catalog").fetchall()
    for row in catalog_rows:
        key = row[0]
        aws_r = row[1]
        # Unknown regions fall back to 'global' so the gcp_projection VIEW's
        # COALESCE(regional_rate, global_rate) always finds a rate.
        aws_r_clean = aws_r.strip().lower() if aws_r else ""
        gcp_r = REGION_MAP.get(aws_r_clean) or DESCRIPTIVE_REGION_MAP.get(aws_r_clean, "global")
        # PDF/flat-CSV bills often omit a Region column entirely, leaving every
        # row on "global" — but usage_type frequently embeds the real region as
        # a prefix (e.g. "APS3-EBS:VolumeUsage.gp3"). Decoding this ONCE here,
        # for every row regardless of which mapper it later routes through,
        # replaces the previous per-mapper copy-pasted decode calls that only
        # covered EBS/MSK/EC2 rows and silently left Lambda/S3/etc. on 'global'
        # — which then breaks description-based SKU resolution downstream,
        # since most GCP catalog descriptions embed a literal region name with
        # no 'global' variant to fall back to.
        if gcp_r == "global":
            gcp_r = decode_region_prefix(row[2], gcp_r)
        conn.execute("UPDATE aws_li_catalog SET gcp_region = ? WHERE aws_li_key = ?", (gcp_r, key))
            
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
    
    for r in all_cat:
        key, total_usage, product, operation, aws_cost, usage_type, lic, os_name, db_eng, deploy_opt, vol_t, p_unit = r
        itype = None
        op = operation or ""
        ut = usage_type or ""
        
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
        
        if "RDS" in product or "Aurora" in product or "Relational Database" in product:
            m = re.search(r'(db\.[a-z0-9]+\.[a-z0-9]+)', op, re.IGNORECASE)
            if m: itype = m.group(1).lower()
        elif "ElastiCache" in product:
            m = re.search(r'(cache\.[a-z0-9]+\.[a-z0-9]+)', op, re.IGNORECASE)
            if m:
                itype = m.group(1).lower()
            else:
                m2 = re.search(r'([A-Z][0-9][A-Za-z0-9]*)\s+(Micro|Small|Medium|Large|XLarge|[0-9]+XLarge)\s+Cache',
                               op, re.IGNORECASE)
                if m2:
                    fam  = m2.group(1).lower()
                    size = m2.group(2).lower()
                    itype = f"cache.{fam}.{size}"
        elif "OpenSearch" in product or "Elasticsearch" in product:
            m = re.search(r'([a-z0-9]+\.[a-z0-9]+\.search)', op, re.IGNORECASE)
            if m: itype = m.group(1).lower()
        elif "Managed Streaming for Apache Kafka" in product or "MSK" in product:
            m = re.search(r'(kafka\.[a-z0-9]+\.[a-z0-9]+)', op, re.IGNORECASE)
            if m: itype = m.group(1).lower()
        elif "Elastic Compute Cloud" in product or "EC2" in product:
            # Match the instance-type SHAPE (family+gen+size, e.g. "t2.xlarge",
            # "c7gd.4xlarge") anywhere in the operation string, rather than
            # assuming it immediately precedes "Instance Hour" — PDF-format Spot
            # rows read "c7gd.4xlarge Linux/UNIX Spot Instance-hour in ..." (other
            # words + a hyphen in between), which the old positional regex missed
            # entirely, leaving instance_type NULL and losing deterministic
            # core+RAM (and Spot-discount) pricing for every Spot row.
            m = re.search(r'\b([a-z][0-9][a-z0-9]*\.[a-z0-9]+)\b', op, re.IGNORECASE)
            if m:
                itype = m.group(1).lower()
            else:
                m = re.search(r'(?:BoxUsage|SpotUsage):(\S+)', op)
                if m: itype = m.group(1)
                
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
        spec = table.get(base_itype) or ec2_table.get(base_itype)
        if not spec:
            # Fall back to legacy static spec lookup (decorated or base name)
            spec = LEGACY_SPECS.get(itype) or LEGACY_SPECS.get(base_itype)
            
        if not spec:
            conn.execute("UPDATE aws_li_catalog SET instance_type = ? WHERE aws_li_key = ?", (itype, key))
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
        
        conn.execute("""
            UPDATE aws_li_catalog 
            SET instance_type = ?, instance_vcpus = ?, instance_ram_gb = ?, instance_arch = ?,
                billing_days = ?, instance_count = ?, aws_effective_unit_rate = ?, workload_class = ?
            WHERE aws_li_key = ?
        """, (itype, spec.get("vcpus"), spec.get("ram_gb"), spec.get("arch"), b_days, instance_count, rate, w_class, key))

    # Stamp billing_format on every row so downstream scripts know what
    # information was available without re-detecting it from column presence.
    fmt = "raw_cur" if is_raw_cur else "flat_csv"
    conn.execute("UPDATE aws_li_catalog SET billing_format = ?", (fmt,))

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
            conn.execute(
                """
                INSERT INTO aws_li_catalog (
                    aws_li_key, product, usage_type, operation, aws_region, gcp_region,
                    pricing_model, line_item_type, is_workload, total_usage,
                    aws_amortized_cost, projection_note, billing_format
                ) VALUES (?, 'Ingestion Materiality Adjustment', 'Aggregate', 'Aggregate',
                          'global', 'global', 'OnDemand', 'Usage', false, 1, ?, ?, ?)
                """,
                [recon_key, low_mat_cost,
                 f"Sum of {low_mat_dropped} line item(s) dropped by the ingestion "
                 f"materiality filter (each individually < 1% of bill cumulative) — "
                 f"re-added as one row so the bill total always reconciles.",
                 fmt],
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

    log.info(f"Ingest complete. billing_format={fmt!r}")

if __name__ == "__main__":
    main()


```

### scripts/classify_mechanics.py

```python
# Source: originally scripts/classify_mechanics.py
#!/usr/bin/env python3
from __future__ import annotations
"""
classify_mechanics.py — stamp each row in aws_li_catalog with mechanic_group.

Usage:
    python3 classify_mechanics.py <projection.duckdb>

Rules applied in order (first match wins):
  1. compute_windows  (Windows EC2 — static mapper handles core+RAM+license)
  2. compute_arm      (Graviton/ARM EC2 — C4A where available, N2D fallback via catalog)
  3. compute_breakdown
  4. managed_db
  5. block_storage
  6. data_transfer
  7. flat_hourly
  8. per_request
  9. object_storage
  10. commitment_discount
  11. misc (fallback)

Exits 0 on success. Prints WARNING if misc > 15% of total aws_amortized_cost.
"""

import json
import sys
import re
import os
import duckdb

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
    except FileNotFoundError:
        _logging_cfg.getLogger("config_loader").warning("config_loader: %s not found", path)
        return {}
    except Exception as e:
        _logging_cfg.getLogger("config_loader").warning("config_loader: failed to load %s: %s", path, e)
        return {}
# alias to match original usage
_cfg = _load_data_config
# ── End config_loader ─────────────────────────────────────────────────────────

_svc_cfg = _cfg("service-classification")

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

def _ilike(value: str | None, pattern: str) -> bool:
    """Case-insensitive substring match (SQL ILIKE '%pattern%' semantics)."""
    if value is None:
        return False
    return pattern.lower() in value.lower()

def _re(value: str | None, pattern: str) -> bool:
    if value is None:
        return False
    return bool(re.search(pattern, value, re.IGNORECASE))

# Accelerator / specialized-silicon families: AI inference/training chips
# (Inferentia inf*, Trainium trn*, Habana dl*) and GPU families (p*, g*, vt*).
# These must NOT be mapped to a general-purpose CPU VM (N2D) — that hides the
# architectural change and mis-prices badly. They are routed to misc for a
# passthrough + manual-review verdict instead.
def _is_accelerator(row) -> bool:
    txt = f"{row.get('instance_type') or ''} {row.get('operation') or ''}".lower()
    if "inferentia" in txt or "trainium" in txt:
        return True
    return bool(re.search(r'\b(inf\d|trn\d|dl\d|p[2-5]|g[3-6]|vt\d)[a-z0-9]*\.', txt))

def _has_instance_type(text: str | None) -> bool:
    if not text:
        return False
    return bool(re.search(r'\b(?:db\.|cache\.|kafka\.)?[a-z]\d[a-z0-9]*\.[a-z0-9]+\b', text, re.IGNORECASE))

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
            r["line_item_type"] in ("RIFee", "SavingsPlanRecurringFee", "EdpDiscount")
            or r["pricing_model"] in ("Reserved", "SavingsPlan")
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
            or _re(r.get("usage_type", ""), r"CPUCredits?")
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
            and r["unit"] in ("Hrs", "hours", "")
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
            )
            and (
                _re(r.get("usage_type", ""), r"BoxUsage:t[234][ag]?\.")
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
            and r["unit"] in ("Hrs", "hours", "")
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
            )
            # Graviton families all contain 'g' in the family name (t4g, c6g, m7g…)
            and _re(r.get("usage_type", ""), r"BoxUsage:[a-z]\d[a-z0-9]*g[a-z0-9]*\.")
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and r["unit"] in ("Hrs", "hours", "")
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
            and (_re(r["usage_type"], r"BoxUsage|SpotUsage|ReservedInstances|running Linux")
                 or _re(r["operation"], r"Instance[\s-]hour|hourly fee per Linux/UNIX|per On Demand \w+.*Instance"))
            # PDF/flat-CSV bills routinely leave pricing_unit/unit blank (this
            # gap is already documented and worked around elsewhere in this
            # file — managed_db, msk, block_storage) — without accepting "",
            # a genuine EC2/NAT/LB/EKS instance-hour row from one of those
            # formats would silently bypass every deterministic mapper this
            # rule feeds and fall through to the wrong classification entirely.
            and r["unit"] in ("Hrs", "hours", "")
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
            )
            # Aurora Serverless v2 bills in ACU-Hrs; include alongside instance Hrs.
            # ElastiCache excluded — has its own static elasticache handler below.
            # DiscountedUsage/SavingsPlanCoveredUsage rows are RI/SP-covered instance
            # hours — their pricing_unit can be NULL but they are still instance rows.
            and (
                r.get("unit") in ("Hrs", "hours", "Hour", "hrs", "ACU-Hrs", "ACU-hours")
                or r.get("line_item_type") in ("DiscountedUsage", "SavingsPlanCoveredUsage")
                # PDF/flat-CSV bills leave pricing_unit blank. Catch InstanceUsage and
                # RDS Proxy rows by usage_type pattern so they don't fall to block_storage
                # where they'd be priced as $/GiBy.mo × hours (10-20x inflation).
                or _re(r.get("usage_type", ""), r"InstanceUsage:|RDS:Proxy")
                # Aurora Serverless V2 PDF rows: usage_type="APS5-Aurora:ServerlessV2Usage"
                # has blank pricing_unit but is ACU billing — not GB storage.
                or _re(r.get("usage_type", ""), r"Aurora:ServerlessV2Usage")
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
        ),
    ),
    (
        "block_storage",
        lambda r: (
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
            # Generic "Storage" usage_type catch — exclusion list prevents misroutes:
            # S3/DynamoDB (own handlers), Lambda ephemeral storage (61x inflation
            # observed when priced as Cloud Storage), VPC (endpoint data ≠ storage),
            # SageMaker (service_map routes it to Vertex AI review).
            or (_re(r["usage_type"], r"Storage")
                and not _ilike(r["product"], "S3")
                and not _ilike(r["product"], "Simple Storage")
                and not _ilike(r["product"], "DynamoDB")
                and not _ilike(r["product"], "Lambda")
                and not _ilike(r["product"], "Virtual Private Cloud")
                and not _ilike(r["product"], "SageMaker"))
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
            and r["unit"] in ("Hrs", "hours", "")
            # Fargate rows use Hrs but are caught by per_request above
            and not _re(r.get("product", ""), r"[Ff]argate")
            and not _re(r.get("usage_type", ""), r"[Ff]argate")
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
                )
            )
            and not any(
                _ilike(r["product"], p) for p in (
                    "Cognito", "DynamoDB", "Simple Email",
                    "GuardDuty", "Security Hub",
                )
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
            )
            and _re(r.get("usage_type", ""), r"Kafka\.[a-z]")
            # PDF/flat-CSV bills leave pricing_unit blank (same gap acknowledged
            # elsewhere in this file, e.g. the block_storage exclusion above) —
            # a genuine MSK broker-hours row from one of those formats would fail
            # this positive check and get silently misrouted to the wrong
            # mechanic group. Accept blank alongside the explicit hour units.
            and r.get("pricing_unit", "").lower() in ("hrs", "hours", "hr", "")
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
        ),
    ),
    (
        # Athena → BigQuery on-demand. Athena bills per-TB-scanned; BigQuery on-demand
        # uses the same unit ($6.25/TB). Static mapper handles the conversion directly.
        "athena",
        lambda r: (
            _ilike(r["product"], "Athena")
            or _ilike(r["product"], "AmazonAthena")
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
            )
            and r.get("unit") in ("Hrs", "hours", "Shard-Hrs", "ShardHours")
        ),
    ),
    (
        # EMR management fee rows — caught before misc so they get a deterministic
        # Dataproc mapping rather than an LLM guess. The underlying EC2 cost for
        # EMR worker nodes comes through compute_breakdown (different product line).
        "emr",
        lambda r: (
            _ilike(r["product"], "Elastic MapReduce")
            or _ilike(r["product"], "AmazonEMR")
            or _ilike(r["product"], "Amazon EMR")
        ),
    ),
    (
        # Route 53 DNS query charges → Cloud DNS. PDF-format bills set unit=None
        # so this must match by product name, not unit. Route 53 and Cloud DNS
        # are at cost parity ($0.40/M for first 1B queries, $0.20/M thereafter).
        "per_request",
        lambda r: (
            _ilike(r["product"], "Route 53")
            or _re(r.get("usage_type", ""), r"Route53|DNS-Queries")
        ),
    ),
    (
        # AWS WAF per-request charges (standard WAF, BotControl, AntiDDoS-Request,
        # BotControl-Targeted-Request). PDF bills leave unit=None so we can't
        # rely on unit="Requests" — catch by product name before misc fallback.
        # The existing Cloud Armor mapping logic in map_per_request handles these.
        "per_request",
        lambda r: (
            _re(r.get("product", ""), r"AWS WAF")
            and not _re(r.get("usage_type", "") + r.get("operation", ""),
                        r"WebACL-Hour|Rule-Hour|Web ACL|WAF Rule|\bMonth\b")
        ),
    ),
    (
        # AWS WAF flat monthly fees (BotControl per-month, AntiDDoS per-month).
        # PDF bills leave unit=None; matched by product + "Month" in operation.
        "flat_hourly",
        lambda r: (
            _re(r.get("product", ""), r"AWS WAF")
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
                    (_re(r["product"], r"\bS3\b") or _ilike(r["product"], "Simple Storage"))
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
            )
        ),
    ),
]

def classify(row: dict) -> str:
    for group, test in RULES:
        try:
            if test(row):
                return group
        except Exception:
            pass
    return "misc"


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


def _misc_reason(row: dict) -> str:
    """Produce a structured reason dict for a misc-classified row."""
    product = row.get("product") or ""
    usage_type = row.get("usage_type") or ""
    unit = row.get("unit") or ""

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

    # --- Add column if missing ---
    existing_cols = {
        r[1].lower()
        for r in con.execute("PRAGMA table_info('aws_li_catalog')").fetchall()
    }
    if "mechanic_group" not in existing_cols:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN mechanic_group TEXT")
        print("Added column mechanic_group to aws_li_catalog.")

    # --- Load rows ---
    # billing_format may be absent in DBs ingested before this column was added
    bf_expr = "billing_format" if "billing_format" in existing_cols else "NULL AS billing_format"
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
            {bf_expr}
        FROM aws_li_catalog
        """
    ).fetchall()

    col_names = [
        "aws_li_key", "product", "usage_type", "operation",
        "unit", "line_item_type", "pricing_model", "aws_amortized_cost",
        "instance_type", "instance_vcpus", "instance_ram_gb", "instance_count",
        "billing_format",
    ]

    # --- Classify ---
    updates: list[tuple[str, str]] = []
    misc_reasons: dict[str, str] = {}   # aws_li_key → why it landed in misc
    for raw in rows:
        row = dict(zip(col_names, raw))
        group = classify(row)
        updates.append((group, row["aws_li_key"]))
        if group == "misc":
            misc_reasons[row["aws_li_key"]] = _misc_reason(row)

    # Bulk update
    con.executemany(
        "UPDATE aws_li_catalog SET mechanic_group = ? WHERE aws_li_key = ?",
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
    misc_rows: list[dict] = []
    for group, row_count, group_spend in stats:
        pct_rows = 100.0 * row_count / total_rows if total_rows else 0.0
        pct_spend = 100.0 * group_spend / total_spend if total_spend else 0.0
        print(f"{group:<25} {row_count:>8}  {pct_rows:>7.1f}%  {group_spend:>14,.2f}  {pct_spend:>7.1f}%")
        if group == "misc":
            misc_spend = group_spend

    print("-" * 70)
    print(f"{'TOTAL':<25} {total_rows:>8}  {'100.0%':>8}  {total_spend:>14,.2f}  {'100.0%':>8}")

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
    import os, collections
    manifest: dict[str, list] = collections.defaultdict(list)
    row_data_full = con.execute(
        """
        SELECT
            aws_li_key, mechanic_group, product, usage_type, operation,
            pricing_unit AS unit, line_item_type, pricing_model,
            aws_amortized_cost, instance_type, instance_vcpus,
            instance_ram_gb, instance_arch, workload_class,
            billing_days, instance_count, aws_effective_unit_rate,
            aws_region AS region, gcp_region, is_workload,
            operating_system, license_model, database_engine, deployment_option
        FROM aws_li_catalog
        ORDER BY mechanic_group, aws_amortized_cost DESC
        """
    ).fetchall()
    full_cols = [
        "aws_li_key", "mechanic_group", "product", "usage_type", "operation",
        "unit", "line_item_type", "pricing_model",
        "aws_amortized_cost", "instance_type", "instance_vcpus",
        "instance_ram_gb", "instance_arch", "workload_class",
        "billing_days", "instance_count", "aws_effective_unit_rate",
        "region", "gcp_region", "is_workload",
        "operating_system", "license_model", "database_engine", "deployment_option",
    ]
    for raw in row_data_full:
        row = dict(zip(full_cols, raw))
        if row["mechanic_group"] == "misc" and row["aws_li_key"] in misc_reasons:
            row["misc_annotation"] = json.loads(misc_reasons[row["aws_li_key"]])
        manifest[row["mechanic_group"]].append(row)

    # Groups resolved deterministically by apply_commitment_ignores.py /
    # apply_static_mappings.py — the LLM must NOT re-touch them, or it could
    # overwrite a stable deterministic mapping with a run-to-run-varying guess.
    skip_groups = {
        "commitment_discount", "negative_cost",
        "flat_hourly", "object_storage", "per_request",
        "block_storage", "data_transfer", "non_workload", "cloudwatch",
        "guardduty", "redshift", "athena", "kinesis", "efs", "xray", "fsx", "emr", "elasticache", "msk",
        # compute_windows/compute_arm/compute_burstable each have a dedicated static
        # handler in apply_static_mappings.py that always emits an output entry (map
        # or passthrough fallback) for every row — same shape as the other
        # deterministic groups above. Without this, these rows stayed in the LLM
        # manifest as needs_llm=true and got a SECOND, independent LLM mapping on
        # top of the deterministic one, which is what caused the same Windows/RHEL
        # SKU to produce a different ratio on every bill (whichever mapping the
        # merge step kept last), rather than the deterministic price being final.
        "compute_windows", "compute_arm", "compute_burstable",
    }
    # Only include groups the LLM actually needs to map. Static groups (flat_hourly,
    # object_storage, block_storage, etc.) are handled deterministically by
    # apply_static_mappings.py — serializing them into the manifest burns ~87K tokens
    # per run as the LLM reads each group and discovers there is nothing to do.
    llm_groups = {g for g in manifest if g not in skip_groups}
    static_groups = {g for g in manifest if g in skip_groups}

    def _drop_null_cols(rows: list) -> list:
        """Remove columns that are None for every row in the group (T3 token optimization)."""
        if not rows:
            return rows
        all_keys = set(rows[0].keys())
        null_cols = {k for k in all_keys if all(r.get(k) is None for r in rows)}
        if not null_cols:
            return rows
        return [{k: v for k, v in r.items() if k not in null_cols} for r in rows]

    manifest_out = {
        g: {"rows": _drop_null_cols(rows), "row_count": len(rows),
            "total_spend": sum(r.get("aws_amortized_cost") or 0 for r in rows),
            "needs_llm": True}
        for g, rows in manifest.items()
        if g in llm_groups
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

```
