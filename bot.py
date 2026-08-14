import os
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: your server ID, for instant slash-command syncing

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("madboys-bot")

intents = discord.Intents.default()
intents.message_content = True  # needed if you add prefix commands later

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")
    else:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} global command(s)")


@bot.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms")


# --- placeholder for future features ---
# /clubstats, /lastgame  -> EA Pro Clubs data (polled + cached, see ea_client.py)
# /lineup set            -> formation + position select menus
# /rotation check        -> position rotation suggestions
# /build <position>      -> curated build/tips embeds


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
