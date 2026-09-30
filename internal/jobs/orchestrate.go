package jobs

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"

	"github.com/facets/cur-web/internal/config"
)

// Phase-by-phase orchestration.
//
// Instead of handing agy ONE prompt and letting the skill self-orchestrate all
// six phases inside a single long-lived agent context (which Gemini executes
// unreliably — it improvises, mis-picks SKUs, drops CUD coverage, over-projects),
// we drive the phases from a small Python orchestrator (run_all.py). It invokes
// `agy` once per phase with a tight, single-purpose prompt — a FRESH agy
// conversation each time, so the model only ever holds one phase's worth of
// context. Between phases a deterministic DuckDB gate runs; if it fails, the
// phase is re-run once with the offending rows named in the prompt.
//
// Phase 2 still launches the skill's 4–5 parallel mapping sub-agents — that
// fan-out happens INSIDE the single Phase-2 agy call and is unchanged.
//
// The watcher tracks the orchestrator process as one PID and waits for the
// report (run_all.py is already one of its liveness signals).

type phaseSpec struct {
	Num       int    `json:"num"`
	Name      string `json:"name"`
	Activity  string `json:"activity"`
	Prompt    string `json:"prompt,omitempty"`
	Script    string `json:"script,omitempty"`
	PreScript string `json:"pre_script,omitempty"`
	// PreLLMScripts / PostLLMScripts: deterministic scripts the orchestrator runs
	// BEFORE / AFTER spawning agy for this phase. Each entry is a script path
	// relative to SKILL_DIR; args are space-separated after the path. "$DB" is
	// substituted with the DB path. These scripts run as direct Python subprocesses
	// (no agy, no LLM tokens).
	PreLLMScripts  []string `json:"pre_llm_scripts,omitempty"`
	PostLLMScripts []string `json:"post_llm_scripts,omitempty"`
	// OnFailurePreScript / OnFailurePrompt: LLM fallback for a deterministic
	// Script phase. If Script fails, OnFailurePreScript (zero-token, builds a
	// bounded preview/manifest of the raw input for the LLM) runs, then agy
	// runs OnFailurePrompt once, then Script is retried exactly once. Only if
	// that retry also fails does the phase hard-fail as usual. Used by Phase 1
	// so bill formats the deterministic parsers don't recognize (multi-sheet
	// workbooks, multi-section reports, non-standard column layouts) still
	// have a path to ingestion instead of an unconditional reject.
	OnFailurePreScript string `json:"on_failure_pre_script,omitempty"`
	OnFailurePrompt    string `json:"on_failure_prompt,omitempty"`
	// CheckName/CheckSQL: a deterministic gate run AFTER the phase's agy call.
	// CheckSQL must return a single integer that is 0 when healthy (>0 = number
	// of violations). Empty CheckSQL skips the gate.
	CheckName string `json:"check_name,omitempty"`
	CheckSQL  string `json:"check_sql,omitempty"`
}

