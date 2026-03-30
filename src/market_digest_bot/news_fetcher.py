from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from . import ai_processor, direct_feeds
from .models import DigestData, IndexSnapshot, Mover, NewsItem

LOGGER = logging.getLogger("market_digest_bot")

GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"
YAHOO_SCREENER_BASE = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
CATEGORY_LIMITS = {
    "macro": 4,
    "stock": 4,
    "crypto": 2,
}

PREFERRED_MACRO_SOURCES = {
    "Reuters",
    "AP News",
    "Bloomberg",
    "CNBC",
    "Yahoo Finance",
    "MarketWatch",
    "Fortune",
    "Barron's",
    "Financial Times",
    "The Wall Street Journal",
}

PREFERRED_STOCK_SOURCES = {
    "Reuters",
    "AP News",
    "Bloomberg",
    "CNBC",
    "Yahoo Finance",
    "MarketWatch",
    "Barron's",
    "The Wall Street Journal",
    "Investor's Business Daily",
    "The Motley Fool",
}

PREFERRED_CRYPTO_SOURCES = {
    "CoinDesk",
    "Cointelegraph",
    "Decrypt",
    "The Block",
    "Forbes",
    "Yahoo Finance",
    "Reuters",
}

HARD_NEWS_REQUIRED_KEYWORDS = {
    "macro": (
        "inflation",
        "fed",
        "federal reserve",
        "rates",
        "treasury",
        "yield",
        "economy",
        "gdp",
        "unemployment",
        "payroll",
        "jobs",
        "cpi",
        "pce",
        "oil",
        "crude",
        "opec",
        "tariff",
        "trade",
        "consumer",
        "budget",
        "spending",
        "recession",
        "central bank",
        "fuel",
    ),
    "stock": (
        "s&p 500",
        "nasdaq",
        "dow",
        "wall street",
        "stock market",
        "earnings",
        "guidance",
        "revenue",
        "profit",
        "results",
        "forecast",
        "outlook",
        "merger",
        "acquisition",
        "ipo",
        "bankruptcy",
        "layoff",
        "sec",
        "investigation",
        "lawsuit",
        "shares",
        "stocks",
        "indexes",
        "correction",
        "tariff",
        "inflation",
        "fed",
    ),
    "crypto": (
        "bitcoin",
        "ethereum",
        "btc",
        "eth",
        "crypto",
        "etf",
        "sec",
        "stablecoin",
        "exchange",
        "token",
        "mining",
        "miner",
        "blockchain",
        "hack",
        "exploit",
        "holdings",
        "treasury",
        "protocol",
    ),
}

HARD_NEWS_BLOCK_PHRASES = {
    "all": (
        "maximize your wealth",
        "tax strateg",
        "how to ",
        "biggest moves",
        "biggest movers",
        "with options",
        "trading another",
        "personal finance",
        "retirement",
        "credit card",
        "mortgage",
        "student loan",
        "savings",
        "budgeting",
        "one of the best times",
    ),
    "stock": (
        "buy this",
        "buy these",
        "sell this",
        "top picks",
        "trading at a discount",
        "could soar",
        "could benefit",
        "may benefit",
        "profit from",
        "tailwinds",
        "screaming buys",
        "price targets suggest",
        "potential gains",
        "overdue for a rally",
        "countertrend move",
        "massive catalyst",
        "wall street may be overlooking",
        "growth stocks",
        "buy right now",
        "worst-performing",
        "research says",
        "nyse insider",
        "all eyes on the s&p 500",
        "best of the bunch",
        "winners and losers of",
        "unpacking q4 earnings",
        "spotting winners",
        "for long-term investors",
        "favorite stock",
        "don't forget this one",
        "looked this gloomy before",
        "vs the rest",
        "the rest of the",
        "market today",
        "(live)",
        "underwhelm",
        "reflecting on",
        "plenty of upside",
        "top analyst calls",
        "earnings call highlights",
        "getting closer to its ending",
    ),
}

HARD_NEWS_BLOCK_PATTERNS = (
    re.compile(r"\bwatch\b.*\blive\b"),
    re.compile(r"\bmade .* list\b"),
    re.compile(r"\?"),
)

INDEX_DEFINITIONS = (
    ("S&P 500", "^GSPC"),
    ("Nasdaq", "^IXIC"),
    # Yahoo's public chart endpoint exposes liquid MSCI World ETFs more reliably than the raw index.
    ("MSCI World", "URTH"),
    ("Europe 600", "^STOXX"),
    ("Bitcoin", "BTC-USD"),
)


@dataclass(slots=True)
class NewsBundle:
    macro_news: list[NewsItem]
    stock_news: list[NewsItem]
    crypto_news: list[NewsItem]
    indexes: list[IndexSnapshot]
    gainers: list[Mover]
    losers: list[Mover]
    macro_used_google_fallback: bool = False
    stock_used_google_fallback: bool = False
    crypto_used_google_fallback: bool = False


