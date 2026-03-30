from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class GuildSettings:
    guild_id: int
    channel_id: int | None
    timezone: str
    post_time: str
    last_posted_on: str | None = None
    language: str = "en"


@dataclass(slots=True)
class NewsItem:
    title: str
    summary: str
    source: str
    url: str
    published_at: datetime | None
    symbol: str | None = None


@dataclass(slots=True)
class Mover:
    symbol: str
    name: str
    change_percent: float | None
    price: float | None = None


@dataclass(slots=True)
class IndexSnapshot:
    label: str
    symbol: str
    value: float | None
    change_percent: float | None


@dataclass(slots=True)
class DigestData:
    generated_at: datetime
    target_date: date
    overview: str | None = None
    macro_news: list[NewsItem] = field(default_factory=list)
    crypto_news: list[NewsItem] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    indexes: list[IndexSnapshot] = field(default_factory=list)
    gainers: list[Mover] = field(default_factory=list)
    losers: list[Mover] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
