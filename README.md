# 🌩️ CUR AWS to GCP

> **Convert AWS Cost and Usage Reports into GCP Cost Projections**

Transform your AWS billing data into actionable GCP cost insights with an intelligent 6-phase mapping pipeline.

---

## 🎯 What This Does

This project bridges AWS and GCP by analyzing your AWS Cost and Usage Reports (CUR) and projecting what those workloads would cost on Google Cloud Platform. It combines:

- 🔍 **Intelligent Service Mapping** — Automatically translates AWS services to GCP equivalents
- 📊 **Cost Projection Engine** — Applies current GCP pricing to your actual AWS consumption
- 🎨 **Interactive Web UI** — Upload bills and explore projections in real-time
- ⚡ **Production-Ready** — Deployed and operational on GCP VMs

---

## 📊 Tech Stack

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   Python (62.7%)        Go (24.2%)      TypeScript (8.4%)  │
│   ████████████████▌     ███████▌        ██▌                │
│   Mapping Logic         REST API         React UI          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

| Component | Language | Role |
|-----------|----------|------|
| **Backend Server** | Go (24.2%) | REST API, Job Orchestration, GCP Integration |
| **Mapping Engine** | Python (62.7%) | Cost projection, service translation, analytics |
| **Frontend App** | TypeScript + React (8.4%) | Bill upload UI, results dashboard |
| **Data** | JSON/Shell (5.4%) | GCP Catalog, deployment scripts |

---

## 🏗️ Repository Structure

```
cur-aws-to-gcp/
│
├── 📁 backend/                          [Go • 24.2%]
│   ├── cmd/
│   │   └── server/                      # Main entry point
│   ├── internal/
│   │   ├── jobs/
│   │   │   └── spawner.go              # Orchestrates skill execution
│   │   └── handlers/                    # HTTP API endpoints
│   └── main.go
│
├── 📁 skill/                            [Python • 62.7%]
│   └── aws-gcp-cost-projection/
│       ├── SKILL.md                     # Skill documentation
│       │
│       ├── 📊 phases/                   # 6-Phase Processing Pipeline
│       │   ├── 01-ingest.md            # Parse AWS CUR PDFs
│       │   ├── 02-map.md               # Map services to GCP
│       │   ├── 03-review.md            # Validate data
│       │   ├── 04-rate-fill.md         # Apply GCP pricing
│       │   ├── 05-outlier.md           # Detect anomalies
│       │   └── 06-report.md            # Generate report
│       │
│       ├── scripts/                     # Utilities
│       │   ├── find-sku.sh             # Locate GCP SKUs
│       │   └── refresh-catalog.sh      # Update pricing
│       │
│       ├── reference/                   # Schemas & recipes
│       │   └── pdf-ingestion.md        # PDF parsing guide
│       │
│       └── data/                        # GCP Billing Catalog
│           ├── services.json            # Service definitions
│           └── skus/                    # SKU data (compressed)
│
├── 📁 frontend/                         [TypeScript • 8.4%]
│   ├── src/
│   │   ├── components/                  # React components
│   │   ├── pages/                       # Page routes
│   │   └── App.tsx                      # Main app
│   ├── vite.config.ts                   # Build config
│   └── package.json                     # Dependencies
│
└── README.md (← you are here)
```

---

## 🔄 How It Works: The 6-Phase Pipeline

When you upload an AWS bill, it flows through this intelligent pipeline:

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  INGEST  │ → │   MAP    │ → │  REVIEW  │ → │RATE-FILL │ → │ OUTLIER  │ → │  REPORT  │
│ PDF/CSV  │    │ Services │    │  Data    │    │GCP Rates │    │Detection │    │Generation│
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘    └──────────┘
```

| Phase | What Happens |
|-------|--------------|
| **1️⃣ Ingest** | Extract billing data from AWS CUR PDFs or CSV files |
| **2️⃣ Map** | Intelligently match AWS services to GCP equivalents |
| **3️⃣ Review** | Validate and verify mapped data for accuracy |
| **4️⃣ Rate-Fill** | Apply current GCP pricing from Cloud Billing Catalog |
| **5️⃣ Outlier** | Detect and flag unusual cost patterns |
| **6️⃣ Report** | Generate side-by-side cost comparison reports |

---

## 🚀 Quick Start

### Prerequisites

```bash
# Core tools (all required)
Go 1.21+           # Backend: https://go.dev/dl/
Node.js 18+        # Frontend: https://nodejs.org
Python 3.9+        # Skill engine: https://python.org

