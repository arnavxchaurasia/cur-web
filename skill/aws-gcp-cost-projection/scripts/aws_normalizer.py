#!/usr/bin/env python3
"""
aws_normalizer.py <projection.duckdb>

Layer 1 of the global logic: stamp every row in aws_li_catalog with a
canonical_service key derived from the raw product name.

Why this exists
---------------
AWS bills the same service under many different product-name strings:
  "Amazon Elastic Container Service APS3-Fargate-GB-Hours"   (PDF bill)
  "Amazon Elastic Container Service"                           (CUR bill)
  "AmazonECS"                                                  (older CUR)
  "AWS Fargate"                                                 (split billing)

Without normalization, every downstream rule has to enumerate all aliases.
A missed alias silently falls to misc. With canonical_service, the alias
problem is solved once here and every rule downstream keys on the canonical key.

Behaviour
---------
- Strips PDF region/charge-type suffixes before matching.
- Writes canonical_service = <key> into aws_li_catalog (new column, idempotent).
- Prints a table of unrecognized product names with their spend to stdout so
  the engineer can add them — loud failure, not silent misc fallthrough.
- Exit 0 always (non-fatal: missing a canonical key is handled downstream).
"""
from __future__ import annotations
import re, sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_data_config as _cfg
try:
    import duckdb
except Exception as e:
    print(f"aws_normalizer: duckdb not available ({e}); skipping", file=sys.stderr)
    sys.exit(0)

# ---------------------------------------------------------------------------
# Alias dictionary: lower-cased product name → canonical_service key.
# Loaded from data/aws-aliases.json — edit that file to add new services.
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = _cfg("aws-aliases").get("aliases", {})

# PDF bills embed region codes and charge-type suffixes directly in the product
# field, e.g. "Amazon Elastic Container Service APS3-Fargate-GB-Hours". Strip
# these before alias lookup.
_PDF_SUFFIX_RE = re.compile(
    r'\s+(APS\d+|USE\d+|USW\d+|EUC\d+|EUW\d+|APN\d+|APSE\d+|APE\d+|MEC\d+|SAE\d+|'
    r'Global|global|EU|US|AP|SA|ME|AF)\b.*$',
    re.IGNORECASE
)
_STRIP_PREFIX_RE = re.compile(r'^(amazon|aws)\s+', re.IGNORECASE)


# Fallback substring matching must be longest-alias-first: "ec2" would otherwise
# claim "EC2 Container Registry" before "ec2 container registry" gets a chance.
_ALIASES_BY_LENGTH = sorted(_ALIASES.items(), key=lambda kv: -len(kv[0]))

# Short aliases (≤4 chars) are substrings of too many unrelated words
# ("ecs" in "secrets", "ses" in "licenses"). They only match on word boundaries.
_SHORT_ALIAS_RES = {
    alias: re.compile(r'\b' + re.escape(alias) + r'\b')
    for alias, _ in _ALIASES_BY_LENGTH if len(alias) <= 4
}


def canonical_service(product: str | None) -> str | None:
    """Return the canonical service key for a product name, or None if unknown."""
    if not product:
        return None
    full_lower = product.lower()
    # Fargate charge hint may live INSIDE the PDF suffix
    # ("Amazon Elastic Container Service APS3-Fargate-GB-Hours") — check the raw
    # string before suffix-stripping so Fargate isn't collapsed into ecs.
    if "fargate" in full_lower:
        return "fargate"
    # Strip PDF region/charge-type suffix
    p = _PDF_SUFFIX_RE.sub("", product.strip())
    # Strip "Amazon "/"AWS " prefix for alias lookup
    norm = _STRIP_PREFIX_RE.sub("", p).lower().strip()
    # Direct lookup (exact match on the normalized name)
    if norm in _ALIASES:
        return _ALIASES[norm]
    # Fallback: longest alias first; short aliases require word boundaries.
    for alias, key in _ALIASES_BY_LENGTH:
        short_re = _SHORT_ALIAS_RES.get(alias)
        if short_re is not None:
            if short_re.search(full_lower):
                return key
        elif alias in full_lower:
            return key
    return None


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <projection.duckdb>", file=sys.stderr)
        sys.exit(0)

    db_path = sys.argv[1]
    con = duckdb.connect(db_path)

    # Idempotent: add column only if missing
    existing_cols = {r[1].lower() for r in con.execute("PRAGMA table_info('aws_li_catalog')").fetchall()}
    if "canonical_service" not in existing_cols:
        con.execute("ALTER TABLE aws_li_catalog ADD COLUMN canonical_service TEXT")

    rows = con.execute(
        "SELECT aws_li_key, product, aws_amortized_cost FROM aws_li_catalog"
    ).fetchall()

    updates: list[tuple[str | None, str]] = []
    unrecognized: dict[str, float] = collections.defaultdict(float)

    for key, product, cost in rows:
        cs = canonical_service(product)
        updates.append((cs, key))
        if cs is None and (cost or 0) > 0:
            unrecognized[product or ""] += cost or 0

    con.executemany(
        "UPDATE aws_li_catalog SET canonical_service = ? WHERE aws_li_key = ?",
        updates,
    )
    con.commit()

    recognized = sum(1 for _, k in updates if _ is not None)
    print(f"aws_normalizer: stamped canonical_service on {len(updates)} rows "
          f"({recognized} recognized, {len(updates)-recognized} unknown)")

    if unrecognized:
        print("\nUNRECOGNIZED PRODUCT NAMES (add to _ALIASES in aws_normalizer.py):")
        print(f"  {'spend':>10}  product")
        for prod, spend in sorted(unrecognized.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  ${spend:>9,.2f}  {prod[:80]}")

    con.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
