# MAIP — Market Access Intelligence Platform

> **AI-powered reimbursement intelligence for pharmaceutical market access teams.**
> Upload any structured dataset. Ask questions in plain English. Get decision-ready answers.

[![Python](https://img.shields.io/badge/Python-3.11+-0A2463?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-1E6091?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Anthropic](https://img.shields.io/badge/Claude-Haiku%20%7C%20Sonnet-00A3E0?style=flat-square)](https://anthropic.com)
[![License](https://img.shields.io/badge/License-MIT-1A936F?style=flat-square)](LICENSE)

---

## What is MAIP?

MAIP is a schema-driven market access intelligence platform. It connects to your reimbursement tracking dataset and lets you query it using natural language — no SQL, no filter menus, no pivot tables.

Ask things like:

- *"Show Opdivo Planned entries in MEA"*
- *"Bar chart: reimbursed entries by region"*
- *"How many countries have Reblozyl planned status?"*
- *"What is the typical HTA timeline for Saudi Arabia?"*
- *"Which product has the most planned records?"*

The platform answers with exact numbers, structured analysis, and visualisations — all derived directly from your data.

---

## Key Features

| Feature | Description |
|---|---|
| **Natural language queries** | Ask in plain English — abbreviations, typos, and short forms all handled |
| **Schema auto-detection** | Upload any `.xlsx`, `.xls`, or `.csv` — columns detected automatically |
| **Exact data accuracy** | Filters run in Python (pandas) — AI only narrates, never estimates |
| **11 intelligence modules** | Reimbursement tracking, competitive landscape, HTA profiles, patient population, IRP risk, and more |
| **SVG charts** | Bar, pie, and line charts — no charting library dependencies |
| **Session isolation** | Each user's data is fully isolated — no cross-contamination |
| **Demo mode** | Try the platform without uploading a file |
| **Export** | Download filtered data as Excel with analytics sheets |

---

## Architecture

```
Browser (React UI)
      ↕  HTTP — no API keys, no raw data exposed
FastAPI Server (Python)
      ↕  Server-side only
  ┌─────────────────────────────────────┐
  │  Anthropic API (key in .env)        │
  │  pandas filter engine               │
  │  SQLite session store               │
  │  Rate limiter (slowapi)             │
  └─────────────────────────────────────┘
```

**Three-stage pipeline — data accuracy guarantee:**

1. **Intent Parser** (Claude Haiku) — converts natural language to a structured filter spec
2. **Filter Engine** (pandas) — executes filters deterministically against your dataset
3. **Narrator** (Claude Haiku) — writes analytical prose from pre-computed stats

Claude never touches raw data values. Every number in every response comes from pandas.

---

## Quick Start

### Prerequisites

- Python 3.11 or higher
- An [Anthropic API key](https://console.anthropic.com)

### Install and run

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
# Edit .env and add your Anthropic API key

# Start the server
python app.py
```

Open **http://localhost:8000** in Chrome or Edge.

### Environment variables

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
DAILY_TOKEN_BUDGET=500000
MAX_FILE_SIZE_MB=20
SESSION_EXPIRY_HOURS=4
```

---

## Dataset Requirements

MAIP works with **any** structured dataset. Upload a `.xlsx`, `.xls`, or `.csv` file and the schema is detected automatically from your column headers.

For reimbursement tracking, the platform works best when your dataset includes columns representing:

| Concept | Example column names |
|---|---|
| Product / Brand | `Product`, `Brand`, `Drug`, `Molecule` |
| Country / Market | `Country`, `Market`, `Territory` |
| Status | `Status`, `Reimbursement Status`, `Access Status` |
| Period | `Period`, `Quarter Period`, `Time Period` |
| Region | `Region`, `Geography`, `Area` |
| Sector | `Sector`, `Channel`, `Payer Setting` |

The platform auto-detects these roles and enables corresponding capabilities. Undetected roles can be manually assigned via the upload wizard.

**Column names and values are never hardcoded** — the platform adapts to whatever headers and values are in your file.

---

## Intelligence Modules

| # | Module | Description |
|---|---|---|
| 1 | **Reimbursement Intelligence** | NL query, filters, charts, paginated table |
| 2 | **Indication Research** | Live web research → structured indication cards |
| 3 | **Competitive Landscape** | Competitor profiles per product + country |
| 4 | **Timeline Intelligence** | Typical months to reimbursement, HTA milestones |
| 5 | **HTA & Public Sector** | Per-country payer landscape, ICER thresholds |
| 6 | **Patient Population** | Prevalence, treated patients, epidemiology |
| 7 | **Analog Intelligence** | Precedent products, outcomes, timelines |
| 8 | **IRP Risk Analysis** | Reference pricing cascade, launch sequencing |
| 9 | **Evidence Gap Analysis** | Clinical data gaps vs HTA requirements |
| 10 | **Hospital & Channel** | Formulary access, KOL landscape |
| 11 | **Payer Landscape** | Payer mix, coverage criteria, managed entry |
| 12 | **Executive Dashboard** | KPI tiles, coverage heatmap, gap analysis |

Modules 2–11 use Claude Sonnet with web search for live research. Results are cached per session.

---

## API Reference

All endpoints are served by FastAPI at `http://localhost:8000`.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Serve frontend |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/demo` | Create demo session (no file needed) |
| `GET` | `/api/stats` | Usage statistics and token budget |
| `POST` | `/api/upload` | Upload dataset file |
| `POST` | `/api/query` | Natural language query |
| `POST` | `/api/research` | Market intelligence research |
| `POST` | `/api/export/excel` | Download filtered data as Excel |
| `POST` | `/api/kpi` | Compute dashboard KPIs |
| `POST` | `/api/gap_analysis` | Identify overdue planned records |
| `POST` | `/api/annotate` | Add note to a data row |
| `GET` | `/api/annotations/{session_id}` | Get all annotations |
| `GET` | `/api/history/{session_id}` | Query history |
| `GET` | `/api/sessions` | List all sessions |
| `GET` | `/api/audit` | Audit log |

Interactive API docs available at `http://localhost:8000/docs` (FastAPI auto-generated).

---

## Deployment

### Railway (Recommended for public demos)

1. Fork this repository
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your fork
4. Add environment variables in the Variables tab
5. Railway generates a public URL automatically

### Azure App Service (Enterprise / BMS)

```bash
az group create --name maip-rg --location uaenorth
az webapp create --resource-group maip-rg \
  --plan maip-plan --name maip-bms \
  --runtime "PYTHON:3.11"
az webapp config appsettings set \
  --resource-group maip-rg --name maip-bms \
  --settings ANTHROPIC_API_KEY="sk-ant-..."
git push azure main
```

Add Azure AD authentication to gate access to Microsoft account holders.

---

## Cost Estimate

MAIP uses [Claude Haiku](https://anthropic.com/claude/haiku) for query processing (fast, low cost) and Claude Sonnet for market intelligence research.

| Usage Level | Sessions/month | Estimated AI Cost |
|---|---|---|
| Personal demo | ~50 | ~$0.30 |
| Small team (10 people) | ~200 | ~$1.20 |
| Department (50 people) | ~500 | ~$3.00 |
| Public demo (100+ visitors) | ~1,000 | ~$6.00 |

Set a monthly spend limit at [console.anthropic.com](https://console.anthropic.com) → Settings → Billing as a safety net.

---

## Project Structure

```
maip-v5/
├── app.py                  # FastAPI backend — all AI, data processing, routes
├── templates/
│   └── index.html          # React frontend — served by FastAPI
├── requirements.txt        # Python dependencies
├── .env                    # API key and config (not committed to Git)
├── .gitignore
└── README.md
```

The entire application runs from two files: `app.py` and `templates/index.html`.

---

## Security

- **API key is server-side only** — never exposed to the browser
- **Session isolation** — each user's uploaded data is bound to their session ID
- **Session expiry** — sessions auto-delete after 4 hours
- **Rate limiting** — 30 queries/hour and 5 research calls/hour per IP
- **File validation** — type and size checked before processing
- **No permanent data storage** — uploaded files exist only in the session

---

## Contributing

This project is built and maintained by the BMS LR Market Access team.

To suggest improvements or report issues:
1. Open an issue on GitHub describing the problem or suggestion
2. For bugs: include the exact query you typed and the unexpected result
3. For features: describe the market access scenario you are trying to solve

---

## Built With

- [FastAPI](https://fastapi.tiangolo.com) — Python web framework
- [Anthropic Claude](https://anthropic.com) — AI language model (Haiku + Sonnet)
- [pandas](https://pandas.pydata.org) — Data processing and filtering
- [React 18](https://react.dev) — Frontend UI
- [SheetJS](https://sheetjs.com) — Excel file parsing in browser
- [slowapi](https://github.com/laurentS/slowapi) — Rate limiting
- [SQLite](https://sqlite.org) — Session and audit storage

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*MAIP v5.0 — Market Access Intelligence Platform*
*Bristol Myers Squibb · LR Markets · Built with Claude*
