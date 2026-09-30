"""Draft helper: pick and ban recommendations, step by step through champ select.

Uses the rosters scouted on the main page (st.session_state.results), OP.gg's tier lists
and matchups when reachable, and falls back to the champion tag file and cached games.
"""

from __future__ import annotations

import streamlit as st

from teamview.analysis import ROLE_LABELS, ROLE_ORDER
from teamview.champions import ChampionDB, champ_key, load_synergies
from teamview.draft import BAN, BLUE, FEARLESS_MODES, PICK, RED, SEQUENCE, DraftState, phase_name, slot_label
from teamview.draft_score import (DraftContext, Rec, assign_pick_roles, comp_profile, default_roles,
                                  recommend_bans, recommend_picks, roster_from_reports, watch_list)
from teamview.matchups import DraftData, from_match_cache
from teamview.opgg import OpggClient, OpggError, load_draft_data
from teamview.riot import Cache, champion_names

st.set_page_config(page_title="Draft helper · teamview.lol", page_icon="⚔️", layout="wide")

CACHE_PATH = "cache.db"
POOL_PER_PLAYER = 6  # champions per scouted player to fetch OP.gg matchups for
TONE_COLOR = {"good": "green", "bad": "red", "info": "gray"}


# ---------- cached resources ----------

@st.cache_data(ttl=24 * 3600, show_spinner=False)
def ddragon_names() -> dict[int, str]:
    try:
        return champion_names()
    except Exception:
        return {}


@st.cache_resource
def champion_db(extra: tuple[str, ...]) -> ChampionDB:
    return ChampionDB.load(extra_names=list(extra))


@st.cache_resource
def synergy_notes() -> dict:
    return load_synergies()


@st.cache_resource
def opgg_client() -> OpggClient:
    return OpggClient(cache=Cache(CACHE_PATH))


@st.cache_data(ttl=600, show_spinner=False)
def match_cache_stats() -> DraftData:
    return from_match_cache(CACHE_PATH)


# ---------- state ----------

def draft() -> DraftState:
    if "draft" not in st.session_state:
        st.session_state.draft = DraftState()
    return st.session_state.draft


def lock(key: str | None, role: str | None = None):
    state = draft()
    try:
        state.apply(key)
    except ValueError as e:
        st.session_state.draft_error = str(e)
        return
    if key and role:
        state.role_overrides[key] = role


def undo():
    draft().undo()


def reset():
    old = draft()
    st.session_state.draft = DraftState(our_side=old.our_side, fearless=old.fearless,
                                        our_earlier=old.our_earlier, their_earlier=old.their_earlier)


def next_game():
    """Fearless: move this game's picks into the series locks and start a fresh draft."""
    old = draft()
    st.session_state.draft = DraftState(
        our_side=old.our_side, fearless=old.fearless,
        our_earlier=old.our_earlier | set(old.picks(True)),
        their_earlier=old.their_earlier | set(old.picks(False)),
    )


# ---------- helpers ----------

def chips(reasons) -> str:
    return " ".join(f":{TONE_COLOR[r.tone]}-badge[{r.text}]" for r in reasons)


def team_rosters(db: ChampionDB):
    results = st.session_state.get("results")
    if not results:
        return {}, {}, [], []
    ours, theirs = results.get("ours", []), results.get("theirs", [])
    in_order = st.session_state.draft_in_order
    for side, reports in (("ours", ours), ("theirs", theirs)):
        key = f"draft_roles_{side}"
        ids = {r.riot_id for r in reports if r.found}
        saved = st.session_state.get(key)
        if not saved or not set(saved.values()) <= ids or st.session_state.get(f"{key}_in_order") != in_order:
            st.session_state[key] = default_roles(reports, in_order)
            st.session_state[f"{key}_in_order"] = in_order
    return (roster_from_reports(ours, st.session_state["draft_roles_ours"]),
            roster_from_reports(theirs, st.session_state["draft_roles_theirs"]), ours, theirs)


