"""teamview.lol: scout an opposing League of Legends team from their Riot IDs.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from teamview.analysis import (ROLE_LABELS, PlayerReport, TeamSummary, assign_roles, ban_suggestions,
                               lane_matchups, parse_riot_ids, summarize_team)
from teamview.riot import PLATFORM_TO_REGION, QUEUE_FLEX, QUEUE_SOLO, RiotClient, RiotError, champion_names
from teamview.scout import scout_player

load_dotenv()
st.set_page_config(page_title="teamview.lol", page_icon="🔎", layout="wide")


def api_key() -> str:
    key = os.environ.get("RIOT_API_KEY", "")
    if not key:
        try:
            key = st.secrets.get("RIOT_API_KEY", "")
        except FileNotFoundError:
            pass
    return key


@st.cache_resource
def get_client(key: str, platform: str) -> RiotClient:
    return RiotClient(key, platform=platform, cache_path="cache.db")


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def get_champ_names() -> dict[int, str]:
    try:
        return champion_names()
    except Exception:
        return {}


def scout_team(client, ids, games, queues, champ_names, progress, done, total, label):
    reports = []
    for name, tag in ids:
        def tick():
            done[0] += 1
            progress.progress(min(done[0] / total, 1.0), text=f"{label}: {name}#{tag}")
        tick()
        reports.append(scout_player(client, name, tag, games, queues, champ_names, on_match=tick))
    return reports


def lane_label(p: PlayerReport, lanes: dict[int, str]) -> str:
    """The lane a player is seated in, else their most-played solo queue role."""
    return ROLE_LABELS.get(lanes.get(id(p)) or p.main_role, "?")


def players_table(players: list[PlayerReport], lanes: dict[int, str]) -> pd.DataFrame:
    rows = []
    for p in players:
        if not p.found:
            continue
        wr = p.recent_winrate
        rows.append({
            "Player": p.riot_id,
            "Lane": ROLE_LABELS.get(lanes.get(id(p)), "-"),
            "Solo Q role": ROLE_LABELS.get(p.main_role, "?"),
            "Rank": p.rank_label + (f" ({p.queue})" if p.queue == "Flex" else ""),
            "Strength": round(p.strength) if p.strength is not None else None,
            "Recent WR": f"{wr:.0%} ({p.recent_games})" if wr is not None else "-",
            "Top champions": ", ".join(c.champion for c in p.champions[:4]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["Strength"] = df["Strength"].astype("Int64")
    return df.sort_values("Strength", ascending=False, na_position="last")


def champ_table(p: PlayerReport) -> pd.DataFrame:
    return pd.DataFrame([{
        "Champion": c.champion,
        "Games": c.games,
        "Win rate": f"{c.winrate:.0%}",
        "KDA": round(c.kda, 2),
        "CS/min": round(c.cs_per_min, 1),
    } for c in p.champions])


def show_team(summary: TeamSummary, seats: dict[str, PlayerReport], is_opponent: bool):
    lanes = {id(p): role for role, p in seats.items()}
    if summary.missing:
        st.warning("Riot ID not found: " + ", ".join(summary.missing))
    if summary.unranked:
        st.info("Unranked (left out of the team average): " + ", ".join(summary.unranked))

    if summary.strongest:
        s = summary.strongest
        top = ", ".join(c.champion for c in s.champions[:3]) or "no recent games"
        st.markdown(f"**Strongest player:** {s.riot_id}, {s.rank_label}, "
                    f"{lane_label(s, lanes)}, plays {top}")

    st.dataframe(players_table(summary.players, lanes), hide_index=True, width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("Picks to watch" if is_opponent else "Our comfort picks")
        if summary.threats:
            st.dataframe(pd.DataFrame([{
                "Champion": t.champion, "Player": t.player, "Games": t.games,
                "Win rate": f"{t.winrate:.0%}", "KDA": round(t.kda, 2),
            } for t in summary.threats]), hide_index=True, width="stretch")
        else:
            st.caption("Not enough recent games.")
    with right:
        if is_opponent:
            st.subheader("Suggested bans")
            for t in ban_suggestions(summary):
                st.markdown(f"- **{t.champion}** ({t.player}: {t.games} games, {t.winrate:.0%} WR)")

    st.subheader("Champion pools")
    for p in summary.players:
        if not p.found:
            continue
        with st.expander(f"{p.riot_id} · {p.rank_label} · {lane_label(p, lanes)}"):
            if p.champions:
                st.dataframe(champ_table(p), hide_index=True, width="stretch")
            else:
                st.caption("No recent ranked games.")
            if p.mastery:
                st.caption("Highest mastery: " + ", ".join(
                    f"{name} ({pts:,})" for name, pts in p.mastery[:6]))


# ---------- UI ----------

st.title("teamview.lol")
st.caption("Paste Riot IDs (Name#TAG), one per line or comma separated, in role order: "
           "Top, Jungle, Mid, ADC, Support, then subs. Lobby chat like \"Name #TAG joined the "
           "lobby\" works too: untick \"Rosters are in role order\" and lanes are guessed.")

with st.sidebar:
    platform = st.selectbox("Server", list(PLATFORM_TO_REGION), index=0)
    default_tag = st.text_input("Default tag", value="NA1",
                                help="Used when a line has no #TAG.")
    in_order = st.checkbox("Rosters are in role order", value=True,
                           help="Lines are Top, Jungle, Mid, ADC, Support, then subs. Untick for "
                                "pasted lobby chat, and lanes are guessed from solo queue games. "
                                "Teams with fewer than five players are always guessed.")
    games = st.slider("Games per player", 10, 50, 20, step=5,
                      help="More games give better champion pools but take longer the first time.")
    include_flex = st.checkbox("Include flex queue", value=True)
    st.caption("Custom games and scrims are private in Riot's API, so this uses "
               "ranked solo and flex games only.")

col_us, col_them = st.columns(2)
placeholder = "Top#NA1\nJungle#NA1\nMid#NA1\nADC#NA1\nSupport#NA1"
our_text = col_us.text_area("Our team", height=160, placeholder=placeholder)
their_text = col_them.text_area("Opponent", height=160, placeholder=placeholder)

if st.button("Scout", type="primary"):
    key = api_key()
    ours = parse_riot_ids(our_text, default_tag)
    theirs = parse_riot_ids(their_text, default_tag)
    if not key:
        st.error("No Riot API key. Copy .env.example to .env and paste your key.")
    elif not theirs:
        st.error("Add at least one opponent Riot ID.")
    else:
        queues = (QUEUE_SOLO, QUEUE_FLEX) if include_flex else (QUEUE_SOLO,)
        client = get_client(key, platform)
        names = get_champ_names()
        total = (len(ours) + len(theirs)) * (games + 1)
        progress = st.progress(0.0, text="Starting")
        done = [0]
        try:
            st.session_state.results = {
                "ours": scout_team(client, ours, games, queues, names, progress, done, total, "Our team"),
                "theirs": scout_team(client, theirs, games, queues, names, progress, done, total, "Opponent"),
            }
        except RiotError as e:
            st.error(str(e))
        progress.empty()

results = st.session_state.get("results")
if results:
    ours, theirs = summarize_team(results["ours"]), summarize_team(results["theirs"])

    a, b, c = st.columns(3)
    a.metric("Our average", ours.avg_label)
    b.metric("Their average", theirs.avg_label)
    if ours.avg_strength is not None and theirs.avg_strength is not None:
        edge = ours.avg_strength - theirs.avg_strength
        c.metric("Our edge", f"{edge:+.0f} pts", f"about {abs(edge) / 100:.1f} divisions "
                 f"{'ahead' if edge >= 0 else 'behind'}")

    our_seats = assign_roles(results["ours"], in_order)
    their_seats = assign_roles(results["theirs"], in_order)
    matchups = lane_matchups(results["ours"], results["theirs"], in_order)
    if matchups:
        st.subheader("Lane by lane")
        df = pd.DataFrame(matchups)
        df["Edge"] = df["Edge"].map(lambda d: "-" if pd.isna(d) else f"{d:+.0f}")
        st.dataframe(df, hide_index=True, width="stretch")

    tab_them, tab_us = st.tabs(["Opponent", "Our team"])
    with tab_them:
        show_team(theirs, their_seats, is_opponent=True)
    with tab_us:
        show_team(ours, our_seats, is_opponent=False)

    st.caption("Strength = rank as points (100 per division, 400 per tier) plus a recent-form "
               "bonus: +80 for a 60% win rate over 20+ games, -80 for 40%.")