// phaseSpecs returns the six per-phase prompts + their deterministic gates.
func phaseSpecs(inputExt string) []phaseSpec {
	return []phaseSpec{
		{
			Num: 1, Name: "Ingestion", Activity: "Loading bill into DuckDB",
			Script: "scripts/ingest.py",
			// LLM fallback: if ingest.py rejects the file as an unrecognized
			// format (multi-sheet workbook, multi-section report, one-off column
			// layout — none of the 4 hardcoded parsers match), don't hard-fail.
			// prepare_ingest_fallback.py (zero LLM tokens) dumps a bounded preview
			// of every sheet/section to ingest_fallback_manifest.md; the LLM reads
			// ONLY that file and writes projection-audit/normalized_bill.csv in the
			// flat-CSV schema ingest.py already parses deterministically
			// (Description, Region, Usage Quantity, Amount in USD). ingest.py is
			// then re-run once and picks up that normalized CSV automatically.
			// The LLM never touches the database directly — it only produces data
			// that the same deterministic loader ingest.py already has consumes.
			OnFailurePreScript: "scripts/prepare_ingest_fallback.py",
			OnFailurePrompt: "Phase 1 fallback — Ingestion could not recognize this bill's format. " +
				"Strict protocol, no deviations.\n\n" +
				"WORKING DIRECTORY: your job's absolute directory is <<JOB_DIR>>. Every " +
				"relative path mentioned below means that path resolved against THIS " +
				"absolute directory — e.g. \"projection-audit/normalized_bill.csv\" means " +
				"<<JOB_DIR>>/projection-audit/normalized_bill.csv. Use the full absolute path " +
				"for every file read/write. This job's files live ONLY under <<JOB_DIR>>, " +
				"isolated from every other job running on this machine at the same time.\n\n" +
				"CONTEXT: scripts/ingest.py tried AWS CUR, the AWS PDF-export layout, the " +
				"Category/Meter/Cost spreadsheet layout, and the flat-CSV layout — none matched. " +
				"projection-audit/ingest_fallback_manifest.md contains a full preview of the raw " +
				"file (every sheet if it's a workbook, every line up to a cap if it's text/CSV).\n\n" +
				"YOUR ONLY JOB: read that manifest and write ONE file: " +
				"projection-audit/normalized_bill.csv\n\n" +
				"MANDATORY RULES for that CSV:\n" +
				"  • Header row EXACTLY: Description,Region,Usage Quantity,Amount in USD\n" +
				"  • One row per REAL granular usage/cost line item — e.g. one EC2 instance type, " +
				"one EBS volume type, one RDS component, one GuardDuty sub-metric, etc.\n" +
				"  • NEVER emit rows for summary/rollup/total sections (e.g. a 'Billing Summary' or " +
				"'Spend by Service' sheet that just totals other sheets) — that double-counts. Only " +
				"the most granular breakdown available for each charge should produce a row.\n" +
				"  • Description must embed the AWS service name AND the resource/instance/operation " +
				"detail so downstream classification still works, e.g. " +
				"\"EC2 - On Demand Linux t3a.small Instance Hour\", \"EBS - gp3 Storage\", " +
				"\"RDS - db.m5.xlarge Multi-AZ Instance Hour\", \"GuardDuty - Kubernetes Audit Logs\".\n" +
				"  • Usage Quantity must be a bare number (strip commas/units like 'GB-month', 'hours', " +
				"'events' — put the unit in Description instead, not in the quantity column).\n" +
				"  • Amount in USD must be the USD line cost for that row (convert from another " +
				"currency first if the source file's granular rows are only given in a non-USD " +
				"currency — use the file's own stated exchange rate/total if present, otherwise use " +
				"the already-USD columns if both are given).\n" +
				"  • Region is optional — leave blank if the file doesn't state it per-row.\n" +
				"  • Do not invent numbers. Every row must trace to an actual value in the manifest.\n" +
				"  • Do NOT read or write projection.duckdb. Do NOT read the raw input file directly — " +
				"the manifest is the complete source of truth. Do NOT use search_web.\n" +
				"  • STOP after writing projection-audit/normalized_bill.csv. Nothing else.",
			CheckName: "ingestion_nonempty",
			CheckSQL:  "SELECT (count(*)=0)::int FROM aws_li_catalog",
		},
		{
			Num: 2, Name: "Mapping", Activity: "Mapping AWS line items to GCP",
			// Deterministic scripts run by the orchestrator BEFORE agy starts.
			// classify_mechanics.py stamps mechanic_group + writes phase2_manifest.json.
			// apply_commitment_ignores.py + apply_static_mappings.py write their
			// mapping files. agy is spawned ONLY for the three LLM groups.
			PreLLMScripts: []string{
				"scripts/classify_mechanics.py $DB",
				"?scripts/apply_commitment_ignores.py $DB",
				"?scripts/apply_static_mappings.py $DB",
				"?scripts/family_mapper.py $DB",
			},
			// Post-merge deterministic passes (zero LLM tokens):
			//   1. merge_mappings.py  — bulk-INSERT all group files into aws_li_to_gcp_li
			//   2. calibrate_confidence.py — apply service-specific confidence ceilings
			//      (OpenSearch 70%, MSK 70%, RDS 72%, Windows 75%) and add architecture notes
			//   3. reconcile_capacity.py — upsize any break_down rows where GCP vCPU/RAM
			//      < AWS spec to enforce the never-underprovision guarantee
			PostLLMScripts: []string{
				"scripts/merge_mappings.py $DB projection-audit/mappings",
				// Service Classification Engine: force the canonical GCP target per
				// data/service_map.json so mappings are deterministic, not per-row
				// LLM guesses (CloudTrail->Cloud Logging, EMR->Dataproc, etc.).
				"?scripts/service_classifier.py $DB",
				// Generic backstop: reroute any OTHER non-object-storage service the
				// LLM dropped onto Cloud Storage to passthrough + manual-review flag.
				"?scripts/fix_storage_misroute.py $DB",
				"?scripts/calibrate_confidence.py $DB",
				"?scripts/reconcile_capacity.py $DB",
				"?scripts/verify_golden_mappings.py $DB",
			},
			Prompt: "Phase 2 — LLM mapping only. Strict protocol, no deviations.\n\n" +
				"WORKING DIRECTORY: your job's absolute directory is <<JOB_DIR>>. Every\n" +
				"relative path mentioned anywhere below (projection-audit/..., scripts/...,\n" +
				"mapping-notes.md, etc.) means that path resolved against THIS absolute\n" +
				"directory — e.g. \"projection-audit/mapping-notes.md\" means\n" +
				"<<JOB_DIR>>/projection-audit/mapping-notes.md. Use the full absolute path for\n" +
				"every file read, file write, and script invocation. Do not rely on your own\n" +
				"working-directory assumption or any shared scratch location — this job's files\n" +
				"live ONLY under <<JOB_DIR>>, isolated from every other job running on this\n" +
				"machine at the same time.\n\n" +
				"SETUP (already done by orchestrator — DO NOT re-run):\n" +
				"  • classify_mechanics.py ran → projection-audit/phase2_manifest.json exists\n" +
				"  • apply_commitment_ignores.py ran → commitment_discount_mappings.json exists\n" +
				"  • apply_static_mappings.py ran → flat_hourly/object_storage/per_request mappings exist\n\n" +
				"YOUR ONLY JOB: map the 3 LLM groups by reading the manifest.\n\n" +
				"MANDATORY STEPS — follow exactly in this order:\n" +
				"1. Read projection-audit/phase2_manifest.json.\n" +
				"   The manifest contains a \"_meta\" key with {\"output_dir\": \"projection-audit/mappings\"}.\n" +
				"   USE THAT output_dir value exactly as-is — it is a relative path from your working directory.\n" +
				"   DO NOT query projection.duckdb directly. The manifest is the single source of truth.\n" +
				"2. For each group in [compute_breakdown, managed_db, misc]:\n" +
				"   a. Read that group's rows from the manifest (manifest[group][\"rows\"]).\n" +
				"   b. Map ALL rows to GCP. Use scripts/find-sku.sh only to look up SKU IDs — no inline DuckDB queries.\n" +
				"   c. Build a JSON array of mapping objects (schema: aws_li_key, gcp_service, gcp_sku_id,\n" +
				"      gcp_sku_name, component, strategy, unit_multiplier, gcp_region, projection_note,\n" +
				"      mapping_confidence, is_workload, break_down).\n" +
				"   d. Write the array to {output_dir}/<group>_mappings.json — use the RELATIVE output_dir from _meta.\n" +
				"      NEVER construct an absolute path. NEVER write outside the current working directory.\n" +
				"   e. One file per group, one write per file.\n" +
				"3. Write projection-audit/mapping-notes.md with a brief summary of decisions.\n\n" +
				"RULES:\n" +
				"  • Same-tier cost optimization (compute rows): GCP often has several machine\n" +
				"    families that meet the same vCPU/RAM/guarantee at different prices. This is\n" +
				"    EXACTLY the procedure scripts/apply_static_mappings.py's cheapest_in_scope()\n" +
				"    runs deterministically — follow it by hand, in this order. Do NOT rely on a\n" +
				"    remembered list of family names — GCP adds/renames/retires families over time,\n" +
				"    and a memorized list goes stale the same way a hardcoded one in code would; you\n" +
				"    must discover the real, current candidate set from the catalog itself:\n" +
				"      1. Determine the AWS source's tier (burstable t2/t3/t3a/t4g vs sustained —\n" +
				"         everything else) and workload (general m/t-family vs compute-optimized\n" +
				"         c-family vs memory-optimized r-family, which stays in the general-purpose\n" +
				"         GCP pool — GCP's M-series has large fixed minimum sizes unsuited to\n" +
				"         typical r5.large-style requests).\n" +
				"      2. Discover EVERY real GCP family and its live rate for THIS row's region in\n" +
				"         one call: `scripts/find-sku.sh --service \"Compute Engine\" --region <region>\n" +
				"         --resource-group CPU --keyword \"Instance Core\"` (and the same with \"Instance\n" +
				"         Ram\") — this returns every family that actually exists and resolves in this\n" +
				"         region, with its real rate, in one shot. Do not ask about one family at a\n" +
				"         time from memory; this single broad query IS the discovery step.\n" +
				"      3. From that real result set, classify each returned family by tier/workload/\n" +
				"         network-tier so you only compare within scope:\n" +
				"           - burstable: E2 only. Everything else returned is sustained.\n" +
				"           - workload: general-purpose line includes E2/N1 Predefined/N2/N2D AMD/\n" +
				"             T2D AMD/N4/N4D/N4A/T2A Arm; compute-optimized line includes C2D AMD/C3/\n" +
				"             C3D/C4/C4D/C4N/C4A Arm; memory-optimized includes M3/M4/M4Ultramem224;\n" +
				"             storage-optimized includes Z3. NEVER cross workload lines even if\n" +
				"             cheaper — a compute-optimized AWS source stays in the compute-optimized\n" +
				"             line only, same rule as tier.\n" +
				"           - network tier: E2/T2A are standard-networking; every other sustained\n" +
				"             family is high-networking. If the AWS source is an EFA/HPC-class\n" +
				"             instance (p4d/p4de/p5/hpc6a/hpc6id/hpc7a/hpc7g/c5n/m5n/m5dn/r5n/r5dn),\n" +
				"             it must only compare against high-networking families, even if a\n" +
				"             standard-tier family is cheaper — that would silently drop the\n" +
				"             networking guarantee the AWS instance was chosen for.\n" +
				"         (Ignore any Sole Tenancy/Custom/Committed Use Discount/Spot Preemptible/\n" +
				"         Confidential Computing variant rows returned by the query — those are a\n" +
				"         different commercial mechanism, not a distinct family choice.)\n" +
				"      4. Among the in-scope candidates that resolved with a real rate, compare their\n" +
				"         $/vCPU and $/GB-RAM for THIS row's region — not a rate you recall from a\n" +
				"         different region, and not just \"cheaper than the AWS price\" (a different,\n" +
				"         unrelated check). Pick the genuine minimum total cost\n" +
				"         (vCPU x core_rate + RAM_GiB x ram_rate).\n" +
				"      5. State which family won AND why in projection_note — \"X cheaper than Y\n" +
				"         here\" if the default was priced but lost, or \"default unavailable in\n" +
				"         region, X used instead\" if it never resolved at all. These read\n" +
				"         differently — a fallback picked for lack of any alternative is not a\n" +
				"         cost optimization and must not be worded like one.\n" +
				"    If the AWS source instance is burstable (t2/t3/t3a/t4g), this is always safe in\n" +
				"    the sustained direction: AWS CPU credits never lower the AWS price, so swapping\n" +
				"    to a cheaper sustained GCP family is a pure win, never a downgrade.\n" +
				"    NEVER do the reverse: if the AWS source is sustained (m5/c5/r5/m7g/m6g/r7g/etc.\n" +
				"    — anything that is NOT t2/t3/t3a/t4g), you may only compare among sustained GCP\n" +
				"    families in the same workload line — never map it to E2 even if E2 is cheaper,\n" +
				"    since that silently changes the performance guarantee the customer already paid for.\n" +
				"  • This applies to EVERY row you map by hand — including MSK/Kafka brokers, EMR,\n" +
				"    or any other service where you pick a self-managed Compute Engine family as the\n" +
				"    target, not just plain EC2 rows. A row falling to your judgment instead of a\n" +
				"    deterministic script is not an excuse to skip this — it is exactly when it\n" +
				"    matters most, since no downstream script re-checks your family choice.\n" +
				"  • If find-sku.sh finds NO SKU for the target family/region: do not silently write\n" +
				"    a passthrough with a generic note as if it were priced normally. State plainly in\n" +
				"    projection_note that no rate was found for that specific region and the row is\n" +
				"    carried through at AWS cost, NOT a real GCP estimate — the report must never look\n" +
				"    identical for a genuinely-priced row and an unpriced fallback.\n" +
				"  • Never-passthrough: EC2/RDS/Aurora/ElastiCache/EBS/DataTransfer/ELB/S3 must be mapped.\n" +
				"  • Total passthrough must stay under 5% of AWS cost.\n" +
				"  • PASSTHROUGH = ONE ROW ONLY: if a service has no GCP equivalent, emit EXACTLY ONE\n" +
				"    row with strategy='passthrough' and break_down=false. NEVER split a passthrough\n" +
				"    into core+ram or any components — each component independently returns the full\n" +
				"    AWS cost, so N components = N× cost multiplication. This is always a bug.\n" +
				"  • break_down=true is ONLY valid when strategy='map' or 'break_down' with a real\n" +
				"    gcp_sku_id. break_down=true combined with strategy='passthrough' is always wrong.\n" +
				"  • DO NOT run merge_mappings.py — the orchestrator runs it after you finish.\n" +
				"  • DO NOT query projection.duckdb with python3 -c or run_command. Read manifest only.\n" +
				"  • DO NOT use search_web or any web browsing tool — all SKU lookups must use scripts/find-sku.sh only.\n" +
				"  • STOP after writing the 3 _mappings.json files and mapping-notes.md. Nothing else.",
			CheckName: "mapping_coverage",
			CheckSQL: "SELECT count(*) FROM aws_li_catalog c WHERE NOT EXISTS " +
				"(SELECT 1 FROM aws_li_to_gcp_li m WHERE m.aws_li_key = c.aws_li_key)",
		},
		{
			Num: 3, Name: "Review", Activity: "Verifying mappings",
			// auto_review.py is a pure suggestion engine: detects illegal passthroughs
			// and spec violations, pre-computes candidate fixes, writes review_flags.md
			// (for LLM) and review_candidates.json (for apply_review_fixes.py).
			// It NEVER modifies the database. Soft (?) so a crash here skips review
			// but still allows the report to be generated.
			PreLLMScripts: []string{"?scripts/auto_review.py"},
			// apply_review_fixes.py reads review_fixes.json from the LLM and
			// review_candidates.json from auto_review.py, then applies confirm/override/veto
			// decisions with schema validation. It is the ONLY script that writes to the DB.
			PostLLMScripts: []string{"?scripts/apply_review_fixes.py"},
			Prompt: "Phase 3 — Review. Strict protocol, no deviations.\n\n" +
				"WORKING DIRECTORY: your job's absolute directory is <<JOB_DIR>>. Every\n" +
				"relative path mentioned below (review_flags.md, review_fixes.json, etc.) means\n" +
				"that path resolved against THIS absolute directory — e.g. \"review_flags.md\"\n" +
				"means <<JOB_DIR>>/review_flags.md. Use the full absolute path for every file\n" +
				"read/write and script invocation. This job's files live ONLY under <<JOB_DIR>>,\n" +
				"isolated from every other job running on this machine at the same time.\n\n" +
				"SETUP (already done by orchestrator):\n" +
				"  • auto_review.py ran → review_flags.md lists every flagged row with a pre-computed\n" +
				"    candidate fix and a confidence label (HIGH / LOW / NONE).\n\n" +
				"YOUR ONLY JOB: read review_flags.md and write review_fixes.json.\n\n" +
				"MANDATORY STEPS — follow exactly in this order:\n" +
				"1. Read review_flags.md. This is the ONLY input you need.\n" +
				"   DO NOT query the database. DO NOT re-scan mappings. DO NOT run SQL.\n" +
				"2. For each flagged aws_li_key, decide:\n" +
				"   • Read the row's existing projection_note FIRST, if one is shown. It is the\n" +
				"     deterministic mapper's own stated reason for the choice it made — a region\n" +
				"     rate comparison, a fallback because a preferred SKU wasn't sold in that\n" +
				"     region, an explicit \"no rate found\" disclosure, etc. Being flagged for\n" +
				"     review does NOT mean the existing mapping is wrong — it means it looked\n" +
				"     unusual enough to warrant a second pair of eyes. Engage with the actual\n" +
				"     stated reasoning before deciding; do not override just because a number\n" +
				"     looks surprising if the note already explains why it's correct.\n" +
				"   • HIGH confidence candidate: confirm unless something is visibly wrong.\n" +
				"   • LOW confidence candidate: verify it makes sense; override ONLY if you can\n" +
				"     identify a concrete flaw in the existing projection_note's reasoning (wrong\n" +
				"     region, wrong unit, wrong service) — not merely because you'd have guessed\n" +
				"     differently without that context.\n" +
				"   • NONE (no candidate): reason from product/usage_type, supply gcp_sku_id + gcp_sku_name.\n" +
				"3. Write review_fixes.json — a JSON array, one object per flagged row:\n" +
				"   [{\"aws_li_key\": \"...\", \"decision\": \"confirm|override|veto\",\n" +
				"     \"gcp_sku_id\": \"...\", \"gcp_sku_name\": \"...\",\n" +
				"     \"unit_multiplier\": 4.0, \"component\": \"core\", \"reason\": \"...\"}]\n" +
				"   confirm  → apply the pre-computed candidate as-is\n" +
				"   override → supply your own values (include gcp_sku_id+gcp_sku_name and/or unit_multiplier)\n" +
				"   veto     → leave unchanged (document why in reason)\n\n" +
				"RULES:\n" +
				"  • DO NOT run any SQL or touch the database — apply_review_fixes.py handles all writes.\n" +
				"  • DO NOT write mapping files or call any other scripts.\n" +
				"  • DO NOT use search_web or any web browsing tool — review_flags.md has all context you need.\n" +
				"  • ONLY emit decisions for aws_li_keys that appear in review_flags.md. Do not invent a\n" +
				"    row or a fix for anything not explicitly flagged there, even if something else looks\n" +
				"    wrong — that row is out of scope for this phase. apply_review_fixes.py rejects and\n" +
				"    ignores any decision for a key outside the flagged set.\n" +
				"  • unit_multiplier means QUANTITY CONVERSION ONLY (vCPU count, RAM GiB, etc.).\n" +
				"    Never set it to aws_rate/gcp_rate to force cost parity — that is always wrong.\n" +
				"    For storage rows unit_multiplier must be 1.0. If GCP costs more, that is correct.\n" +
				"  • If overriding a compute row's SKU/family: same procedure as Phase 2 —\n" +
				"    determine tier (burstable t2/t3/t3a/t4g vs sustained) and workload (general\n" +
				"    vs compute-optimized c-family vs memory-optimized, which stays general-\n" +
				"    purpose). Do NOT compare against a remembered family list — run\n" +
				"    `scripts/find-sku.sh --service \"Compute Engine\" --region <region>\n" +
				"    --resource-group CPU --keyword \"Instance Core\"` (and \"Instance Ram\") to get\n" +
				"    every real family and its live rate in this region in one call, classify each\n" +
				"    by tier/workload/network-tier (same rules as Phase 2), then pick the genuine\n" +
				"    cheapest among the in-scope candidates that actually resolved — not a family\n" +
				"    you recall being cheap elsewhere. A burstable AWS source (t2/t3/t3a/t4g) may\n" +
				"    switch to any cheaper same-or-better GCP family — that's always safe, AWS CPU\n" +
				"    credits never lower the AWS price. A sustained AWS source (m5/c5/r5/m7g/etc.)\n" +
				"    may only switch among sustained GCP families in the SAME workload line — never\n" +
				"    to a burstable family, and never crossing general-purpose <-> compute-optimized\n" +
				"    <-> memory-optimized <-> storage-optimized, even if the other line is cheaper,\n" +
				"    since that silently changes the performance guarantee. An EFA/HPC-class AWS\n" +
				"    source (p4d/p4de/p5/hpc6a/hpc6id/hpc7a/hpc7g/c5n/m5n/m5dn/r5n/r5dn) must only\n" +
				"    switch among high-networking families, never to a standard-networking family\n" +
				"    (E2/T2A) even if cheaper.\n" +
				"  • If you cannot find a real GCP SKU/rate for a row: don't override it with a\n" +
				"    guessed SKU or a generic note that implies it was priced. Veto it and state\n" +
				"    plainly that no rate was found for that region — a report must never look the\n" +
				"    same for a genuinely-priced row and an unpriced fallback.\n" +
				"  • STOP after writing review_fixes.json. Nothing else.",
			CheckName: "no_illegal_passthroughs",
			// Only flag services with clear, direct GCP equivalents as illegal passthroughs.
			// Excluded (legitimately passthrough): CloudWatch (pricing model incompatible),
			// Lambda (memory_size_mb missing from CUR), Data Transfer (multi-directional),
			// NAT Gateway (multi-component, maps to Cloud NAT passthrough).
			CheckSQL: "SELECT count(*) FROM aws_li_catalog c " +
				"JOIN aws_li_to_gcp_li m USING(aws_li_key) " +
				"WHERE m.strategy = 'passthrough' " +
				"AND (c.product ILIKE '%Elastic Compute Cloud%' " +
				"OR c.product ILIKE '%Elastic Block Store%' " +
				"OR c.product ILIKE '%Relational Database%' " +
				"OR c.product ILIKE '%Aurora%' " +
				"OR c.product ILIKE '%ElastiCache%' " +
				"OR c.product ILIKE '%Simple Storage%' " +
				"OR c.product ILIKE '%Load Balanc%') " +
				"AND c.product NOT ILIKE '%NatGateway%' " +
				"AND c.product NOT ILIKE '%Nat:%'",
		},
		{
			Num: 4, Name: "Rate-Card Fill", Activity: "Fetching GCP rates",
			// ensure_catalog_coverage.py is soft: if the catalog fetch fails, apply_rates.py
			// falls back to cached resolved_skus.json entries (99% hit rate after warmup).
			// A crash here should not prevent report generation.
			PreLLMScripts: []string{"?scripts/ensure_catalog_coverage.py"},
			Script:        "?scripts/apply_rates.py",
			// After rates load, run the deterministic autofixer (regional-SKU
			// repair, CUD synthesis, per-N clamping, illegal-passthrough repair)
			// BEFORE the gate evaluates. Previously this only ran read-only in
			// the watcher, so its repairs never applied in-pipeline (D4).
			PostLLMScripts: []string{"?scripts/validate_fix.py $JOBDIR"},
			CheckName:      "no_null_projected_cost",
			// validate_fix.py already computes null_gcp_service and gcp_service_echo
			// as hard violations in validation_report.json, but it's invoked as a
			// SOFT ('?') script above — its own exit(1) is logged and thrown away,
			// and no later phase gate re-checked either condition, so a passthrough
			// row with gcp_service left NULL (or echoing the AWS product name back)
			// sailed all the way into the final report unlabeled/unjustified.
			// Folding both checks into this enforced CheckSQL is what actually
			// blocks the job on them, instead of only logging a violation nobody reads.
			CheckSQL: "SELECT " +
				"(SELECT count(*) FROM gcp_projection WHERE strategy IN ('map','break_down') " +
				"AND gcp_projected_cost IS NULL AND aws_amortized_cost > 1) + " +
				"(SELECT count(*) FROM gcp_projection WHERE is_workload AND strategy NOT IN ('ignore') " +
				"AND (gcp_service IS NULL OR TRIM(gcp_service) = '') AND aws_amortized_cost > 1) + " +
				"(SELECT count(*) FROM gcp_projection WHERE is_workload AND strategy NOT IN ('ignore') " +
				"AND gcp_service IS NOT NULL AND (LOWER(gcp_service) LIKE 'amazon %' " +
				"OR LOWER(gcp_service) LIKE 'aws %' OR LOWER(gcp_service) LIKE 'amazon%') " +
				"AND aws_amortized_cost > 5)",
		},
		{
			Num: 5, Name: "Outlier Triage", Activity: "Running outlier queries",
			// detect_outliers.py: splits output into structural_outliers.md + pricing_outliers.md,
			// writes outliers_data.json, and fails hard if total rows > 20 (systematic mapper bug).
			// auto_triage.py: pure suggestion engine — reads outliers_data.json, computes
			// candidates for structural rows (D/E/G/B/C/H/I), enriches pricing rows (A1/A2/F)
			// with context only (no candidate — word-overlap re-resolution caused Glacier 120x).
			// Writes triage_suggestions.md (for LLM) and triage_candidates.json (for apply script).
			// incremental_rerate.py before LLM: ensures LLM reasons over fresh costs.
			PreLLMScripts: []string{
				"?scripts/ensure_catalog_coverage.py",
				"?scripts/detect_outliers.py",
				"?scripts/auto_triage.py",
				"?scripts/incremental_rerate.py",
			},
			// apply_outlier_fixes.py: single application point — reads outlier_fixes.json
			// (LLM output) + triage_candidates.json, applies confirm/override/veto with
			// schema validation. LLM never touches the DB directly.
			// incremental_rerate.py after LLM: fills rates for new SKU IDs introduced by fixes.
			// outlier_gate.py: hard gate unchanged.
			PostLLMScripts: []string{
				"?scripts/apply_outlier_fixes.py",
				"?scripts/incremental_rerate.py",
				"?scripts/validate_fix.py $JOBDIR",
				"?scripts/outlier_gate.py $DB",
			},
			Prompt: "Phase 5 — Outlier Triage. Strict protocol, no deviations.\n\n" +
				"WORKING DIRECTORY: your job's absolute directory is <<JOB_DIR>>. Every\n" +
				"relative path mentioned below (triage_suggestions.md, outlier_fixes.json, etc.)\n" +
				"means that path resolved against THIS absolute directory — e.g.\n" +
				"\"outlier_fixes.json\" means <<JOB_DIR>>/outlier_fixes.json. Use the full\n" +
				"absolute path for every file read/write and script invocation. This job's\n" +
				"files live ONLY under <<JOB_DIR>>, isolated from every other job running on\n" +
				"this machine at the same time.\n\n" +
				"SETUP (already done by orchestrator):\n" +
				"  • detect_outliers.py ran → structural_outliers.md + pricing_outliers.md\n" +
				"  • auto_triage.py ran → triage_suggestions.md with pre-computed candidates\n\n" +
				"YOUR ONLY JOB: read triage_suggestions.md and write outlier_fixes.json.\n\n" +
				"MANDATORY STEPS — follow exactly in this order:\n" +
				"1. Read triage_suggestions.md. This is the ONLY input you need.\n" +
				"   DO NOT query the database. DO NOT run SQL. DO NOT re-run outlier queries.\n" +
				"2. For each aws_li_key in triage_suggestions.md, decide:\n" +
				"   STRUCTURAL rows (D/E/G/B/C/H/I):\n" +
				"   • HIGH confidence candidate: confirm unless something is visibly wrong.\n" +
				"   • LOW confidence candidate: validate carefully; override if it looks wrong.\n" +
				"   • NONE (no candidate): reason from the context provided, supply fix fields.\n" +
				"   PRICING rows (A1/A2/F — no candidate provided):\n" +
				"   • Use the current gcp_sku_name, ratio, and context to reason about the fix.\n" +
				"   • If unit_multiplier is wrong: provide the correct value.\n" +
				"   • If SKU is wrong tier: supply correct gcp_sku_id + gcp_sku_name. For compute\n" +
				"     rows this MUST follow the same procedure as Phase 2: determine tier\n" +
				"     (burstable vs sustained) and workload (general vs compute-optimized\n" +
				"     c-family vs memory-optimized, which stays general-purpose), list every\n" +
				"     allowed family in that scope — do NOT compare against a remembered family\n" +
				"     list, run `scripts/find-sku.sh --service \"Compute Engine\" --region <region>\n" +
				"     --resource-group CPU --keyword \"Instance Core\"` (and \"Instance Ram\") to get\n" +
				"     every real family and its live rate in this region in one call, classify each\n" +
				"     by tier/workload/network-tier (same rules as Phase 2), then pick the genuine\n" +
				"     cheapest among what actually resolved. A burstable AWS source (t2/t3/t3a/t4g)\n" +
				"     may switch to any cheaper same-or-better GCP family; a sustained AWS source\n" +
				"     (m5/c5/r5/m7g/etc.) may only switch among sustained GCP families in the SAME\n" +
				"     workload line — never to a burstable family, and never crossing general-\n" +
				"     purpose <-> compute-optimized <-> memory-optimized <-> storage-optimized, even\n" +
				"     if the other line is cheaper, since that silently changes the performance\n" +
				"     guarantee. An EFA/HPC-class AWS source (p4d/p4de/p5/hpc6a/hpc6id/hpc7a/hpc7g/\n" +
				"     c5n/m5n/m5dn/r5n/r5dn) must only switch among high-networking families, never\n" +
				"     to a standard-networking family (E2/T2A) even if cheaper.\n" +
				"   • If you cannot determine the fix: veto and document as rate gap. Never write a\n" +
				"     gcp_sku_id/gcp_sku_name you couldn't actually verify has a rate in this row's\n" +
				"     region — a made-up SKU that silently falls back to AWS cost is worse than an\n" +
				"     honest veto, because it looks priced when it isn't.\n" +
				"   ABSOLUTE RULE: strategy='passthrough' means GCP has no equivalent service.\n" +
				"   Never use passthrough to resolve a missing rate or NULL projected cost.\n" +
				"   CRITICAL — unit_multiplier rules:\n" +
				"   • unit_multiplier means QUANTITY CONVERSION ONLY (e.g. vCPU count, RAM GiB).\n" +
				"     It is NEVER a cost-adjustment knob. Do NOT set it to aws_rate/gcp_rate to\n" +
				"     force cost parity — that is always wrong.\n" +
				"   • For storage rows (block_storage, EBS, EFS, S3): unit_multiplier MUST be 1.0.\n" +
				"     If GCP storage costs more than AWS, that is a legitimate price difference — veto.\n" +
				"   • For compute rows: unit_multiplier = vCPU count or RAM GiB from the instance spec.\n" +
				"     Never adjust it to match a target cost.\n" +
				"   • If a ratio looks wrong but unit_multiplier is already 1.0 and the SKU is correct,\n" +
				"     the answer is veto (rate gap), not override with a fractional multiplier.\n" +
				"3. Write outlier_fixes.json — a JSON array, one object per row:\n" +
				"   [{\"aws_li_key\": \"...\", \"decision\": \"confirm|override|veto\",\n" +
				"     \"gcp_sku_id\": \"...\", \"gcp_sku_name\": \"...\",\n" +
				"     \"unit_multiplier\": 4.0, \"component\": \"core\",\n" +
				"     \"gcp_service\": \"...\", \"gcp_region\": \"...\", \"reason\": \"...\"}]\n" +
				"   confirm  → apply the pre-computed candidate as-is\n" +
				"   override → supply your own values (include every field you want changed)\n" +
				"   veto     → leave unchanged, reason goes to rate-gaps section of mapping-notes.md\n\n" +
				"RULES:\n" +
				"  • DO NOT use search_web or any web browsing tool — EVER. Calling search_web is always wrong here.\n" +
				"  • DO NOT run any SQL or touch the database — apply_outlier_fixes.py handles all writes.\n" +
				"  • DO NOT read run_all.py, outlier_gate.py, detect_outliers.py, or any script file — your ONLY input is triage_suggestions.md.\n" +
				"  • DO NOT write to mapping-notes.md — vetoed rows are logged by apply_outlier_fixes.py.\n" +
				"  • ONLY emit decisions for aws_li_keys that appear in triage_suggestions.md. Do not invent\n" +
				"    a row or a fix for anything not listed there, even if something else looks wrong — that\n" +
				"    row is out of scope for this phase. apply_outlier_fixes.py rejects and ignores any\n" +
				"    decision for a key outside the triaged set.\n" +
				"  • STOP after writing outlier_fixes.json. Nothing else.",
			CheckName: "over_and_under_projection",
			// Under-projection zero-check uses a $10 materiality floor: a small
			// row can legitimately project to $0 on GCP (usage within a free
			// tier, e.g. Cloud Monitoring's first 150 MiB), so only a MATERIAL
			// mapped row at exactly $0 signals a real mapping/rate failure.
			CheckSQL: "SELECT (SELECT count(*) FROM gcp_projection WHERE is_workload AND strategy IN ('map','break_down') " +
				"AND aws_amortized_cost > 20 AND gcp_projected_cost > aws_amortized_cost * 3) + " +
				"(SELECT count(*) FROM gcp_projection WHERE is_workload AND strategy IN ('map','break_down') " +
				"AND aws_amortized_cost > 10 AND gcp_projected_cost IS NOT NULL AND gcp_projected_cost = 0)",
		},
		{
			Num: 6, Name: "Reporting", Activity: "Generating HTML report",
			// Deterministic, zero-token safety net that runs BEFORE the report is
			// generated — never depends on an LLM noticing something on its own.
			// Appends to outlier_violations.md (already surfaced by render_report.py)
			// rather than blocking; soft ('?') so a crash here never stops the report.
			PreLLMScripts: []string{"?scripts/final_mapping_sanity_check.py $DB"},
			Script:        "scripts/render_report.py",
		},
	}
}

