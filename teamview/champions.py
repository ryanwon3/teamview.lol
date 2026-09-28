"""Champion tags used by the draft helper: roles, damage type and team-comp ratings.

The ratings live in `data/champions.csv` so the team can edit them in a spreadsheet.
Every rating is 0-3:

- frontline: can stand in front and soak damage
- engage:    can start a fight on its own terms
- peel:      protects its carries (disengage, shields, heals)
- poke:      damages from long range before a fight
- waveclear: clears minion waves fast
- pick:      catches a single target out of position
- split:     wins a side lane alone
- scaling:   1 = strongest early, 2 = mid game, 3 = late game
- blind:     safe to pick before seeing the lane opponent (used when there is no matchup data)
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# CSV role names -> Riot's teamPosition values (what the scouting data uses).
CSV_ROLES = {"top": "TOP", "jungle": "JUNGLE", "mid": "MIDDLE", "bot": "BOTTOM", "support": "UTILITY"}
RATINGS = ("frontline", "engage", "peel", "poke", "waveclear", "pick", "split", "scaling", "blind")

# Riot's internal ids that don't match the display name once punctuation is stripped.
ALIASES = {"monkeyking": "wukong", "nunuwillump": "nunu", "renataglasc": "renata"}


def champ_key(name: str) -> str:
    """Normalize any spelling of a champion to one key.

    Match-V5 says "MonkeyKing" and "KSante", Data Dragon says "Wukong" and "K'Sante",
    other sites use "MONKEY_KING". All of them map to the same key.
    """
    key = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return ALIASES.get(key, key)


@dataclass
class Champion:
    name: str
    roles: list[str] = field(default_factory=list)  # Riot positions, most common first
    damage: str = "MIX"  # AD, AP or MIX
    ratings: dict[str, int] = field(default_factory=dict)
    known: bool = True  # False for champions missing from the CSV (e.g. a brand-new release)

    @property
    def key(self) -> str:
        return champ_key(self.name)

    def rating(self, name: str) -> int:
        return self.ratings.get(name, 1)

    def plays(self, role: str) -> bool:
        return role in self.roles

    def role_fit(self, role: str) -> float:
        """How normal it is to see this champion in `role`: 1.0 main role, less for off-roles."""
        if not self.roles:
            return 0.3
        if role not in self.roles:
            return 0.05
        return (1.0, 0.6, 0.4, 0.3)[min(self.roles.index(role), 3)]


class ChampionDB:
    def __init__(self, champions: list[Champion]):
        self.by_key = {c.key: c for c in champions}

    @classmethod
    def load(cls, path: Path | None = None, extra_names: list[str] | None = None) -> "ChampionDB":
        """Load the CSV. `extra_names` (e.g. from Data Dragon) adds champions the CSV doesn't know yet."""
        champions = []
        with open(path or DATA_DIR / "champions.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champions.append(Champion(
                    name=row["name"].strip(),
                    roles=[CSV_ROLES[r] for r in row["roles"].split() if r in CSV_ROLES],
                    damage=row["damage"].strip().upper(),
                    ratings={k: int(row[k]) for k in RATINGS},
                ))
        db = cls(champions)
        for name in extra_names or []:
            if champ_key(name) not in db.by_key:
                db.by_key[champ_key(name)] = Champion(name=name, known=False)
        return db

    def get(self, name_or_key: str) -> Champion:
        key = champ_key(name_or_key)
        return self.by_key.get(key) or Champion(name=str(name_or_key), known=False)

    def name(self, name_or_key: str) -> str:
        return self.get(name_or_key).name

    def names(self) -> list[str]:
        return sorted(c.name for c in self.by_key.values())

    def keys(self) -> list[str]:
        return list(self.by_key)


def load_synergies(path: Path | None = None) -> dict[frozenset, str]:
    """Hand-picked champion pairs that combo well, keyed by the unordered pair of keys."""
    out = {}
    with open(path or DATA_DIR / "synergies.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[frozenset((champ_key(row["champion"]), champ_key(row["partner"])))] = row["note"].strip()
    return out
