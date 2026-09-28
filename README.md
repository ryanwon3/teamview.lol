# teamview.lol

Scout an opposing League of Legends team from their Riot IDs. Paste both rosters and get:

- **Team strength**: each team's average rank, and how far ahead or behind you are
- **Lane by lane**: each player seated in a different role, matching their recent games
- **Strongest player** on each team
- **Champion pools**: games, win rate, KDA and CS/min per champion, plus top mastery
- **Picks to watch and suggested bans**: champions a player plays a lot *and* wins on

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste your key into .env
streamlit run app.py
```

Get a key at [developer.riotgames.com](https://developer.riotgames.com). A development key expires
every 24 hours; for regular use, register a free **Personal API key**. `.env` is git-ignored, so the
key never gets committed. On Streamlit Community Cloud, set `RIOT_API_KEY` in the app's secrets.

## How it works

Data comes from the Riot API: Account-V1 (Riot ID to PUUID), League-V4 (rank), Champion-Mastery-V4,
and Match-V5 (recent ranked solo and flex games). Responses are cached in `cache.db` (finished matches
forever, ranks for 30 minutes), so re-scouting the same team is nearly instant. The client respects
the dev key limits of 20 requests/second and 100 per 2 minutes, so a fresh 10-player scout at 20 games
each takes about 3–4 minutes the first time.

**Strength** = rank as points (100 per division, 400 per tier, LP on top; Master+ continues by LP)
plus a recent-form bonus of `(win rate − 50%) × 800`, scaled down when a player has fewer than 20
recent games. Unranked players are shown but left out of the team average.

**Threat** for a champion = games × smoothed win rate ÷ 50%, where the smoothed win rate is
`(wins + 2) / (games + 4)`. So a champion someone spams and wins on ranks highest, and a 1–0 fluke
doesn't.

## Limitations

- **No scrim or custom game data.** Riot made custom games private: Match-V5 returns 404 for them
  ([developer-relations #472](https://github.com/RiotGames/developer-relations/issues/472)). Only
  games played on tournament codes you created are readable, so your own scrims could be tracked
  later that way, but not an opponent's scrims against other teams.
- Solo queue habits are a proxy for what a team plays together.

## Draft helper

The **Draft helper** page (in the sidebar) walks through champ select one step at a time and
suggests picks and bans, each with the reasons behind it. Scout both teams on the main page first:
the helper uses your players' pools for comfort and the opponent's pools for target bans and blind
pick risk.

- **Draft order**: the standard 20-step tournament draft. Choose whether you draft first; with
  2026's First Selection rule that's separate from map side, and map side doesn't change the order.
- **Fearless**: off, soft (a team can't replay its own champions) or hard (nobody can replay any
  champion picked earlier in the series). After a game, "Next game in series" carries the locks over.
- **Pick score** (0–100) mixes player comfort, strength in that role this patch, the lane matchup
  if the enemy laner is known, a penalty for blind picking into their laner's pool, what your comp
  is missing (frontline, engage, damage mix, peel, waveclear), known combos, denying their comfort
  picks, and flex value early on. Early picks lean on safety and power, late picks on counters and comp.
- **Ban score** favors what a specific enemy player is best on, strong meta picks, and in phase 2
  counters to champions you already locked, only for roles they still have to fill.

Data comes from three places:

1. **OP.gg** tier lists, counters and synergies through OP.gg's official MCP server
   (`mcp-api.op.gg`, press *Load OP.gg data*; cached 12 hours). It isn't a documented public API,
   so if it fails the page says so and carries on with the next two.
2. **`teamview/data/champions.csv`**, a hand-written tag file: each champion's roles, damage type
   and 0–3 ratings for frontline, engage, peel, poke, waveclear, pick, split push, scaling and how
   safe it is to blind pick. Edit it in a spreadsheet; `synergies.csv` holds known combos.
3. **Lane matchups from cached ranked games** that scouting already downloaded, heavily pulled
   toward 50% because the samples are small.

## Tests

```bash
python -m pytest
```
