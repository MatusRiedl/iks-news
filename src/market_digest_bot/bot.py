from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import ai_processor
from .config import AppConfig
from .digest import build_digest_messages
from .models import GuildSettings
from .news_fetcher import build_live_digest
from .storage import SettingsStore

LOGGER = logging.getLogger("market_digest_bot")


class MarketDigestBot(commands.Bot):
    def __init__(self, config: AppConfig) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.store = SettingsStore(
            path=config.data_dir / "guild_settings.json",
            default_timezone=config.default_timezone,
            default_post_time=config.default_post_time,
            default_language=config.default_language,
        )
        self._posting_guild_ids: set[int] = set()

    async def setup_hook(self) -> None:
        if self.config.gemini_api_key or self.config.groq_api_key:
            ai_processor.configure(
                gemini_api_key=self.config.gemini_api_key,
                groq_api_key=self.config.groq_api_key,
            )
        await self.store.load()
        await self.add_cog(DigestAdminCog(self))

        synced = await self.tree.sync()
        LOGGER.info("Synced %s global slash commands", len(synced))

        if self.config.test_guild_id:
            guild = discord.Object(id=self.config.test_guild_id)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            LOGGER.info(
                "Synced %s slash commands to test guild %s for faster development updates",
                len(guild_synced),
                self.config.test_guild_id,
            )

        self.scheduler.start()

    async def close(self) -> None:
        if self.scheduler.is_running():
            self.scheduler.cancel()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info(
            "Logged in as %s (%s) across %s guild(s)",
            self.user,
            getattr(self.user, "id", "unknown"),
            len(self.guilds),
        )

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        now_utc = datetime.now(UTC)
        settings = await self.store.list_all()

        for setting in settings:
            if setting.channel_id is None:
                continue
            if setting.guild_id in self._posting_guild_ids:
                continue

            try:
                timezone = ZoneInfo(setting.timezone)
                scheduled_hour, scheduled_minute = _parse_time_24h(setting.post_time)
            except ValueError:
                LOGGER.warning("Skipping guild %s because its schedule is invalid.", setting.guild_id)
                continue
            except ZoneInfoNotFoundError:
                LOGGER.warning("Skipping guild %s because timezone %s is invalid.", setting.guild_id, setting.timezone)
                continue

            local_now = now_utc.astimezone(timezone)
            scheduled_dt = local_now.replace(
                hour=scheduled_hour,
                minute=scheduled_minute,
                second=0,
                microsecond=0,
            )

            if local_now < scheduled_dt:
                continue
            if setting.last_posted_on == local_now.date().isoformat():
                continue

            self._posting_guild_ids.add(setting.guild_id)
            try:
                await self.post_digest_for_setting(setting, target_timezone=timezone)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Scheduled digest run failed for guild %s", setting.guild_id)
            finally:
                self._posting_guild_ids.discard(setting.guild_id)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()

    async def post_digest_for_setting(
        self,
        setting: GuildSettings,
        *,
        target_timezone: ZoneInfo | None = None,
        target_channel: discord.abc.Messageable | None = None,
        record_post: bool = True,
    ) -> None:
        timezone = target_timezone or ZoneInfo(setting.timezone)
        target_date = datetime.now(UTC).astimezone(timezone).date()

        channel = target_channel
        if channel is None:
            channel = self.get_channel(setting.channel_id or 0)
            if channel is None and setting.channel_id is not None:
                channel = await self.fetch_channel(setting.channel_id)

        if channel is None:
            raise RuntimeError("Could not resolve the configured Discord channel.")

        digest = await build_live_digest(
            target_date=target_date,
            language=setting.language,
            target_timezone=timezone,
        )

        messages = build_digest_messages(digest, timezone.key, language=setting.language)
        for message in messages:
            await channel.send(message)

        if record_post:
            await self.store.mark_posted(setting.guild_id, target_date.isoformat())


