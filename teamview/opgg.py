"""Client for OP.gg's official MCP server: per-role tier lists, counters and synergies.

OP.gg has no public REST API. Its MCP server (https://mcp-api.op.gg/mcp) speaks
JSON-RPC over "streamable HTTP" and is meant for AI agents, with no published limits
or response schema. So this client is defensive:

- it reads each tool's input schema from `tools/list` and fills arguments from it
  (champion and position enums, game mode, language) instead of hard-coding them,
- it pulls champions and win rates out of whatever JSON comes back,
- every response is cached for 12 hours in the scouting app's SQLite cache,
- any failure raises OpggError, and the draft page falls back to the tag file and
  the Riot match cache.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from typing import Any, Callable, Iterable

import requests

from .analysis import ROLE_ORDER
from .champions import champ_key
from .matchups import DraftData, MetaEntry, shrunk

MCP_URL = "https://mcp-api.op.gg/mcp"
PROTOCOL_VERSION = "2025-06-18"
TTL_TOOLS = 24 * 3600
TTL_DATA = 12 * 3600
SOURCE = "OP.gg"

TOOL_META = "lol_list_lane_meta_champions"
TOOL_ANALYSIS = "lol_get_champion_analysis"
TOOL_SYNERGY = "lol_get_champion_synergies"

ROLE_WORDS = {
    "TOP": ("top",),
    "JUNGLE": ("jungle",),
    "MIDDLE": ("mid", "middle"),
    "BOTTOM": ("adc", "bottom", "bot"),
    "UTILITY": ("support", "utility", "sup"),
}


class OpggError(Exception):
    pass


# ---------- JSON-RPC over streamable HTTP ----------

def parse_rpc_response(text: str, content_type: str, want_id: int) -> dict:
    """The server may answer with plain JSON or with a server-sent-events stream."""
    if "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:")):
        for block in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        raise OpggError("OP.gg's reply stream had no answer to the request.")
    try:
        return json.loads(text)
    except ValueError as e:
        raise OpggError(f"OP.gg sent something that isn't JSON: {text[:120]!r}") from e


def parse_tool_result(result: dict) -> Any:
    """Turn a tools/call result into Python data (JSON where possible, else the raw text)."""
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    if result.get("isError"):
        raise OpggError("OP.gg tool error: " + " ".join(texts)[:300])
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    joined = "\n".join(texts).strip()
    try:
        return json.loads(joined)
    except ValueError:
        return joined


class OpggClient:
    def __init__(self, cache=None, session: requests.Session | None = None, url: str = MCP_URL,
                 timeout: float = 20, min_interval: float = 0.2):
        self.cache = cache  # teamview.riot.Cache, or None to skip caching
        self.session = session or requests.Session()
        self.url = url
        self.timeout = timeout
        self.min_interval = min_interval
        self.session_id: str | None = None
        self.initialized = False
        self._next_id = 0
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _post(self, payload: dict) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise OpggError(f"Couldn't reach OP.gg ({type(e).__name__}). Is mcp-api.op.gg allowed?") from e
        finally:
            self._last_call = time.monotonic()
        if resp.status_code >= 400:
            raise OpggError(f"OP.gg answered HTTP {resp.status_code}.")
        if resp.headers.get("Mcp-Session-Id"):
            self.session_id = resp.headers["Mcp-Session-Id"]
        if "id" not in payload:
            return None  # a notification: no answer expected
        return parse_rpc_response(resp.text, resp.headers.get("Content-Type", ""), payload["id"])

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._next_id += 1
            msg = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        if msg.get("error"):
            raise OpggError(f"OP.gg error: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def connect(self):
        if self.initialized:
            return
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "teamview.lol", "version": "1.0"},
        })
        with self._lock:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    def _cached(self, key: str, ttl: float, fetch: Callable[[], Any]) -> Any:
        if self.cache is not None:
            hit = self.cache.get(key, ttl)
            if hit is not None:
                return hit
        value = fetch()
        if self.cache is not None and value not in (None, "", [], {}):
            self.cache.put(key, value)
        return value

    def tools(self) -> dict[str, dict]:
        """Tool name -> input JSON schema."""
        def fetch():
            self.connect()
            out, cursor = {}, None
            for _ in range(10):
                result = self._request("tools/list", {"cursor": cursor} if cursor else {})
                for tool in result.get("tools", []):
                    out[tool["name"]] = tool.get("inputSchema") or {}
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            return out
        return self._cached("opgg:tools/list", TTL_TOOLS, fetch)

    def call(self, name: str, arguments: dict) -> Any:
        key = f"opgg:{name}:{json.dumps(arguments, sort_keys=True)}"

        def fetch():
            self.connect()
            return parse_tool_result(self._request("tools/call", {"name": name, "arguments": arguments}))
        return self._cached(key, TTL_DATA, fetch)


# ---------- arguments from the tool's schema ----------

def _enum_of(spec: dict) -> list:
    if spec.get("enum"):
        return list(spec["enum"])
    for key in ("anyOf", "oneOf"):
        for option in spec.get(key, []):
            if option.get("enum"):
                return list(option["enum"])
            if option.get("const") is not None:
                return [o.get("const") for o in spec[key] if o.get("const") is not None]
    if isinstance(spec.get("items"), dict):
        return _enum_of(spec["items"])
    return []


def _match_enum(enum: list, wanted: Iterable[str], by_champion: bool = False) -> Any:
    norm = champ_key if by_champion else (lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower()))
    wanted = [norm(w) for w in wanted]
    for w in wanted:
        for value in enum:
            if norm(value) == w:
                return value
    for w in wanted:
        for value in enum:
            if w and w in norm(value):
                return value
    return None


def build_args(schema: dict, champion: str | None = None, role: str | None = None) -> dict:
    """Fill a tool's arguments from its input schema."""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    args = {}
    for name, spec in props.items():
        spec = spec or {}
        lname = name.lower()
        enum = _enum_of(spec)
        value = None
        if "champion" in lname and champion is not None:
            value = _match_enum(enum, [champion], by_champion=True) if enum else champion
            if spec.get("type") == "array" and value is not None:
                value = [value]
        elif lname in ("position", "lane", "role", "positions") and role:
            value = _match_enum(enum, ROLE_WORDS[role]) if enum else ROLE_WORDS[role][0]
        elif "game_mode" in lname or lname in ("mode", "queue", "queue_type"):
            value = _match_enum(enum, ("ranked", "solo")) or (enum[0] if enum else "ranked")
        elif lname in ("lang", "language", "locale", "hl"):
            value = _match_enum(enum, ("en_US", "en-US", "en")) or "en_US"
        elif name in required and "region" in lname:
            value = _match_enum(enum, ("global", "all", "na")) or (enum[0] if enum else "global")
        elif name in required and spec.get("default") is not None:
            value = spec["default"]
        elif name in required and lname == "desired_output_fields":
            value = ["data"]
        if value is not None:
            args[name] = value
    missing = [n for n in required if n not in args]
    if missing:
        raise OpggError(f"Don't know how to fill OP.gg arguments: {', '.join(missing)}")
    return args


