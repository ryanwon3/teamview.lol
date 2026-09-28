from collections import Counter

import pytest

from teamview.analysis import PlayerReport
from teamview.champions import ChampionDB, champ_key, load_synergies
from teamview.draft import BAN, BLUE, PICK, RED, SEQUENCE, DraftState, slot_label
from teamview.draft_score import (DraftContext, Player, PoolEntry, assign_pick_roles, comp_profile, default_roles,
                                  need_deficits, recommend_bans, recommend_picks, roster_from_reports, score_pick,
                                  watch_list)
from teamview.matchups import DraftData, MetaEntry, from_matches

DB = ChampionDB.load()
K = champ_key


def player(name, *pool):
    return Player(name, {K(c): PoolEntry(c, g, w) for c, g, w in pool})


def context(state=None, data=None, ours=None, theirs=None):
    return DraftContext(state or DraftState(), DB, data or DraftData(), ours or {}, theirs or {}, load_synergies())


def play(state, *champs):
    for c in champs:
        state.apply(K(c) if c else None)
    return state


# ---------- champion data ----------

def test_champion_csv_covers_every_champion_with_valid_ratings():
    assert len(DB.keys()) == 173
    for key in DB.keys():
        c = DB.get(key)
        assert c.roles, c.name
        assert c.damage in ("AD", "AP", "MIX"), c.name
        assert all(0 <= v <= 3 for v in c.ratings.values()), c.name


def test_champ_key_matches_riot_ddragon_and_opgg_spellings():
    assert K("MonkeyKing") == K("Wukong") == K("MONKEY_KING")
    assert K("KSante") == K("K'Sante")
    assert K("Nunu") == K("Nunu & Willump")
    assert K("Renata") == K("Renata Glasc")
    assert K("JarvanIV") == K("Jarvan IV")
    assert DB.get("MonkeyKing").name == "Wukong"


def test_unknown_champion_gets_neutral_defaults():
    db = ChampionDB.load(extra_names=["Brandnewchamp"])
    c = db.get("Brandnewchamp")
    assert not c.known and c.rating("engage") == 1 and c.role_fit("TOP") == 0.3


def test_synergy_pairs_are_real_champions():
    for pair in load_synergies():
        assert all(k in DB.by_key for k in pair)


# ---------- draft order ----------

def test_sequence_is_standard_tournament_draft():
    assert Counter(SEQUENCE) == {(BLUE, BAN): 5, (RED, BAN): 5, (BLUE, PICK): 5, (RED, PICK): 5}
    picks = "".join("B" if s == BLUE else "R" for s, a in SEQUENCE if a == PICK)
    assert picks == "BRRBBRRBBR"
    bans2 = "".join("B" if s == BLUE else "R" for s, a in SEQUENCE[12:16])
    assert bans2 == "RBRB"
    assert [slot_label(i) for i in (0, 6, 7, 8, 19)] == ["B ban 1", "B1", "R1", "R2", "R5"]


def test_state_tracks_turns_picks_and_undo():
    s = DraftState(our_side=RED)
    assert not s.our_turn  # blue bans first
    play(s, "Azir", "Rumble", "Vi", None, "Kalista", "Poppy", "Ahri")
    assert s.bans(True) == [K("Rumble"), K("Poppy")]  # our skipped ban isn't listed
    assert s.bans(False) == [K("Azir"), K("Vi"), K("Kalista")]
    assert s.picks(False) == [K("Ahri")]
    assert s.our_turn and s.current == (RED, PICK)
    assert s.picks_left(True) == 5 and s.picks_left(False) == 4
    with pytest.raises(ValueError):
        s.apply(K("Azir"))
    s.undo()
    assert s.picks(False) == [] and s.step == 6


def test_fearless_locks():
    s = DraftState(fearless="hard", our_earlier={K("Ahri")}, their_earlier={K("Syndra")})
    assert s.unavailable(True) >= {K("Ahri"), K("Syndra")}
    s.fearless = "soft"
    assert K("Syndra") not in s.unavailable(True) and K("Ahri") in s.unavailable(True)
    assert K("Syndra") in s.unavailable(False)
    s.fearless = "off"
    assert not s.fearless_locked(True)


# ---------- roles ----------

def test_pick_roles_follow_champion_roles_and_roster():
    assert assign_pick_roles([K("Gragas"), K("Lee Sin")], {}, DB) == {K("Lee Sin"): "JUNGLE", K("Gragas"): "TOP"}
    # A support who plays Gragas makes Gragas a support pick
    roster = {"UTILITY": player("Sup", ("Gragas", 20, 12))}
    assert assign_pick_roles([K("Gragas")], roster, DB)[K("Gragas")] == "UTILITY"
    assert assign_pick_roles([K("Gragas")], {}, DB, {K("Gragas"): "MIDDLE"})[K("Gragas")] == "MIDDLE"


