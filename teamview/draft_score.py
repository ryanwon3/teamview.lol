"""Scores picks and bans for the current step of a draft and explains each score.

Pure functions only (no network or Streamlit), so everything here is unit tested.

A pick candidate is a (champion, role) pair scored 0-100 from these signals:

- comfort:  how well our player in that role plays it (scouting: games, win rate, mastery)
- meta:     how strong it is in that role this patch (OP.gg tiers)
- counter:  its lane matchup against an enemy pick we can already see
- blind:    a penalty for how hard it can be countered by what the enemy laner still has
            in their pool (only when their laner hasn't picked yet)
- comp:     how much it fills what our comp is missing (frontline, engage, damage mix...)
- synergy:  known combos with champions we already picked
- denial:   how much the enemy players want it
- flex:     early picks that could go to two of our open roles hide our plan

Weights move with the draft: early picks favor meta, flex and blind safety; late picks
favor counters and comp fit. Bans score a champion by how much a specific enemy
player wants it, how strong it is, and whether it counters what we already locked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import permutations

from .analysis import ROLE_LABELS, ROLE_ORDER
from .analysis import assign_roles as seat_players
from .champions import Champion, ChampionDB, champ_key
from .draft import DraftState
from .matchups import DraftData

MASTERY_FLOORS = ((500_000, 0.6), (200_000, 0.45), (75_000, 0.3))


# ---------- rosters from scouting ----------

@dataclass
class PoolEntry:
    champion: str
    games: int = 0
    wins: int = 0
    mastery: int = 0

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    def comfort(self) -> float:
        """0-1. About 0.35 at 3 games with a winning record, 0.7 at 9 games, 0.9 at 18."""
        recent = 0.0
        if self.games:
            smoothed = (self.wins + 2) / (self.games + 4)
            recent = 1 - math.exp(-(self.games * smoothed / 0.5) / 8)
        floor = next((f for points, f in MASTERY_FLOORS if self.mastery >= points), 0.0)
        return max(recent, floor)

    def describe(self) -> str:
        if self.games:
            return f"{self.games} games, {self.winrate:.0%}"
        return f"{self.mastery // 1000}k mastery"


@dataclass
class Player:
    name: str
    pool: dict[str, PoolEntry] = field(default_factory=dict)

    @property
    def short(self) -> str:
        return self.name.split("#")[0]

    def comfort(self, champ: str) -> float:
        entry = self.pool.get(champ_key(champ))
        return entry.comfort() if entry else 0.0

    def entry(self, champ: str) -> PoolEntry | None:
        return self.pool.get(champ_key(champ))

    def best(self, n: int = 3, exclude: set[str] = frozenset()) -> list[PoolEntry]:
        ranked = sorted((e for k, e in self.pool.items() if k not in exclude),
                        key=lambda e: -e.comfort())
        return [e for e in ranked if e.comfort() >= 0.2][:n]


Roster = dict  # Riot position -> Player


def player_from_report(report) -> Player:
    """Build a Player from a scouting PlayerReport (champion stats plus mastery)."""
    player = Player(report.riot_id)
    for c in report.champions:
        player.pool[champ_key(c.champion)] = PoolEntry(c.champion, c.games, c.wins)
    for name, points in report.mastery:
        entry = player.pool.setdefault(champ_key(name), PoolEntry(name))
        entry.mastery = points
    return player


def default_roles(reports, in_order: bool = False) -> dict[str, str]:
    """Position -> Riot ID, seated the same way as the scouting page's lane-by-lane table.

    `in_order` reads a roster of five or more as Top, Jungle, Mid, ADC, Support, like the
    scouting page's "Rosters are in role order" box. Otherwise players with no recent ranked
    games fill whatever roles are left.
    """
    out = {role: p.riot_id for role, p in seat_players(reports, in_order).items()}
    if in_order and len(reports) >= len(ROLE_ORDER):
        return out
    leftovers = [r.riot_id for r in reports if r.found and r.riot_id not in out.values()]
    for role in ROLE_ORDER:
        if role not in out and leftovers:
            out[role] = leftovers.pop(0)
    return out


def roster_from_reports(reports, roles: dict[str, str] | None = None, in_order: bool = False) -> Roster:
    """`roles` maps a position to a Riot ID; defaults to `default_roles`."""
    roles = roles if roles is not None else default_roles(reports, in_order)
    by_id = {r.riot_id: r for r in reports if r.found}
    return {role: player_from_report(by_id[rid]) for role, rid in roles.items() if rid in by_id}


# ---------- composition ----------

ARCHETYPES = {
    "Engage and teamfight": ("engage", "frontline"),
    "Poke and siege": ("poke", "waveclear"),
    "Pick": ("pick",),
    "Split push": ("split",),
    "Protect the carry": ("peel",),
}
NEED_WEIGHTS = {"frontline": 1.0, "engage": 0.8, "damage": 1.0, "waveclear": 0.5, "peel": 0.6}


@dataclass
class CompProfile:
    size: int
    totals: dict[str, int]
    max_engage: int
    ap_share: float | None  # share of damage that is magic, None with no damage dealers yet
    archetype: str | None
    scaling: str | None
    missing: list[str]


def _damage_split(champs: list[Champion]) -> tuple[float, float]:
    ad = ap = 0.0
    for c in champs:
        weight = 1 - 0.15 * c.rating("frontline")  # tanks deal less of the team's damage
        if c.damage == "AD":
            ad += weight
        elif c.damage == "AP":
            ap += weight
        else:
            ad += weight / 2
            ap += weight / 2
    return ad, ap


def need_deficits(champs: list[Champion]) -> dict[str, float]:
    """0-1 per need, where 0 means a full team would have enough of it."""
    total = {k: sum(c.rating(k) for c in champs) for k in ("frontline", "engage", "peel", "waveclear")}
    max_engage = max((c.rating("engage") for c in champs), default=0)
    ad, ap = _damage_split(champs)
    ap_share = ap / (ad + ap) if ad + ap else 0.5
    carries_need_peel = any(c.plays("BOTTOM") and c.rating("scaling") == 3 for c in champs)
    return {
        "frontline": max(0, 4 - total["frontline"]) / 4,
        "engage": 0.6 * max(0, 3 - max_engage) / 3 + 0.4 * max(0, 5 - total["engage"]) / 5,
        "damage": min(1.0, max(0.0, 0.3 - ap_share, ap_share - 0.7) / 0.3),
        "waveclear": max(0, 7 - total["waveclear"]) / 7,
        "peel": max(0, 3 - total["peel"]) / 3 * (1.0 if carries_need_peel else 0.5),
    }


def comp_gap(champs: list[Champion]) -> float:
    d = need_deficits(champs)
    return sum(NEED_WEIGHTS[k] * v for k, v in d.items()) / sum(NEED_WEIGHTS.values())


def archetype_scores(champs: list[Champion]) -> dict[str, float]:
    if not champs:
        return {}
    return {name: sum(c.rating(a) for c in champs for a in attrs) / (3 * len(attrs) * len(champs))
            for name, attrs in ARCHETYPES.items()}


def comp_profile(champs: list[Champion]) -> CompProfile:
    totals = {k: sum(c.rating(k) for c in champs) for k in ("frontline", "engage", "peel", "poke", "waveclear",
                                                             "pick", "split")}
    ad, ap = _damage_split(champs)
    arch = archetype_scores(champs)
    top = max(arch, key=arch.get) if arch else None
    scaling = None
    if champs:
        avg = sum(c.rating("scaling") for c in champs) / len(champs)
        scaling = "Early game" if avg < 1.7 else "Late game" if avg > 2.3 else "Mid game"
    missing = []
    if champs:
        d = need_deficits(champs)
        if max(c.rating("frontline") for c in champs) < 2:
            missing.append("No frontline")
        if max(c.rating("engage") for c in champs) < 2:
            missing.append("No hard engage")
        if len(champs) >= 2 and d["damage"] >= 0.5:
            missing.append("All physical damage" if ap / (ad + ap) < 0.5 else "All magic damage")
    return CompProfile(
        size=len(champs), totals=totals,
        max_engage=max((c.rating("engage") for c in champs), default=0),
        ap_share=ap / (ad + ap) if ad + ap else None,
        archetype=top if top and len(champs) >= 2 and arch[top] >= 0.3 else None,
        scaling=scaling, missing=missing,
    )


# ---------- recommendations ----------

@dataclass
class Reason:
    text: str
    tone: str = "good"  # good, bad or info
    weight: float = 0.0


@dataclass
class Rec:
    key: str
    champion: str
    role: str | None
    score: int
    reasons: list[Reason]
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, "") if self.role else ""


@dataclass
class DraftContext:
    state: DraftState
    champs: ChampionDB
    data: DraftData = field(default_factory=DraftData)
    ours: Roster = field(default_factory=dict)
    theirs: Roster = field(default_factory=dict)
    synergy_notes: dict = field(default_factory=dict)
    _roles_cache: dict = field(default_factory=dict, repr=False)

    def roster(self, us: bool) -> Roster:
        return self.ours if us else self.theirs

    def roles(self, us: bool) -> dict[str, str]:
        """Champion key -> position for a team's picks so far."""
        cache_key = (us, tuple(self.state.entries), tuple(sorted(self.state.role_overrides.items())))
        if cache_key not in self._roles_cache:
            self._roles_cache[cache_key] = assign_pick_roles(
                self.state.picks(us), self.roster(us), self.champs, self.state.role_overrides)
        return self._roles_cache[cache_key]

    def open_roles(self, us: bool) -> list[str]:
        taken = set(self.roles(us).values())
        return [r for r in ROLE_ORDER if r not in taken]

    def meta_strength(self, champ: str, role: str) -> float:
        entry = self.data.meta_entry(role, champ)
        if entry:
            return entry.strength()
        return 0.25 if self.data.meta.get(role) else 0.5


