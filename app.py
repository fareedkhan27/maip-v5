# app.py
# pip install fastapi uvicorn[standard] anthropic pandas
#             openpyxl python-dotenv python-multipart
#             slowapi jinja2
# Create .env: ANTHROPIC_API_KEY=sk-ant-...
# Run: python app.py → http://localhost:8000

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Header, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import anthropic, pandas as pd, sqlite3, json, os, io
import hashlib, re, difflib, time, traceback

load_dotenv()

ACCESS_KEY = os.getenv("ACCESS_KEY")
if not ACCESS_KEY:
    import sys
    print("FATAL: ACCESS_KEY environment variable is not set. Server will not start.")
    sys.exit(1)

DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "500000"))

def require_access_key(x_access_key: str = Header(default=None)):
    if not x_access_key or x_access_key != ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing access key.")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
SESSION_EXPIRY_HOURS = int(os.getenv("SESSION_EXPIRY_HOURS", "4"))

# ── Daily token budget tracker ─────────────────────────────────────
_token_usage = {
    "count": 0,
    "reset_at": time.time() + 86400
}


def _check_budget(estimated: int = 2000):
    """Raise HTTP 429 if daily token budget is exceeded."""
    now = time.time()
    if now > _token_usage["reset_at"]:
        _token_usage["count"] = 0
        _token_usage["reset_at"] = now + 86400
    if _token_usage["count"] + estimated > DAILY_TOKEN_BUDGET:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily AI query budget reached "
                f"({DAILY_TOKEN_BUDGET:,} tokens). "
                f"Resets in 24 hours."
            )
        )
    _token_usage["count"] += estimated
# ───────────────────────────────────────────────────────────────────

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maip.db")

