import json
from pathlib import Path

import pytest

from teamview.champions import ChampionDB, champ_key
from teamview.opgg import (OpggClient, OpggError, build_args, extract_counters, extract_meta, extract_synergies,
                           load_draft_data, opgg_champion, parse_class_text, parse_rpc_response, parse_tool_result)

KNOWN = set(ChampionDB.load().keys())
DATA = Path(__file__).parent / "data"  # replies recorded from mcp-api.op.gg on 2026-09-30
META_TEXT = """class LolListLaneMetaChampions: data
class Data: positions
class Positions: adc
class Adc: champion,is_rip,play,win,win_rate,pick_rate,ban_rate,tier

LolListLaneMetaChampions(Data(Positions([Adc("Jinx",false,1000,525,0.53,0.17,0.09,0),
                                         Adc("Kai'Sa",false,800,400,0.5,0.12,0.2,1)])))"""


def test_parses_json_and_event_stream_replies():
    assert parse_rpc_response('{"jsonrpc":"2.0","id":3,"result":{}}', "application/json", 3)["id"] == 3
    stream = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
              'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n\n')
    assert parse_rpc_response(stream, "text/event-stream", 7)["result"] == {"ok": True}
    with pytest.raises(OpggError):
        parse_rpc_response(stream, "text/event-stream", 8)


def test_tool_results_prefer_structured_content_then_json_text():
    assert parse_tool_result({"structuredContent": {"a": 1}, "content": []}) == {"a": 1}
    assert parse_tool_result({"content": [{"type": "text", "text": '{"b": 2}'}]}) == {"b": 2}
    assert parse_tool_result({"content": [{"type": "text", "text": "plain"}]}) == "plain"
    with pytest.raises(OpggError):
        parse_tool_result({"isError": True, "content": [{"type": "text", "text": "bad champion"}]})


def test_reads_opgg_class_format():
    parsed = parse_class_text(META_TEXT)
    assert parsed["data"]["positions"]["adc"][1] == {
        "champion": "Kai'Sa", "is_rip": False, "play": 800, "win": 400, "win_rate": 0.5, "pick_rate": 0.12,
        "ban_rate": 0.2, "tier": 1}
    assert parse_tool_result({"content": [{"type": "text", "text": META_TEXT}]}) == parsed
    escaped = parse_class_text('class A: s,xs,n\n\nA("say \\"hi\\"",[],-1.5e2)')
    assert escaped == {"s": 'say "hi"', "xs": [], "n": -150.0}
    for broken in ('class A: x\n\nB(1)', 'class A: x\n\nA("open)', 'class A: x\n\nA(1,2)', 'class A: x\n\nA(1) A(2)'):
        with pytest.raises(OpggError):
            parse_class_text(broken)


def test_meta_from_a_class_format_reply():
    meta = extract_meta(parse_class_text(META_TEXT), "BOTTOM", known=KNOWN)
    assert list(meta) == ["jinx", "kaisa"]
    assert meta["jinx"].tier == 0 and meta["jinx"].winrate == pytest.approx(0.525) and meta["kaisa"].banrate == 0.2


def test_real_analysis_reply():
    payload = parse_class_text((DATA / "opgg_analysis_wukong_jungle.txt").read_text())
    rows = {champ_key(c): (wr, g) for c, wr, g in extract_counters(payload, "Wukong", known=KNOWN, role="JUNGLE")}
    # Wukong's top-lane counters (Singed, Vladimir, Warwick) stay out of jungle matchups
    assert set(rows) == {"malphite", "taliyah", "nidalee", "darius", "trundle", "naafiri", "zac"}
    assert rows["malphite"] == (pytest.approx(108 / 239), 239)  # weak counter: Wukong's own win rate
    assert rows["darius"][0] > 0.5  # strong counter
    duos = {champ_key(c): wr for c, wr, _ in extract_synergies(payload, "Wukong", known=KNOWN)}
    assert "wukong" not in duos and duos["jinx"] == pytest.approx(644 / 1143) and len(duos) == 9


def test_real_synergy_reply():
    payload = parse_class_text((DATA / "opgg_synergies_wukong_support.txt").read_text())
    duos = extract_synergies(payload, "Wukong", known=KNOWN)
    assert len(duos) == 10 and "wukong" not in {champ_key(c) for c, _, _ in duos}