def role_editor(label: str, reports, state_key: str):
    found = [r.riot_id for r in reports if r.found]
    if not found:
        st.caption(f"No scouted players for {label.lower()}.")
        return
    st.markdown(f"**{label}**")
    current = st.session_state[state_key]
    cols = st.columns(5)
    new = {}
    for col, role in zip(cols, ROLE_ORDER):
        options = ["(nobody)"] + found
        chosen = current.get(role, "(nobody)")
        pick = col.selectbox(ROLE_LABELS[role], options, index=options.index(chosen) if chosen in options else 0,
                             key=f"{state_key}_{role}")
        if pick != "(nobody)":
            new[role] = pick
    st.session_state[state_key] = new


def load_opgg(db: ChampionDB, ours, theirs, ctx: DraftContext, only_new: bool = False) -> bool:
    """Fetch OP.gg tiers and matchups for everyone's pools and the champions locked so far.

    Returns True when new data arrived.
    """
    wanted: dict[str, set[str]] = {}
    if not only_new:
        for roster in (ours, theirs):
            for role, player in roster.items():
                for entry in player.best(POOL_PER_PLAYER):
                    wanted.setdefault(db.name(entry.champion), set()).add(role)
    for us in (True, False):
        for key, role in ctx.roles(us).items():
            wanted.setdefault(db.name(key), set()).add(role)
    fetched = st.session_state.setdefault("opgg_fetched", set())
    todo = {name: {r for r in roles if (name, r) not in fetched} for name, roles in wanted.items()}
    todo = {name: roles for name, roles in todo.items() if roles}
    our_roles = ctx.roles(True)
    synergy_for = [(db.name(k), our_roles.get(k)) for k in ctx.state.picks(True)
                   if (db.name(k), "synergy", our_roles.get(k)) not in fetched]
    if only_new and not todo and not synergy_for:
        return False
    progress = st.progress(0.0, text="Loading OP.gg data")
    try:
        data, problems = load_draft_data(
            opgg_client(), todo, meta=not only_new, synergy_for=synergy_for, id_names=ddragon_names(),
            known=set(db.keys()), on_progress=lambda f, t: progress.progress(min(f, 1.0), text=t))
    except OpggError as e:
        st.session_state.opgg_error = str(e)
        progress.empty()
        return False
    progress.empty()
    st.session_state.opgg_error = None
    current = st.session_state.get("opgg_data") or DraftData()
    st.session_state.opgg_data = data.merge(current)
    st.session_state.opgg_problems = problems
    fetched |= {(name, r) for name, roles in todo.items() for r in roles}
    fetched |= {(name, "synergy", role) for name, role in synergy_for}
    return True


def show_recs(recs: list[Rec], action: str, ours_turn: bool, step: int):
    if not recs:
        st.caption("Nothing to suggest yet.")
        return
    for i, rec in enumerate(recs):
        c1, c2, c3 = st.columns([1, 7, 2], vertical_alignment="center")
        c1.markdown(f"### {rec.score}")
        role = f" · {rec.role_label}" if rec.role_label else ""
        c2.markdown(f"**{rec.champion}**{role}  \n{chips(rec.reasons) or ':gray[No strong signals]'}")
        label = ("Ban" if action == BAN else "Pick") if ours_turn else "They took it"
        c3.button(label, key=f"rec_{step}_{i}", on_click=lock, args=(rec.key, rec.role if ours_turn else None),
                  width="stretch")


def show_team(title: str, color: str, state: DraftState, us: bool, ctx: DraftContext, db: ChampionDB):
    st.markdown(f"#### :{color}[{title}]")
    bans = state.bans(us)
    st.markdown("Bans: " + (" ".join(f":gray-badge[~~{db.name(b)}~~]" for b in bans) if bans else ":gray[none yet]"))
    roles = ctx.roles(us)
    side = state.side_of(us)
    rows = []
    for step, (s, a) in enumerate(SEQUENCE):
        if s != side or a != PICK:
            continue
        label = slot_label(step)
        if step < len(state.entries) and state.entries[step]:
            key = state.entries[step]
            rows.append(f"`{label}` **{db.name(key)}** · {ROLE_LABELS.get(roles.get(key), '?')}")
        elif step == state.step:
            rows.append(f"`{label}` :blue[**picking now**]")
        else:
            rows.append(f"`{label}` :gray[open]")
    st.markdown("  \n".join(rows))
    comp = comp_profile([db.get(k) for k in roles])
    if comp.size:
        bits = []
        if comp.archetype:
            bits.append(f":blue-badge[{comp.archetype}]")
        if comp.scaling:
            bits.append(f":gray-badge[{comp.scaling}]")
        if comp.ap_share is not None:
            bits.append(f":gray-badge[AD {1 - comp.ap_share:.0%} · AP {comp.ap_share:.0%}]")
        bits += [f":orange-badge[{m}]" for m in comp.missing]
        st.markdown(" ".join(bits))


