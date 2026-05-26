const state = {
  portfolio: [],
  selected: null,
  activeTab: "summary",
  stockSearchTimer: null,
  stockSearchRequestId: 0,
  latestStockMatches: [],
};

const $ = (id) => document.getElementById(id);

const STOCK_DIRECTORY = [
  { symbol: "RELIANCE", name: "Reliance Industries", bse: "500325" },
  { symbol: "TCS", name: "Tata Consultancy Services", bse: "532540" },
  { symbol: "HDFCBANK", name: "HDFC Bank", bse: "500180" },
  { symbol: "ICICIBANK", name: "ICICI Bank", bse: "532174" },
  { symbol: "INFY", name: "Infosys", bse: "500209" },
  { symbol: "SBIN", name: "State Bank of India", bse: "500112" },
  { symbol: "BHARTIARTL", name: "Bharti Airtel", bse: "532454" },
  { symbol: "ITC", name: "ITC", bse: "500875" },
  { symbol: "LT", name: "Larsen & Toubro", bse: "500510" },
  { symbol: "HINDUNILVR", name: "Hindustan Unilever", bse: "500696" },
  { symbol: "AXISBANK", name: "Axis Bank", bse: "532215" },
  { symbol: "KOTAKBANK", name: "Kotak Mahindra Bank", bse: "500247" },
  { symbol: "BAJFINANCE", name: "Bajaj Finance", bse: "500034" },
  { symbol: "ASIANPAINT", name: "Asian Paints", bse: "500820" },
  { symbol: "MARUTI", name: "Maruti Suzuki India", bse: "532500" },
  { symbol: "M&M", name: "Mahindra & Mahindra", bse: "500520" },
  { symbol: "SUNPHARMA", name: "Sun Pharmaceutical Industries", bse: "524715" },
  { symbol: "TITAN", name: "Titan Company", bse: "500114" },
  { symbol: "ULTRACEMCO", name: "UltraTech Cement", bse: "532538" },
  { symbol: "WIPRO", name: "Wipro", bse: "507685" },
  { symbol: "ONGC", name: "Oil and Natural Gas Corporation", bse: "500312" },
  { symbol: "NTPC", name: "NTPC", bse: "532555" },
  { symbol: "POWERGRID", name: "Power Grid Corporation of India", bse: "532898" },
  { symbol: "TATAMOTORS", name: "Tata Motors", bse: "500570" },
  { symbol: "TATASTEEL", name: "Tata Steel", bse: "500470" },
  { symbol: "JSWSTEEL", name: "JSW Steel", bse: "500228" },
  { symbol: "ADANIENT", name: "Adani Enterprises", bse: "512599" },
  { symbol: "ADANIPORTS", name: "Adani Ports and Special Economic Zone", bse: "532921" },
  { symbol: "COALINDIA", name: "Coal India", bse: "533278" },
  { symbol: "HCLTECH", name: "HCL Technologies", bse: "532281" },
  { symbol: "TECHM", name: "Tech Mahindra", bse: "532755" },
  { symbol: "NESTLEIND", name: "Nestle India", bse: "500790" },
  { symbol: "GRASIM", name: "Grasim Industries", bse: "500300" },
  { symbol: "BAJAJFINSV", name: "Bajaj Finserv", bse: "532978" },
  { symbol: "HDFCLIFE", name: "HDFC Life Insurance", bse: "540777" },
  { symbol: "SBILIFE", name: "SBI Life Insurance", bse: "540719" },
  { symbol: "DIVISLAB", name: "Divi's Laboratories", bse: "532488" },
  { symbol: "DRREDDY", name: "Dr. Reddy's Laboratories", bse: "500124" },
  { symbol: "CIPLA", name: "Cipla", bse: "500087" },
  { symbol: "EICHERMOT", name: "Eicher Motors", bse: "505200" },
  { symbol: "HEROMOTOCO", name: "Hero MotoCorp", bse: "500182" },
  { symbol: "TATACONSUM", name: "Tata Consumer Products", bse: "500800" },
  { symbol: "BRITANNIA", name: "Britannia Industries", bse: "500825" },
  { symbol: "APOLLOHOSP", name: "Apollo Hospitals Enterprise", bse: "508869" },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return response.json();
}