# ---------- pulling champions and win rates out of responses ----------

NAME_KEYS = ("champion_name", "championName", "champion", "name", "champion_key", "key", "champion_id",
             "championId", "id")
WINRATE_KEYS = ("win_rate", "winRate", "winrate", "win_ratio", "winRatio", "wr")
GAMES_KEYS = ("play", "plays", "games", "play_count", "playCount", "count", "total", "sample_size")
TIER_KEYS = ("tier", "tier_rank", "rank_tier", "op_tier", "position_tier")


def _walk(obj, path=()):
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from _walk(v, path + (str(k).lower(),))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path)


def _rate(value) -> float | None:
    try:
        x = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return x / 100 if x > 1 else x


def _champ_of(d: dict, id_names: dict[int, str]) -> str | None:
    for k in NAME_KEYS:
        v = d.get(k)
        if isinstance(v, dict):
            inner = _champ_of(v, id_names)
            if inner:
                return inner
        elif isinstance(v, str) and v and not v.isdigit():
            return v
        elif isinstance(v, (int, str)) and str(v).isdigit() and int(v) in id_names:
            return id_names[int(v)]
    return None


def _games_of(d: dict) -> int:
    for k in GAMES_KEYS:
        if isinstance(d.get(k), (int, float)):
            return int(d[k])
    return 0


def _winrate_of(d: dict) -> float | None:
    for k in WINRATE_KEYS:
        if k in d:
            return _rate(d[k])
    wins = d.get("win") if "win" in d else d.get("wins")
    games = _games_of(d)
    if isinstance(wins, (int, float)) and games:
        return wins / games
    return None


def _entries(payload, id_names, must_have: str | None = None, known: set[str] | None = None):
    """(path, champion, winrate, games, dict) for every champion-with-a-win-rate in the payload.

    `known` (champion keys) drops things that aren't champions, like items in a build list.
    """
    for path, d in _walk(payload):
        if must_have and not any(must_have in p for p in path):
            continue
        champ = _champ_of(d, id_names)
        if champ and known and champ_key(champ) not in known:
            continue
        wr = _winrate_of(d)
        if champ and wr is not None and 0 <= wr <= 1:
            yield path, champ, wr, _games_of(d), d


def extract_counters(payload, champion: str, id_names: dict[int, str] | None = None,
                     known: set[str] | None = None) -> list[tuple[str, float, int]]:
    """(opponent, win rate of `champion` against them, games) from a champion analysis."""
    id_names = id_names or {}
    me = champ_key(champion)
    groups: dict[tuple, list] = {}
    for key in ("counter", "matchup"):
        for path, champ, wr, games, _ in _entries(payload, id_names, key, known):
            if champ_key(champ) != me:
                groups.setdefault(path, []).append((champ, wr, games))
        if groups:
            break
    out = {}
    for path, rows in groups.items():
        label = " ".join(path)
        median = statistics.median(wr for _, wr, _ in rows)
        # "weak" lists should hold matchups we lose; if the numbers say otherwise they're the
        # opponent's win rate, so flip them. Same idea for "strong"/"good" lists.
        flip = ("weak" in label or "bad" in label) and median > 0.5 or \
               ("strong" in label or "good" in label) and median < 0.5
        for champ, wr, games in rows:
            out[champ_key(champ)] = (champ, 1 - wr if flip else wr, games)
    return list(out.values())


