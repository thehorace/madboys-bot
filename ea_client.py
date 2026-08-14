"""
EA Pro Clubs API client.

EA's endpoints are unofficial and sit behind Akamai — requests must look like
browser traffic from proclubs.ea.com or they get blocked. We also cache
responses locally so we don't hammer EA and survive short outages.
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger("madboys-bot.ea")

BASE_URL = "https://proclubs.ea.com/api/fc"

# Akamai fingerprints requests heavily — we need to mimic a real Chrome browser
# session as closely as possible, including accept-language, sec-fetch headers,
# and a realistic cookie consent value.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://proclubs.ea.com/",
    "Origin": "https://proclubs.ea.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "DNT": "1",
    # Basic cookie consent — EA requires this to be set or it bounces requests
    "Cookie": "AKA_A2=A; notice_behavior=implied; notice_gdpr_prefs=0|1|2:1|2:1",
}

# Simple in-memory cache: {cache_key: (timestamp, data)}
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 600  # seconds (10 min) — don't hammer EA


def _cached(key: str) -> Optional[any]:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None


def _store(key: str, data: any) -> None:
    _cache[key] = (time.time(), data)


async def _get(session: aiohttp.ClientSession, url: str, params: dict) -> Optional[dict]:
    cache_key = url + str(sorted(params.items()))
    cached = _cached(cache_key)
    if cached is not None:
        log.debug(f"Cache hit: {cache_key}")
        return cached

    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                _store(cache_key, data)
                return data
            else:
                body = await resp.text()
                log.warning(f"EA API returned {resp.status} for {url} {params} — body: {body[:200]}")
                return None
    except asyncio.TimeoutError:
        log.warning(f"EA API timed out: {url}")
        return None
    except Exception as e:
        log.error(f"EA API error: {e}")
        return None


class EAClient:
    def __init__(self, platform: str = "common-gen5"):
        self.platform = platform
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # keepalive + limit connections like a real browser would
            connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": HEADERS["User-Agent"]},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_club_info(self, club_id: int) -> Optional[dict]:
        """Basic club info: name, members, skill rating, wins/losses/draws."""
        session = await self._get_session()
        data = await _get(session, f"{BASE_URL}/clubs/info", {
            "platform": self.platform,
            "clubIds": str(club_id),
        })
        if data and str(club_id) in data:
            return data[str(club_id)]
        # Some responses return a list
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_recent_matches(self, club_id: int, match_type: str = "leagueMatch", count: int = 5) -> Optional[list]:
        """
        Recent matches for a club.
        match_type: leagueMatch | friendlyMatch | playoffMatch
        """
        session = await self._get_session()
        data = await _get(session, f"{BASE_URL}/clubs/matches", {
            "platform": self.platform,
            "clubIds": str(club_id),
            "matchType": match_type,
            "maxResultCount": str(count),
        })
        if isinstance(data, list):
            return data
        return None

    async def get_member_stats(self, club_id: int) -> Optional[list]:
        """Per-player stats for everyone in the club."""
        session = await self._get_session()
        data = await _get(session, f"{BASE_URL}/members/stats", {
            "platform": self.platform,
            "clubId": str(club_id),
        })
        if data and "members" in data:
            return data["members"]
        if isinstance(data, list):
            return data
        return None

    async def get_overall_stats(self, club_id: int) -> Optional[dict]:
        """Season overall stats for the club."""
        session = await self._get_session()
        data = await _get(session, f"{BASE_URL}/clubs/overallStats", {
            "platform": self.platform,
            "clubIds": str(club_id),
        })
        if isinstance(data, list) and data:
            return data[0]
        if data and str(club_id) in data:
            return data[str(club_id)]
        return None
