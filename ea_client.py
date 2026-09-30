"""
EA Pro Clubs API client.

Calls the home middleware (madboys-middleware), which fetches from EA using
curl-cffi with Chrome TLS impersonation to get past Akamai's cloud-IP block.

  MIDDLEWARE_URL      your Cloudflare tunnel URL (required for EA features)
  MIDDLEWARE_API_KEY  optional, if you configured one on the middleware

One EAClient is created in bot.py and shared as `bot.ea`, so every cog uses
the same HTTP session and cache.

Caching:
  - Fresh results are reused for CACHE_TTL seconds.
  - If the middleware is unreachable (e.g. the home PC is off), the last good
    result is served instead of failing, for up to STALE_MAX seconds. Commands
    can call ea.stale_note() to tell the user the data is old.
  - Failures are remembered for FAIL_COOLDOWN seconds so ten people spamming
    /lastgame while the relay is down doesn't mean ten 20-second timeouts.
"""

import asyncio
import contextvars
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Optional

import aiohttp

log = logging.getLogger("madboys-bot.ea")

CACHE_TTL = 600          # 10 min
STALE_MAX = 7 * 24 * 3600
FAIL_COOLDOWN = 30
CACHE_MAX_ENTRIES = 200

# Per-command record of the oldest stale cache entry served. Each slash command
# runs in its own asyncio task with a fresh copy of the context, so this starts
# as None for every command.
_stale_age: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar("ea_stale_age", default=None)


def _human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f} days"


class EAClient:
    def __init__(self, platform: str = "common-gen5"):
        self.platform = platform
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._fail_until: dict[str, float] = {}

        self._base = os.getenv("MIDDLEWARE_URL", "").rstrip("/")
        self._api_key = os.getenv("MIDDLEWARE_API_KEY", "")

        # Health info for /status
        self.last_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_error_at: Optional[float] = None

        if self._base:
            log.info(f"EA requests via middleware: {self._base}")
        else:
            log.warning("MIDDLEWARE_URL not set — EA commands will not work")

    @property
    def configured(self) -> bool:
        return bool(self._base)

    # ------------------------------------------------------------------ #
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"X-API-Key": self._api_key} if self._api_key else {}
            self._session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def stale_note(self) -> Optional[str]:
        """If the last fetch in this command was served from an old cache, a short footer note."""
        age = _stale_age.get()
        if age is None:
            return None
        return f"⚠️ EA relay unreachable — showing data from {_human_age(age)} ago"

    def _remember(self, key: str, data: Any):
        self._cache[key] = (time.time(), data)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    async def _get(self, path: str, params: dict, bypass_cache: bool = False, allow_stale: bool = True) -> Optional[Any]:
        if not self._base:
            return None

        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self._cache.get(key)
        now = time.time()

        if cached and not bypass_cache and now - cached[0] < CACHE_TTL:
            return cached[1]

        def fallback():
            if allow_stale and cached and now - cached[0] < STALE_MAX:
                prev = _stale_age.get()
                _stale_age.set(max(prev or 0, now - cached[0]))
                return cached[1]
            return None

        if self._fail_until.get(key, 0) > now:
            return fallback()

        session = await self._get_session()
        url = f"{self._base}{path}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    self._remember(key, data)
                    self._fail_until.pop(key, None)
                    self.last_ok_at = time.time()
                    return data
                body = await resp.text()
                err = f"HTTP {resp.status} — {body[:200]}"
        except asyncio.TimeoutError:
            err = "timed out"
        except aiohttp.ClientError as e:
            err = f"{type(e).__name__}: {e}"
        except Exception as e:  # bad JSON etc.
            err = f"{type(e).__name__}: {e}"

        log.warning(f"Middleware request failed ({err}): {url}")
        self.last_error, self.last_error_at = err, time.time()
        self._fail_until[key] = time.time() + FAIL_COOLDOWN
        return fallback()

    async def ping(self) -> tuple[bool, float, str]:
        """Round-trip check of the relay (bypasses cache). Returns (ok, ms, detail)."""
        if not self._base:
            return False, 0.0, "MIDDLEWARE_URL not set"
        t0 = time.perf_counter()
        from config import CLUB_ID
        data = await self._get("/clubinfo", {"clubId": str(CLUB_ID), "platform": self.platform},
                               bypass_cache=True, allow_stale=False)
        ms = (time.perf_counter() - t0) * 1000
        return (data is not None), ms, ("ok" if data is not None else (self.last_error or "no data"))

    # ------------------------------------------------------------------ #
    #  Endpoints
    # ------------------------------------------------------------------ #
    async def get_club_info(self, club_id: int) -> Optional[dict]:
        data = await self._get("/clubinfo", {"clubId": str(club_id), "platform": self.platform})
        if isinstance(data, dict) and str(club_id) in data:
            return data[str(club_id)]
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_recent_matches(
        self, club_id: int, match_type: str = "leagueMatch", count: int = 5,
        bypass_cache: bool = False, allow_stale: bool = True,
    ) -> Optional[list]:
        data = await self._get(
            "/matches",
            {"clubId": str(club_id), "matchType": match_type, "count": str(count), "platform": self.platform},
            bypass_cache=bypass_cache, allow_stale=allow_stale,
        )
        return data if isinstance(data, list) else None

    async def get_recent_matches_multi(
        self, club_id: int, match_types: Optional[list[str]] = None, count: int = 5,
        bypass_cache: bool = False, allow_stale: bool = True,
    ) -> Optional[list]:
        """
        EA partitions match history by matchType, so "leagueMatch" alone misses
        playoff games. Fetch each type (in parallel), tag each match with its
        type, dedupe, sort newest-first, return the top `count`.
        Returns None only if *every* request failed.
        (Sequential on purpose: the stale-data flag is a context variable, and
        asyncio.gather would run each fetch in a copied context and lose it.)
        """
        if match_types is None:
            from config import MATCH_TYPES
            match_types = MATCH_TYPES

        results = [
            await self.get_recent_matches(club_id, match_type=mt, count=count,
                                          bypass_cache=bypass_cache, allow_stale=allow_stale)
            for mt in match_types
        ]
        if all(r is None for r in results):
            return None

        seen, merged = set(), []
        for mt, matches in zip(match_types, results):
            for m in matches or []:
                mid = str(m.get("matchId") or m.get("timestamp") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                m.setdefault("_matchType", mt)
                merged.append(m)

        def ts(m: dict) -> int:
            try:
                return int(m.get("timestamp", 0))
            except (TypeError, ValueError):
                return 0

        merged.sort(key=ts, reverse=True)
        return merged[:count]

    async def get_member_stats(self, club_id: int, career: bool = False, bypass_cache: bool = False) -> Optional[list]:
        path = "/members/career" if career else "/members"
        data = await self._get(path, {"clubId": str(club_id), "platform": self.platform}, bypass_cache=bypass_cache)
        if isinstance(data, dict) and "members" in data:
            return data["members"]
        if isinstance(data, list):
            return data
        return None

    async def get_overall_stats(self, club_id: int) -> Optional[dict]:
        data = await self._get("/overallstats", {"clubId": str(club_id), "platform": self.platform})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and str(club_id) in data:
            return data[str(club_id)]
        return None