def report(riot_id, role_games, champs=(), mastery=()):
    r = PlayerReport(riot_id=riot_id, puuid=riot_id)
    r.roles = Counter(role_games)
    from teamview.analysis import ChampStats
    r.champions = [ChampStats(c, games=g, wins=w) for c, g, w in champs]
    r.mastery = list(mastery)
    return r


def test_rosters_seat_players_in_distinct_roles_and_merge_mastery():
    reports = [report("A#1", {"MIDDLE": 10}, [("Ahri", 8, 5)], [("Wukong", 300_000)]),
               report("B#1", {"MIDDLE": 12, "TOP": 5}), report("C#1", {})]
    roles = default_roles(reports)
    # Same seating as the scouting page: most recent games in seat (A mid 10 + B top 5 beats B mid 12)
    assert roles["MIDDLE"] == "A#1" and roles["TOP"] == "B#1"
    assert "C#1" in roles.values()
    roster = roster_from_reports(reports, roles)
    a = roster["MIDDLE"]
    assert a.comfort("Ahri") > 0.5
    assert a.comfort("MonkeyKing") == 0.45  # mastery floor, matched across spellings


# ---------- composition ----------

def test_comp_needs_and_profile():
    ad_only = [DB.get(c) for c in ("Draven", "Lee Sin", "Zed")]
    d = need_deficits(ad_only)
    assert d["damage"] == 1.0 and d["frontline"] > 0.5
    prof = comp_profile([DB.get(c) for c in ("Ornn", "Sejuani", "Orianna", "Xayah", "Rakan")])
    assert prof.archetype == "Engage and teamfight"
    assert not prof.missing
    assert "No frontline" in comp_profile(ad_only).missing


# ---------- recommendations ----------

OURS = {"TOP": player("UsTop", ("Sion", 12, 7), ("K'Sante", 8, 5)),
        "JUNGLE": player("UsJg", ("Jarvan IV", 15, 9)),
        "MIDDLE": player("UsMid", ("Orianna", 14, 8), ("Syndra", 4, 2)),
        "BOTTOM": player("UsAdc", ("Xayah", 10, 6)),
        "UTILITY": player("UsSup", ("Rakan", 16, 10))}
THEIRS = {"TOP": player("ThemTop", ("Renekton", 14, 9), ("Gnar", 6, 3)),
          "JUNGLE": player("ThemJg", ("Lee Sin", 12, 6)),
          "MIDDLE": player("ThemMid", ("Syndra", 28, 17), ("Taliyah", 4, 2)),
          "BOTTOM": player("ThemAdc", ("Ezreal", 18, 10)),
          "UTILITY": player("ThemSup", ("Nautilus", 11, 6))}


def test_first_bans_target_the_opponents_comfort_picks():
    recs = recommend_bans(context(ours=OURS, theirs=THEIRS), n=5)
    assert {r.champion for r in recs[:3]} <= {"Syndra", "Ezreal", "Renekton", "Lee Sin"}
    syndra = next(r for r in recs if r.champion == "Syndra")
    assert any("ThemMid" in x.text for x in syndra.reasons)


def test_phase_two_bans_only_target_roles_they_have_not_filled():
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista",
             "Jarvan IV", "Syndra", "Kai'Sa", "Rakan", "Xayah", "Lee Sin")  # their mid, adc, jungle are in
    recs = recommend_bans(context(s, ours=OURS, theirs=THEIRS), us=True)
    roles = {r for rec in recs[:3] for r in DB.get(rec.key).roles}
    assert recs[0].champion == "Renekton"
    assert "TOP" in roles or "UTILITY" in roles
    assert all(rec.champion not in ("Taliyah",) for rec in recs[:2])


def test_picks_prefer_comfort_and_skip_unavailable():
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista")
    recs = recommend_picks(context(s, ours=OURS, theirs=THEIRS))
    names = [r.champion for r in recs]
    assert names[0] in ("Jarvan IV", "Orianna", "Rakan", "Sion")
    assert not {"Azir", "Varus", "Vi"} & set(names)


def test_counter_matchup_moves_a_pick():
    s = play(DraftState(our_side=RED), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista",
             "Renekton")  # B1 Renekton, so their top is known when we pick
    data = DraftData()
    data.add_matchup("TOP", "Renekton", "Sion", 0.55, 5000, "test")
    data.add_matchup("TOP", "K'Sante", "Renekton", 0.53, 5000, "test")
    ctx = context(s, data, OURS, THEIRS)
    sion, ksante = score_pick(ctx, K("Sion"), "TOP"), score_pick(ctx, K("K'Sante"), "TOP")
    assert ksante.score > sion.score
    assert any("Loses lane to Renekton" in r.text for r in sion.reasons)
    assert any("Beats Renekton" in r.text for r in ksante.reasons)


