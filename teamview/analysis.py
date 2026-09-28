"""Turns raw Riot API data into player and team scouting reports.

Everything here is pure (no network), so it can be unit tested with fixtures.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import permutations

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND"]
APEX_TIERS = ["MASTER", "GRANDMASTER", "CHALLENGER"]
DIVISIONS = ["IV", "III", "II", "I"]
APEX_BASE = len(TIERS) * 400  # Master 0 LP

ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_LABELS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}

# How much recent win rate moves a player's strength, in LP-equivalent points.
# A 60% win rate over 20+ games adds 80 points (almost one division).
FORM_WEIGHT = 800
FORM_FULL_GAMES = 20
REMAKE_SECONDS = 300


def parse_riot_ids(text: str, default_tag: str = "NA1") -> list[tuple[str, str]]:
    """Parse Riot IDs from free text: one per line or comma separated.

    Also accepts pasted lobby chat ("Name #TAG joined the lobby"). IDs without a
    tag get `default_tag`.
    """
    ids, seen = [], set()
    for raw in re.split(r"[\n,]", text):
        entry = re.sub(r"\s+(joined|left) the lobby\.?$", "", raw.strip(), flags=re.I).strip()
        if not entry:
            continue
        name, _, tag = entry.partition("#")
        name, tag = name.strip(), (tag.strip() or default_tag)
        if not name:
            continue
        key = (name.lower(), tag.lower())
        if key not in seen:
            seen.add(key)
            ids.append((name, tag))
    return ids


def rank_score(tier: str, division: str, lp: int) -> int:
    """Map a rank to one number: 100 points per division, 400 per tier, LP on top."""
    tier = tier.upper()
    if tier in APEX_TIERS:
        return APEX_BASE + lp
    return TIERS.index(tier) * 400 + DIVISIONS.index(division) * 100 + min(lp, 100)


def score_label(score: float) -> str:
    """Inverse of rank_score, for showing a team average as a rank."""
    score = round(score)
    if score >= APEX_BASE:
        return f"Master+ {score - APEX_BASE} LP"
    score = max(score, 0)
    tier = TIERS[min(score // 400, len(TIERS) - 1)]
    division = DIVISIONS[min((score % 400) // 100, 3)]
    return f"{tier.title()} {division} {score % 100} LP"


@dataclass
class ChampStats:
    champion: str
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    cs: int = 0
    minutes: float = 0.0

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(self.deaths, 1)

    @property
    def cs_per_min(self) -> float:
        return self.cs / self.minutes if self.minutes else 0.0

    @property
    def threat(self) -> float:
        """Comfort times success: games played, scaled by a smoothed win rate.

        Equals `games` at a 50% win rate. The +2/+4 smoothing stops a 1-0 record
        from counting as a 100% win rate.
        """
        smoothed = (self.wins + 2) / (self.games + 4)
        return self.games * smoothed / 0.5


@dataclass
class PlayerReport:
    riot_id: str
    puuid: str | None
    found: bool = True
    queue: str | None = None  # "Solo" or "Flex": whichever rank we used
    tier: str | None = None
    division: str | None = None
    lp: int = 0
    ranked_wins: int = 0
    ranked_losses: int = 0
    champions: list[ChampStats] = field(default_factory=list)
    roles: Counter = field(default_factory=Counter)
    recent_games: int = 0
    recent_wins: int = 0
    mastery: list[tuple[str, int]] = field(default_factory=list)  # (champion, points)

    @property
    def rank_label(self) -> str:
        if not self.tier:
            return "Unranked"
        if self.tier in APEX_TIERS:
            return f"{self.tier.title()} {self.lp} LP"
        return f"{self.tier.title()} {self.division} {self.lp} LP"

    @property
    def rank_points(self) -> int | None:
        return rank_score(self.tier, self.division, self.lp) if self.tier else None

    @property
    def recent_winrate(self) -> float | None:
        return self.recent_wins / self.recent_games if self.recent_games else None

    @property
    def strength(self) -> float | None:
        """Rank points plus a recent-form bonus. None when unranked."""
        base = self.rank_points
        if base is None:
            return None
        if not self.recent_games:
            return float(base)
        confidence = min(self.recent_games, FORM_FULL_GAMES) / FORM_FULL_GAMES
        return base + (self.recent_winrate - 0.5) * FORM_WEIGHT * confidence

    @property
    def main_role(self) -> str | None:
        return self.roles.most_common(1)[0][0] if self.roles else None


def build_player_report(riot_id: str, puuid: str | None, league_entries: list[dict],
                        masteries: list[dict], matches: list[dict],
                        champ_names: dict[int, str] | None = None) -> PlayerReport:
    if puuid is None:
        return PlayerReport(riot_id=riot_id, puuid=None, found=False)

    report = PlayerReport(riot_id=riot_id, puuid=puuid)

    by_queue = {e["queueType"]: e for e in league_entries}
    # Prefer solo queue; fall back to flex when a player only plays flex.
    for queue_type, label in (("RANKED_SOLO_5x5", "Solo"), ("RANKED_FLEX_SR", "Flex")):
        entry = by_queue.get(queue_type)
        if entry:
            report.queue = label
            report.tier, report.division = entry["tier"], entry["rank"]
            report.lp = entry["leaguePoints"]
            report.ranked_wins, report.ranked_losses = entry["wins"], entry["losses"]
            break

    names = champ_names or {}
    champs: dict[str, ChampStats] = {}
    for match in matches:
        info = match["info"]
        if info["gameDuration"] < REMAKE_SECONDS:
            continue
        me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
        if me is None:
            continue
        # championName is Riot's internal key ("MonkeyKing", "JarvanIV"), so label by id with
        # the Data Dragon display name ("Wukong", "Jarvan IV") when we have it, as mastery does.
        champion = names.get(me.get("championId"), me["championName"])
        stats = champs.setdefault(champion, ChampStats(champion))
        stats.games += 1
        stats.wins += int(me["win"])
        stats.kills += me["kills"]
        stats.deaths += me["deaths"]
        stats.assists += me["assists"]
        stats.cs += me["totalMinionsKilled"] + me["neutralMinionsKilled"]
        stats.minutes += info["gameDuration"] / 60
        if me.get("teamPosition"):
            report.roles[me["teamPosition"]] += 1
        report.recent_games += 1
        report.recent_wins += int(me["win"])

    report.champions = sorted(champs.values(), key=lambda c: (-c.games, -c.wins))
    report.mastery = [
        (names.get(m["championId"], str(m["championId"])), m["championPoints"]) for m in masteries
    ]
    return report


@dataclass
class Threat:
    champion: str
    player: str
    games: int
    winrate: float
    kda: float
    score: float


@dataclass
class TeamSummary:
    players: list[PlayerReport]
    avg_strength: float | None
    strongest: PlayerReport | None
    threats: list[Threat]
    unranked: list[str]
    missing: list[str]

    @property
    def avg_label(self) -> str:
        return score_label(self.avg_strength) if self.avg_strength is not None else "n/a"


def summarize_team(players: list[PlayerReport], min_games: int = 2, top_n: int = 10) -> TeamSummary:
    found = [p for p in players if p.found]
    rated = [p for p in found if p.strength is not None]
    avg = sum(p.strength for p in rated) / len(rated) if rated else None
    strongest = max(rated, key=lambda p: p.strength) if rated else None

    threats = [
        Threat(c.champion, p.riot_id, c.games, c.winrate, c.kda, c.threat)
        for p in found for c in p.champions if c.games >= min_games
    ]
    threats.sort(key=lambda t: -t.score)

    return TeamSummary(
        players=players,
        avg_strength=avg,
        strongest=strongest,
        threats=threats[:top_n],
        unranked=[p.riot_id for p in found if p.strength is None],
        missing=[p.riot_id for p in players if not p.found],
    )


def assign_roles(players: list[PlayerReport], in_order: bool = False) -> dict[str, PlayerReport]:
    """Seat each player in a different role.

    With `in_order`, a roster of five or more is read as Top, Jungle, Mid, ADC, Support, then
    subs, and a Riot ID that wasn't found leaves its lane empty. Otherwise (or with fewer than
    five players) roles come from recent games: every seating is tried and the one with the most
    recent games in each player's seat wins, so a mid who also plays top moves to top instead of
    colliding with another mid. With more than five players, the stronger lineup wins ties.
    """
    if in_order and len(players) >= len(ROLE_ORDER):
        return {role: p for role, p in zip(ROLE_ORDER, players) if p.found}

    seated = [p for p in players if p.found and p.roles]
    if len(seated) <= len(ROLE_ORDER):
        options = (dict(zip(roles, seated)) for roles in permutations(ROLE_ORDER, len(seated)))
    else:
        options = (dict(zip(ROLE_ORDER, lineup)) for lineup in permutations(seated, len(ROLE_ORDER)))

    def fit(option):
        return (sum(p.roles[r] for r, p in option.items()),
                sum(p.strength or 0 for p in option.values()))
    return max(options, key=fit, default={})


def lane_matchups(mine: list[PlayerReport], theirs: list[PlayerReport],
                  in_order: bool = False) -> list[dict]:
    """Seat both teams in the five roles and compare strength lane by lane."""
    my_roles, their_roles = assign_roles(mine, in_order), assign_roles(theirs, in_order)
    rows = []
    for role in ROLE_ORDER:
        a, b = my_roles.get(role), their_roles.get(role)
        if not a and not b:
            continue
        diff = (a.strength - b.strength) if a and b and a.strength is not None and b.strength is not None else None
        rows.append({
            "Role": ROLE_LABELS[role],
            "Us": f"{a.riot_id} ({a.rank_label})" if a else "?",
            "Them": f"{b.riot_id} ({b.rank_label})" if b else "?",
            "Edge": diff,
        })
    return rows


def ban_suggestions(summary: TeamSummary, n: int = 5) -> list[Threat]:
    """Top threats, one per champion, skipping repeats of the same champion."""
    seen, out = set(), []
    for t in summary.threats:
        if t.champion not in seen:
            seen.add(t.champion)
            out.append(t)
        if len(out) == n:
            break
    return out
