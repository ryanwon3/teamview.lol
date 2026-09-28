import json

import pytest

from teamview.champions import ChampionDB, champ_key
from teamview.opgg import (OpggClient, OpggError, build_args, extract_counters, extract_meta, extract_synergies,
                           load_draft_data, parse_rpc_response, parse_tool_result)

KNOWN = set(ChampionDB.load().keys())


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
            return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "lol_get_champion_analysis", "inputSchema": schema},
                {"name": "lol_list_lane_meta_champions", "inputSchema": meta_schema}]}})
        if method == "tools/call":
            name, args = payload["params"]["name"], payload["params"]["arguments"]
            if name == "lol_list_lane_meta_champions":
                body = {"champions": [{"champion_name": "Ahri", "tier": 1}]} if args["position"] == "mid" else {}
            elif args["champion"] == "Sion":
                body = {"counters": [{"champion_name": "Renekton", "win_rate": 0.45, "play": 2000}]}
            else:
                return FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {
                    "isError": True, "content": [{"type": "text", "text": "unknown champion"}]}})
            reply = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(body)}]}}
            return FakeResponse(f"event: message\ndata: {json.dumps(reply)}\n\n", content_type="text/event-stream")
        raise AssertionError(method)


def test_load_draft_data_end_to_end_with_a_fake_server():
    session = FakeSession()
    client = OpggClient(session=session, min_interval=0)
    data, problems = load_draft_data(client, {"Sion": {"TOP"}, "Zed": {"MIDDLE"}}, known=KNOWN)
    assert data.meta["MIDDLE"]["ahri"].tier == 1
    rate = data.matchup("TOP", "Renekton", "Sion")
    assert rate.winrate > 0.5 and rate.source == "OP.gg"
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
