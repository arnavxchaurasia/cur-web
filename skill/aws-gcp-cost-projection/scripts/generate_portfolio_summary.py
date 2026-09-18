#!/usr/bin/env python3
"""
generate_portfolio_summary.py — Consolidated AWS -> GCP summary across
multiple already-completed jobs, matching the "Summary" tab format of the
original reference workbook: a top-line total, a per-customer breakdown,
and a per-category breakdown (Compute/Network/Storage/Monitoring/Database/
Other), each with AWS Total, AWS Mapped Cost, GCP On-Demand, Diff, Diff%.

Usage:
    python3 generate_portfolio_summary.py <job_dir_1> [<job_dir_2> ...] [--json]

Each <job_dir> is a job's root directory (containing projection-audit/
projection.duckdb and customer_name.txt) — the same directory the web app
stores per-job state in. Prints a JSON summary to stdout by default (for the
Go backend to embed), or a human-readable table with --json omitted... no —
default is JSON; pass --pretty for the human-readable version.

Reads only already-computed data (aws_li_catalog, aws_li_to_gcp_li,
gcp_projection, run_results) — never re-runs mapping/pricing. If a job's
duckdb is missing a table (job never finished, or predates gcp_projection),
that job is skipped with a warning on stderr, not a hard failure — one bad
job directory should never keep the rest of the portfolio from summarizing.
"""
import datetime
import html as _html
import json
import os
import sys

import duckdb

# Reuses the same page CSS as the per-job report (render_report.py) so the
# portfolio summary looks like one family of documents, not a bolted-on
# second style. Both scripts live in this same directory and are invoked
# directly (not as an installed package), so a plain module import — not a
# package-relative one — is what actually resolves at runtime here.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_report import CSS  # noqa: E402

_FC_LOGO_ICON = """<svg width="24" height="20" viewBox="0 0 34 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Facets">
<path d="M15.8331 19.9996V0L0 19.9996H15.8331Z" fill="#00C2BB"/>
<path d="M33.5 8.67844e-05H17.6669V5H29.5417L33.5 8.67844e-05Z" fill="#645DF6"/>
<path d="M17.7501 14.0001V20L22.5 14.0001H17.7501Z" fill="#645DF6"/>
<path d="M28.082 7.00009H17.7905V12H24.1238L28.082 7.00009Z" fill="#00C2BB"/>
</svg>"""
# Wordmark rendered as styled text (not the vector letterforms) — avoids
# embedding/duplicating render_report.py's much longer path data here.
_FC_LOGO_FULL = (
    f'<span style="display:inline-flex;align-items:center;gap:8px">'
    f'{_FC_LOGO_ICON}'
    f'<span style="font:600 16px \'Google Sans\',Roboto,Arial,sans-serif">'
    f'<span style="color:#645DF6">Facets</span><span style="color:#00C2BB">.cloud</span>'
    f'</span></span>'
)

# Category classification mirrors the reference workbook's own 6 buckets.
# Order matters: first match wins. Kept in one place (not duplicated per
# customer) so every job in the portfolio is bucketed the same way.
_CATEGORY_CASE_SQL = """
    CASE
      WHEN c.product ILIKE '%CloudWatch%' OR c.product ILIKE '%CloudTrail%'
        OR c.product ILIKE '%Config%' THEN 'Monitoring'
      WHEN c.product ILIKE '%RDS%' OR c.product ILIKE '%Aurora%'
        OR c.product ILIKE '%Relational Database%' OR c.product ILIKE '%Redshift%'
        OR c.product ILIKE '%DynamoDB%' THEN 'Database'
      WHEN c.product ILIKE '%S3%' OR c.product ILIKE '%EBS%' OR c.product ILIKE '%Glacier%'
        OR c.product ILIKE '%Elastic Block Store%' OR c.product ILIKE '%Simple Storage%'
        OR c.product ILIKE '%Backup%' OR c.product ILIKE '%EFS%' THEN 'Storage'
      WHEN c.product ILIKE '%Route 53%' OR c.product ILIKE '%CloudFront%'
        OR c.product ILIKE '%Virtual Private Cloud%' OR c.product ILIKE '%VPC%'
        OR c.product ILIKE '%Load Balancing%' OR c.product ILIKE '%Data Transfer%'
        OR c.product ILIKE '%WAF%' OR c.product ILIKE '%Direct Connect%'
        OR c.product ILIKE '%NatGateway%' OR c.product ILIKE '%Bandwidth%' THEN 'Network'
      WHEN c.product ILIKE '%Elastic Compute%' OR c.product ILIKE '%EC2%'
        OR c.product ILIKE '%OpenSearch%' OR c.product ILIKE '%Elasticsearch%'
        OR c.product ILIKE '%MSK%' OR c.product ILIKE '%Fargate%'
        OR c.product ILIKE '%Lambda%' OR c.product ILIKE '%ElastiCache%' THEN 'Compute'
      ELSE 'Other'
    END
"""


