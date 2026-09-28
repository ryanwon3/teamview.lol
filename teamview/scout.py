"""Fetches everything needed for a player report from the Riot API."""

from __future__ import annotations

from typing import Callable

from .analysis import PlayerReport, build_player_report
from .riot import QUEUE_SOLO, RiotClient


def scout_player(client: RiotClient, name: str, tag: str, games: int = 20,
                 queues: tuple[int, ...] = (QUEUE_SOLO,),
                 champ_names: dict[int, str] | None = None,
                 on_match: Callable[[], None] | None = None) -> PlayerReport:
    riot_id = f"{name}#{tag}"
    account = client.account(name, tag)
    if account is None:
        return build_player_report(riot_id, None, [], [], [])
    puuid = account["puuid"]
    riot_id = f"{account.get('gameName', name)}#{account.get('tagLine', tag)}"

    ids: list[str] = []
    for queue in queues:
        ids += client.match_ids(puuid, queue, games)
    # Newest first across queues (match ids increase over time within a platform).
    ids = sorted(set(ids), key=lambda m: int(m.split("_")[1]), reverse=True)[:games]

    matches = []
    for match_id in ids:
        match = client.match(match_id)
        if match:
            matches.append(match)
        if on_match:
            on_match()

    return build_player_report(
        riot_id, puuid, client.league_entries(puuid), client.top_mastery(puuid), matches, champ_names
    )
