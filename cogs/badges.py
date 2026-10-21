"""
Badges on Discord: announcements, nickname emojis and /badges.

  - After every tracked game, players in it are re-checked; new badges are announced in the
    announcements channel and the newest badge's emoji goes on their server nickname.
  - On first launch, everyone's history is badged quietly, nicknames are updated, and one
    "badges are here" message is posted.
  - /badges shows your badges (or someone else's) and lets you pick which emoji to show,
    or turn the nickname emoji off.

Nicknames need the bot to have Manage Nicknames and a role above the players'. Discord
never lets a bot rename the server owner.
"""
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import badges as B
from cogs.link import find_discord_id_by_ea_name, get_all_links
from config import CLUB_COLOUR, GUILD_ID
from db import get_setting, set_setting
from interaction_tracking import TrackedView, failed
from utils import resolve_name

log = logging.getLogger("madboys-bot.badges")


def badge_summary(guild_id: str, discord_id: str) -> str:
    mine = B.stored(guild_id, discord_id)
    return "  ".join(B.badge_line(v, k).replace("**", "") for k, v in
                     sorted(mine.items(), key=lambda kv: kv[1]["earned_at"]))


def badges_embed(guild_id: str, discord_id: str, name: str) -> discord.Embed:
    mine = B.stored(guild_id, discord_id)
    lines = []
    for key, (emoji, label, how) in B.BADGES.items():
        if key in mine:
            info = mine[key]
            lines.append(f"{B.badge_line(info, key)} — {info['detail']} (<t:{info['earned_at']}:d>)")
        else:
            lines.append(f"🔒 ~~{label}~~ — {how}")
    embed = discord.Embed(title=f"🏅 {name}'s badges ({len(mine)}/{len(B.BADGES)})", description="\n".join(lines),
                          colour=CLUB_COLOUR)
    showing = B.display_badge(guild_id, discord_id)
    note = B.user_row(guild_id, discord_id).get("nick_note")
    footer = (f"Nickname shows {B.BADGES[showing][0]} {B.BADGES[showing][1]}" if showing
              else "No badge emoji on the nickname")
    if note:
        footer += f" • Can't change the nickname: {note.replace('**', '')}"
    embed.set_footer(text=footer[:2048])
    return embed


def badge_board(guild: discord.Guild) -> list[tuple[str, dict]]:
    from db import connect
    with connect() as conn:
        rows = conn.execute("SELECT discord_id, badge, count, earned_at FROM player_badges WHERE guild_id=?",
                            (str(guild.id),)).fetchall()
    by_user: dict[str, dict] = {}
    for r in rows:
        if r["badge"] in B.BADGES:
            by_user.setdefault(r["discord_id"], {})[r["badge"]] = dict(r)
    return sorted(by_user.items(), key=lambda kv: (-len(kv[1]), -sum(b["count"] for b in kv[1].values())))


async def build_badge_board(guild: discord.Guild) -> discord.Embed | str:
    board = badge_board(guild)
    if not board:
        return "Nobody has a badge yet — they're earned from tracked games (see /badges for how)."
    lines = []
    for did, mine in board[:20]:
        icons = " ".join(B.BADGES[k][0] + (f"×{v['count']}" if v["count"] > 1 else "")
                         for k, v in sorted(mine.items(), key=lambda kv: kv[1]["earned_at"]))
        lines.append(f"**{await resolve_name(guild, did)}** — {icons}")
    legend = " · ".join(f"{e} {n}" for e, n, _ in B.BADGES.values())
    embed = discord.Embed(title="🏅 Badge collection", description="\n".join(lines)[:4000], colour=CLUB_COLOUR)
    embed.set_footer(text=legend[:2048])
    return embed


