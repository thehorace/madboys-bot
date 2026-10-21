"""Tracker speed/reliability with several clubs: parallel fetch, milestones last, failing-club cut-off."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import clubs
import db
import ea_client
import match_data as md
from cogs import matchday
from config import CLUB_ID
import tests.test_multi_club as TMC


class TrackerSpeedTests(TMC.MultiClubTests):
    async def test_clubs_are_fetched_at_the_same_time(self):
        self.seed()

        async def slow(cid, **kw):
            await asyncio.sleep(0.2)
            return []
        self.tracker.ea.get_recent_matches_multi.side_effect = slow
        t0 = time.perf_counter()
        await self.tracker.poll_once()
        self.assertLess(time.perf_counter() - t0, 0.35)   # one wait, not one per club
        self.assertTrue(self.tracker.last_poll_ok)

    async def test_milestones_run_after_every_club_has_posted(self):
        self.seed()
        order = []
        self.channel.send.side_effect = lambda **kw: order.append("post") or self.channel
        self.tracker.check_milestones.side_effect = lambda *a, **kw: order.append("milestones")
        self.tracker.ea.get_recent_matches_multi.side_effect = \
            lambda cid, **kw: [self.raw(clubs.club_for(cid), "m", 200)]
        await self.tracker.poll_once()
        self.assertEqual(order, ["post", "post", "milestones", "milestones"])
        # each milestone check still sees only its own club's games
        for call, club in zip(self.tracker.check_milestones.call_args_list, clubs.monitored_clubs()):
            self.assertEqual({m.club_id for m in call.kwargs["matches"]}, {club["club_id"]})

    async def test_a_club_failing_for_hours_stops_blocking_session_summaries(self):
        self.seed()
        health = AsyncMock()

        async def fetch(cid, **kw):
            if cid != CLUB_ID:
                raise RuntimeError("bad club id")
            return []
        self.tracker.ea.get_recent_matches_multi.side_effect = fetch
        with patch.object(matchday, "report_health", health):
            await self.tracker.poll_once()
            self.assertFalse(self.tracker.last_poll_ok)          # short failure: summaries wait
            self.reports.after_poll.assert_not_awaited()
            state = self.tracker.club_status[self.secondary["club_id"]]
            state["failing_since"] -= matchday.STALE_CLUB_SECONDS + 1
            await self.tracker.poll_once()
            self.assertTrue(self.tracker.last_poll_ok)           # hours later: stop waiting
            self.reports.after_poll.assert_awaited_once()
            self.assertIn("Club tracking", health.await_args.args[2])
            self.assertIsNotNone(health.await_args.args[3])      # alert raised once
            alerts = health.await_count
            await self.tracker.poll_once()
            self.assertEqual(health.await_count, alerts)         # ...and not repeated every poll
            self.tracker.ea.get_recent_matches_multi.side_effect = lambda cid, **kw: []
            await self.tracker.poll_once()
            self.assertIsNone(health.await_args.args[3] if len(health.await_args.args) > 3 else None)

    async def test_every_club_failing_never_counts_as_a_good_check(self):
        self.seed()
        self.tracker.ea.get_recent_matches_multi.side_effect = RuntimeError("relay down")
        with patch.object(matchday, "report_health", AsyncMock()):
            for state in [None, None]:
                await self.tracker.poll_once()
                for s in self.tracker.club_status.values():
                    s["failing_since"] = (s.get("failing_since") or time.time()) - matchday.STALE_CLUB_SECONDS - 1
            await self.tracker.poll_once()
        self.assertFalse(self.tracker.last_poll_ok)
        self.reports.after_poll.assert_not_awaited()

    def test_result_repair_runs_once(self):
        pm = self.store(self.primary, "x", 100)
        with db.connect() as conn:
            conn.execute("UPDATE matches SET result='D' WHERE match_id='x'")
        md.backfill_results()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT result FROM matches WHERE match_id='x'").fetchone()[0], pm.result)
            conn.execute("UPDATE matches SET result='D' WHERE match_id='x'")
        md.backfill_results()   # second startup: skipped, nothing re-parsed
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT result FROM matches WHERE match_id='x'").fetchone()[0], "D")


class ParallelTypesTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracker_fetches_match_types_in_parallel(self):
        ea = ea_client.EAClient()

        async def slow(*a, **kw):
            await asyncio.sleep(0.2)
            return [{"matchId": kw["match_type"], "timestamp": "1"}]
        ea.get_recent_matches = AsyncMock(side_effect=slow)
        t0 = time.perf_counter()
        got = await ea.get_recent_matches_multi(1, match_types=["leagueMatch", "playoffMatch"],
                                                bypass_cache=True, allow_stale=False)
        self.assertLess(time.perf_counter() - t0, 0.35)
        self.assertEqual(len(got), 2)
        self.assertTrue(got.complete)


for _name in dir(TMC.MultiClubTests):   # the inherited tests already run in test_multi_club
    if _name.startswith("test_") and _name not in TrackerSpeedTests.__dict__:
        setattr(TrackerSpeedTests, _name, None)

if __name__ == "__main__":
    unittest.main()
