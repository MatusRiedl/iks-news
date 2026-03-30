from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import Any

import httpx

from .models import NewsItem

try:
    from google import genai
except ImportError:  # pragma: no cover - optional until dependency is installed
    genai = None

LOGGER = logging.getLogger("market_digest_bot")
GEMINI_MODEL_NAME = "gemini-2.5-flash"
GROQ_MODEL_NAME = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_ITEMS = {
    "macro": 4,
    "stock": 4,
    "crypto": 2,
}

_ROOT_KEYS = ("overview", "macro", "stock", "crypto")

_gemini_client: Any | None = None
_groq_api_key: str | None = None


@dataclass(slots=True)
class AiResult:
    overview: str
    macro_news: list[NewsItem]
    stock_news: list[NewsItem]
    crypto_news: list[NewsItem]


def configure(*, gemini_api_key: str | None = None, groq_api_key: str | None = None) -> None:
    global _gemini_client, _groq_api_key

    gemini_key = (gemini_api_key or "").strip()
    _groq_api_key = (groq_api_key or "").strip() or None

    if not gemini_key or genai is None:
        _gemini_client = None
        if gemini_key and genai is None:
            LOGGER.warning("Gemini API key was provided, but google-genai is not installed.")
    else:
        _gemini_client = genai.Client(api_key=gemini_key)


async def process_bundle(bundle: "NewsBundle", language: str) -> AiResult | None:
    if not any((bundle.macro_news, bundle.stock_news, bundle.crypto_news)):
        return None

    prompt = _build_prompt(bundle, language=language)
    response_schema = _build_response_schema(bundle, language=language)

    if _gemini_client is not None:
        result = await _process_with_provider(
            provider_name="Gemini",
            response_loader=lambda: asyncio.to_thread(_generate_gemini_response, prompt, response_schema),
            bundle=bundle,
            language=language,
        )
        if result is not None:
            return result

    if _groq_api_key:
        result = await _process_with_provider(
            provider_name="Groq",
            response_loader=lambda: _generate_groq_response(prompt, response_schema),
            bundle=bundle,
            language=language,
        )
        if result is not None:
            return result

    return None


async def _process_with_provider(
    *,
    provider_name: str,
    response_loader: Any,
    bundle: "NewsBundle",
    language: str,
) -> AiResult | None:
    try:
        response_text = await response_loader()
        payload = _normalize_payload(json.loads(response_text), language=language)
        overview, selections = _validate_payload(payload, bundle=bundle, language=language)
    except json.JSONDecodeError as exc:
        LOGGER.warning("%s response was not valid JSON: %s", provider_name, exc)
        return None
    except ValueError as exc:
        LOGGER.warning("%s response validation failed: %s", provider_name, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("%s processing failed: %s", provider_name, exc)
        return None

    return AiResult(
        overview=overview,
        macro_news=_copy_selected_items(bundle.macro_news, selections["macro"], language=language),
        stock_news=_copy_selected_items(bundle.stock_news, selections["stock"], language=language),
        crypto_news=_copy_selected_items(bundle.crypto_news, selections["crypto"], language=language),
    )


def _generate_gemini_response(prompt: str, response_schema: dict[str, Any]) -> str:
    if _gemini_client is None:
        raise RuntimeError("Gemini client is not configured.")

    response = _gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=prompt,
        config={
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "response_json_schema": response_schema,
        },
    )

    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini returned an empty response body.")
    return text


