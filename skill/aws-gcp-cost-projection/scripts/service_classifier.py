#!/usr/bin/env python3
"""
service_classifier.py <projection.duckdb>

The Service Classification Engine. Applies the curated data/service_map.json
(AWS-service -> category -> GCP-service) to every mapped row so the GCP target is
DETERMINISTIC instead of a per-row LLM guess. This is what eliminates variance
like "QuickSight -> Looker on one row, -> Cloud Storage on another".

Per matched rule:
  - mode 'review' : force gcp_service = target, strategy = 'passthrough'
                    (carry AWS cost — an honest baseline, never an invented GCP
                    figure), clear the SKU, set confidence, stamp the reason.
                    Used for services whose correct GCP target is known but whose
                    precise GCP pricing still needs per-service modelling.
  - mode 'keep'   : the pipeline already prices this well — only STAMP metadata
                    (category + reason), never touch pricing or the mapped SKU.

Rich metadata is written to projection_note as:
  "[<category>] <reason> (rule=service_map_v1, gcp=<target>)"

Idempotent, never fatal. Runs post-merge, before fix_storage_misroute (which is
the generic backstop for anything not in the map).
"""
import json, os, sys
try:
    import duckdb
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"service_classifier: duckdb import failed ({e}); skipping\n")
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from aws_normalizer import canonical_service
except Exception:  # pragma: no cover
    canonical_service = lambda _p: None

RULE_TAG = "service_map_v1"


def _norm(product):
    p = (product or "")
    for pre in ("Amazon ", "AWS "):
        if p.startswith(pre):
            p = p[len(pre):]
    return p.lower()