async def fetch_news_bundle(target_date: date, *, target_timezone: tzinfo = UTC) -> NewsBundle:
    exact_queries = {
        "macro": _build_google_news_query(
            '("Federal Reserve" OR inflation OR GDP OR unemployment OR payrolls OR recession OR economy OR macroeconomics)',
            target_date,
        ),
        "stock": _build_google_news_query(
            '("stock market" OR "Wall Street" OR "S&P 500" OR Nasdaq OR Dow OR earnings)',
            target_date,
        ),
        "crypto": _build_google_news_query(
            "(bitcoin OR ethereum OR BTC OR ETH)",
            target_date,
        ),
    }

    async with httpx.AsyncClient(timeout=20.0, headers=BROWSER_HEADERS, follow_redirects=True) as client:
        direct_tasks = {
            "macro": direct_feeds.fetch_direct_feeds(client, "macro", target_date, timezone=target_timezone),
            "stock": direct_feeds.fetch_direct_feeds(client, "stock", target_date, timezone=target_timezone),
            "crypto": direct_feeds.fetch_direct_feeds(client, "crypto", target_date, timezone=target_timezone),
        }
        direct_responses = await asyncio.gather(*direct_tasks.values(), return_exceptions=True)

        parsed: dict[str, list[NewsItem]] = {"macro": [], "stock": [], "crypto": []}
        for name, payload in zip(direct_tasks, direct_responses, strict=True):
            if isinstance(payload, Exception):
                LOGGER.warning("Direct feed collection failed for %s: %s", name, payload)
                continue
            parsed[name] = _filter_hard_news(payload, name)

        google_tasks = {
            name: _fetch_google_news_feed(client, exact_queries[name])
            for name, items in parsed.items()
            if len(items) < CATEGORY_LIMITS[name]
        }
        google_responses = await asyncio.gather(*google_tasks.values(), return_exceptions=True) if google_tasks else []
        mover_responses = await asyncio.gather(
            _fetch_yahoo_movers(client, "day_gainers"),
            _fetch_yahoo_movers(client, "day_losers"),
            _fetch_index_snapshots(client),
            return_exceptions=True,
        )

    google_results: dict[str, list[NewsItem] | Exception] = {}
    for name, payload in zip(google_tasks, google_responses, strict=True):
        google_results[name] = payload

    fallback_used = {"macro": False, "stock": False, "crypto": False}
    merged: dict[str, list[NewsItem]] = {}
    for name, direct_items in parsed.items():
        preferred_sources = _preferred_sources_for(name)
        google_items: list[NewsItem] = []

        if name in google_results:
            payload = google_results[name]
            if isinstance(payload, Exception):
                LOGGER.warning("Google News fallback failed for %s: %s", name, payload)
            else:
                google_items = _filter_hard_news(_filter_preferred_sources(payload, preferred_sources), name)
                fallback_used[name] = bool(google_items)

        merged[name] = _dedupe_news([*direct_items, *google_items])

    return NewsBundle(
        macro_news=merged["macro"],
        stock_news=merged["stock"],
        crypto_news=merged["crypto"],
        indexes=[] if isinstance(mover_responses[2], Exception) else mover_responses[2],
        gainers=[] if isinstance(mover_responses[0], Exception) else mover_responses[0],
        losers=[] if isinstance(mover_responses[1], Exception) else mover_responses[1],
        macro_used_google_fallback=fallback_used["macro"],
        stock_used_google_fallback=fallback_used["stock"],
        crypto_used_google_fallback=fallback_used["crypto"],
    )


async def build_live_digest(
    *,
    target_date: date,
    language: str = "en",
    target_timezone: tzinfo = UTC,
) -> DigestData:
    digest = DigestData(generated_at=datetime.now(UTC), target_date=target_date)
    bundle = await fetch_news_bundle(target_date, target_timezone=target_timezone)

    ai_result = await ai_processor.process_bundle(bundle, language=language)
    if ai_result is not None:
        digest.overview = ai_result.overview
        digest.macro_news = ai_result.macro_news
        digest.crypto_news = ai_result.crypto_news
        digest.news = ai_result.stock_news
    else:
        macro_news = _prioritize_sources(bundle.macro_news, PREFERRED_MACRO_SOURCES, limit=CATEGORY_LIMITS["macro"])
        stock_news = _prioritize_sources(bundle.stock_news, PREFERRED_STOCK_SOURCES, limit=CATEGORY_LIMITS["stock"])
        crypto_news = _prioritize_sources(bundle.crypto_news, PREFERRED_CRYPTO_SOURCES, limit=CATEGORY_LIMITS["crypto"])

        fallback_bundle = NewsBundle(
            macro_news=macro_news,
            stock_news=stock_news,
            crypto_news=crypto_news,
            indexes=bundle.indexes,
            gainers=bundle.gainers,
            losers=bundle.losers,
            macro_used_google_fallback=bundle.macro_used_google_fallback,
            stock_used_google_fallback=bundle.stock_used_google_fallback,
            crypto_used_google_fallback=bundle.crypto_used_google_fallback,
        )
        digest.overview = _build_overview(fallback_bundle, language=language)
        digest.macro_news = macro_news
        digest.crypto_news = crypto_news
        digest.news = stock_news

    digest.generated_at = datetime.now(UTC)
    digest.indexes = bundle.indexes
    digest.gainers = bundle.gainers
    digest.losers = bundle.losers
    return digest


