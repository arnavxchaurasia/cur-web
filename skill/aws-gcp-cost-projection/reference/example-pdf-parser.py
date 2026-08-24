#!/usr/bin/env python3
"""Example PDF parser for AWS console bills.

WORKED EXAMPLE, NOT A DROP-IN TOOL. The regex toolbox (AMOUNT_RE,
QTY_RE, PAGE_HEADER_RE, parse_amount, parse_qty) and the
continuation-line joiner are generic and reusable. The indent-walking
heuristics in walk_rows() are tuned to one observed PDF layout
(multi-page itemized AWS India console export); other layouts may
need small adjustments to the indent baselines or how service /
region / group headers are distinguished.

Use this as a starting template — read it, understand what each phase
does, then adapt as needed for the new PDF flavor. See
pdf-ingestion.md in this directory for the general recipe.

Usage:
    pdftotext -layout input.pdf input.txt
    python3 example-pdf-parser.py \\
        --input input.txt \\
        --output input.csv \\
        --entity "Amazon Web Services India Private Limited"

The script anchors on the entity prefix (matched as-is, optionally
followed by a "(N)" account-count suffix) and auto-discovers the
closing "Total tax USD X" line that ends the line-item block. For US
billing pass --entity "Amazon Web Services, Inc."; for other entity
flavors adjust accordingly. The "Taxes by service" table is used for
per-service reconciliation; the CSV is emitted regardless and a
mismatch summary printed to stderr.

Output columns: Service, Region, Custom Usage Type, Description,
Usage Quantity, Cost ($)
"""
import argparse
import csv
import re
import sys
from pathlib import Path

# --- 1. Pull the canonical service list from "Taxes by service" table ---
# This gives us authoritative service names + pre-tax totals for reconciliation.
def extract_service_totals(text: str, entity_prefix: str) -> dict[str, float]:
    """Returns {service_name: pre_tax_total}.

    Locates the "Taxes by service" block by matching the entity prefix
    (optionally followed by "(N)" account count) up to its closing
    "Total tax USD X" subtotal — both anchors are auto-discovered.
    """
    m = re.search(
        rf"{re.escape(entity_prefix)}(?:\s*\(\d+\))?\s+Total tax\s+USD\s+[\d,]+\.\d{{2}}.*",
        text, re.DOTALL,
    )
    if not m:
        return {}
    block = m.group(0)
    # rows look like:
    # " Elastic Compute Cloud  USD 9,569.32  USD 8,109.60  USD 1,459.72"
    # (post-tax, pre-tax, tax)
    pattern = re.compile(
        r"^[ \t]{1,3}([A-Za-z][\w \t\(\)\-/&]+?)[ \t]{2,}"
        r"USD\s+[\d,]+\.\d{2}\s+"            # post-tax
        r"USD\s+([\d,]+\.\d{2})\s+"          # pre-tax
        r"USD\s+[\d,]+\.\d{2}[ \t]*$",       # tax
        re.MULTILINE,
    )
    totals: dict[str, float] = {}
    for mm in pattern.finditer(block):
        name = mm.group(1).strip()
        amount = float(mm.group(2).replace(",", ""))
        totals[name] = amount
    return totals


# Match a trailing amount on a row, capturing sign and value.
# Examples: "USD 8,109.60", "(USD 1,575.40)", "USD 0.00"
AMOUNT_RE = re.compile(r"(\()?USD\s+([\d,]+\.\d{2})(\))?\s*$")

# Trailing "<qty> <unit>" at the end of `left` (the bit of the line before
# the amount has been stripped off). Examples:
#   "5,322.68 GB", "8,064 Hrs", "9,000 IOPS-Mo",
#   "182,918,082 Objects", "1 Bucket-Mo",
#   "1,028,508,833.429 Objects", "0 GB-Mo"
# Unit may be a single token or two tokens (e.g. "Tag-Mo", "GB-Mo", "vCPU-Hours").
QTY_RE = re.compile(
    r"(?P<qty>\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Za-z][\w\-/]*(?:\s[A-Za-z][\w\-/]*)?)\s*$"
)

PAGE_HEADER_RE = re.compile(
    r"^\s*Description\s+Usage Quantity\s+Amount in USD\s*$"
)


def parse_amount(line: str) -> tuple[float, str] | None:
    m = AMOUNT_RE.search(line)
    if not m:
        return None
    neg = m.group(1) == "("
    val = float(m.group(2).replace(",", ""))
    return (-val if neg else val, line[: m.start()].rstrip())


