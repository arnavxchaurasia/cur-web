# Phase 1 — Ingestion

**Run by:** main agent. Mechanical script phase, no judgment calls.
**Reads:** the input bill file path.
**Writes:** `aws_raw`, `aws_li_catalog` in `projection-audit/projection.duckdb`.

## Execution

This phase used to require you to manually inspect CSV/Parquet files, extract instance types using regex, perform region mapping, and build the catalog in DuckDB via SQL.

**That is no longer required.** A deterministic Python script now does all of this perfectly without hallucination.

Run the script:

```bash
python3 scripts/ingest.py
```

Check `failure.txt` after running the script. If the file exists and has content, it means the input bill was structurally unprocessable. In that case, you should stop and let the orchestrator handle the exit.

## Step 2 — Classify rows and produce the Phase 2 dispatch manifest

Once ingestion succeeds, run the mechanic-group classifier immediately:

```bash
python3 scripts/classify_mechanics.py projection-audit/projection.duckdb
```

This stamps a `mechanic_group` column on every row in `aws_li_catalog` and writes `projection-audit/phase2_manifest.json` — the file Phase 2 reads to dispatch parallel agents. The manifest lists every row (with its full field set) grouped by mechanic, so each Phase 2 agent only receives rows relevant to its specialty.

If the script prints a `WARNING: misc group is X% of total spend` line, note it but do not stop — Phase 5 will triage misc rows. If the script exits non-zero for any other reason, treat it as a fatal ingestion error.

The phase is complete once both scripts exit 0 and `projection-audit/phase2_manifest.json` exists.
