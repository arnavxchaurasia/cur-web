# Agent Guardrails

Read this file before doing anything. These rules are non-negotiable.

---

## What the agent CAN do

- Write Python code to a temp file inside the **job directory** and execute it with `python3`
- Read/write files inside the **job working directory** (the directory passed as the run context)
- Read data files from this skill's own `data/` directory — **read-only**
- Execute DuckDB queries via the Python `duckdb` module
- Call the `agy` CLI for LLM normalization fallbacks
- Write to `progress.json` in the job directory
- Create subdirectories inside the job directory (e.g., `projection-audit/`)
- Read phase spec files and reference docs from this skill directory — **read-only**

---

## What the agent MUST NEVER do

### Do NOT touch the Go backend
The following paths are **off-limits**. Never read, write, create, or delete anything in:
- `cmd/` — Go entry point
- `internal/` — Go handlers and job spawner
- `go.mod`, `go.sum` — Go module files
- Any `*.go` file anywhere in the repo
- `server.exe`, `cur-web-server.exe` — compiled binaries
- `Makefile` — build instructions
- `systemd/`, `nginx/` — deployment config

### Do NOT touch the React frontend
- `frontend/` — entire directory is off-limits (React/TypeScript/Vite app)

### Do NOT touch skill source files
- `skill/` — the original skill directory
- `skill-standalone/` — this directory itself

Never modify phase specs, SKILL.md, GUARDRAILS.md, or any reference doc. These are read-only instructions.

### Do NOT touch shared data files
- `skill/aws-gcp-cost-projection/data/*.json` — JSON config files
- `skill/aws-gcp-cost-projection/data/*.duckdb` — the GCP billing catalog
- `skill/aws-gcp-cost-projection/data/skus/` — per-SKU gzip catalog files

These are read-only inputs. Never write back to them.

### Do NOT do these things
- Install Python packages (`pip install`) — all required packages are pre-installed
- Make direct network requests to GCP/AWS APIs — use `agy` CLI or the bundled catalog only
- Run `scripts/refresh-catalog.sh` — catalog refresh is a maintainer action, not a per-run action
- Delete or overwrite the input bill file
- Run `git` commands of any kind
- Bulk-load the entire SKU catalog — load lazily, only SKUs that appear in `aws_li_to_gcp_li`
- Print more than 50 rows of query output to stdout — use `LIMIT` in SQL or summarize in Python
- Web-search for GCP rates — the rate card lives in `data/` and `gcp_sku_rates` table

---

## How to run embedded Python code

Each phase file contains complete Python scripts as code blocks. To run them:

1. Write the code block to a temp file in the job directory:
   ```bash
   cat > /tmp/phase_script.py << 'EOF'
   # paste the embedded code here
   EOF
   python3 /tmp/phase_script.py
   ```

2. Or write it inline if short enough:
   ```bash
   python3 - << 'EOF'
   # short inline code
   EOF
   ```

Always set `SKILL_DIR` environment variable to the path of this skill directory before running embedded scripts — scripts use it to locate `data/` files:
```bash
export SKILL_DIR="/path/to/skill-standalone/aws-gcp-cost-projection"
python3 /tmp/phase_script.py
```

---

## Scope

This skill covers exactly **phases 1 through 6** of the AWS→GCP cost projection pipeline:
- Phase 1: Ingestion
- Phase 2: Mapping
- Phase 3: Review
- Phase 4: Rate-card fill
- Phase 5: Outlier triage
- Phase 6: Report generation

Do not add phases, create new tools, or extend scope without explicit user instruction.
