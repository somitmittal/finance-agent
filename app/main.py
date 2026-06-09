from __future__ import annotations

import csv
import datetime as dt
import email.utils
import hashlib
import html
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlparse

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None

try:
    from nsepython import nsefetch
except Exception:  # pragma: no cover - optional dependency
    nsefetch = None


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "portfolio.db"

NSE_HOME = "https://www.nseindia.com"
BSE_HOME = "https://www.bseindia.com"
INDIAN_API_HOME = "https://stock.indianapi.in"
GEMINI_HOME = "https://generativelanguage.googleapis.com/v1beta"
NSE_EQUITY_MASTER_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STOCK_MASTER_CACHE: Dict[str, Any] = {"loaded_at": None, "items": []}
STOCK_SEARCH_FALLBACK = [
    {"symbol": "RELIANCE", "name": "Reliance Industries", "series": "EQ", "bse_code": "500325"},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "series": "EQ", "bse_code": "532540"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "series": "EQ", "bse_code": "500180"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "series": "EQ", "bse_code": "532174"},
    {"symbol": "INFY", "name": "Infosys", "series": "EQ", "bse_code": "500209"},
    {"symbol": "SBIN", "name": "State Bank of India", "series": "EQ", "bse_code": "500112"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "series": "EQ", "bse_code": "532454"},
    {"symbol": "ITC", "name": "ITC", "series": "EQ", "bse_code": "500875"},
]

app = FastAPI(title="Finance Agent", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def load_local_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()


class PortfolioIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    company_name: str = Field("", max_length=180)
    bse_code: str = Field("", max_length=16)
    quantity: float = Field(0, ge=0)
    avg_price: float = Field(0, ge=0)
    thesis: str = Field("", max_length=1000)


class PortfolioOut(PortfolioIn):
    id: int
    created_at: str
    updated_at: str


class AuthIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=180)
    password: str = Field(..., min_length=6, max_length=200)


class StockChatIn(BaseModel):
    question: str = Field(..., min_length=3, max_length=1200)
    bse_code: str = Field("", max_length=16)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_symbol(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9&.-]", "", symbol).upper().strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Enter a valid stock symbol.")
    return cleaned


def normalize_bse_code(bse_code: str = "") -> str:
    cleaned = str(bse_code or "").strip()
    if cleaned and not re.fullmatch(r"\d{4,10}", cleaned):
        raise HTTPException(status_code=400, detail="BSE code must contain only digits.")
    return cleaned


def stock_master_cache_valid() -> bool:
    loaded_at = STOCK_MASTER_CACHE.get("loaded_at")
    if not loaded_at or not STOCK_MASTER_CACHE.get("items"):
        return False
    return (dt.datetime.now(dt.timezone.utc) - loaded_at) < dt.timedelta(hours=24)


def fetch_nse_stock_master() -> List[Dict[str, Any]]:
    if stock_master_cache_valid():
        return STOCK_MASTER_CACHE["items"]

    response = http_session().get(NSE_EQUITY_MASTER_URL, timeout=12)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    items = []
    for row in reader:
        symbol = clean_text(row.get("SYMBOL", "")).upper()
        name = clean_text(row.get("NAME OF COMPANY", ""))
        series = clean_text(row.get(" SERIES", "") or row.get("SERIES", "")).upper()
        if symbol and name:
            items.append({"symbol": symbol, "name": name, "series": series, "bse_code": ""})

    if not items:
        raise RuntimeError("NSE stock master returned no symbols.")

    STOCK_MASTER_CACHE["loaded_at"] = dt.datetime.now(dt.timezone.utc)
    STOCK_MASTER_CACHE["items"] = items
    return items


def stock_search_score(query: str, stock: Dict[str, Any]) -> int:
    normalized = query.strip().lower()
    symbol = str(stock.get("symbol", "")).lower()
    name = str(stock.get("name", "")).lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
    score = 0

    if normalized == symbol or normalized == name:
        score += 120
    if symbol.startswith(normalized):
        score += 80
    if name.startswith(normalized):
        score += 70
    if normalized in symbol:
        score += 45
    if normalized in name:
        score += 40
    if tokens and all(token in f"{symbol} {name}" for token in tokens):
        score += 25 + (5 * len(tokens))
    if score and stock.get("series") == "EQ":
        score += 5
    return score


def search_stock_master(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        items = fetch_nse_stock_master()
    except Exception:
        items = STOCK_SEARCH_FALLBACK

    scored = []
    for stock in items:
        score = stock_search_score(query, stock)
        if score > 0:
            scored.append({**stock, "score": score})
    scored.sort(key=lambda stock: (-stock["score"], stock.get("name", "")))
    return [{key: stock.get(key, "") for key in ("symbol", "name", "series", "bse_code")} for stock in scored[:limit]]


def warm_stock_master_cache() -> None:
    try:
        fetch_nse_stock_master()
    except Exception:
        pass


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    create_or_migrate_portfolio(conn)
    conn.commit()


def create_or_migrate_portfolio(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'portfolio'").fetchone()
    if not existing:
        create_portfolio_table(conn)
        return

    columns = [row["name"] for row in conn.execute("PRAGMA table_info(portfolio)").fetchall()]
    indexes = conn.execute("PRAGMA index_list(portfolio)").fetchall()
    has_global_symbol_unique = any(
        row["unique"] and "symbol" in [info["name"] for info in conn.execute(f"PRAGMA index_info({row['name']})").fetchall()]
        and len(conn.execute(f"PRAGMA index_info({row['name']})").fetchall()) == 1
        for row in indexes
    )
    if "user_id" in columns and not has_global_symbol_unique:
        return

    legacy_user_id = ensure_legacy_user(conn)
    rows = conn.execute("SELECT * FROM portfolio").fetchall()
    conn.execute("ALTER TABLE portfolio RENAME TO portfolio_legacy")
    create_portfolio_table(conn)
    for row in rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO portfolio
                (user_id, symbol, company_name, bse_code, quantity, avg_price, thesis, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_user_id,
                row["symbol"],
                row["company_name"],
                row["bse_code"],
                row["quantity"],
                row["avg_price"],
                row["thesis"],
                row["created_at"],
                row["updated_at"],
            ),
        )
    conn.execute("DROP TABLE portfolio_legacy")


def create_portfolio_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            company_name TEXT DEFAULT '',
            bse_code TEXT DEFAULT '',
            quantity REAL DEFAULT 0,
            avg_price REAL DEFAULT 0,
            thesis TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, symbol),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )


def ensure_legacy_user(conn: sqlite3.Connection) -> int:
    email = "legacy@local"
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return int(row["id"])
    salt = secrets.token_hex(16)
    password_hash = hash_password(secrets.token_urlsafe(32), salt)
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
        (email, password_hash, salt, now_iso()),
    )
    return int(cursor.lastrowid)


def row_to_portfolio(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if "@" not in cleaned or "." not in cleaned.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    return cleaned


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, now_iso()),
    )
    return token


def public_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}


def auth_payload(user: sqlite3.Row, token: str) -> Dict[str, Any]:
    return {"token": token, "user": public_user(user)}


def bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Login required.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid auth token.")
    return token.strip()