limiter = Limiter(key_func=get_remote_address)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT,
        row_count INTEGER,
        schema_json TEXT,
        profile_json TEXT,
        data_json TEXT,
        loaded_at TEXT,
        expires_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        original_query TEXT,
        expanded_query TEXT,
        filter_spec TEXT,
        confidence_score REAL,
        result_count INTEGER,
        status_breakdown TEXT,
        product_breakdown TEXT,
        execution_ms INTEGER,
        timestamp TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS research_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_key TEXT UNIQUE,
        subtype TEXT,
        context_json TEXT,
        result_text TEXT,
        confidence TEXT,
        sources_json TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        row_key TEXT,
        note_text TEXT,
        author TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT,
        details TEXT,
        ip_address TEXT,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER,
        query_text   TEXT,
        issue_type   TEXT,
        detail       TEXT,
        ip_address   TEXT,
        user_agent   TEXT,
        timestamp    TEXT
    )""")
    # Migration: add expires_at column to sessions if not present
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    conn.commit()
    conn.close()


def log_audit(action: str, details: str, ip: str = ""):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO audit_log (action, details, ip_address, timestamp) VALUES (?,?,?,?)",
            (action, details[:2000], ip, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        print(f"✅ ANTHROPIC_API_KEY loaded (ends ...{key[-4:]})")
    else:
        print("⚠️  ANTHROPIC_API_KEY not set — AI features disabled")
    print("✅ SQLite database initialized")
    print("✅ MAIP v5.0 ready")
    # ── Clean up expired sessions on startup ───────────────────────────
    try:
        _c = sqlite3.connect(DB_PATH)
        _c.execute(
            "DELETE FROM sessions "
            "WHERE expires_at IS NOT NULL "
            "AND expires_at < ?",
            [datetime.utcnow().isoformat()]
        )
        _c.commit()
        _c.close()
        print("✅ Expired sessions cleaned up")
    except Exception as _e:
        print(f"⚠️ Session cleanup skipped: {_e}")
    # ───────────────────────────────────────────────────────────────────
    yield


app = FastAPI(title="MAIP", version="5.0", lifespan=lifespan)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"error": "Rate limit exceeded. Please wait before trying again."})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://lrmaip.com",
        "https://www.lrmaip.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

client = None
try:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key:
        client = anthropic.Anthropic(api_key=api_key)
except Exception:
    client = None


# ─────────────────────────────────────────────
# ABBREVIATION EXPANSION
# ─────────────────────────────────────────────

STATIC_ABBREVS = {
    r"\breimb\b": "Reimbursed",
    r"\brimb\b": "Reimbursed",
    r"\breimbu\b": "Reimbursed",
    r"\breimbusement\b": "Reimbursed",
    r"\bapproved\b": "Reimbursed",
    r"\bactive\b": "Reimbursed",
    r"\bpending\b": "Planned",
    r"\bpipeline\b": "Planned",
    r"\bupcoming\b": "Planned",
    r"\bplan\b": "Planned",
    r"\bplann\b": "Planned",
    r"\bmea\b": "LR MEA",
    r"\blatam\b": "LR LATAM",
    r"\blat\b": "LR LATAM",
    r"\blac\b": "LR LATAM",
    r"\bcee\b": "LR EU",
    r"\bpriv\b": "Private",
    r"\boop\b": "Private",
    r"\bout.of.pocket\b": "Private",
    r"\bpub\b": "Public",
    r"\bnhs\b": "Public",
    r"\bnational\b": "Public",
    r"\bbar\b": "bar chart",
    r"\bpie\b": "pie chart",
    r"\bdonut\b": "pie chart",
    r"\bline\b": "line chart",
    r"\btrend\b": "line chart over time",
    r"\bsparkline\b": "line chart",
    r"\bnsclc\b": "non-small cell lung cancer",
    r"\bsclc\b": "small cell lung cancer",
    r"\brcc\b": "renal cell carcinoma",
    r"\bhcc\b": "hepatocellular carcinoma",
    r"\bmds\b": "myelodysplastic syndrome",
    r"\btdt\b": "transfusion dependent thalassemia",
    r"\bntdt\b": "non-transfusion dependent thalassemia",
    r"\bcrc\b": "colorectal cancer",
    r"\bhnscc\b": "head and neck squamous cell carcinoma",
    r"\bescc\b": "esophageal squamous cell carcinoma",
    r"\bmpm\b": "mesothelioma",
    r"\bchl\b": "hodgkin lymphoma",
    r"\bgc\b": "gastric cancer",
    r"\bgej\b": "gastroesophageal junction",
    r"\bmsi.?h\b": "microsatellite instability",
    r"\bpdl1\b": "PD-L1",
    r"\bhta\b": "health technology assessment",
    r"\birp\b": "international reference pricing",
    r"\besa\b": "erythropoiesis stimulating agent",
    r"\brbc\b": "red blood cell",
    r"\bl1\b": "first line",
    r"\bl2\b": "second line",
    r"\b1l\b": "first line",
    r"\b2l\b": "second line",
}


def expand_abbreviations(query: str, profile: dict) -> str:
    q = query.strip()
    for pattern, replacement in STATIC_ABBREVS.items():
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    q = fuzzy_match_to_dataset_values(q, profile)
    return q


def fuzzy_match_to_dataset_values(query: str, profile: dict) -> str:
    all_values = []
    uv = profile.get("uniqueValues", {})
    for col, vals in uv.items():
        if isinstance(vals, list) and len(vals) < 200:
            all_values.extend([(str(v), col) for v in vals])
    if not all_values:
        return query

    candidates = [v for v, c in all_values]
    candidates_lower = [c.lower() for c in candidates]

    words = query.split()
    corrected = []
    i = 0
    while i < len(words):
        matched = False
        for span in range(min(4, len(words) - i), 0, -1):
            phrase = " ".join(words[i : i + span])
            if len(phrase) < 3:
                continue
            matches = difflib.get_close_matches(
                phrase.lower(), candidates_lower, n=1, cutoff=0.82
            )
            if matches:
                idx = candidates_lower.index(matches[0])
                corrected.append(candidates[idx])
                i += span
                matched = True
                break
        if not matched:
            corrected.append(words[i])
            i += 1
    return " ".join(corrected)


def find_closest(val: str, known: list, n: int = 3) -> list:
    if not known:
        return []
    matches = difflib.get_close_matches(val.lower(), [str(k).lower() for k in known], n=n, cutoff=0.5)
    result = []
    known_lower = [str(k).lower() for k in known]
    for m in matches:
        if m in known_lower:
            idx = known_lower.index(m)
            result.append(str(known[idx]))
    return result


# ─────────────────────────────────────────────
# SCHEMA DETECTION
# ─────────────────────────────────────────────

ROLE_PATTERNS = {
    "PRODUCT": ["product", "brand", "drug", "molecule", "medicine", "therapy", "treatment", "asset"],
    "COUNTRY": ["country", "market", "nation", "territory", "geography", "location", "site"],
    "INDICATION": ["indication", "ind", "disease", "tumor", "tumour", "condition", "therapeutic area", "therapy area", "ta", "oncology", "hematology", "diagnosis"],
    "REGION": ["region", "geo", "area", "zone", "cluster"],
    "SECTOR": ["sector", "channel", "payer", "setting", "access", "funding", "coverage type", "segment"],
    "YEAR": ["year", "yr", "fy", "fiscal year", "cal year"],
    "QUARTER": ["quarter", "qtr", "period quarter"],
    "STATUS": ["status", "reimbursement", "access status", "approval", "coverage", "decision"],
    "PERIOD": ["period", "time period", "timeframe", "date period", "quarter period"],
    "DATASOURCE": ["datasource", "source", "data source", "origin"],
}


def detect_schema(df: pd.DataFrame) -> dict:
    columns = []
    roles = {}
    confidence = {}

    for col in df.columns:
        col_lower = col.strip().lower()
        nunique = int(df[col].nunique())
        unique_vals = sorted([str(v) for v in df[col].dropna().unique()])[:500]
        sample_vals = [str(v) for v in df[col].dropna().head(5).tolist()]

        if pd.api.types.is_numeric_dtype(df[col]):
            col_type = "numeric"
        elif nunique < 100:
            col_type = "categorical"
        else:
            col_type = "text"

        columns.append(
            {
                "name": col,
                "col_type": col_type,
                "unique_count": nunique,
                "unique_values": unique_vals,
                "sample_values": sample_vals,
            }
        )

        best_role = None
        best_score = 0.0
        for role, patterns in ROLE_PATTERNS.items():
            for pat in patterns:
                if col_lower == pat:
                    score = 1.0
                elif pat in col_lower:
                    score = 0.8
                elif col_lower in pat:
                    score = 0.6
                else:
                    score = 0.0
                if score > best_score:
                    best_score = score
                    best_role = role

        if best_role and best_score >= 0.6:
            if best_role not in roles or best_score > confidence.get(best_role, 0):
                roles[best_role] = col
                confidence[best_role] = best_score

    return {"columns": columns, "roles": roles, "confidence": confidence}


def build_profile(df: pd.DataFrame, schema: dict) -> dict:
    profile = {"uniqueValues": {}, "rowCount": len(df)}
    for col_info in schema["columns"]:
        col = col_info["name"]
        vals = col_info["unique_values"]
        profile["uniqueValues"][col] = vals

    product_col = schema["roles"].get("PRODUCT")
    country_col = schema["roles"].get("COUNTRY")
    status_col = schema["roles"].get("STATUS")
    region_col = schema["roles"].get("REGION")
    period_col = schema["roles"].get("PERIOD")
    indication_col = schema["roles"].get("INDICATION")

    if product_col and product_col in df.columns:
        profile["productValues"] = sorted(df[product_col].dropna().unique().tolist())
    if country_col and country_col in df.columns:
        profile["countryValues"] = sorted(df[country_col].dropna().unique().tolist())
    if status_col and status_col in df.columns:
        profile["statusValues"] = sorted(df[status_col].dropna().unique().tolist())
    if region_col and region_col in df.columns:
        profile["regionValues"] = sorted(df[region_col].dropna().unique().tolist())
    if period_col and period_col in df.columns:
        profile["periodValues"] = sorted(df[period_col].dropna().unique().tolist())
    if indication_col and indication_col in df.columns:
        profile["indicationValues"] = sorted(df[indication_col].dropna().unique().tolist())

    return profile


# ─────────────────────────────────────────────
# DATA QUALITY AUDIT
# ─────────────────────────────────────────────

def audit_data_quality(df: pd.DataFrame, schema: dict) -> dict:
    score = 100
    issues = []

    for col in df.columns:
        null_rate = df[col].isnull().mean()
        if null_rate > 0.05:
            score -= 5
            issues.append({"severity": "warning", "message": f"Column '{col}' has {null_rate*100:.1f}% null values"})

    dup_count = df.duplicated().sum()
    if dup_count > 0:
        score -= 10
        issues.append({"severity": "warning", "message": f"{dup_count} duplicate rows detected"})

    for col in df.select_dtypes(include=["object"]).columns:
        has_ws = (df[col].dropna().str.len() != df[col].dropna().str.strip().str.len()).any()
        if has_ws:
            score -= 10
            issues.append({"severity": "error", "message": f"Column '{col}' has leading/trailing whitespace (auto-stripped)"})
            break

    single_val_cols = [col for col in df.columns if df[col].nunique() == 1]
    for col in single_val_cols:
        score -= 5
        issues.append({"severity": "info", "message": f"Column '{col}' has only one unique value"})

    period_col = schema["roles"].get("PERIOD")
    if period_col and period_col in df.columns:
        periods = sorted(df[period_col].dropna().unique())
        if len(periods) >= 2:
            pass

    score = max(0, min(100, score))
    return {"score": score, "issues": issues, "total_checks": 7}


# ─────────────────────────────────────────────
# FILTER ENGINE
# ─────────────────────────────────────────────

def resolve_role_to_column(role: str, schema: dict) -> Optional[str]:
    roles = schema.get("roles", {})
    role_upper = role.upper()
    if role_upper in roles and roles[role_upper]:
        return roles[role_upper]
    for k, v in roles.items():
        if k.lower() == role.lower() and v:
            return v
    cols = [c["name"] for c in schema.get("columns", []) if isinstance(c, dict)]
    if role in cols:
        return role
    for c in cols:
        if c.lower() == role.lower():
            return c
    return None


def apply_filters(df: pd.DataFrame, filters: dict, schema: dict) -> pd.DataFrame:
    result = df.copy()
    product_col = schema["roles"].get("PRODUCT")

    for role, values in filters.items():
        if not values:
            continue
        col = resolve_role_to_column(role, schema)
        if not col or col not in result.columns:
            continue
        clean_vals = [str(v).strip() for v in values if v is not None and str(v).strip()]
        if not clean_vals:
            continue

        if col == product_col:
            mask = result[col].astype(str).str.strip().str.lower().isin([v.lower() for v in clean_vals])
        else:
            mask = pd.Series([False] * len(result), index=result.index)
            for val in clean_vals:
                mask = mask | result[col].astype(str).str.strip().str.lower().str.contains(val.lower(), na=False, regex=False)

        result = result[mask]

    return result


# ─────────────────────────────────────────────
# STATS COMPUTATION
# ─────────────────────────────────────────────

def compute_stats(df: pd.DataFrame, schema: dict) -> dict:
    status_col = schema["roles"].get("STATUS")
    product_col = schema["roles"].get("PRODUCT")
    country_col = schema["roles"].get("COUNTRY")
    region_col = schema["roles"].get("REGION")
    period_col = schema["roles"].get("PERIOD")

    stats = {
        "total": len(df),
        "status_breakdown": {},
        "product_breakdown": {},
        "country_breakdown": {},
        "region_breakdown": {},
        "period_breakdown": {},
        "unique_counts": {},
        "top_by_status": {},
    }

    if status_col and status_col in df.columns:
        stats["status_breakdown"] = df[status_col].value_counts().to_dict()
    if product_col and product_col in df.columns:
        stats["product_breakdown"] = df[product_col].value_counts().to_dict()
    if country_col and country_col in df.columns:
        stats["country_breakdown"] = df[country_col].value_counts().to_dict()
    if region_col and region_col in df.columns:
        stats["region_breakdown"] = df[region_col].value_counts().to_dict()
    if period_col and period_col in df.columns:
        stats["period_breakdown"] = df[period_col].value_counts().to_dict()

    if product_col and status_col and product_col in df.columns and status_col in df.columns:
        for sv in df[status_col].unique():
            sub = df[df[status_col] == sv]
            if len(sub) > 0:
                vc = sub[product_col].value_counts()
                key = f"top_product_{str(sv).lower().replace(' ', '_')}"
                stats["top_by_status"][key] = {"product": str(vc.index[0]), "count": int(vc.iloc[0])}

    for col in df.columns:
        if df[col].nunique() < 500:
            stats["unique_counts"][col] = int(df[col].nunique())

    return stats


# ─────────────────────────────────────────────
# CHART DATA BUILDER
# ─────────────────────────────────────────────

def build_chart_data(df: pd.DataFrame, viz_spec: dict, schema: dict, profile: dict) -> Optional[dict]:
    if not viz_spec or not viz_spec.get("requested"):
        return None

    chart_type = viz_spec.get("type", "bar")
    x_field = viz_spec.get("x_field")
    title = viz_spec.get("title", "")

    col = resolve_role_to_column(x_field, schema) if x_field else None

    if not col or col not in df.columns:
        if chart_type == "line":
            col = schema["roles"].get("PERIOD")
        elif chart_type == "pie":
            col = schema["roles"].get("STATUS")
        else:
            col = schema["roles"].get("PRODUCT") or schema["roles"].get("STATUS")

    if not col or col not in df.columns:
        return None

    if chart_type == "line":
        period_col = schema["roles"].get("PERIOD")
        if col == period_col:
            all_periods = sorted(profile.get("uniqueValues", {}).get(col, []))
            counts = df[col].value_counts().to_dict()
            data = [{"name": str(p), "value": counts.get(p, 0)} for p in all_periods]
        else:
            counts = df[col].value_counts()
            data = [{"name": str(k), "value": int(v)} for k, v in counts.items()]
    else:
        counts = df[col].value_counts()
        data = [{"name": str(k), "value": int(v)} for k, v in counts.items()]

    if chart_type != "line":
        data = sorted(data, key=lambda x: x["value"], reverse=True)

    if not title:
        title = f"Distribution by {col} ({len(df):,} records)"

    # Surface the semantic dimension so the client can select chart type without AI
    dimension = "product"
    for role, rcol in schema.get("roles", {}).items():
        if rcol == col:
            dimension = role.lower()
            break
    return {"type": chart_type, "x_field": col, "title": title, "data": data, "dimension": dimension}


# ─────────────────────────────────────────────
# CONFIDENCE SCORING
# ─────────────────────────────────────────────

def score_filter_confidence(filter_spec: dict, profile: dict, schema: dict) -> dict:
    score = 1.0
    warnings = []

    filters = filter_spec.get("filters", {})
    for field, values in filters.items():
        if not values:
            continue
        col = resolve_role_to_column(field, schema)
        if not col:
            score -= 0.1
            warnings.append(f"Column for '{field}' not detected in dataset")
            continue
        known = [str(v).lower() for v in profile.get("uniqueValues", {}).get(col, [])]
        for val in values:
            if str(val).lower() not in known:
                closest = find_closest(str(val), profile.get("uniqueValues", {}).get(col, []))
                score -= 0.15
                closest_str = ", ".join(closest[:3]) if closest else "none"
                warnings.append(f"'{val}' not found exactly in {col}. Closest: {closest_str}")

    return {"score": max(0.0, round(score, 2)), "warnings": warnings, "reliable": score >= 0.7}


def generate_correction_suggestions(filter_spec: dict, profile: dict, schema: dict) -> list:
    suggestions = []
    filters = filter_spec.get("filters", {})
    for field, values in filters.items():
        if not values:
            continue
        col = resolve_role_to_column(field, schema)
        if not col:
            continue
        known = profile.get("uniqueValues", {}).get(col, [])
        for val in values:
            closest = find_closest(str(val), known, n=1)
            if closest and closest[0].lower() != str(val).lower():
                suggestions.append(f"Did you mean '{closest[0]}' for {col}?")
    return suggestions[:3]


# ─────────────────────────────────────────────
# AI PROMPT BUILDERS
# ─────────────────────────────────────────────

def trim_stats_for_narrator(stats: dict) -> dict:
    trimmed = {}
    for k, v in stats.items():
        if isinstance(v, dict) and len(v) > 8:
            trimmed[k] = dict(list(v.items())[:8])
        else:
            trimmed[k] = v
    return trimmed


def build_intent_parser_prompt(profile: dict, schema: dict) -> str:
    col_listings = []
    for col_info in schema["columns"]:
        col = col_info["name"]
        vals = profile.get("uniqueValues", {}).get(col, [])[:30]
        more = col_info["unique_count"] - min(30, len(vals))
        line = f'  {col} ({col_info["col_type"]}): '
        line += " | ".join(str(v) for v in vals)
        if more > 0:
            line += f" (+ {more} more)"
        col_listings.append(line)

    role_lines = [f"  {role} → column '{col}'" for role, col in schema["roles"].items() if col]

    return f"""You are a precise query intent parser for a structured dataset.