async def _generate_groq_response(prompt: str, response_schema: dict[str, Any]) -> str:
    if not _groq_api_key:
        raise RuntimeError("Groq API key is not configured.")

    request_json = {
        "model": GROQ_MODEL_NAME,
        "temperature": 0,
        "include_reasoning": False,
        "reasoning_effort": "low",
        "max_completion_tokens": 4096,
        "messages": [
            {
                "role": "system",
                "content": "You are a precise financial news editor. Return a valid JSON object only, with no prose or markdown.",
            },
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\n"
                    "Return a JSON object with exactly these top-level keys: "
                    f"{', '.join(_ROOT_KEYS)}."
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {_groq_api_key}",
                "Content-Type": "application/json",
            },
            json=request_json,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()
            if detail:
                raise RuntimeError(f"Groq request failed: {detail}") from exc
            raise

    payload = response.json()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Groq returned no choices.")

    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    if isinstance(content, list):
        joined = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if joined:
            return joined

    raise ValueError("Groq returned an empty response body.")


def _build_prompt(bundle: "NewsBundle", *, language: str) -> str:
    output_language = "Slovak" if language == "sk" else "English"
    translation_instruction = (
        "For every selected item, include a `title_translated` value written in natural Slovak."
        if language == "sk"
        else "Do not include `title_translated` for any item."
    )

    return "\n".join(
        [
            "You are curating a concise financial market digest.",
            f"Write the overview in {output_language}.",
            "Tasks:",
            "1. Deduplicate semantically within each category.",
            "2. Keep only hard-news items with concrete market relevance.",
            "3. Exclude personal-finance advice, tax tips, listicles, watch-live teasers, TV segment plugs, trading setups, generic biggest-movers roundups, and shopping-style analyst recommendations unless they report a clear market-moving event.",
            "4. Rank the remaining items by market significance, not click appeal.",
            "5. Return at most 4 macro items, 4 stock items, and 2 crypto items.",
            "6. Write a 2-3 sentence overview that reflects the selected items.",
            f"7. {translation_instruction}",
            "Rules:",
            "- Use only the provided 0-based indices.",
            "- Preserve category boundaries; do not move an item to another category.",
            "- If a category has nothing worth keeping, return an empty list.",
            "- Return JSON only.",
            f"- Valid macro indices: {_format_index_range(bundle.macro_news)}.",
            f"- Valid stock indices: {_format_index_range(bundle.stock_news)}.",
            f"- Valid crypto indices: {_format_index_range(bundle.crypto_news)}.",
            "",
            "Macro candidates:",
            _format_category_items(bundle.macro_news),
            "",
            "Stock candidates:",
            _format_category_items(bundle.stock_news),
            "",
            "Crypto candidates:",
            _format_category_items(bundle.crypto_news),
        ]
    )


def _format_category_items(items: list[NewsItem]) -> str:
    if not items:
        return "(none)"

    return "\n".join(
        f"{index}. [{item.source}] {item.title}{_format_summary_suffix(item.summary)}"
        for index, item in enumerate(items)
    )


def _format_summary_suffix(summary: str) -> str:
    cleaned = summary.strip()
    if not cleaned:
        return ""
    compact = cleaned.replace("\n", " ")
    if len(compact) > 180:
        compact = compact[:177].rstrip() + "..."
    return f" | Summary: {compact}"


def _format_index_range(items: list[NewsItem]) -> str:
    if not items:
        return "none available"
    return f"0 to {len(items) - 1}"


def _normalize_payload(payload: Any, *, language: str) -> Any:
    if not isinstance(payload, dict):
        return payload

    normalized: dict[str, Any] = {key: payload.get(key) for key in _ROOT_KEYS}
    allowed_item_keys = {"index"} if language == "en" else {"index", "title_translated"}

    for category in ("macro", "stock", "crypto"):
        raw_items = normalized.get(category)
        if not isinstance(raw_items, list):
            continue

        cleaned_items: list[Any] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                cleaned_items.append(raw)
                continue
            cleaned_items.append({key: raw.get(key) for key in allowed_item_keys if key in raw})
        normalized[category] = cleaned_items

    return normalized


def _build_response_schema(bundle: "NewsBundle", *, language: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "overview": {
                "type": "string",
                "description": "A concise 2-3 sentence market overview in the requested language.",
            },
            "macro": _build_category_schema(
                max_items=MAX_ITEMS["macro"],
                candidate_count=len(bundle.macro_news),
                language=language,
                description="Selected macro/economics hard-news items in ranked order.",
            ),
            "stock": _build_category_schema(
                max_items=MAX_ITEMS["stock"],
                candidate_count=len(bundle.stock_news),
                language=language,
                description="Selected stock-market hard-news items in ranked order.",
            ),
            "crypto": _build_category_schema(
                max_items=MAX_ITEMS["crypto"],
                candidate_count=len(bundle.crypto_news),
                language=language,
                description="Selected crypto hard-news items in ranked order.",
            ),
        },
        "required": list(_ROOT_KEYS),
        "additionalProperties": False,
    }