def extract_meta(payload, role: str, id_names: dict[int, str] | None = None,
                 known: set[str] | None = None) -> dict[str, MetaEntry]:
    id_names = id_names or {}
    other_roles = {w for r, words in ROLE_WORDS.items() if r != role for w in words}
    out = {}
    for path, d in _walk(payload):
        if any(p in other_roles for p in path):
            continue
        position = str(d.get("position") or d.get("lane") or "").lower()
        if position and position not in ROLE_WORDS[role]:
            continue
        champ = _champ_of(d, id_names)
        if not champ or (known and champ_key(champ) not in known):
            continue
        tier = next((d[k] for k in TIER_KEYS if k in d), None)
        entry = MetaEntry(
            tier=_tier(tier),
            winrate=_winrate_of(d),
            pickrate=_rate(d.get("pick_rate", d.get("pickRate"))) if ("pick_rate" in d or "pickRate" in d) else None,
            banrate=_rate(d.get("ban_rate", d.get("banRate"))) if ("ban_rate" in d or "banRate" in d) else None,
        )
        if entry.tier is None and entry.winrate is None:
            continue
        out.setdefault(champ_key(champ), entry)
    return out


def _tier(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().upper()
    if s in ("OP", "S+", "S"):
        return 1 if s != "OP" else 0
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else None


def extract_synergies(payload, champion: str, id_names: dict[int, str] | None = None,
                      known: set[str] | None = None) -> list[tuple[str, float, int]]:
    id_names = id_names or {}
    me = champ_key(champion)
    out = {}
    for _, champ, wr, games, _ in _entries(payload, id_names, "synerg", known):
        if champ_key(champ) != me:
            out[champ_key(champ)] = (champ, wr, games)
    return list(out.values())


# ---------- building DraftData for the draft page ----------

def load_draft_data(client: OpggClient, champions: dict[str, set[str]], meta: bool = True,
                    synergy_for: Iterable[str] = (), id_names: dict[int, str] | None = None,
                    known: set[str] | None = None,
                    on_progress: Callable[[float, str], None] | None = None) -> tuple[DraftData, list[str]]:
    """Fetch tier lists, counters for `champions` (display name -> positions), and synergies.

    Returns the data plus a list of problems (one per failed call) so the page can show
    what's missing without failing the whole load. Raises OpggError if OP.gg can't be
    reached at all.
    """
    tools = client.tools()
    data, problems = DraftData(), []
    jobs: list[tuple[str, str | None, str | None]] = []
    if meta and TOOL_META in tools:
        jobs += [(TOOL_META, None, role) for role in ROLE_ORDER]
    if TOOL_ANALYSIS in tools:
        jobs += [(TOOL_ANALYSIS, champ, role) for champ, roles in champions.items() for role in sorted(roles)]
    synergy_tool = TOOL_SYNERGY if TOOL_SYNERGY in tools else None
    if synergy_tool:
        jobs += [(synergy_tool, champ, None) for champ in synergy_for]
    if not jobs:
        raise OpggError("OP.gg's server doesn't list the champion tools this app expects.")

    for i, (tool, champ, role) in enumerate(jobs):
        if on_progress:
            on_progress(i / len(jobs), f"OP.gg: {champ or 'tier list'}{' ' + role.lower() if role else ''}")
        try:
            payload = client.call(tool, build_args(tools[tool], champion=champ, role=role))
        except OpggError as e:
            problems.append(f"{tool} {champ or ''} {role or ''}: {e}".strip())
            if not data.lane and not data.meta and len(problems) >= 3:
                raise OpggError(f"OP.gg calls keep failing: {e}") from e
            continue
        if tool == TOOL_META:
            entries = extract_meta(payload, role, id_names, known)
            if entries:
                data.meta[role] = entries
                data.sources.add(SOURCE)
        elif tool == TOOL_ANALYSIS:
            for opp, wr, games in extract_counters(payload, champ, id_names, known):
                # OP.gg samples are large; still shrink a little so tiny samples don't dominate.
                adjusted = shrunk(wr * games, games) if games else wr
                data.add_matchup(role, champ, opp, adjusted, games or 100, SOURCE)
            for ally, wr, games in extract_synergies(payload, champ, id_names, known):
                data.add_synergy(champ, ally, wr, games or 100, SOURCE)
        else:
            for ally, wr, games in extract_synergies(payload, champ, id_names, known):
                data.add_synergy(champ, ally, wr, games or 100, SOURCE)
    if on_progress:
        on_progress(1.0, "OP.gg data loaded")
    return data, problems
