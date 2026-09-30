import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # must run before importing config, which reads env vars

import db  # noqa: E402
from config import GUILD_ID, PLATFORM  # noqa: E402
from ea_client import EAClient  # noqa: E402

TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("madboys-bot")

# Order matters only in that every cog expects bot.ea to exist (set in setup_hook).
COGS = [
    "cogs.link",
    "cogs.rotation",
    "cogs.stats",
    "cogs.lineup",
    "cogs.sessions",
    "cogs.matchday",
    "cogs.hub",
    "cogs.misc",
]


class MadBoysBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # member cache, so names resolve without extra API calls
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.ea = EAClient(platform=PLATFORM)

    async def setup_hook(self):
        # Runs once at startup (on_ready can fire again on every reconnect,
        # which used to re-sync slash commands each time and risk rate limits).
        db.init_all()
        for ext in COGS:
            await self.load_extension(ext)
            log.info(f"Loaded {ext}")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # Clear any old *global* copies so commands don't show up twice.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} global command(s) (can take up to an hour to appear)")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (id: {self.user.id}) in {len(self.guilds)} server(s)")

    async def close(self):
        await self.ea.close()
        await super().close()


bot = MadBoysBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """One friendly error message for every command, instead of 'The application did not respond'."""
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down — try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You can't use that command here."
    else:
        log.exception(f"Error in /{interaction.command.qualified_name if interaction.command else '?'}",
                      exc_info=error)
        msg = "Something went wrong running that command. It's been logged."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