def test_build_args_for_the_synergy_tool():
    positions = {"type": "string", "enum": ["all", "none", "top", "mid", "jungle", "adc", "support"]}
    schema = {"properties": {
        "champion": {"type": "string"}, "my_position": positions, "synergy_position": positions,
        "desired_output_fields": {"type": "array", "description": (
            "Select ONLY from fields below.\n\nAvailable fields:\n- data.synergies[].{champion_name,win}\n- lang\n\n"
            "Field descriptions:\n- synergies.*.tier: Synergy tier")},
    }, "required": ["champion", "my_position", "synergy_position", "desired_output_fields"]}
    args = build_args(schema, champion="Wukong", role="JUNGLE", partner_role="UTILITY", fields=("synergies",))
    assert args == {"champion": "WUKONG", "my_position": "jungle", "synergy_position": "support",
                    "desired_output_fields": ["data.synergies[].{champion_name,win}"]}
    with pytest.raises(OpggError):
        build_args(schema, champion="Wukong", role="JUNGLE")  # no teammate role


def test_opgg_champion_names():
    # Spellings OP.gg's synergy tool accepted on 2026-09-30
    names = ["Kai'Sa", "Nunu & Willump", "Dr. Mundo", "Renata Glasc", "Jarvan IV", "Wukong", "Lee Sin", "LeBlanc"]
    assert [opgg_champion(n) for n in names] == [
        "KAISA", "NUNU_WILLUMP", "DR_MUNDO", "RENATA_GLASC", "JARVAN_IV", "WUKONG", "LEE_SIN", "LEBLANC"]


def test_build_args_fills_from_schema_enums():
    schema = {"properties": {
        "champion": {"type": "string", "enum": ["AHRI", "MONKEY_KING", "JARVAN_IV"]},
        "position": {"type": "string", "enum": ["top", "jungle", "mid", "adc", "support"]},
        "game_mode": {"type": "string", "enum": ["ARAM", "RANKED"]},
        "lang": {"type": "string", "enum": ["ko_KR", "en_US"]},
        "desired_output_fields": {"type": "array", "items": {"type": "string"}},
    }, "required": ["champion", "position", "game_mode", "lang"]}
    args = build_args(schema, champion="Wukong", role="BOTTOM")
    assert args == {"champion": "MONKEY_KING", "position": "adc", "game_mode": "RANKED", "lang": "en_US"}
    with pytest.raises(OpggError):
        build_args({"properties": {"summoner": {}}, "required": ["summoner"]})


def test_counters_are_read_from_either_perspective():
    ours = {"data": {"weakCounters": [{"champion_name": "Renekton", "win_rate": 0.46, "play": 900}],
                     "strongCounters": [{"champion_name": "Gnar", "win_rate": 53.5, "play": 700}],
                     "items": [{"name": "Sunfire Aegis", "win_rate": 0.55}]}}
    rows = {champ_key(c): (wr, g) for c, wr, g in extract_counters(ours, "Sion", known=KNOWN)}
    assert rows == {"renekton": (0.46, 900), "gnar": (0.535, 700)}
    # A "weak" list holding the opponents' win rates gets flipped to ours
    theirs = {"weak_counters": [{"champion": {"name": "Renekton"}, "win_rate": 0.54, "play": 900},
                                {"champion": {"name": "Darius"}, "win_rate": 0.52, "play": 400}]}
    rows = {champ_key(c): wr for c, wr, _ in extract_counters(theirs, "Sion", known=KNOWN)}
    assert rows["renekton"] == pytest.approx(0.46)


def test_meta_skips_other_roles_and_non_champions():
    payload = {"data": {"mid": [{"champion_name": "Ahri", "tier": 1, "win_rate": 0.52, "ban_rate": 0.1}],
                        "top": [{"champion_name": "Sion", "tier": 2, "win_rate": 0.5}]}}
    meta = extract_meta(payload, "MIDDLE", known=KNOWN)
    assert list(meta) == ["ahri"] and meta["ahri"].tier == 1 and meta["ahri"].banrate == 0.1
    ids = {"positions": [{"position": "support", "champion_id": 412, "tier": "OP"},
                         {"position": "mid", "champion_id": 103, "tier": "3"}]}
    meta = extract_meta(ids, "UTILITY", id_names={412: "Thresh", 103: "Ahri"}, known=KNOWN)
    assert list(meta) == ["thresh"] and meta["thresh"].tier == 0


def test_synergies():
    payload = {"synergies": [{"champion_name": "Rakan", "win_rate": 0.53, "play": 3000},
                             {"champion_name": "Xayah", "win_rate": 0.5, "play": 1}]}
    assert extract_synergies(payload, "Xayah", known=KNOWN) == [("Rakan", 0.53, 3000)]


class FakeResponse:
    def __init__(self, body, content_type="application/json", status=200, session_id=None):
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id


