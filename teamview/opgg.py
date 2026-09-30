"""Client for OP.gg's official MCP server: per-role tier lists, counters and synergies.

OP.gg has no public REST API. Its MCP server (https://mcp-api.op.gg/mcp) speaks
JSON-RPC over "streamable HTTP" and is meant for AI agents, with no published limits
or response schema. So this client is defensive:

- it reads each tool's input schema from `tools/list` and fills arguments from it
  (champion and position enums, game mode, language, output fields) instead of
  hard-coding them,
- tools reply in a compact "class" text format rather than JSON (see
  `parse_class_text`); it pulls champions and win rates out of whatever comes back,
- calls run a few at a time (each takes about 3 seconds), and every response is
  cached for 12 hours in the scouting app's SQLite cache,
- any failure raises OpggError, and the draft page falls back to the tag file and
  the Riot match cache.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
WORKERS = 6  # parallel OP.gg calls; each takes about 3 seconds

# Parts of each tool's reply the draft page reads, matched against the field list in the
# tool's `desired_output_fields` description.
TOOL_FIELDS = {
    TOOL_META: ("data.positions",),
    TOOL_ANALYSIS: ("counters", "positions[].name", "synergies"),
    TOOL_SYNERGY: ("synergies",),
}

ROLE_WORDS = {
    "TOP": ("top",),
    "JUNGLE": ("jungle",),
    "MIDDLE": ("mid", "middle"),
    "BOTTOM": ("adc", "bottom", "bot"),
    "UTILITY": ("support", "utility", "sup"),
}


class OpggError(Exception):
    pass


class OpggUnavailable(OpggError):
    """The server couldn't be reached or answered with an HTTP error (not a per-call tool error)."""


def opgg_champion(name: str) -> str:
    """OP.gg's UPPER_SNAKE_CASE champion name: Kai'Sa -> KAISA, Dr. Mundo -> DR_MUNDO, Wukong -> WUKONG.

    The champion analysis tool also takes display names, but the synergy tool only takes this.
    """
    return "_".join(re.sub(r"[^A-Za-z0-9\s]", "", name).upper().split())


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