Parse the user's natural language query (already abbreviation-expanded) into a structured JSON filter spec.
Return ONLY valid JSON. No markdown. No explanation. No code fences.

DATASET SCHEMA:
{chr(10).join(col_listings)}

DETECTED SEMANTIC ROLES:
{chr(10).join(role_lines)}

FILTER RULES:
- Empty array [] = no filter on that field (include all)
- Multiple values = OR logic; cross-field = AND logic
- PRODUCT field: use EXACT case-insensitive match (never substring — "Opdivo" ≠ "Opdivo+CT")
- ALL other fields: substring, case-insensitive is fine
- Resolve status synonyms: "reimbursed","active","approved","listed" → match the closest actual status value. "planned","pending","pipeline","upcoming" → match the closest actual status value.
- "current" or "now" → filter Period to most recent period
- Normalise case: match against known values case-insensitively

AGGREGATION:
- "how many" / "count" / "number of" → type:"count"
- "list" / "show" / "which" / "what" → type:"list"
- "by X" / "per X" / "group by X" → type:"group_by", group_by_field = EXACT column name

CHART GROUPING (CRITICAL — never default to Status for bar charts):
- Extract grouping dimension from "by X" / "per X" patterns
- "by product" → x_field: exact PRODUCT column name
- "by country" → x_field: exact COUNTRY column name
- "by region" → x_field: exact REGION column name
- "by sector" → x_field: exact SECTOR column name
- "by indication" → x_field: exact INDICATION column name
- "by status" → x_field: exact STATUS column name
- "over time" / "trend" / "timeline" → x_field: PERIOD column name
- Default if no "by X" present: bar → PRODUCT column name, pie → STATUS column name, line → PERIOD column name

VISUALIZATION:
- "bar" / "compare" → type:"bar"
- "pie" / "distribution" / "share" / "proportion" → type:"pie"
- "line" / "trend" / "over time" / "timeline" → type:"line"
- "chart" / "plot" / "graph" → type:"bar" (default)
- title: generate a descriptive title from the query
- If user asks for a chart, set requested:true

MARKET INTELLIGENCE ROUTING:
If the query contains any of these concepts, set marketIntelligence.triggered: true and choose subtype:
  competitor/competing/vs/versus/alternatives/landscape → subtype: "competitive"
  timeline/how long/when expected/typical/forecast → subtype: "timeline"
  HTA/payer assessment/health technology/cost-effective → subtype: "public_sector"
  patients/prevalence/incidence/how many patients/eligible → subtype: "patient_population"
  analog/similar product/precedent/comparable launch → subtype: "analog"
  IRP/international reference/price corridor/reference pricing → subtype: "irp_risk"
  evidence/clinical data/trial/PICO/gap/RWE → subtype: "evidence_gap"
  hospital/formulary/institution/prescriber/KOL → subtype: "hospital_channel"
  payer/insurer/coverage/reimbursement body/scheme → subtype: "payer_landscape"

RETURN EXACTLY THIS JSON SHAPE (no other text):
{{
  "filters": {{
    "product": [], "country": [], "indication": [],
    "region": [], "sector": [], "year": [],
    "quarter": [], "status": [], "period": []
  }},
  "aggregation": {{
    "type": "list",
    "group_by_field": null
  }},
  "visualization": {{
    "requested": false,
    "type": null,
    "x_field": null,
    "title": null
  }},
  "marketIntelligence": {{
    "triggered": false,
    "subtype": null,
    "context": {{
      "product": null, "country": null,
      "indication": null, "region": null
    }}
  }}
}}"""


NARRATOR_SYSTEM_PROMPT = """You are a senior Market Access Intelligence Analyst. You receive pre-computed query results from a reimbursement tracking platform.

CRITICAL RULES:
1. Every number you write must come from the provided stats. Never invent, estimate, or round figures.
2. If total = 0: state clearly that no records were found. Do not make up data or reference the full dataset.
3. If total > 0: lead with the exact count.

RESPONSE STRUCTURE — always follow this exact format:

**FINDING:** [One sentence — the direct answer with exact numbers]

**DATA SNAPSHOT:**
[2–3 bullet points with specific counts, percentages, breakdowns]

**MARKET ACCESS CONTEXT:**
[1–2 sentences — what this means for reimbursement planning, stakeholder positioning, or access strategy]

**SIGNAL:**
[One specific watch item or risk flag relevant to the data]

**SUGGESTED NEXT QUERY:**
[One natural follow-up question the user could ask]

TONE: Professional, direct, decisive. No hedging language. No "it appears that" or "it seems like". State facts from the data with confidence.

LENGTH: 120–180 words total. Never exceed this. Quality over quantity — every sentence adds value.

ZERO RESULT RESPONSE FORMAT:
**FINDING:** No records found matching the applied filters.

**WHY:** The filter value is not present in the current dataset.

**AVAILABLE OPTIONS:** [List 5–8 actual values from the same column if provided]

**SUGGESTED NEXT QUERY:** [Corrected version of the query]"""


# ─────────────────────────────────────────────
# RESEARCH MODULE PROMPTS
# ─────────────────────────────────────────────

RESEARCH_PROMPTS = {
    "indication": """You are a pharmaceutical regulatory intelligence analyst.
Research approved and late-stage pipeline indications for the specified product(s). Return ONLY a JSON array where each element contains:
{{"indication":"...", "line_of_therapy":"...", "trial_name":"...", "trial_code":"...", "regulatory_status":"...", "key_combinations":"...", "therapy_area":"...", "approved_markets":"...", "approval_year":"...", "notes":"..."}}
Include FDA, EMA, and major market approvals. No markdown. No explanation. JSON array only.""",
    "competitive": """You are a competitive intelligence analyst for pharmaceutical market access.
Research the competitive landscape for the specified product and indication.
Structure your response with these sections:
## Competitor Products
## Approval & Reimbursement Status
## Market Positioning
## Biosimilar/Generic Threats
## Key Differentiators
## Strategic Implications
Label each claim: [Verified] [Likely] [Estimate]
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "timeline": """You are a market access timeline intelligence analyst.
Research typical timelines for reimbursement in the specified market.
Structure your response with these sections:
## Typical Timeline (months)
## HTA Milestones
## Historical Precedents
## Risk Factors
## Fast-Track Paths
## Overall Confidence: High/Medium/Low
Include specific month ranges. Cite precedents.
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "public_sector": """You are an HTA and public sector market access analyst.
Research the HTA body and assessment process for the specified market.
Structure your response with these sections:
## HTA Body and Mandate
## Assessment Process
## Evaluation Criteria
## Cost-Effectiveness Thresholds
## Budget Impact Requirements
## Patient Access Pathway
## Recent Notable Decisions (last 24 months)
Note data currency: [Current as of YYYY] or [Estimated]
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "patient_population": """You are a pharmaceutical epidemiology analyst.
Research patient population data for the specified indication and market.
Structure your response with these sections:
## Total Patient Pool
## Diagnosed Patients
## Currently Treated
## Treatment Rate
## Annual Incidence
## Data Sources and Years
## Confidence: High/Medium/Low/Modelled
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "analog": """You are a market access analog intelligence analyst.
Research precedent products for the specified context.
Structure your response with these sections:
## Approved Analogs (last 5 years)
## Failed Cases
## Time-to-Reimbursement Comparison
## Key Success Factors
## Lessons for Current Product
Include company, indication, months to decision, outcome.
Label each claim [Verified] [Likely] [Estimate]. If no source supports a claim, mark it [Estimate].
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "irp_risk": """You are an international reference pricing analyst.
Research IRP risks for the specified product and markets.
Structure your response with these sections:
## Referencing Markets
## Price Corridor Impact
## Recommended Launch Sequence
## High-Risk Combinations
## Mitigation Strategies (managed entry, outcomes-based)
Flag any IRP framework changes in last 12 months.
Label each claim [Verified] [Likely] [Estimate]. If no source supports a claim, mark it [Estimate].
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "evidence_gap": """You are a health economics and outcomes research analyst.
Research evidence requirements for the specified context.
Structure your response with these sections:
## Required Evidence Package
## Current Inventory
## Critical Gaps [Critical/Important/Nice-to-Have]
## RWE Requirements
## PICO Framework
## Timeline to Address
Label each claim [Verified] [Likely] [Estimate]. If no source supports a claim, mark it [Estimate].
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "hospital_channel": """You are a hospital and institutional market access analyst.
Research hospital channel access for the specified context.
Structure your response with these sections:
## Hospital Formulary Process
## Key Prescribing Centres
## KOL Landscape
## Institutional vs Retail Access
## Tender/Procurement Process
## Channel Strategy Implications
Label each claim [Verified] [Likely] [Estimate]. If no source supports a claim, mark it [Estimate].
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
    "payer_landscape": """You are a payer landscape intelligence analyst.
