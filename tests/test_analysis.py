from teamview.analysis import (assign_roles, build_player_report, ban_suggestions, lane_matchups,
                               parse_riot_ids, rank_score, score_label, summarize_team)
from teamview.riot import RateLimiter


def match(puuid, champ, win, role="MIDDLE", duration=1800, k=5, d=3, a=7, champ_id=0):
    return {"info": {"gameDuration": duration, "participants": [
        {"puuid": "someone-else", "championName": "Teemo", "championId": 17, "win": not win,
         "kills": 0, "deaths": 0, "assists": 0, "totalMinionsKilled": 0, "neutralMinionsKilled": 0,
         "teamPosition": "TOP"},
        {"puuid": puuid, "championName": champ, "championId": champ_id, "win": win, "kills": k,
         "deaths": d, "assists": a, "totalMinionsKilled": 200, "neutralMinionsKilled": 10,
         "teamPosition": role},
    ]}}


def player(riot_id, roles, tier="GOLD", rank="I"):
    """A player whose recent games are `roles`, e.g. {"MIDDLE": 5, "TOP": 3}."""
    games = [match(riot_id, "Ahri", True, role) for role, n in roles.items() for _ in range(n)]
    return build_player_report(riot_id, riot_id, [entry(tier, rank, 0)], [], games)


def entry(tier, rank, lp, queue="RANKED_SOLO_5x5"):
    return {"queueType": queue, "tier": tier, "rank": rank, "leaguePoints": lp, "wins": 50, "losses": 40}


def test_parse_riot_ids_handles_lobby_chat_commas_and_missing_tags():
    text = "Faker#KR1 joined the lobby\nDoublelift #NA1, Bjergsen\n\nfaker#kr1"
    assert parse_riot_ids(text, "NA1") == [("Faker", "KR1"), ("Doublelift", "NA1"), ("Bjergsen", "NA1")]


def test_rank_score_round_trips():
    assert rank_score("IRON", "IV", 0) == 0
    assert rank_score("GOLD", "II", 50) == 3 * 400 + 2 * 100 + 50
    assert rank_score("MASTER", "I", 120) == 7 * 400 + 120
    assert score_label(rank_score("GOLD", "II", 50)) == "Gold II 50 LP"
    assert score_label(2900) == "Master+ 100 LP"


def test_player_report_aggregates_champions_and_skips_remakes():
    matches = [match("p1", "Ahri", True)] * 3 + [match("p1", "Ahri", False), match("p1", "Syndra", True),
                                                  match("p1", "Zed", False, duration=200)]
    r = build_player_report("A#NA1", "p1", [entry("PLATINUM", "I", 20)], [], matches)
    assert r.recent_games == 5  # the 200s remake is skipped
    assert [c.champion for c in r.champions] == ["Ahri", "Syndra"]
    ahri = r.champions[0]
    assert (ahri.games, ahri.wins) == (4, 3)
    assert ahri.cs_per_min == 7.0
    assert r.main_role == "MIDDLE"
    assert r.rank_label == "Platinum I 20 LP"


def test_champions_use_display_names_by_id():
    matches = [match("p1", "MonkeyKing", True, champ_id=62), match("p1", "Kaisa", True, champ_id=145),
               match("p1", "NewChamp", True, champ_id=999)]
    names = {62: "Wukong", 145: "Kai'Sa"}
    r = build_player_report("A#NA1", "p1", [], [{"championId": 62, "championPoints": 1000}], matches, names)
    assert sorted(c.champion for c in r.champions) == ["Kai'Sa", "NewChamp", "Wukong"]  # unknown id keeps its key
    assert r.mastery == [("Wukong", 1000)]


def test_flex_rank_used_when_no_solo():
    r = build_player_report("A#NA1", "p1", [entry("SILVER", "III", 0, "RANKED_FLEX_SR")], [], [])
    assert r.queue == "Flex" and r.tier == "SILVER"


def test_strength_adds_recent_form():
    wins = [match("p1", "Ahri", True)] * 12 + [match("p1", "Ahri", False)] * 8  # 60% over 20
    r = build_player_report("A#NA1", "p1", [entry("GOLD", "IV", 0)], [], wins)
    assert r.strength == rank_score("GOLD", "IV", 0) + 80


def test_unranked_and_missing_players_are_excluded_from_average():
    ranked = build_player_report("A#NA1", "p1", [entry("GOLD", "IV", 0)], [], [])
    unranked = build_player_report("B#NA1", "p2", [], [], [])
    missing = build_player_report("C#NA1", None, [], [], [])
    s = summarize_team([ranked, unranked, missing])
    assert s.avg_strength == ranked.strength
    assert s.unranked == ["B#NA1"] and s.missing == ["C#NA1"]
    assert s.strongest is ranked