def _customer_name(job_dir):
    p = os.path.join(job_dir, "customer_name.txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                name = f.read().strip()
            if name:
                return name
        except Exception:
            pass
    return os.path.basename(job_dir.rstrip("/\\"))


def _job_summary(job_dir):
    """Return one job's rollup, or None if this job has no usable data."""
    db_path = os.path.join(job_dir, "projection-audit", "projection.duckdb")
    if not os.path.exists(db_path):
        print(f"WARNING: no projection.duckdb in {job_dir!r} — skipping", file=sys.stderr)
        return None

    con = duckdb.connect(db_path, read_only=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
        if "gcp_projection" not in tables or "aws_li_catalog" not in tables:
            print(f"WARNING: {job_dir!r} missing gcp_projection/aws_li_catalog "
                  "(job never completed rendering) — skipping", file=sys.stderr)
            return None

        line_items = con.execute("SELECT COUNT(*) FROM aws_li_catalog").fetchone()[0]

        # AWS Total (Bill): one amount per distinct line item (a compute row's
        # core+ram components share the same aws_amortized_cost — summing the
        # raw catalog, not the component-split projection table, avoids
        # double-counting it per component).
        aws_total = con.execute(
            "SELECT COALESCE(SUM(aws_amortized_cost), 0) FROM aws_li_catalog"
        ).fetchone()[0]

        # AWS Mapped Cost: AWS spend for rows that got a real GCP price
        # attempt (strategy IN ('map','ignore')) — excludes passthrough rows,
        # which carry the AWS amount forward as-is with no GCP-side pricing
        # decision made, same distinction the reference workbook's own
        # "AWS Mapped Cost" column draws.
        aws_mapped = con.execute("""
            SELECT COALESCE(SUM(c.aws_amortized_cost), 0)
            FROM aws_li_catalog c
            WHERE c.aws_li_key IN (
                SELECT DISTINCT aws_li_key FROM aws_li_to_gcp_li WHERE strategy != 'passthrough'
            )
        """).fetchone()[0]

        # GCP On-Demand: sum across every component row (core+ram are real,
        # separate charges that together make up one line item's GCP cost).
        gcp_od = con.execute(
            "SELECT COALESCE(SUM(gcp_projected_cost), 0) FROM gcp_projection"
        ).fetchone()[0]

        by_category = con.execute(f"""
            SELECT {_CATEGORY_CASE_SQL} AS category,
                   COUNT(DISTINCT c.aws_li_key) AS line_items,
                   COALESCE(SUM(DISTINCT_AWS.aws_amortized_cost), 0) AS aws_total
            FROM aws_li_catalog c
            JOIN (SELECT DISTINCT aws_li_key, aws_amortized_cost FROM aws_li_catalog) DISTINCT_AWS
              ON DISTINCT_AWS.aws_li_key = c.aws_li_key
            GROUP BY category
        """).fetchall()
        # The join above is a no-op dedup guard (aws_li_catalog is already one
        # row per key) — kept explicit so this stays correct if that ever
        # changes. GCP side must come from gcp_projection (component-split).
        gcp_by_category = con.execute(f"""
            SELECT {_CATEGORY_CASE_SQL} AS category,
                   COALESCE(SUM(g.gcp_projected_cost), 0) AS gcp_total
            FROM gcp_projection g
            JOIN aws_li_catalog c USING (aws_li_key)
            GROUP BY category
        """).fetchall()
        gcp_cat_map = {row[0]: row[1] for row in gcp_by_category}

        categories = []
        for cat, cnt, aws_cat_total in by_category:
            gcp_cat_total = gcp_cat_map.get(cat, 0.0)
            categories.append({
                "category": cat,
                "line_items": cnt,
                "aws_total": round(aws_cat_total, 2),
                "gcp_od": round(gcp_cat_total, 2),
                "diff": round(aws_cat_total - gcp_cat_total, 2),
                "diff_pct": round((aws_cat_total - gcp_cat_total) / aws_cat_total, 6)
                            if aws_cat_total else 0.0,
            })
    finally:
        con.close()

    diff = aws_total - gcp_od
    return {
        "job_dir": job_dir,
        "customer": _customer_name(job_dir),
        "line_items": line_items,
        "aws_total": round(aws_total, 2),
        "aws_mapped_cost": round(aws_mapped, 2),
        "gcp_od": round(gcp_od, 2),
        "diff": round(diff, 2),
        "diff_pct": round(diff / aws_total, 6) if aws_total else 0.0,
        "categories": categories,
    }


def build_portfolio_summary(job_dirs):
    per_customer = []
    for jd in job_dirs:
        s = _job_summary(jd)
        if s is not None:
            per_customer.append(s)

    total_line_items = sum(c["line_items"] for c in per_customer)
    total_aws = sum(c["aws_total"] for c in per_customer)
    total_aws_mapped = sum(c["aws_mapped_cost"] for c in per_customer)
    total_gcp = sum(c["gcp_od"] for c in per_customer)
    total_diff = total_aws - total_gcp

    # Merge category totals across every customer in the portfolio.
    cat_totals = {}
    for c in per_customer:
        for cat in c["categories"]:
            entry = cat_totals.setdefault(cat["category"], {
                "category": cat["category"], "line_items": 0, "aws_total": 0.0, "gcp_od": 0.0,
            })
            entry["line_items"] += cat["line_items"]
            entry["aws_total"] += cat["aws_total"]
            entry["gcp_od"] += cat["gcp_od"]
    categories = []
    for cat in sorted(cat_totals.values(), key=lambda x: -x["aws_total"]):
        diff = cat["aws_total"] - cat["gcp_od"]
        categories.append({
            "category": cat["category"],
            "line_items": cat["line_items"],
            "aws_total": round(cat["aws_total"], 2),
            "gcp_od": round(cat["gcp_od"], 2),
            "diff": round(diff, 2),
            "diff_pct": round(diff / cat["aws_total"], 6) if cat["aws_total"] else 0.0,
            "share_of_aws_spend": round(cat["aws_total"] / total_aws, 6) if total_aws else 0.0,
        })

    return {
        "totals": {
            "customers": len(per_customer),
            "line_items": total_line_items,
            "aws_total": round(total_aws, 2),
            "aws_mapped_cost": round(total_aws_mapped, 2),
            "gcp_od": round(total_gcp, 2),
            "diff": round(total_diff, 2),
            "diff_pct": round(total_diff / total_aws, 6) if total_aws else 0.0,
        },
        "by_customer": per_customer,
        "by_category": categories,
    }


def render_pretty(summary):
    t = summary["totals"]
    lines = []
    lines.append("Consolidated AWS -> GCP Cloud Cost Summary")
    lines.append(f"{t['customers']} customers - {t['line_items']} line items")
    lines.append("")
    lines.append(f"AWS Total (Bill): ${t['aws_total']:,.2f}")
    lines.append(f"AWS Mapped Cost:  ${t['aws_mapped_cost']:,.2f}")
    lines.append(f"GCP On-Demand:    ${t['gcp_od']:,.2f}")
    lines.append(f"Diff:             ${t['diff']:,.2f} ({t['diff_pct']*100:.1f}%)")
    lines.append("")
    lines.append("Summary by Customer")
    lines.append(f"{'Customer':<22} {'Items':>6} {'AWS Total':>12} {'Mapped':>12} {'GCP OD':>12} {'Diff':>10} {'Diff%':>8}")
    for c in summary["by_customer"]:
        lines.append(f"{c['customer']:<22} {c['line_items']:>6} {c['aws_total']:>12,.2f} "
                      f"{c['aws_mapped_cost']:>12,.2f} {c['gcp_od']:>12,.2f} {c['diff']:>10,.2f} {c['diff_pct']*100:>7.1f}%")
    lines.append("")
    lines.append("Summary by Category")
    lines.append(f"{'Category':<14} {'Items':>6} {'AWS':>12} {'GCP OD':>12} {'Diff':>10} {'Diff%':>8} {'Share':>7}")
    for c in summary["by_category"]:
        lines.append(f"{c['category']:<14} {c['line_items']:>6} {c['aws_total']:>12,.2f} "
                      f"{c['gcp_od']:>12,.2f} {c['diff']:>10,.2f} {c['diff_pct']*100:>7.1f}% {c['share_of_aws_spend']*100:>6.1f}%")
    return "\n".join(lines)


def write_xlsx(summary, out_path):
    """Write summary as a single-sheet .xlsx matching the reference workbook's
    Summary tab layout: header totals, per-customer table, per-category table.
    Static values, no formulas — this is a generated report snapshot (same
    convention as render_report.py's HTML reports), not an editable model, so
    the xlsx skill's "always use formulas" rule doesn't apply here; nothing
    downstream re-derives these numbers from the sheet's own formulas."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    from openpyxl.styles import Font
    bold = Font(name="Arial", bold=True)
    title_font = Font(name="Arial", bold=True, size=13)
    normal = Font(name="Arial")
    money_fmt = '$#,##0.00;($#,##0.00);"-"'
    pct_fmt = "0.0%"

    def money(row, col, val):
        c = ws.cell(row=row, column=col, value=val)
        c.number_format = money_fmt
        c.font = normal
        return c

    def pct(row, col, val):
        c = ws.cell(row=row, column=col, value=val)
        c.number_format = pct_fmt
        c.font = normal
        return c

    def text(row, col, val, font=normal):
        c = ws.cell(row=row, column=col, value=val)
        c.font = font
        return c

    t = summary["totals"]
    text(1, 1, "Consolidated AWS → GCP Cloud Cost Summary", title_font)
    text(2, 1, f"{t['customers']} customers · {t['line_items']} line items", normal)

    headers = ["AWS Total (Bill)", "AWS Mapped Cost", "GCP On-Demand",
               "Diff (AWS Total Bill − GCP)", "Diff %"]
    for i, h in enumerate(headers):
        text(4, i + 1, h, bold)
    money(5, 1, t["aws_total"]); money(5, 2, t["aws_mapped_cost"]); money(5, 3, t["gcp_od"])
    money(5, 4, t["diff"]); pct(5, 5, t["diff_pct"])

    row = 8
    text(row, 1, "Summary by Customer", bold)
    row += 1
    for i, h in enumerate(["Customer", "Line Items", "AWS Total (Bill)",
                           "AWS Mapped Cost", "GCP On-Demand", "Diff", "Diff %"]):
        text(row, i + 1, h, bold)
    row += 1
    for c in summary["by_customer"]:
        text(row, 1, c["customer"])
        text(row, 2, c["line_items"])
        money(row, 3, c["aws_total"]); money(row, 4, c["aws_mapped_cost"]); money(row, 5, c["gcp_od"])
        money(row, 6, c["diff"]); pct(row, 7, c["diff_pct"])
        row += 1
    text(row, 1, "TOTAL — ALL CUSTOMERS", bold)
    text(row, 2, t["line_items"], bold)
    money(row, 3, t["aws_total"]); money(row, 4, t["aws_mapped_cost"]); money(row, 5, t["gcp_od"])
    money(row, 6, t["diff"]); pct(row, 7, t["diff_pct"])
    for col in range(1, 8):
        ws.cell(row=row, column=col).font = bold
    row += 3

    text(row, 1, "Summary by Category — all customers combined", bold)
    row += 1
    for i, h in enumerate(["Category", "Line Items", "AWS", "GCP On-Demand",
                           "Diff", "Diff %", "Share of AWS Spend"]):
        text(row, i + 1, h, bold)
    row += 1
    for cat in summary["by_category"]:
        text(row, 1, cat["category"])
        text(row, 2, cat["line_items"])
        money(row, 3, cat["aws_total"]); money(row, 4, cat["gcp_od"])
        money(row, 5, cat["diff"]); pct(row, 6, cat["diff_pct"])
        pct(row, 7, cat.get("share_of_aws_spend", 0.0))
        row += 1

    for col, width in zip("ABCDEFG", [26, 12, 16, 16, 16, 14, 12]):
        ws.column_dimensions[col].width = width

    wb.save(out_path)


def _fmt_money(v):
    sign = "−" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v):
    return f"{v * 100:.1f}%"


def write_html(summary, out_path):
    """Write the portfolio summary as a self-contained HTML report, styled
    like the per-job report (render_report.py) — same header/footer/card/
    table look — instead of the .xlsx workbook. This is the primary output
    format for the internal portfolio-summary tool; write_xlsx is kept only
    for anyone who still wants a spreadsheet to pivot on."""
    t = summary["totals"]
    now = datetime.datetime.now(datetime.timezone.utc)

    diff_class = "green" if t["diff"] >= 0 else "red"

    cards_html = "".join([
        f'<div class="card card-accent"><div class="card-label">AWS Total (Bill)</div>'
        f'<div class="card-value">{_fmt_money(t["aws_total"])}</div></div>',
        f'<div class="card"><div class="card-label">AWS Mapped Cost</div>'
        f'<div class="card-value">{_fmt_money(t["aws_mapped_cost"])}</div></div>',
        f'<div class="card"><div class="card-label">GCP On-Demand</div>'
        f'<div class="card-value">{_fmt_money(t["gcp_od"])}</div></div>',
        f'<div class="card"><div class="card-label">Diff</div>'
        f'<div class="card-value {diff_class}">{_fmt_money(t["diff"])}</div>'
        f'<div class="card-sub {diff_class}">{_fmt_pct(t["diff_pct"])}</div></div>',
    ])

    customer_rows = "".join(
        f'<tr><td>{_html.escape(c["customer"])}</td>'
        f'<td class="num">{c["line_items"]:,}</td>'
        f'<td class="num">{_fmt_money(c["aws_total"])}</td>'
        f'<td class="num">{_fmt_money(c["aws_mapped_cost"])}</td>'
        f'<td class="num">{_fmt_money(c["gcp_od"])}</td>'
        f'<td class="num {"green" if c["diff"] >= 0 else "red"}">{_fmt_money(c["diff"])}</td>'
        f'<td class="num {"green" if c["diff"] >= 0 else "red"}">{_fmt_pct(c["diff_pct"])}</td></tr>'
        for c in summary["by_customer"]
    )
    customer_rows += (
        f'<tr class="total-row"><td>TOTAL — ALL CUSTOMERS</td>'
        f'<td class="num">{t["line_items"]:,}</td>'
        f'<td class="num">{_fmt_money(t["aws_total"])}</td>'
        f'<td class="num">{_fmt_money(t["aws_mapped_cost"])}</td>'
        f'<td class="num">{_fmt_money(t["gcp_od"])}</td>'
        f'<td class="num {diff_class}">{_fmt_money(t["diff"])}</td>'
        f'<td class="num {diff_class}">{_fmt_pct(t["diff_pct"])}</td></tr>'
    )

    category_rows = "".join(
        f'<tr><td>{_html.escape(cat["category"])}</td>'
        f'<td class="num">{cat["line_items"]:,}</td>'
        f'<td class="num">{_fmt_money(cat["aws_total"])}</td>'
        f'<td class="num">{_fmt_money(cat["gcp_od"])}</td>'
        f'<td class="num {"green" if cat["diff"] >= 0 else "red"}">{_fmt_money(cat["diff"])}</td>'
        f'<td class="num {"green" if cat["diff"] >= 0 else "red"}">{_fmt_pct(cat["diff_pct"])}</td>'
        f'<td class="num">{_fmt_pct(cat.get("share_of_aws_spend", 0.0))}</td></tr>'
        for cat in summary["by_category"]
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Portfolio Summary — AWS → GCP Cost Analysis</title>
<meta name="description" content="Consolidated AWS to GCP cloud cost projection across {t['customers']} customers. Prepared by Facets.cloud Cloud Cost Intelligence.">
{CSS}
</head>
<body>

<header class="fc-header">
  <a href="https://www.facets.cloud/" target="_blank" rel="noopener" class="fc-logo-link">
    {_FC_LOGO_FULL}
  </a>
  <div class="fc-right">
    <div class="fc-badge">Portfolio Summary</div>
    <div class="fc-prepared">{t['customers']} customers &nbsp;·&nbsp; {now.strftime('%B %Y')}</div>
  </div>
</header>
<div class="fc-divider"></div>

<div class="page-body">

<h1>Consolidated AWS &rarr; GCP Cloud Cost Summary</h1>
<p class="subhead">
  <b>Customers:</b> {t['customers']} &nbsp;·&nbsp;
  <b>Line Items:</b> {t['line_items']:,} &nbsp;·&nbsp;
  <b>Generated:</b> {now.strftime('%B %Y')}
</p>

<div class="summary-grid">
{cards_html}
</div>

<h2>Summary by Customer</h2>
<div class="table-scroll"><table>
  <tr>
    <th>Customer</th>
    <th class="num">Items</th>
    <th class="num">AWS Total</th>
    <th class="num">Mapped</th>
    <th class="num">GCP OD</th>
    <th class="num">Diff</th>
    <th class="num">Diff %</th>
  </tr>
  {customer_rows}
</table></div>

<h2>Summary by Category &mdash; All Customers Combined</h2>
<p class="section-meta">Diff = AWS &minus; GCP On-Demand; <span class="green">green = GCP cheaper</span>, <span class="red">red = GCP more expensive</span>.</p>
<div class="table-scroll"><table>
  <tr>
    <th>Category</th>
    <th class="num">Items</th>
    <th class="num">AWS</th>
    <th class="num">GCP OD</th>
    <th class="num">Diff</th>
    <th class="num">Diff %</th>
    <th class="num">Share</th>
  </tr>
  {category_rows}
</table></div>

<footer class="fc-footer">
  <a href="https://www.facets.cloud/" target="_blank" rel="noopener" class="fc-footer-brand" style="text-decoration:none">
    {_FC_LOGO_ICON}
    <span>Generated by <b>facets.cloud</b> &mdash; Cloud Cost Intelligence Platform</span>
  </a>
  <span class="fc-footer-note">
    Report generated {now.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC &nbsp;·&nbsp;
    Prices are estimates &mdash; verify with GCP Pricing Calculator before committing.
  </span>
</footer>

</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)


def main():
    args = sys.argv[1:]
    pretty = "--pretty" in args
    xlsx_path = None
    if "--xlsx" in args:
        idx = args.index("--xlsx")
        xlsx_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    html_path = None
    if "--html" in args:
        idx = args.index("--html")
        html_path = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    job_dirs = [a for a in args if not a.startswith("--")]
    if not job_dirs:
        print("Usage: generate_portfolio_summary.py <job_dir_1> [<job_dir_2> ...] "
              "[--pretty] [--html <out.html>] [--xlsx <out.xlsx>]",
              file=sys.stderr)
        sys.exit(1)

    summary = build_portfolio_summary(job_dirs)
    if html_path:
        write_html(summary, html_path)
        print(f"Wrote {html_path}", file=sys.stderr)
    if xlsx_path:
        write_xlsx(summary, xlsx_path)
        print(f"Wrote {xlsx_path}", file=sys.stderr)
    if pretty:
        print(render_pretty(summary))
    elif not xlsx_path and not html_path:
        json.dump(summary, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
