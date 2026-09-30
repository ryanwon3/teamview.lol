"""Matchup, synergy and meta data for the draft helper, from any source.

OP.gg fills these when it is reachable (see `opgg.py`). Otherwise `from_match_cache`
builds them from the ranked games the scouting tool already cached. Win rates from
small samples are pulled toward 50% so a 2-0 record doesn't look like a hard counter.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from .champions import champ_key

PRIOR_GAMES = 10  # games' worth of 50% added to large samples (OP.gg)
CACHE_PRIOR_GAMES = 30  # our own cached games are few, so trust them less
REMAKE_SECONDS = 300


def shrunk(wins: float, games: float, prior: float = PRIOR_GAMES) -> float:
    return (wins + prior * 0.5) / (games + prior) if games + prior else 0.5


@dataclass
class Rate:
    winrate: float  # already shrunk toward 50%
    games: int
    source: str


@dataclass
class MetaEntry:
    tier: int | None = None  # 1 (best) to 5
    winrate: float | None = None
    pickrate: float | None = None
    banrate: float | None = None

    def strength(self) -> float:
        """0-1: how strong this champion is in this role on the current patch."""
        if self.tier is not None:
            return {0: 1.0, 1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}.get(self.tier, 0.2)
        if self.winrate is not None:
            return min(max((self.winrate - 0.47) / 0.06, 0.0), 1.0)
        return 0.5


@dataclass
class DraftData:
    """Everything the scorer knows beyond the champion tags and scouting pools."""
    lane: dict[tuple[str, str, str], Rate] = field(default_factory=dict)  # (role, champ, opponent)
    duo: dict[frozenset, Rate] = field(default_factory=dict)  # same-team pairs
    meta: dict[str, dict[str, MetaEntry]] = field(default_factory=dict)  # role -> champ -> entry
    sources: set[str] = field(default_factory=set)

    def matchup(self, role: str, champ: str, opponent: str) -> Rate | None:
        """Win rate of `champ` against `opponent` in `role`, from either side's data."""
        champ, opponent = champ_key(champ), champ_key(opponent)
        direct = self.lane.get((role, champ, opponent))
        if direct:
            return direct
        flipped = self.lane.get((role, opponent, champ))
        if flipped:
            return Rate(1 - flipped.winrate, flipped.games, flipped.source)
        return None

    def add_matchup(self, role: str, champ: str, opponent: str, winrate: float, games: int, source: str):
        key = (role, champ_key(champ), champ_key(opponent))
        existing = self.lane.get(key)
        if existing is None or games >= existing.games:
            self.lane[key] = Rate(winrate, games, source)
        self.sources.add(source)

    def synergy(self, a: str, b: str) -> Rate | None:
        return self.duo.get(frozenset((champ_key(a), champ_key(b))))

    def add_synergy(self, a: str, b: str, winrate: float, games: int, source: str):
        key = frozenset((champ_key(a), champ_key(b)))
        existing = self.duo.get(key)
        if existing is None or games >= existing.games:
            self.duo[key] = Rate(winrate, games, source)
        self.sources.add(source)

    def meta_entry(self, role: str, champ: str) -> MetaEntry | None:
        return self.meta.get(role, {}).get(champ_key(champ))

    def has_meta(self) -> bool:
        return any(self.meta.values())

    def merge(self, other: "DraftData") -> "DraftData":
        """Fill gaps in this data with `other` (this one wins where both have a value)."""
        for k, v in other.lane.items():
            self.lane.setdefault(k, v)
        for k, v in other.duo.items():
            self.duo.setdefault(k, v)
        for role, entries in other.meta.items():
            for champ, entry in entries.items():
                self.meta.setdefault(role, {}).setdefault(champ, entry)
        self.sources |= other.sources
        return self


def from_matches(matches: list[dict], source: str = "cached ranked games",
                 min_games: int = 3) -> DraftData:
    """Lane matchups and duo win rates from raw Match-V5 match objects."""
    lane = defaultdict(lambda: [0, 0])  # (role, champ, opp) -> [wins, games]
    duo = defaultdict(lambda: [0, 0])
    seen = set()
    for match in matches:
        info = match.get("info") or {}
        match_id = (match.get("metadata") or {}).get("matchId")
        if match_id in seen or info.get("gameDuration", 0) < REMAKE_SECONDS:
            continue
        seen.add(match_id)
        teams = defaultdict(dict)  # teamId -> role -> participant
        for p in info.get("participants", []):
            if p.get("teamPosition"):
                teams[p["teamId"]][p["teamPosition"]] = p
        if len(teams) != 2:
            continue
        a, b = teams.values()
        for role in set(a) & set(b):
            for me, them in ((a[role], b[role]), (b[role], a[role])):
                rec = lane[(role, champ_key(me["championName"]), champ_key(them["championName"]))]
                rec[0] += int(me["win"])
                rec[1] += 1
        for team in (a, b):
            for p, q in combinations(team.values(), 2):
                rec = duo[frozenset((champ_key(p["championName"]), champ_key(q["championName"])))]
                rec[0] += int(p["win"])
                rec[1] += 1

    data = DraftData()
    for (role, champ, opp), (wins, games) in lane.items():
        if games >= min_games:
            data.add_matchup(role, champ, opp, shrunk(wins, games, CACHE_PRIOR_GAMES), games, source)
    for pair, (wins, games) in duo.items():
        if games >= min_games and len(pair) == 2:
            a, b = tuple(pair)
            data.add_synergy(a, b, shrunk(wins, games, CACHE_PRIOR_GAMES), games, source)
    return data


def from_match_cache(cache_path: str = "cache.db") -> DraftData:
    """Read every finished match the scouting tool cached and build fallback stats."""
    try:
        conn = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT body FROM responses WHERE key LIKE '%/lol/match/v5/matches/%' "
            "AND key NOT LIKE '%/ids%'"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return DraftData()
    matches = []
    for (body,) in rows:
        try:
            m = json.loads(body)
        except ValueError:
            continue
        if isinstance(m, dict) and "info" in m:
            matches.append(m)
    return from_matches(matches)
