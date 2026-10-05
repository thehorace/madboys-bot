"""Monitor official FC 27 update articles, with durable per-server deduplication."""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import survive

from cogs.operations import report_health
from cogs.usage import can_view
from config import CLUB_COLOUR, GUILD_ID
from db import connect, get_setting, set_setting

SOURCE = "https://www.ea.com/games/ea-sports-fc/fc-27/news"
MAX_UPDATE_AGE = 24 * 3600
MAX_EXCERPT_CHARS = 1500
log = logging.getLogger("madboys-bot.patchnotes")


def utc_now():
    return datetime.now(timezone.utc)


class NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def page_data(html: str) -> dict:
    parser = NextDataParser()
    parser.feed(html)
    try:
        props = json.loads("".join(parser.parts))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("EA news page format changed or an access-block page was returned") from exc
    if not isinstance(props, dict):
        raise ValueError("EA page data is invalid")
    return props


def update_articles(html: str) -> list[dict]:
    data = page_data(html).get("initialNewsData", {})
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("EA's FC 27 news listing could not be read")
    items = list(items)
    featured = data.get("featured")
    if isinstance(featured, dict):
        items.append(featured)
    elif isinstance(featured, list):
        items.extend(featured)
    if not items:
        raise ValueError("EA's FC 27 news listing could not be read")
    found = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        title, slug = str(item.get("title", "")), str(item.get("slug", ""))
        games = [link.get("slug") for link in item.get("linkedTo", []) if link.get("type") == "Game"]
        if games and "fc-27" not in games:
            continue
        if re.search(r"\bFC\s*(?:25|26|28)\b", title, re.I):
            continue
        if not relevant_news(item):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
            continue
        try:
            published = datetime.fromisoformat(item["publishingDate"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise ValueError("EA update publication date is missing or invalid")
        if published > utc_now():
            continue
        article = dict(item)
        article["url"] = SOURCE + "/" + slug
        article["published_ts"] = int(published.timestamp())
        found[article["url"]] = article
    return sorted(found.values(), key=lambda a: (a["published_ts"], a["url"]))


def relevant_news(item: dict) -> bool:
    title = str(item.get("title", ""))
    # Actual patch notes may mention edition rewards as bugs being fixed.
    if re.search(r"title[ -]*update|patch[ -]*notes", title, re.I):
        return True
    tags = []
    for tag in item.get("tags") or []:
        if isinstance(tag, dict):
            tags.extend(str(tag.get(key, "")) for key in ("name", "label", "slug"))
        elif isinstance(tag, str):
            tags.append(tag)
    text = " ".join([title, str(item.get("slug", "")), str(item.get("summary", "")), *tags])
    words = text.replace("-", " ")
    if re.search(r"\b(?:ultimate|standard|deluxe|icon) edition\b|\bpre ?order\b|"
                 r"\b(?:launch|purchase|edition|loyalty) (?:rewards|bonuses|bonus)\b|"
                 r"\b(?:buy now|fc points|icon pack|discount|sale offer)\b", words, re.I):
        return False
    if re.search(r"\bhow to\b|\bgetting started\b|\bget started\b|\bbeginners?\b|\bfirst steps\b", words, re.I):
        return False
    modes = bool(re.search(r"\bclubs\b|\bgrounds\b", text, re.I))
    if not modes and re.search(r"\bFUT\b|\bUltimate Team\b|\bCareer Mode\b", title, re.I):
        return False
    changes = bool(re.search(r"\bupdates?\b|\bfeatures?\b|\bdeveloper\b|\bimprovements?\b|"
                             r"\bchanges?\b|\bfix(?:es)?\b|\bintroducing\b|\brollout\b|\brelease\b", words, re.I))
    return (modes and changes) or bool(re.search(r"gameplay.*update|launch update", title, re.I))


def article_details(html: str, expected_slug: str) -> dict:
    article = page_data(html).get("articleDetailsFallback")
    if not isinstance(article, dict) or article.get("slug") != expected_slug or not str(article.get("body", "")).strip():
        raise ValueError("EA update details could not be read")
    return article


def article_excerpt(article: dict) -> str:
    body = str(article["body"])
    # Do not let decorative images, table-of-contents links and their long URLs
    # consume the limited Discord excerpt ahead of the actual changes.
    body = re.split(r"Want to provide us|Thanks to those|Throughout the course", body, maxsplit=1, flags=re.I)[0]
    body = re.sub(r"!\[[^\]]*\]\([^\n]*\)", "", body)
    body = re.sub(r"\*\*Table of Contents\*\*.*?(?=^#{1,6}\s|\Z)", "", body, flags=re.S | re.M)
    body = re.sub(r"\{#[^}]+\}", "", body)
    body = re.sub(r"</?u>|<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    headings = list(re.finditer(r"^(?:#{1,6}\s+.+|\*\*[^*\n]+\*\*)\s*$", body, re.M))
    if headings:
        intro = body[:headings[0].start()].strip()
        sections = [body[h.start():headings[i + 1].start() if i + 1 < len(headings) else len(body)].strip()
                    for i, h in enumerate(headings)]
        def priority(section):
            heading = section.splitlines()[0]
            if re.search(r"\bClubs\b", heading, re.I):
                return 0
            if re.search(r"quality.of.life|live issues|\bGrounds\b", heading, re.I):
                return 1
            if re.search(r"gameplay|matchmaking|menu performance|win streaks", heading, re.I):
                return 2
            return 3
        sections.sort(key=priority)
        # The official intro remains available below the relevant changes.
        body = "\n\n".join(sections + ([intro] if intro else []))
        if len(body) > MAX_EXCERPT_CHARS:
            highlights = []
            for section in sections[:5]:
                lines = section.splitlines()
                heading = re.sub(r"^[#\s]+|\*\*", "", lines[0]).strip()
                content = "\n".join(lines[1:]).strip()
                bullets = re.findall(r"^[-*] .+", content, re.M)
                if bullets:
                    snippet = " ".join(bullets[:3])
                else:
                    paragraphs = [p.strip().replace("\n", " ") for p in content.split("\n\n") if p.strip()]
                    snippet = next((p for p in paragraphs if re.search(
                        r"explor|investigat|looking|testing|deployed|reintroduced|introduced|added|improv|working|plan", p, re.I)),
                        paragraphs[0] if paragraphs else "")
                if snippet:
                    if len(snippet) > 220:
                        snippet = snippet[:220].rsplit(" ", 1)[0] + "…"
                    highlights.append(f"**{heading}**\n{snippet}")
            body = "\n\n".join(highlights) or body
    body = discord.utils.escape_mentions(body)
    excerpt = body[:MAX_EXCERPT_CHARS]
    if len(body) > MAX_EXCERPT_CHARS:
        excerpt = excerpt.rsplit("\n", 1)[0] + "\n\n*More changes in the full notes below.*"
    return excerpt


def patch_embed(article: dict) -> discord.Embed:
    excerpt = article_excerpt(article)
    embed = discord.Embed(title=str(article["title"])[:256], url=article["url"],
                          description=excerpt + f"\n\n[Read the full official EA article]({article['url']})",
                          colour=CLUB_COLOUR)
    embed.set_footer(text="Official EA FC 27 news • excerpt from EA's article • no @everyone ping")
    embed.timestamp = datetime.fromtimestamp(article["published_ts"], timezone.utc)
    return embed


def patch_settings(gid: str) -> dict:
    return {"enabled": get_setting(gid, "patchnotes:enabled") != "0",
            "channel_id": get_setting(gid, "patchnotes:channel") or os.getenv("PATCHNOTES_CHANNEL_ID", ""),
            "last_checked": get_setting(gid, "patchnotes:last_checked") or "Not yet"}


def init_patchnotes():
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS patchnotes_seen (guild_id TEXT NOT NULL, url TEXT NOT NULL, "
                     "title TEXT NOT NULL, published_ts INTEGER NOT NULL, message_id TEXT, PRIMARY KEY(guild_id,url))")


class PatchNotesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        init_patchnotes()
        self.ticker.start()

    def cog_unload(self):
        self.ticker.cancel()

    async def fetch(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "www.ea.com" or not parts.path.startswith("/games/ea-sports-fc/fc-27/news"):
            raise ValueError("Only the official EA FC 27 news source is allowed")
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "MadBoysBot/1.0 (FC27 update monitor)"}) as session:
            async with session.get(url, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise ValueError("EA returned an unexpected response")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError("EA page exceeded the expected size")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8")

    def channel(self, guild):
        cid = patch_settings(str(guild.id))["channel_id"]
        if cid:
            return guild.get_channel(int(cid))
        choices = [c for c in guild.text_channels if c.name.lower() == "general"]
        return choices[0] if len(choices) == 1 else None

    async def latest(self):
        articles = update_articles(await self.fetch(SOURCE))
        if not articles:
            raise ValueError("No official FC 27 update article is available yet")
        article = articles[-1]
        details = article_details(await self.fetch(article["url"]), article["slug"])
        return {**article, **details, "url": article["url"], "published_ts": article["published_ts"]}

    async def check(self, guild):
        async with self._lock:
            gid = str(guild.id)
            if not patch_settings(gid)["enabled"]:
                return "Automatic patch-note posts are disabled. Enable them in the admin panel."
            channel = self.channel(guild)
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("No patch-notes channel found; choose one in /admin → Patch notes")
            articles = update_articles(await self.fetch(SOURCE))
            now = utc_now()
            watch_since = get_setting(gid, "patchnotes:watch_since")
            if watch_since is None:
                # Quietly migrate old monitors too. Discovering a new URL or
                # changing the filter must never replay historical articles.
                with connect() as conn:
                    for article in articles:
                        conn.execute("INSERT OR IGNORE INTO patchnotes_seen VALUES (?,?,?,?,NULL)",
                                     (gid, article["url"], article["title"], article["published_ts"]))
                    for key, value in (("watch_since", str(int(now.timestamp()))), ("initialized", "1"),
                                       ("last_checked", now.isoformat())):
                        conn.execute("INSERT INTO settings (guild_id,key,value) VALUES (?,?,?) "
                                     "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (gid, "patchnotes:" + key, value))
                await report_health(self.bot, gid, "EA patch notes")
                return "Monitoring started quietly. Existing articles were not posted. Preview latest is private."
            with connect() as conn:
                seen = {r["url"] for r in conn.execute("SELECT url FROM patchnotes_seen WHERE guild_id=?", (gid,))}
            cutoff = max(int(watch_since), int(now.timestamp()) - MAX_UPDATE_AGE)
            pending = [a for a in articles if a["url"] not in seen and a["published_ts"] > cutoff]
            posted, skipped = 0, 0
            for article in pending:
                # One bad article must not block every newer one behind it.
                try:
                    page = await self.fetch(article["url"])
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    log.warning("Couldn't download %s; will retry next check", article["url"])
                    skipped += 1
                    continue
                try:
                    details = article_details(page, article["slug"])
                except Exception:
                    log.exception("Couldn't read EA article %s; skipping it", article["url"])
                    with connect() as conn:   # remember it so it isn't retried every hour
                        conn.execute("INSERT OR IGNORE INTO patchnotes_seen VALUES (?,?,?,?,NULL)",
                                     (gid, article["url"], article["title"], article["published_ts"]))
                    skipped += 1
                    continue
                message = await channel.send(embed=patch_embed({**article, **details}),
                                             allowed_mentions=discord.AllowedMentions.none())
                with connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO patchnotes_seen VALUES (?,?,?,?,?)",
                                 (gid, article["url"], article["title"], article["published_ts"], str(message.id)))
                posted += 1
            set_setting(gid, "patchnotes:last_checked", now.isoformat())
            await report_health(self.bot, gid, "EA patch notes")
            note = f" ({skipped} couldn't be read and were skipped.)" if skipped else ""
            return (f"Posted {posted} new update(s)." if posted else "Checked EA: no new patch notes.") + note

    @tasks.loop(hours=1)
    @survive
    async def ticker(self):
        guild = self.bot.get_guild(int(GUILD_ID)) if GUILD_ID else self.bot.guilds[0] if len(self.bot.guilds) == 1 else None
        if guild:
            try:
                await self.check(guild)
            except Exception:
                log.exception("Couldn't check or post EA patch notes")
                await report_health(self.bot, guild.id, "EA patch notes", "EA patch notes could not be fetched or posted. Check channel permissions and bot logs.")

    @ticker.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

    patchnotes = app_commands.Group(name="patchnotes", description="Private controls for FC 27 updates, Clubs and Grounds news")

    @patchnotes.command(name="settings", description="Private: enable patch notes or choose their channel")
    async def settings(self, interaction: discord.Interaction, enabled: bool = None, channel: discord.TextChannel = None):
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("Patch-note controls are private.", ephemeral=True)
            return
        gid = str(interaction.guild_id)
        if enabled is not None:
            set_setting(gid, "patchnotes:enabled", "1" if enabled else "0")
        if channel is not None:
            set_setting(gid, "patchnotes:channel", str(channel.id))
        settings = patch_settings(gid)
        target = f"<#{settings['channel_id']}>" if settings["channel_id"] else "#general"
        await interaction.response.send_message(f"EA FC 27 patch notes: **{'On' if settings['enabled'] else 'Off'}** · {target}\n"
                                                f"Checks every hour. Last successful check: {settings['last_checked']}", ephemeral=True)

    @patchnotes.command(name="check", description="Private: check EA now and post any new update once")
    async def check_command(self, interaction: discord.Interaction):
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("Patch-note controls are private.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = await self.check(interaction.guild)
        except Exception:
            log.exception("Manual EA patch notes check failed")
            await report_health(self.bot, interaction.guild_id, "EA patch notes", "The manual check failed. Check EA access, channel permissions and bot logs.")
            text = "Couldn't check/post EA notes. No update has been marked as posted; you can retry."
        await interaction.followup.send(text, ephemeral=True)

    @patchnotes.command(name="latest", description="Private: preview the latest official update, Clubs or Grounds news")
    async def preview(self, interaction: discord.Interaction):
        if not can_view(interaction.user, interaction.guild):
            await interaction.response.send_message("Patch-note controls are private.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            article = await self.latest()
        except Exception:
            log.exception("Couldn't preview EA patch notes")
            await interaction.followup.send("Couldn't read EA's notes right now. Please try again later.", ephemeral=True)
            return
        await interaction.followup.send(embed=patch_embed(article), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(PatchNotesCog(bot))
