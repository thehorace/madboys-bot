"""Private, on-demand progress screens; no scheduled posts or DM changes."""
import asyncio
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import progress as P
from clubs import club_id, club_scoped, monitored_clubs
from cogs.link import get_link
from cogs.stats import player_autocomplete
from cogs.session_reports import PrivateSessionView, squad_in_voice
from db import connect


async def build_session(bot, guild):
    tracker=bot.get_cog('MatchdayCog')
    running=bool(tracker and tracker.ticker.is_running())
    return await asyncio.to_thread(P.session_embed,str(guild.id),in_voice=squad_in_voice(guild),
        tracker_ok=getattr(tracker,'last_poll_ok',None),tracker_running=running,
        next_check=tracker.next_poll_at if running else None)


def roster(cid):
    with connect() as conn:
        return [r[0] for r in conn.execute('SELECT p.name FROM match_players p JOIN matches m '
            'ON m.club_id=p.club_id AND m.match_id=p.match_id WHERE m.club_id=? AND m.ts>=? '
            'GROUP BY p.name COLLATE NOCASE ORDER BY COUNT(*) DESC,p.name LIMIT 24',(cid,int(time.time())-2*P.WEEK))]


class ProgressView(PrivateSessionView):
    def __init__(self,bot,user_id,guild,mode,player=None):
        super().__init__(timeout=600)
        self.bot,self.user_id,self.guild,self.mode=bot,user_id,guild,mode
        self.cid=club_id()
        self.player=player
        self.embed=None

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.user_id or interaction.guild_id!=self.guild.id:
            await interaction.response.send_message('Open your own progress screen from the panel.',ephemeral=True)
            return False
        return True

    async def rebuild(self):
        self.clear_items()
        if self.mode=='weekly':
            self.embed=await asyncio.to_thread(P.weekly_embed,self.cid,self.player)
            names=await asyncio.to_thread(roster,self.cid)
            linked=get_link(str(self.guild.id),str(self.user_id))
            names=list(dict.fromkeys(([self.player] if self.player else [])+([linked] if linked else [])+names))[:24]
            self.players = {f'p:{i}':n for i,n in enumerate(names)}
            picker=discord.ui.Select(placeholder='Club overview or a player',row=0,
                options=[discord.SelectOption(label='Club overview',value='club',default=self.player is None)]+
                [discord.SelectOption(label=n[:100],value=value,default=n==self.player) for value,n in self.players.items()])
            picker.callback=self.pick_player
            self.add_item(picker)
            clubs=monitored_clubs()
            if clubs:
                picker=discord.ui.Select(placeholder='Choose club',row=1,options=[discord.SelectOption(
                    label=c['name'][:100],value=str(c['club_id']),default=c['club_id']==self.cid) for c in clubs])
                picker.callback=self.pick_club
                self.add_item(picker)
        else:
            self.embed=await build_session(self.bot,self.guild)
        self.add_item(self.refresh)

    async def redraw(self,interaction):
        self.anchor=interaction
        await interaction.response.defer()
        await self.rebuild()
        await interaction.edit_original_response(embed=self.embed,view=self)

    async def pick_player(self,interaction):
        value=interaction.data['values'][0]
        if value != 'club' and value not in self.players:
            await interaction.response.send_message('Reopen Weekly Progress to choose a player.',ephemeral=True)
            return
        self.player=None if value=='club' else self.players[value]
        await self.redraw(interaction)

    async def pick_club(self,interaction):
        cid=int(interaction.data['values'][0])
        if cid not in {c['club_id'] for c in monitored_clubs()}:
            await interaction.response.send_message('That club is no longer enabled. Reopen Weekly Progress.',ephemeral=True)
            return
        self.cid=cid
        self.club_id=cid
        await self.redraw(interaction)

    @discord.ui.button(label='Refresh',emoji='🔄',row=2)
    async def refresh(self,interaction,_):
        await self.redraw(interaction)

    async def on_timeout(self):
        if self.anchor:
            try:
                await self.anchor.edit_original_response(content='This progress menu has expired. Reopen it from the panel.',view=None)
            except discord.HTTPException:
                pass


class ProgressCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot

    async def open_weekly(self,interaction,player=None):
        if player == 'me':
            player = None
        await self.open_screen(interaction,'weekly',player or get_link(str(interaction.guild_id),str(interaction.user.id)))

    async def open_session(self,interaction):
        await self.open_screen(interaction,'session')

    async def open_screen(self,interaction,mode,player=None):
        await interaction.response.defer(ephemeral=True,thinking=True)
        view=ProgressView(self.bot,interaction.user.id,interaction.guild,mode,player)
        view.anchor=interaction
        await view.rebuild()
        await interaction.followup.send(embed=view.embed,view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='weeklyprogress',description='Compare the last 7 days with the previous 7 days for your club or player')
    @app_commands.autocomplete(player=player_autocomplete)
    @app_commands.guild_only()
    @club_scoped
    async def weeklyprogress(self,interaction:discord.Interaction,player:Optional[str]=None):
        await self.open_weekly(interaction,player)

    @app_commands.command(name='sessionprogress',description='See recorded session activity, recap timing and why it is waiting')
    @app_commands.guild_only()
    async def sessionprogress(self,interaction:discord.Interaction):
        await self.open_session(interaction)


async def setup(bot):
    await bot.add_cog(ProgressCog(bot))