def test_threats_rank_comfort_winners_above_one_offs():
    p1 = build_player_report("A#NA1", "p1", [entry("GOLD", "IV", 0)], [],
                             [match("p1", "Ahri", True)] * 6 + [match("p1", "Zed", True)])
    p2 = build_player_report("B#NA1", "p2", [entry("GOLD", "IV", 0)], [],
                             [match("p2", "Lee Sin", False)] * 3 + [match("p2", "Ahri", True)] * 2)
    s = summarize_team([p1, p2])
    # A 2-0 Ahri outranks a 0-3 Lee Sin: winning matters, not just volume.
    assert [(t.champion, t.player) for t in s.threats] == [
        ("Ahri", "A#NA1"), ("Ahri", "B#NA1"), ("Lee Sin", "B#NA1")]
    assert "Zed" not in [t.champion for t in s.threats]  # 1 game is below min_games
    assert [t.champion for t in ban_suggestions(s)] == ["Ahri", "Lee Sin"]  # no duplicate Ahri


def test_lane_matchups_pair_by_main_role():
    us = build_player_report("Us#NA1", "u", [entry("GOLD", "I", 0)], [], [match("u", "Ahri", True, "MIDDLE")])
    them = build_player_report("Them#NA1", "t", [entry("GOLD", "III", 0)], [],
                               [match("t", "Zed", True, "MIDDLE")])
    rows = lane_matchups([us], [them])
    assert rows[0]["Role"] == "Mid"
    assert rows[0]["Edge"] == us.strength - them.strength


def test_assign_roles_moves_a_flexible_player_off_a_shared_main_role():
    one_trick = player("Mid#NA1", {"MIDDLE": 6})
    flex = player("Flex#NA1", {"MIDDLE": 5, "TOP": 3})
    seats = assign_roles([one_trick, flex])
    assert seats == {"MIDDLE": one_trick, "TOP": flex}


def test_assign_roles_seats_five_and_benches_the_weaker_duplicate():
    team = [player("T#NA1", {"TOP": 5}), player("J#NA1", {"JUNGLE": 5}), player("M#NA1", {"MIDDLE": 5}),
            player("A#NA1", {"BOTTOM": 5}), player("S#NA1", {"UTILITY": 5}),
            player("Sub#NA1", {"UTILITY": 5}, tier="SILVER")]
    seats = assign_roles(team)
    assert [seats[r].riot_id for r in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")] == [
        "T#NA1", "J#NA1", "M#NA1", "A#NA1", "S#NA1"]


def test_lane_matchups_keep_every_player_when_mains_collide():
    us = [player("Top1#NA1", {"TOP": 6}), player("Top2#NA1", {"TOP": 4, "BOTTOM": 2})]
    them = [player("Mid#NA1", {"MIDDLE": 5})]
    rows = lane_matchups(us, them)
    assert [(r["Role"], r["Us"].split(" ")[0]) for r in rows] == [
        ("Top", "Top1#NA1"), ("Mid", "?"), ("ADC", "Top2#NA1")]


def test_assign_roles_in_order_follows_the_roster_not_solo_queue():
    # Solo queue says this is two supports and two ADCs; the roster order says otherwise.
    team = [player("Doh#NA1", {"UTILITY": 5}), player("Exos#NA1", {"JUNGLE": 5}),
            player("TFT#NA1", {"BOTTOM": 5}), player("Yaz#NA1", {"JUNGLE": 5}),
            player("Ado#NA1", {"BOTTOM": 5}), player("Sub#NA1", {"MIDDLE": 5})]
    seats = assign_roles(team, in_order=True)
    assert {r: p.riot_id for r, p in seats.items()} == {
        "TOP": "Doh#NA1", "JUNGLE": "Exos#NA1", "MIDDLE": "TFT#NA1", "BOTTOM": "Yaz#NA1", "UTILITY": "Ado#NA1"}


def test_assign_roles_in_order_leaves_a_gap_for_a_missing_player():
    missing = build_player_report("Typo#NA1", None, [], [], [])
    team = [player("T#NA1", {"TOP": 5}), missing, player("M#NA1", {"MIDDLE": 5}),
            player("A#NA1", {"BOTTOM": 5}), player("S#NA1", {"UTILITY": 5})]
    seats = assign_roles(team, in_order=True)
    assert "JUNGLE" not in seats and seats["MIDDLE"].riot_id == "M#NA1"


def test_assign_roles_in_order_guesses_for_short_rosters():
    team = [player("Mid#NA1", {"MIDDLE": 5}), player("Top#NA1", {"TOP": 5})]
    assert {r: p.riot_id for r, p in assign_roles(team, in_order=True).items()} == {
        "MIDDLE": "Mid#NA1", "TOP": "Top#NA1"}


def test_rate_limiter_blocks_when_window_full(monkeypatch):
    clock = [0.0]
    slept = []
    monkeypatch.setattr("teamview.riot.time.monotonic", lambda: clock[0])

    def fake_sleep(s):
        slept.append(s)
        clock[0] += s
    monkeypatch.setattr("teamview.riot.time.sleep", fake_sleep)

    limiter = RateLimiter(windows=((2, 1.0),))
    for _ in range(3):
        limiter.acquire()
    assert slept and slept[0] >= 1.0