Research the payer landscape for the specified market.
Structure your response with these sections:
## Payer Mix (public/private/social insurance)
## Key Decision Makers
## Coverage Criteria
## Managed Entry Agreement Precedents
## Patient Co-payment Structure
## Budget Holder Priorities
Label each claim [Verified] [Likely] [Estimate]. If no source supports a claim, mark it [Estimate].
Start directly with the first section header. No preamble. No introduction. No concluding summary.""",
}


# ─────────────────────────────────────────────
# RETRY UTILITY
# ─────────────────────────────────────────────

RETRYABLE_STATUS_CODES = {429, 529}
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2  # exponential: 2, 4, 8


def call_anthropic_with_retry(fn, *args, **kwargs):
    """
    Wraps any Anthropic API call with exponential backoff retry.
    Retries on 529 (overloaded) and 429 (rate limit) only.
    Raises on all other errors immediately.
    """
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code in RETRYABLE_STATUS_CODES:
                last_exception = e
                wait = BASE_DELAY_SECONDS * (2 ** attempt)
                time.sleep(wait)
            else:
                raise  # non-retryable: surface immediately
        except anthropic.APIConnectionError as e:
            last_exception = e
            wait = BASE_DELAY_SECONDS * (2 ** attempt)
            time.sleep(wait)
    raise last_exception  # all retries exhausted


def map_api_error_to_user_message(e: Exception) -> dict:
    """
    Returns a structured error dict safe for frontend display.
    Never exposes raw API error strings or request IDs to the user.
    The 'error' key mirrors 'user_message' so the api() JS helper
    can surface it via e.detail||e.error.
    """
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code == 529:
            msg = {
                "error_code": "SERVICE_BUSY",
                "user_message": "Our AI service is experiencing high demand. Please try again in 30 seconds.",
                "retry_suggested": True,
            }
        elif e.status_code == 429:
            msg = {
                "error_code": "RATE_LIMITED",
                "user_message": "Request limit reached. Please wait a moment before running another research query.",
                "retry_suggested": True,
            }
        elif e.status_code in (401, 403):
            msg = {
                "error_code": "AUTH_ERROR",
                "user_message": "A configuration issue was detected. Please contact support.",
                "retry_suggested": False,
            }
        else:
            msg = {
                "error_code": "AI_ERROR",
                "user_message": "The AI research service returned an unexpected error. Please try again.",
                "retry_suggested": True,
            }
    elif isinstance(e, anthropic.APIConnectionError):
        msg = {
            "error_code": "CONNECTION_ERROR",
            "user_message": "Could not reach the AI service. Check your connection and try again.",
            "retry_suggested": True,
        }
    else:
        msg = {
            "error_code": "UNKNOWN_ERROR",
            "user_message": "An unexpected error occurred. Please try again or contact support.",
            "retry_suggested": False,
        }
    # Mirror user_message → error so api() JS helper reads it via e.error
    msg["error"] = msg["user_message"]
    return msg


# ─────────────────────────────────────────────
# MULTI-TURN TOOL USE HANDLER
# ─────────────────────────────────────────────

def call_with_tools(system: str, user_msg: str, model: str = SONNET, max_turns: int = 3) -> str:
    if not client:
        return "AI features unavailable — ANTHROPIC_API_KEY not set."

    messages = [{"role": "user", "content": user_msg}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    response = None

    for turn in range(max_turns):
        try:
            response = call_anthropic_with_retry(
                client.messages.create,
                model=model,
                max_tokens=2000,
                system=system,
                messages=messages,
                tools=tools,
            )
        except Exception as e:
            raise  # propagate to route handler after retries exhausted

        if response.stop_reason == "end_turn":
            return "\n".join(b.text for b in response.content if hasattr(b, "text") and b.type == "text")

        if response.stop_reason == "tool_use":
            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if not tool_use:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": "Search executed successfully"}],
                }
            )
            continue
        break

    if response:
        texts = [b.text for b in response.content if hasattr(b, "text")]
        if texts:
            return "\n".join(texts)
    return "Research completed — no structured response returned."


# ─────────────────────────────────────────────
# SESSION HELPERS
# ─────────────────────────────────────────────

def load_session(session_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    schema = json.loads(row["schema_json"])
    profile = json.loads(row["profile_json"])
    df = pd.DataFrame(json.loads(row["data_json"]))
    return df, schema, profile, row


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/api/health")
async def health():
    return {"status": "ok", "api_key_set": bool(os.getenv("ANTHROPIC_API_KEY")), "db_path": DB_PATH}


@app.get("/api/stats", dependencies=[Depends(require_access_key)])
async def usage_stats():
    """Admin usage overview — live token budget and query counts."""
    conn = get_db()

    total_q = conn.execute(
        "SELECT COUNT(*) FROM queries"
    ).fetchone()[0]

    total_s = conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone()[0]

    today_q = conn.execute(
        "SELECT COUNT(*) FROM queries "
        "WHERE timestamp > date('now')"
    ).fetchone()[0]

    cached_r = conn.execute(
        "SELECT COUNT(*) FROM research_cache"
    ).fetchone()[0]

    haiku_calls_today = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE action='QUERY' AND timestamp > date('now')"
    ).fetchone()[0]

    sonnet_calls_today = conn.execute(
        "SELECT COUNT(*) FROM audit_log "
        "WHERE action='RESEARCH' AND timestamp > date('now')"
    ).fetchone()[0]

    conn.close()

    _pct = round((_token_usage["count"] / DAILY_TOKEN_BUDGET) * 100, 1) if DAILY_TOKEN_BUDGET > 0 else 0
    _status = "critical" if _pct >= 90 else "moderate" if _pct >= 70 else "healthy"

    return {
        "total_queries":          total_q,
        "total_sessions":         total_s,
        "queries_today":          today_q,
        "research_cache_entries": cached_r,
        "token_budget_used":      _token_usage["count"],
        "token_budget_limit":     DAILY_TOKEN_BUDGET,
        "budget_pct_used":        _pct,
        "budget_resets_at":       datetime.fromtimestamp(
            _token_usage["reset_at"]
        ).isoformat(),
        # Enriched fields for token usage indicator
        "haiku_calls_today":      haiku_calls_today,
        "sonnet_calls_today":     sonnet_calls_today,
        "tokens_used_today":      _token_usage["count"],
        "daily_token_limit":      DAILY_TOKEN_BUDGET,
        "pct_used":               _pct,
        "status":                 _status,
    }


@app.get("/api/feedback", dependencies=[Depends(require_access_key)])
async def get_feedback(limit: int = 50):
    """
    Returns recent feedback entries for admin review.
    Visit /api/feedback to see what users are reporting.
    """
    conn  = get_db()
    rows  = conn.execute(
        """SELECT id, session_id, query_text, issue_type,
                  detail, ip_address, timestamp
           FROM feedback_log
           ORDER BY id DESC
           LIMIT ?""",
        [min(limit, 200)]
    ).fetchall()

    return {
        "total":   conn.execute(
            "SELECT COUNT(*) FROM feedback_log"
        ).fetchone()[0],
        "showing": len(rows),
        "entries": [
            {
                "id":         r[0],
                "session_id": r[1],
                "query_text": r[2],
                "issue_type": r[3],
                "detail":     r[4],
                "ip_address": r[5],
                "timestamp":  r[6],
            }
            for r in rows
        ],
    }


@app.get("/api/demo", dependencies=[Depends(require_access_key)])
async def demo(request: Request):
    """
    Creates a temporary 1-hour demo session with synthetic generic data.
    No real product, country, or indication names.
    """
    import random
    random.seed(42)

    DEMO_PRODUCTS  = [
        "Product Alpha", "Product Beta", "Product Gamma",
        "Combination X+Y", "Treatment Z"
    ]
    DEMO_COUNTRIES = [
        "Country A", "Country B", "Country C",
        "Country D", "Country E", "Country F"
    ]
    DEMO_REGIONS   = ["Region North", "Region South", "Region West"]
    DEMO_SECTORS   = ["Public", "Private"]

    country_region = {
        "Country A": "Region North", "Country B": "Region North",
        "Country C": "Region South", "Country D": "Region South",
        "Country E": "Region West",  "Country F": "Region West",
    }
    private_eligible = {"Country A", "Country C", "Country E"}

    rows = []
    for country in DEMO_COUNTRIES:
        region   = country_region[country]
        sectors  = (["Public", "Private"]
                    if country in private_eligible
                    else ["Public"])
        products = random.sample(DEMO_PRODUCTS, random.randint(2, 4))
        for product in products:
            for sector in sectors:
                for year in range(2023, 2027):
                    for q in ["Q1", "Q2", "Q3", "Q4"]:
                        status = (
                            "Active"
                            if year < 2026 or
                               (year == 2026 and q == "Q1")
                            else "Pending"
                        )
                        rows.append({
                            "Country": country,
                            "Product": product,
                            "Region":  region,
                            "Sector":  sector,
                            "Year":    str(year),
                            "Quarter": q,
                            "Status":  status,
                            "Period":  f"{year}-{q}",
                        })

    df  = pd.DataFrame(rows)
    sch = detect_schema(df)

    profile = {
        "totalRows":    len(df),
        "columns":      [c["name"] for c in sch["columns"]],
        "roles":        sch["roles"],
        "uniqueValues": {
            col: sorted(df[col].dropna().unique().tolist())
            for col in df.columns
            if df[col].nunique() < 200
        },
    }

    expires_at = (
        datetime.utcnow() + timedelta(hours=1)
    ).isoformat()

    conn = get_db()
    cur  = conn.execute(
        """INSERT INTO sessions
           (filename, row_count, schema_json,
            profile_json, data_json, loaded_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            "Demo Dataset",
            len(df),
            json.dumps({"columns": sch["columns"],
                        "roles":   sch["roles"]}),
            json.dumps(profile),
            df.to_json(orient="records"),
            datetime.utcnow().isoformat(),
            expires_at,
        ]
    )
    conn.commit()
    session_id = cur.lastrowid

    try:
        log_audit(
            "DEMO_CREATED",
            json.dumps({"session_id": session_id, "rows": len(df)}),
            request.client.host if request.client else "unknown"
        )
    except Exception:
        pass  # log_audit signature safe skip

    return {
        "session_id": session_id,
        "filename":   "Demo Dataset",
        "row_count":  len(df),
        "schema":     {"columns": sch["columns"],
                       "roles":   sch["roles"]},
        "profile":    profile,
        "is_demo":    True,
        "expires_at": expires_at,
        "message":    (
            f"Demo session created with {len(df):,} rows. "
            f"Expires in 1 hour."
        ),
    }


