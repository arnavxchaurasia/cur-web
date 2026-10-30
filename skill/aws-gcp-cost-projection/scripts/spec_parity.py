#!/usr/bin/env python3
"""
spec_parity.py [<jobs-root> ...] — shadow-mode comparison of the spec-dispatch
engine against the legacy classify_mechanics rule chain, over real job DBs.

Read-only. Writes nothing, changes nothing. Run it before and after any change
to data/service_specs.json or classify_mechanics.RULES.

For every row in every projection.duckdb found under the given roots, it
compares classify_mechanics.classify(row) with spec_dispatch.classify_via_specs(row)
and buckets the result:

    agree      both engines return the same mechanic_group
    differ     they disagree — review each pattern individually
    spec_gap   the service HAS a spec but no charge_type matched the row
               (a real coverage hole; today this silently falls back)
    no_spec    the service has no spec at all

It also reports:
    ambiguous  rows where MORE THAN ONE charge_type in the service matched.
               First-match-wins hides these; the second match may be the
               better one. Must be zero before a service is cut over.

Everything is weighted by aws_amortized_cost, because row counts do not tell
you whether a gap matters.

Usage:
    python3 spec_parity.py                       # default job roots
    python3 spec_parity.py /path/to/jobs ...
"""
from __future__ import annotations

import collections
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"spec_parity: duckdb unavailable ({e})\n")
    sys.exit(0)

import classify_mechanics as cm
import spec_dispatch as sd

_DEFAULT_ROOTS = [
    os.path.expanduser("~/.cur-web/jobs"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "cur-web-data", "jobs"),
]

_COLS = [
    "aws_li_key", "product", "usage_type", "operation", "pricing_unit",
    "line_item_type", "pricing_model", "aws_amortized_cost", "instance_type",
    "instance_vcpus", "instance_ram_gb", "instance_count", "billing_format",
    "product_code", "canonical_service", "is_workload",
]


def _load(db):
    con = duckdb.connect(db, read_only=True)
    try:
        cols = {r[1].lower() for r in con.execute(
            "PRAGMA table_info('aws_li_catalog')").fetchall()}
        if "canonical_service" not in cols:
            return None  # pre-Layer-1 DB; identity not stamped
        sel = [c if c in cols else f"NULL AS {c}" for c in _COLS]
        return con.execute("SELECT " + ", ".join(sel) + " FROM aws_li_catalog").fetchall()
    finally:
        con.close()


def main(argv):
    roots = argv[1:] or [r for r in _DEFAULT_ROOTS if r and os.path.isdir(r)]
    dbs = []
    for root in roots:
        dbs += glob.glob(os.path.join(root, "*", "projection-audit", "projection.duckdb"))
    if not dbs:
        print("spec_parity: no job DBs found under " + ", ".join(roots))
        return 0

    buckets, bspend = collections.Counter(), collections.Counter()
    differ, dspend = collections.Counter(), collections.Counter()
    gap, gapspend = collections.Counter(), collections.Counter()
    nospec, nospend = collections.Counter(), collections.Counter()
    ambig = collections.Counter()
    jobs = skipped = rows = 0

    for db in dbs:
        try:
            recs = _load(db)
        except Exception as e:
            print(f"  skip {os.path.basename(os.path.dirname(os.path.dirname(db)))}: {type(e).__name__}")
            continue
        if recs is None:
            skipped += 1
            continue
        jobs += 1
        for raw in recs:
            r = dict(zip(_COLS, raw))
            r["unit"] = r.pop("pricing_unit")
            r["canonical"] = r.pop("canonical_service")
            r["_bill_has_discounted_usage"] = True
            if not r.get("is_workload"):
                continue
            cost = float(r.get("aws_amortized_cost") or 0)
            rows += 1
            canon = r.get("canonical")

            legacy = cm.classify(r)
            via = sd.classify_via_specs(r)
            d = sd.dispatch(r)

            if d is None:
                if canon in sd.SPECS:
                    buckets["spec_gap"] += 1
                    bspend["spec_gap"] += cost
                    gap[(canon, (r.get("usage_type") or "")[:48])] += 1
                    gapspend[canon] += cost
                elif via in ("negative_cost", "commitment_discount", "non_workload"):
                    buckets["excluded"] += 1
                    bspend["excluded"] += cost
                else:
                    buckets["no_spec"] += 1
                    bspend["no_spec"] += cost
                    nospec[canon or "?"] += 1
                    nospend[canon or "?"] += cost
                continue

            matched = sum(1 for ct in sd.SPECS[canon].get("charge_types", [])
                          if sd._charge_type_matches(ct, r) and not sd.is_default_charge_type(ct))
            if matched > 1:
                ambig[(canon, d["charge_type"], matched)] += 1

            if via == legacy:
                buckets["agree"] += 1
                bspend["agree"] += cost
            else:
                buckets["differ"] += 1
                bspend["differ"] += cost
                differ[(canon, legacy, via)] += 1
                dspend[(canon, legacy, via)] += cost

    total = sum(bspend.values()) or 1.0
    print(f"\njobs={jobs} (skipped {skipped} pre-Layer-1)  workload_rows={rows}  "
          f"specs={len(sd.SPECS)}")
    print(f"\n{'bucket':<12}{'rows':>8}{'spend':>16}{'% spend':>10}")
    print("-" * 46)
    for b in ("agree", "differ", "spec_gap", "no_spec", "excluded"):
        if buckets[b] > 0 or bspend[b] != 0:
            print(f"{b:<12}{buckets[b]:>8}{bspend[b]:>16,.0f}{100*bspend[b]/total:>9.1f}%")

    def _section(title, counter, spend_by, keyfmt):
        print(f"\n--- {title} ---")
        if not counter:
            print("  none")
            return
        for k, n in counter.most_common(15):
            print("  " + keyfmt(k, n, spend_by))

    _section("DISAGREEMENTS  (canonical: legacy -> spec)", differ, dspend,
             lambda k, n, s: f"{k[0]:<16}{k[1]:<22}-> {k[2]:<22} rows={n:<5} ${s[k]:>12,.0f}")
    print(f"\n--- SPEC GAPS (service declared, nothing matched) ---")
    print("  Compare this table PER SERVICE against your baseline, not just the")
    print("  bucket total. A fix that improves one service while regressing another")
    print("  can leave the total almost unchanged: confirmed real — folding the ebs")
    print("  canonical into ec2 added $184 of EC2 gap while kinesis dropped $142 out,")
    print("  for a net +$44 that looked like noise.")
    if not gapspend:
        print("  none")
    for canon, s in sorted(gapspend.items(), key=lambda kv: -abs(kv[1]))[:15]:
        ex = [u for (c, u) in gap if c == canon][:3]
        print(f"  {canon:<16} ${s:>12,.0f}   e.g. {ex}")
    print(f"\n--- NO SPEC (service undeclared) ---")
    if not nospend:
        print("  none")
    for canon, s in sorted(nospend.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {canon:<16} ${s:>12,.0f}   rows={nospec[canon]}")
    print(f"\n--- AMBIGUOUS (>1 charge_type matched — must be 0 before cutover) ---")
    if not ambig:
        print("  none")
    for (canon, ct, n), cnt in ambig.most_common(15):
        print(f"  {canon:<16}{ct:<30} matched_by={n}  rows={cnt}")
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover
        print(f"spec_parity: failed ({type(e).__name__}: {e})")
        raise SystemExit(0)
