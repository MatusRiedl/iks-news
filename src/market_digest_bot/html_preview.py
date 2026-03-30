from __future__ import annotations

import asyncio
import html
import os
import re
import webbrowser
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ai_processor
from .config import ConfigError, normalize_language
from .digest import build_digest_messages
from .news_fetcher import build_live_digest

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional for sample-only preview
    def load_dotenv() -> bool:
        return False


def main() -> None:
    load_dotenv()

    timezone_name = (os.getenv("DEFAULT_TIMEZONE") or "America/New_York").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    print("Loading preview configuration...", flush=True)
    try:
        language = normalize_language(os.getenv("DEFAULT_LANGUAGE", "sk"), variable_name="DEFAULT_LANGUAGE")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    if gemini_api_key or groq_api_key:
        ai_processor.configure(
            gemini_api_key=gemini_api_key or None,
            groq_api_key=groq_api_key or None,
        )
        if gemini_api_key and groq_api_key:
            print("Gemini is enabled for the preview, with Groq available as AI fallback.", flush=True)
        elif gemini_api_key:
            print("Gemini is enabled for the preview.", flush=True)
        else:
            print("Groq is enabled for the preview as the AI provider.", flush=True)
    else:
        print("AI providers are disabled for the preview. Using fallback summary logic.", flush=True)

    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(
            f"Invalid timezone `{timezone_name}`. Try a value like America/New_York."
        ) from exc

    target_date = datetime.now(UTC).astimezone(timezone).date()
    print(
        f"Fetching live news for {target_date.isoformat()} in {timezone.key}. This can take a little while...",
        flush=True,
    )

    digest = asyncio.run(_load_digest(target_date=target_date, language=language, target_timezone=timezone))
    print("Rendering preview HTML...", flush=True)
    messages = build_digest_messages(digest, timezone.key, language=language)

    output_path = Path("data") / "today_digest_preview.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_html(messages=messages, target_date=target_date, timezone_name=timezone.key, language=language),
        encoding="utf-8",
    )

    webbrowser.open(output_path.resolve().as_uri())
    print(f"Opened {output_path.resolve()}")


async def _load_digest(*, target_date: date, language: str, target_timezone: ZoneInfo):
    return await build_live_digest(target_date=target_date, language=language, target_timezone=target_timezone)


def _render_html(*, messages: list[str], target_date: date, timezone_name: str, language: str) -> str:
    cards = "\n".join(_render_message_card(message, index) for index, message in enumerate(messages, start=1))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="{html.escape(language)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Today's Digest Preview</title>
  <style>
    :root {{
      --bg: #0f1220;
      --panel: #1a1f33;
      --panel-alt: #202741;
      --border: #313a5d;
      --text: #edf2ff;
      --muted: #a9b3d6;
      --accent: #73e0a9;
      --code-bg: #111627;
      --shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: "Segoe UI", "Trebuchet MS", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(115, 224, 169, 0.14), transparent 30%),
        radial-gradient(circle at top left, rgba(84, 125, 255, 0.20), transparent 32%),
        linear-gradient(180deg, #0b0e19, #13182a 55%, #0e1120);
      min-height: 100vh;
    }}

    .shell {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 36px 20px 48px;
    }}

    .hero {{
      background: linear-gradient(160deg, rgba(32, 39, 65, 0.96), rgba(18, 24, 41, 0.96));
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 28px;
      box-shadow: var(--shadow);
    }}

    .eyebrow {{
      display: inline-block;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: #08111b;
      background: var(--accent);
      padding: 8px 12px;
      border-radius: 999px;
      font-weight: 700;
    }}

    h1 {{
      margin: 16px 0 10px;
      font-size: clamp(28px, 4vw, 46px);
      line-height: 1;
      letter-spacing: -0.04em;
    }}

    .meta {{
      color: var(--muted);
      font-size: 15px;
      line-height: 1.6;
      max-width: 760px;
    }}

    .grid {{
      display: grid;
      gap: 18px;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      margin-top: 24px;
    }}

    .card {{
      background: linear-gradient(180deg, rgba(26, 31, 51, 0.95), rgba(19, 24, 42, 0.95));
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 22px;
      box-shadow: var(--shadow);
    }}

    .card-tag {{
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}

    .card h2 {{
      margin: 34px 0 14px;
      font-size: 24px;
      line-height: 1.15;
    }}

    .card h2:first-of-type {{
      margin-top: 0;
    }}

    .card p {{
      margin: 0 0 12px;
      color: var(--muted);
      line-height: 1.6;
    }}

    .card ul {{
      margin: 0;
      padding-left: 20px;
    }}

    .card li {{
      margin: 0 0 10px;
      line-height: 1.55;
    }}

    code {{
      font-family: "Cascadia Code", "Consolas", monospace;
      background: var(--code-bg);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 2px 6px;
      font-size: 0.95em;
      color: #c9f5ff;
    }}

    a {{
      color: #9bd5ff;
      text-decoration: none;
    }}

    a:hover {{
      text-decoration: underline;
    }}

    .footer {{
      margin-top: 18px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Live News</div>
      <h1>Iks News</h1>
      <div class="meta">
        Preview date: {html.escape(target_date.isoformat())} in {html.escape(timezone_name)}.<br>
        Generated from the current Python formatter. Refreshed at {html.escape(generated_at)}.
      </div>
    </section>
    <section class="grid">
      {cards}
    </section>
    <div class="footer">
      This file is regenerated each time you open the preview launcher.
    </div>
  </main>
</body>
</html>
"""


def _render_message_card(message: str, index: int) -> str:
    lines = [line.rstrip() for line in message.splitlines()]
    body: list[str] = [f'<article class="card"><div class="card-tag">Discord message {index}</div>']
    in_list = False
    title_used = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                body.append("</ul>")
                in_list = False
            continue

        if stripped.startswith("**") and stripped.endswith("**"):
            if in_list:
                body.append("</ul>")
                in_list = False
            heading = _inline_format(stripped[2:-2])
            tag = "h2" if title_used else "h2"
            body.append(f"<{tag}>{heading}</{tag}>")
            title_used = True
            continue

        if stripped.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline_format(stripped[2:])}</li>")
            continue

        if in_list:
            body.append("</ul>")
            in_list = False

        body.append(f"<p>{_inline_format(stripped)}</p>")

    if in_list:
        body.append("</ul>")

    body.append("</article>")
    return "\n".join(body)


def _inline_format(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(https://[^\s<]+)", r'<a href="\1" target="_blank" rel="noreferrer">\1</a>', escaped)
    return escaped


if __name__ == "__main__":
    main()
