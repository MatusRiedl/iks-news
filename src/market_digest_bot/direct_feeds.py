from __future__ import annotations

import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from email.utils import parsedate_to_datetime

import httpx

from .models import NewsItem

LOGGER = logging.getLogger("market_digest_bot")


@dataclass(frozen=True, slots=True)
class FeedDefinition:
    source: str
    url: str


DIRECT_FEEDS: dict[str, tuple[FeedDefinition, ...]] = {
    "macro": (
        FeedDefinition("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        FeedDefinition("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
        FeedDefinition("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories"),
        FeedDefinition("MarketWatch", "https://feeds.marketwatch.com/marketwatch/marketpulse"),
        FeedDefinition("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ),
    "stock": (
        FeedDefinition("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ),
    "crypto": (
        FeedDefinition("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        FeedDefinition("Cointelegraph", "https://cointelegraph.com/rss"),
        FeedDefinition("Decrypt", "https://decrypt.co/feed"),
    ),
}

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


async def fetch_direct_feeds(
    client: httpx.AsyncClient,
    category: str,
    target_date: date,
    *,
    timezone: tzinfo = UTC,
) -> list[NewsItem]:
    feeds = DIRECT_FEEDS.get(category)
    if feeds is None:
        raise ValueError(f"Unsupported direct feed category: {category}")

    day_start = datetime.combine(target_date, time.min, tzinfo=timezone)
    day_end = day_start + timedelta(days=1)

    responses = await asyncio.gather(
        *[_fetch_single_feed(client, feed, day_start, day_end, timezone) for feed in feeds],
        return_exceptions=True,
    )

    items: list[NewsItem] = []
    for feed, payload in zip(feeds, responses, strict=True):
        if isinstance(payload, Exception):
            LOGGER.warning("Direct RSS fetch failed for %s (%s): %s", feed.source, feed.url, payload)
            continue
        items.extend(payload)

    return _dedupe_news(items)


async def _fetch_single_feed(
    client: httpx.AsyncClient,
    feed: FeedDefinition,
    day_start: datetime,
    day_end: datetime,
    timezone: tzinfo,
) -> list[NewsItem]:
    response = await client.get(feed.url, timeout=10.0)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    return _parse_feed_items(root, default_source=feed.source, day_start=day_start, day_end=day_end, timezone=timezone)


def _parse_feed_items(
    root: ET.Element,
    *,
    default_source: str,
    day_start: datetime,
    day_end: datetime,
    timezone: tzinfo,
) -> list[NewsItem]:
    rss_items = root.findall("./channel/item")
    if rss_items:
        return _parse_rss_items(
            rss_items,
            default_source=default_source,
            day_start=day_start,
            day_end=day_end,
            timezone=timezone,
        )

    atom_entries = root.findall("./atom:entry", ATOM_NS)
    if atom_entries:
        return _parse_atom_entries(
            atom_entries,
            default_source=default_source,
            day_start=day_start,
            day_end=day_end,
            timezone=timezone,
        )

    return []


def _parse_rss_items(
    items: list[ET.Element],
    *,
    default_source: str,
    day_start: datetime,
    day_end: datetime,
    timezone: tzinfo,
) -> list[NewsItem]:
    parsed: list[NewsItem] = []
    for raw in items:
        title = (raw.findtext("title") or "").strip()
        link = (raw.findtext("link") or "").strip()
        source_text = (raw.findtext("source") or "").strip()
        description = raw.findtext("description")
        published_at = _parse_datetime(
            raw.findtext("pubDate") or raw.findtext("published") or raw.findtext("updated")
        )

        if not title or not link or published_at is None:
            continue

        local_published = published_at.astimezone(timezone)
        if not (day_start <= local_published < day_end):
            continue

        source = source_text or default_source
        parsed.append(
            NewsItem(
                title=_strip_title_suffix(title, source),
                summary=_clean_summary(description),
                source=source,
                url=link,
                published_at=published_at,
            )
        )

    return parsed


def _parse_atom_entries(
    entries: list[ET.Element],
    *,
    default_source: str,
    day_start: datetime,
    day_end: datetime,
    timezone: tzinfo,
) -> list[NewsItem]:
    parsed: list[NewsItem] = []
    for raw in entries:
        title = (raw.findtext("atom:title", namespaces=ATOM_NS) or "").strip()
        summary = raw.findtext("atom:summary", namespaces=ATOM_NS) or raw.findtext("atom:content", namespaces=ATOM_NS)
        published_at = _parse_datetime(
            raw.findtext("atom:published", namespaces=ATOM_NS) or raw.findtext("atom:updated", namespaces=ATOM_NS)
        )
        link = _extract_atom_link(raw)

        if not title or not link or published_at is None:
            continue

        local_published = published_at.astimezone(timezone)
        if not (day_start <= local_published < day_end):
            continue

        parsed.append(
            NewsItem(
                title=title,
                summary=_clean_summary(summary),
                source=default_source,
                url=link,
                published_at=published_at,
            )
        )

    return parsed


def _extract_atom_link(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", ATOM_NS):
        href = (link.get("href") or "").strip()
        rel = (link.get("rel") or "alternate").strip()
        if href and rel == "alternate":
            return href

    fallback = entry.find("atom:link", ATOM_NS)
    if fallback is None:
        return ""
    return (fallback.get("href") or "").strip()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _strip_title_suffix(title: str, source: str) -> str:
    suffix = f" - {source}"
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def _clean_summary(value: str | None) -> str:
    if not value:
        return ""

    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