# ---------- page ----------

st.title("Draft helper")
st.caption("Step through champ select and get pick and ban suggestions with the reasons behind them. "
           "Scout both teams on the main page first so suggestions use your players' pools and the "
           "opponent's comfort picks.")

names = ddragon_names()
db = champion_db(tuple(sorted(names.values())))
state = draft()
if "draft_in_order" not in st.session_state:
    # Match the scouting page's "Rosters are in role order" box when it shares its value
    st.session_state.draft_in_order = st.session_state.get("roster_in_order", True)
ours, theirs, our_reports, their_reports = team_rosters(db)

with st.expander("Draft settings", expanded=state.step == 0):
    first = st.radio("Who drafts first?", ["We do (blue order)", "They do (we draft second, red order)"],
                     index=0 if state.our_side == BLUE else 1, horizontal=True, disabled=state.step > 0,
                     help="With First Selection, drafting first isn't tied to map side. "
                          "Map side doesn't change the order, so only this matters here.")
    if state.step == 0:
        state.our_side = BLUE if first.startswith("We") else RED
    state.fearless = st.selectbox("Fearless draft", list(FEARLESS_MODES), format_func=FEARLESS_MODES.get,
                                  index=list(FEARLESS_MODES).index(state.fearless))
    if state.fearless != "off":
        all_names = db.names()
        state.our_earlier = {champ_key(n) for n in st.multiselect(
            "Champions we played earlier in this series", all_names,
            default=sorted(db.name(k) for k in state.our_earlier))}
        state.their_earlier = {champ_key(n) for n in st.multiselect(
            "Champions they played earlier in this series", all_names,
            default=sorted(db.name(k) for k in state.their_earlier))}
    if our_reports or their_reports:
        st.caption("Who plays which role. This decides whose champion pool counts for each pick.")
        st.checkbox("Rosters are in role order", key="draft_in_order",
                    help="Same as on the scouting page: five or more Riot IDs are read as Top, Jungle, "
                         "Mid, ADC, Support. Untick to guess roles from solo queue games.")
        ours, theirs, our_reports, their_reports = team_rosters(db)
        role_editor("Our team", our_reports, "draft_roles_ours")
        role_editor("Opponent", their_reports, "draft_roles_theirs")
        ours, theirs, _, _ = team_rosters(db)

opgg_data = st.session_state.get("opgg_data")
data = DraftData().merge(opgg_data or DraftData()).merge(match_cache_stats())
ctx = DraftContext(state, db, data, ours, theirs, synergy_notes())

# Data sources
src = st.columns([3, 3, 2], vertical_alignment="center")
if ours or theirs:
    src[0].markdown(f":green-badge[Scouting: {len(ours)} of ours, {len(theirs)} of theirs]")
else:
    src[0].markdown(":orange-badge[No scouting data yet]",
                    help="Suggestions use comp needs and OP.gg's meta only until you scout both teams.")
if opgg_data and opgg_data.sources:
    src[1].markdown(f":green-badge[OP.gg: tiers for {len(opgg_data.meta)} roles, {len(opgg_data.lane)} matchups]")
else:
    src[1].markdown(":gray-badge[OP.gg not loaded]",
                    help=f"Using {len(data.lane)} lane matchups from ranked games already in the cache.")
if src[2].button("Load OP.gg data", width="stretch",
                 help="Tier lists for every role, plus matchups for both teams' champion pools. "
                      "Cached for 12 hours."):
    load_opgg(db, ours, theirs, ctx)
    st.rerun()
if st.session_state.get("opgg_error"):
    st.warning(f"{st.session_state.opgg_error} Using the champion tag file and cached games instead.")
elif opgg_data and load_opgg(db, ours, theirs, ctx, only_new=True):  # matchups for newly locked champions
    st.rerun()
if st.session_state.get("opgg_problems"):
    with st.expander(f"{len(st.session_state.opgg_problems)} OP.gg requests failed"):
        st.code("\n".join(st.session_state.opgg_problems))