@app.post("/api/upload", dependencies=[Depends(require_access_key)])
@limiter.limit("10/hour")
async def upload(request: Request, file: UploadFile = File(...)):
    # ── Guard 1: File extension ─────────────────────────────────────────
    _ext = os.path.splitext(file.filename or "")[1].lower()
    if _ext not in {".xlsx", ".xls", ".csv"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{_ext}'. "
                f"Please upload a .xlsx, .xls, or .csv file."
            )
        )
    # ── Guard 2: File size ──────────────────────────────────────────────
    _contents = await file.read()
    _size_mb = len(_contents) / (1024 * 1024)
    if _size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({_size_mb:.1f} MB). "
                f"Maximum allowed is {MAX_FILE_SIZE_MB} MB."
            )
        )
    await file.seek(0)
    # ───────────────────────────────────────────────────────────────────
    start = time.time()
    try:
        content = _contents  # already read above — replaced from: await file.read()
        fname = file.filename or "unknown"
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

        if ext in ("xlsx", "xls"):
            df = pd.read_excel(io.BytesIO(content), engine="openpyxl" if ext == "xlsx" else None)
        elif ext == "csv":
            df = pd.read_csv(io.BytesIO(content))
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Use .xlsx, .xls, or .csv")

        # Strip all string columns
        for col in df.select_dtypes(include=["object"]).columns:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace("nan", pd.NA)

        schema = detect_schema(df)
        profile = build_profile(df, schema)
        quality = audit_data_quality(df, schema)

        # Store in SQLite
        expires_at = (
            datetime.utcnow() + timedelta(hours=SESSION_EXPIRY_HOURS)
        ).isoformat()
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO sessions (filename, row_count, schema_json, profile_json, data_json, loaded_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (
                fname,
                len(df),
                json.dumps(schema),
                json.dumps(profile),
                df.to_json(orient="records"),
                datetime.utcnow().isoformat(),
                expires_at,
            ),
        )
        session_id = cur.lastrowid
        conn.commit()
        conn.close()

        log_audit("UPLOAD", f"File: {fname}, Rows: {len(df)}, Session: {session_id}", request.client.host if request.client else "")

        sample_rows = json.loads(df.head(5).to_json(orient="records"))

        return {
            "session_id": session_id,
            "filename": fname,
            "row_count": len(df),
            "schema": schema,
            "profile": profile,
            "dataQuality": quality,
            "sampleRows": sample_rows,
            "execution_ms": int((time.time() - start) * 1000),
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


class QueryRequest(BaseModel):
    session_id: int
    query: str
    filter_spec: Optional[dict] = None
    model: Optional[str] = None


@app.post("/api/query", dependencies=[Depends(require_access_key)])
@limiter.limit("30/hour")
async def query(request: Request, body: QueryRequest):
    start = time.time()
    try:
        df, schema, profile, session = load_session(body.session_id)

        # ── Session expiry check ────────────────────────────────────────────
        _session_dict = dict(session) if not isinstance(session, dict) else session
        if _session_dict.get("expires_at") and _session_dict["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(
                status_code=410,
                detail=(
                    "Your session has expired. "
                    "Please re-upload your file to continue."
                )
            )
        # ───────────────────────────────────────────────────────────────────

        expanded = expand_abbreviations(body.query, profile)

        # Stage 1: Intent parsing
        if body.filter_spec:
            filter_spec = body.filter_spec
        else:
            if not client:
                return JSONResponse(status_code=503, content={"error": "AI unavailable — ANTHROPIC_API_KEY not set"})

            _check_budget(2000)  # intent parser + narrator

            intent_prompt = build_intent_parser_prompt(profile, schema)
            model_to_use = body.model if body.model else HAIKU

            try:
                intent_response = client.messages.create(
                    model=model_to_use,
                    max_tokens=600,
                    system=intent_prompt,
                    messages=[{"role": "user", "content": expanded}],
                )
                raw_text = intent_response.content[0].text.strip()
                # Clean markdown fences if present
                if raw_text.startswith("```"):
                    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                    raw_text = re.sub(r"\s*```$", "", raw_text)
                filter_spec = json.loads(raw_text)
            except json.JSONDecodeError:
                filter_spec = {
                    "filters": {},
                    "aggregation": {"type": "list", "group_by_field": None},
                    "visualization": {"requested": False},
                    "marketIntelligence": {"triggered": False},
                }
            except Exception as e:
                return JSONResponse(status_code=500, content={"error": f"Intent parsing failed: {str(e)}"})

        # Confidence scoring
        confidence = score_filter_confidence(filter_spec, profile, schema)

        # Stage 2: Deterministic filtering
        filters = filter_spec.get("filters", {})
        filtered = apply_filters(df, filters, schema)
        total = len(filtered)

        # Build chart data
        viz_spec = filter_spec.get("visualization", {})
        chart_data = build_chart_data(filtered, viz_spec, schema, profile) if total > 0 else None

        # Compute stats
        stats = compute_stats(filtered, schema) if total > 0 else {"total": 0}

        # Zero-result handling
        zero_suggestions = []
        if total == 0:
            zero_suggestions = generate_correction_suggestions(filter_spec, profile, schema)
            # Add available values hint
            for field, values in filters.items():
                if values:
                    col = resolve_role_to_column(field, schema)
                    if col:
                        avail = profile.get("uniqueValues", {}).get(col, [])[:8]
                        if avail:
                            stats["available_values"] = {col: avail}

        # Stage 3: Narrative generation
        narrative = ""
        if client:
            model_to_use = body.model if body.model else HAIKU
            try:
                if total == 0:
                    avail_info = ""
                    for field, values in filters.items():
                        if values:
                            col = resolve_role_to_column(field, schema)
                            if col:
                                avail = profile.get("uniqueValues", {}).get(col, [])[:10]
                                avail_info += f"\nAvailable {col} values: {', '.join(str(v) for v in avail)}"

                    narrator_input = f"Query: {body.query}\nExpanded: {expanded}\nFilters applied: {json.dumps(filters)}\nTotal results: 0\nNo records matched the filters.{avail_info}"
                else:
                    narrator_input = f"Query: {body.query}\nExpanded: {expanded}\nFilters applied: {json.dumps(filters)}\nStats: {json.dumps(trim_stats_for_narrator(stats), default=str)}"

                narrator_response = client.messages.create(
                    model=model_to_use,
                    max_tokens=500,
                    system=NARRATOR_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": narrator_input}],
                )
                narrative = narrator_response.content[0].text.strip()
            except Exception as e:
                narrative = f"Narrative generation error: {str(e)}"

        # Prepare row data (max 500)
        rows = json.loads(filtered.head(500).to_json(orient="records"))

        # Log query
        exec_ms = int((time.time() - start) * 1000)
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO queries (session_id, original_query, expanded_query, filter_spec, confidence_score, result_count, status_breakdown, product_breakdown, execution_ms, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    body.session_id,
                    body.query,
                    expanded,
                    json.dumps(filter_spec),
                    confidence["score"],
                    total,
                    json.dumps(stats.get("status_breakdown", {})),
                    json.dumps(stats.get("product_breakdown", {})),
                    exec_ms,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        log_audit("QUERY", f"Query: {body.query}, Results: {total}", request.client.host if request.client else "")

        return {
            "original_query": body.query,
            "expanded_query": expanded,
            "filter_spec": filter_spec,
            "confidence": confidence,
            "rows": rows,
            "total_rows": total,
            "stats": stats,
            "chart_data": chart_data,
            "narrative": narrative,
            "zero_result_suggestions": zero_suggestions,
            "execution_ms": exec_ms,
        }

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Query failed: {str(e)}")


class ResearchRequest(BaseModel):
    session_id: int
    subtype: str
    context: dict
    model: Optional[str] = None


@app.post("/api/research", dependencies=[Depends(require_access_key)])
@limiter.limit("3/hour")
async def research(request: Request, body: ResearchRequest):
    start = time.time()

    cache_key = hashlib.sha256(
        (body.subtype + json.dumps(body.context, sort_keys=True)).encode()
    ).hexdigest()

    # Check cache
    conn = get_db()
    cached = conn.execute("SELECT * FROM research_cache WHERE cache_key=?", (cache_key,)).fetchone()
    conn.close()

    if cached:
        return {
            "subtype": body.subtype,
            "context": body.context,
            "result_text": cached["result_text"],
            "confidence": cached["confidence"],
            "sources": json.loads(cached["sources_json"]) if cached["sources_json"] else [],
            "cached": True,
            "created_at": cached["created_at"],
        }

    if not client:
        raise HTTPException(status_code=503, detail="AI unavailable")

    system_prompt = RESEARCH_PROMPTS.get(body.subtype, RESEARCH_PROMPTS.get("competitive", "Provide a detailed analysis."))

    context_str = "\n".join(f"{k}: {v}" for k, v in body.context.items() if v)
    user_msg = f"Research context:\n{context_str}"

    model_to_use = body.model if body.model else SONNET
    _check_budget(5000)  # research calls consume more tokens
    try:
        result_text = call_with_tools(system_prompt, user_msg, model=model_to_use)
    except Exception as e:
        error_payload = map_api_error_to_user_message(e)
        return JSONResponse(
            status_code=503 if error_payload.get("retry_suggested") else 500,
            content=error_payload,
        )

    confidence_label = "Medium"
    if "[Verified]" in result_text:
        confidence_label = "High"

    # Cache result
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO research_cache (cache_key, subtype, context_json, result_text, confidence, sources_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                cache_key,
                body.subtype,
                json.dumps(body.context),
                result_text,
                confidence_label,
                json.dumps([]),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    log_audit("RESEARCH", f"Subtype: {body.subtype}, Context: {json.dumps(body.context)}", request.client.host if request.client else "")

    return {
        "subtype": body.subtype,
        "context": body.context,
        "result_text": result_text,
        "confidence": confidence_label,
        "sources": [],
        "cached": False,
        "created_at": datetime.utcnow().isoformat(),
        "execution_ms": int((time.time() - start) * 1000),
    }


class ExportRequest(BaseModel):
    session_id: int
    filters: Optional[dict] = None


@app.post("/api/export/excel", dependencies=[Depends(require_access_key)])
async def export_excel(request: Request, body: ExportRequest):
    df, schema, profile, session = load_session(body.session_id)

    if body.filters:
        df = apply_filters(df, body.filters, schema)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Data", index=False)

        stats = compute_stats(df, schema)
        stats_rows = []
        for k, v in stats.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    stats_rows.append({"Metric": f"{k}.{kk}", "Value": str(vv)})
            else:
                stats_rows.append({"Metric": k, "Value": str(v)})
        if stats_rows:
            pd.DataFrame(stats_rows).to_excel(writer, sheet_name="Summary", index=False)

    output.seek(0)
    log_audit("EXPORT", f"Session: {body.session_id}, Rows: {len(df)}", request.client.host if request.client else "")

    fname = session["filename"].rsplit(".", 1)[0] if session["filename"] else "export"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}_export.xlsx"'},
    )


class KPIRequest(BaseModel):
    session_id: int


@app.post("/api/kpi", dependencies=[Depends(require_access_key)])
async def kpi(request: Request, body: KPIRequest):
    df, schema, profile, session = load_session(body.session_id)
    stats = compute_stats(df, schema)

    status_col = schema["roles"].get("STATUS")
    product_col = schema["roles"].get("PRODUCT")
    country_col = schema["roles"].get("COUNTRY")

    total = len(df)
    coverage_rate = 0.0
    if status_col and status_col in df.columns:
        status_vals = df[status_col].value_counts()
        if len(status_vals) > 0:
            top_status = status_vals.index[0]
            coverage_rate = round(status_vals.iloc[0] / total * 100, 1) if total > 0 else 0

    markets = df[country_col].nunique() if country_col and country_col in df.columns else 0
    products = df[product_col].nunique() if product_col and product_col in df.columns else 0

    return {
        "total_records": total,
        "coverage_rate": coverage_rate,
        "markets": markets,
        "products": products,
        "data_quality": audit_data_quality(df, schema)["score"],
        "stats": stats,
    }


class GapRequest(BaseModel):
    session_id: int


@app.post("/api/gap_analysis", dependencies=[Depends(require_access_key)])
async def gap_analysis(request: Request, body: GapRequest):
    df, schema, profile, session = load_session(body.session_id)

    status_col = schema["roles"].get("STATUS")
    period_col = schema["roles"].get("PERIOD")
    product_col = schema["roles"].get("PRODUCT")
    country_col = schema["roles"].get("COUNTRY")

    gaps = []

    if not status_col or not period_col:
        return {"gaps": gaps}

    all_periods = sorted(df[period_col].dropna().unique()) if period_col in df.columns else []
    if len(all_periods) < 2:
        return {"gaps": gaps}

    current_period = all_periods[-1]
    prev_periods = all_periods[-4:] if len(all_periods) >= 4 else all_periods

    status_vals = df[status_col].unique().tolist()
    planned_like = [s for s in status_vals if any(k in str(s).lower() for k in ["plan", "pending", "pipeline", "upcoming"])]

    if not planned_like:
        if len(status_vals) >= 2:
            planned_like = [status_vals[-1]]
        else:
            return {"gaps": gaps}

    planned_df = df[df[status_col].isin(planned_like)]

    for _, row in planned_df.iterrows():
        period_val = str(row.get(period_col, "")) if period_col else ""
        severity = "watch"

        if period_val in prev_periods:
            idx = prev_periods.index(period_val) if period_val in prev_periods else -1
            if idx >= 0:
                quarters_ago = len(prev_periods) - 1 - idx
                if quarters_ago >= 3:
                    severity = "critical"
                elif quarters_ago >= 1:
                    severity = "high"
                else:
                    severity = "medium"

        gap_entry = {"severity": severity}
        if product_col and product_col in row.index:
            gap_entry["product"] = str(row[product_col])
        if country_col and country_col in row.index:
            gap_entry["country"] = str(row[country_col])
        gap_entry["period"] = period_val
        gap_entry["status"] = str(row[status_col])

        gaps.append(gap_entry)

    gaps.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "watch": 3}.get(x["severity"], 4))

    return {"gaps": gaps[:50]}