// renderOrchestrator returns the run_all.py source with the phase specs baked
// in (base64-encoded JSON to dodge all shell/quoting hazards).
func renderOrchestrator(inputExt string) (string, error) {
	specs, err := json.Marshal(phaseSpecs(inputExt))
	if err != nil {
		return "", err
	}
	enc := base64.StdEncoding.EncodeToString(specs)
	return fmt.Sprintf(orchestratorTemplate, enc), nil
}

// StartOrchestrated writes run_all.py into jobDir and spawns it detached. The
// orchestrator drives the six phases; the watcher polls the returned PID and
// finalizes when the report appears (same contract as the old single-agy Start).
func (s *Spawner) StartOrchestrated(jobDir, inputExt string) (int, error) {
	script, err := renderOrchestrator(inputExt)
	if err != nil {
		return 0, fmt.Errorf("render orchestrator: %w", err)
	}
	if err := os.WriteFile(filepath.Join(filepath.Clean(jobDir), "run_all.py"), []byte(script), 0644); err != nil {
		return 0, fmt.Errorf("write run_all.py: %w", err)
	}

	pyBin := pythonBin()
	cmd := exec.Command(pyBin, "run_all.py")
	cmd.Dir = jobDir
	// The orchestrator reads the agy binary, model, duckdb binary, and skill dir
	// from the env. DUCKDB_BIN and PYTHON_BIN are resolved to absolute paths so the
	// gates never silently skip just because the detached process has a thin PATH.
	env := append(s.skillEnv(),
		"AGY_BIN="+s.agyBin(),
		"AGY_MODEL="+s.geminiModel(),
		"DUCKDB_BIN="+duckdbBin(),
		"PYTHON_BIN="+pyBin,
		"PRINT_TIMEOUT="+config.AGYPrintTimeout(),
		"TOTAL_PHASES="+strconv.Itoa(config.TotalPhases),
	)
	cmd.Env = env
	cmd.SysProcAttr = sysProcAttr

	logFile, err := os.OpenFile(filepath.Join(filepath.Clean(jobDir), "agy.log"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return 0, fmt.Errorf("open log: %w", err)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	if err := cmd.Start(); err != nil {
		logFile.Close()
		return 0, fmt.Errorf("start orchestrator: %w", err)
	}
	pid := cmd.Process.Pid
	cmd.Process.Release()
	logFile.Close() // child inherited the fd; close parent's copy to avoid leak
	return pid, nil
}

// geminiModel resolves the model alias with the same default as baseFlags.
func (s *Spawner) geminiModel() string {
	if s.cfg.AGYModel == "" {
		return config.DefaultAGYModel
	}
	return s.cfg.AGYModel
}

// pythonBin resolves an absolute path to the Python interpreter. On Windows the
// executable is "python"; on Linux/macOS it is typically "python3". Falls back
// to "python" so Windows works out of the box without any configuration.
func pythonBin() string {
	for _, name := range []string{"python3", "python"} {
		if p, err := exec.LookPath(name); err == nil {
			return p
		}
	}
	return "python"
}

// duckdbBin resolves an absolute path to the duckdb CLI so the orchestrator's
// gates work even when the detached process inherits a thin PATH. Falls back to
// the common ~/.local/bin install location, then to the bare name.
func duckdbBin() string {
	if p, err := exec.LookPath("duckdb"); err == nil {
		return p
	}
	if home, err := os.UserHomeDir(); err == nil {
		p := filepath.Join(home, ".local", "bin", "duckdb")
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return "duckdb"
}

// orchestratorTemplate is the run_all.py source. %s is the base64 JSON phase
// list. It runs agy once per phase (fresh conversation), writes progress.json
// and a heartbeat so the watcher sees liveness, runs each phase's deterministic
// DuckDB gate, and retries a phase once (with offending rows named) if its gate
// fails. agy stdout/stderr append to agy.log; agy internals go to agy-internal.log.
const orchestratorTemplate = `#!/usr/bin/env python3
import atexit, base64, json, os, subprocess, sys, threading, time, re, glob, hashlib, traceback
from collections import defaultdict

JOB_DIR   = os.getcwd()
JOB_ID    = os.path.basename(JOB_DIR)
AGY       = os.environ.get("AGY_BIN", "agy")
MODEL     = os.environ.get("AGY_MODEL", "gemini-3.5-flash")
DUCKDB    = os.environ.get("DUCKDB_BIN", "duckdb")
PYTHON    = os.environ.get("PYTHON_BIN", "python3")
# PRINT_TIMEOUT and TOTAL_PHASES are injected by the Go spawner from the
# single-source config constants; the literals here are dead fallbacks.
PRINT_TIMEOUT = os.environ.get("PRINT_TIMEOUT", "45m")
TOTAL_PHASES  = int(os.environ.get("TOTAL_PHASES", "6"))
DB        = os.path.join(JOB_DIR, "projection-audit", "projection.duckdb")
AGY_LOG   = os.path.join(JOB_DIR, "agy-internal.log")
PHASES    = json.loads(base64.b64decode("%s").decode())
END_PHASE = int(os.environ.get("END_PHASE", str(TOTAL_PHASES)))
SKILL_DIR = os.environ.get("SKILL_DIR", "")

# Single-instance lock. This exists because an agy agent has, in practice,
# invoked "python3 run_all.py" itself as a shell command mid-phase (e.g. after
# deciding on its own to "verify" the pipeline by re-running it), then deleted
# phase_checkpoint.json and done it again — an infinite self-reinvocation loop
# with no natural stopping condition, burning API calls until someone manually
# kills the process tree. This guard makes that structurally impossible: only
# one run_all.py may hold the lock for a given job dir at a time, regardless
# of who invokes it (the Go spawner, or an agent's own shell tool call).
LOCK_PATH = os.path.join(JOB_DIR, "orchestrator.lock")
_MY_PID = os.getpid()

def _pid_alive(pid):
    import platform
    if platform.system() == "Windows":
        # On Windows os.kill(pid, 0) returns True for dead processes whose handle
        # the parent still holds (via cmd.Wait). Use GetExitCodeProcess instead —
        # it returns STILL_ACTIVE (259) only when the process is actually running.
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        STILL_ACTIVE = 259
        try:
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            alive = (ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                     and exit_code.value == STILL_ACTIVE)
            ctypes.windll.kernel32.CloseHandle(handle)
            return alive
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

if os.path.exists(LOCK_PATH):
    try:
        _holder = int(open(LOCK_PATH).read().strip())
    except Exception:
        _holder = None
    if _holder and _holder != _MY_PID and _pid_alive(_holder):
        print("[orchestrator] REFUSING TO START: another run_all.py (pid %%d) is already "
              "active for this job. If this invocation came from an agy tool call rather "
              "than the job spawner, that is exactly the self-reinvocation loop this lock "
              "exists to stop — exiting immediately, no phases will run." %% _holder, flush=True)
        sys.exit(0)

with open(LOCK_PATH, "w") as _f:
    _f.write(str(_MY_PID))

def _release_lock():
    try:
        if os.path.exists(LOCK_PATH) and open(LOCK_PATH).read().strip() == str(_MY_PID):
            os.remove(LOCK_PATH)
    except Exception:
        pass

atexit.register(_release_lock)

# Resume from checkpoint if a prior partial run wrote one; fall back to env/default.
_ckpt_path = os.path.join(JOB_DIR, "phase_checkpoint.json")
try:
    _ckpt = json.load(open(_ckpt_path))
    START_PHASE = int(_ckpt.get("last_completed", 0)) + 1
    print("[orchestrator] Resuming from checkpoint: last_completed=%%d, starting at phase %%d" %%
          (_ckpt.get("last_completed", 0), START_PHASE), flush=True)
except Exception:
    START_PHASE = int(os.environ.get("START_PHASE", "1"))

# Permanent quota / unrecoverable-auth markers. RESOURCE_EXHAUSTED is
# intentionally excluded — it also fires on transient RPM/TPM rate limits
# (spiky phases like Phase 2 with multiple sub-agents). Treating a transient
# rate limit as permanent quota would write failure.txt and skip retry.
# Only markers that unambiguously mean "every future call will also fail" are
# listed here: individual daily-quota exhaustion, auth failures, and model
# unavailability. A transient RESOURCE_EXHAUSTED falls through to the normal
# phase retry path, giving the job another chance once the rate window resets.
QUOTA_MARKERS = ("Individual quota reached", "quota exhausted",
                 "model unreachable", "PERMISSION_DENIED", "UNAUTHENTICATED")

def quota_blocked(start_pos=0):
    # Only scan bytes written after start_pos so retries never see old errors.
    try:
        with open(AGY_LOG, "rb") as f:
            f.seek(start_pos)
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return None
    for m in QUOTA_MARKERS:
        if m in tail:
            return m
    return None

def log(msg):
    with open(os.path.join(JOB_DIR, "agy.log"), "a") as f:
        f.write("[orchestrator] " + msg + "\n")
        f.flush()

def write_progress(num, name, activity):
    with open(os.path.join(JOB_DIR, "progress.json"), "w") as f:
        json.dump({"phase": num, "phase_name": name, "last_activity": activity}, f)

def write_phase_checkpoint(num):
    with open(os.path.join(JOB_DIR, "phase_checkpoint.json"), "w") as f:
        json.dump({"last_completed": num, "ts": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S")}, f)

def run_script(script_cmd):
    """Run a skill script directly (no LLM). script_cmd may contain $DB / $JOBDIR.

    A leading '?' marks the script as SOFT: a non-zero exit is logged but does
    NOT fail the job. Use it for repair/validator passes (e.g. validate_fix.py
    autofix) whose success is judged by the phase gate that follows, not by
    their own exit code — a remaining-violations exit(1) there is expected.
    """
    soft = script_cmd.startswith("?")
    if soft:
        script_cmd = script_cmd[1:]
    cmd = script_cmd.replace("$DB", DB).replace("$JOBDIR", JOB_DIR)
    parts = cmd.split()
    script_path = os.path.join(SKILL_DIR, parts[0])
    full = [PYTHON, script_path] + parts[1:]
    log("[SCRIPT] " + ("(soft) " if soft else "") + " ".join(full))
    result = subprocess.run(full, cwd=JOB_DIR)
    if result.returncode != 0:
        if soft:
            log("[SCRIPT] soft script exited %%d (non-fatal): %%s" %% (result.returncode, " ".join(full)))
            return
        msg = "Script failed (exit %%d): %%s" %% (result.returncode, " ".join(full))
        log("[SCRIPT] FATAL: " + msg)
        with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
            _f.write(msg)
        sys.exit(1)

def run_agy(prompt, phase_label=""):
    # Every prompt (see the "WORKING DIRECTORY: your job's absolute directory
    # is <<JOB_DIR>>" preamble baked into each phase's prompt text) tells the
    # agent to resolve every relative path it's given against this job's real
    # absolute directory, and to use the resulting ABSOLUTE path for every
    # file read/write and script invocation. Verified directly: agy, called
    # with --add-dir JOB_DIR (below) + --dangerously-skip-permissions, reads,
    # writes, and executes scripts correctly via absolute paths with zero
    # reliance on its own internal scratch directory. This REPLACES the old
    # approach — repointing a single shared ~/.gemini/antigravity-cli/scratch/
    # symlink at whichever job was currently running, serialized behind a
    # cross-process flock, because agy would otherwise resolve the agent's
    # RELATIVE file writes into that one fixed, shared location. That meant
    # every job's LLM phases queued one-at-a-time system-wide — a real
    # throughput bottleneck once multiple customers use the product at once.
    # With absolute paths, each job's agy call touches ONLY its own directory,
    # genuinely isolated from every other concurrently-running job — no
    # shared mutable resource, so no lock is needed. If the agent ever
    # ignores the absolute-path instruction and writes to a relative path
    # anyway, that file lands in agy's own ephemeral scratch dir and is
    # simply never found — the SAME class of failure the pipeline's existing
    # phase gates (CheckSQL / PostLLMScripts requiring specific output files)
    # already catch and retry for any other reason a phase produces the
    # wrong output, so no new safety net is required for that case either.
    resolved_prompt = prompt.replace("<<JOB_DIR>>", JOB_DIR)
    t0 = time.time()
    ts_start = time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S")
    log("[LLM:START] phase=%%s ts=%%s" %% (phase_label, ts_start))
    # Heartbeat keeps agy.log mtime fresh while the model is "thinking" (quiet
    # on stdout) so the watcher's stale-timeout never kills a working phase.
    stop = threading.Event()
    def beat():
        while not stop.wait(60):
            log("heartbeat")
    hb = threading.Thread(target=beat, daemon=True); hb.start()
    args = [AGY, "--dangerously-skip-permissions", "--model", MODEL,
            "--print-timeout", PRINT_TIMEOUT,
            "--log-file", os.path.join(JOB_DIR, "agy-internal.log"),
            "--add-dir", JOB_DIR, "-p", resolved_prompt]
    # Antigravity.exe is an Electron app; ELECTRON_RUN_AS_NODE=1 puts it into
    # Node CLI mode so it behaves like the agy CLI. This is a no-op on Linux/Mac
    # where agy is a plain binary.
    agy_env = os.environ.copy()
    agy_env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        with open(os.path.join(JOB_DIR, "agy.log"), "a") as lf:
            rc = subprocess.run(args, cwd=JOB_DIR, stdout=lf, stderr=lf, env=agy_env).returncode
    finally:
        stop.set()
    elapsed = int(time.time() - t0)
    log("[LLM:END] phase=%%s elapsed=%%ds exit_code=%%d ts=%%s" %% (
        phase_label, elapsed, rc, time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S")))
    return rc

def gate(sql):
    # Returns int violations, or None if the gate could not be evaluated.
    try:
        out = subprocess.check_output([DUCKDB, DB, "-noheader", "-list", sql],
                                      text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return None

def failed_structurally():
    p = os.path.join(JOB_DIR, "failure.txt")
    return os.path.exists(p) and os.path.getsize(p) > 0

def get_new_convs(log_path, start_pos):
    # Conversation UUIDs appear in agy-internal.log (AGY's internal trace),
    # not in agy.log (which only carries skill output). Scan internal log
    # from start_pos; fall back to agy.log if internal log is absent.
    convs = []
    internal_log = os.path.join(JOB_DIR, "agy-internal.log")
    sources = [internal_log, log_path]
    job_id = JOB_DIR.split("/")[-1].lower()
    for src in sources:
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(start_pos if src == log_path else 0)
                content = f.read()
                matches = re.findall(
                    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                    content, re.IGNORECASE)
                for m in matches:
                    m = m.lower()
                    if m != job_id and m not in convs:
                        brain_path = os.path.join(
                            os.path.expanduser("~/.gemini/antigravity-cli/brain"),
                            m, ".system_generated", "logs", "transcript_full.jsonl")
                        if os.path.exists(brain_path):
                            convs.append(m)
        except Exception as e:
            log("Error reading new convs from " + src + ": " + str(e))
        if convs:
            break
    return convs

def measure_metrics(runs):
    import csv
    log("Starting metrics aggregation for " + str(len(runs)) + " phases...")
    
    # 1. Read customer name
    customer_name = "unknown"
    cust_txt = os.path.join(JOB_DIR, "customer_name.txt")
    if os.path.exists(cust_txt):
        try:
            with open(cust_txt, "r") as cf:
                customer_name = cf.read().strip().replace(" ", "_").replace("/", "_")
        except Exception:
            pass
            
    brain_dir = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    
    # Fallback to time-based if runs are empty
    if not runs:
        job_start_time = os.path.getctime(JOB_DIR) if os.path.exists(JOB_DIR) else time.time() - 7200
        all_convs = set()
        if os.path.exists(brain_dir):
            transcripts = glob.glob(os.path.join(brain_dir, "*/.system_generated/logs/transcript_full.jsonl"))
            for path in transcripts:
                if os.path.getmtime(path) >= job_start_time - 120:
                    all_convs.add(path.split("/")[-4].lower())
        runs = [{"num": 0, "name": "General/Unknown", "convs": list(all_convs)}]
        
    csv_rows_tokens = []
    csv_rows_tools = []
    
    def extract_tool_info(step):
        tool_calls = step.get("tool_calls", []) or []
        if not tool_calls:
            return None
        tc = tool_calls[0]
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}
        details = ""
        if name == "view_file":
            details = args.get("AbsolutePath", "")
        elif name == "run_command":
            details = args.get("CommandLine", "")
        elif name == "grep_search":
            details = "Query='{}' Path='{}'".format(args.get("Query", ""), args.get("SearchPath", ""))
        elif name == "list_dir":
            details = args.get("DirectoryPath", "")
        else:
            details = json.dumps(args)
        return name, details

    def get_step_details(step):
        step_type = step.get("type", "UNKNOWN")
        tool_calls = step.get("tool_calls", []) or []
        if tool_calls:
            tc = tool_calls[0]
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            if name == "view_file":
                return "view_file: {}".format(args.get("AbsolutePath", ""))
            elif name == "run_command":
                return "run_command: {}".format(args.get("CommandLine", ""))
            elif name == "grep_search":
                return "grep_search: Query='{}' Path='{}'".format(args.get("Query", ""), args.get("SearchPath", ""))
            elif name == "list_dir":
                return "list_dir: {}".format(args.get("DirectoryPath", ""))
            return "tool_call: {}".format(name)
            
        content = step.get("content", "") or ""
        thinking = step.get("thinking", "") or ""
        if step_type == "USER_INPUT":
            req = content.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()
            req_line = req.split("\n")[0] if req else ""
            return "user_request: {}".format(req_line[:120])
        elif step_type == "PLANNER_RESPONSE" and thinking:
            think_line = thinking.replace("\n", " ").strip()
            return "model_thinking: {}...".format(think_line[:120])
        clean_content = content.replace("\n", " ").strip()
        return clean_content[:120]

    for run in runs:
        phase_num = run["num"]
        phase_name = run["name"]
        
        for cid in run["convs"]:
            path = os.path.join(brain_dir, cid, ".system_generated/logs/transcript_full.jsonl")
            if not os.path.exists(path):
                continue
                
            conv_tool_counts = defaultdict(int)
            step_idx = 0
            
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            step = json.loads(line)
                        except Exception:
                            continue
                        
                        step_idx += 1
                        content = step.get("content", "") or ""
                        thinking = step.get("thinking", "") or ""
                        chars = len(content) + len(thinking)
                        
                        tool_calls = step.get("tool_calls", []) or []
                        if tool_calls:
                            chars += len(json.dumps(tool_calls))
                            
                        step_type = step.get("type", "UNKNOWN")
                        details = get_step_details(step)
                        
                        csv_rows_tokens.append({
                            "job_id": JOB_DIR.split("/")[-1],
                            "phase_number": phase_num,
                            "phase_name": phase_name,
                            "conversation_id": cid,
                            "step_index": step.get("step_index", step_idx - 1),
                            "step_type": step_type,
                            "details": details,
                            "characters": chars,
                            "estimated_tokens": chars // 4
                        })
                        
                        tool_info = extract_tool_info(step)
                        if tool_info:
                            name, details = tool_info
                            conv_tool_counts[(name, details)] += 1
            except Exception as e:
                log("Error reading transcript: " + str(e))
                continue
                
            for (name, details), count in conv_tool_counts.items():
                csv_rows_tools.append({
                    "job_id": JOB_DIR.split("/")[-1],
                    "phase_number": phase_num,
                    "phase_name": phase_name,
                    "conversation_id": cid,
                    "tool_name": name,
                    "details": details,
                    "call_count": count
                })

    try:
        # Write Token CSV
        token_csv = os.path.join(JOB_DIR, "token_usage_breakdown_{}.csv".format(customer_name))
        fields_tokens = ["job_id", "phase_number", "phase_name", "conversation_id", "step_index", "step_type", "details", "characters", "estimated_tokens"]
        with open(token_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields_tokens)
            writer.writeheader()
            writer.writerows(csv_rows_tokens)
            
        # Write Tool Call CSV
        tool_csv = os.path.join(JOB_DIR, "tool_calls_frequency_{}.csv".format(customer_name))
        fields_tools = ["job_id", "phase_number", "phase_name", "conversation_id", "tool_name", "details", "call_count"]
        with open(tool_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields_tools)
            writer.writeheader()
            writer.writerows(csv_rows_tools)
            
        log("Saved token usage breakdown to: " + token_csv)
        log("Saved tool calls frequency to: " + tool_csv)

        # Phase-level summary: wall-clock timing + aggregated token estimates per phase
        phase_summary_csv = os.path.join(JOB_DIR, "phase_metrics_summary_{}.csv".format(customer_name))
        phase_token_totals = {}
        for row in csv_rows_tokens:
            pn = row["phase_number"]
            if pn not in phase_token_totals:
                phase_token_totals[pn] = {"phase_name": row["phase_name"], "chars": 0, "tokens": 0}
            phase_token_totals[pn]["chars"] += row["characters"]
            phase_token_totals[pn]["tokens"] += row["estimated_tokens"]
        fields_summary = ["phase_num", "phase_name", "start_time", "end_time", "duration_seconds",
                          "total_chars", "total_tokens_est"]
        summary_rows = []
        seen_phases = set()
        for run in runs:
            pn = run["num"]
            seen_phases.add(pn)
            agg = phase_token_totals.get(pn, {"chars": 0, "tokens": 0})
            summary_rows.append({
                "phase_num": pn,
                "phase_name": run["name"],
                "start_time": run.get("start_time", ""),
                "end_time": run.get("end_time", ""),
                "duration_seconds": run.get("duration_seconds", ""),
                "total_chars": agg["chars"],
                "total_tokens_est": agg["tokens"],
            })
        for pn, agg in sorted(phase_token_totals.items()):
            if pn not in seen_phases:
                summary_rows.append({
                    "phase_num": pn, "phase_name": agg["phase_name"],
                    "start_time": "", "end_time": "", "duration_seconds": "",
                    "total_chars": agg["chars"], "total_tokens_est": agg["tokens"],
                })
        summary_rows.sort(key=lambda r: r["phase_num"])
        with open(phase_summary_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields_summary)
            writer.writeheader()
            writer.writerows(summary_rows)
        log("Saved phase metrics summary to: " + phase_summary_csv)
    except Exception as e:
        log("Error writing CSV files: " + str(e))

phase_runs = []

# CRITICAL_PHASES are load-bearing: without ingested + mapped data a report is
# meaningless, so an unexpected crash there is fatal. Every later phase only
# refines the projection, so a crash there is logged and skipped — the report
# is still generated from whatever is in the DB (affected rows fall back to
# their passthrough / best-effort values). See the driver loop below.
CRITICAL_PHASES = {1, 2}


def _build_mapping_coverage_retry_prompt(v):
    """
    Phase 2's mapping_coverage gate fails when some aws_li_key rows in
    aws_li_catalog have no corresponding row in aws_li_to_gcp_li at all.
    Previously this fell through to the generic fallback below, which resends
    the ENTIRE Phase 2 prompt (every mechanic_group, all rows) to a fresh LLM
    invocation — even when only a handful of rows in ONE group are actually
    unmapped. Confirmed real: a job with 3 unmapped OpenSearch rows out of 245
    total triggered an ~8-minute full Mapping-phase re-run (parallel sub-agents
    re-processing compute_breakdown/managed_db/misc/etc. from scratch) to fix
    what was a 3-row problem. Query the DB for exactly the unmapped rows and
    send only those, mirroring the over_and_under_projection targeted-retry
    pattern below — fix via direct INSERT into aws_li_to_gcp_li, no JSON
    mapping files, no merge_mappings.py re-run.
    """
    import duckdb as _ddb
    _conn = _ddb.connect(DB, read_only=True)
    _missing = _conn.execute("""
        SELECT c.aws_li_key, c.mechanic_group, c.product, c.usage_type, c.operation,
               c.instance_type, c.instance_vcpus, c.instance_ram_gb, c.database_engine,
               c.deployment_option, c.volume_type, c.pricing_unit, c.pricing_model,
               c.total_usage, round(c.aws_amortized_cost,2) AS aws_cost,
               c.aws_region, c.gcp_region
        FROM aws_li_catalog c
        WHERE NOT EXISTS (SELECT 1 FROM aws_li_to_gcp_li m WHERE m.aws_li_key = c.aws_li_key)
    """).fetchall()
    _conn.close()

    if not _missing:
        raise ValueError("no unmapped rows found")

    cols = ["aws_li_key", "mechanic_group", "product", "usage_type", "operation",
            "instance_type", "instance_vcpus", "instance_ram_gb", "database_engine",
            "deployment_option", "volume_type", "pricing_unit", "pricing_model",
            "total_usage", "aws_cost", "aws_region", "gcp_region"]
    lines = []
    by_group = {}
    for _row in _missing:
        d = dict(zip(cols, _row))
        by_group.setdefault(d["mechanic_group"], []).append(d)
    for _group, _rows in by_group.items():
        lines.append("### mechanic_group=" + str(_group) + " (" + str(len(_rows)) + " unmapped row(s))")
        for d in _rows:
            lines.append("- " + d["aws_li_key"] + ":")
            for k, val in d.items():
                if k not in ("aws_li_key", "mechanic_group") and val is not None:
                    lines.append("    " + k + ": " + str(val))
        lines.append("")

    row_block = "\n".join(lines)
    return (
        "You are the Phase 2 mapping-coverage retry agent. The mapping_coverage gate reports "
        + str(v) + " row(s) in aws_li_catalog with NO corresponding row in aws_li_to_gcp_li at all."
        " Your ONLY job is to map the specific rows listed below — do not re-process any other row.\n\n"
        "Your job's absolute directory is " + JOB_DIR + " — this job's files live ONLY there,\n"
        "isolated from every other job running on this machine at the same time.\n\n"
        "## Unmapped rows, grouped by mechanic_group\n\n" + row_block +
        "## Instructions\n\n"
        "For each row above, determine the correct GCP mapping (service, SKU via scripts/find-sku.sh,\n"
        "unit_multiplier, strategy) following the same rules as the normal Phase 2 mapping guidance for\n"
        "that row's mechanic_group (see phases/02-mapping.md and phases/01-ingestion.md schemas if you\n"
        "need field definitions). Then INSERT one row per component directly into aws_li_to_gcp_li in "
        + DB + " via SQL (aws_li_key, gcp_service, gcp_sku_id, gcp_sku_name, gcp_sku_unit, component,\n"
        "strategy, unit_multiplier, gcp_region, projection_note, mapping_confidence, is_workload,\n"
        "break_down, rate_source). Every one of the " + str(v) + " aws_li_keys listed above must end up\n"
        "with at least one row in aws_li_to_gcp_li.\n"
        "RULES:\n"
        "  * Map ONLY the rows listed above. Do not touch, re-map, or overwrite any other aws_li_key.\n"
        "  * DO NOT write to any *_mappings.json file or run merge_mappings.py — INSERT directly.\n"
        "  * DO NOT use search_web or any web browsing tool — SKU lookups via scripts/find-sku.sh only.\n"
        "  * DO NOT DROP, DELETE, or OVERWRITE any existing row in aws_li_to_gcp_li.\n"
        "  * PASSTHROUGH = ONE ROW ONLY, break_down=false — never split a passthrough into components.\n"
        "  * If genuinely no GCP equivalent exists, strategy='passthrough' with a clear gcp_service label\n"
        "    is fine — but every row must get an entry; leaving one unmapped fails the gate again.\n"
        "  * STOP once all " + str(v) + " rows have an aws_li_to_gcp_li entry."
    )


def _build_targeted_retry_prompt(ph, v):
    """
    Build a minimal focused retry prompt for gate failures.
    For Phase 5 (over_and_under_projection gate), query the DB for the specific
    violating rows and send only those — not the full triage_suggestions.md.
    For Phase 2 (mapping_coverage gate), query the DB for the specific unmapped
    aws_li_keys and send only those — see _build_mapping_coverage_retry_prompt.
    Falls back to the original full-prompt approach for other phases.
    """
    if ph.get("check_name") == "mapping_coverage":
        try:
            return _build_mapping_coverage_retry_prompt(v)
        except Exception as _e:
            log("mapping_coverage targeted retry build failed (%%s) — falling back to full prompt" %% str(_e))
            return (ph.get("prompt", "") + " IMPORTANT: a deterministic validation gate ('"
                    + ph.get("check_name","") + "') still reports " + str(v)
                    + " violation(s). Find and fix exactly those rows in " + DB + "."
                    + " DO NOT DELETE, DROP, OR OVERWRITE any existing data or tables."
                    + " Only address the missing/violating rows; do not stop until the gate passes.")
    if ph.get("check_name") != "over_and_under_projection":
        return (ph.get("prompt", "") + " IMPORTANT: a deterministic validation gate ('"
                + ph.get("check_name","") + "') still reports " + str(v)
                + " violation(s). Find and fix exactly those rows in " + DB + "."
                + " DO NOT DELETE, DROP, OR OVERWRITE any existing data or tables."
                + " Only address the missing/violating rows; do not stop until the gate passes.")
    try:
        import duckdb as _ddb
        _conn = _ddb.connect(DB, read_only=True)
        _over = _conn.execute("""
            SELECT m.aws_li_key, m.product, m.gcp_service, m.gcp_sku_name,
                   m.unit_multiplier, m.component, m.projection_note,
                   round(p.aws_amortized_cost,2) AS aws_cost,
                   round(p.gcp_projected_cost,2) AS gcp_cost,
                   round(p.gcp_projected_cost / nullif(p.aws_amortized_cost,0), 2) AS ratio
            FROM aws_li_to_gcp_li m JOIN gcp_projection p USING (aws_li_key)
            WHERE p.is_workload AND m.strategy IN ('map','break_down')
              AND p.aws_amortized_cost > 20 AND p.gcp_projected_cost > p.aws_amortized_cost * 3
        """).fetchall()
        _zero = _conn.execute("""
            SELECT m.aws_li_key, m.product, m.gcp_service, m.gcp_sku_name,
                   m.unit_multiplier, m.component, m.projection_note,
                   round(p.aws_amortized_cost,2) AS aws_cost,
                   p.gcp_projected_cost,
                   0.0 AS ratio
            FROM aws_li_to_gcp_li m JOIN gcp_projection p USING (aws_li_key)
            WHERE p.is_workload AND m.strategy IN ('map','break_down')
              AND p.aws_amortized_cost > 10 AND p.gcp_projected_cost IS NOT NULL
              AND p.gcp_projected_cost = 0
        """).fetchall()
        _conn.close()

        # Load prior attempt decisions so LLM knows what already failed.
        import json as _json
        _prev_fixes = {}
        try:
            _fixes_path = os.path.join(JOB_DIR, "outlier_fixes.json")
            if os.path.exists(_fixes_path):
                with open(_fixes_path) as _fp:
                    _fx_list = _json.load(_fp)
                for _fx in (_fx_list if isinstance(_fx_list, list) else []):
                    if isinstance(_fx, dict) and "aws_li_key" in _fx:
                        _prev_fixes[_fx["aws_li_key"]] = _fx
        except Exception:
            pass

        cols = ["aws_li_key","product","gcp_service","gcp_sku_name",
                "unit_multiplier","component","projection_note","aws_cost","gcp_cost","ratio"]
        lines = []
        for _row in _over:
            d = dict(zip(cols, _row))
            lines.append("### " + d["aws_li_key"] + " — OVER-PROJECTION (ratio=" + str(d["ratio"]) + "x, aws=$" + str(d["aws_cost"]) + ", gcp=$" + str(d["gcp_cost"]) + ")")
            for k, val in d.items():
                if k != "aws_li_key" and val is not None:
                    lines.append("- " + k + ": " + str(val))
            if d["aws_li_key"] in _prev_fixes:
                _fx = _prev_fixes[d["aws_li_key"]]
                lines.append("- prior_attempt: decision=" + str(_fx.get("decision","?")) + ", reason=" + str(_fx.get("reason","(none)")))
            lines.append("")
        for _row in _zero:
            d = dict(zip(cols, _row))
            lines.append("### " + d["aws_li_key"] + " — ZERO PROJECTION (aws=$" + str(d["aws_cost"]) + ", gcp=$0)")
            for k, val in d.items():
                if k != "aws_li_key" and val is not None:
                    lines.append("- " + k + ": " + str(val))
            if d["aws_li_key"] in _prev_fixes:
                _fx = _prev_fixes[d["aws_li_key"]]
                lines.append("- prior_attempt: decision=" + str(_fx.get("decision","?")) + ", reason=" + str(_fx.get("reason","(none)")))
            lines.append("")

        if not lines:
            # Gate said violations exist but query found none — race condition, use fallback
            raise ValueError("no violating rows found")

        row_block = "\n".join(lines)
        return (
            "You are the Phase 5 outlier-triage retry agent. The over_and_under_projection gate"
            " reports " + str(v) + " violation(s) that were NOT fixed in the first pass. Your ONLY job is"
            " to fix the rows listed below.\n\n"
            "Your job's absolute directory is " + JOB_DIR + " — this job's files live ONLY there,\n"
            "isolated from every other job running on this machine at the same time.\n\n"
            "## Violating rows\n\n" + row_block +
            "## Instructions\n\n"
            "Fix each row by updating aws_li_to_gcp_li in " + DB + ".\n"
            "Over-projection: reduce unit_multiplier or switch to a cheaper SKU.\n"
            "Zero projection: the SKU has no rate — set strategy=passthrough with a clear gcp_service label.\n"
            "After fixing, run scripts/incremental_rerate.py " + DB + " then scripts/outlier_gate.py " + DB + ".\n"
            "RULES:\n"
            "  * Fix ONLY the rows listed above. Do not touch anything else.\n"
            "  * DO NOT list the directory or read any files — the rows above have all context you need.\n"
            "  * DO NOT use search_web.\n"
            "  * DO NOT DROP or DELETE any table or row.\n"
            "  * STOP after outlier_gate.py prints OK."
        )
    except Exception as _e:
        log("targeted retry build failed (%%s) — falling back to full prompt" %% str(_e))
        return (ph.get("prompt", "") + " IMPORTANT: a deterministic validation gate ('"
                + ph.get("check_name","") + "') still reports " + str(v)
                + " violation(s). Find and fix exactly those rows in " + DB + "."
                + " DO NOT DELETE, DROP, OR OVERWRITE any existing data or tables."
                + " Only address the missing/violating rows; do not stop until the gate passes.")


def run_phase(ph):
    if ph["num"] < START_PHASE or ph["num"] > END_PHASE:
        log("Skipping Phase %%d (%%s)" %% (ph["num"], ph["name"]))
        return

    write_progress(ph["num"], ph["name"], ph["activity"])
    log("=== Phase %%d (%%s) ===" %% (ph["num"], ph["name"]))
    _phase_start_t = time.time()
    _phase_start_ts = time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S")

    start_pos = 0
    if os.path.exists(AGY_LOG):
        start_pos = os.path.getsize(AGY_LOG)
        
    pre_script = ph.get("pre_script")
    if pre_script:
        log("Running pre_script: " + pre_script)
        try:
            subprocess.run([PYTHON, os.path.join(SKILL_DIR, pre_script)], cwd=JOB_DIR, check=True)
        except subprocess.CalledProcessError as e:
            msg = ("pre_script " + pre_script + " exited with code " + str(e.returncode)
                   + " during Phase " + str(ph["num"]) + " (" + ph["name"] + "). Check agy.log for details.")
            log("ERROR: " + msg)
            if ph["num"] in CRITICAL_PHASES:
                with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                    _f.write(msg)
                measure_metrics(phase_runs)
                sys.exit(0)
            else:
                log("WARNING: pre_script failed on non-critical Phase %%d — continuing to report" %% ph["num"])
                return

    # Deterministic pre-LLM scripts always run (zero LLM tokens).
    for scmd in ph.get("pre_llm_scripts") or []:
        run_script(scmd)

    script = ph.get("script")
    if script:
        # Deterministic-only phase: run the single script in place of agy.
        # A leading '?' marks the script as soft — failure is logged but the
        # pipeline continues so the report is always generated.
        soft_script = script.startswith("?")
        script_path = script[1:] if soft_script else script
        log("Running script: " + ("(soft) " if soft_script else "") + script_path)
        try:
            subprocess.run([PYTHON, os.path.join(SKILL_DIR, script_path)], cwd=JOB_DIR, check=True)
        except subprocess.CalledProcessError as e:
            msg = ("script " + script_path + " exited with code " + str(e.returncode)
                   + " during Phase " + str(ph["num"]) + " (" + ph["name"] + "). Check agy.log for details.")
            log("ERROR: " + msg)
            on_failure_prompt = ph.get("on_failure_prompt")
            if soft_script:
                log("soft script failure — continuing to next phase")
            elif on_failure_prompt:
                # LLM fallback: the deterministic parser doesn't recognize this
                # file's format. Build a preview for the LLM (zero tokens), have
                # it normalize the bill into the schema the script already
                # understands, then retry the script exactly once.
                log("script failed — attempting LLM fallback for Phase " + str(ph["num"]))
                on_failure_pre = ph.get("on_failure_pre_script")
                fallback_ok = True
                if on_failure_pre:
                    try:
                        subprocess.run([PYTHON, os.path.join(SKILL_DIR, on_failure_pre)],
                                        cwd=JOB_DIR, check=True)
                    except subprocess.CalledProcessError as e2:
                        log("WARNING: on_failure_pre_script " + on_failure_pre
                            + " failed (code " + str(e2.returncode) + ") — skipping LLM fallback")
                        fallback_ok = False
                if fallback_ok:
                    run_agy(on_failure_prompt, phase_label=ph.get("name", "") + "-fallback")
                    qm_fallback = quota_blocked(start_pos)
                    if qm_fallback:
                        log("quota/auth block (" + qm_fallback + ") during Phase " + str(ph["num"])
                            + " fallback — cannot retry")
                        fallback_ok = False
                if fallback_ok:
                    log("retrying script after LLM fallback: " + script_path)
                    try:
                        subprocess.run([PYTHON, os.path.join(SKILL_DIR, script_path)],
                                        cwd=JOB_DIR, check=True)
                        log("LLM fallback succeeded — Phase " + str(ph["num"]) + " recovered")
                    except subprocess.CalledProcessError as e3:
                        msg = ("script " + script_path + " exited with code " + str(e3.returncode)
                               + " during Phase " + str(ph["num"]) + " (" + ph["name"]
                               + ") — LLM fallback did not resolve it. Check agy.log for details.")
                        log("ERROR: " + msg)
                        with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                            _f.write(msg)
                        measure_metrics(phase_runs)
                        sys.exit(0)
                else:
                    with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                        _f.write(msg)
                    measure_metrics(phase_runs)
                    sys.exit(0)
            else:
                with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                    _f.write(msg)
                measure_metrics(phase_runs)
                sys.exit(0)
    else:
        # Spawn agy only for the LLM portion of this phase.
        run_agy(ph.get("prompt", ""), phase_label=ph.get("name", ""))

    # If the LLM phase hit a quota/auth wall it produced NOTHING usable — stop
    # cleanly with the real reason BEFORE post-LLM scripts run. Otherwise a
    # post-script like merge_mappings.py fails with a misleading "missing mapping
    # file" error that masks the true cause (quota exhausted).
    qm_early = quota_blocked(start_pos)
    if qm_early:
        log("quota/auth block (%%s) during Phase %%d — stopping before post-LLM scripts" %% (qm_early, ph["num"]))
        phase_runs.append({"num": ph["num"], "name": ph["name"],
                           "convs": get_new_convs(AGY_LOG, start_pos),
                           "start_time": _phase_start_ts,
                           "end_time": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S"),
                           "duration_seconds": int(time.time() - _phase_start_t)})
        if ph["num"] in CRITICAL_PHASES:
            with open(os.path.join(JOB_DIR, "failure.txt"), "w") as f:
                f.write("Gemini quota/auth limit reached (" + qm_early + ") during Phase "
                        + str(ph["num"]) + " (" + ph["name"] + "). The model API returned a "
                        "quota error, so the projection could not be completed. Re-run once the "
                        "quota resets or switch to an account with available quota.")
            measure_metrics(phase_runs)
            sys.exit(0)
        else:
            log("WARNING: quota/auth block on non-critical Phase %%d — skipping phase, report will still be generated" %% ph["num"])
            return

    # Deterministic post-LLM scripts always run (zero LLM tokens). For a script
    # phase these are the recompute/repair steps that must follow it (D4/O3):
    # rates are refilled and the autofixer repairs any deterministic violation
    # BEFORE the phase gate evaluates.
    for scmd in ph.get("post_llm_scripts") or []:
        run_script(scmd)

    phase_convs = get_new_convs(AGY_LOG, start_pos)
    phase_entry = {
        "num": ph["num"],
        "name": ph["name"],
        "convs": phase_convs,
        "start_time": _phase_start_ts,
        "end_time": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S"),
        "duration_seconds": int(time.time() - _phase_start_t)
    }
    phase_runs.append(phase_entry)

    if failed_structurally():
        log("failure.txt present after Phase %%d — structural failure, stopping" %% ph["num"])
        measure_metrics(phase_runs)
        sys.exit(0)
    qm = quota_blocked(start_pos)
    if qm:
        log("quota/auth block detected (%%s) after Phase %%d — stopping, no retry" %% (qm, ph["num"]))
        if ph["num"] in CRITICAL_PHASES:
            with open(os.path.join(JOB_DIR, "failure.txt"), "w") as f:
                f.write("Gemini quota/auth limit reached (" + qm + ") during Phase "
                        + str(ph["num"]) + " (" + ph["name"] + "). The model API returned a "
                        "quota error, so the projection could not be completed. Re-run once the "
                        "quota resets or switch to an account with available quota.")
            measure_metrics(phase_runs)
            sys.exit(0)
        else:
            log("WARNING: quota/auth block on non-critical Phase %%d — continuing to report" %% ph["num"])
            return
    sql = ph.get("check_sql")
    if sql:
        v = gate(sql)
        if v is None:
            log("gate %%s: could not evaluate (skipping)" %% ph.get("check_name", "?"))
        elif v > 0:
            log("gate %%s FAILED: %%d violation(s) — re-running phase once" %% (ph.get("check_name","?"), v))

            start_pos_retry = 0
            if os.path.exists(AGY_LOG):
                start_pos_retry = os.path.getsize(AGY_LOG)

            retry_prompt = _build_targeted_retry_prompt(ph, v)
            run_agy(retry_prompt, phase_label=ph.get("name","") + "-retry")

            retry_convs = get_new_convs(AGY_LOG, start_pos_retry)
            phase_entry["convs"].extend(retry_convs)

            # Check quota BEFORE post-LLM scripts: if the retry LLM call hit quota,
            # post-scripts would run on stale/unchanged data and the gate would fail
            # with a misleading error instead of surfacing the real cause.
            qm_retry = quota_blocked(start_pos_retry)
            if qm_retry:
                log("quota/auth block (" + qm_retry + ") during retry of Phase " + str(ph["num"]) + " — stopping")
                if ph["num"] in CRITICAL_PHASES:
                    with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                        _f.write("Gemini quota/auth limit reached (" + qm_retry + ") during retry of Phase "
                                 + str(ph["num"]) + " (" + ph["name"] + "). The model API returned a "
                                 "quota error, so the projection could not be completed. Re-run once the "
                                 "quota resets or switch to an account with available quota.")
                    measure_metrics(phase_runs)
                    sys.exit(0)
                else:
                    log("WARNING: quota/auth block on non-critical Phase %%d retry — continuing to report" %% ph["num"])
                    return

            # Re-run post-LLM recompute/repair before re-checking, so the
            # retry's edits are reflected in the projection (mirrors the normal
            # post-LLM step).
            for scmd in ph.get("post_llm_scripts") or []:
                run_script(scmd)

            v2 = gate(sql)
            log("gate %%s after retry: %%s" %% (ph.get("check_name","?"), "PASS" if v2 == 0 else str(v2) + " remaining"))
            if v2 and v2 > 0:
                msg = ("Phase %%d (%%s) gate '%%s' still reports %%d violation(s) after retry "
                       "and deterministic repair. This is a data/coverage gap the pipeline "
                       "cannot auto-resolve — inspect projection.duckdb for the offending rows."
                       %% (ph["num"], ph["name"], ph.get("check_name","?"), v2))
                if ph["num"] in CRITICAL_PHASES:
                    # Critical phases (ingestion, mapping) — report cannot be
                    # generated without correct data. Stop the pipeline.
                    log("gate FATAL: " + msg)
                    with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                        _f.write(msg)
                    measure_metrics(phase_runs)
                    sys.exit(0)
                else:
                    # Non-critical phases (review, outlier triage) — log the
                    # violation count and continue to generate the report.
                    log("gate WARNING (non-critical phase, continuing): " + msg)
        else:
            log("gate %%s PASS" %% ph.get("check_name","?"))

    # Record this phase as successfully completed so a retry can resume here.
    write_phase_checkpoint(ph["num"])


for ph in PHASES:
    try:
        run_phase(ph)
    except SystemExit:
        # Intentional graceful stop (quota exhausted, unresolved gate, structural
        # failure). Preserve it — these are the pipeline's quality gates.
        raise
    except Exception as e:
        # Any OTHER exception is an unexpected bug in this phase. Never let it
        # abort the whole run with a raw traceback and no report.
        tb = traceback.format_exc()
        if ph["num"] in CRITICAL_PHASES:
            msg = ("Phase %%d (%%s) crashed: %%s. This phase is required for a "
                   "meaningful projection, so the run cannot continue." %% (ph["num"], ph["name"], e))
            log("FATAL: " + msg)
            log(tb)
            with open(os.path.join(JOB_DIR, "failure.txt"), "w") as _f:
                _f.write(msg)
            measure_metrics(phase_runs)
            sys.exit(0)
        log("WARNING: Phase %%d (%%s) crashed but is non-critical: %%s — skipping it so "
            "the report is still generated." %% (ph["num"], ph["name"], e))
        log(tb)

try:
    measure_metrics(phase_runs)
except Exception as e:
    log("Failed to measure final metrics: " + str(e))

log("orchestrator done")
`
