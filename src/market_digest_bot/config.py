from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(slots=True)
class AppConfig:
    discord_token: str
    default_timezone: str
    default_post_time: str
    default_language: str
    data_dir: Path
    test_guild_id: int | None
    gemini_api_key: str | None
    groq_api_key: str | None

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()

        discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        default_timezone = os.getenv("DEFAULT_TIMEZONE", "America/New_York").strip()
        default_post_time = os.getenv("DEFAULT_POST_TIME", "08:30").strip()
        default_language = normalize_language(os.getenv("DEFAULT_LANGUAGE", "sk"), variable_name="DEFAULT_LANGUAGE")
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        test_guild_raw = os.getenv("DISCORD_TEST_GUILD_ID", "").strip()
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
        groq_api_key = os.getenv("GROQ_API_KEY", "").strip() or None

        missing = [
            name
            for name, value in (
                ("DISCORD_TOKEN", discord_token),
            )
            if not value
        ]
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(f"Missing required environment variables: {joined}")

        _validate_time_24h(default_post_time)

        try:
            ZoneInfo(default_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                "DEFAULT_TIMEZONE must be a valid IANA timezone such as America/New_York."
            ) from exc

        test_guild_id: int | None = None
        if test_guild_raw:
            try:
                test_guild_id = int(test_guild_raw)
            except ValueError as exc:
                raise ConfigError("DISCORD_TEST_GUILD_ID must be an integer.") from exc

        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            discord_token=discord_token,
            default_timezone=default_timezone,
            default_post_time=default_post_time,
            default_language=default_language,
            data_dir=data_dir,
            test_guild_id=test_guild_id,
            gemini_api_key=gemini_api_key,
            groq_api_key=groq_api_key,
        )


def _validate_time_24h(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ConfigError("DEFAULT_POST_TIME must use HH:MM 24-hour format, for example 08:30.")

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ConfigError("DEFAULT_POST_TIME must use HH:MM 24-hour format, for example 08:30.") from exc

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError("DEFAULT_POST_TIME must be a valid 24-hour time, for example 08:30 or 17:45.")


def normalize_language(value: str, *, variable_name: str = "language") -> str:
    language = value.strip().lower()
    if language not in {"en", "sk"}:
        raise ConfigError(f"{variable_name} must be either `en` or `sk`.")
    return language
