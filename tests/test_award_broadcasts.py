"""Explicit everyone broadcasts to both destinations, without repeat retry pings."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import db
import awards as A
from announce import _named
from cogs.awards import AwardsCog
from cogs.link import set_link
from tests.test_squad_features import Base, SYD, ts


class AwardsBroadcastTests(Base):
    def setUp(self):
        super().setUp()
        minimum=patch.object(A,'MIN_POTM_GAMES',3)
        minimum.start()
        self.addCleanup(minimum.stop)
        self.general=SimpleNamespace(id=20,name='general',send=AsyncMock(return_value=SimpleNamespace(id=100)))
        self.announcements=SimpleNamespace(id=30,name='announcements',send=AsyncMock(return_value=SimpleNamespace(id=101)))
        self.guild=SimpleNamespace(id=10,text_channels=[self.general,self.announcements])
        db.set_setting('10','announce:channel','30')
        self.addCleanup(lambda:_named.pop('10',None))
        self.cog=object.__new__(AwardsCog)
        self.cog.bot=SimpleNamespace(get_cog=lambda _:None,get_channel={20:self.general,30:self.announcements}.get)
        self.cog.update_role=AsyncMock()
        set_link('10','42','Ali','self')
        for i in range(3):
            self.game(ts(2026,10,3+i),players=[('Ali',{'r':8,'g':2})])

    async def post(self,force=False):
        with patch('cogs.awards._tz',return_value=SYD),patch('cogs.awards.report_health',AsyncMock()):
            return await self.cog.broadcast(self.guild,2026,10,force)

    async def test_both_channels_get_congratulations_one_everyone_and_only_winner_mention(self):
        self.assertEqual(await self.post(),2)
        for ch in (self.general,self.announcements):
            kwargs=ch.send.call_args.kwargs
            self.assertEqual(kwargs['content'].count('@everyone'),1)
            self.assertIn('Congratulations <@42>',kwargs['content'])
            self.assertIn('October 2026',kwargs['content'])
            mentions=kwargs['allowed_mentions']
            self.assertTrue(mentions.everyone)
            self.assertFalse(mentions.roles)
            self.assertEqual([u.id for u in mentions.users],[42])
        self.assertEqual(await self.post(),0)
        self.general.send.assert_awaited_once()
        self.announcements.send.assert_awaited_once()

    async def test_partial_failure_retry_only_posts_missing_channel_even_after_restart(self):
        self.announcements.send.side_effect=discord.HTTPException(SimpleNamespace(status=500,reason='Temporary'),'Retry')
        self.assertEqual(await self.post(),1)
        bot=self.cog.bot
        self.cog=object.__new__(AwardsCog)
        self.cog.bot=bot
        self.cog.update_role=AsyncMock()
        self.announcements.send.side_effect=None
        self.assertEqual(await self.post(force=True),1)
        self.general.send.assert_awaited_once()
        self.assertEqual(self.announcements.send.await_count,2)
        self.assertEqual(await self.post(),0)

    async def test_explicit_resend_can_start_new_round_after_both_deliveries_finish(self):
        self.assertEqual(await self.post(),2)
        self.assertEqual(await self.post(force=True),2)
        self.assertEqual(self.general.send.await_count,2)
        self.assertEqual(self.announcements.send.await_count,2)
        self.assertEqual(await self.post(),0)

    async def test_same_destination_is_not_pinged_twice(self):
        db.set_setting('10','announce:channel','20')
        self.assertEqual(await self.post(),1)
        self.general.send.assert_awaited_once()
        self.announcements.send.assert_not_awaited()

    async def test_old_minimum_snapshot_gets_one_corrected_broadcast(self):
        self.assertEqual(await self.post(),2)
        for i in range(28):
            self.game(ts(2026,10,10)+i*600,players=[('Ali',{'r':8,'g':1})])
        with patch.object(A,'MIN_POTM_GAMES',31):
            self.assertEqual(await self.post(),2)
            self.assertEqual(await self.post(),0)
        self.assertEqual(self.general.send.await_count,2)
        self.assertEqual(self.announcements.send.await_count,2)
        self.assertIn('31+',self.general.send.call_args.kwargs['embed'].footer.text)

    async def test_missing_everyone_permission_retries_only_that_channel_when_fixed(self):
        self.guild.me=SimpleNamespace()
        perms=SimpleNamespace(mention_everyone=False)
        self.announcements.permissions_for=lambda _:perms
        self.assertEqual(await self.post(),1)
        self.announcements.send.assert_not_awaited()
        perms.mention_everyone=True
        self.assertEqual(await self.post(),1)
        self.general.send.assert_awaited_once()
        self.announcements.send.assert_awaited_once()

    async def test_resend_command_is_private_and_rejects_unfinished_month(self):
        interaction=SimpleNamespace(guild_id=10,guild=self.guild,user=SimpleNamespace(id=42,name='fauz'),
            response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        with patch('cogs.usage.can_view',return_value=False):
            await AwardsCog.resendawards.callback(self.cog,interaction,'2026-10')
        interaction.response.defer.assert_not_awaited()
        self.general.send.assert_not_awaited()
        with patch('cogs.usage.can_view',return_value=True),patch('cogs.awards.datetime',wraps=datetime) as clock,patch('cogs.awards._tz',return_value=SYD),patch('cogs.awards.report_health',AsyncMock()):
            clock.now.return_value=datetime(2026,11,1,12,tzinfo=SYD)
            await AwardsCog.resendawards.callback(self.cog,interaction,'2026-11')
            self.general.send.assert_not_awaited()
            await AwardsCog.resendawards.callback(self.cog,interaction,'2026-10')
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        self.general.send.assert_awaited_once()
        self.announcements.send.assert_awaited_once()