class BadgeChoiceView(TrackedView):
    """Private menu from /badges: choose which earned badge shows on your nickname."""

    def __init__(self, cog: "BadgesCog", guild: discord.Guild, user: discord.abc.User):
        super().__init__(timeout=10 * 60)
        self.cog, self.guild, self.user = cog, guild, user
        gid, uid = str(guild.id), str(user.id)
        mine = B.stored(gid, uid)
        show = B.user_row(gid, uid)["show"]
        options = [discord.SelectOption(label="Newest badge (automatic)", value="auto", emoji="✨", default=show == "auto")]
        options += [discord.SelectOption(label=B.BADGES[k][1], value=k, emoji=B.BADGES[k][0], default=show == k)
                    for k in B.BADGES if k in mine]
        options.append(discord.SelectOption(label="No emoji on my nickname", value="none", emoji="🚫",
                                            default=show == "none"))
        pick = discord.ui.Select(placeholder="Badge on your nickname…", options=options[:25])
        pick.callback = self.choose
        self.add_item(pick)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This menu is someone else's — use /badges.", ephemeral=True)
            return False
        return True

    async def choose(self, interaction: discord.Interaction):
        gid, uid = str(self.guild.id), str(self.user.id)
        B.set_user(gid, uid, show=interaction.data["values"][0])
        await interaction.response.defer()
        member = self.guild.get_member(self.user.id) or interaction.user
        await self.cog.apply_nickname(member)
        await interaction.edit_original_response(
            embed=badges_embed(gid, uid, getattr(member, "display_name", self.user.name)),
            view=BadgeChoiceView(self.cog, self.guild, self.user))


class BadgesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        B.init_badges()
        self._lock = asyncio.Lock()
        self._launch_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._launch_task = asyncio.create_task(self._launch())

    def cog_unload(self):
        if self._launch_task:
            self._launch_task.cancel()

    def home_guild(self) -> Optional[discord.Guild]:
        if GUILD_ID:
            return self.bot.get_guild(int(GUILD_ID))
        return self.bot.guilds[0] if len(self.bot.guilds) == 1 else None

    async def _channel(self, guild: discord.Guild):
        tracker = self.bot.get_cog("MatchdayCog")
        cid = tracker.channel_id_for(str(guild.id)) if tracker else None
        if not cid:
            return None
        return self.bot.get_channel(int(cid)) or await self.bot.fetch_channel(int(cid))

    # ------------------------------------------------------------------ #
    async def _launch(self):
        """Badge everyone's history quietly (first launch, and anyone who linked since)."""
        try:
            await self.bot.wait_until_ready()
            guild = self.home_guild()
            if not guild:
                return
            gid = str(guild.id)
            first_launch = get_setting(gid, "badges:launched") is None
            earned = 0
            async with self._lock:
                links = get_all_links(gid)
                for did in B.users_with_badges(gid) - set(links):
                    await self._forget(guild, did)              # unlinked since: badges and emoji go
                for did, ea_name in links.items():
                    row = B.user_row(gid, did)
                    if row["seeded"] and (row["ea_name"] or "").casefold() == ea_name.casefold():
                        continue
                    await asyncio.to_thread(B.refresh, gid, did, ea_name)
                    if B.stored(gid, did):
                        earned += 1
                        member = guild.get_member(int(did))
                        if member:
                            await self.apply_nickname(member)
            if first_launch:
                set_setting(gid, "badges:launched", "1")
                channel = await self._channel(guild)
                if channel and earned:
                    await channel.send(
                        "🏅 **Badges are here!** They're earned from real tracked games — hat-tricks, 9.0+ ratings, "
                        f"clean-sheet runs and more. **{earned}** of you already earned some from past games, so "
                        "check your nickname 👀\nSee yours and choose which emoji to show with `/badges`.",
                        allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Badge launch failed")

    async def after_matches(self, guild: Optional[discord.Guild], matches: list):
        """Called by the tracker after new games are posted (not for quiet backfills)."""
        if not guild or not matches:
            return
        gid = str(guild.id)
        names = {p.name for pm in matches for p in pm.players}
        # Only badges earned in THESE games are announced. Older ones (e.g. a club's history that was
        # saved quietly when it was added) are stored without a fanfare.
        times = [pm.ts for pm in matches if getattr(pm, "ts", None)]
        since = min(times) if times else None
        lines = []
        async with self._lock:
            for name in sorted(names, key=str.lower):
                did = find_discord_id_by_ea_name(gid, name)
                if not did:
                    continue
                changed, quiet = await asyncio.to_thread(B.refresh, gid, did, name)
                if not changed:
                    continue
                if not quiet:
                    for key, info in changed:
                        if since is not None and info["earned_at"] < since:
                            continue
                        again = f" (×{info['count']})" if info["count"] > 1 else ""
                        lines.append(f"<@{did}> earned {B.BADGES[key][0]} **{B.BADGES[key][1]}**{again} — {info['detail']}")
                member = guild.get_member(int(did))
                if member:
                    await self.apply_nickname(member)
        if lines:
            channel = await self._channel(guild)
            if channel:
                try:
                    await channel.send("🏅 **New badge" + ("s" if len(lines) > 1 else "") + "!**\n" + "\n".join(lines)[:1900],
                                       allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    log.warning("Couldn't post badge announcement")

    async def refresh_member(self, guild: discord.Guild, discord_id: str):
        """Re-check one linked player (e.g. after a monthly award) and update their nickname."""
        from cogs.link import get_link
        name = get_link(str(guild.id), discord_id)
        if not name:
            return []
        async with self._lock:
            changed, _ = await asyncio.to_thread(B.refresh, str(guild.id), discord_id, name)
            member = guild.get_member(int(discord_id))
            if member:
                await self.apply_nickname(member)
        return changed

    async def _forget(self, guild: discord.Guild, discord_id: str):
        """Unlinked: drop their badges and take our emoji off their nickname."""
        B.clear_badges(str(guild.id), discord_id)
        member = guild.get_member(int(discord_id))
        if member:
            await self.apply_nickname(member)

    async def forget(self, guild: discord.Guild, discord_id: str):
        async with self._lock:
            await self._forget(guild, discord_id)

    async def apply_nickname(self, member: discord.Member) -> Optional[str]:
        """Put the right badge emoji on this member's nickname. Returns why it couldn't, if it couldn't."""
        gid, uid = str(member.guild.id), str(member.id)
        key = B.display_badge(gid, uid)
        emoji = B.BADGES[key][0] if key else None
        applied = B.user_row(gid, uid)["applied"]
        account = getattr(member, "global_name", None) or member.name
        new = B.nickname_for(member.nick, account, emoji, applied)
        if new == member.nick:
            # nothing to change; remember whether our emoji is on it right now
            B.set_user(gid, uid, nick_note=None, applied=emoji if emoji and (new or "").endswith(emoji) else None)
            return None
        reason = B.cant_rename_reason(member, member.guild.me)
        if not reason:
            try:
                await member.edit(nick=new, reason="Badge emoji")
                B.set_user(gid, uid, applied=emoji)
            except discord.Forbidden:
                reason = "Discord refused — check the bot's role is above theirs and it has Manage Nicknames."
            except discord.HTTPException as e:
                log.warning("Nickname change failed for %s: %s", uid, e)
                reason = "Discord had a hiccup — try again with /badges."
        B.set_user(gid, uid, nick_note=reason)
        return reason

    # ------------------------------------------------------------------ #
    @app_commands.command(name="badges", description="Your badges (or someone else's) and which emoji shows on your nickname")
    @app_commands.describe(member="See someone else's badges")
    @app_commands.guild_only()
    async def badges(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await self.open_badges(interaction, member)

    async def open_badges(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        """Private badge page (+ emoji picker for your own). Used by /badges, the panel and the stats menu."""
        gid = str(interaction.guild_id)
        target = member or interaction.user
        if target.bot:
            failed(interaction)
            await interaction.response.send_message("Bots don't earn badges 🙂", ephemeral=True)
            return
        from cogs.link import get_link
        if not get_link(gid, str(target.id)):
            failed(interaction)
            who = "You're" if target == interaction.user else f"{target.display_name} is"
            await interaction.response.send_message(
                f"{who} not linked to an EA name yet, so badges can't be tracked — tap **Setup** on the panel first.",
                ephemeral=True)
            if str(target.id) in B.users_with_badges(gid):
                await self.forget(interaction.guild, str(target.id))   # unlinked since: clean up
            return
        await interaction.response.defer(ephemeral=True)
        if not B.user_row(gid, str(target.id))["seeded"]:
            await self.refresh_member(interaction.guild, str(target.id))
        embed = badges_embed(gid, str(target.id), target.display_name)
        if target == interaction.user:
            await interaction.followup.send(embed=embed, view=BadgeChoiceView(self, interaction.guild, interaction.user),
                                            ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BadgesCog(bot))