# CLI tools
brew install duckdb jq    # Skill dependencies
which agy                 # Antigravity CLI (internal tool)
gzip                      # Pre-installed on macOS
```

### Environment Setup

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env with your values
SESSION_SECRET=<output of: openssl rand -base64 32>
APP_BASE_URL=http://localhost:8080
DEV_AUTH_BYPASS=true
ANTIGRAVITY_API_KEY=<your agy key>
```

### Start the Backend

```bash
go mod download
go run ./cmd/server
# ✅ Server runs on http://localhost:8080
# On first start, it validates that agy, duckdb, and jq are installed
```

### Start the Frontend

```bash
cd frontend
npm install
npm run dev
# ✅ Frontend runs on http://localhost:5173
# Automatically proxies /api requests to localhost:8080
```

### Skill Pipeline

The skill is self-contained at `skill/aws-gcp-cost-projection/`. The backend automatically discovers and uses it — no extra setup needed for local development.

---

## 🔧 Architecture

```
┌────────────────────────────────────────────────┐
│          React UI (TypeScript + Vite)          │
│     • Bill Upload  • Results Dashboard        │
│     • Report Viewer  • Cost Comparison         │
└──────────────────┬─────────────────────────────┘
                   │ HTTP/REST (port 5173 → 8080)
┌──────────────────▼─────────────────────────────┐
│              Go REST API (port 8080)           │
│     • /api/upload      Upload bills            │
│     • /api/status      Check job status        │
│     • /api/report      Fetch results           │
└──────────────────┬─────────────────────────────┘
                   │ Spawns as subprocess
┌──────────────────▼─────────────────────────────┐
│        Python Skill Engine (6-Phase)           │
│     • Parse PDFs  • Map services               │
│     • Apply rates • Generate reports           │
└──────────────────┬─────────────────────────────┘
                   │ Integrates with
┌──────────────────▼─────────────────────────────┐
│      GCP Cloud Billing API + Catalog           │
│     • Current pricing  • Service definitions   │
└────────────────────────────────────────────────┘
```

---

## 📚 Key Files & Docs

| File | Purpose |
|------|---------|
| **[skill/aws-gcp-cost-projection/SKILL.md](skill/aws-gcp-cost-projection/SKILL.md)** | Complete skill specification |
| **[skill/aws-gcp-cost-projection/phases/](skill/aws-gcp-cost-projection/phases/)** | Detailed documentation for each phase |
| **[skill/aws-gcp-cost-projection/reference/pdf-ingestion.md](skill/aws-gcp-cost-projection/reference/pdf-ingestion.md)** | Guide to PDF parsing and schema |
| **[skill/README.md](skill/README.md)** | Skill deployment and iteration guide |
| **[frontend/README.md](frontend/README.md)** | React/TypeScript setup details |

---

## 📤 Deploying Changes

### Skill Updates → Production VM

After editing skill files, sync them to the GCP VM:

```bash
gcloud compute scp --recurse --zone=asia-south1-a \
  skill/aws-gcp-cost-projection/phases \
  skill/aws-gcp-cost-projection/reference \
  cur-web:~/.claude/skills/aws-gcp-cost-projection/
```

*No restart required — the skill is read from disk on each job.*

### GCP Catalog Updates

```bash
cd skill/aws-gcp-cost-projection
bash scripts/refresh-catalog.sh
# Re-syncs the data/ directory with latest pricing
```

---

## 🤝 Contributing

1. **Backend changes** → Edit `cmd/` or `internal/`, test locally
2. **Skill changes** → Modify `skill/aws-gcp-cost-projection/phases/`, then deploy
3. **Frontend changes** → Update `frontend/src/`, build and test
4. **Catalog changes** → Run `refresh-catalog.sh`, commit updated data

---

## 📋 Project Info

- **Owner:** [arnavchaurasia-knp](https://github.com/arnavchaurasia-knp)
- **License:** MIT
- **Status:** Active & Deployed ✅
- **Repo:** [arnavchaurasia-knp/cur-aws-to-gcp](https://github.com/arnavchaurasia-knp/cur-aws-to-gcp)

---

## 💬 Support

Found a bug or have a question? [Open an issue](https://github.com/arnavchaurasia-knp/cur-aws-to-gcp/issues) on GitHub.

---

<div align="center">

**Built with ❤️ to make cloud cost migration analysis simple**

[⬆ Back to top](#-cur-aws-to-gcp)

</div>
