"""
EA Pro Clubs API client.

Calls the home middleware (madboys-middleware) which fetches from EA
using curl-cffi with Chrome TLS impersonation, bypassing Akamai's
cloud IP blocking that affects Railway directly.

Set MIDDLEWARE_URL in Railway env vars to your Cloudflare tunnel URL.
Optionally set MIDDLEWARE_API_KEY if you configured one on the middleware.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

log = logging.getLogger("madboys-bot.ea")

# Simple in-memory cache
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 600  # 10 minutes


def _cached(key: str) -> Optional[any]:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _store(key: str, data: any) -> None:
    _cache[key] = (time.time(), data)


class EAClient:
    def __init__(self, platform: str = "common-gen5"):
        self.platform = platform
        self._session: Optional[aiohttp.ClientSession] = None

        self._base = os.getenv("MIDDLEWARE_URL", "").rstrip("/")
        self._api_key = os.getenv("MIDDLEWARE_API_KEY", "")

        if self._base:
            log.info(f"EA requests via middleware: {self._base}")
        else:
            log.warning("MIDDLEWARE_URL not set — EA commands will not work")

    def _headers(self) -> dict:
        h = {}
        if self._api_key:
            h["X-API-Key"] = self._api_key
        return h

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict, bypass_cache: bool = False) -> Optional[any]:
        if not self._base:
            return None

        cache_key = path + str(sorted(params.items()))
        if not bypass_cache:
            cached = _cached(cache_key)
            if cached is not None:
                log.debug(f"Cache hit: {cache_key}")
                return cached

        url = f"{self._base}{path}"
        session = await self._get_session()

        try:
            async with session.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if not bypass_cache:
                        _store(cache_key, data)
                    return data
                else:
                    body = await resp.text()
                    log.warning(f"Middleware returned {resp.status} for {url} — {body[:200]}")
                    return None
        except asyncio.TimeoutError:
            log.warning(f"Middleware timed out: {url}")
            return None
        except Exception as e:
            log.error(f"Middleware error: {e}")
            return None

    async def get_club_info(self, club_id: int) -> Optional[dict]:
        data = await self._get("/clubinfo", {"clubId": str(club_id), "platform": self.platform})
        if data and str(club_id) in data:
            return data[str(club_id)]
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_recent_matches(
        self, club_id: int, match_type: str = "leagueMatch", count: int = 5, bypass_cache: bool = False
    ) -> Optional[list]:
        data = await self._get(
            "/matches",
            {
                "clubId": str(club_id),
                "matchType": match_type,
                "count": str(count),
                "platform": self.platform,
            },
            bypass_cache=bypass_cache,
        )
        if isinstance(data, list):
            return data
        return None

    async def get_recent_matches_multi(
        self,
        club_id: int,
        match_types: Optional[list[str]] = None,
        count: int = 5,
        bypass_cache: bool = False,
    ) -> Optional[list]:
        """
        Fetch recent matches across multiple match types and merge them.

        EA's /matches endpoint partitions match history by matchType —
        querying "leagueMatch" alone misses a club's playoff games entirely
        (and vice versa), so a club that just finished a playoff run would
        show up as having "no recent matches" even though they played
        yesterday. This fetches each type separately, dedupes by match
        identifier, sorts the combined set newest-first by timestamp, and
        returns the top `count` overall.

        Defaults to league + playoff, which covers Pro Clubs' two
        competitive match types. Friendlies/other types can be added here
        later if needed.
        """
        if match_types is None:
            match_types = ["leagueMatch", "playoffMatch"]

        all_matches: list[dict] = []
        for mt in match_types:
            matches = await self.get_recent_matches(
                club_id, match_type=mt, count=count, bypass_cache=bypass_cache
            )
            if matches:
                all_matches.extend(matches)

        if not all_matches:
            return None

        # Dedupe (in case EA ever double-reports across types) then sort
        # newest first. Fall back to matchId if timestamp is missing so a
        # bad/missing value doesn't crash the sort.
        seen = set()
        deduped = []
        for m in all_matches:
            match_id = str(m.get("matchId") or m.get("timestamp") or id(m))
            if match_id in seen:
                continue
            seen.add(match_id)
            deduped.append(m)

        def sort_key(m: dict) -> int:
            try:
                return int(m.get("timestamp", 0))
            except (TypeError, ValueError):
                return 0

        deduped.sort(key=sort_key, reverse=True)
        return deduped[:count]

    async def get_member_stats(self, club_id: int, career: bool = False) -> Optional[list]:
        path = "/members/career" if career else "/members"
        data = await self._get(path, {"clubId": str(club_id), "platform": self.platform})
        if data and "members" in data:
            return data["members"]
        if isinstance(data, list):
            return data
        return None

    async def get_overall_stats(self, club_id: int) -> Optional[dict]:
        data = await self._get("/overallstats", {"clubId": str(club_id), "platform": self.platform})
        if isinstance(data, list) and data:
            return data[0]
        if data and str(club_id) in data:
            return data[str(club_id)]
        return None