def current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    token = bearer_token(authorization)
    conn = db()
    row = conn.execute(
        """
        SELECT users.*
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Login required.")
    return public_user(row)


def optional_current_user(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    token = bearer_token(authorization)
    conn = db()
    row = conn.execute(
        """
        SELECT users.*
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired auth token.")
    return public_user(row)


def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    return session


def fetch_json(session: requests.Session, url: str, *, referer: str, timeout: int = 8) -> Any:
    headers = {"Referer": referer}
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def indian_api_key() -> str:
    return os.getenv("INDIAN_API_KEY", "").strip()


def fetch_indian_api(path: str, params: Dict[str, Any]) -> Any:
    key = indian_api_key()
    if not key:
        raise RuntimeError("INDIAN_API_KEY is not configured.")
    session = http_session()
    response = session.get(
        f"{INDIAN_API_HOME}{path}",
        params=params,
        headers={"x-api-key": key},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def nested_get(data: Dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def list_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "announcements", "recent_announcements", "items", "result", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = list_from_payload(value)
            if nested:
                return nested
    return []


def value_by_key_fragment(data: Any, fragments: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(fragment in normalized for fragment in fragments):
                return value
        for value in data.values():
            found = value_by_key_fragment(value, fragments)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(data, list):
        for item in data:
            found = value_by_key_fragment(item, fragments)
            if found not in (None, "", [], {}):
                return found
    return None


def extract_shareholding(data: Dict[str, Any]) -> Dict[str, Any]:
    raw = (
        value_by_key_fragment(data, ("shareholding", "shareholdingpattern", "shareholder"))
        or data.get("shareholding")
        or data.get("shareHoldingPattern")
        or {}
    )
    text = json.dumps(raw, default=str).lower() if raw else ""
    categories = {
        "promoter": ("promoter", "promoters"),
        "fii": ("fii", "foreigninstitution", "foreignportfolio", "fpi"),
        "dii": ("dii", "domesticinstitution", "mutualfund", "insurance"),
        "public": ("public", "retail", "others"),
    }
    parsed = {}
    for label, fragments in categories.items():
        value = value_by_key_fragment(raw, fragments) if raw else None
        if isinstance(value, dict):
            percent = value_by_key_fragment(value, ("percent", "holding", "share"))
        else:
            percent = value
        parsed[label] = parse_float(percent)

    signals = []
    if parsed.get("promoter") is not None:
        if parsed["promoter"] >= 50:
            signals.append("Promoter holding is above 50%, indicating owner alignment if pledge is not elevated.")
        elif parsed["promoter"] < 30:
            signals.append("Promoter holding is below 30%; check institutional ownership and governance history.")
    if parsed.get("fii") is not None and parsed["fii"] > 10:
        signals.append("FII ownership is meaningful, suggesting foreign institutional interest.")
    if parsed.get("dii") is not None and parsed["dii"] > 10:
        signals.append("DII ownership is meaningful, suggesting domestic institutional interest.")
    if "pledge" in text:
        signals.append("Pledge-related wording appears in shareholding data; verify pledged promoter shares.")

    available = any(value is not None for value in parsed.values())
    return {
        "available": available,
        "promoter_percent": parsed.get("promoter"),
        "fii_percent": parsed.get("fii"),
        "dii_percent": parsed.get("dii"),
        "public_percent": parsed.get("public"),
        "signals": signals,
        "note": "Shareholding is parsed from provider payload when available; verify with latest exchange shareholding pattern filings.",
    }


def extract_valuation_metrics(data: Dict[str, Any], last_price: Optional[float] = None) -> Dict[str, Any]:
    metrics = {
        "market_cap_crore": metric_value(data, ("marketcap", "mcap", "marketcapitalization")),
        "pe_ratio": metric_value(data, ("peratio", "trailingpe", "priceearnings", "priceearningratio")),
        "pb_ratio": metric_value(data, ("pbratio", "pb", "pricebook")),
        "eps": metric_value(data, ("eps", "earningpershare")),
        "book_value": metric_value(data, ("bookvalue", "bookvalueshare")),
        "roe_percent": metric_value(data, ("roe", "returnonequity")),
        "roce_percent": metric_value(data, ("roce", "returnoncapital")),
        "debt_to_equity": metric_value(data, ("debttoequity", "debtequity")),
        "dividend_yield_percent": metric_value(data, ("dividendyield",)),
        "revenue_growth_percent": metric_value(data, ("revenuegrowth", "salesgrowth")),
        "profit_growth_percent": metric_value(data, ("profitgrowth", "patgrowth", "netprofitgrowth")),
    }
    if metrics["pe_ratio"] is None and last_price and metrics["eps"]:
        metrics["pe_ratio"] = round(float(last_price) / metrics["eps"], 2) if metrics["eps"] else None
    if metrics["pb_ratio"] is None and last_price and metrics["book_value"]:
        metrics["pb_ratio"] = round(float(last_price) / metrics["book_value"], 2) if metrics["book_value"] else None
    available = {key: value for key, value in metrics.items() if value is not None}
    missing = [key for key, value in metrics.items() if value is None]
    return {
        "available": bool(available),
        "metrics": available,
        "missing": missing,
        "note": "Valuation metrics are parsed from provider payloads when available; verify against latest financial statements and peers.",
    }


def fetch_indian_api_stock(symbol: str) -> Dict[str, Any]:
    data = fetch_indian_api("/stock", {"name": symbol})
    if not isinstance(data, dict):
        return {"symbol": symbol, "provider": "IndianAPI"}

    current_price = data.get("currentPrice") or data.get("current_price") or {}
    price_value = current_price.get("NSE") if isinstance(current_price, dict) else current_price
    if isinstance(price_value, dict):
        last_price = price_value.get("price") or price_value.get("currentPrice") or price_value.get("lastPrice")
        change = price_value.get("priceChange") or price_value.get("change")
        percent_change = price_value.get("percentageChange") or price_value.get("percentChange") or price_value.get("pChange")
    else:
        last_price = price_value or data.get("price") or data.get("lastPrice")
        change = data.get("priceChange") or data.get("change")
        percent_change = data.get("percentageChange") or data.get("percentChange") or data.get("pChange")

    return {
        "symbol": data.get("tickerId") or data.get("ticker") or symbol,
        "company": data.get("companyName") or data.get("company_name") or symbol,
        "last_price": parse_float(last_price),
        "change": parse_float(change),
        "percent_change": parse_float(percent_change),
        "year_high": parse_float(data.get("yearHigh")),
        "year_low": parse_float(data.get("yearLow")),
        "industry": data.get("industry"),
        "bse_code": nested_get(data, "companyProfile", "exchangeCodeBse"),
        "nse_code": nested_get(data, "companyProfile", "exchangeCodeNse"),
        "shareholding": extract_shareholding(data),
        "valuation": extract_valuation_metrics(data, parse_float(last_price)),
        "provider": "IndianAPI",
    }


def fetch_indian_api_announcements(symbol: str) -> List[Dict[str, Any]]:
    payload = fetch_indian_api("/recent_announcements", {"stock_name": symbol})
    records = list_from_payload(payload)
    items: List[Dict[str, Any]] = []
    for record in records[:20]:
        subject = (
            record.get("title")
            or record.get("headline")
            or record.get("subject")
            or record.get("announcement")
            or record.get("description")
            or "Recent announcement"
        )
        details = record.get("description") or record.get("details") or record.get("summary") or record.get("content") or subject
        date_value = (
            record.get("date")
            or record.get("announced_at")
            or record.get("announcementDate")
            or record.get("publishedAt")
            or record.get("timestamp")
            or ""
        )
        attachment = record.get("attachment") or record.get("url") or record.get("link") or record.get("pdf") or ""
        items.append(
            {
                "source": "IndianAPI",
                "symbol": symbol,
                "company": clean_text(record.get("company") or record.get("companyName") or symbol),
                "subject": clean_text(subject),
                "category": clean_text(record.get("category") or record.get("type") or ""),
                "details": clean_text(details),
                "date": str(date_value),
                "sort_date": str(date_value),
                "attachment": str(attachment),
                "raw": record,
            }
        )
    return items


def domain_from_url(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_trusted_news_source(url: str, source: str = "") -> bool:
    domain = domain_from_url(url)
    source_l = source.lower()
    return any(domain.endswith(allowed) or allowed in source_l for allowed in TRUSTED_NEWS_DOMAINS)


def looks_speculative(title: str, summary: str = "") -> bool:
    text = f"{title} {summary}".lower()
    return any(term in text for term in SPECULATIVE_TERMS)


def parse_news_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        return parsed.isoformat()
    except Exception:
        return text


def normalize_news_item(
    *,
    title: Any,
    summary: Any = "",
    url: Any = "",
    source: Any = "",
    published_at: Any = "",
    provider: str,
) -> Optional[Dict[str, Any]]:
    clean_title = clean_text(html.unescape(str(title or "")))
    clean_summary = clean_text(html.unescape(str(summary or "")))
    clean_url = str(url or "")
    clean_source = clean_text(source or domain_from_url(clean_url) or provider)
    if not clean_title or looks_speculative(clean_title, clean_summary):
        return None
    trusted = is_trusted_news_source(clean_url, clean_source) or provider in {"Official", "IndianAPI"}
    if not trusted:
        return None
    return {
        "title": clean_title,
        "summary": clean_summary,
        "url": clean_url,
        "source": clean_source,
        "domain": domain_from_url(clean_url),
        "published_at": parse_news_date(published_at),
        "provider": provider,
        "verified": True,
    }


def extract_indian_api_news(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        records = []
        for key in ("recentNews", "news", "stockNews", "latestNews", "articles"):
            value = payload.get(key)
            if isinstance(value, list):
                records.extend(item for item in value if isinstance(item, dict))
        if not records:
            records = list_from_payload(payload.get("newsData") or payload.get("recentNewsData") or {})
    else:
        records = []

    items = []
    for record in records[:30]:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        url = record.get("url") or metadata.get("url") or record.get("link") or ""
        if url and str(url).startswith("/"):
            url = urljoin("https://www.livemint.com", str(url))
        item = normalize_news_item(
            title=record.get("headline") or record.get("title") or record.get("name"),
            summary=record.get("summary") or record.get("description"),
            url=url,
            source=record.get("source") or record.get("publisher") or "LiveMint",
            published_at=record.get("lastPublishedDate") or record.get("date") or record.get("publishedAt"),
            provider="IndianAPI",
        )
        if item:
            items.append(item)
    return items


def fetch_indian_api_news(symbol: str) -> List[Dict[str, Any]]:
    data = fetch_indian_api("/stock", {"name": symbol})
    return extract_indian_api_news(data)


def fetch_google_news(symbol: str, company: str = "") -> List[Dict[str, Any]]:
    query_parts = [company or symbol, symbol, "stock", "shares", "when:30d"]
    query = " ".join(part for part in query_parts if part)
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    response = http_session().get(url, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for entry in root.findall("./channel/item")[:30]:
        source_el = entry.find("source")
        source_name = source_el.text if source_el is not None else "Google News"
        source_url = source_el.attrib.get("url", "") if source_el is not None else ""
        item = normalize_news_item(
            title=entry.findtext("title"),
            summary=entry.findtext("description"),
            url=source_url or entry.findtext("link") or "",
            source=source_name,
            published_at=entry.findtext("pubDate"),
            provider="Google News",
        )
        if item:
            items.append(item)
    return items


def dedupe_news(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item.get("title", "").lower()).strip()[:120]
        domain = item.get("domain") or item.get("source")
        combined = (key, domain)
        if key and combined not in seen:
            seen.add(combined)
            deduped.append(item)
    return deduped


def fetch_verified_news(symbol: str, company: str = "") -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        items.extend(fetch_indian_api_news(symbol))
    except Exception:
        pass
    try:
        items.extend(fetch_google_news(symbol, company))
    except Exception:
        pass

    def sort_key(item: Dict[str, Any]) -> str:
        return str(item.get("published_at") or "")

    deduped = dedupe_news(items)
    deduped.sort(key=sort_key, reverse=True)
    return deduped[:20]


def parse_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def parse_metric_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    multiplier = 1.0
    lowered = text.lower()
    if "lakh cr" in lowered or "lakh crore" in lowered:
        multiplier = 100000
    elif "cr" in lowered or "crore" in lowered:
        multiplier = 1
    elif "bn" in lowered or "billion" in lowered:
        multiplier = 100
    elif "mn" in lowered or "million" in lowered:
        multiplier = 0.1
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def metric_value(data: Any, fragments: tuple[str, ...]) -> Optional[float]:
    value = value_by_key_fragment(data, fragments)
    return parse_metric_number(value)


def prime_nse(session: requests.Session) -> None:
    session.headers.update({"Host": "www.nseindia.com"})
    session.get(NSE_HOME, timeout=8)


def fetch_nse_announcements(symbol: str) -> List[Dict[str, Any]]:
    if nsefetch is not None:
        try:
            data = nsefetch(f"{NSE_HOME}/api/corporate-announcements?index=equities&symbol={quote(symbol)}")
            return normalize_nse_announcements(symbol, data)
        except Exception:
            pass
    session = http_session()
    prime_nse(session)
    url = f"{NSE_HOME}/api/corporate-announcements?index=equities&symbol={quote(symbol)}"
    data = fetch_json(session, url, referer=f"{NSE_HOME}/companies-listing/corporate-filings-announcements")
    return normalize_nse_announcements(symbol, data)


def normalize_nse_announcements(symbol: str, data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        records = data.get("data") or data.get("rows") or []
    else:
        records = data or []

    items: List[Dict[str, Any]] = []
    for record in records[:20]:
        attachment = record.get("attchmntFile") or record.get("attachmentFile") or ""
        if attachment and not attachment.startswith("http"):
            attachment = urljoin("https://nsearchives.nseindia.com", attachment)
        items.append(
            {
                "source": "NSE",
                "symbol": symbol,
                "company": record.get("sm_name") or record.get("companyName") or record.get("symbol") or symbol,
                "subject": clean_text(record.get("desc") or record.get("subject") or record.get("annoucementSubject") or ""),
                "category": clean_text(record.get("category") or record.get("subCategory") or record.get("desc") or ""),
                "details": clean_text(record.get("sm_desc") or record.get("attchmntText") or record.get("details") or ""),
                "date": record.get("an_dt") or record.get("date") or record.get("exchdisstime") or "",
                "sort_date": record.get("sort_date") or record.get("an_dt") or record.get("exchdisstime") or "",
                "attachment": attachment,
                "raw": record,
            }
        )
    return items


def fetch_nse_quote(symbol: str) -> Dict[str, Any]:
    if nsefetch is not None:
        try:
            data = nsefetch(f"{NSE_HOME}/api/quote-equity?symbol={quote(symbol)}")
            return normalize_nse_quote(symbol, data)
        except Exception:
            pass
    session = http_session()
    prime_nse(session)
    url = f"{NSE_HOME}/api/quote-equity?symbol={quote(symbol)}"
    data = fetch_json(session, url, referer=f"{NSE_HOME}/get-quotes/equity?symbol={quote(symbol)}")
    return normalize_nse_quote(symbol, data)


def normalize_nse_quote(symbol: str, data: Any) -> Dict[str, Any]:
    price = data.get("priceInfo", {}) if isinstance(data, dict) else {}
    security = data.get("info", {}) if isinstance(data, dict) else {}
    last_price = price.get("lastPrice")
    return {
        "symbol": symbol,
        "company": security.get("companyName") or symbol,
        "last_price": last_price,
        "change": price.get("change"),
        "percent_change": price.get("pChange"),
        "previous_close": price.get("previousClose"),
        "open": price.get("open"),
        "day_high": price.get("intraDayHighLow", {}).get("max"),
        "day_low": price.get("intraDayHighLow", {}).get("min"),
        "valuation": extract_valuation_metrics(data if isinstance(data, dict) else {}, parse_float(last_price)),
        "provider": "nsepython" if nsefetch is not None else "NSE",
    }


def fetch_bse_announcements(symbol: str, bse_code: str = "") -> List[Dict[str, Any]]:
    session = http_session()
    session.headers.update({"Origin": BSE_HOME, "Referer": f"{BSE_HOME}/corporates/ann.html"})
    today = dt.date.today()
    start = today - dt.timedelta(days=45)
    params = {
        "pageno": "1",
        "strCat": "-1",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strScrip": bse_code.strip(),
        "strSearch": "" if bse_code.strip() else symbol,
        "strToDate": today.strftime("%Y%m%d"),
        "strType": "C",
        "PageSize": "20",
    }
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
    response = session.get(url, params=params, timeout=8)
    response.raise_for_status()
    data = response.json()
    rows = data.get("Table") or data.get("Table1") or []

    items: List[Dict[str, Any]] = []
    for record in rows[:20]:
        attachment = record.get("ATTACHMENTNAME") or record.get("NSURL") or ""
        if attachment and not attachment.startswith("http"):
            attachment = urljoin("https://www.bseindia.com/xml-data/corpfiling/AttachHis/", attachment)
        items.append(
            {
                "source": "BSE",
                "symbol": symbol,
                "company": clean_text(record.get("SLONGNAME") or record.get("SCRIP_CD") or symbol),
                "subject": clean_text(record.get("HEADLINE") or record.get("CATEGORYNAME") or ""),
                "category": clean_text(record.get("CATEGORYNAME") or ""),
                "details": clean_text(record.get("MORE") or record.get("NEWS_SUB") or ""),
                "date": record.get("NEWS_DT") or record.get("DissemDT") or "",
                "sort_date": record.get("DissemDT") or record.get("NEWS_DT") or "",
                "attachment": attachment,
                "raw": record,
            }
        )
    return items


def clean_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pdf_text(url: str) -> str:
    if not url or not url.lower().endswith(".pdf"):
        return ""
    session = http_session()
    response = session.get(url, timeout=8)
    response.raise_for_status()
    if len(response.content) > 2_500_000:
        return ""
    reader = PdfReader(io.BytesIO(response.content))
    pages = []
    for page in reader.pages[:3]:
        pages.append(page.extract_text() or "")
    return clean_text(" ".join(pages))[:8000]


POSITIVE_WORDS = {
    "approval",
    "approved",
    "award",
    "bonus",
    "buyback",
    "dividend",
    "expansion",
    "growth",
    "order",
    "profit",
    "record",
    "resignation withdrawn",
    "split",
    "upgrade",
    "wins",
}

NEGATIVE_WORDS = {
    "default",
    "downgrade",
    "fraud",
    "loss",
    "penalty",
    "pledge",
    "raid",
    "resignation",
    "show cause",
    "suspension",
    "termination",
    "warning",
}

MATERIAL_WORDS = {
    "acquisition",
    "board meeting",
    "dividend",
    "financial results",
    "fund raising",
    "merger",
    "order",
    "preferential",
    "results",
    "scheme",
    "shareholders",
}

RISK_BUCKETS = {
    "governance": {
        "label": "Governance",
        "description": "Promoter conduct, related-party dealings, weak controls, or misleading disclosures.",
        "keywords": [
            "fund diversion",
            "misutilisation",
            "mis-utilisation",
            "misappropriation",
            "siphoning",
            "fraud",
            "falsified",
            "forged",
            "misleading",
            "related party",
            "related-party",
            "promoter-controlled",
            "personal use",
            "governance",
            "internal controls",
        ],
    },
    "regulatory": {
        "label": "Regulatory / Legal",
        "description": "SEBI, exchange, tribunal, enforcement, or litigation actions.",
        "keywords": [
            "sebi",
            "show cause",
            "interim order",
            "confirmatory order",
            "restrained",
            "barred",
            "penalty",
            "investigation",
            "enforcement",
            "nclt",
            "nclat",
            "cirp",
            "insolvency",
            "ibc",
            "liquidation",
            "wilful defaulter",
        ],
    },
    "debt_stress": {
        "label": "Debt / Default",
        "description": "Default, rating downgrade, delayed repayment, insolvency, or lender stress.",
        "keywords": [
            "default",
            "downgrade",
            "rating downgraded",
            "credit rating",
            "delay in repayment",
            "debt servicing",
            "loan recall",
            "ireda",
            "bankruptcy",
            "insolvency",
            "cirp",
            "debt",
            "liquidity",
            "going concern",
        ],
    },
    "promoter_pledge": {
        "label": "Promoter / Pledge",
        "description": "Promoter pledge, stake sale, encumbrance, or control-risk signals.",
        "keywords": [
            "pledge",
            "encumbrance",
            "promoter shareholding",
            "promoter holding",
            "stake sale",
            "shares sold",
            "invocation",
            "revocation",
            "release of pledge",
        ],
    },
    "auditor_board": {
        "label": "Auditor / Board Exit",
        "description": "Auditor, CFO, KMP, independent director, or board resignations.",
        "keywords": [
            "auditor resignation",
            "resignation of auditor",
            "statutory auditor",
            "independent director resigned",
            "director resigned",
            "chief financial officer resigned",
            "company secretary resigned",
            "kmp resigned",
            "resignation",
        ],
    },
    "disclosure_quality": {
        "label": "Disclosure Quality",
        "description": "Delayed, revised, contradictory, or incomplete filings.",
        "keywords": [
            "clarification",
            "revised disclosure",
            "corrigendum",
            "delay in submission",
            "non-compliance",
            "late submission",
            "discrepancy",
            "incorrect",
            "unverified",
            "qualified opinion",
            "adverse opinion",
        ],
    },
    "price_stress": {
        "label": "Price Stress",
        "description": "Large drawdown, lower-circuit behavior, or trading restrictions.",
        "keywords": [
            "lower circuit",
            "upper circuit",
            "suspension of trading",
            "trade-to-trade",
            "asm",
            "gsm",
            "surveillance",
            "price manipulation",
            "manipulation",
        ],
    },
}

TRUSTED_NEWS_DOMAINS = {
    "bseindia.com",
    "business-standard.com",
    "economictimes.indiatimes.com",
    "financialexpress.com",
    "indianexpress.com",
    "livemint.com",
    "moneycontrol.com",
    "ndtvprofit.com",
    "nseindia.com",
    "reuters.com",
    "sebi.gov.in",
    "thehindubusinessline.com",
}

SPECULATIVE_TERMS = {
    "rumour",
    "rumor",
    "unconfirmed",
    "speculation",
    "may reportedly",
    "could reportedly",
    "social media claims",
    "whatsapp",
}

NEWS_POSITIVE_TERMS = {
    "beats estimates",
    "profit rises",
    "revenue rises",
    "order win",
    "wins order",
    "dividend",
    "buyback",
    "upgrade",
    "debt reduced",
    "approval",
    "expansion",
}

NEWS_NEGATIVE_TERMS = {
    "fraud",
    "default",
    "downgrade",
    "loss widens",
    "resignation",
    "sebi",
    "penalty",
    "insolvency",
    "nclt",
    "pledge",
    "lower circuit",
    "probe",
    "investigation",
}

ORDER_KEYWORDS = {
    "award",
    "contract",
    "customer agreement",
    "epc",
    "letter of acceptance",
    "letter of award",
    "loa",
    "order",
    "project",
    "service agreement",
    "supply agreement",
    "purchase order",
    "tender",
    "work order",
}

ORDER_EXCLUSION_TERMS = {
    "adjudication order",
    "court order",
    "credit agreement",
    "facility agreement",
    "financing agreement",
    "interim order",
    "loan agreement",
    "sebi order",
    "show cause",
    "term loan",
    "working capital facility",
}

PREFERENTIAL_KEYWORDS = {
    "allotment of warrants",
    "convertible warrants",
    "preferential allotment",
    "preferential basis",
    "preferential issue",
    "preferential offer",
    "warrants",
}

MOVEMENT_UP_TERMS = NEWS_POSITIVE_TERMS | {
    "capacity expansion",
    "fund raise",
    "large order",
    "new order",
    "partnership",
    "preferential issue",
    "strategic investor",
}

MOVEMENT_DOWN_TERMS = NEWS_NEGATIVE_TERMS | {
    "auditor resignation",
    "equity dilution",
    "pledged shares",
    "promoter selling",
    "weak results",
}

THEME_BUCKETS = {
    "data_center_ai": {
        "label": "Data Center / AI Infrastructure",
        "keywords": [
            "ai",
            "artificial intelligence",
            "cloud",
            "colocation",
            "data center",
            "gpu",
            "hyperscale",
            "server",
        ],
    },
    "water_management": {
        "label": "Water Management",
        "keywords": [
            "desalination",
            "effluent",
            "irrigation",
            "sewage",
            "stp",
            "wastewater",
            "water",
            "water treatment",
        ],
    },
    "renewable_energy": {
        "label": "Renewable Energy / Power Transition",
        "keywords": [
            "battery",
            "ev",
            "green hydrogen",
            "hybrid power",
            "renewable",
            "solar",
            "wind",
        ],
    },
    "defence_aerospace": {
        "label": "Defence / Aerospace",
        "keywords": [
            "aerospace",
            "defence",
            "defense",
            "drone",
            "missile",
            "radar",
            "space",
        ],
    },
    "railways_mobility": {
        "label": "Railways / Mobility",
        "keywords": [
            "coach",
            "metro",
            "mobility",
            "rail",
            "railway",
            "rolling stock",
            "vande bharat",
        ],
    },
    "electronics_semiconductor": {
        "label": "Electronics / Semiconductors",
        "keywords": [
            "chip",
            "electronics",
            "ems",
            "fab",
            "printed circuit",
            "semiconductor",
        ],
    },
    "healthcare_pharma": {
        "label": "Healthcare / Pharma",
        "keywords": [
            "api",
            "biosimilar",
            "clinical",
            "drug",
            "healthcare",
            "hospital",
            "pharma",
        ],
    },
    "infra_capex": {
        "label": "Infrastructure / Capex",
        "keywords": [
            "capex",
            "cement",
            "construction",
            "engineering",
            "infrastructure",
            "project",
            "road",
        ],
    },
}

THEME_TAILWINDS = {
    "data_center_ai": "AI, cloud, GPU, and data-center capex remain strong investor themes; verify revenue exposure, customer quality, and valuation.",
    "electronics_semiconductor": "Electronics manufacturing and semiconductor localization are active market themes; verify margins, scale, and customer concentration.",
    "renewable_energy": "Power transition and renewable capex are active themes; verify project economics, debt load, and execution record.",
    "defence_aerospace": "Defence indigenization has market tailwinds; verify order visibility, working capital, and execution milestones.",
    "water_management": "Water and wastewater infrastructure have structural demand; verify order conversion, receivables, and government-payment risk.",
    "railways_mobility": "Railway and mobility capex can support order flow; verify tender wins, margins, and delivery timelines.",
    "infra_capex": "Infrastructure capex can support growth, but execution, receivables, and leverage matter more than headlines.",
}

MAJOR_RED_FLAG_TERMS = {
    "default": "Debt default / repayment stress",
    "fraud": "Fraud or governance allegation",
    "insolvency": "Insolvency / NCLT risk",
    "nclt": "NCLT / tribunal action",
    "pledge": "Promoter pledge or encumbrance",
    "resignation": "Auditor, board, or KMP resignation",
    "sebi": "SEBI or regulatory action",
    "wilful defaulter": "Wilful defaulter risk",
}


def score_announcement(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    positives = sorted(word for word in POSITIVE_WORDS if word in lowered)
    negatives = sorted(word for word in NEGATIVE_WORDS if word in lowered)
    material = sorted(word for word in MATERIAL_WORDS if word in lowered)
    score = len(positives) - len(negatives) + min(len(material), 2)
    if score >= 3:
        sentiment = "constructive"
    elif score <= -1:
        sentiment = "caution"
    else:
        sentiment = "neutral"
    return {"score": score, "sentiment": sentiment, "positives": positives, "negatives": negatives, "material": material}


def first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return parts[0][:260] if parts and parts[0] else ""


def heuristic_summary(announcement: Dict[str, Any], filing_text: str = "") -> Dict[str, Any]:
    combined = clean_text(
        " ".join(
            [
                announcement.get("subject", ""),
                announcement.get("category", ""),
                announcement.get("details", ""),
                filing_text,
            ]
        )
    )
    signal = score_announcement(combined)
    subject = announcement.get("subject") or "Latest exchange announcement"
    plain = first_sentence(filing_text) or first_sentence(announcement.get("details", "")) or subject
    if not plain or len(plain) < 20:
        plain = f"{announcement.get('company') or announcement.get('symbol')} filed an exchange update titled '{subject}'."

    why = []
    if signal["material"]:
        why.append(f"It looks material because it mentions {', '.join(signal['material'][:3])}.")
    if signal["negatives"]:
        why.append(f"Risk terms detected: {', '.join(signal['negatives'][:3])}.")
    if signal["positives"]:
        why.append(f"Positive terms detected: {', '.join(signal['positives'][:3])}.")
    if not why:
        why.append("No obvious red-flag or strong positive trigger was detected from the extracted text.")

    return {
        "headline": subject,
        "plain_english": plain,
        "what_changed": why,
        "sentiment": signal["sentiment"],
        "score": signal["score"],
        "source": "rules",
    }


def openai_summary(announcement: Dict[str, Any], filing_text: str = "") -> Optional[Dict[str, Any]]:
    if not os.getenv("OPENAI_API_KEY") or OpenAI is None:
        return None
    client = OpenAI(timeout=12)
    payload = {
        "source": announcement.get("source"),
        "company": announcement.get("company"),
        "subject": announcement.get("subject"),
        "category": announcement.get("category"),
        "date": announcement.get("date"),
        "details": announcement.get("details"),
        "filing_text": filing_text[:6000],
    }
    prompt = (
        "Summarize this Indian stock exchange filing for a retail investor. "
        "Use plain English, separate facts from interpretation, and output JSON with "
        "headline, plain_english, what_changed array, sentiment one of constructive/neutral/caution, and score -3 to 3."
    )
    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload)},
            ],
            text={"format": {"type": "json_object"}},
        )
        parsed = json.loads(response.output_text)
        parsed["source"] = "openai"
        return parsed
    except Exception:
        return None


def summarize_announcement(
    announcement: Dict[str, Any],
    *,
    include_pdf: bool = False,
    include_ai: bool = False,
) -> Dict[str, Any]:
    filing_text = ""
    if include_pdf:
        try:
            filing_text = extract_pdf_text(announcement.get("attachment", ""))
        except Exception:
            filing_text = ""
    summary = None
    if include_ai:
        summary = openai_summary(announcement, filing_text)
    summary = summary or heuristic_summary(announcement, filing_text)
    normalized = {**announcement, "summary": summary, "extracted_text_available": bool(filing_text)}
    normalized.pop("raw", None)
    return normalized


def public_announcement(item: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(item)
    public.pop("raw", None)
    return public


def announcement_text(item: Dict[str, Any]) -> str:
    return clean_text(
        " ".join(
            [
                str(item.get("source", "")),
                str(item.get("subject", "")),
                str(item.get("category", "")),
                str(item.get("details", "")),
                str(item.get("summary", {}).get("plain_english", "")) if isinstance(item.get("summary"), dict) else "",
            ]
        )
    )


def risk_bucket_hits(text: str) -> List[Dict[str, Any]]:
    lowered = text.lower()
    hits = []
    for bucket_id, bucket in RISK_BUCKETS.items():
        matched = [word for word in bucket["keywords"] if word in lowered]
        if matched:
            hits.append(
                {
                    "bucket": bucket_id,
                    "label": bucket["label"],
                    "severity": min(3, max(1, len(matched))),
                    "matched_terms": matched[:5],
                }
            )
    return hits


def analyze_risk(
    symbol: str,
    quote: Dict[str, Any],
    announcements: List[Dict[str, Any]],
    holding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    bucket_map: Dict[str, Dict[str, Any]] = {
        bucket_id: {
            "id": bucket_id,
            "label": bucket["label"],
            "description": bucket["description"],
            "severity": 0,
            "signals": [],
        }
        for bucket_id, bucket in RISK_BUCKETS.items()
    }

    for item in announcements:
        text = announcement_text(item)
        for hit in risk_bucket_hits(text):
            bucket = bucket_map[hit["bucket"]]
            bucket["severity"] = max(bucket["severity"], hit["severity"])
            bucket["signals"].append(
                {
                    "source": item.get("source"),
                    "date": item.get("date"),
                    "headline": item.get("subject") or item.get("category"),
                    "terms": hit["matched_terms"],
                }
            )

    last_price = parse_float(quote.get("last_price"))
    year_low = parse_float(quote.get("year_low"))
    year_high = parse_float(quote.get("year_high"))
    percent_change = parse_float(quote.get("percent_change"))
    avg_price = parse_float((holding or {}).get("avg_price"))

    if last_price and year_high and year_high > 0:
        drawdown = ((last_price - year_high) / year_high) * 100
        if drawdown <= -35:
            bucket = bucket_map["price_stress"]
            bucket["severity"] = max(bucket["severity"], 2 if drawdown > -60 else 3)
            bucket["signals"].append(
                {
                    "source": quote.get("provider") or "Quote",
                    "date": "",
                    "headline": f"Price is {abs(drawdown):.1f}% below 52-week high.",
                    "terms": ["52-week drawdown"],
                }
            )

    if last_price and year_low and last_price <= year_low * 1.08:
        bucket = bucket_map["price_stress"]
        bucket["severity"] = max(bucket["severity"], 1)
        bucket["signals"].append(
            {
                "source": quote.get("provider") or "Quote",
                "date": "",
                "headline": "Price is close to its 52-week low.",
                "terms": ["near 52-week low"],
            }
        )

    if percent_change is not None and percent_change <= -8:
        bucket = bucket_map["price_stress"]
        bucket["severity"] = max(bucket["severity"], 2)
        bucket["signals"].append(
            {
                "source": quote.get("provider") or "Quote",
                "date": "",
                "headline": f"Large one-day move: {percent_change:.2f}%.",
                "terms": ["sharp price fall"],
            }
        )

    if avg_price and last_price:
        position_drawdown = ((last_price - avg_price) / avg_price) * 100
        if position_drawdown <= -25:
            bucket = bucket_map["price_stress"]
            bucket["severity"] = max(bucket["severity"], 2)
            bucket["signals"].append(
                {
                    "source": "Portfolio",
                    "date": "",
                    "headline": f"Your position is {position_drawdown:.1f}% below average cost.",
                    "terms": ["portfolio drawdown"],
                }
            )

    active_buckets = [bucket for bucket in bucket_map.values() if bucket["severity"] > 0]
    active_buckets.sort(key=lambda item: item["severity"], reverse=True)
    score = sum(bucket["severity"] for bucket in active_buckets)

    if score >= 9 or any(bucket["severity"] == 3 for bucket in active_buckets[:2]):
        level = "high"
        verdict = "High-risk review signal"
    elif score >= 4:
        level = "medium"
        verdict = "Watch closely"
    elif score > 0:
        level = "low"
        verdict = "Monitor"
    else:
        level = "clear"
        verdict = "No major red flags found"

    return {
        "symbol": symbol,
        "level": level,
        "score": score,
        "verdict": verdict,
        "buckets": active_buckets,
        "all_buckets": list(bucket_map.values()),
        "rules": [
            "Gensol-style severe risks include fund diversion, falsified records, SEBI action, defaults/downgrades, promoter-linked transactions, and insolvency filings.",
            "Board/auditor/KMP resignations, related-party disclosures, promoter pledge changes, and repeated corrected filings are treated as governance watch items.",
            "Price stress is not proof of fraud, but a large drawdown or 52-week-low behavior raises the urgency of checking filings.",
        ],
    }


def analyze_verified_news(news_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    positives = []
    negatives = []
    governance_flags = []
    for item in news_items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        positive_terms = [term for term in NEWS_POSITIVE_TERMS if term in text]
        negative_terms = [term for term in NEWS_NEGATIVE_TERMS if term in text]
        risk_terms = [hit["label"] for hit in risk_bucket_hits(text)]
        if positive_terms:
            positives.append({"title": item.get("title"), "source": item.get("source"), "terms": positive_terms[:4]})
        if negative_terms:
            negatives.append({"title": item.get("title"), "source": item.get("source"), "terms": negative_terms[:4]})
        if risk_terms:
            governance_flags.append({"title": item.get("title"), "source": item.get("source"), "buckets": risk_terms[:4]})

    score = min(5, len(positives)) - min(7, len(negatives) + len(governance_flags))
    if score >= 2:
        tone = "constructive"
    elif score <= -2:
        tone = "negative"
    else:
        tone = "mixed"

    return {
        "count": len(news_items),
        "tone": tone,
        "score": score,
        "positives": positives[:5],
        "negatives": negatives[:5],
        "governance_flags": governance_flags[:5],
        "summary": news_summary_sentence(news_items, tone, positives, negatives, governance_flags),
    }


def news_summary_sentence(
    news_items: List[Dict[str, Any]],
    tone: str,
    positives: List[Dict[str, Any]],
    negatives: List[Dict[str, Any]],
    governance_flags: List[Dict[str, Any]],
) -> str:
    if not news_items:
        return "No verified trusted-publisher news was found in the current scan."
    if governance_flags:
        return f"Verified news includes governance or regulatory risk signals from {governance_flags[0].get('source')}."
    if negatives:
        return f"Verified news tone is negative, led by {negatives[0].get('source')} coverage."
    if positives and tone == "constructive":
        return f"Verified news tone is constructive, led by {positives[0].get('source')} coverage."
    return f"Verified news is mixed or neutral across {len(news_items)} trusted items."


def snippet(text: str, limit: int = 240) -> str:
    cleaned = clean_text(text)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


def evidence_items(announcements: List[Dict[str, Any]], news_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for item in announcements:
        summary = item.get("summary", {}) if isinstance(item.get("summary"), dict) else {}
        text = clean_text(
            " ".join(
                [
                    str(item.get("subject", "")),
                    str(item.get("category", "")),
                    str(item.get("details", "")),
                    str(summary.get("plain_english", "")),
                ]
            )
        )
        items.append(
            {
                "kind": "announcement",
                "source": item.get("source") or "Exchange filing",
                "date": item.get("date") or "",
                "title": item.get("subject") or item.get("category") or "Exchange filing",
                "text": text,
                "url": item.get("attachment") or "",
            }
        )
    for item in news_items:
        text = clean_text(f"{item.get('title', '')} {item.get('summary', '')}")
        items.append(
            {
                "kind": "news",
                "source": item.get("source") or item.get("provider") or "Verified news",
                "date": item.get("published_at") or "",
                "title": item.get("title") or "Verified news",
                "text": text,
                "url": item.get("url") or "",
            }
        )
    return items


def money_mentions(text: str) -> List[Dict[str, Any]]:
    pattern = re.compile(
        r"(\d[\d,]*(?:\.\d+)?)\s*(crore|cr|lakh|million|mn|billion|bn)s?\b",
        re.IGNORECASE,
    )
    mentions = []
    for match in pattern.finditer(text):
        amount = parse_float(match.group(1))
        unit = match.group(2).lower()
        if amount is None:
            continue
        if unit.startswith(("cr", "crore")):
            crore_value = amount
        elif unit.startswith("lakh"):
            crore_value = amount / 100
        elif unit in {"million", "mn"}:
            crore_value = amount / 10
        elif unit in {"billion", "bn"}:
            crore_value = amount * 100
        else:
            crore_value = None
        mentions.append({"display": clean_text(match.group(0)), "crore_value": crore_value})
    return mentions


def price_mentions(text: str) -> List[str]:
    patterns = [
        r"(?:issue price|floor price|conversion price|priced at|price of)\D{0,45}(?:rs\.?|inr|rupees)\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"(?:rs\.?|inr|rupees)\s*([0-9][0-9,]*(?:\.\d+)?)\D{0,45}(?:per share|per warrant|each)",
    ]
    prices = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = parse_float(match.group(1))
            if value is not None:
                prices.append(f"Rs {value:g}")
    return list(dict.fromkeys(prices))


def has_any_term(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def has_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text.lower()))


def has_any_phrase(text: str, terms: set[str]) -> bool:
    return any(has_phrase(text, term) for term in terms)


def parse_any_date(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%Y%m%d%H%M%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def financial_year(value: Any) -> str:
    parsed = parse_any_date(value)
    if not parsed:
        return "FY unknown"
    start_year = parsed.year if parsed.month >= 4 else parsed.year - 1
    return f"FY{start_year}-{str(start_year + 1)[-2:]}"


def yahoo_symbol(symbol: str) -> str:
    return f"{symbol.replace('&', '%26')}.NS"


def fetch_price_history(symbol: str) -> List[Dict[str, Any]]:
    end = int(dt.datetime.now(dt.timezone.utc).timestamp())
    start = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=430)).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(symbol)}"
    payload = fetch_json(http_session(), f"{url}?period1={start}&period2={end}&interval=1d", referer="https://finance.yahoo.com", timeout=10)
    result = (payload.get("chart", {}).get("result") or [{}])[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    history = []
    for index, ts in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        history.append(
            {
                "date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(),
                "close": float(close),
                "high": float(highs[index]) if index < len(highs) and highs[index] is not None else float(close),
                "low": float(lows[index]) if index < len(lows) and lows[index] is not None else float(close),
            }
        )
    return history


def average(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def analyze_technical(symbol: str, quote: Dict[str, Any]) -> Dict[str, Any]:
    try:
        history = fetch_price_history(symbol)
    except Exception as exc:
        return {
            "available": False,
            "pattern": "Technical data unavailable",
            "bias": "unknown",
            "summary": f"Could not fetch daily price history: {exc}",
            "signals": [],
            "levels": {},
            "note": "Technical analysis requires external daily price history and is not financial advice.",
        }

    closes = [item["close"] for item in history]
    highs = [item["high"] for item in history]
    lows = [item["low"] for item in history]
    last = parse_float(quote.get("last_price")) or (closes[-1] if closes else None)
    if not closes or last is None:
        return {
            "available": False,
            "pattern": "Technical data unavailable",
            "bias": "unknown",
            "summary": "Daily price history did not include usable closes.",
            "signals": [],
            "levels": {},
            "note": "Technical analysis requires external daily price history and is not financial advice.",
        }

    sma20 = average(closes[-20:])
    sma50 = average(closes[-50:])
    sma200 = average(closes[-200:])
    support = min(lows[-30:]) if len(lows) >= 30 else min(lows)
    resistance = max(highs[-30:]) if len(highs) >= 30 else max(highs)
    year_high = max(highs[-252:]) if len(highs) >= 60 else max(highs)
    year_low = min(lows[-252:]) if len(lows) >= 60 else min(lows)
    recent_return = ((last - closes[-20]) / closes[-20]) * 100 if len(closes) >= 20 and closes[-20] else 0
    price_range = max(resistance - support, last * 0.03)

    signals = []
    if sma20 and sma50 and last > sma20 > sma50:
        pattern = "bullish moving-average stack"
        bias = "bullish"
        signals.append("Price is above the 20-DMA and 20-DMA is above 50-DMA.")
    elif sma20 and sma50 and last < sma20 < sma50:
        pattern = "bearish moving-average stack"
        bias = "bearish"
        signals.append("Price is below the 20-DMA and 20-DMA is below 50-DMA.")
    elif last >= resistance * 0.98 and recent_return > 5:
        pattern = "near breakout / momentum continuation"
        bias = "bullish"
        signals.append("Price is trading close to recent resistance after a strong 20-day move.")
    elif last <= support * 1.03 and recent_return < -5:
        pattern = "breakdown risk / weak momentum"
        bias = "bearish"
        signals.append("Price is close to recent support after a weak 20-day move.")
    else:
        pattern = "range-bound consolidation"
        bias = "neutral"
        signals.append("Price is between recent support and resistance.")

    if sma50 and sma200:
        if sma50 > sma200:
            signals.append("50-DMA is above 200-DMA, a constructive medium-term trend signal.")
        elif sma50 < sma200:
            signals.append("50-DMA is below 200-DMA, a cautious medium-term trend signal.")

    bullish_target = resistance + price_range * 0.618
    bearish_target = support - price_range * 0.618
    summary = f"Currently forming a {pattern}; technical bias is {bias}."
    return {
        "available": True,
        "pattern": pattern,
        "bias": bias,
        "summary": summary,
        "signals": signals[:6],
        "levels": {
            "last_price": round(last, 2),
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "sma20": round(sma20, 2) if sma20 else None,
            "sma50": round(sma50, 2) if sma50 else None,
            "sma200": round(sma200, 2) if sma200 else None,
            "year_high": round(year_high, 2),
            "year_low": round(year_low, 2),
            "bullish_target": round(bullish_target, 2),
            "bearish_target": round(max(0, bearish_target), 2),
        },
        "note": "Scenario levels are rule-based from Yahoo daily prices and recent support/resistance. They are not price targets or guaranteed outcomes.",
    }


def analyze_order_book(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    order_items = []
    yearly: Dict[str, Dict[str, Any]] = {}
    for item in items:
        text = item.get("text", "")
        lowered = text.lower()
        if not has_any_phrase(lowered, ORDER_KEYWORDS) or has_any_phrase(lowered, ORDER_EXCLUSION_TERMS):
            continue
        amounts = money_mentions(text)
        best_amount = max(
            (amount for amount in amounts if amount.get("crore_value") is not None),
            key=lambda amount: amount["crore_value"],
            default=None,
        )
        fy = financial_year(item.get("date", ""))
        if best_amount:
            bucket = yearly.setdefault(fy, {"fy": fy, "total_crore": 0.0, "item_count": 0, "undisclosed_count": 0})
            bucket["total_crore"] += float(best_amount["crore_value"])
        else:
            bucket = yearly.setdefault(fy, {"fy": fy, "total_crore": 0.0, "item_count": 0, "undisclosed_count": 0})
            bucket["undisclosed_count"] += 1
        bucket["item_count"] += 1
        order_items.append(
            {
                "fy": fy,
                "date": item.get("date", ""),
                "source": item.get("source", ""),
                "title": item.get("title", ""),
                "summary": snippet(text),
                "amount": best_amount["display"] if best_amount else "Value not disclosed",
                "amount_crore": round(float(best_amount["crore_value"]), 2) if best_amount else None,
                "url": item.get("url", ""),
            }
        )

    yearly_totals = sorted(
        [
            {
                **bucket,
                "total_crore": round(bucket["total_crore"], 2),
                "headline": f"{bucket['fy']}: Rs {bucket['total_crore']:,.2f} crore across {bucket['item_count']} fetched updates",
            }
            for bucket in yearly.values()
        ],
        key=lambda bucket: bucket["fy"],
        reverse=True,
    )
    lifetime_total = sum(bucket["total_crore"] for bucket in yearly_totals)
    if yearly_totals:
        headline = f"Latest FY disclosed order wins: {yearly_totals[0]['headline']}"
    elif order_items:
        headline = "Order/contract updates found, but value was not disclosed in fetched text"
    else:
        headline = "No order-book additions found in fetched filings/news"

    return {
        "headline": headline,
        "disclosed_order_value_crore": round(lifetime_total, 2) if lifetime_total else None,
        "yearly_totals": yearly_totals,
        "items": order_items[:8],
        "note": "FY buckets aggregate only fetched order/contract disclosures by Indian financial year. This is not the company's full audited order book unless every order was disclosed and fetched.",
    }


def analyze_preferential_issues(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    issues = []
    for item in items:
        text = item.get("text", "")
        if not has_any_term(text, PREFERENTIAL_KEYWORDS):
            continue
        amounts = money_mentions(text)
        issues.append(
            {
                "date": item.get("date", ""),
                "source": item.get("source", ""),
                "title": item.get("title", ""),
                "prices": price_mentions(text),
                "amounts": [amount["display"] for amount in amounts[:3]],
                "summary": snippet(text),
                "url": item.get("url", ""),
            }
        )
    return {
        "count": len(issues),
        "items": issues[:8],
        "note": "Preferential issue data is parsed from fetched filings/news; open the filing before relying on price or allotment terms.",
    }


def analyze_movement_drivers(
    quote: Dict[str, Any],
    announcements: List[Dict[str, Any]],
    news_items: List[Dict[str, Any]],
    order_book: Dict[str, Any],
    preferential: Dict[str, Any],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    pct_change = parse_float(quote.get("percent_change"))
    drivers = []
    if pct_change is not None:
        if pct_change >= 3:
            direction = "upward move"
            drivers.append(f"Price is up {pct_change:.2f}% in the latest quote snapshot.")
        elif pct_change <= -3:
            direction = "falling"
            drivers.append(f"Price is down {abs(pct_change):.2f}% in the latest quote snapshot.")
        else:
            direction = "stable"
            drivers.append(f"Latest move is modest at {pct_change:.2f}%.")
    else:
        direction = "unknown"
        drivers.append("Live price movement was not available from the fetched quote providers.")

    latest_texts = [announcement_text(item) for item in announcements[:4]]
    latest_texts.extend(f"{item.get('title', '')} {item.get('summary', '')}" for item in news_items[:6])
    combined = clean_text(" ".join(latest_texts)).lower()
    positives = sorted(term for term in MOVEMENT_UP_TERMS if term in combined)
    negatives = sorted(term for term in MOVEMENT_DOWN_TERMS if term in combined)

    if positives:
        drivers.append(f"Positive triggers found: {', '.join(positives[:5])}.")
    if negatives:
        drivers.append(f"Negative triggers found: {', '.join(negatives[:5])}.")
    if order_book.get("items"):
        drivers.append(order_book["headline"])
    if preferential.get("items"):
        drivers.append("Preferential issue or warrant activity is present, so dilution and investor quality need review.")
    if risk.get("level") in {"high", "medium"}:
        drivers.append(f"Risk bucket is {risk.get('level')}: {risk.get('verdict')}.")

    if direction == "upward move":
        summary = "Possible positive drivers were found near the latest upward move; this is not proof of causality."
    elif direction == "falling":
        summary = "Possible caution drivers were found near the latest downward move; this is not proof of causality."
    elif direction == "stable":
        summary = "The stock is not making a large move in the latest quote snapshot."
    else:
        summary = "Movement could not be classified because quote movement was unavailable."

    return {"direction": direction, "summary": summary, "drivers": drivers[:8]}


def analyze_themes(
    symbol: str,
    quote: Dict[str, Any],
    announcements: List[Dict[str, Any]],
    news_items: List[Dict[str, Any]],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    company_text = clean_text(
        " ".join(
            [
                symbol,
                str(quote.get("company", "")),
                str(quote.get("industry", "")),
                " ".join(announcement_text(item) for item in announcements[:8]),
                " ".join(f"{item.get('title', '')} {item.get('summary', '')}" for item in news_items[:10]),
            ]
        )
    ).lower()
    buckets = []
    for bucket_id, bucket in THEME_BUCKETS.items():
        matched = [keyword for keyword in bucket["keywords"] if keyword in company_text]
        if matched:
            buckets.append(
                {
                    "id": bucket_id,
                    "label": bucket["label"],
                    "score": len(matched),
                    "matched_terms": matched[:8],
                    "summary": f"Matched theme terms: {', '.join(matched[:5])}.",
                }
            )
    buckets.sort(key=lambda bucket: bucket["score"], reverse=True)

    if not buckets:
        assessment = "No clear business/theme exposure was found from the fetched filings/news."
        futuristic = "unclear"
    elif risk.get("level") == "high":
        assessment = "Theme mentions exist, but high-risk red flags weaken the long-term case."
        futuristic = "risky"
    elif buckets[0]["score"] >= 3:
        assessment = f"Multiple mentions point to {buckets[0]['label']}; validate revenue exposure, order quality, valuation, and execution before relying on the theme."
        futuristic = "potential"
    else:
        assessment = f"Some mentions found in {buckets[0]['label']}, but evidence is still limited."
        futuristic = "watch"

    return {"futuristic": futuristic, "assessment": assessment, "buckets": buckets[:6]}


def analyze_shareholding(quote: Dict[str, Any]) -> Dict[str, Any]:
    shareholding = quote.get("shareholding") if isinstance(quote.get("shareholding"), dict) else {}
    if not shareholding or not shareholding.get("available"):
        return {
            "available": False,
            "summary": "Shareholding could not be parsed from provider data.",
            "categories": [],
            "signals": ["Check the latest exchange shareholding pattern for promoter, FII, DII, public, and pledge trends."],
            "note": "Open the latest official quarterly shareholding pattern before relying on ownership or pledge conclusions.",
        }

    categories = [
        {"label": "Promoters", "value": shareholding.get("promoter_percent")},
        {"label": "FII / FPI", "value": shareholding.get("fii_percent")},
        {"label": "DII", "value": shareholding.get("dii_percent")},
        {"label": "Public / Others", "value": shareholding.get("public_percent")},
    ]
    present = [item for item in categories if item["value"] is not None]
    promoter = shareholding.get("promoter_percent")
    fii = shareholding.get("fii_percent")
    dii = shareholding.get("dii_percent")
    total_known = sum(float(item["value"]) for item in present)
    if len(present) >= 3 and not 90 <= total_known <= 110:
        summary = "Shareholding percentages were parsed, but totals do not sanity-check near 100%; verify official filings."
    elif promoter is not None and promoter >= 50:
        summary = "Promoter ownership appears strong; still check pledge and recent promoter transactions."
    elif fii is not None and dii is not None and fii + dii >= 20:
        summary = "Institutional ownership appears meaningful, led by FII/DII participation."
    elif present:
        summary = "Shareholding data is partially available; review latest quarterly pattern for trend changes."
    else:
        summary = "Shareholding categories were present but percentages could not be parsed reliably."

    return {
        "available": bool(present),
        "summary": summary,
        "categories": present,
        "signals": shareholding.get("signals") or [],
        "note": shareholding.get("note") or "Provider-parsed ownership data; verify with latest official shareholding pattern filings.",
    }


def analyze_major_red_flags(
    items: List[Dict[str, Any]],
    preferential: Dict[str, Any],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    flags = []
    for bucket in risk.get("buckets", []):
        if bucket.get("severity", 0) >= 2:
            flags.append(
                {
                    "severity": "high" if bucket.get("severity", 0) >= 3 else "medium",
                    "label": bucket.get("label", "Risk flag"),
                    "summary": bucket.get("description", ""),
                    "source": "Risk buckets",
                }
            )

    seen_terms = set()
    for item in items:
        text = item.get("text", "").lower()
        for term, label in MAJOR_RED_FLAG_TERMS.items():
            if term in text and (term, item.get("title")) not in seen_terms:
                seen_terms.add((term, item.get("title")))
                flags.append(
                    {
                        "severity": "high" if term in {"default", "fraud", "insolvency", "sebi", "wilful defaulter"} else "medium",
                        "label": label,
                        "summary": snippet(item.get("text", "")),
                        "source": item.get("source", ""),
                        "date": item.get("date", ""),
                        "url": item.get("url", ""),
                    }
                )
    if preferential.get("items"):
        flags.append(
            {
                "severity": "watch",
                "label": "Potential dilution from preferential issue/warrants",
                "summary": "Preferential allotments can fund growth, but they can also dilute existing shareholders.",
                "source": "Preferential issue bucket",
            }
        )

    major_flags = [flag for flag in flags if flag.get("severity") in {"high", "medium"}]
    if major_flags:
        summary = "Major red flags found."
    elif flags:
        summary = "Watch items found, but no major red flags in fetched filings/news."
    else:
        summary = "No major red flags found in fetched filings/news."

    return {
        "level": risk.get("level", "clear"),
        "summary": summary,
        "items": flags[:10],
    }


def valuation_view(quote: Dict[str, Any], themes: Dict[str, Any]) -> Dict[str, Any]:
    valuation = quote.get("valuation") if isinstance(quote.get("valuation"), dict) else {}
    metrics = valuation.get("metrics") or {}
    pe = metrics.get("pe_ratio")
    pb = metrics.get("pb_ratio")
    roe = metrics.get("roe_percent")
    debt_to_equity = metrics.get("debt_to_equity")
    growth = metrics.get("profit_growth_percent") or metrics.get("revenue_growth_percent")
    score = 0
    observations = []

    if pe is not None:
        if pe <= 20:
            score += 2
            observations.append(f"P/E {pe:g} is not demanding on an absolute basis.")
        elif pe <= 40:
            score += 1
            observations.append(f"P/E {pe:g} is moderate; compare with peers and growth.")
        elif pe >= 70:
            score -= 2
            observations.append(f"P/E {pe:g} is expensive unless growth visibility is exceptional.")
        else:
            score -= 1
            observations.append(f"P/E {pe:g} needs strong earnings growth to justify.")
    else:
        observations.append("P/E was not available from provider data.")

    if pb is not None:
        if pb <= 3:
            score += 1
            observations.append(f"P/B {pb:g} is reasonable for many non-financial businesses.")
        elif pb >= 8:
            score -= 1
            observations.append(f"P/B {pb:g} is rich; verify ROE quality.")
    if roe is not None:
        if roe >= 18:
            score += 2
            observations.append(f"ROE {roe:g}% suggests strong return profile if sustainable.")
        elif roe < 10:
            score -= 1
            observations.append(f"ROE {roe:g}% is weak; check margins and capital intensity.")
    if debt_to_equity is not None:
        if debt_to_equity <= 0.5:
            score += 1
            observations.append(f"Debt/equity {debt_to_equity:g} looks manageable.")
        elif debt_to_equity >= 2:
            score -= 2
            observations.append(f"Debt/equity {debt_to_equity:g} is elevated.")
    if growth is not None:
        if growth >= 20:
            score += 1
            observations.append(f"Growth metric {growth:g}% supports a premium if repeatable.")
        elif growth < 0:
            score -= 1
            observations.append(f"Growth metric {growth:g}% is negative.")

    if not metrics:
        status = "valuation unavailable"
        summary = "Provider did not return usable valuation metrics; do not infer cheap or expensive from price move alone."
    elif score >= 3:
        status = "valuation supportive"
        summary = "Available valuation/quality metrics look supportive, subject to peer and latest-result verification."
    elif score <= -2:
        status = "valuation demanding"
        summary = "Available valuation/quality metrics look demanding or lower quality; require stronger evidence before shortlisting."
    else:
        status = "valuation mixed"
        summary = "Available valuation metrics are mixed; compare with peers, growth, and cyclicality."

    if themes.get("futuristic") == "potential" and pe is not None and pe <= 40:
        observations.append("Theme exposure plus non-extreme P/E can make this a research candidate, not an automatic entry.")

    return {
        "status": status,
        "score": score,
        "summary": summary,
        "metrics": metrics,
        "missing": valuation.get("missing") or [],
        "observations": observations[:8],
        "note": valuation.get("note") or "Verify valuation with official financial statements and peer multiples.",
    }


def analyze_investor_view(
    symbol: str,
    quote: Dict[str, Any],
    dossier: Dict[str, Any],
    risk: Dict[str, Any],
    news_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    order_book = dossier.get("order_book", {})
    themes = dossier.get("themes", {})
    technical = dossier.get("technical", {})
    red_flags = dossier.get("red_flags", {})
    preferential = dossier.get("preferential_issues", {})
    valuation = valuation_view(quote, themes)
    positives = []
    concerns = []
    score = 0

    order_value = order_book.get("disclosed_order_value_crore") or 0
    if order_value >= 500:
        score += 3
        positives.append({"label": "Large disclosed order momentum", "value": f"Rs {order_value:,.2f} crore", "detail": order_book.get("headline", "")})
    elif order_value > 0:
        score += 1
        positives.append({"label": "Disclosed order momentum", "value": f"Rs {order_value:,.2f} crore", "detail": order_book.get("headline", "")})

    top_theme = (themes.get("buckets") or [{}])[0]
    if themes.get("futuristic") == "potential":
        score += 2
        positives.append({
            "label": "Market-theme fit",
            "value": top_theme.get("label", "Theme exposure"),
            "detail": THEME_TAILWINDS.get(top_theme.get("id"), themes.get("assessment", "")),
        })
    elif themes.get("futuristic") == "watch":
        score += 1
        positives.append({"label": "Possible theme exposure", "value": top_theme.get("label", "Watch"), "detail": themes.get("assessment", "")})

    if technical.get("bias") == "bullish":
        score += 1
        positives.append({"label": "Technical setup", "value": technical.get("pattern", "bullish"), "detail": technical.get("summary", "")})
    elif technical.get("bias") == "bearish":
        score -= 2
        concerns.append({"label": "Technical weakness", "value": technical.get("pattern", "bearish"), "detail": technical.get("summary", "")})

    if (news_analysis or {}).get("tone") == "constructive":
        score += 1
        positives.append({"label": "Verified news tone", "value": "Constructive", "detail": news_analysis.get("summary", "")})
    elif (news_analysis or {}).get("tone") == "negative":
        score -= 1
        concerns.append({"label": "Verified news tone", "value": "Negative", "detail": news_analysis.get("summary", "")})

    score += max(-3, min(3, valuation.get("score", 0)))
    if valuation.get("status") == "valuation supportive":
        positives.append({"label": "Valuation quality", "value": valuation["status"], "detail": valuation.get("summary", "")})
    elif valuation.get("status") in {"valuation demanding", "valuation unavailable"}:
        concerns.append({"label": "Valuation check", "value": valuation["status"], "detail": valuation.get("summary", "")})

    if risk.get("level") == "high":
        score -= 5
        concerns.append({"label": "Major risk bucket", "value": risk.get("verdict", "High risk"), "detail": "High-risk bucket triggered in fetched filings/news."})
    elif risk.get("level") == "medium":
        score -= 2
        concerns.append({"label": "Risk bucket", "value": risk.get("verdict", "Medium risk"), "detail": "Medium-risk bucket needs review before fresh allocation."})
    if preferential.get("items"):
        score -= 1
        concerns.append({"label": "Dilution watch", "value": f"{preferential.get('count', 0)} preferential issue update(s)", "detail": "Check issue price, investor quality, lock-in, and use of proceeds."})
    if red_flags.get("items"):
        concerns.extend(red_flags["items"][:3])

    if risk.get("level") == "high":
        stance = "High-risk: do not shortlist without deeper diligence"
        confidence = "medium"
    elif score >= 5 and valuation.get("status") != "valuation demanding":
        stance = "Attractive research candidate"
        confidence = "medium" if valuation.get("metrics") else "low"
    elif score >= 2:
        stance = "Watchlist candidate; verify valuation and execution"
        confidence = "medium" if valuation.get("metrics") else "low"
    elif score <= -2:
        stance = "Weak setup; wait for better evidence"
        confidence = "medium"
    else:
        stance = "Neutral; evidence is not decisive"
        confidence = "low"

    next_checks = [
        "Open latest annual/quarterly results and verify revenue, margin, debt, cash flow, and promoter pledge trends.",
        "Compare P/E, P/B, ROE, growth, and order-book-to-sales against direct peers.",
        "Read the original order/preferential issue filings before relying on parsed amounts.",
    ]
    if top_theme.get("id") in THEME_TAILWINDS:
        next_checks.append("Validate how much revenue actually comes from the detected theme; theme keywords alone are not enough.")

    return {
        "symbol": symbol,
        "stance": stance,
        "score": max(0, min(100, 50 + score * 7)),
        "confidence": confidence,
        "summary": f"{stance}. Score is driven by disclosed orders, theme fit, valuation availability, technical bias, verified news, and red flags.",
        "positives": positives[:6],
        "concerns": concerns[:8],
        "valuation": valuation,
        "market_tailwind": {
            "label": top_theme.get("label", "No strong theme detected"),
            "summary": THEME_TAILWINDS.get(top_theme.get("id"), themes.get("assessment", "No clear market tailwind was detected from fetched text.")),
            "score": top_theme.get("score", 0),
        },
        "technical": {
            "bias": technical.get("bias", "unknown"),
            "pattern": technical.get("pattern", "unknown"),
            "summary": technical.get("summary", "Technical setup unavailable."),
            "levels": technical.get("levels", {}),
        },
        "next_checks": next_checks[:5],
        "disclaimer": "Research decision-support only, not personalized investment advice.",
    }


def analyze_dossier(
    symbol: str,
    quote: Dict[str, Any],
    announcements: List[Dict[str, Any]],
    news_items: List[Dict[str, Any]],
    risk: Dict[str, Any],
) -> Dict[str, Any]:
    items = evidence_items(announcements, news_items)
    order_book = analyze_order_book(items)
    preferential = analyze_preferential_issues(items)
    return {
        "order_book": order_book,
        "preferential_issues": preferential,
        "movement": analyze_movement_drivers(quote, announcements, news_items, order_book, preferential, risk),
        "themes": analyze_themes(symbol, quote, announcements, news_items, risk),
        "technical": analyze_technical(symbol, quote),
        "shareholding": analyze_shareholding(quote),
        "red_flags": analyze_major_red_flags(items, preferential, risk),
    }


def recommendation(
    quote: Dict[str, Any],
    announcements: List[Dict[str, Any]],
    holding: Optional[Dict[str, Any]],
    risk: Optional[Dict[str, Any]] = None,
    news_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    latest_scores = [item.get("summary", {}).get("score", 0) for item in announcements[:5]]
    news_score = sum(float(score or 0) for score in latest_scores)
    pct_change = quote.get("percent_change")
    avg_price = (holding or {}).get("avg_price") or 0
    last_price = quote.get("last_price") or 0

    reasons = []
    if announcements:
        latest = announcements[0]
        reasons.append(f"Latest official filing: {latest.get('source')} - {latest.get('subject') or 'announcement'}")
    else:
        reasons.append("No recent official NSE/BSE announcement was found for the query window.")

    if pct_change is not None:
        reasons.append(f"Today move on NSE quote: {pct_change:.2f}%.")

    unrealized_pct = None
    if avg_price and last_price:
        unrealized_pct = ((float(last_price) - float(avg_price)) / float(avg_price)) * 100
        reasons.append(f"Position is {unrealized_pct:.2f}% versus your average price.")

    risk_level = (risk or {}).get("level")
    risk_score = int((risk or {}).get("score") or 0)
    verified_news_count = int((news_analysis or {}).get("count") or 0)
    verified_news_score = int((news_analysis or {}).get("score") or 0)
    if risk_level == "high":
        action = "High-risk review signal"
        reasons.append("High-risk governance, regulatory, debt, or price-stress bucket triggered.")
    elif risk_level == "medium":
        action = "Caution signal / additional review"
        reasons.append("Multiple risk buckets need review before adding fresh money.")
    elif news_score <= -2:
        action = "Caution / reduce-risk review"
        reasons.append("Recent announcement language carries caution signals.")
    elif verified_news_score <= -2:
        action = "Caution / reduce-risk review"
        reasons.append("Verified news coverage has a negative tilt.")
    elif news_score >= 4 and (pct_change is None or pct_change > -3):
        action = "Constructive signal / valuation check"
        reasons.append("Recent filing signals look constructive, but valuation still needs a separate check.")
    elif verified_news_score >= 2 and risk_level in {"clear", "low"}:
        action = "Constructive signal / watchlist"
        reasons.append("Verified news tone is constructive and no major risk bucket is active.")
    elif unrealized_pct is not None and unrealized_pct < -12 and news_score <= 0:
        action = "Re-check thesis"
        reasons.append("The stock is materially below your cost without a strong positive filing trigger.")
    else:
        action = "Neutral signal / watch"
        reasons.append("Signals are mixed or not strong enough for a decisive action.")

    confidence_score = 35
    confidence_score += min(20, len(announcements) * 2)
    confidence_score += min(20, verified_news_count * 2)
    if risk_level in {"high", "medium"}:
        confidence_score += min(15, risk_score * 2)
    if not announcements and not verified_news_count:
        confidence_score -= 20
    confidence_score = max(15, min(95, confidence_score))

    if confidence_score >= 70:
        confidence = "high"
    elif confidence_score >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    if news_analysis and news_analysis.get("summary"):
        reasons.append(news_analysis["summary"])

    return {
        "action": action,
        "confidence": confidence,
        "confidence_score": confidence_score,
        "reasons": reasons,
        "disclaimer": "Decision-support signal only, not financial advice. The app does not yet include full fundamentals, valuation, peer comparison, or suitability checks.",
    }


def compact_stock_context(context: Dict[str, Any]) -> Dict[str, Any]:
    dossier = context.get("dossier", {})
    return {
        "symbol": context.get("symbol"),
        "quote": context.get("quote", {}),
        "recommendation": context.get("recommendation", {}),
        "investor_view": context.get("investor_view", {}),
        "risk": context.get("risk", {}),
        "valuation": (context.get("investor_view", {}) or {}).get("valuation", {}),
        "order_book": dossier.get("order_book", {}),
        "preferential_issues": dossier.get("preferential_issues", {}),
        "movement": dossier.get("movement", {}),
        "themes": dossier.get("themes", {}),
        "technical": dossier.get("technical", {}),
        "shareholding": dossier.get("shareholding", {}),
        "red_flags": dossier.get("red_flags", {}),
        "latest_announcements": [
            {
                "date": item.get("date"),
                "source": item.get("source"),
                "subject": item.get("subject"),
                "summary": (item.get("summary") or {}).get("plain_english") if isinstance(item.get("summary"), dict) else "",
            }
            for item in context.get("announcements", [])[:5]
        ],
        "verified_news": [
            {"source": item.get("source"), "title": item.get("title"), "summary": item.get("summary")}
            for item in context.get("verified_news", [])[:5]
        ],
    }


def deterministic_stock_answer(question: str, context: Dict[str, Any]) -> str:
    compact = compact_stock_context(context)
    lowered_question = question.lower()
    valuation = compact.get("valuation", {})
    has_valuation = bool(valuation.get("metrics"))
    unsupported_terms = {
        "cash flow",
        "debt to equity",
        "ebitda",
        "financials",
        "margin",
        "peer",
        "profit",
        "ratio",
        "revenue",
        "roce",
    }
    if any(term in lowered_question for term in unsupported_terms):
        return (
            f"Question: {question} "
            "That data is not available in the current scan. This portal currently uses fetched filings, trusted news, quote/price history, "
            "portfolio cost basis, and provider shareholding payloads where available. For fundamentals, valuation, ratios, peer comparison, "
            "cash flow, or leverage, open official financial statements and a valuation source before deciding. This is not financial advice."
        )
    recommendation_data = compact.get("recommendation", {})
    risk = compact.get("risk", {})
    movement = compact.get("movement", {})
    technical = compact.get("technical", {})
    themes = compact.get("themes", {})
    investor_view = compact.get("investor_view", {})
    order_book = compact.get("order_book", {})
    shareholding = compact.get("shareholding", {})
    red_flags = compact.get("red_flags", {})
    parts = [
        f"Question: {question}",
        f"Investor stance: {investor_view.get('stance', 'No investor stance available')} with score {investor_view.get('score', '-')}/100.",
        f"For {compact.get('symbol')}, the current decision-support signal is {recommendation_data.get('action', 'Neutral signal / watch')} with {recommendation_data.get('confidence', 'low')} confidence.",
        valuation.get("summary", "") if has_valuation else "Valuation metrics are not available in the current scan.",
        movement.get("summary", ""),
        technical.get("summary", ""),
        themes.get("assessment", ""),
        order_book.get("headline", ""),
        shareholding.get("summary", ""),
        red_flags.get("summary", ""),
        f"Risk verdict: {risk.get('verdict', 'No risk verdict available')}.",
    ]
    reasons = recommendation_data.get("reasons") or []
    if reasons:
        parts.append("Key reasons: " + " ".join(reasons[:4]))
    parts.append("This is grounded only in the fetched context and is not financial advice. Verify filings, fundamentals, valuation, and position sizing before acting.")
    return " ".join(part for part in parts if part)


def openai_stock_answer(question: str, context: Dict[str, Any]) -> Optional[str]:
    if not os.getenv("OPENAI_API_KEY") or OpenAI is None:
        return None
    client = OpenAI(timeout=18)
    payload = compact_stock_context(context)
    system_prompt = (
        "You are a cautious Indian equities research assistant. Answer only from the provided JSON context. "
        "Separate facts, inference, and risk. Do not invent financial, valuation, peer, or ownership data. "
        "If the requested data is missing, say it is not available in the current scan. Cite the relevant context fields by name, "
        "such as investor_view, valuation, latest_announcements, verified_news, order_book, technical, shareholding, risk, or red_flags. "
        "Do not give direct trade advice; frame answers as decision-support signals. Include a short disclaimer that this is not financial advice."
    )
    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({"question": question, "context": payload})},
            ],
        )
        return response.output_text
    except Exception:
        return None


def gemini_stock_answer(question: str, context: Dict[str, Any]) -> Optional[str]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    payload = compact_stock_context(context)
    system_prompt = (
        "You are a cautious Indian equities research assistant. Use only the provided JSON context. "
        "Give an investor decision-support view with facts, positives, risks, valuation caveats, technical setup, and next checks. "
        "Do not invent financials, valuation, order-book values, or peer data. If data is missing, say so. "
        "Do not give direct trade advice; use research-candidate/watchlist/high-risk language and include a brief not-financial-advice disclaimer."
    )
    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    try:
        response = http_session().post(
            f"{GEMINI_HOME}/models/{model}:generateContent",
            params={"key": key},
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": f"{system_prompt}\n\nQuestion: {question}\n\nContext JSON:\n{json.dumps(payload, default=str)}"
                            }
                        ],
                    }
                ],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
            },
            timeout=18,
        )
        response.raise_for_status()
        data = response.json()
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()
        return text or None
    except Exception:
        return None


def answer_stock_question(symbol: str, question: str, bse_code: str = "") -> Dict[str, Any]:
    context = stock_context(symbol, bse_code)
    answer = gemini_stock_answer(question, context)
    source = "gemini" if answer else "rules"
    if not answer:
        answer = openai_stock_answer(question, context)
        source = "openai" if answer else "rules"
    answer = answer or deterministic_stock_answer(question, context)
    return {
        "symbol": symbol,
        "question": question,
        "answer": answer,
        "source": source,
        "disclaimer": "Educational signal only, not financial advice.",
    }


def stock_context(symbol: str, bse_code: str = "", holding: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    quote: Dict[str, Any] = {"symbol": symbol}
    try:
        quote = fetch_indian_api_stock(symbol)
    except Exception:
        try:
            quote = fetch_nse_quote(symbol)
        except Exception:
            pass
    announcements = combined_announcements(symbol, bse_code or (holding or {}).get("bse_code", ""))
    verified_news = fetch_verified_news(symbol, quote.get("company") or (holding or {}).get("company_name", ""))
    news_analysis = analyze_verified_news(verified_news)
    risk = analyze_risk(symbol, quote, announcements, holding)
    dossier = analyze_dossier(symbol, quote, announcements, verified_news, risk)
    recommendation_data = recommendation(quote, announcements, holding, risk, news_analysis)
    investor_view = analyze_investor_view(symbol, quote, dossier, risk, news_analysis)
    return {
        "symbol": symbol,
        "quote": quote,
        "announcements": announcements,
        "verified_news": verified_news,
        "news_analysis": news_analysis,
        "risk": risk,
        "dossier": dossier,
        "recommendation": recommendation_data,
        "investor_view": investor_view,
    }


def combined_announcements(symbol: str, bse_code: str = "") -> List[Dict[str, Any]]:
    errors = []
    items: List[Dict[str, Any]] = []
    try:
        items.extend(fetch_indian_api_announcements(symbol))
    except Exception as exc:
        errors.append(f"IndianAPI: {exc}")
    try:
        items.extend(fetch_nse_announcements(symbol))
    except Exception as exc:
        errors.append(f"NSE: {exc}")
    try:
        items.extend(fetch_bse_announcements(symbol, bse_code))
    except Exception as exc:
        errors.append(f"BSE: {exc}")

    def sort_key(item: Dict[str, Any]) -> dt.datetime:
        value = str(item.get("sort_date") or item.get("date") or "")
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%d-%b-%Y %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%Y-%m-%dT%H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
        ):
            try:
                return dt.datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
        return dt.datetime.min

    items.sort(key=sort_key, reverse=True)
    for index, item in enumerate(items[:8]):
        item.update(summarize_announcement(item, include_pdf=False, include_ai=index == 0))
    if not items and errors:
        raise HTTPException(status_code=502, detail="; ".join(errors))
    return [public_announcement(item) for item in items[:12]]


@app.on_event("startup")
def startup() -> None:
    db().close()
    threading.Thread(target=warm_stock_master_cache, daemon=True).start()


@app.get("/health")
@app.head("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.head("/")
def index_head() -> Response:
    return Response(status_code=200)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/stocks/search")
def search_stocks(q: str = Query("", min_length=0), limit: int = Query(50, ge=1, le=100)) -> Dict[str, Any]:
    query = clean_text(q)
    if len(query) < 2:
        return {"query": query, "items": []}
    return {"query": query, "items": search_stock_master(query, limit)}


@app.post("/api/auth/register")
def register(payload: AuthIn) -> Dict[str, Any]:
    email = normalize_email(payload.email)
    salt = secrets.token_hex(16)
    password_hash = hash_password(payload.password, salt)
    conn = db()
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (email, password_hash, salt, now_iso()),
        )
        token = create_session(conn, int(cursor.lastrowid))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="An account already exists for this email.")
    conn.close()
    return auth_payload(user, token)


@app.post("/api/auth/login")
def login(payload: AuthIn) -> Dict[str, Any]:
    email = normalize_email(payload.email)
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or hash_password(payload.password, user["salt"]) != user["password_hash"]:
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_session(conn, int(user["id"]))
    conn.commit()
    conn.close()
    return auth_payload(user, token)


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    token = bearer_token(authorization)
    conn = db()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"user": user}


@app.get("/api/portfolio", response_model=List[PortfolioOut])
def list_portfolio(user: Dict[str, Any] = Depends(current_user)) -> List[Dict[str, Any]]:
    conn = db()
    rows = conn.execute("SELECT * FROM portfolio WHERE user_id = ? ORDER BY symbol", (user["id"],)).fetchall()
    conn.close()
    return [row_to_portfolio(row) for row in rows]


@app.post("/api/portfolio", response_model=PortfolioOut)
def add_portfolio(item: PortfolioIn, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    symbol = normalize_symbol(item.symbol)
    bse_code = normalize_bse_code(item.bse_code)
    stamp = now_iso()
    conn = db()
    try:
        cursor = conn.execute(
            """
            INSERT INTO portfolio (user_id, symbol, company_name, bse_code, quantity, avg_price, thesis, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], symbol, item.company_name, bse_code, item.quantity, item.avg_price, item.thesis, stamp, stamp),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM portfolio WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="This symbol is already in your portfolio.")
    conn.close()
    return row_to_portfolio(row)


@app.put("/api/portfolio/{item_id}", response_model=PortfolioOut)
def update_portfolio(item_id: int, item: PortfolioIn, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    symbol = normalize_symbol(item.symbol)
    bse_code = normalize_bse_code(item.bse_code)
    conn = db()
    try:
        conn.execute(
            """
            UPDATE portfolio
            SET symbol = ?, company_name = ?, bse_code = ?, quantity = ?, avg_price = ?, thesis = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (symbol, item.company_name, bse_code, item.quantity, item.avg_price, item.thesis, now_iso(), item_id, user["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM portfolio WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="This symbol is already in your portfolio.")
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Portfolio item not found.")
    return row_to_portfolio(row)


@app.delete("/api/portfolio/{item_id}")
def delete_portfolio(item_id: int, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    conn = db()
    cursor = conn.execute("DELETE FROM portfolio WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Portfolio item not found.")
    return {"ok": True}


@app.get("/api/stock/{symbol}/announcements")
def stock_announcements(symbol: str, bse_code: str = Query("")) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    items = combined_announcements(normalized, normalize_bse_code(bse_code))
    return {"symbol": normalized, "announcements": items}


@app.get("/api/stock/{symbol}/news")
def stock_news(symbol: str, company: str = Query("")) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    items = fetch_verified_news(normalized, company)
    return {"symbol": normalized, "verified_news": items, "news_analysis": analyze_verified_news(items)}


@app.get("/api/stock/{symbol}/quote")
def stock_quote(symbol: str) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    try:
        return fetch_indian_api_stock(normalized)
    except Exception as exc:
        indian_api_error = exc
    try:
        return fetch_nse_quote(normalized)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Quote failed. IndianAPI: {indian_api_error}; NSE: {exc}")


@app.get("/api/stock/{symbol}/insight")
def stock_insight(symbol: str, bse_code: str = Query(""), authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    normalized_bse = normalize_bse_code(bse_code)
    user = optional_current_user(authorization)
    holding_dict = None
    if user:
        conn = db()
        holding = conn.execute("SELECT * FROM portfolio WHERE symbol = ? AND user_id = ?", (normalized, user["id"])).fetchone()
        conn.close()
        holding_dict = row_to_portfolio(holding) if holding else None
    return stock_context(normalized, normalized_bse, holding_dict)


@app.post("/api/stock/{symbol}/chat")
def stock_chat(symbol: str, payload: StockChatIn) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    return answer_stock_question(normalized, clean_text(payload.question), normalize_bse_code(payload.bse_code))


@app.get("/api/portfolio/risks")
def portfolio_risks(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    conn = db()
    rows = conn.execute("SELECT * FROM portfolio WHERE user_id = ? ORDER BY symbol", (user["id"],)).fetchall()
    conn.close()
    holdings = [row_to_portfolio(row) for row in rows]
    results = []
    for holding in holdings:
        symbol = normalize_symbol(holding["symbol"])
        try:
            context = stock_context(symbol, holding.get("bse_code", ""), holding)
            results.append(
                {
                    "holding": holding,
                    "symbol": symbol,
                    "company": context.get("quote", {}).get("company") or holding.get("company_name") or symbol,
                    "quote": context.get("quote", {}),
                    "risk": context.get("risk", {}),
                    "recommendation": context.get("recommendation", {}),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "holding": holding,
                    "symbol": symbol,
                    "company": holding.get("company_name") or symbol,
                    "error": str(exc),
                }
            )
    high_count = sum(1 for item in results if item.get("risk", {}).get("level") == "high")
    medium_count = sum(1 for item in results if item.get("risk", {}).get("level") == "medium")
    return {"count": len(results), "high_count": high_count, "medium_count": medium_count, "items": results}