def assign_pick_roles(picks: list[str], roster: Roster, champs: ChampionDB,
                 overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Most likely position for each pick: champion's usual roles plus who plays it on the roster."""
    overrides = overrides or {}
    fixed = {k: overrides[k] for k in picks if k in overrides}
    free = [k for k in picks if k not in fixed]
    roles_left = [r for r in ROLE_ORDER if r not in fixed.values()]

    def likelihood(k, role):
        p = champs.get(k).role_fit(role)
        player = roster.get(role)
        if player:
            p += 1.5 * player.comfort(k)
        return math.log(p)

    best, best_score = {}, -math.inf
    for perm in permutations(roles_left, min(len(free), len(roles_left))):
        score = sum(likelihood(k, r) for k, r in zip(free, perm))
        if score > best_score:
            best, best_score = dict(zip(free, perm)), score
    return {**best, **fixed}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _rate_text(winrate: float, games: int) -> str:
    """"53%", or "53% over 12 games" when the sample is small enough to matter."""
    return f"{winrate:.0%}" + (f" over {games} games" if games < 200 else "")


def _finish(key, champ, role, parts, weights, penalties, reasons) -> Rec:
    positive = sum(weights.values())
    raw = sum(weights[k] * parts.get(k, 0.0) for k in weights)
    raw -= sum(w * parts.get(k, 0.0) for k, w in penalties.items())
    reasons.sort(key=lambda r: -r.weight)
    return Rec(key, champ.name, role, round(100 * _clamp(raw / positive)), reasons[:4], parts)


def pick_weights(early: bool) -> tuple[dict[str, float], dict[str, float]]:
    weights = {
        "comfort": 0.28,
        "meta": 0.18 if early else 0.12,
        "counter": 0.20,
        "comp": 0.10 if early else 0.22,
        "synergy": 0.08,
        "denial": 0.06,
        "flex": 0.05 if early else 0.0,
    }
    return weights, {"blind": 0.20}


def score_pick(ctx: DraftContext, key: str, role: str, us: bool = True) -> Rec:
    state, data, champs = ctx.state, ctx.data, ctx.champs
    champ = champs.get(key)
    mine, theirs = ctx.roster(us), ctx.roster(not us)
    my_roles, their_roles = ctx.roles(us), ctx.roles(not us)
    their_lane = {r: k for k, r in their_roles.items()}
    my_open = [r for r in ROLE_ORDER if r not in my_roles.values()]
    their_open = [r for r in ROLE_ORDER if r not in their_roles.values()]
    their_picks_after = state.picks_left(not us, state.step + 1)
    early = their_picks_after >= 3
    weights, penalties = pick_weights(early)
    if not mine.get(role):
        weights["comfort"] = 0.0  # no scouting for this seat: score on the other signals alone
    if not theirs:
        weights["denial"] = 0.0
    parts, reasons = {}, []
    role_name = ROLE_LABELS[role]

    # Comfort
    player = mine.get(role)
    parts["comfort"] = player.comfort(key) if player else 0.0
    if player:
        entry = player.entry(key)
        if entry and parts["comfort"] >= 0.3:
            reasons.append(Reason(f"{player.short}: {entry.describe()}", "good", parts["comfort"] * 0.28))
        elif player.pool:
            reasons.append(Reason(f"Not in {player.short}'s recent pool", "bad", 0.05))

    # Meta
    parts["meta"] = ctx.meta_strength(key, role)
    entry = data.meta_entry(role, key)
    if entry and entry.tier is not None and entry.tier <= 2:
        reasons.append(Reason(f"Tier {entry.tier} {role_name} this patch", "good", parts["meta"] * 0.15))
    elif data.meta.get(role) and not entry:
        reasons.append(Reason(f"Off-meta as {role_name}", "info", 0.02))

    # Counter, or blind risk if their laner is still to come
    parts["counter"] = 0.5
    opponent = their_lane.get(role)
    if opponent:
        rate = data.matchup(role, key, opponent)
        if rate:
            adv = rate.winrate - 0.5
            parts["counter"] = _clamp(0.5 + adv / 0.08)
            opp_name = champs.name(opponent)
            if adv >= 0.015:
                reasons.append(Reason(f"Beats {opp_name} in lane ({_rate_text(rate.winrate, rate.games)})",
                                      "good", adv * 6))
            elif adv <= -0.015:
                reasons.append(Reason(f"Loses lane to {opp_name} ({_rate_text(rate.winrate, rate.games)})",
                                      "bad", -adv * 6))
    parts["blind"] = 0.0
    if not opponent and role in their_open and their_picks_after > 0:
        parts["blind"], reason = blind_risk(ctx, key, role, us)
        if reason:
            reasons.append(reason)

    # Comp fit
    allies = [champs.get(k) for k in my_roles]
    before, after = need_deficits(allies), need_deficits(allies + [champ])
    delta = (sum(NEED_WEIGHTS[k] * (before[k] - after[k]) for k in before) / sum(NEED_WEIGHTS.values()))
    coherence = 0.0
    if len(allies) >= 2:
        arch = archetype_scores(allies)
        top = max(arch, key=arch.get)
        coherence = sum(champ.rating(a) for a in ARCHETYPES[top]) / (3 * len(ARCHETYPES[top]))
    parts["comp"] = _clamp(0.8 * _clamp(delta / 0.3) + 0.2 * coherence)
    gains = {k: NEED_WEIGHTS[k] * (before[k] - after[k]) for k in before}
    best_need = max(gains, key=gains.get)
    if allies and gains[best_need] >= 0.15:
        label = {
            "frontline": "Adds the frontline you need",
            "engage": "Adds engage",
            "damage": "Adds magic damage" if champ.damage == "AP" else "Adds physical damage",
            "waveclear": "Adds waveclear",
            "peel": "Adds peel for your carry",
        }[best_need]
        reasons.append(Reason(label, "good", gains[best_need] * 0.3))
    my_picks_left = state.picks_left(us)
    if my_picks_left == 1 and after["frontline"] >= 0.75:
        reasons.append(Reason("Leaves the comp with no frontline", "bad", 0.2))
    elif my_picks_left == 1 and after["damage"] >= 0.8:
        reasons.append(Reason("Leaves the comp one damage type", "bad", 0.2))

    # Synergy with our picks
    parts["synergy"] = 0.0
    for ally_key in my_roles:
        note = ctx.synergy_notes.get(frozenset((key, ally_key)))
        rate = data.synergy(key, ally_key)
        value, text = 0.0, None
        if note:
            value, text = 0.8, f"Pairs with {champs.name(ally_key)}"
        if rate and rate.games >= 5 and rate.winrate > 0.5:
            v = _clamp((rate.winrate - 0.5) / 0.04)
            if v > value:
                value, text = v, f"Wins with {champs.name(ally_key)} ({_pct(rate.winrate)})"
        if value > parts["synergy"]:
            parts["synergy"] = value
            best_synergy = text
    if parts["synergy"] > 0:
        reasons.append(Reason(best_synergy, "good", parts["synergy"] * 0.1))

    # Denial: an enemy player whose role is open wants it
    parts["denial"] = 0.0
    for r in their_open:
        p = theirs.get(r)
        if p and p.comfort(key) > parts["denial"]:
            parts["denial"] = p.comfort(key)
            if parts["denial"] >= 0.5:
                denial_reason = Reason(f"Denies {p.short}'s {champ.name} ({p.entry(key).describe()})",
                                       "good", parts["denial"] * 0.08)
    if parts["denial"] >= 0.5:
        reasons.append(denial_reason)

    # Flex
    parts["flex"] = 0.0
    if early:
        flex_roles = [r for r in my_open if champ.plays(r)
                      and ((mine.get(r) and mine[r].comfort(key) >= 0.3) or ctx.meta_strength(key, r) >= 0.6)]
        if len(flex_roles) >= 2:
            parts["flex"] = 1.0
            reasons.append(Reason("Flex: " + " or ".join(ROLE_LABELS[r] for r in flex_roles), "good", 0.06))

    return _finish(key, champ, role, parts, weights, penalties, reasons)


def blind_risk(ctx: DraftContext, key: str, role: str, us: bool) -> tuple[float, Reason | None]:
    """0-1 penalty for picking `key` before the enemy laner in `role` has picked."""
    champs, data = ctx.champs, ctx.data
    opp_player = ctx.roster(not us).get(role)
    blocked = ctx.state.unavailable(not us) | {key}

    pool: list[tuple[str, float]] = []
    if opp_player:
        pool = [(k, e.comfort()) for k, e in opp_player.pool.items()
                if k not in blocked and e.comfort() >= 0.2]
    if not pool and data.meta.get(role):
        ranked = sorted(data.meta[role].items(), key=lambda kv: -kv[1].strength())
        pool = [(k, e.strength()) for k, e in ranked if k not in blocked][:8]

    rated = []
    for k, weight in pool:
        rate = data.matchup(role, key, k)
        if rate:
            rated.append((k, rate.winrate - 0.5, weight))

    if not rated:
        blind = champs.get(key).rating("blind")
        risk = (3 - blind) / 3 * 0.6
        if blind >= 3:
            return risk, Reason("Safe to blind pick", "good", 0.05)
        if blind == 0:
            return risk, Reason("Easy to counter; better picked late", "bad", 0.12)
        return risk, None

    worst_k, worst_adv, worst_w = min(rated, key=lambda t: t[1] * t[2])
    worst = max(0.0, -worst_adv * worst_w)
    total_w = sum(w for _, _, w in rated)
    expected = sum(max(0.0, -adv) * w for _, adv, w in rated) / total_w
    risk = _clamp((0.6 * worst + 0.4 * expected) / 0.04)
    who = f"{opp_player.short} plays" if opp_player else "They could pick"
    if risk >= 0.4:
        return risk, Reason(f"Blind risk: {who} {champs.name(worst_k)} ({_pct(0.5 - worst_adv)} for them)",
                            "bad", risk * 0.2)
    if opp_player and risk < 0.15 and len(rated) >= min(3, len(pool)):
        return risk, Reason(f"Blind-safe vs {opp_player.short}'s pool", "good", 0.1)
    return risk, None


def recommend_picks(ctx: DraftContext, us: bool = True, n: int = 8) -> list[Rec]:
    """Best (champion, role) picks for a team at the current step, one row per champion."""
    unavailable = ctx.state.unavailable(us)
    open_roles = ctx.open_roles(us)
    mine = ctx.roster(us)
    best: dict[str, Rec] = {}
    for key in ctx.champs.keys():
        if key in unavailable:
            continue
        champ = ctx.champs.get(key)
        for role in open_roles:
            player = mine.get(role)
            if not champ.plays(role) and not (player and player.comfort(key) >= 0.3):
                continue
            rec = score_pick(ctx, key, role, us)
            if key not in best or rec.score > best[key].score:
                best[key] = rec
    return sorted(best.values(), key=lambda r: (-r.score, r.champion))[:n]


def recommend_bans(ctx: DraftContext, us: bool = True, n: int = 8) -> list[Rec]:
    """Best bans for a team (`us`) against the other team at the current step."""
    state, champs, data = ctx.state, ctx.champs, ctx.data
    mine, theirs = ctx.roster(us), ctx.roster(not us)
    my_roles = ctx.roles(us)
    their_open = ctx.open_roles(not us)
    my_open = ctx.open_roles(us)
    phase2 = state.step >= 12
    weights = ({"target": 0.45, "meta": 0.20, "protect": 0.35} if phase2
               else {"target": 0.50, "meta": 0.35, "protect": 0.0})
    penalties = {"keep": 0.25}  # scaled below so their big threats still get banned
    skip = state.taken() | state.fearless_locked(not us)  # no point banning what they can't pick

    recs = []
    for key in champs.keys():
        if key in skip:
            continue
        champ = champs.get(key)
        roles = [r for r in their_open
                 if champ.plays(r) or (theirs.get(r) and theirs[r].comfort(key) >= 0.3)]
        if not roles:
            continue
        parts, reasons = {}, []

        parts["target"] = 0.0
        for r in roles:
            p = theirs.get(r)
            if p:
                value = p.comfort(key) * (0.6 + 0.4 * ctx.meta_strength(key, r))
                if value > parts["target"]:
                    parts["target"] = value
                    target_reason = Reason(f"{p.short}'s pick: {p.entry(key).describe()}", "good", value * 0.5)
        if parts["target"] >= 0.25:
            reasons.append(target_reason)

        parts["meta"] = max(ctx.meta_strength(key, r) for r in roles)
        best_role = max(roles, key=lambda r: ctx.meta_strength(key, r))
        entry = data.meta_entry(best_role, key)
        if entry and entry.tier is not None and entry.tier <= 1:
            reasons.append(Reason(f"Tier {entry.tier} {ROLE_LABELS[best_role]} this patch", "good", 0.15))
        elif entry and entry.banrate and entry.banrate >= 0.2:
            reasons.append(Reason(f"Banned in {_pct(entry.banrate)} of games", "info", 0.1))

        parts["protect"] = 0.0
        for ally_key, ally_role in my_roles.items():
            if ally_role not in roles:
                continue
            rate = data.matchup(ally_role, ally_key, key)
            if rate:
                value = _clamp((0.5 - rate.winrate) / 0.05)
                if value > parts["protect"]:
                    parts["protect"] = value
                    protect_reason = Reason(f"Counters your {champs.name(ally_key)} "
                                            f"({_pct(1 - rate.winrate)} for them)", "good", value * 0.35)
        if parts["protect"] >= 0.3:
            reasons.append(protect_reason)

        if phase2 and parts["target"] >= 0.25 and len(their_open) < 5:
            reasons.append(Reason(f"Their {ROLE_LABELS[roles[0]]} hasn't picked yet", "info", 0.01))

        parts["keep"] = 0.0
        for r in my_open:
            p = mine.get(r)
            if p and p.comfort(key) > parts["keep"]:
                parts["keep"] = p.comfort(key)
                if parts["keep"] >= 0.5:
                    keep_reason = Reason(f"{p.short} plays it too; maybe pick it instead", "info", 0.03)
        if parts["keep"] >= 0.5:
            reasons.append(keep_reason)
        parts["keep"] *= 1 - parts["target"]

        if parts["target"] < 0.1 and parts["protect"] < 0.1 and not data.has_meta():
            continue  # nothing says this ban matters
        recs.append(_finish(key, champ, None, parts, weights, penalties, reasons))
    return sorted(recs, key=lambda r: (-r.score, r.champion))[:n]


@dataclass
class Watch:
    role: str
    player: str
    champions: list[PoolEntry]


def watch_list(ctx: DraftContext, us: bool = True) -> list[Watch]:
    """For each role the enemy hasn't filled, the champions that player is best on."""
    theirs = ctx.roster(not us)
    blocked = ctx.state.unavailable(not us)
    out = []
    for role in ctx.open_roles(not us):
        p = theirs.get(role)
        if p:
            best = p.best(3, exclude=blocked)
            if best:
                out.append(Watch(role, p.short, best))
    return out
