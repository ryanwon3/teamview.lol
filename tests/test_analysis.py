from teamview.analysis import (build_player_report, ban_suggestions, lane_matchups, parse_riot_ids,
                               rank_score, score_label, summarize_team)
from teamview.riot import RateLimiter


def match(puuid, champ, win, role="MIDDLE", duration=1800, k=5, d=3, a=7):
    return {"info": {"gameDuration": duration, "participants": [
        {"puuid": "someone-else", "championName": "Teemo", "win": not win, "kills": 0, "deaths": 0,
         "assists": 0, "totalMinionsKilled": 0, "neutralMinionsKilled": 0, "teamPosition": "TOP"},
        {"puuid": puuid, "championName": champ, "win": win, "kills": k, "deaths": d, "assists": a,
         "totalMinionsKilled": 200, "neutralMinionsKilled": 10, "teamPosition": role},
    ]}}


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