def test_blind_risk_uses_the_enemy_laners_pool():
    data = DraftData()
    data.add_matchup("TOP", "Renekton", "Sion", 0.56, 5000, "test")
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista")  # B1, their top still open
    risky = score_pick(context(s, data, OURS, THEIRS), K("Sion"), "TOP")
    assert risky.parts["blind"] > 0.4
    assert any("Blind risk" in r.text and "Renekton" in r.text for r in risky.reasons)
    # With Renekton banned the same pick is safe
    s2 = play(DraftState(), "Renekton", "Varus", "Ezreal", "Rumble", "Vi", "Kalista")
    assert score_pick(context(s2, data, OURS, THEIRS), K("Sion"), "TOP").parts["blind"] < 0.2


def test_last_pick_has_no_blind_risk():
    s = play(DraftState(our_side=RED), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista",
             "Jarvan IV", "Rakan", "Xayah", "Nautilus", "Kai'Sa", "Sejuani",
             "Viego", "Jinx", "Taliyah", "Gnar",
             "Sion", "Renekton", "Ahri")
    assert s.our_turn and s.step == 19  # R5, the last pick of the draft
    assert score_pick(context(s, ours=OURS, theirs=THEIRS), K("Syndra"), "MIDDLE").parts["blind"] == 0.0


def test_synergy_and_denial_reasons():
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista", "Rakan", "Lee Sin", "Viego")
    ctx = context(s, ours=OURS, theirs=THEIRS)
    xayah = score_pick(ctx, K("Xayah"), "BOTTOM")
    assert any("Pairs with Rakan" in r.text for r in xayah.reasons)
    syndra = score_pick(ctx, K("Syndra"), "MIDDLE")
    assert any("Denies ThemMid's Syndra" in r.text for r in syndra.reasons)


def test_predictions_for_the_other_team_use_their_pools():
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista", "Jarvan IV")
    recs = recommend_picks(context(s, ours=OURS, theirs=THEIRS), us=False)
    assert recs[0].champion in ("Syndra", "Renekton", "Lee Sin", "Nautilus")


def test_meta_tiers_show_up_without_scouting():
    data = DraftData()
    data.meta["MIDDLE"] = {K("Ahri"): MetaEntry(tier=1), K("Zoe"): MetaEntry(tier=5)}
    recs = recommend_picks(context(data=data), n=200)
    ahri = next(r for r in recs if r.champion == "Ahri")
    zoe = next(r for r in recs if r.champion == "Zoe")
    assert ahri.score > zoe.score
    assert any("Tier 1 Mid" in r.text for r in ahri.reasons)


def test_watch_list_shows_open_enemy_roles():
    s = play(DraftState(), "Azir", "Varus", "Ezreal", "Rumble", "Vi", "Kalista", "Jarvan IV", "Syndra")
    watch = watch_list(context(s, ours=OURS, theirs=THEIRS))
    assert "MIDDLE" not in [w.role for w in watch]
    top = next(w for w in watch if w.role == "TOP")
    assert top.champions[0].champion == "Renekton"


# ---------- fallback stats from cached matches ----------

def match(mid, blue, red, blue_wins, duration=1800):
    roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    parts = [{"teamId": 100, "teamPosition": r, "championName": c, "win": blue_wins} for r, c in zip(roles, blue)]
    parts += [{"teamId": 200, "teamPosition": r, "championName": c, "win": not blue_wins} for r, c in zip(roles, red)]
    return {"metadata": {"matchId": mid}, "info": {"gameDuration": duration, "participants": parts}}


def test_lane_stats_from_cached_matches_are_shrunk_and_deduplicated():
    blue = ["Renekton", "LeeSin", "Ahri", "Jinx", "Thresh"]
    red = ["Sion", "Vi", "Syndra", "Ezreal", "Nautilus"]
    games = [match(f"NA1_{i}", blue, red, True) for i in range(6)] + [match("NA1_0", blue, red, True)]
    games.append(match("NA1_99", blue, red, False, duration=120))  # remake
    data = from_matches(games)
    rate = data.matchup("TOP", "Renekton", "Sion")
    assert rate.games == 6
    assert 0.55 < rate.winrate < 0.62  # 6-0, pulled toward 50%
    assert data.matchup("TOP", "Sion", "Renekton").winrate == pytest.approx(1 - rate.winrate)
    assert data.synergy("LeeSin", "Renekton").games == 6
