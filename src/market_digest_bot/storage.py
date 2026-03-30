from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .models import GuildSettings


class SettingsStore:
    def __init__(
        self,
        path: Path,
        default_timezone: str,
        default_post_time: str,
        default_language: str,
    ) -> None:
        self.path = path
        self.default_timezone = default_timezone
        self.default_post_time = default_post_time
        self.default_language = default_language
        self._lock = asyncio.Lock()
        self._settings: dict[int, GuildSettings] = {}

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                self._settings = {}
                return

            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            if not raw.strip():
                self._settings = {}
                return

            data = json.loads(raw)
            loaded: dict[int, GuildSettings] = {}
            for item in data:
                setting = GuildSettings(
                    guild_id=int(item["guild_id"]),
                    channel_id=item.get("channel_id"),
                    timezone=item.get("timezone", self.default_timezone),
                    post_time=item.get("post_time", self.default_post_time),
                    last_posted_on=item.get("last_posted_on"),
                    language=item.get("language", "en"),
                )
                loaded[setting.guild_id] = setting
            self._settings = loaded

    async def _save_locked(self) -> None:
        payload = [asdict(setting) for setting in sorted(self._settings.values(), key=lambda item: item.guild_id)]
        text = json.dumps(payload, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self.path.write_text, text, encoding="utf-8")

    async def get(self, guild_id: int) -> GuildSettings:
        async with self._lock:
            existing = self._settings.get(guild_id)
            if existing is not None:
                return GuildSettings(**asdict(existing))

            return GuildSettings(
                guild_id=guild_id,
                channel_id=None,
                timezone=self.default_timezone,
                post_time=self.default_post_time,
                last_posted_on=None,
                language=self.default_language,
            )

    async def list_all(self) -> list[GuildSettings]:
        async with self._lock:
            return [GuildSettings(**asdict(setting)) for setting in self._settings.values()]

    async def upsert(self, setting: GuildSettings) -> GuildSettings:
        async with self._lock:
            self._settings[setting.guild_id] = GuildSettings(**asdict(setting))
            await self._save_locked()
            return GuildSettings(**asdict(setting))

    async def disable(self, guild_id: int) -> GuildSettings:
        async with self._lock:
            existing = self._settings.get(guild_id)
            if existing is None:
                existing = GuildSettings(
                    guild_id=guild_id,
                    channel_id=None,
                    timezone=self.default_timezone,
                    post_time=self.default_post_time,
                    language=self.default_language,
                )
            else:
                existing = GuildSettings(**asdict(existing))

            existing.channel_id = None
            self._settings[guild_id] = existing
            await self._save_locked()
            return GuildSettings(**asdict(existing))

    async def mark_posted(self, guild_id: int, posted_on: str) -> None:
        async with self._lock:
            current = self._settings.get(guild_id)
            if current is None:
                return

            current = GuildSettings(**asdict(current))
            current.last_posted_on = posted_on
            self._settings[guild_id] = current
            await self._save_locked()