def parse_qty(left: str) -> tuple[str, float, str] | None:
    """Pull qty+unit off the right side of `left`. Returns (desc, qty, unit)."""
    stripped = left.rstrip()
    m = QTY_RE.search(stripped)
    if not m:
        return None
    qty = float(m.group("qty").replace(",", ""))
    unit = m.group("unit").strip()
    desc = stripped[: m.start()].rstrip()
    return desc, qty, unit


def main() -> None:
    ap = argparse.ArgumentParser(description="See module docstring.")
    ap.add_argument("--input", required=True, help="pdftotext -layout output (.txt)")
    ap.add_argument("--output", required=True, help="Path to write the flat CSV")
    ap.add_argument(
        "--entity",
        required=True,
        help='Entity prefix to anchor the line-item block, e.g. '
        '"Amazon Web Services India Private Limited" or '
        '"Amazon Web Services, Inc."',
    )
    args = ap.parse_args()

    src = Path(args.input)
    out = Path(args.output)
    entity_prefix = args.entity

    text = src.read_text()
    service_totals = extract_service_totals(text, entity_prefix)
    if not service_totals:
        sys.exit(
            "Could not locate 'Taxes by service' table for entity "
            f"{entity_prefix!r}. Check the PDF actually contains that "
            "billing entity, or adjust --entity."
        )

    known_services = set(service_totals.keys())
    # Plus a few seen as section heads in "Charges by service" that may not
    # appear in the tax table (e.g., $0 services). We add as we encounter.

    # Locate the entity's "Charges by service" section header. The entity name
    # appears several times in an AWS PDF (summary, charges section header,
    # invoice header, taxes section, etc.); we want the one that begins a
    # line-item block. Distinguishing signal: it's followed by either
    # "Total pre-tax USD <amount>" on the same line (most common) or by an
    # indented service list. We match the first occurrence that has
    # "Total pre-tax" within 200 chars to its right.
    start = -1
    for m in re.finditer(rf"{re.escape(entity_prefix)}(?:\s*\(\d+\))?", text):
        tail = text[m.start(): m.start() + 200]
        if re.search(r"Total pre-tax\s+USD", tail):
            start = m.start()
            break
    if start < 0:
        sys.exit(
            f"Could not locate the 'Charges by service' header for entity "
            f"{entity_prefix!r}. Looked for occurrence followed by "
            "'Total pre-tax USD' marker."
        )
    # End at the first non-zero "Total tax USD X" line after start. Zero-amount
    # tax subtotals appear before the line-item block in some PDFs; skip them.
    end = len(text)
    for m in re.finditer(
        r"^Total tax\s+USD\s+([\d,]+\.\d{2})\s*$",
        text[start:],
        re.MULTILINE,
    ):
        amt = float(m.group(1).replace(",", ""))
        if amt > 0:
            end = start + m.start()
            break

    section = text[start:end]
    raw_lines = section.splitlines()

    # --- Join wrapped continuation lines ---
    # A line is a continuation if it has no amount AND the previous emitted
    # line was a leaf (had an amount, indent >= 3). For leaf rows the
    # structure is `<desc>  <qty> <unit>  <amount>` — the continuation must
    # be inserted INTO the description, BEFORE qty+unit, so the qty stays
    # rightmost-pre-amount.
    joined: list[str] = []
    for ln in raw_lines:
        if not ln.strip():
            joined.append(ln)
            continue
        if PAGE_HEADER_RE.match(ln):
            continue
        if AMOUNT_RE.search(ln):
            joined.append(ln)
            continue
        indent = len(ln) - len(ln.lstrip())
        if joined and indent >= 3 and AMOUNT_RE.search(joined[-1] or ""):
            prev_indent = len(joined[-1]) - len(joined[-1].lstrip())
            if prev_indent >= 3:
                prev = joined[-1]
                am = AMOUNT_RE.search(prev)
                left = prev[: am.start()].rstrip()
                amount_part = prev[am.start():]
                # Try to peel off "<qty> <unit>" from end of left.
                q = parse_qty(left)
                if q:
                    desc, qty, unit = q
                    # Re-insert continuation INTO desc.
                    new_desc = f"{desc} {ln.strip()}".rstrip()
                    # Rebuild a synthetic line preserving rough spacing.
                    joined[-1] = (
                        f"{' ' * prev_indent}{new_desc.lstrip()}"
                        f"   {qty:g} {unit}   {amount_part}"
                    )
                else:
                    # No qty parseable — just glue continuation before amount.
                    joined[-1] = f"{left} {ln.strip()}  {amount_part}"
                continue
        joined.append(ln)

    # --- Walk and emit rows ---
    # Indent scheme drifts across pages (service=1/region=1/group=2/leaf=5
    # on one page vs service=0/region=0/group=1/leaf=3 on another). We
    # latch the service's indent each time we see a service header, then
    # use it as the baseline: region = svc_indent, group = svc_indent+1,
    # leaf = svc_indent+3 (or anything with qty+amount).
    current_service: str | None = None
    current_region: str | None = None
    current_group: str | None = None
    svc_indent: int = 0  # baseline; reset when we latch a new service
    rows: list[dict] = []

    def emit(desc: str, qty: float | None, unit: str | None, cost: float) -> None:
        if not current_service:
            return  # skip stuff before any service header
        rows.append(
            {
                "Service": current_service,
                "Region": current_region or "",
                "Custom Usage Type": current_group or "",
                "Description": desc.strip(),
                "Usage Quantity": (
                    f"{qty:g} {unit}" if qty is not None and unit else ""
                ),
                "Cost ($)": f"{cost:.6f}",
            }
        )

    for ln in joined:
        if not ln.strip():
            continue
        if PAGE_HEADER_RE.match(ln):
            continue
        indent = len(ln) - len(ln.lstrip())
        amt = parse_amount(ln)

        # Skip the entity-header line itself at indent=0 (e.g. "Amazon Web
        # Services India Private Limited (40)") — pulled from --entity arg.
        if indent == 0 and entity_prefix in ln:
            continue
        # "Total tax" subtotals on left edge
        if indent == 0 and ln.lstrip().startswith("Total tax"):
            continue

        if amt is None:
            # No amount — skip (could be artifact)
            continue

        cost, left = amt
        text_part = left.strip()

        stripped = text_part

        # --- Try to parse as a leaf first (quantity + amount) ---
        # Leaf detection is indent-scheme-agnostic: if the line has a qty
        # before the amount, treat it as a leaf regardless of indent.
        q = parse_qty(left)
        if q and indent >= max(2, svc_indent + 2):
            desc, qty, unit = q
            emit(desc, qty, unit, cost)
            continue
        # Credit lines have no numeric qty but a "Credit" marker.
        if "Credit" in left and indent >= max(2, svc_indent + 2):
            cm = re.search(r"^\s*(.+?)\s{2,}Credit\s*$", left)
            if cm:
                emit(cm.group(1), None, "Credit", cost)
                continue

        # --- Headers ---
        if stripped == "Enterprise Discount Program Discounts":
            if current_service:
                rows.append(
                    {
                        "Service": current_service,
                        "Region": "",
                        "Custom Usage Type": "EDP-Discount",
                        "Description": "Enterprise Discount Program Discount",
                        "Usage Quantity": "1",
                        "Cost ($)": f"{cost:.6f}",
                    }
                )
            continue

        # Service header — match against the full known_services set plus
        # a few $0 services that don't appear in the tax-by-service table.
        zero_dollar_services = {
            "CloudFront", "Simple Notification Service", "CloudWatch Events",
            "X-Ray", "Athena",
        }
        is_service = stripped in known_services or stripped in zero_dollar_services
        if is_service:
            current_service = stripped
            current_region = None
            current_group = None
            svc_indent = indent
            continue

        # Region vs group, relative to the latched service indent.
        if current_service is None:
            continue
        if indent == svc_indent:
            current_region = stripped
            current_group = None
            continue
        if indent >= svc_indent + 1:
            current_group = stripped
            continue

    # --- Write CSV ---
    fieldnames = [
        "Service", "Region", "Custom Usage Type", "Description",
        "Usage Quantity", "Cost ($)",
    ]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # --- Reconcile ---
    csv_total = sum(float(r['Cost ($)']) for r in rows)
    pdf_pretax_total = sum(service_totals.values())
    print(f"Rows emitted: {len(rows)}")
    print(f"CSV total:    {csv_total:,.2f}")
    print(f"PDF pre-tax:  {pdf_pretax_total:,.2f}")
    print()
    print("Per-service reconciliation (CSV vs PDF pre-tax):")
    csv_by_svc: dict[str, float] = {}
    for r in rows:
        csv_by_svc[r["Service"]] = csv_by_svc.get(r["Service"], 0.0) + float(r["Cost ($)"])
    print(f"  {'Service':<45} {'CSV':>12} {'PDF':>12} {'diff':>10}")
    for svc, pdf_total in sorted(service_totals.items(), key=lambda kv: -kv[1]):
        csv_total = csv_by_svc.get(svc, 0.0)
        diff = csv_total - pdf_total
        flag = "" if abs(diff) < 0.05 else " ⚠"
        print(f"  {svc:<45} {csv_total:>12,.2f} {pdf_total:>12,.2f} {diff:>+10,.2f}{flag}")


if __name__ == "__main__":
    main()