def _rule_matches(rule, product, usage_type, cs_product, cs_rule_cache):
    """A rule matches a row if its `match` fragment appears in the product name
    (legacy behaviour), OR in usage_type (catches services like NAT Gateway whose
    identifying signal lives in usage_type, not product, on raw-CUR bills), OR the
    row's canonical_service (Layer 1, aws_normalizer.py) equals the rule's own
    canonical_service — this catches product-name variants ("AWSKMS") that don't
    literally contain the rule's match text ("key management") but are recognized
    as the same service by the shared alias table."""
    match_l = rule["match"].lower()
    norm = _norm(product)
    if match_l in norm:
        return True
    if usage_type and match_l in usage_type.lower():
        return True
    if rule["match"] not in cs_rule_cache:
        cs_rule_cache[rule["match"]] = canonical_service(rule["match"])
    cs_rule = cs_rule_cache[rule["match"]]
    return bool(cs_rule and cs_product and cs_rule == cs_product)


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    db = sys.argv[1]

    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    map_path = os.path.join(skill_dir, "data", "service_map.json")
    if not os.path.exists(map_path):
        sys.stderr.write(f"service_classifier: no {map_path}; skipping\n")
        sys.exit(0)
    rules = json.load(open(map_path)).get("rules", [])
    if not rules:
        sys.exit(0)

    con = duckdb.connect(db)
    rows = con.execute(
        "SELECT DISTINCT c.product, c.usage_type "
        "FROM aws_li_to_gcp_li m JOIN aws_li_catalog c USING(aws_li_key)"
    ).fetchall()

    # Skip comment/divider entries that carry no "match" key.
    rules = [r for r in rules if r.get("match")]

    cs_rule_cache = {}
    review_updates, keep_updates, ignore_updates = [], [], []
    stats = {"review": 0, "keep": 0, "ignore": 0, "unmatched": 0}
    for (product, usage_type) in rows:
        cs_product = canonical_service(product)
        rule = next(
            (r for r in rules if _rule_matches(r, product, usage_type, cs_product, cs_rule_cache)),
            None,
        )
        if not rule:
            stats["unmatched"] += 1
            continue
        note = (f"[{rule['category']}] {rule['reason']} "
                f"(rule={RULE_TAG}, gcp={rule['gcp_service']})")
        mode = rule["mode"]
        # service_map.json confidences are on a 0-100 scale; mapping_confidence
        # everywhere else is 0-1. Averaging the two scales made the report show
        # "Avg Confidence 2399.6%". Normalize at write time.
        conf = rule.get("confidence", 60 if mode == "review" else 90 if mode == "ignore" else 80)
        if conf > 1:
            conf = conf / 100.0
        if mode == "review":
            review_updates.append((rule["gcp_service"], conf, note, product, usage_type))
            stats["review"] += 1
        elif mode == "ignore":
            ignore_updates.append((rule["gcp_service"], conf, note, product, usage_type))
            stats["ignore"] += 1
        else:  # keep
            keep_updates.append((conf, note, note, product, usage_type))
            stats["keep"] += 1

    # usage_type may be NULL (PDF-ingested bills); match it with IS NOT DISTINCT FROM
    # so NULL == NULL rather than SQL's usual NULL != NULL.
    if review_updates:
        con.executemany(
            """
            UPDATE aws_li_to_gcp_li SET
                gcp_service = ?, mapping_confidence = ?, projection_note = ?,
                strategy = 'passthrough', gcp_sku_id = NULL, gcp_sku_name = NULL
            WHERE aws_li_key IN (
                SELECT aws_li_key FROM aws_li_catalog
                WHERE product = ? AND usage_type IS NOT DISTINCT FROM ?)
            """,
            review_updates,
        )
    if ignore_updates:
        con.executemany(
            """
            UPDATE aws_li_to_gcp_li SET
                gcp_service = ?, mapping_confidence = ?, projection_note = ?,
                strategy = 'ignore', gcp_sku_id = NULL, gcp_sku_name = NULL
            WHERE aws_li_key IN (
                SELECT aws_li_key FROM aws_li_catalog
                WHERE product = ? AND usage_type IS NOT DISTINCT FROM ?)
            """,
            ignore_updates,
        )
    if keep_updates:
        # 'keep' never touches strategy/gcp_sku_id, but it used to unconditionally
        # overwrite projection_note with the rule's generic reason text — clobbering
        # a mapper's own explicit "unpriced"/"no SKU found"/"service model change"
        # note with wording that implies the row was priced normally against GCP
        # rates at the same operational tier. Preserve any note that already flags
        # an unresolved, unpriced, or cross-tier-substitution state instead of
        # stamping over it.
        #
        # mapping_confidence had the same clobbering bug, just less visible: a flat
        # overwrite discarded a mapper's own evidence-based confidence entirely —
        # confirmed real for ElastiCache, where map_elasticache() (apply_static_
        # mappings.py) computes 0.85 when deployment_option genuinely confirms
        # HA/non-HA topology vs 0.70 when it's absent (real ambiguity) — both values
        # were being flattened to this rule's flat 78%, then further capped to 72%
        # by calibrate_confidence.py, so the confirmed-vs-unclear distinction never
        # reached the report. LEAST() makes this rule's confidence an additional
        # ceiling, not a stamp: a mapper's own LOWER (more honest) confidence is
        # preserved, while an overconfident mapper still gets capped.
        con.executemany(
            """
            UPDATE aws_li_to_gcp_li SET
                mapping_confidence = LEAST(COALESCE(mapping_confidence, 1.0), ?),
                projection_note = CASE
                    WHEN projection_note ILIKE '%unpriced%' OR projection_note ILIKE '%no rate%'
                      OR projection_note ILIKE '%service model change%'
                      OR projection_note ILIKE '%architecture review recommended%'
                      OR projection_note ILIKE '%verify with customer%'
                      -- unavailable-in-region fallback chains (e.g. "Arm Autopilot
                      -- pods unavailable in asia-south1 — x86 pricing used instead;
                      -- real cost may differ") were silently clobbered by this same
                      -- rule before this was added — confidence correctly reflected
                      -- the fallback (LEAST() ceiling) but the note explaining WHY
                      -- was lost, replaced by the generic category note. Same bug
                      -- class already fixed for ElastiCache; this whitelist was
                      -- just narrower than the set of honest-fallback phrasings
                      -- mappers across this file actually use.
                      OR projection_note ILIKE '%unavailable in%'
                      OR projection_note ILIKE '%real cost may differ%'
                      -- Preserve notes from mappers that emit their own per-operation
                      -- pricing (e.g. PutLogEvents → Cloud Logging Log Ingestion),
                      -- so the service_map generic "Multi-component" reason doesn't
                      -- overwrite a more specific, accurate SKU-level note.
                      OR projection_note ILIKE '%log ingestion%'
                    -- The mapper's own note already carries real, row-specific
                    -- evidence (e.g. the actual RAM/tier picked, or its own
                    -- verify-with-customer flag) — append the rule's note rather
                    -- than picking one or the other, since the rule may carry a
                    -- genuinely distinct caveat (e.g. cluster-mode vs Multi-AZ)
                    -- that the mapper's own note doesn't cover.
                    THEN projection_note || ' ' || ?
                    ELSE ?
                END
            WHERE aws_li_key IN (
                SELECT aws_li_key FROM aws_li_catalog
                WHERE product = ? AND usage_type IS NOT DISTINCT FROM ?)
            """,
            keep_updates,
        )

    print(f"service_classifier: {stats['review']} review-routed, {stats['ignore']} ignored ($0), "
          f"{stats['keep']} kept, {stats['unmatched']} unmatched product(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