_CLASS_LINE = re.compile(r"class (\w+):[ \t]*(.*)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_WORD = re.compile(r"\w+")
_LITERALS = {"true": True, "false": False, "null": None, "none": None}


def parse_class_text(text: str) -> Any:
    """Read OP.gg's compact reply format into dicts and lists.

    The real server answers with class headers, then one expression of positional values:

        class Data: positions
        class Positions: top
        class Top: champion,tier,win_rate

        Data(Positions([Top("Malphite",1,0.51),Top("Garen",1,0.51)]))

    which reads as {"positions": {"top": [{"champion": "Malphite", "tier": 1, ...}, ...]}}.
    The outermost class (named after the tool) wraps everything, so the result has the
    same shape as the `desired_output_fields` paths in the tool's schema.
    """
    fields: dict[str, list[str]] = {}
    lines = text.strip().splitlines()
    i = 0
    while i < len(lines) and (m := _CLASS_LINE.fullmatch(lines[i].strip())):
        fields[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
        i += 1
    body = "\n".join(lines[i:]).strip()
    pos = 0

    def fail(why: str):
        raise OpggError(f"Couldn't read OP.gg's reply ({why} at {pos}): {body[max(0, pos - 40):pos + 40]!r}")

    def skip_space():
        nonlocal pos
        while pos < len(body) and body[pos].isspace():
            pos += 1

    def items(close: str) -> list:
        nonlocal pos
        out = []
        skip_space()
        if body.startswith(close, pos):
            pos += 1
            return out
        while True:
            out.append(value())
            skip_space()
            if body.startswith(",", pos):
                pos += 1
            elif body.startswith(close, pos):
                pos += 1
                return out
            else:
                fail(f"expected ',' or {close!r}")

    def value() -> Any:
        nonlocal pos
        skip_space()
        if pos >= len(body):
            fail("reply ended early")
        ch = body[pos]
        if ch == '"':
            end = pos + 1
            while end < len(body) and body[end] != '"':
                end += 2 if body[end] == "\\" else 1
            if end >= len(body):
                fail("unclosed string")
            s = body[pos:end + 1]
            pos = end + 1
            try:
                return json.loads(s)
            except ValueError:
                return s[1:-1]
        if ch == "[":
            pos += 1
            return items("]")
        if m := _NUMBER.match(body, pos):
            pos = m.end()
            n = m.group()
            return float(n) if any(c in n for c in ".eE") else int(n)
        if m := _WORD.match(body, pos):
            word = m.group()
            pos = m.end()
            if body.startswith("(", pos):
                pos += 1
                args = items(")")
                names = fields.get(word)
                if names is None:
                    fail(f"no header for class {word}")
                if len(args) > len(names):
                    fail(f"{word} has {len(args)} values for {len(names)} fields")
                return dict(zip(names, args))
            if word.lower() in _LITERALS:
                return _LITERALS[word.lower()]
            fail(f"unexpected word {word!r}")
        fail(f"unexpected {ch!r}")

    result = value()
    skip_space()
    if pos != len(body):
        fail("extra text after the reply")
    return result


def decode_text(text: str) -> Any:
    """JSON or OP.gg's class format as Python data; any other text is returned as is."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if _CLASS_LINE.match(text):
        return parse_class_text(text)
    return text


def parse_tool_result(result: dict) -> Any:
    """Turn a tools/call result into Python data (JSON or class format, else the raw text)."""
    texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    if result.get("isError"):
        raise OpggError("OP.gg tool error: " + " ".join(texts)[:300])
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return decode_text("\n".join(texts))


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
        self._connect_lock = threading.Lock()

    def _post(self, payload: dict) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        with self._lock:  # space out request starts; the requests themselves run in parallel
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        try:
            resp = self.session.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise OpggUnavailable(f"Couldn't reach OP.gg ({type(e).__name__}). Is mcp-api.op.gg allowed?") from e
        if resp.status_code >= 400:
            raise OpggUnavailable(f"OP.gg answered HTTP {resp.status_code}.")
        if resp.headers.get("Mcp-Session-Id"):
            self.session_id = resp.headers["Mcp-Session-Id"]
        if "id" not in payload:
            return None  # a notification: no answer expected
        return parse_rpc_response(resp.text, resp.headers.get("Content-Type", ""), payload["id"])

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if msg.get("error"):
            raise OpggError(f"OP.gg error: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def connect(self):
        with self._connect_lock:
            if self.initialized:
                return
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "teamview.lol", "version": "1.0"},
            })
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
        value = self._cached(key, TTL_DATA, fetch)
        return decode_text(value) if isinstance(value, str) else value  # raw text cached by older versions


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


def output_fields(spec: dict, wanted: Iterable[str]) -> list[str]:
    """The `desired_output_fields` entries (listed in the field's description) that mention `wanted`."""
    block = re.search(r"Available fields:\s*\n((?:- .*\n?)+)", spec.get("description", ""))
    listed = re.findall(r"^- (\S+)", block.group(1), flags=re.M) if block else []
    picked = [f for f in listed if any(w in f for w in wanted)]
    return picked or ["data"]


def build_args(schema: dict, champion: str | None = None, role: str | None = None,
               partner_role: str | None = None, fields: Iterable[str] = ()) -> dict:
    """Fill a tool's arguments from its input schema.

    `partner_role` is the teammate's role for synergy tools; `fields` picks what to ask
    for in `desired_output_fields`.
    """
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    args = {}
    for name, spec in props.items():
        spec = spec or {}
        lname = name.lower()
        enum = _enum_of(spec)
        value = None
        if "champion" in lname and champion is not None:
            value = _match_enum(enum, [champion], by_champion=True) if enum else opgg_champion(champion)
            if spec.get("type") == "array" and value is not None:
                value = [value]
        elif "synergy" in lname and "position" in lname:
            if partner_role:
                value = _match_enum(enum, ROLE_WORDS[partner_role]) if enum else ROLE_WORDS[partner_role][0]
        elif (lname in ("lane", "role", "positions") or lname.endswith("position")) and role:
            value = _match_enum(enum, ROLE_WORDS[role]) if enum else ROLE_WORDS[role][0]
        elif "game_mode" in lname or lname in ("mode", "queue", "queue_type"):
            value = _match_enum(enum, ("ranked", "solo")) or (enum[0] if enum else "ranked")
        elif lname in ("lang", "language", "locale", "hl"):
            value = _match_enum(enum, ("en_US", "en-US", "en")) or "en_US"
        elif name in required and "region" in lname:
            value = _match_enum(enum, ("global", "all", "na")) or (enum[0] if enum else "global")
        elif name in required and spec.get("default") is not None:
            value = spec["default"]
        elif lname == "desired_output_fields" and (name in required or fields):
            value = output_fields(spec, fields)
        if value is not None:
            args[name] = value
    missing = [n for n in required if n not in args]
    if missing:
        raise OpggError(f"Don't know how to fill OP.gg arguments: {', '.join(missing)}")
    return args


# ---------- pulling champions and win rates out of responses ----------

NAME_KEYS = ("champion_name", "championName", "champion", "name", "champion_key", "key", "champion_id",
             "championId", "id")
PARTNER_KEYS = ("synergy_champion_name", "synergy_champion_id", "synergyChampionName", "synergyChampionId")
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


def _champ_of(d: dict, id_names: dict[int, str], keys: tuple[str, ...] = NAME_KEYS) -> str | None:
    for k in keys:
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
    """The win rate of the champion the entry is about (for OP.gg counters: the selected one)."""
    wins = d.get("win") if "win" in d else d.get("wins")
    games = _games_of(d)
    if isinstance(wins, (int, float)) and not isinstance(wins, bool) and games:
        return wins / games  # most precise: OP.gg's rates are rounded to two decimals
    for k in ("my_win_rate", "myWinRate") + WINRATE_KEYS:
        if k in d:
            return _rate(d[k])
    return None


def _entries(payload, id_names, must_have: str | None = None, known: set[str] | None = None,
             keys: tuple[str, ...] = NAME_KEYS):
    """(path, champion, winrate, games, dict) for every champion-with-a-win-rate in the payload.

    `known` (champion keys) drops things that aren't champions, like items in a build list.
    """
    for path, d in _walk(payload):
        if must_have and not any(must_have in p for p in path):
            continue
        champ = _champ_of(d, id_names, keys)
        if champ and known and champ_key(champ) not in known:
            continue
        wr = _winrate_of(d)
        if champ and wr is not None and 0 <= wr <= 1:
            yield path, champ, wr, _games_of(d), d


def _only_role(payload, role: str | None):
    """Drop per-position blocks for other roles (OP.gg's summary.positions[] has one per lane played)."""
    if role is None:
        return payload
    others = {w for r, words in ROLE_WORDS.items() if r != role for w in words}
    if isinstance(payload, dict):
        return {k: _only_role(v, role) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_only_role(v, role) for v in payload
                if not (isinstance(v, dict) and str(v.get("name", "")).lower() in others)]
    return payload


def extract_counters(payload, champion: str, id_names: dict[int, str] | None = None,
                     known: set[str] | None = None, role: str | None = None) -> list[tuple[str, float, int]]:
    """(opponent, win rate of `champion` against them, games) from a champion analysis."""
    id_names = id_names or {}
    me = champ_key(champion)
    payload = _only_role(payload, role)
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
    # OP.gg's synergy rows name both champions; the teammate is the synergy_champion_*
    for _, champ, wr, games, _ in _entries(payload, id_names, "synerg", known, PARTNER_KEYS + NAME_KEYS):
        if champ_key(champ) != me:
            out[champ_key(champ)] = (champ, wr, games)
    return list(out.values())


# ---------- building DraftData for the draft page ----------

def load_draft_data(client: OpggClient, champions: dict[str, set[str]], meta: bool = True,
                    synergy_for: Iterable[str | tuple[str, str | None]] = (),
                    id_names: dict[int, str] | None = None, known: set[str] | None = None,
                    on_progress: Callable[[float, str], None] | None = None,
                    workers: int = WORKERS) -> tuple[DraftData, list[str]]:
    """Fetch tier lists, counters for `champions` (display name -> positions), and synergies.

    `synergy_for` lists our picks, as names or (name, role); OP.gg's synergy tool needs the
    pick's role and is asked once per teammate lane. Without a role, the synergies that come
    with the champion analysis are used instead.

    Calls run `workers` at a time. Returns the data plus a list of problems (one per failed
    call) so the page can show what's missing without failing the whole load. Raises
    OpggError if OP.gg can't be reached at all.
    """
    tools = client.tools()
    data, problems = DraftData(), []
    jobs: list[tuple[str, str | None, str | None, str | None]] = []  # tool, champion, role, teammate role
    if meta and TOOL_META in tools:
        jobs += [(TOOL_META, None, role, None) for role in ROLE_ORDER]
    if TOOL_ANALYSIS in tools:
        jobs += [(TOOL_ANALYSIS, champ, role, None) for champ, roles in champions.items() for role in sorted(roles)]
    if TOOL_SYNERGY in tools:
        for item in synergy_for:
            champ, role = item if isinstance(item, tuple) else (item, None)
            if role is None and len(champions.get(champ, ())) == 1:
                role = next(iter(champions[champ]))
            if role is not None:
                jobs += [(TOOL_SYNERGY, champ, role, other) for other in ROLE_ORDER if other != role]
    if not jobs:
        raise OpggError("OP.gg's server doesn't list the champion tools this app expects.")

    def label(tool, champ, role, partner):
        what = champ or "tier list"
        return f"{tool} {what} {role or ''}{' + ' + partner if partner else ''}".strip()

    ok = done = down = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for job in jobs:
            tool, champ, role, partner = job
            try:
                args = build_args(tools[tool], champion=champ, role=role, partner_role=partner,
                                  fields=TOOL_FIELDS.get(tool, ()))
            except OpggError as e:
                problems.append(f"{label(*job)}: {e}")
                continue
            futures[pool.submit(client.call, tool, args)] = job
        for future in as_completed(futures):
            tool, champ, role, partner = job = futures[future]
            done += 1
            if on_progress:
                on_progress(done / len(futures),
                            f"OP.gg: {champ or 'tier list'}{' ' + role.lower() if role else ''}")
            try:
                payload = future.result()
            except OpggError as e:
                problems.append(f"{label(*job)}: {e}")
                down += isinstance(e, OpggUnavailable)
                if not ok and down >= 3:  # the server is down; a bad champion name is just one problem
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise OpggError(f"OP.gg calls keep failing: {e}") from e
                continue
            ok += 1
            if isinstance(payload, str):
                problems.append(f"{label(*job)}: OP.gg's reply isn't in a format this app reads: {payload[:80]!r}")
                continue
            if tool == TOOL_META:
                entries = extract_meta(payload, role, id_names, known)
                if entries:
                    data.meta[role] = entries
                    data.sources.add(SOURCE)
            elif tool == TOOL_ANALYSIS:
                for opp, wr, games in extract_counters(payload, champ, id_names, known, role=role):
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
