# Reference: PDF AWS bills

Some prospects export their AWS bill as a console PDF instead of a
CUR or Cost Explorer Detail CSV. The skill can absorb these with a
short pre-processing step that converts the PDF into the flat Cost
Explorer shape, after which Phase 1 ingestion proceeds normally.

## Tools

- `pdftotext` (from `poppler-utils`). On macOS: `brew install poppler`.
  On Debian/Ubuntu: `sudo apt-get install poppler-utils`.

The skill's Read tool also renders PDF pages as images and can be used
as a vision-based fallback, but vision tokens are expensive on long
invoices — prefer `pdftotext -layout` when available.

## Recipe

1. **Convert.** `pdftotext -layout input.pdf input.txt`. The `-layout`
   flag is critical — it preserves column alignment, which the parser
   depends on.

2. **Find anchors.** Locate two markers in the text that bound the
   line-item region:
   - The **entity / payer header** where line items start, e.g.
     `"Amazon Web Services India Private Limited (40)"` for AWS India
     billing, or `"Amazon Web Services, Inc."` for US billing.
   - A **closing subtotal** where line items end, e.g. a
     `"Total tax  USD X,XXX.XX"` line, or the start of a "Taxes by
     service" table.

3. **Reconcile target.** Pull each service's pre-tax total from the
   "Taxes by service" table (one row per service: post-tax, pre-tax,
   tax). This is your ground truth for parser validation.

4. **Walk lines, classify by indentation.** Within the line-item
   region:
   - service header — indent = baseline (0 or 1)
   - region — indent = baseline
   - usage group — indent = baseline + 1
   - leaf row with qty + amount — indent ≥ baseline + 3

   Indentation can drift across pages (e.g. page 1 has region at
   indent=1, page 5 has it at indent=0). Latch the baseline each time
   you see a service header and re-derive group/leaf indents from
   that.

5. **Parse leaf rows** with these regexes:
   - amount: `(\()?USD\s+([\d,]+\.\d{2})(\))?\s*$` — captures
     parenthesized negatives like `(USD 1,575.40)`
   - qty + unit: `(\d[\d,]*(?:\.\d+)?)\s+([A-Za-z][\w\-/]*(?:\s[A-Za-z][\w\-/]*)?)\s*$`
     handles single (`GB`, `Hrs`) and two-token (`GB-Mo`, `Tag-Mo`,
     `vCPU-Hours`) units.

6. **Handle continuation lines.** A line with no `USD` amount and
   non-zero indent is a wrapped description from the previous row.
   Splice it INTO the description portion (before the qty/unit), not
   onto the end of the previous row — otherwise the qty regex will
   misfire on the next pass.

7. **Drop page headers.** `^\s*Description\s+Usage Quantity\s+Amount in USD\s*$`
   shows up between rows on every page break — skip.

8. **Reconcile.** Sum the emitted rows per service and compare to the
   "Taxes by service" pre-tax totals from step 3. If any service
   differs by more than $0.01, fix the parser before continuing. Don't
   proceed with skill phases on a CSV that doesn't reconcile.

9. **Emit CSV** in Cost Explorer Detail flat shape:
   `Service,Region,Custom Usage Type,Description,Usage Quantity,Cost ($)`.
   Phase 1 ingestion treats it identically to a real Cost Explorer
   export.

## What's lost vs CSV/CUR input

PDFs omit columns that the Phase 1 classifier normally reads:

- `lineItem/LineItemType` — can't distinguish RIFee, SavingsPlanFee,
  Refund, Credit independently. They show up as combined totals.
- `pricing/term` — can't tell On-Demand from Committed/RI usage at the
  row level. The PDF's section headers ("Reserved Instances", "On Demand")
  are your only hint, propagate them into `Custom Usage Type` if
  helpful.
- Operation, availability zone, resource ID — gone.

Phase 1's `is_workload` classifier falls back to: drop anything that
matches Tax/Refund/Credit by service name; everything else is workload.
Accept that the projection will be slightly less precise than a real
CSV would give.

## Worked example

Validated end-to-end on an 8-page AWS India console PDF (~USD 14,500
pre-tax):

- `pdftotext -layout` produced ~6.6K lines of layout text.
- Parser walked the "Charges by service" section bounded by the entity
  header and the first non-zero "Total tax" footer.
- 602 line items emitted across 34 services. Per-service reconciliation
  matched all 34 to within $0.01.
- Phase 1 ingested the synthesized CSV with no skill changes needed.

A parser like the above is ~280 lines of Python. Each PDF flavor
(AWS India vs Inc., one-page summary vs multi-page itemized) needs at
most a small adjustment to the indent-walking heuristics; the regex
toolbox is unchanged.

## Worked-example parser

`example-pdf-parser.py` in this directory is a working starting
template. Usage:

```bash
pdftotext -layout input.pdf input.txt
python3 example-pdf-parser.py \
    --input input.txt \
    --output input.csv \
    --entity "Amazon Web Services India Private Limited"
```

The script anchors on the `--entity` prefix (auto-matches the
optional `(N)` account-count suffix) and auto-discovers the closing
"Total tax USD X" line by skipping zero-amount false positives. For
US billing pass `--entity "Amazon Web Services, Inc."`; for other
entity flavors adjust accordingly.

The regex toolbox at the top (`AMOUNT_RE`, `QTY_RE`, `PAGE_HEADER_RE`,
`parse_amount`, `parse_qty`, `join_continuations`) is fully generic
and copy-pasteable. The indent-walking logic in `main()` was tuned to
one observed PDF layout — read it as a template, then adapt as needed
if your PDF has a different baseline indent scheme. Don't expect it
to run as-is on every PDF; do expect to crib 70-80% of it.
