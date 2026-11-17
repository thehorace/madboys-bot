"""Persistent match details button; reads saved data without another EA request."""
import asyncio
import json
import discord
import match_data as md
from db import connect


class ResultDetails(discord.ui.DynamicItem[discord.ui.Button], template=r'result:details:(?P<club>\d+):(?P<mid>[A-Za-z0-9_-]{1,60})'):
    def __init__(self, cid, mid):
        self.cid, self.mid = int(cid), str(mid)
        super().__init__(discord.ui.Button(label='Full match stats', emoji='📊', custom_id=f'result:details:{cid}:{mid}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['club']), match['mid'])

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        def read():
            with connect() as conn:
                row = conn.execute('SELECT raw_json FROM matches WHERE club_id=? AND match_id=?', (self.cid, self.mid)).fetchone()
            return md.parse_match(json.loads(row[0]), self.cid) if row and row[0] else None
        match = await asyncio.to_thread(read)
        if match is None:
            await interaction.followup.send('This match is no longer available in saved history.', ephemeral=True)
            return
        embed, file = await md.match_post(match)
        await interaction.followup.send(embed=embed, **({'file': file} if file else {}), ephemeral=True,
                                       allowed_mentions=discord.AllowedMentions.none())
