"""Personal stats selection and private runtime tracking controls."""
import discord
from discord import app_commands
from discord.ext import commands
from clubs import monitored_clubs, selected_club
from db import connect, set_setting


def registry():
    with connect() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM monitored_clubs ORDER BY club_id')]


def toggle_club(cid):
    with connect() as conn:
        row = conn.execute('SELECT enabled FROM monitored_clubs WHERE club_id=?', (cid,)).fetchone()
        if not row:
            raise ValueError('Club no longer exists.')
        if row[0] and conn.execute('SELECT COUNT(*) FROM monitored_clubs WHERE enabled=1').fetchone()[0] <= 1:
            raise ValueError('Keep at least one club enabled.')
        conn.execute('UPDATE monitored_clubs SET enabled=? WHERE club_id=?', (not row[0], cid))


class ClubModal(discord.ui.Modal, title='Add a monitored EA club'):
    name = discord.ui.TextInput(label='Club display name', max_length=80)
    cid = discord.ui.TextInput(label='EA club ID', max_length=20)

    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel

    async def on_submit(self, interaction):
        if not await self.panel.authorize(interaction):
            return
        try:
            cid = int(self.cid.value)
            name = self.name.value.strip()
            if cid <= 0 or not name:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message('Enter a positive EA club ID and a name.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        info = await self.panel.bot.ea.get_club_info(cid)
        if not info or not info.get('name'):
            await interaction.followup.send('EA could not verify that club. Nothing was saved; check the ID and relay, then retry.', ephemeral=True)
            return
        with connect() as conn:
            count = conn.execute('SELECT COUNT(*) FROM monitored_clubs').fetchone()[0]
            duplicate = conn.execute('SELECT 1 FROM monitored_clubs WHERE club_id=? OR name=? COLLATE NOCASE', (cid, name)).fetchone()
            if not duplicate and count < 10:
                conn.execute('INSERT INTO monitored_clubs (club_id,name) VALUES (?,?)', (cid, name))
        if duplicate or count >= 10:
            await interaction.followup.send('That club ID/name already exists, or the 10-club limit was reached. Existing names stay fixed to preserve lineup history.', ephemeral=True)
            return
        await interaction.followup.send(f'Added **{name}** (EA: {info["name"]}). Tracking starts on the next check; existing games are saved quietly.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await self.panel.refresh_anchor(interaction)


async def club_autocomplete(interaction, current):
    return [app_commands.Choice(name=c['name'], value=str(c['club_id'])) for c in monitored_clubs() if current.lower() in c['name'].lower()][:25]


class ClubsCog(commands.Cog):
    @app_commands.command(name='club', description='Choose your stats/lineup club; tracking always watches all enabled clubs')
    @app_commands.guild_only()
    @app_commands.autocomplete(name=club_autocomplete)
    async def club(self, interaction: discord.Interaction, name: str = 'auto'):
        choice = next((c for c in monitored_clubs() if name == str(c['club_id']) or name.casefold() == c['name'].casefold()), None)
        if not choice and name.lower() != 'auto':
            await interaction.response.send_message('Choose an enabled club from autocomplete, or enter `auto`.', ephemeral=True)
            return
        set_setting(str(interaction.guild_id), f'club:{interaction.user.id}', str(choice['club_id']) if choice else None)
        current = selected_club(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(f'Your menus now use **{current["name"]}**' + (' (following latest activity).' if not choice else '.') + ' Open a fresh stats or lineup menu. The tracker keeps watching every enabled club.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(ClubsCog())
