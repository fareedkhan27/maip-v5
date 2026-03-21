# MAIP — Market Access Intelligence Platform

> **AI-powered reimbursement intelligence for market access professionals.**
> Upload any structured dataset. Ask questions in plain English. Get decision-ready answers.

[![Python](https://img.shields.io/badge/Python-3.11+-0A2463?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-1E6091?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Anthropic](https://img.shields.io/badge/Claude-Haiku%20%7C%20Sonnet-00A3E0?style=flat-square)](https://anthropic.com)
[![Railway](https://img.shields.io/badge/Deployed-Railway-7B2D8B?style=flat-square)](https://lrmaip.com)
[![License](https://img.shields.io/badge/License-MIT-1A936F?style=flat-square)](LICENSE)

**Live platform:** [lrmaip.com](https://lrmaip.com)
**Built by:** Fareed Khan · fareedkhan27@gmail.com

---

## What Is MAIP?

MAIP is a schema-driven market access intelligence platform built for LR (Local Representative) markets across CEE, LATAM, and MEA. It connects to your reimbursement tracking dataset and lets you query it using natural language — no SQL, no filter menus, no pivot tables.

It also delivers AI-powered market intelligence across 10 research modules, covering competitive landscape, HTA pathways, timeline forecasting, IRP risk, payer dynamics, patient population, and more — all grounded in live web search and your uploaded data.

---

## Key Features

| Feature | Description |
|---|---|
| **Natural language queries** | Ask in plain English — abbreviations, typos, and short forms handled via fuzzy matching |
| **Schema auto-detection** | Upload any `.xlsx`, `.xls`, or `.csv` — 10 semantic roles detected automatically |
| **Exact data accuracy** | Filters run in Python (pandas) — AI only narrates, never estimates from data |
| **10 research modules** | Competitive landscape, HTA, timeline, IRP risk, payer landscape, patient population, analog intelligence, evidence gap, hospital channel, indication research |
| **Indication Landscape** | Full therapy-area grouped indication map (Approved + Pipeline) with strategic view — Sonnet + web search, cached 30 days |
| **Indication Coverage** | Cross-references FDA/EMA approved indications against your dataset per country — shows reimbursement gaps |
| **Leadership Summary** | Structured executive cards for Competitive (M3), Timeline (M4), HTA (M5), IRP Risk (M8) modules |
| **Cross-Market Comparison** | Side-by-side comparison of any two markets across the 4 leadership-enabled modules |
| **Gap Priority Score** | Composite access gap scoring (0–100) across all markets with Critical/Defend/Watch/Monitor tiers |
| **Chart.js + SVG charts** | Bar/clustered/stacked bar via Chart.js 4.4.0; pie and line via SVG — deterministic type selection |
| **PDF export** | Leadership briefing PDF export per module (jsPDF 2.5.1) |
| **Excel export** | Filtered data + summary analytics sheet |
| **Session isolation** | Each user's data is fully isolated — no cross-contamination |
| **Demo mode** | Try the platform without uploading a file — synthetic generic dataset |
| **Token usage indicator** | Live header chip showing daily token consumption vs budget |
| **Retry logic** | Exponential backoff (2s/4s/8s) on Anthropic 429 and 529 errors |

---

## Architecture

```
Browser (React 18 UI)
      ↕  HTTP — access-key gated, no raw data exposed
FastAPI Server (Python 3.11) — app.py
      ↕  Server-side only
  ┌──────────────────────────────────────────────────────┐
  │  Anthropic API (Claude Haiku + Sonnet)               │
  │  pandas filter engine (deterministic)                │
  │  SQLite session + cache store (maip.db)              │
  │  Rate limiter (slowapi)                              │
  │  Daily token budget guard                            │
  │  Exponential backoff retry (3 attempts max)          │
  └──────────────────────────────────────────────────────┘
```

### Three-Stage Query Pipeline — Data Accuracy Guarantee

Every natural language query passes through three stages:

```
Stage 1 — Intent Parser (Haiku)
  Input:  raw user query + dataset schema + unique values
  Output: structured filter spec JSON (product, country, status, period, etc.)
  
Stage 2 — Filter Engine (pandas)
  Input:  filter spec + full dataset DataFrame
  Output: filtered rows + computed stats (deterministic — no AI involvement)
  
Stage 3 — Narrator (Haiku)
  Input:  pre-computed stats (NOT raw data)
  Output: analytical prose in 5-section format
  
Claude never touches raw data values. Every number comes from pandas.
```

### Research Module Pipeline

```
/api/research POST
  Input:  subtype + {product, country, indication}
  Stage:  call_with_tools() — Sonnet + web_search (max 3 turns)
  Output: structured markdown intelligence card

/api/indication-landscape GET
  Input:  product
  Stage:  Sonnet + web_search (max 4 turns)
  Output: JSON {indications[], strategic_view{}} grouped by therapy area
  Cache:  720h (30 days) in response_cache
```

---

## Intelligence Modules

### Query Module (Module 1)

Natural language interface to your uploaded dataset.

| Capability | Detail |
|---|---|
| Query engine | Claude Haiku (intent parsing + narration) |
| Filter execution | pandas (deterministic) |
| Chart types | Bar (Chart.js), Pie (SVG), Line (SVG) |
| Chart type selection | Deterministic client-side logic — no AI token cost |
| Rate limit | 30 queries/hour |
| Cache | 24h per session+query hash |

### Research Modules (Modules 2–10)

All 9 research subtypes route through `POST /api/research` using Claude Sonnet + web search (3 turns max). Rate limit: 3 requests/hour.

| # | Module | Subtype Key | Indication Required |
|---|---|---|---|
| 2 | Competitive Landscape | `competitive` | ✅ Yes |
| 3 | Timeline Intelligence | `timeline` | ✅ Yes |
| 4 | HTA & Public Sector | `public_sector` | ❌ No (country-level) |
| 5 | Patient Population | `patient_population` | ✅ Yes |
| 6 | Analog Intelligence | `analog` | ✅ Yes |
| 7 | IRP Risk Analysis | `irp_risk` | ❌ No (product-level) |
| 8 | Evidence Gap | `evidence_gap` | ✅ Yes |
| 9 | Hospital Channel | `hospital_channel` | ❌ No (country/product-level) |
| 10 | Payer Landscape | `payer_landscape` | ❌ No (country-level) |

An additional subtype `indication` retrieves all indications for a product via the research pipeline. For the structured indication lookup and landscape features, see dedicated routes below.

### Indication Routes (Dedicated)

Three standalone routes handle indication intelligence:

| Route | Purpose | Model | Rate | Cache TTL |
|---|---|---|---|---|
| `GET /api/indications` | Short indication list — dropdown population | Haiku + web_search | 10/hr | 720h |
| `GET /api/indication-landscape` | Full therapy-area grouped landscape with strategic view | Sonnet + web_search | 10/hr | 720h |
| `POST /api/indication-coverage` | Cross-reference FDA/EMA indications vs dataset per country | Haiku normalisation | 15/hr | 24h |

### Leadership Summary (Modules 3, 4, 5, 8 Only)

Structured executive cards extracted from research module outputs. Supported modules:

| Module ID | Research Subtype | Summary Fields |
|---|---|---|
| 3 | Competitive | Competitors, access status, biosimilar threat, our position, access gap |
| 4 | Timeline | Current milestone, next milestone, estimated timeline, regional benchmark, delay risk |
| 5 | HTA & Public Sector | HTA body, evidence standard, current status, expected timeline, risk rating |
| 8 | IRP Risk | Reference basket, price position, cascade exposure, financial risk, risk level |

All summaries include a `leadership_signal` — one action-oriented sentence grounded exclusively in the intelligence output.

**Cross-Market Comparison** is available for the same 4 modules. Country B is analysed using Sonnet training knowledge if no local intelligence has been run; estimates are labelled `[Benchmark]`.

**PDF Export** generates a dark-theme A4 briefing document with all summary fields and the leadership signal.

### Gap Priority Score

Composite scoring engine across all markets in your dataset.

```
Score (0–100) = Gap % × 0.5 + Indication Breadth × 0.3 + Years in Gap × 0.2

Tiers:
  Critical  ≥ 70   →  Immediate engagement
  Defend    ≥ 45   →  Targeted submission required
  Watch     ≥ 20   →  Monitor trajectory
  Monitor    < 20  →  Stable
```

---

## API Reference

All endpoints require the `x-access-key` header except `GET /api/health`.

### Core Routes

| Method | Route | Purpose | Rate |
|---|---|---|---|
| `GET` | `/` | Serve React UI | — |
| `GET` | `/api/health` | Health check | — |
| `GET` | `/api/stats` | Token usage + query counts | — |
| `POST` | `/api/upload` | Upload dataset (.xlsx/.xls/.csv) | 10/hr |
| `GET` | `/api/demo` | Create demo session | — |

### Query & Analysis

| Method | Route | Purpose | Rate |
|---|---|---|---|
| `POST` | `/api/query` | Natural language query | 30/hr |
| `POST` | `/api/kpi` | KPI summary for dashboard | — |
| `POST` | `/api/gap_analysis` | Simple gap list (legacy) | — |
| `GET` | `/api/gap-analysis` | Gap Priority Score with tiers | 20/hr |
| `POST` | `/api/export/excel` | Export filtered data as Excel | — |

### Research & Intelligence

| Method | Route | Purpose | Rate |
|---|---|---|---|
| `POST` | `/api/research` | Run research module (subtypes 2–10) | 3/hr |
| `POST` | `/api/leadership-summary` | Generate executive summary card | 5/hr |
| `POST` | `/api/compare-markets` | Cross-market comparison | 3/hr |
| `GET` | `/api/indications` | Indication list for dropdown | 10/hr |
| `GET` | `/api/indication-landscape` | Full landscape with therapy areas | 10/hr |
| `POST` | `/api/indication-coverage` | Coverage gap vs dataset | 15/hr |

### Utility

| Method | Route | Purpose | Rate |
|---|---|---|---|
| `POST` | `/api/feedback` | Submit issue report | — |
| `GET` | `/api/feedback` | View feedback log (admin) | — |
| `POST` | `/api/annotate` | Add row annotation | — |
| `GET` | `/api/annotations/{session_id}` | Get annotations | — |
| `GET` | `/api/history/{session_id}` | Query history | — |
| `GET` | `/api/sessions` | Active sessions list | — |
| `GET` | `/api/audit` | Audit log (admin) | — |

---

## Cache Architecture

MAIP uses a dual-layer cache — server-side SQLite + frontend React state — to minimise Anthropic API calls.

```
Request arrives
      ↓
Frontend cache hit? → Return instantly (0 tokens)
      ↓ miss
Server response_cache hit + not expired? → Return (0 tokens)
      ↓ miss
Anthropic API call → Write to server cache + frontend cache
```

| Endpoint | Server Cache Table | Key | TTL |
|---|---|---|---|
| `/api/query` | `response_cache` | `session_id + sha256(query)` | 24h |
| `/api/research` | `research_cache` | `subtype + sha256(context)` | 24h |
| `/api/leadership-summary` | `response_cache` | `module+country+product+indication+text[:4000]` | 24h |
| `/api/compare-markets` | `response_cache` | `module+countryA+countryB+product+indication` | 24h |
| `/api/indications` | `response_cache` | `product` | 720h |
| `/api/indication-landscape` | `response_cache` | `product` | 720h |
| `/api/indication-coverage` | `response_cache` | `product+country+sorted(fdaIndications[:15])` | 24h |

Error responses (429, 529, rate_limit text) are never written to cache. A startup purge removes any poisoned entries on each deploy.

---

## Database

MAIP uses **SQLite** in production (Railway, single-instance deployment). The database file is `maip.db` in the project root.

### Tables

| Table | Domain | Purpose | Persistent |
|---|---|---|---|
| `sessions` | Session | Dataset storage (JSON) — 4hr expiry | ❌ |
| `queries` | Session | Query log + filter specs | ✅ |
| `research_cache` | Cache | Research module results | ❌ (24h TTL) |
| `response_cache` | Cache | Leadership, compare, indications, query | ❌ (24h/720h TTL) |
| `annotations` | Data | User row notes | ✅ |
| `audit_log` | Observability | All API actions | ✅ |
| `feedback_log` | Observability | User issue reports | ✅ |

> **Roadmap:** Migration to Railway PostgreSQL is planned (15-table schema designed). This will add persistent indication master registry, team results store, per-call token tracking, and daily cost aggregation.

---

## Dataset Requirements

MAIP works with **any** structured dataset. Upload a `.xlsx`, `.xls`, or `.csv` file and the schema is detected automatically from column headers.

For reimbursement tracking, the platform works best when your dataset includes columns representing:

| Semantic Role | Example Column Names |
|---|---|
| `PRODUCT` | Product, Brand, Drug, Molecule |
| `COUNTRY` | Country, Market, Territory |
| `STATUS` | Status, Reimbursement Status, Access Status |
| `PERIOD` | Period, Quarter Period (format: `YYYY-QN`) |
| `REGION` | Region, Geography, Area |
| `SECTOR` | Sector, Channel, Payer Setting |
| `INDICATION` | Indication, Indication Details, Disease |
| `DATASOURCE` | Data-Source, Source (for dual-domain datasets) |

Column names and values are never hardcoded. The platform adapts to whatever headers and values are in your file.

### Dual-Domain Dataset Model (LR Markets)

MAIP natively supports dual-domain datasets where:

- `Data-Source = "Access Only"` → Reimbursement data (Period format: `YYYY-QN`)
- `Data-Source = "Launch Only"` → Launch data (Launch-Month format: `MMM-YY`)

`Data-Source` acts as the primary query router. Zero-hallucination is the governing constraint — the intent parser routes queries to the correct domain before filtering.

---

## Quick Start

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)

### Install and Run

```bash
# Clone the repository
git clone https://github.com/fareedkhan27/maip-v5.git
cd maip-v5

# Create virtual environment
python -m venv venv
source venv/bin/activate        # Mac / Linux
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env: add your ANTHROPIC_API_KEY and ACCESS_KEY

# Start the server
python app.py
```

Open **http://localhost:8000** in Chrome or Edge.

### Environment Variables

```bash
ANTHROPIC_API_KEY=sk-ant-api03-...     # Required — Anthropic API key
ACCESS_KEY=your-access-key             # Required — gates all /api/* routes
DAILY_TOKEN_BUDGET=500000              # Optional — daily token cap (default 500k)
MAX_FILE_SIZE_MB=20                    # Optional — upload size limit (default 20MB)
SESSION_EXPIRY_HOURS=4                 # Optional — session lifetime (default 4h)
RESEARCH_CACHE_TTL_HOURS=24            # Optional — cache TTL (default 24h)
PORT=8000                              # Optional — server port (default 8000)
```

---

## Models Used

| Route(s) | Model | Rationale |
|---|---|---|
| `/api/query` (intent + narrator) | `claude-haiku-4-5-20251001` | High-volume, latency-sensitive, deterministic parsing |
| `/api/research` | `claude-sonnet-4-6` | Deep web research, multi-turn tool use |
| `/api/leadership-summary` | `claude-sonnet-4-6` | Executive-grade synthesis — reasoning depth justifies cost |
| `/api/compare-markets` | `claude-sonnet-4-6` × 2 | Parallel market summaries |
| `/api/indication-landscape` | `claude-sonnet-4-6` | Full indication research + strategic view |
| `/api/indications` | `claude-haiku-4-5-20251001` + web_search | Lightweight list fetch |
| `/api/indication-coverage` | `claude-haiku-4-5-20251001` | Nomenclature normalisation |

Both models can be overridden per-request via the Settings panel in the UI (Haiku ↔ Sonnet).

---

## Accuracy Standards

All research outputs include an explicit accuracy disclaimer rendered in the UI:

> *AI-generated analysis · Figures sourced from uploaded dataset · Regulatory and market access context requires independent verification before executive use.*

Research module system prompts enforce:

1. Only assert facts directly supported by web search results retrieved in the current session
2. Any claim not from a current search result is prefixed: `[Unverified — training data only]`
3. Never fabricate regulatory approval dates, reimbursement percentages, HTA decisions, pricing figures, or market share numbers
4. Unavailable data points are stated explicitly: *"Not available from current search — recommend primary source verification"*
5. Confirmed regulatory status is clearly distinguished from pipeline/expected status

---

## Security

- All `/api/*` routes require `x-access-key` header — validated against `ACCESS_KEY` env var
- ANTHROPIC_API_KEY is server-side only — never exposed to the browser
- Session data (uploaded datasets) lives in SQLite only — never logged, never transmitted
- `audit_log` stores `{product, country, indication, module}` only — no raw query text
- CORS restricted to `localhost:8000` and `lrmaip.com`
- Rate limiting via `slowapi` on all AI-consuming endpoints
- Exponential backoff retry (2s/4s/8s) on Anthropic 429/529 — never surfaces raw error strings to users
- Injection defence on all AI prompt boundaries — user-supplied values wrapped in XML tags with explicit instructions to treat as data identifiers only

---

## Deployment (Railway)

MAIP is deployed on [Railway](https://railway.app) as a single Python service.

```bash
# Deploy
git push origin main   # Railway auto-deploys on push to main

# Check health
curl https://lrmaip.com/api/health
```

Railway reads the `Procfile` for the start command and `.python-version` for the runtime. No additional configuration required.

**Monthly API cost estimate:** $20–30 USD after dual-layer cache (40–50% reduction from caching). Hard cap: $50/day via `DAILY_TOKEN_BUDGET`.

---

## Changelog

### v5.0 (Current)
- Exponential backoff retry on Anthropic 429/529 (3 attempts, 2s/4s/8s)
- Chart.js 4.4.0 integration — deterministic chart type selection (clustered bar / stacked bar / horizontal bar / line)
- Live token usage indicator in header (polling `/api/stats` every 60s)
- Dual-layer cache (server SQLite + frontend React state) across all 7 AI endpoints
- Budget guard on all previously unguarded endpoints
- Indication Landscape module (`/api/indication-landscape`) — full therapy-area grouped map with strategic view
- Cross-market comparison with `summary_a_cached` passthrough (saves one Sonnet call)
- Leadership Summary explicit trigger — removed auto-fire to prevent silent token cascade
- `fdaIndications` memoised in `IndicationCoverage` component
- Error response cache guard — 429/529 responses never written to cache
- Startup purge of poisoned cache entries

### v4.x
- Leadership Summary cards (Competitive, Timeline, HTA, IRP Risk)
- Indication Coverage gap overlay
- Gap Priority Score with composite scoring
- PDF export for leadership briefings
- Indication Lookup dropdown with FDA/EMA source tagging
- Recommendation Engine — cross-module signal routing
- Feedback reporting system

---

## License

MIT — see [LICENSE](LICENSE)

Built by [Fareed Khan](mailto:fareedkhan27@gmail.com) · [lrmaip.com](https://lrmaip.com)
