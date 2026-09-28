"""Thin Riot API client with rate limiting and a SQLite response cache."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from urllib.parse import quote

import requests

# Platform (e.g. na1) -> regional routing value used by Account-V1 and Match-V5.
PLATFORM_TO_REGION = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

QUEUE_SOLO = 420
QUEUE_FLEX = 440

# Cache lifetimes in seconds. Finished matches never change, so they never expire.
TTL_ACCOUNT = 7 * 24 * 3600
TTL_RANK = 30 * 60
TTL_MASTERY = 6 * 3600
TTL_MATCH_IDS = 30 * 60
TTL_FOREVER = None


class RiotError(Exception):
    pass


class RateLimiter:
    """Sliding-window limiter covering every window in the key's limit (dev key: 20/1s and 100/120s)."""

    def __init__(self, windows=((20, 1.0), (100, 120.0))):
        self.windows = [(limit, period, deque()) for limit, period in windows]
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                wait = 0.0
                for limit, period, calls in self.windows:
                    while calls and now - calls[0] >= period:
                        calls.popleft()
                    if len(calls) >= limit:
                        wait = max(wait, period - (now - calls[0]))
                if wait <= 0:
                    for _, _, calls in self.windows:
                        calls.append(now)
                    return
            time.sleep(wait + 0.05)


class Cache:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT, fetched REAL)"
        )
        self.lock = threading.Lock()

    def get(self, key: str, ttl: float | None):
        with self.lock:
            row = self.conn.execute(
                "SELECT body, fetched FROM responses WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        body, fetched = row
        if ttl is not None and time.time() - fetched > ttl:
            return None
        return json.loads(body)

    def put(self, key: str, value):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
                (key, json.dumps(value), time.time()),
            )
            self.conn.commit()


class RiotClient:
    def __init__(self, api_key: str, platform: str = "na1", cache_path: str = "cache.db",
                 limiter: RateLimiter | None = None):
        if not api_key:
            raise RiotError("Missing Riot API key. Set RIOT_API_KEY in .env.")
        self.platform = platform
        self.region = PLATFORM_TO_REGION[platform]
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = api_key
        self.cache = Cache(cache_path)
        self.limiter = limiter or RateLimiter()

    def _get(self, host: str, path: str, ttl: float | None, params: dict | None = None):
        url = f"https://{host}.api.riotgames.com{path}"
        key = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
        cached = self.cache.get(key, ttl)
        if cached is not None:
            return cached

        for _ in range(5):
            self.limiter.acquire()
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 5)))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403):
                raise RiotError(
                    "Riot rejected the API key (dev keys expire every 24 hours). "
                    "Get a fresh one at developer.riotgames.com."
                )
            if resp.status_code >= 500:
                time.sleep(2)
                continue
            resp.raise_for_status()
            data = resp.json()
            self.cache.put(key, data)
            return data
        raise RiotError(f"Riot API kept failing for {path}")

    def account(self, game_name: str, tag_line: str):
        path = f"/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        return self._get(self.region, path, TTL_ACCOUNT)

    def league_entries(self, puuid: str):
        return self._get(self.platform, f"/lol/league/v4/entries/by-puuid/{puuid}", TTL_RANK) or []

    def top_mastery(self, puuid: str, count: int = 10):
        path = f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top"
        return self._get(self.platform, path, TTL_MASTERY, {"count": count}) or []

    def match_ids(self, puuid: str, queue: int, count: int):
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        return self._get(self.region, path, TTL_MATCH_IDS, {"queue": queue, "count": count}) or []

    def match(self, match_id: str):
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}", TTL_FOREVER)


def champion_names(session: requests.Session | None = None) -> dict[int, str]:
    """Champion id -> display name, from Data Dragon (used to label mastery entries)."""
    s = session or requests.Session()
    version = s.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=15).json()[0]
    data = s.get(
        f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json", timeout=15
    ).json()["data"]
    return {int(c["key"]): c["name"] for c in data.values()}