class DigestAdminCog(commands.Cog):
    def __init__(self, bot: MarketDigestBot) -> None:
        self.bot = bot

    @app_commands.command(name="news_setup", description="Configure the daily news post for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel that should receive the report.")
    @app_commands.describe(time_24h="Time in 24-hour format, for example 08:30 or 17:45.")
    @app_commands.describe(timezone="IANA timezone, for example America/New_York.")
    @app_commands.describe(language="Digest language.")
    @app_commands.choices(language=[
        app_commands.Choice(name="Sloven\u010dina", value="sk"),
        app_commands.Choice(name="English", value="en"),
    ])
    async def news_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        time_24h: str,
        timezone: str,
        language: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild_id is not None

        try:
            _parse_time_24h(time_24h)
            ZoneInfo(timezone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                "Timezone not found. Use a value like `America/New_York` or `Europe/Budapest`.",
                ephemeral=True,
            )
            return

        current = await self.bot.store.get(interaction.guild_id)
        setting = GuildSettings(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            timezone=timezone,
            post_time=time_24h,
            last_posted_on=current.last_posted_on,
            language=language.value if language is not None else current.language,
        )
        await self.bot.store.upsert(setting)

        await interaction.response.send_message(
            f"Daily news post enabled for {channel.mention} at `{time_24h}` in `{timezone}` with language `{_format_language_label(setting.language)}`.",
            ephemeral=True,
        )

    @app_commands.command(name="news_status", description="Show the current news schedule for this server.")
    @app_commands.guild_only()
    async def news_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        setting = await self.bot.store.get(interaction.guild_id)

        if setting.channel_id is None:
            await interaction.response.send_message(
                "Daily news posting is currently disabled for this server. Use `/news_setup` to enable it.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(setting.channel_id) if interaction.guild else None
        channel_label = channel.mention if isinstance(channel, discord.TextChannel) else f"`{setting.channel_id}`"
        last_posted = setting.last_posted_on or "never"

        await interaction.response.send_message(
            f"Channel: {channel_label}\nTime: `{setting.post_time}`\nTimezone: `{setting.timezone}`\nLanguage: `{_format_language_label(setting.language)}`\nLast posted: `{last_posted}`",
            ephemeral=True,
        )

    @app_commands.command(name="news_now", description="Post today's news immediately.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def news_now(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)

        setting = await self.bot.store.get(interaction.guild_id)
        target_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None

        if setting.channel_id is None:
            if target_channel is None:
                await interaction.followup.send(
                    "Run `/news_setup` first, or use this command in a standard text channel.",
                    ephemeral=True,
                )
                return
            setting.channel_id = target_channel.id

        try:
            await self.bot.post_digest_for_setting(setting, target_channel=target_channel, record_post=False)
        except discord.Forbidden:
            channel_label = target_channel.mention if target_channel is not None else "this channel"
            await interaction.followup.send(
                f"I can't post in {channel_label}. Give the bot `View Channel`, `Send Messages`, and `Read Message History` there, then try `/news_now` again.",
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Manual digest run failed")
            await interaction.followup.send(f"Digest run failed: `{exc}`", ephemeral=True)
            return

        await interaction.followup.send("News posted.", ephemeral=True)

    @app_commands.command(name="news_disable", description="Disable the daily news post for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def news_disable(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await self.bot.store.disable(interaction.guild_id)
        await interaction.response.send_message("Daily news posting disabled for this server.", ephemeral=True)

    @news_setup.error
    @news_status.error
    @news_now.error
    @news_disable.error
    async def admin_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await _handle_app_command_error(interaction, error)


async def _handle_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You need `Manage Server` permission to use that command."
    elif isinstance(error, app_commands.errors.NoPrivateMessage):
        message = "This command can only be used in a server."
    else:
        message = f"Command failed: `{error}`"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _parse_time_24h(value: str) -> tuple[int, int]:
    text = value.strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("Time must use `HH:MM` 24-hour format, for example `08:30`.")

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("Time must use `HH:MM` 24-hour format, for example `08:30`.") from exc

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Time must use a valid 24-hour clock, for example `08:30` or `17:45`.")
    return hour, minute


def _format_language_label(language: str) -> str:
    if language == "sk":
        return "Slovak"
    return "English"


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = AppConfig.from_env()
    bot = MarketDigestBot(config)
    bot.run(config.discord_token, log_handler=None)