st.divider()

# Board
left, right = st.columns(2)
our_color, their_color = ("blue", "red") if state.our_side == BLUE else ("red", "blue")
with left:
    show_team("Us" + (" · first pick" if state.our_side == BLUE else ""), our_color, state, True, ctx, db)
with right:
    show_team("Them" + (" · first pick" if state.our_side == RED else ""), their_color, state, False, ctx, db)

st.divider()

if st.session_state.get("draft_error"):
    st.error(st.session_state.pop("draft_error"))

if state.done:
    st.success("Draft complete.")
    b1, b2, b3 = st.columns(3)
    b1.button("Undo last", on_click=undo, width="stretch")
    b2.button("New draft", on_click=reset, width="stretch")
    if state.fearless != "off":
        b3.button("Next game in series", on_click=next_game, width="stretch",
                  help="Locks this game's picks for the rest of the series and starts a new draft.")
    st.stop()

side, action = state.current
ours_turn = state.our_turn
who = "Your" if ours_turn else "Their"
st.subheader(f"{who} {'ban' if action == BAN else 'pick'}: {slot_label(state.step)}")
st.caption(f"{phase_name(state.step)} · step {state.step + 1} of {len(SEQUENCE)}")

# Manual entry
available = sorted({db.name(k) for k in db.keys()} - {db.name(k) for k in state.unavailable(ours_turn)})
e1, e2, e3, e4 = st.columns([4, 2, 2, 2], vertical_alignment="bottom")
choice = e1.selectbox("Champion", available, index=None, key=f"entry_{state.step}",
                      placeholder="Type to search")
role = None
if action == PICK and choice:
    guess = assign_pick_roles(state.picks(ours_turn) + [champ_key(choice)], ctx.roster(ours_turn), db,
                              state.role_overrides).get(champ_key(choice))
    open_roles = ctx.open_roles(ours_turn)
    role = e2.selectbox("Role", open_roles, index=open_roles.index(guess) if guess in open_roles else 0,
                        format_func=ROLE_LABELS.get, key=f"role_{state.step}")
e3.button("Lock in", type="primary", disabled=not choice, width="stretch",
          on_click=lock, args=(champ_key(choice) if choice else None, role))
if action == BAN:
    e4.button("No ban", on_click=lock, args=(None,), width="stretch")
else:
    e4.button("Undo", on_click=undo, disabled=state.step == 0, width="stretch")

# Recommendations
has_signal = bool(ours or theirs or data.has_meta())
if ours_turn:
    st.markdown(f"##### Recommended {'bans' if action == BAN else 'picks'}")
    recs = recommend_bans(ctx) if action == BAN else recommend_picks(ctx)
else:
    st.markdown(f"##### What they might {'ban' if action == BAN else 'pick'}")
    recs = recommend_bans(ctx, us=False) if action == BAN else recommend_picks(ctx, us=False)
if action == BAN and not has_signal:
    st.info("Ban suggestions need scouting data or OP.gg's tier lists. Scout the opponent on the main page "
            "or load OP.gg data above.")
else:
    show_recs(recs, action, ours_turn, state.step)

# Enemy watch list
watch = watch_list(ctx)
if watch:
    st.markdown("##### Still to come from them")
    for w in watch:
        champs = ", ".join(f"{e.champion} ({e.describe()})" for e in w.champions)
        st.markdown(f"- **{ROLE_LABELS[w.role]}** · {w.player}: {champs}")

with st.expander("Fix enemy roles"):
    st.caption("Enemy roles are guessed from each champion's usual roles and who on their team plays it. "
               "Correct a flex pick here once you know where it went.")
    their_roles = ctx.roles(False)
    for key, guessed in their_roles.items():
        options = list(ROLE_ORDER)
        chosen = st.selectbox(db.name(key), options, index=options.index(guessed), format_func=ROLE_LABELS.get,
                              key=f"fix_{key}_{guessed}")  # new widget whenever the guess changes
        if chosen != guessed:
            state.role_overrides[key] = chosen
            st.rerun()

b1, b2 = st.columns(2)
b1.button("Undo last", on_click=undo, disabled=state.step == 0, width="stretch", key="undo_bottom")
b2.button("Reset draft", on_click=reset, width="stretch")
