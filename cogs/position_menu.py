"""Private position defaults and deliberate, game-specific corrections."""
import discord
from clubs import club_id,club_scope,club_for
from db import connect
from config import CLUB_COLOUR
from interaction_tracking import TrackedView
import positions as P

def clubs():
    with connect() as conn:
        return [dict(r) for r in conn.execute('SELECT club_id,name FROM monitored_clubs ORDER BY club_id')]

def selected_club(value):
    matches=[c for c in clubs() if str(c['club_id'])==value or c['name'].casefold()==value.casefold()]
    if len(matches)!=1:
        raise ValueError('Choose MADBOYS FC or GRAYSBOYS from the club suggestions.')
    return matches[0]['club_id']

async def club_autocomplete(interaction,current):
    return [discord.app_commands.Choice(name=c['name'][:100],value=str(c['club_id'])) for c in clubs()
            if current.casefold() in c['name'].casefold() or current in str(c['club_id'])][:25]

class PositionView(TrackedView):
    def __init__(self,bot,user,gid,cid):
        super().__init__(timeout=600)
        self.bot,self.user,self.gid,self.cid=bot,user,str(gid),cid
        self.club_id=cid
        self.correction=False
        self.render()

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.user.id or str(interaction.guild_id)!=self.gid:
            await interaction.response.send_message('Open your own position controls with `/position`.',ephemeral=True)
            return False
        return True

    def embed(self,note=None):
        with club_scope(self.cid):
            text=P.profile_text(self.gid,str(self.user.id))
        if note:
            text=discord.utils.escape_markdown(note)+'\n\n'+text
        text+='\n\n'+('Pick the exact position to correct your last game in this club. Your saved default will stay as it is.'
            if self.correction else 'Pick a saved position for future matches. Saved positions are inferred; past games stay as recorded.')
        text+='\nFor an older game: `/position position:CB match_id:ID club:…`.'
        return discord.Embed(title=f"📍 Position — {club_for(self.cid)['name']}",description=text,colour=CLUB_COLOUR)

    def render(self):
        self.clear_items()
        options=[discord.SelectOption(label=p,value=p) for p in P.ALL_POSITIONS]
        if not self.correction:
            options.append(discord.SelectOption(label='Clear saved position',value='_clear'))
        picker=discord.ui.Select(placeholder='Correct last game…' if self.correction else 'Saved position for this club…',row=0,options=options)
        picker.callback=self.choose
        self.add_item(picker)
        choices=clubs()
        if len(choices)>1:
            picker=discord.ui.Select(placeholder='Choose club',row=1,options=[discord.SelectOption(label=c['name'][:100],value=str(c['club_id']),default=c['club_id']==self.cid) for c in choices[:25]])
            picker.callback=self.choose_club
            self.add_item(picker)
        button=discord.ui.Button(label='Edit saved position' if self.correction else 'Correct last game',row=2)
        button.callback=self.toggle
        self.add_item(button)

    async def choose(self,interaction):
        if not await self.interaction_check(interaction):
            return
        value=interaction.data['values'][0]
        with club_scope(self.cid):
            try:
                if self.correction:
                    note=P.correct_position(self.gid,str(self.user.id),value)
                else:
                    P.save_default(self.gid,str(self.user.id),None if value=='_clear' else value)
                    note='Saved position cleared.' if value=='_clear' else f'{value} saved for future {club_for(self.cid)["name"]} games.'
            except ValueError as exc:
                await interaction.response.send_message(str(exc),ephemeral=True)
                return
        await interaction.response.edit_message(embed=self.embed(note),view=self)

    async def choose_club(self,interaction):
        if not await self.interaction_check(interaction):
            return
        try:
            self.cid=selected_club(interaction.data['values'][0])
            self.club_id=self.cid
        except ValueError as exc:
            await interaction.response.send_message(str(exc),ephemeral=True)
            return
        self.render()
        await interaction.response.edit_message(embed=self.embed(),view=self)

    async def toggle(self,interaction):
        if not await self.interaction_check(interaction):
            return
        self.correction=not self.correction
        self.render()
        await interaction.response.edit_message(embed=self.embed(),view=self)

async def open_menu(bot,interaction,cid=None):
    await interaction.response.defer(ephemeral=True,thinking=True)
    view=PositionView(bot,interaction.user,interaction.guild_id,club_id() if cid is None else cid)
    await interaction.followup.send(embed=view.embed(),view=view,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