class LeadershipSummaryRequest(BaseModel):
    module_id: int
    module_name: str
    country: str
    product: str
    indication: str
    intelligence_text: str  # full text output from the module


LEADERSHIP_MODULE_PROMPTS = {
    3: """You are a pharmaceutical market access intelligence analyst.
Extract a structured leadership summary from the competitive landscape intelligence below.
Return ONLY a raw JSON object. No markdown, no code fences, no backticks, no preamble, no explanation. Start your response with { and end with }.
JSON structure:
{
  "key_competitors": "string — approved products in this space, comma separated",
  "their_access_status": "string — Reimbursed / Restricted / Mixed, with brief context",
  "biosimilar_threat": "string — entry timeline if applicable, or 'Not applicable'",
  "our_position": "string — Differentiated / At risk / Unclear, with one-line reason",
  "access_gap": "string — where BMS leads or trails vs competitors",
  "leadership_signal": "string — ONE sentence, action language, so-what only. No hedging."
}""",
    4: """You are a pharmaceutical market access intelligence analyst.
Extract a structured leadership summary from the timeline intelligence below.
Return ONLY a raw JSON object. No markdown, no code fences, no backticks, no preamble, no explanation. Start your response with { and end with }.
JSON structure:
{
  "current_milestone": "string — where in the reimbursement journey today",
  "next_milestone": "string — what triggers the next step",
  "estimated_timeline": "string — months to reimbursement decision",
  "regional_benchmark": "string — vs CEE / LATAM / MEA average if available, else 'Benchmark unavailable'",
  "delay_risk": "string — known blockers or 'No blockers identified'",
  "leadership_signal": "string — ONE sentence. On track / at risk / action needed. No hedging."
}""",
    5: """You are a pharmaceutical market access intelligence analyst.
Extract a structured leadership summary from the HTA and public sector intelligence below.
Return ONLY a raw JSON object. No markdown, no code fences, no backticks, no preamble, no explanation. Start your response with { and end with }.
JSON structure:
{
  "hta_body": "string — name and decision framework of the relevant HTA body",
  "evidence_standard": "string — Clinical / CEA / BIA / MCDA or combination",
  "current_status": "string — stage in HTA journey",
  "expected_timeline": "string — months to HTA decision",
  "risk_rating": "string — Low / Medium / High / Blocked with one-line reason",
  "leadership_signal": "string — ONE sentence. What this means for access strategy. No hedging."
}""",
    8: """You are a pharmaceutical market access intelligence analyst.
Extract a structured leadership summary from the IRP risk intelligence below.
Return ONLY a raw JSON object. No markdown, no code fences, no backticks, no preamble, no explanation. Start your response with { and end with }.
JSON structure:
{
  "reference_basket": "string — countries that reference this market for IRP",
  "price_position": "string — Above / At / Below basket with brief context",
  "cascade_exposure": "string — markets at risk if price is adjusted",
  "financial_risk": "string — estimated exposure band or 'Insufficient data to estimate'",
  "risk_level": "string — Low / Medium / High / Critical with one-line reason",
  "leadership_signal": "string — ONE sentence. Recommended action or hold. No hedging."
}"""
}