class FakeSession:
    """Plays OP.gg's MCP server: initialize, tools/list, tools/call."""

    def __init__(self):
        self.calls = []
        self.synergy_positions = []

    def post(self, url, **kwargs):
        payload, headers = kwargs["json"], kwargs["headers"]
        self.calls.append((payload.get("method"), headers.get("Mcp-Session-Id")))
        method, rid = payload.get("method"), payload.get("id")
        if method == "initialize":
            return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {}}, session_id="abc")
        if method == "notifications/initialized":
            return FakeResponse("", status=202)
        if method == "tools/list":
            schema = {"properties": {"champion": {"type": "string"}, "position": {"type": "string"}},
                      "required": ["champion", "position"]}
            meta_schema = {"properties": {"position": {"type": "string"}}, "required": ["position"]}
            synergy_schema = {"properties": {"champion": {"type": "string"}, "my_position": {"type": "string"},
                                             "synergy_position": {"type": "string"}},
                              "required": ["champion", "my_position", "synergy_position"]}
            return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "lol_get_champion_analysis", "inputSchema": schema},
                {"name": "lol_list_lane_meta_champions", "inputSchema": meta_schema},
                {"name": "lol_get_champion_synergies", "inputSchema": synergy_schema}]}})
        if method == "tools/call":
            name, args = payload["params"]["name"], payload["params"]["arguments"]
            if name == "lol_list_lane_meta_champions":
                body = ('class Meta: data\nclass Data: mid\nclass Mid: champion,tier\n\nMeta(Data([Mid("Ahri",1)]))'
                        if args["position"] == "mid" else {})
            elif name == "lol_get_champion_synergies" and args["champion"] != "ZED":
                self.synergy_positions.append(args["synergy_position"])
                row = {"champion_name": "Sion", "synergy_champion_name": "Lee Sin", "play": 100, "win": 55}
                body = {"data": {"synergies": [row]}} if args["synergy_position"] == "jungle" else {}
            elif args["champion"] == "SION":
                body = {"counters": [{"champion_name": "Renekton", "win_rate": 0.45, "play": 2000}]}
            else:
                return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {
                    "isError": True, "content": [{"type": "text", "text": "unknown champion"}]}})
            text = body if isinstance(body, str) else json.dumps(body)
            reply = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}
            return FakeResponse(f"event: message\ndata: {json.dumps(reply)}\n\n", content_type="text/event-stream")
        raise AssertionError(method)


def test_load_draft_data_end_to_end_with_a_fake_server():
    session = FakeSession()
    client = OpggClient(session=session, min_interval=0)
    data, problems = load_draft_data(client, {"Sion": {"TOP"}, "Zed": {"MIDDLE"}}, synergy_for=[("Sion", "TOP")],
                                     known=KNOWN)
    assert data.meta["MIDDLE"]["ahri"].tier == 1
    rate = data.matchup("TOP", "Renekton", "Sion")
    assert rate.winrate > 0.5 and rate.source == "OP.gg"
    assert sorted(session.synergy_positions) == ["adc", "jungle", "mid", "support"]  # one call per teammate lane
    assert data.synergy("Sion", "Lee Sin").winrate == pytest.approx(0.55)
    assert len(problems) == 1 and "unknown champion" in problems[0]
    # Session id from initialize is sent on later requests
    assert session.calls[0] == ("initialize", None)
    assert all(sid == "abc" for _, sid in session.calls[1:])


def test_unreachable_server_raises_a_clear_error():
    import requests

    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("proxy said no")
    with pytest.raises(OpggError, match="mcp-api.op.gg"):
        load_draft_data(OpggClient(session=Down(), min_interval=0), {"Sion": {"TOP"}})


def test_gives_up_when_every_call_fails():
    class Broken(FakeSession):
        def post(self, url, **kwargs):
            if kwargs["json"].get("method") == "tools/call":
                return FakeResponse("", status=500)
            return super().post(url, **kwargs)
    client = OpggClient(session=Broken(), min_interval=0)
    with pytest.raises(OpggError, match="keep failing"):
        load_draft_data(client, {"Sion": {"TOP"}, "Zed": {"MIDDLE"}}, known=KNOWN)


def test_tool_errors_alone_dont_switch_opgg_off():
    # Four synergy calls for one pick all fail with a tool error: report them, don't give up on OP.gg
    session = FakeSession()
    client = OpggClient(session=session, min_interval=0)
    data, problems = load_draft_data(client, {}, meta=False, synergy_for=[("Zed", "MIDDLE")], known=KNOWN)
    assert len(problems) == 4 and all("unknown champion" in p for p in problems)
