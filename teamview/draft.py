"""Tournament draft order and state, including fearless series locks.

The standard competitive order is 20 steps:

    Ban phase 1   B R B R B R       (3 bans each, blue first)
    Pick phase 1  B | R R | B B | R (snake)
    Ban phase 2   R B R B           (2 bans each, red first)
    Pick phase 2  R | B B | R       (red picks last)

"Blue" here means the team drafting first. Since 2026 pro play uses First Selection
(the team with priority takes either map side or first pick), drafting first is no
longer tied to the blue side of the map, and map side doesn't change the order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BLUE, RED = "blue", "red"
BAN, PICK = "ban", "pick"

SEQUENCE: list[tuple[str, str]] = (
    [(BLUE, BAN), (RED, BAN)] * 3
    + [(BLUE, PICK), (RED, PICK), (RED, PICK), (BLUE, PICK), (BLUE, PICK), (RED, PICK)]
    + [(RED, BAN), (BLUE, BAN)] * 2
    + [(RED, PICK), (BLUE, PICK), (BLUE, PICK), (RED, PICK)]
)
assert len(SEQUENCE) == 20

FEARLESS_MODES = {
    "off": "Off",
    "soft": "Soft: you can't replay your own champions",
    "hard": "Hard: nobody can replay any champion picked earlier",
}


def other(side: str) -> str:
    return RED if side == BLUE else BLUE


def phase_name(step: int) -> str:
    if step < 6:
        return "Ban phase 1"
    if step < 12:
        return "Pick phase 1"
    if step < 16:
        return "Ban phase 2"
    return "Pick phase 2"


def slot_label(step: int) -> str:
    """Broadcast-style label for a step, e.g. "B1" for blue's first pick or "R ban 2"."""
    side, action = SEQUENCE[step]
    n = sum(1 for s, a in SEQUENCE[: step + 1] if s == side and a == action)
    letter = "B" if side == BLUE else "R"
    return f"{letter}{n}" if action == PICK else f"{letter} ban {n}"


@dataclass
class DraftState:
    our_side: str = BLUE  # BLUE = we draft first
    entries: list[str | None] = field(default_factory=list)  # champion key per finished step; None = no ban
    role_overrides: dict[str, str] = field(default_factory=dict)  # champion key -> Riot position
    fearless: str = "hard"  # the team's in-house scrims run fearless; game 1 is unaffected
    our_earlier: set[str] = field(default_factory=set)  # champions we played earlier in the series
    their_earlier: set[str] = field(default_factory=set)

    @property
    def their_side(self) -> str:
        return other(self.our_side)

    @property
    def step(self) -> int:
        return len(self.entries)

    @property
    def done(self) -> bool:
        return self.step >= len(SEQUENCE)

    @property
    def current(self) -> tuple[str, str] | None:
        return None if self.done else SEQUENCE[self.step]

    @property
    def our_turn(self) -> bool:
        return not self.done and SEQUENCE[self.step][0] == self.our_side

    def side_of(self, us: bool) -> str:
        return self.our_side if us else self.their_side

    def _entries(self, side: str, action: str) -> list[str]:
        return [e for e, (s, a) in zip(self.entries, SEQUENCE) if s == side and a == action and e]

    def picks(self, us: bool) -> list[str]:
        return self._entries(self.side_of(us), PICK)

    def bans(self, us: bool) -> list[str]:
        return self._entries(self.side_of(us), BAN)

    def taken(self) -> set[str]:
        return {e for e in self.entries if e}

    def fearless_locked(self, us: bool) -> set[str]:
        """Champions a team can't pick this game because of earlier games in the series."""
        if self.fearless == "hard":
            return self.our_earlier | self.their_earlier
        if self.fearless == "soft":
            return set(self.our_earlier if us else self.their_earlier)
        return set()

    def unavailable(self, us: bool) -> set[str]:
        return self.taken() | self.fearless_locked(us)

    def picks_left(self, us: bool, after: int | None = None) -> int:
        """Picks a team still has from step `after` onward (default: from the current step)."""
        start = self.step if after is None else after
        side = self.side_of(us)
        return sum(1 for s, a in SEQUENCE[start:] if s == side and a == PICK)

    def apply(self, champion_key: str | None):
        if self.done:
            raise ValueError("The draft is already complete.")
        if champion_key and champion_key in self.taken():
            raise ValueError("That champion is already picked or banned.")
        self.entries.append(champion_key)

    def undo(self):
        if self.entries:
            removed = self.entries.pop()
            if removed:
                self.role_overrides.pop(removed, None)
