from __future__ import annotations

from .models import DigestData, IndexSnapshot, Mover, NewsItem

LABELS = {
    "en": {
        "overview": "Overview",
        "macro": "Macro / Economics",
        "stock": "Stock News",
        "indexes": "Indexes",
        "gainers": "Top Gainers",
        "losers": "Top Losers",
        "notes": "Data Source Notes",
        "no_macro": "No macro items were returned for today.",
        "no_stock": "No stock news was returned.",
    },
    "sk": {
        "overview": "Preh\u013ead",
        "macro": "Makro / Ekonomika",
        "stock": "Akciov\u00e9 spr\u00e1vy",
        "indexes": "Indexy",
        "gainers": "Najv\u00e4\u010d\u0161ie rasty",
        "losers": "Najv\u00e4\u010d\u0161ie poklesy",
        "notes": "Pozn\u00e1mky k zdrojom",
        "no_macro": "Na dne\u0161ok sa nepodarilo na\u010d\u00edta\u0165 \u017eiadne makro spr\u00e1vy.",
        "no_stock": "Nepodarilo sa na\u010d\u00edta\u0165 \u017eiadne akciov\u00e9 spr\u00e1vy.",
    },
}


def build_digest_messages(data: DigestData, timezone_name: str, language: str = "en") -> list[str]:
    labels = LABELS["sk"] if language == "sk" else LABELS["en"]
    movers_section = _join_sections(
        _build_movers_section(labels["gainers"], data.gainers),
        _build_movers_section(labels["losers"], data.losers),
    )
    paragraphs = [
        _build_header(data),
        _build_overview_section(data.overview, labels["overview"]),
        _build_macro_section(data.macro_news, data.crypto_news, labels["macro"], labels["no_macro"]),
        _build_news_section(data.news, data.crypto_news, labels["stock"], labels["no_stock"]),
        _build_indexes_section(labels["indexes"], data.indexes),
        movers_section,
        _build_error_section(data.errors, labels["notes"]),
    ]

    return _chunk_for_discord([item for item in paragraphs if item])


def _build_header(data: DigestData) -> str:
    return f"**[{data.target_date.strftime('%d-%m-%Y')}]**"


def _build_overview_section(overview: str | None, title: str) -> str:
    if not overview:
        return ""
    return f"**{title}**\n{overview}"


def _build_macro_section(
    macro_news: list[NewsItem],
    crypto_news: list[NewsItem],
    title: str,
    empty_message: str,
) -> str:
    if not macro_news and not crypto_news:
        return f"**{title}**\n- {empty_message}"

    lines = [f"**{title}**"]

    section_items = _select_section_items(primary=macro_news, overflow=crypto_news, limit=4)
    for item in section_items:
        lines.append(f"- {_format_news_line(item)}")

    return "\n".join(lines)


def _build_news_section(
    news: list[NewsItem],
    crypto_news: list[NewsItem],
    title: str,
    empty_message: str,
) -> str:
    if not news and not crypto_news:
        return f"**{title}**\n- {empty_message}"

    lines = [f"**{title}**"]
    section_items = _select_section_items(primary=news, overflow=crypto_news, limit=4)
    for item in section_items:
        lines.append(f"- {_format_news_line(item)}")
    return "\n".join(lines)


def _build_movers_section(title: str, movers: list[Mover]) -> str:
    if not movers:
        return ""

    lines = [f"**{title}**"]
    for item in movers[:5]:
        percent_text = "n/a" if item.change_percent is None else f"**{item.change_percent:+.2f}%**"
        detail = percent_text
        if item.price is not None:
            detail = f"{detail} | ${item.price:,.2f}"
        lines.append(f"- `{item.symbol}` *{item.name}*: {detail}")
    return "\n".join(lines)


def _build_indexes_section(title: str, indexes: list[IndexSnapshot]) -> str:
    if not indexes:
        return ""

    lines = [f"**{title}**"]
    for item in indexes:
        detail = "n/a" if item.change_percent is None else f"**{item.change_percent:+.2f}%**"
        if item.value is not None:
            detail = f"{detail} | {item.value:,.2f}"
        lines.append(f"- *{item.label}*: {detail}")
    return "\n".join(lines)


def _build_error_section(errors: list[str], title: str) -> str:
    if not errors:
        return ""

    lines = [f"**{title}**"]
    for item in errors[:4]:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _join_sections(*sections: str) -> str:
    return "\n\n".join(section for section in sections if section)


def _trim_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_news_line(item: NewsItem) -> str:
    symbol_prefix = f"`{item.symbol}` " if item.symbol else ""
    return f"{symbol_prefix}{item.title}"


def _select_section_items(*, primary: list[NewsItem], overflow: list[NewsItem], limit: int) -> list[NewsItem]:
    selected = list(primary[:limit])
    if len(selected) >= limit:
        return selected

    remaining = limit - len(selected)
    selected.extend(overflow[:remaining])
    return selected


def _chunk_for_discord(paragraphs: list[str], limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        block = paragraph.strip()
        if not block:
            continue

        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(block) <= limit:
            current = block
            continue

        lines = block.splitlines()
        partial = ""
        for line in lines:
            candidate = line if not partial else f"{partial}\n{line}"
            if len(candidate) <= limit:
                partial = candidate
                continue

            if partial:
                chunks.append(partial)
            partial = line

        if partial:
            current = partial

    if current:
        chunks.append(current)

    return chunks