function money(value) {
  if (value === null || value === undefined || value === "") return "-";
  return Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function pct(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function recommendationLevel(action = "") {
  const normalized = action.toLowerCase();
  if (normalized.includes("sell") || normalized.includes("avoid")) return "high";
  if (normalized.includes("reduce") || normalized.includes("re-check")) return "medium";
  if (normalized.includes("buy")) return "clear";
  return "low";
}

function renderRecommendationCard(rec = {}) {
  const level = recommendationLevel(rec.action || "");
  const reasons = rec.reasons || [];
  return `
    <section class="recommendation-card ${level}">
      <div class="recommendation-head">
        <div>
          <span class="label">Recommendation</span>
          <strong>${escapeHtml(rec.action || "Hold / watch")}</strong>
        </div>
        <span class="risk-badge ${level}">${escapeHtml(
          rec.confidence_score ? `${rec.confidence || "confidence"} ${rec.confidence_score}%` : rec.confidence || "low confidence"
        )}</span>
      </div>
      ${
        reasons.length
          ? `<ul>${reasons.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
          : `<p>No recommendation reasons were returned for this stock.</p>`
      }
      ${rec.disclaimer ? `<p class="small">${escapeHtml(rec.disclaimer)}</p>` : ""}
    </section>
  `;
}

function renderSummary(result) {
  const latest = result.announcements?.[0];
  const rec = result.recommendation || {};
  const quote = result.quote || {};

  $("actionText").textContent = rec.action || "No signal";
  $("confidenceText").textContent = rec.confidence_score ? `${rec.confidence || "-"} (${rec.confidence_score}%)` : rec.confidence || "-";
  $("priceText").textContent = quote.last_price ? `Rs ${money(quote.last_price)}` : "-";
  $("moveText").textContent = pct(quote.percent_change);

  if (!latest) {
    $("summary").className = "summary";
    $("summary").innerHTML = `
      ${renderRecommendationCard(rec)}
      <div class="empty-block">No latest NSE/BSE announcement found for this symbol.</div>
    `;
    return;
  }

  const summary = latest.summary || {};
  $("summary").className = "summary";
  $("summary").innerHTML = `
    ${renderRecommendationCard(rec)}
    <h3>${escapeHtml(summary.headline || latest.subject)}</h3>
    <p>${escapeHtml(summary.plain_english || "No readable summary available.")}</p>
    <ul>
      ${(summary.what_changed || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
    </ul>
  `;
}

function renderVerifiedNews(items = [], analysis = {}) {
  const tone = analysis.tone || "mixed";
  $("verifiedNews").innerHTML = `
    <div class="risk-header">
      <div>
        <span class="label">Verified News Tone</span>
        <strong>${escapeHtml(analysis.summary || "No verified news scanned yet")}</strong>
      </div>
      <span class="risk-badge ${tone === "negative" ? "high" : tone === "constructive" ? "clear" : "low"}">${escapeHtml(tone)}</span>
    </div>
    ${
      items.length
        ? `<div class="news-list">${items.map(renderNewsItem).join("")}</div>`
        : `<div class="empty-block">No trusted-publisher news was found for this stock in the current scan.</div>`
    }
  `;
}

function renderNewsItem(item) {
  const link = item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">Open source</a>` : "";
  return `
    <article class="news-item">
      <div class="meta">
        <span class="pill constructive">verified</span>
        <span>${escapeHtml(item.source || item.domain || "Trusted source")}</span>
        <span>${escapeHtml(item.published_at || "")}</span>
      </div>
      <strong>${escapeHtml(item.title)}</strong>
      ${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}
      ${link}
    </article>
  `;
}

function severityLabel(severity) {
  if (severity >= 3) return "high";
  if (severity === 2) return "medium";
  if (severity === 1) return "low";
  return "clear";
}

function renderRiskBuckets(risk = {}) {
  const buckets = risk.buckets || [];
  const rules = risk.rules || [];
  const verdict = risk.verdict || "No stock analyzed";
  const level = risk.level || "clear";

  $("riskBuckets").innerHTML = `
    <div class="risk-header">
      <div>
        <span class="label">Risk Verdict</span>
        <strong>${escapeHtml(verdict)}</strong>
      </div>
      <span class="risk-badge ${escapeHtml(level)}">${escapeHtml(level)}</span>
    </div>
    ${
      buckets.length
        ? `<div class="bucket-grid">${buckets.map(renderBucket).join("")}</div>`
        : `<div class="empty-block">No major governance, debt, promoter, board, disclosure, or price-stress red flags found in the fetched data.</div>`
    }
    <div class="rule-list">
      ${rules.map((rule) => `<p>${escapeHtml(rule)}</p>`).join("")}
    </div>
  `;
}

function renderBucket(bucket) {
  const signals = bucket.signals || [];
  return `
    <article class="bucket ${severityLabel(bucket.severity)}">
      <div class="bucket-head">
        <strong>${escapeHtml(bucket.label)}</strong>
        <span>${severityLabel(bucket.severity)}</span>
      </div>
      <p>${escapeHtml(bucket.description || "")}</p>
      <ul>
        ${signals
          .slice(0, 4)
          .map(
            (signal) => `
              <li>
                <b>${escapeHtml(signal.source || "Signal")}</b>
                ${signal.date ? ` ${escapeHtml(signal.date)}` : ""}
                <br />
                ${escapeHtml(signal.headline || "")}
                ${signal.terms?.length ? `<em>${escapeHtml(signal.terms.join(", "))}</em>` : ""}
              </li>
            `
          )
          .join("")}
      </ul>
    </article>
  `;
}

function setTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-view").forEach((view) => view.classList.add("hidden"));
  const target =
    name === "risks" ? "riskBuckets" : name === "portfolioRisks" ? "portfolioRisk" : name === "news" ? "verifiedNews" : "summary";
  $(target).classList.remove("hidden");
  if (name === "portfolioRisks") {
    loadPortfolioRisks();
  }
}

async function loadPortfolioRisks() {
  if (!state.portfolio.length) {
    $("portfolioRisk").innerHTML = `<div class="empty-block">Add portfolio stocks, then scan risk buckets.</div>`;
    return;
  }
  $("portfolioRisk").innerHTML = `<div class="empty-block">Scanning portfolio risk buckets...</div>`;
  try {
    const result = await api("/api/portfolio/risks");
    renderPortfolioRisks(result.items || []);
  } catch (error) {
    $("portfolioRisk").innerHTML = `<div class="empty-block">${escapeHtml(error.message)}</div>`;
  }
}

function renderPortfolioRisks(items) {
  $("portfolioRisk").innerHTML = items.length
    ? items
        .map((item) => {
          if (item.error) {
            return `
              <article class="portfolio-risk-card">
                <strong>${escapeHtml(item.symbol)}</strong>
                <p class="small">${escapeHtml(item.error)}</p>
              </article>
            `;
          }
          const risk = item.risk || {};
          const buckets = risk.buckets || [];
          return `
            <article class="portfolio-risk-card ${escapeHtml(risk.level || "clear")}">
              <div class="holding-main">
                <div>
                  <strong>${escapeHtml(item.symbol)}</strong>
                  <div class="small">${escapeHtml(item.company || "")}</div>
                </div>
                <span class="risk-badge ${escapeHtml(risk.level || "clear")}">${escapeHtml(risk.verdict || "Clear")}</span>
              </div>
              <p>${escapeHtml((item.recommendation || {}).action || "No action signal")}</p>
              <div class="bucket-tags">
                ${
                  buckets.length
                    ? buckets.map((bucket) => `<span>${escapeHtml(bucket.label)}: ${severityLabel(bucket.severity)}</span>`).join("")
                    : "<span>No major red flags</span>"
                }
              </div>
            </article>
          `;
        })
        .join("")
    : `<div class="empty-block">No holdings saved yet.</div>`;
}

function renderAnnouncements(items = []) {
  $("announcementList").innerHTML = items
    .map((item) => {
      const summary = item.summary || {};
      const sentiment = summary.sentiment || "neutral";
      const attachment = item.attachment
        ? `<a href="${escapeHtml(item.attachment)}" target="_blank" rel="noreferrer">Open filing</a>`
        : "";
      return `
        <article class="announcement">
          <div class="meta">
            <span class="pill ${escapeHtml(sentiment)}">${escapeHtml(sentiment)}</span>
            <span>${escapeHtml(item.source)}</span>
            <span>${escapeHtml(item.date)}</span>
            <span>${escapeHtml(item.company)}</span>
          </div>
          <strong>${escapeHtml(item.subject || "Exchange announcement")}</strong>
          <p>${escapeHtml(summary.plain_english || item.details || "")}</p>
          ${attachment}
        </article>
      `;
    })
    .join("");
}

function matchingStocks(query) {
  const normalized = query.trim().toLowerCase();
  if (normalized.length < 2) return [];
  return STOCK_DIRECTORY.map((stock) => {
    const symbol = stock.symbol.toLowerCase();
    const name = stock.name.toLowerCase();
    let score = 0;
    if (symbol === normalized || name === normalized) score += 100;
    if (symbol.startsWith(normalized)) score += 60;
    if (name.startsWith(normalized)) score += 50;
    if (symbol.includes(normalized)) score += 30;
    if (name.includes(normalized)) score += 25;
    return { ...stock, score };
  })
    .filter((stock) => stock.score > 0)
    .sort((a, b) => b.score - a.score || a.name.localeCompare(b.name))
    .slice(0, 8);
}

function hideSearchRecommendations() {
  state.latestStockMatches = [];
  $("searchRecommendations").classList.add("hidden");
  $("searchRecommendations").innerHTML = "";
}

function renderSearchRecommendations(matches) {
  state.latestStockMatches = matches;
  if (!matches.length) {
    hideSearchRecommendations();
    return;
  }
  $("searchRecommendations").classList.remove("hidden");
  $("searchRecommendations").innerHTML = matches
    .map(
      (stock) => {
        const bseCode = stock.bse_code || stock.bse || "";
        return `
        <button type="button" class="search-recommendation" data-symbol="${escapeHtml(stock.symbol)}" data-bse="${escapeHtml(bseCode)}">
          <strong>${escapeHtml(stock.name)}</strong>
          <span>${escapeHtml(stock.symbol)}${stock.series ? ` | ${escapeHtml(stock.series)}` : ""}${bseCode ? ` | BSE ${escapeHtml(bseCode)}` : ""}</span>
        </button>
      `;
      }
    )
    .join("");
}

function loadStockRecommendations(query) {
  clearTimeout(state.stockSearchTimer);
  const trimmed = query.trim();
  if (trimmed.length < 2) {
    hideSearchRecommendations();
    return;
  }

  const requestId = ++state.stockSearchRequestId;
  state.stockSearchTimer = setTimeout(async () => {
    try {
      const result = await api(`/api/stocks/search?q=${encodeURIComponent(trimmed)}&limit=10`);
      if (requestId === state.stockSearchRequestId) {
        renderSearchRecommendations(result.items || []);
      }
    } catch (error) {
      if (requestId === state.stockSearchRequestId) {
        renderSearchRecommendations(matchingStocks(trimmed));
      }
    }
  }, 180);
}

function selectedSearchMatch(value) {
  const normalized = value.trim().toLowerCase();
  return state.latestStockMatches.find((stock) => {
    const symbol = String(stock.symbol || "").toLowerCase();
    const name = String(stock.name || "").toLowerCase();
    return normalized === symbol || normalized === name;
  });
}

async function analyze(symbol, bseCode = "") {
  const normalized = symbol.trim().toUpperCase();
  if (!normalized) return;
  hideSearchRecommendations();
  $("statusText").textContent = "Fetching latest exchange data";
  $("symbolInput").value = normalized;
  $("bseInput").value = bseCode || $("bseInput").value;
  try {
    const params = bseCode ? `?bse_code=${encodeURIComponent(bseCode)}` : "";
    const result = await api(`/api/stock/${encodeURIComponent(normalized)}/insight${params}`);
    renderSummary(result);
    renderVerifiedNews(result.verified_news || [], result.news_analysis || {});
    renderRiskBuckets(result.risk || {});
    renderAnnouncements(result.announcements || []);
    $("statusText").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("statusText").textContent = "Fetch failed";
    $("summary").className = "summary empty";
    $("summary").textContent = error.message;
    renderVerifiedNews([], {});
    renderRiskBuckets({});
    renderAnnouncements([]);
  }
}

function clearForm() {
  $("portfolioId").value = "";
  $("portfolioSymbol").value = "";
  $("companyName").value = "";
  $("bseCode").value = "";
  $("quantity").value = "0";
  $("avgPrice").value = "0";
  $("thesis").value = "";
}

function fillForm(item) {
  $("portfolioId").value = item.id;
  $("portfolioSymbol").value = item.symbol;
  $("companyName").value = item.company_name || "";
  $("bseCode").value = item.bse_code || "";
  $("quantity").value = item.quantity || 0;
  $("avgPrice").value = item.avg_price || 0;
  $("thesis").value = item.thesis || "";
}

function renderPortfolio() {
  $("portfolioList").innerHTML = state.portfolio
    .map(
      (item) => `
      <article class="holding">
        <div class="holding-main">
          <div>
            <strong>${escapeHtml(item.symbol)}</strong>
            <div class="small">${escapeHtml(item.company_name || "Tracked stock")}</div>
          </div>
          <div class="holding-actions">
            <button class="ghost" data-action="analyze" data-id="${item.id}">Analyze</button>
            <button class="ghost" data-action="edit" data-id="${item.id}">Edit</button>
            <button class="danger" data-action="delete" data-id="${item.id}">Delete</button>
          </div>
        </div>
        <div class="small">Qty ${money(item.quantity)} at Rs ${money(item.avg_price)} ${item.bse_code ? `| BSE ${escapeHtml(item.bse_code)}` : ""}</div>
      </article>
    `
    )
    .join("");
}

async function loadPortfolio() {
  state.portfolio = await api("/api/portfolio");
  renderPortfolio();
  if (state.activeTab === "portfolioRisks") {
    loadPortfolioRisks();
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => setTab(button.dataset.tab));
});

$("searchForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const match = selectedSearchMatch($("symbolInput").value);
  analyze(match?.symbol || $("symbolInput").value, match?.bse_code || match?.bse || $("bseInput").value);
});

$("symbolInput").addEventListener("input", (event) => {
  loadStockRecommendations(event.target.value);
});

$("symbolInput").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideSearchRecommendations();
  }
});