def _build_category_schema(
    *,
    max_items: int,
    candidate_count: int,
    language: str,
    description: str,
) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _build_item_schema(candidate_count=candidate_count, language=language),
        "maxItems": min(max_items, candidate_count),
        "description": description,
    }


def _build_item_schema(*, candidate_count: int, language: str) -> dict[str, Any]:
    if candidate_count <= 0:
        enum_values = [0]
    else:
        enum_values = list(range(candidate_count))

    properties: dict[str, Any] = {
        "index": {
            "type": "integer",
            "enum": enum_values,
            "description": "0-based index into the original category list.",
        },
    }
    required = ["index"]

    if language != "en":
        properties["title_translated"] = {
            "type": "string",
            "description": "Translated headline, only when the requested digest language is Slovak.",
        }
        required.append("title_translated")

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _validate_payload(
    payload: Any,
    *,
    bundle: "NewsBundle",
    language: str,
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    if not isinstance(payload, dict):
        raise ValueError("Root JSON value must be an object.")

    if set(payload) != set(_ROOT_KEYS):
        raise ValueError("Root JSON keys must be exactly overview, macro, stock, and crypto.")

    overview = payload.get("overview")
    if not isinstance(overview, str):
        raise ValueError("overview must be a string.")

    selections = {
        "macro": _validate_category_items(payload.get("macro"), bundle.macro_news, MAX_ITEMS["macro"], language=language),
        "stock": _validate_category_items(payload.get("stock"), bundle.stock_news, MAX_ITEMS["stock"], language=language),
        "crypto": _validate_category_items(payload.get("crypto"), bundle.crypto_news, MAX_ITEMS["crypto"], language=language),
    }
    return overview.strip(), selections


def _validate_category_items(
    value: Any,
    source_items: list[NewsItem],
    limit: int,
    *,
    language: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Category output must be a list.")

    trimmed = value[:limit]
    validated: list[dict[str, Any]] = []
    seen_indices: set[int] = set()

    for raw in trimmed:
        if not isinstance(raw, dict):
            raise ValueError("Each selected item must be an object.")

        allowed_keys = {"index"} if language == "en" else {"index", "title_translated"}
        if set(raw) - allowed_keys:
            raise ValueError("Selected item contains unexpected keys.")

        index = raw.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("Each selected item index must be an integer.")
        if not 0 <= index < len(source_items):
            raise ValueError("Selected item index is out of bounds.")
        if index in seen_indices:
            raise ValueError("Duplicate indices are not allowed within a category.")

        selection: dict[str, Any] = {"index": index}
        if language != "en":
            translated = raw.get("title_translated")
            if not isinstance(translated, str) or not translated.strip():
                raise ValueError("Slovak responses must include a non-empty title_translated string.")
            selection["title_translated"] = translated.strip()

        seen_indices.add(index)
        validated.append(selection)

    return validated


def _copy_selected_items(
    source_items: list[NewsItem],
    selections: list[dict[str, Any]],
    *,
    language: str,
) -> list[NewsItem]:
    selected: list[NewsItem] = []
    for selection in selections:
        item = source_items[selection["index"]]
        if language != "en":
            selected.append(replace(item, title=selection["title_translated"]))
        else:
            selected.append(replace(item))
    return selected
