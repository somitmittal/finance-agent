from __future__ import annotations

import csv
import datetime as dt
import email.utils
import html
import io
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
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
    quantity: float = 0
    avg_price: float = 0
    thesis: str = Field("", max_length=1000)


class PortfolioOut(PortfolioIn):
    id: int
    created_at: str
    updated_at: str


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_symbol(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9&.-]", "", symbol).upper().strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Enter a valid stock symbol.")
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

    scored = [
        {**stock, "score": stock_search_score(query, stock)}
        for stock in items
        if stock_search_score(query, stock) > 0
    ]
    scored.sort(key=lambda stock: (-stock["score"], stock.get("name", "")))
    return [{key: stock.get(key, "") for key in ("symbol", "name", "series", "bse_code")} for stock in scored[:limit]]


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            company_name TEXT DEFAULT '',
            bse_code TEXT DEFAULT '',
            quantity REAL DEFAULT 0,
            avg_price REAL DEFAULT 0,
            thesis TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def row_to_portfolio(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


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
    return {
        "symbol": symbol,
        "company": security.get("companyName") or symbol,
        "last_price": price.get("lastPrice"),
        "change": price.get("change"),
        "percent_change": price.get("pChange"),
        "previous_close": price.get("previousClose"),
        "open": price.get("open"),
        "day_high": price.get("intraDayHighLow", {}).get("max"),
        "day_low": price.get("intraDayHighLow", {}).get("min"),
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
        verdict = "Avoid / exit review"
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
        action = "Sell / avoid"
        reasons.append("High-risk governance, regulatory, debt, or price-stress bucket triggered.")
    elif risk_level == "medium":
        action = "Hold / avoid fresh buy"
        reasons.append("Multiple risk buckets need review before adding fresh money.")
    elif news_score <= -2:
        action = "Sell / reduce"
        reasons.append("Recent announcement language carries caution signals.")
    elif verified_news_score <= -2:
        action = "Hold / reduce risk"
        reasons.append("Verified news coverage has a negative tilt.")
    elif news_score >= 4 and (pct_change is None or pct_change > -3):
        action = "Buy / add only on valuation comfort"
        reasons.append("Recent filing signals look constructive, but valuation still needs a separate check.")
    elif verified_news_score >= 2 and risk_level in {"clear", "low"}:
        action = "Buy / watch entry"
        reasons.append("Verified news tone is constructive and no major risk bucket is active.")
    elif unrealized_pct is not None and unrealized_pct < -12 and news_score <= 0:
        action = "Re-check thesis"
        reasons.append("The stock is materially below your cost without a strong positive filing trigger.")
    else:
        action = "Hold / watch"
        reasons.append("Signals are mixed or not strong enough for a decisive action.")

    confidence_score = 35
    confidence_score += min(20, len(announcements) * 2)
    confidence_score += min(20, verified_news_count * 2)
    if risk_level in {"high", "medium"}:
        confidence_score += min(15, risk_score * 2)
    if action.startswith("Buy") and risk_level not in {"clear", "low"}:
        confidence_score -= 20
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
        "disclaimer": "Educational signal only, not financial advice. Check valuation, risk, and your own suitability before trading.",
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
    return {
        "symbol": symbol,
        "quote": quote,
        "announcements": announcements,
        "verified_news": verified_news,
        "news_analysis": news_analysis,
        "risk": risk,
        "recommendation": recommendation(quote, announcements, holding, risk, news_analysis),
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
def search_stocks(q: str = Query("", min_length=0), limit: int = Query(10, ge=1, le=25)) -> Dict[str, Any]:
    query = clean_text(q)
    if len(query) < 2:
        return {"query": query, "items": []}
    return {"query": query, "items": search_stock_master(query, limit)}


@app.get("/api/portfolio", response_model=List[PortfolioOut])
def list_portfolio() -> List[Dict[str, Any]]:
    conn = db()
    rows = conn.execute("SELECT * FROM portfolio ORDER BY symbol").fetchall()
    conn.close()
    return [row_to_portfolio(row) for row in rows]


@app.post("/api/portfolio", response_model=PortfolioOut)
def add_portfolio(item: PortfolioIn) -> Dict[str, Any]:
    symbol = normalize_symbol(item.symbol)
    stamp = now_iso()
    conn = db()
    try:
        cursor = conn.execute(
            """
            INSERT INTO portfolio (symbol, company_name, bse_code, quantity, avg_price, thesis, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, item.company_name, item.bse_code, item.quantity, item.avg_price, item.thesis, stamp, stamp),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM portfolio WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="This symbol is already in your portfolio.")
    conn.close()
    return row_to_portfolio(row)


@app.put("/api/portfolio/{item_id}", response_model=PortfolioOut)
def update_portfolio(item_id: int, item: PortfolioIn) -> Dict[str, Any]:
    symbol = normalize_symbol(item.symbol)
    conn = db()
    conn.execute(
        """
        UPDATE portfolio
        SET symbol = ?, company_name = ?, bse_code = ?, quantity = ?, avg_price = ?, thesis = ?, updated_at = ?
        WHERE id = ?
        """,
        (symbol, item.company_name, item.bse_code, item.quantity, item.avg_price, item.thesis, now_iso(), item_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM portfolio WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Portfolio item not found.")
    return row_to_portfolio(row)


@app.delete("/api/portfolio/{item_id}")
def delete_portfolio(item_id: int) -> Dict[str, Any]:
    conn = db()
    cursor = conn.execute("DELETE FROM portfolio WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Portfolio item not found.")
    return {"ok": True}


@app.get("/api/stock/{symbol}/announcements")
def stock_announcements(symbol: str, bse_code: str = Query("")) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    items = combined_announcements(normalized, bse_code)
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
def stock_insight(symbol: str, bse_code: str = Query("")) -> Dict[str, Any]:
    normalized = normalize_symbol(symbol)
    conn = db()
    holding = conn.execute("SELECT * FROM portfolio WHERE symbol = ?", (normalized,)).fetchone()
    conn.close()
    holding_dict = row_to_portfolio(holding) if holding else None
    return stock_context(normalized, bse_code, holding_dict)


@app.get("/api/portfolio/risks")
def portfolio_risks() -> Dict[str, Any]:
    conn = db()
    rows = conn.execute("SELECT * FROM portfolio ORDER BY symbol").fetchall()
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