class CompareMarketsRequest(BaseModel):
    module_id:    int
    module_name:  str
    product:      str
    indication:   str
    country_a:    str
    country_b:    str
    intel_text_a: str
    intel_text_b: str  # empty string is valid — Sonnet uses training knowledge
    summary_a_cached: Optional[dict] = None


@app.post("/api/leadership-summary", dependencies=[Depends(require_access_key)])
@limiter.limit("5/hour")
def leadership_summary(request: Request, body: LeadershipSummaryRequest):
    SUPPORTED_MODULES = {3, 4, 5, 8}
    if body.module_id not in SUPPORTED_MODULES:
        raise HTTPException(status_code=400, detail="Module not supported for leadership summary.")
    system_prompt = LEADERSHIP_MODULE_PROMPTS[body.module_id]
    user_message = f"""Country: {body.country}
Product: {body.product}
Indication: {body.indication}
Intelligence Output:
{body.intelligence_text[:4000]}"""
    if not client:
        return {"success": False, "error": "AI unavailable.", "module_id": body.module_id}
    try:
        response = call_anthropic_with_retry(
            client.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences Sonnet sometimes wraps around JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
        summary_data = json.loads(raw)
        return {"success": True, "summary": summary_data, "module_id": body.module_id}
    except (json.JSONDecodeError, ValueError):
        return {"success": False, "error": "Summary parsing failed.", "module_id": body.module_id}
    except Exception as e:
        return {"success": False, "error": str(e), "module_id": body.module_id}


@app.post("/api/compare-markets",
          dependencies=[Depends(require_access_key)])
@limiter.limit("3/hour")
async def compare_markets(request: Request,
                          body: CompareMarketsRequest):
    SUPPORTED_MODULES = {3, 4, 5, 8}
    if body.module_id not in SUPPORTED_MODULES:
        raise HTTPException(status_code=400,
            detail="Module not supported for comparison.")

    def summarise(country: str, intel_text: str) -> dict:
        """
        Synchronous summary call — safe for asyncio.to_thread.
        If intel_text is empty, Sonnet uses training knowledge.
        System prompt instructs it to label estimates as [Benchmark].
        """
        base_prompt = LEADERSHIP_MODULE_PROMPTS.get(body.module_id)
        if not base_prompt:
            raise ValueError(
                f"No prompt defined for module_id {body.module_id}")
        if intel_text.strip():
            context_note = (
                "Use the intelligence output below as your primary source."
            )
        else:
            context_note = (
                "No local intelligence data is available for this market. "
                "Use your training knowledge. Label all estimates as "
                "[Benchmark] in the JSON values."
            )
        system_prompt = base_prompt + (
            "\n\nIMPORTANT: " + context_note
        )
        user_msg = (
            f"Country: {country}\n"
            f"Product: {body.product}\n"
            f"Indication: {body.indication}\n\n"
            f"Intelligence Output:\n"
            f"{intel_text[:4000] if intel_text.strip() else 'Not available.'}"
        )
        response = call_anthropic_with_retry(
            client.messages.create,
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
        return json.loads(raw)

    import asyncio
    try:
        if body.summary_a_cached:
            summary_a = body.summary_a_cached
            summary_b = await asyncio.to_thread(
                summarise, body.country_b, body.intel_text_b)
        else:
            summary_a, summary_b = await asyncio.gather(
                asyncio.to_thread(summarise, body.country_a, body.intel_text_a),
                asyncio.to_thread(summarise, body.country_b, body.intel_text_b),
            )
        return {
            "success":   True,
            "module_id": body.module_id,
            "country_a": body.country_a,
            "country_b": body.country_b,
            "summary_a": summary_a,
            "summary_b": summary_b,
        }
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Comparison JSON parse failed: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Comparison failed: {str(e)}"
        )


@app.get("/api/gap-analysis", dependencies=[Depends(require_access_key)])
@limiter.limit("20/hour")
async def gap_analysis(request: Request, session_id: int = Query(...)):
    df, schema, profile, session = load_session(session_id)
    # Require Status column
    if "Status" not in df.columns or "Country" not in df.columns:
        raise HTTPException(status_code=400,
            detail="Dataset missing required columns: Country, Status")
    results = []
    for country, grp in df.groupby("Country"):
        total       = len(grp)
        reimbursed  = (grp["Status"] == "Reimbursed").sum()
        planned     = (grp["Status"] == "Planned").sum()
        gap_pct     = round((planned / total) * 100, 1) if total > 0 else 0
        # Indication count in gap
        gap_rows = grp[grp["Status"] == "Planned"]
        gap_indications = int(
            gap_rows["Indication Details"].nunique()
            if "Indication Details" in gap_rows.columns else 0
        )
        # Earliest Planned period — years in gap
        years_in_gap = 0
        if "Period" in gap_rows.columns and len(gap_rows) > 0:
            try:
                earliest = gap_rows["Period"].dropna().min()
                # Period format YYYY-QN
                year_str = str(earliest)[:4]
                years_in_gap = max(0, 2026 - int(year_str))
            except Exception:
                years_in_gap = 0
        # Composite score 0–100
        # Weight: gap_pct (50%) + indication breadth (30%) + years (20%)
        indication_score = min(gap_indications / 5 * 100, 100)
        year_score       = min(years_in_gap / 5 * 100, 100)
        composite = round(
            (gap_pct * 0.5) + (indication_score * 0.3) + (year_score * 0.2), 1
        )
        # Tier classification
        if composite >= 70:
            tier = "Critical"
        elif composite >= 45:
            tier = "Defend"
        elif composite >= 20:
            tier = "Watch"
        else:
            tier = "Monitor"
        # Region
        region = grp["Region"].iloc[0] if "Region" in grp.columns else "Unknown"
        # Action flag
        if tier == "Critical":
            action = "Immediate engagement — all indications in access gap"
        elif tier == "Defend":
            action = "Targeted submission required — partial access at risk"
        elif tier == "Watch":
            action = "Monitor — reimbursement trajectory uncertain"
        else:
            action = "Stable — maintain current access strategy"
        results.append({
            "country":         country,
            "region":          region,
            "score":           composite,
            "tier":            tier,
            "gap_pct":         gap_pct,
            "reimbursed":      int(reimbursed),
            "planned":         int(planned),
            "gap_indications": gap_indications,
            "years_in_gap":    years_in_gap,
            "action":          action
        })
    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return {
        "success":  True,
        "markets":  results,
        "total":    len(results),
        "critical": sum(1 for r in results if r["tier"] == "Critical"),
        "defend":   sum(1 for r in results if r["tier"] == "Defend"),
        "watch":    sum(1 for r in results if r["tier"] == "Watch"),
        "monitor":  sum(1 for r in results if r["tier"] == "Monitor"),
    }


@app.get("/api/indications",
         dependencies=[Depends(require_access_key)])
@limiter.limit("10/hour")
async def get_indications(
    request: Request,
    product: str = Query(...),
    session_id: str = Query(...)
):
    # ── Layer 1: Dataset extraction ──────────────────────
    dataset_indications = []
    try:
        df, schema, profile, session = load_session(int(session_id))
        df.columns = [c.strip() for c in df.columns]
        col_map = {c.lower(): c for c in df.columns}
        product_col = col_map.get("product")
        ind_col     = col_map.get("indication details")
        if product_col and ind_col:
            mask = df[product_col].str.strip().str.lower() \
                   == product.strip().lower()
            raw_inds = df.loc[mask, ind_col].dropna().unique()
            dataset_indications = [
                str(i).strip() for i in raw_inds
                if str(i).strip()
            ]
    except Exception:
        # Session not found or column mismatch — non-fatal
        dataset_indications = []

    # ── Layer 2: AI enrichment (Haiku + web_search) ──────
    ai_indications = []
    try:
        system_prompt = (
            "You are a pharmaceutical regulatory specialist. "
            "Return ONLY a valid JSON array of strings. "
            "No markdown, no code fences, no preamble. "
            "Start with [ and end with ]. "
            "Each string is one approved indication name, "
            "short form preferred (e.g. 'NSCLC', 'RCC', "
            "'Melanoma', 'HCC'). Maximum 15 items."
        )
        user_msg = (
            f"List all currently FDA-approved and EMA-approved "
            f"indications for {product}. Use web search to verify "
            f"current label. Include all markets globally. "
            f"Return only the indication names as a JSON array. "
            f"No markdown. No explanation."
        )
        response = call_anthropic_with_retry(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=system_prompt,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 2
            }],
            messages=[{"role": "user", "content": user_msg}]
        )
        # Extract text from response — may contain preamble + tool_use blocks
        # before the final JSON text block; use the LAST non-empty text block
        raw_text = ""
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                candidate = block.text.strip()
                if candidate:
                    raw_text = candidate  # keep updating — last non-empty wins
        if raw_text:
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[-1]
            if raw_text.endswith("```"):
                raw_text = raw_text.rsplit("```", 1)[0]
            raw_text = raw_text.strip()
        # Extract embedded JSON array if Haiku prepended prose preamble
        arr_match = re.search(r'\[.*\]', raw_text, re.DOTALL)
        if arr_match:
            raw_text = arr_match.group()
        if raw_text:  # re-check after fence stripping
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                ai_indications = [
                    str(i).strip() for i in parsed
                    if str(i).strip()
                ]
    except Exception as e:
        # AI enrichment failure is non-fatal —
        # dataset indications still returned
        ai_indications = []

    # ── Layer 3: Merge and deduplicate ───────────────────
    seen    = set()
    results = []
    for ind in dataset_indications:
        key = ind.lower().strip()
        if key not in seen:
            seen.add(key)
            results.append({"label": ind, "source": "Dataset"})
    for ind in ai_indications:
        key = ind.lower().strip()
        if key not in seen:
            seen.add(key)
            results.append({"label": ind, "source": "FDA/EMA"})
    return {
        "success":      True,
        "product":      product,
        "indications":  results,
        "total":        len(results),
        "from_dataset": len(dataset_indications),
        "from_ai":      len(ai_indications)
    }


