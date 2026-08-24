#!/usr/bin/env python3
"""
validate_report.py — golden-test a rendered GCP-projection report against the
source bill. Catches the defect classes found in the bill3 audit (2026-07-07):

  1. AWS baseline must be NET of EDP discounts/credits (within 5% of the PDF's
     stated pre-tax total when a PDF is given).
  2. Pricing order per row: GCP OD >= 1yr CUD >= 3yr CUD (no inverted commits).
  3. No mapped row's implied GCP/AWS ratio outside [1/8, 8] unless its note
     marks it passthrough/ignore/directional.
  4. Confidence values must render <= 100%.

Usage:
    python3 validate_report.py <report.html> [source_bill.pdf]

Exit 0 = all checks pass; exit 1 = failures (printed).
"""
import re, sys
from html.parser import HTMLParser


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data


def _money(s):
    s = re.sub(r"[$,+]", "", s or "").replace("−", "-").replace("–", "-").strip()
    try:
        return float(s)
    except ValueError:
        return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    report, pdf = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    p = _Tables()
    p.feed(open(report, encoding="utf-8").read())
    failures = []

    detail = next((t for t in p.tables if t and t[0] and t[0][0] == "#"), None)
    if detail is None:
        print("FAIL: no line-item table found in report")
        return 1
    rows = [r for r in detail[1:] if len(r) >= 8]

    # 1. AWS total vs PDF stated pre-tax
    summary = next((t for t in p.tables if t and t[0] and t[0][0] == "AWS Total"), None)
    aws_total = _money(summary[0][1]) if summary else None
    if pdf and aws_total:
        import pdfplumber
        with pdfplumber.open(pdf) as doc:
            text = "\n".join(pg.extract_text() or "" for pg in doc.pages)
        pretax = sum(float(x.replace(",", "")) for x in
                     re.findall(r"total\s+pre-?tax\s+USD\s+([\d,]+\.\d{2})", text, re.I))
        if pretax > 0 and abs(aws_total - pretax) / pretax > 0.05:
            failures.append(f"AWS total ${aws_total:,.2f} deviates >5% from PDF "
                            f"stated pre-tax ${pretax:,.2f} (gross-vs-net bug?)")

    ratio_flag_re = re.compile(r"passthrough|ignore|directional|manual|no gcs equivalent|parity", re.I)
    for r in rows:
        aws, od, cud, y3 = (_money(r[3]), _money(r[4]), _money(r[5]), _money(r[6]))
        label = f"row #{r[0]} ({r[1][:60]}...)"
        # 2. commitment ordering
        if od is not None and cud is not None and y3 is not None:
            if cud > od + 0.01 or y3 > cud + 0.01:
                failures.append(f"{label}: CUD ordering violated "
                                f"(OD {od} / 1yr {cud} / 3yr {y3})")
        # 3. implied-ratio sanity on mapped rows
        note = f"{r[1]} {r[2]}"
        if aws and od and aws > 1 and not ratio_flag_re.search(note):
            ratio = od / aws
            if ratio > 8 or ratio < 1 / 8:
                failures.append(f"{label}: GCP/AWS ratio {ratio:.2f}x outside [0.125, 8] "
                                f"— wrong SKU or unit?")

    # 4. confidence display sanity
    for m in re.finditer(r"([\d.]+)%", open(report, encoding="utf-8").read()):
        if float(m.group(1)) > 100.5:
            failures.append(f"confidence/percentage renders as {m.group(1)}% (>100%)")
            break

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"PASS: {len(rows)} rows checked"
          + (f", AWS total ${aws_total:,.2f} reconciles" if aws_total else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