$("searchRecommendations").addEventListener("click", (event) => {
  const button = event.target.closest(".search-recommendation");
  if (!button) return;
  $("symbolInput").value = button.dataset.symbol;
  $("bseInput").value = button.dataset.bse || "";
  analyze(button.dataset.symbol, button.dataset.bse || "");
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".search-symbol-field")) {
    hideSearchRecommendations();
  }
});

$("portfolioForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    symbol: $("portfolioSymbol").value,
    company_name: $("companyName").value,
    bse_code: $("bseCode").value,
    quantity: Number($("quantity").value || 0),
    avg_price: Number($("avgPrice").value || 0),
    thesis: $("thesis").value,
  };
  const id = $("portfolioId").value;
  try {
    if (id) {
      await api(`/api/portfolio/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/portfolio", { method: "POST", body: JSON.stringify(payload) });
    }
    clearForm();
    await loadPortfolio();
  } catch (error) {
    alert(error.message);
  }
});

$("portfolioList").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const item = state.portfolio.find((stock) => stock.id === Number(button.dataset.id));
  if (!item) return;
  if (button.dataset.action === "edit") {
    fillForm(item);
  }
  if (button.dataset.action === "analyze") {
    analyze(item.symbol, item.bse_code);
  }
  if (button.dataset.action === "delete") {
    await api(`/api/portfolio/${item.id}`, { method: "DELETE" });
    await loadPortfolio();
  }
});

$("newStockBtn").addEventListener("click", clearForm);

loadPortfolio().catch((error) => {
  $("portfolioList").innerHTML = `<div class="summary empty">${escapeHtml(error.message)}</div>`;
});
