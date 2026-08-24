# Skill — aws-gcp-cost-projection

The [`aws-gcp-cost-projection`](./aws-gcp-cost-projection/) skill is the
mapping engine the cur-web app spawns (via `internal/jobs/spawner.go`)
to convert an uploaded AWS bill into a GCP cost projection report.

This directory is the **source of truth** for the skill within this
repo. The same skill also lives upstream in
[`Facets-cloud/devops-skills`](https://github.com/Facets-cloud/devops-skills/tree/main/skills/aws-gcp-cost-projection),
but for now we iterate on it here — it's easier for contributors who
already have cur-web access, and the close coupling with the Go code
that spawns it means changes often need to land together.

## Layout

```
aws-gcp-cost-projection/
├── SKILL.md              # Top-level skill brief — read first
├── preflight.sh          # Tool / catalog sanity-check (called per-run)
├── phases/01..06.md      # Six-phase pipeline: ingest → map → review → rate-fill → outlier → report
├── reference/            # Schemas, PDF ingestion recipe, sample artifacts
├── scripts/              # find-sku.sh, refresh-catalog.sh, allow-list
└── data/                 # Bundled GCP Cloud Billing Catalog (services.json + skus/*.json.gz)
```

## Deploying changes to the VM

The skill is consumed by the cur-web service running on the GCP VM
at `~/.claude/skills/aws-gcp-cost-projection/` (a plain copy, not a
git checkout). To push a change:

```bash
# from the repo root, after editing skill/aws-gcp-cost-projection/...
gcloud compute scp --recurse --zone=asia-south1-a \
  skill/aws-gcp-cost-projection/phases \
  skill/aws-gcp-cost-projection/reference \
  cur-web:~/.claude/skills/aws-gcp-cost-projection/
```

Restart isn't needed for skill changes — every new job spawns a fresh
`claude` subprocess that reads the skill files from disk.

For changes to `data/` (the GCP catalog), prefer running
`scripts/refresh-catalog.sh` upstream and copying the regenerated
catalog files; do not hand-edit SKU JSON.

## Iteration tips

- The PDF-ingestion recipe lives at `reference/pdf-ingestion.md` and
  the Phase 1 sub-agent regenerates a per-run `ingest.py` from it; if
  PDF parsing misbehaves for a new bill flavor, that doc is where to
  add the anchor markers.
- The skill is invoked headlessly via `claude -p` with the prompt
  built in `internal/jobs/spawner.go`. If you change phase contracts,
  re-check the spawner prompt.
- The cur-web Go binary does **not** do any mapping logic — all
  correctness logic lives in the phase docs here. Bugs in the
  projection numbers are almost always in `phases/*.md`, not in
  `internal/jobs/`.