class CoverageRequest(BaseModel):
    product:         str
    country:         str
    session_id:      int
    fda_indications: list[str]  # from frontend cache


@app.post("/api/indication-coverage",
          dependencies=[Depends(require_access_key)])
@limiter.limit("15/hour")
async def indication_coverage(request: Request,
                               body: CoverageRequest):
    try:
        # ── Layer 1: Dataset cross-reference ─────────────
        dataset_map = {}
        try:
            df, schema, profile, session = load_session(body.session_id)
            df.columns = [c.strip() for c in df.columns]
            col_map = {c.lower(): c for c in df.columns}
            product_col = col_map.get("product")
            country_col = col_map.get("country")
            ind_col     = col_map.get("indication details")
            status_col  = col_map.get("status")
            if all([product_col, country_col, ind_col, status_col]):
                mask = (
                    (df[product_col].str.strip().str.lower()
                     == body.product.strip().lower()) &
                    (df[country_col].str.strip().str.lower()
                     == body.country.strip().lower())
                )
                filtered = df[mask]
                for ind, grp in filtered.groupby(ind_col):
                    ind_str = str(ind).strip()
                    if not ind_str:
                        continue
                    statuses = grp[status_col].dropna() \
                                              .str.strip() \
                                              .unique() \
                                              .tolist()
                    if len(statuses) == 0:
                        continue
                    elif "Reimbursed" in statuses and "Planned" in statuses:
                        dataset_map[ind_str] = "Mixed"
                    elif "Reimbursed" in statuses:
                        dataset_map[ind_str] = "Reimbursed"
                    else:
                        dataset_map[ind_str] = "Planned"
        except Exception:
            dataset_map = {}

        # ── Layer 2: Haiku normalisation ──────────────────
        matches = []
        if body.fda_indications and dataset_map:
            try:
                fda_list = body.fda_indications[:15]
                ds_list  = list(dataset_map.keys())[:20]
                system_prompt = (
                    "You are a pharmaceutical nomenclature "
                    "specialist. Match indication names across "
                    "two lists. Same indication = same disease "
                    "regardless of abbreviation or phrasing. "
                    "Return ONLY a valid JSON array. "
                    "No markdown, no code fences, no preamble. "
                    "Start with [ and end with ]. "
                    "Each element: "
                    "{\"fda\": \"<fda name>\", "
                    "\"match\": \"<dataset name or null>\"}"
                )
                user_msg = (
                    f"FDA/EMA list: {json.dumps(fda_list)}\n"
                    f"Dataset list: {json.dumps(ds_list)}\n\n"
                    "For each FDA indication, find the best "
                    "matching dataset indication. If no match "
                    "exists, set match to null. "
                    "Each FDA indication appears exactly once."
                )
                response = call_anthropic_with_retry(
                    client.messages.create,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_msg}]
                )
                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1]
                if raw.endswith("```"):
                    raw = raw.rsplit("```", 1)[0]
                raw = raw.strip()
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    matches = parsed
            except Exception:
                matches = []

        # ── Fallback: substring matching ──────────────────
        if not matches and body.fda_indications:
            for fda_ind in body.fda_indications[:15]:
                best_match = None
                fda_lower  = fda_ind.lower().strip()
                for ds_ind in dataset_map.keys():
                    ds_lower = ds_ind.lower().strip()
                    if (fda_lower in ds_lower
                            or ds_lower in fda_lower
                            or fda_lower[:4] == ds_lower[:4]):
                        best_match = ds_ind
                        break
                matches.append({"fda": fda_ind, "match": best_match})

        # ── Layer 3: Build coverage matrix ────────────────
        matrix     = []
        reimbursed = 0
        planned    = 0
        mixed      = 0
        gap        = 0
        for item in matches:
            fda_name  = item.get("fda", "")
            ds_match  = item.get("match")
            ds_status = dataset_map.get(ds_match) if ds_match else None
            if ds_status == "Reimbursed":
                status_label = "Reimbursed"
                reimbursed  += 1
            elif ds_status == "Planned":
                status_label = "Planned"
                planned     += 1
            elif ds_status == "Mixed":
                status_label = "Mixed"
                mixed       += 1
            else:
                status_label = "Gap"
                gap         += 1
            matrix.append({
                "fda_indication": fda_name,
                "dataset_match":  ds_match,
                "country_status": status_label,
            })
        total        = len(matrix)
        covered      = reimbursed + mixed
        coverage_pct = round(
            (covered / total * 100) if total > 0 else 0, 1
        )
        return {
            "success":       True,
            "product":       body.product,
            "country":       body.country,
            "matrix":        matrix,
            "total":         total,
            "reimbursed":    reimbursed,
            "planned":       planned,
            "mixed":         mixed,
            "gap":           gap,
            "coverage_pct":  coverage_pct,
            "normalisation": "haiku" if matches and
                             body.fda_indications and
                             dataset_map else "fallback"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Coverage analysis failed: {str(e)}"
        )


class AnnotateRequest(BaseModel):
    session_id: int
    row_key: str
    note_text: str
    author: Optional[str] = "User"


class FeedbackRequest(BaseModel):
    session_id:  Optional[int] = None
    query_text:  Optional[str] = None
    issue_type:  str             # "wrong_result"|"chart_broken"|"wrong_count"|"slow"|"suggestion"|"other"
    detail:      Optional[str] = None


@app.post("/api/annotate", dependencies=[Depends(require_access_key)])
async def annotate(request: Request, body: AnnotateRequest):
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM annotations WHERE session_id=? AND row_key=?", (body.session_id, body.row_key)
    ).fetchone()

    now = datetime.utcnow().isoformat()
    if existing:
        conn.execute(
            "UPDATE annotations SET note_text=?, author=?, updated_at=? WHERE id=?",
            (body.note_text, body.author, now, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO annotations (session_id, row_key, note_text, author, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (body.session_id, body.row_key, body.note_text, body.author, now, now),
        )
    conn.commit()
    conn.close()

    log_audit("ANNOTATE", f"Session: {body.session_id}, Row: {body.row_key}", request.client.host if request.client else "")
    return {"status": "ok"}


@app.get("/api/annotations/{session_id}", dependencies=[Depends(require_access_key)])
async def get_annotations(session_id: int):
    conn = get_db()
    rows = conn.execute("SELECT * FROM annotations WHERE session_id=?", (session_id,)).fetchall()
    conn.close()
    return {"annotations": {r["row_key"]: {"note": r["note_text"], "author": r["author"], "updated_at": r["updated_at"]} for r in rows}}


@app.get("/api/history/{session_id}", dependencies=[Depends(require_access_key)])
async def get_history(session_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM queries WHERE session_id=? ORDER BY id DESC LIMIT 50", (session_id,)
    ).fetchall()
    conn.close()
    return {
        "history": [
            {
                "id": r["id"],
                "query": r["original_query"],
                "expanded": r["expanded_query"],
                "results": r["result_count"],
                "confidence": r["confidence_score"],
                "timestamp": r["timestamp"],
                "execution_ms": r["execution_ms"],
            }
            for r in rows
        ]
    }


@app.get("/api/sessions", dependencies=[Depends(require_access_key)])
async def get_sessions():
    conn = get_db()
    rows = conn.execute("SELECT id, filename, row_count, loaded_at FROM sessions ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/audit", dependencies=[Depends(require_access_key)])
async def get_audit():
    conn = get_db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return {"log": [dict(r) for r in rows]}


@app.post("/api/feedback", dependencies=[Depends(require_access_key)])
async def submit_feedback(
    body: FeedbackRequest,
    request: Request
):
    """
    Accepts structured feedback from users.
    Stored in feedback_log table and audit_log.
    """
    conn      = get_db()
    timestamp = datetime.utcnow().isoformat()
    ip        = request.client.host if request.client else "unknown"
    ua        = request.headers.get("user-agent", "")[:200]

    # Validate issue_type
    VALID_TYPES = {
        "wrong_result", "chart_broken", "wrong_count",
        "slow", "suggestion", "other"
    }
    if body.issue_type not in VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid issue_type '{body.issue_type}'. "
                f"Valid values: {', '.join(sorted(VALID_TYPES))}"
            )
        )

    # Insert into feedback_log
    conn.execute(
        """INSERT INTO feedback_log
           (session_id, query_text, issue_type,
            detail, ip_address, user_agent, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            body.session_id,
            body.query_text,
            body.issue_type,
            body.detail,
            ip,
            ua,
            timestamp,
        ]
    )

    # Also write to audit_log for unified log view
    conn.execute(
        """INSERT INTO audit_log
           (action, details, ip_address, timestamp)
           VALUES (?, ?, ?, ?)""",
        [
            "FEEDBACK",
            json.dumps({
                "issue_type": body.issue_type,
                "session_id": body.session_id,
                "query_text": body.query_text,
                "detail":     body.detail,
            }),
            ip,
            timestamp,
        ]
    )

    conn.commit()

    return {
        "status":  "received",
        "message": "Thank you for your feedback.",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False        # reload=False in production
    )