async def _fetch_google_news_feed(client: httpx.AsyncClient, query: str) -> list[NewsItem]:
    response = await client.get(
        GOOGLE_NEWS_BASE,
        params={
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)

    items: list[NewsItem] = []
    for raw in root.findall("./channel/item"):
        source_element = raw.find("source")
        source = (source_element.text or "").strip() if source_element is not None and source_element.text else "Google News"
        link = (raw.findtext("link") or "").strip()
        title = (raw.findtext("title") or "").strip()

        if not title or not link:
            continue

        items.append(
            NewsItem(
                title=_strip_title_suffix(title, source),
                summary="",
                source=source,
                url=link,
                published_at=_parse_rfc822(raw.findtext("pubDate")),
            )
        )

    return _dedupe_news(items)


async def _fetch_yahoo_movers(client: httpx.AsyncClient, screener_id: str) -> list[Mover]:
    response = await client.get(
        YAHOO_SCREENER_BASE,
        params={
            "count": 5,
            "scrIds": screener_id,
        },
        headers={
            **BROWSER_HEADERS,
            "Origin": "https://finance.yahoo.com",
            "Referer": "https://finance.yahoo.com/markets/stocks/gainers/",
        },
    )
    response.raise_for_status()
    payload = response.json()

    results = payload.get("finance", {}).get("result", [])
    if not results:
        return []

    movers: list[Mover] = []
    for quote in results[0].get("quotes", [])[:5]:
        symbol = str(quote.get("symbol") or "").strip()
        if not symbol:
            continue

        name = (
            quote.get("shortName")
            or quote.get("longName")
            or quote.get("displayName")
            or symbol
        )
        movers.append(
            Mover(
                symbol=symbol,
                name=str(name).strip(),
                change_percent=_to_float(quote.get("regularMarketChangePercent")),
                price=_to_float(quote.get("regularMarketPrice")),
            )
        )

    return movers


async def _fetch_index_snapshots(client: httpx.AsyncClient) -> list[IndexSnapshot]:
    responses = await asyncio.gather(
        *[_fetch_single_index_snapshot(client, label=label, symbol=symbol) for label, symbol in INDEX_DEFINITIONS],
        return_exceptions=True,
    )

    snapshots: list[IndexSnapshot] = []
    for (label, symbol), payload in zip(INDEX_DEFINITIONS, responses, strict=True):
        if isinstance(payload, Exception):
            LOGGER.warning("Index snapshot fetch failed for %s (%s): %s", label, symbol, payload)
            continue
        snapshots.append(payload)

    return snapshots


async def _fetch_single_index_snapshot(
    client: httpx.AsyncClient,
    *,
    label: str,
    symbol: str,
) -> IndexSnapshot:
    encoded_symbol = quote(symbol, safe="-._~")
    response = await client.get(
        f"{YAHOO_CHART_BASE}/{encoded_symbol}",
        params={
            "range": "5d",
            "interval": "1d",
            "includePrePost": "false",
        },
    )
    response.raise_for_status()
    payload = response.json()

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise ValueError(f"Yahoo chart error for {symbol}: {chart['error']}")

    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo chart returned no results for {symbol}.")

    result = results[0]
    meta = result.get("meta", {})
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    closes = [value for value in quote_data.get("close", []) if isinstance(value, (int, float))]

    current_value = _to_float(meta.get("regularMarketPrice"))
    if current_value is None and closes:
        current_value = float(closes[-1])

    previous_close = float(closes[-2]) if len(closes) >= 2 else None
    change_percent: float | None = None
    if current_value is not None and previous_close not in (None, 0):
        change_percent = ((current_value - previous_close) / previous_close) * 100

    return IndexSnapshot(
        label=label,
        symbol=symbol,
        value=current_value,
        change_percent=change_percent,
    )


def _build_google_news_query(base_terms: str, target_date: date) -> str:
    next_date = target_date + timedelta(days=1)
    return f"{base_terms} after:{target_date.isoformat()} before:{next_date.isoformat()}"


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _strip_title_suffix(title: str, source: str) -> str:
    suffix = f" - {source}"
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def _dedupe_news(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[NewsItem] = []

    for item in items:
        key = (item.title.casefold(), item.source.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def _filter_hard_news(items: list[NewsItem], category: str) -> list[NewsItem]:
    return [item for item in items if _is_hard_news(item, category)]


def _is_hard_news(item: NewsItem, category: str) -> bool:
    text = f"{item.title} {item.summary}".casefold()

    if any(pattern.search(text) for pattern in HARD_NEWS_BLOCK_PATTERNS):
        return False

    blocked_phrases = [*HARD_NEWS_BLOCK_PHRASES["all"], *HARD_NEWS_BLOCK_PHRASES.get(category, ())]
    if any(phrase in text for phrase in blocked_phrases):
        return False

    required_keywords = HARD_NEWS_REQUIRED_KEYWORDS.get(category, ())
    return any(keyword in text for keyword in required_keywords)


def _filter_preferred_sources(items: list[NewsItem], preferred_sources: set[str]) -> list[NewsItem]:
    normalized = {source.casefold() for source in preferred_sources}
    return [item for item in items if item.source.casefold() in normalized]


def _preferred_sources_for(category: str) -> set[str]:
    if category == "macro":
        return PREFERRED_MACRO_SOURCES
    if category == "stock":
        return PREFERRED_STOCK_SOURCES
    if category == "crypto":
        return PREFERRED_CRYPTO_SOURCES
    raise ValueError(f"Unsupported category: {category}")


def _prioritize_sources(items: list[NewsItem], preferred_sources: set[str], *, limit: int) -> list[NewsItem]:
    normalized = {source.casefold() for source in preferred_sources}
    preferred = [item for item in items if item.source.casefold() in normalized]
    fallback = [item for item in items if item.source.casefold() not in normalized]
    ordered = preferred + fallback
    return ordered[:limit]


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _build_overview(bundle: NewsBundle, *, language: str = "en") -> str | None:
    titles = [item.title for item in [*bundle.macro_news, *bundle.stock_news, *bundle.crypto_news]]
    if not titles:
        return None

    theme_rules = [
        (
            "oil",
            {"oil", "crude", "iran", "middle east", "war", "ground troops", "energy"},
            {
                "en": "Oil and geopolitical risk are the main market driver today, shaping both inflation worries and equity sentiment.",
                "sk": "Ropa a geopolitické riziko sú dnes hlavným hybateľom trhu a ovplyvňujú obavy z inflácie aj náladu na akciách.",
            },
        ),
        (
            "inflation",
            {"inflation", "fed", "rate", "rates", "treasury", "yield", "pce", "cpi", "payroll", "unemployment"},
            {
                "en": "Inflation and Fed expectations are the main macro focus today, keeping rate pressure front and center.",
                "sk": "Inflácia a očakávania ohľadom Fedu sú dnes hlavnou makro témou a držia vývoj sadzieb v centre pozornosti.",
            },
        ),
        (
            "earnings",
            {"earnings", "guidance", "outlook", "revenue", "profit"},
            {
                "en": "Earnings and company guidance are setting the tone for today's stock-specific moves.",
                "sk": "Výsledky firiem a ich výhľady dnes určujú tón akciových pohybov.",
            },
        ),
        (
            "tech",
            {"ai", "chip", "semiconductor", "nvidia", "micron", "tesla", "tech"},
            {
                "en": "Tech and AI-linked names are one of the clearest market themes today.",
                "sk": "Technologické a AI tituly patria dnes medzi najvýraznejšie trhové témy.",
            },
        ),
        (
            "crypto",
            {"bitcoin", "ethereum", "btc", "eth", "crypto"},
            {
                "en": "Bitcoin and crypto sentiment are a visible part of today's market story.",
                "sk": "Bitcoin a nálada na kryptotrhu sú dnes viditeľnou súčasťou trhového diania.",
            },
        ),
    ]

    lowered_titles = [title.casefold() for title in titles]
    best_message: str | None = None
    best_score = 0
    for _, keywords, messages in theme_rules:
        score = 0
        for title in lowered_titles:
            if any(keyword in title for keyword in keywords):
                score += 1
        if score > best_score:
            best_score = score
            best_message = messages["sk"] if language == "sk" else messages["en"]

    if best_message:
        return best_message

    if language == "sk":
        return f"Hlavná téma dnes: {_trim_headline(titles[0])}."
    return f"Main focus today: {_trim_headline(titles[0])}."


def _trim_headline(title: str, limit: int = 120) -> str:
    text = title.strip().rstrip(".")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
