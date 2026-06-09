# Finance Agent

A local financial agent for Indian equities. It pulls latest official announcements from NSE and BSE, turns filings into plain-English summaries, and lets you maintain a watch/portfolio list with action-oriented insights.

## Run

Create a local `.env` file:

```bash
INDIAN_API_KEY=your_indianapi_key
OPENAI_API_KEY=optional_openai_key
GEMINI_API_KEY=optional_gemini_key
GEMINI_MODEL=gemini-1.5-flash
```

```bash
python3 -m uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000`.

## Deploy on Render

Set the **Start Command** to:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Render injects `PORT` automatically. The app must listen on `0.0.0.0`, not `127.0.0.1`, or the deploy health check will fail.

Add environment variables in the Render dashboard: `INDIAN_API_KEY`, and optionally `GEMINI_API_KEY`, `GEMINI_MODEL`, and `OPENAI_API_KEY`.

Health check path: `/health`

## Notes

- Enter NSE symbols such as `RELIANCE`, `TCS`, `INFY`, `HDFCBANK`.
- Data provider order is IndianAPI first, then `nsepython`/direct NSE, then BSE for announcements.
- Add a BSE scrip code in the portfolio form when you know it. BSE keyword search is also attempted when only a symbol/name is available.
- `nsepython` wraps NSE's public site endpoints. The app keeps a direct NSE fallback if the package is unavailable or fails.
- BSE announcements still use BSE's public web endpoint as a fallback/source.
- If `GEMINI_API_KEY` or `OPENAI_API_KEY` is set, the chat analyst uses that LLM for grounded answers. Gemini is tried first, then OpenAI, then deterministic heuristics.
- Recommendations are informational signals, not financial advice.
- The investor readout weighs positives, red flags, technical setup, market-theme fit, valuation availability, and next diligence checks. It does not infer cheap/expensive valuation when provider metrics are missing.

## Risk Buckets

The risk tab screens each stock and portfolio holding for Gensol-style red flags:

- Governance: fund diversion, misleading disclosures, promoter-linked transactions, weak controls.
- Regulatory / Legal: SEBI orders, exchange action, NCLT/CIRP, penalties, investigations.
- Debt / Default: default, credit-rating downgrade, delayed repayment, liquidity or going-concern stress.
- Promoter / Pledge: pledged shares, encumbrance, stake sale, invocation/revocation.
- Auditor / Board Exit: auditor, director, CFO, KMP, or company secretary resignations.
- Disclosure Quality: revised filings, corrigenda, late filings, non-compliance, qualified/adverse opinions.
- Price Stress: sharp falls, 52-week drawdowns, lower circuit, surveillance/trading restrictions.

## Verified News

The agent also scans trusted news sources before giving a decision-support signal:

- IndianAPI stock news is used when available.
- Google News RSS is queried for recent stock/company coverage.
- Only trusted publishers and official-style sources are kept, such as NSE, BSE, SEBI, Reuters, Economic Times, Business Standard, Moneycontrol, LiveMint, Indian Express, Financial Express, NDTV Profit, and Hindu BusinessLine.
- Rumor/speculation language is filtered out before scoring.
- The final signal combines exchange filings, risk buckets, verified news tone, price context, valuation fields when available, and portfolio cost basis into a confidence score.
