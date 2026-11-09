"""What players may do to the fortress, and the record of who did it.

Digging goes through RemoteFortressReader's SendDigCommand, which writes the
designation, flags the block, gives the job DF's default priority (4) and
cancels any dig job already on the tile. It does not check that the tile can
take the designation, which DF's own UI does, so that check lives here: a
"dig" on open air or a constructed wall would otherwise sit there forever.

Unexplored tiles accept any dig kind, as in DF; digging into the unknown is
how a fortress grows.
"""

from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from dfhack_rpc import rfr

# client name -> RFR TileDigDesignation
DIG_KINDS = {
    "dig": rfr.DEFAULT_DIG,
    "updown": rfr.UP_DOWN_STAIR_DIG,
    "up": rfr.UP_STAIR_DIG,
    "down": rfr.DOWN_STAIR_DIG,
    "ramp": rfr.RAMP_DIG,
    "channel": rfr.CHANNEL_DIG,
    "erase": rfr.NO_DIG,
}
DIG_NAMES = {v: k for k, v in DIG_KINDS.items()}

MAX_DIG_TILES = 4096

SOLID = {"WALL", "FORTIFICATION"}
FLOORISH = {"FLOOR", "BOULDER", "PEBBLES", "SAPLING", "SHRUB",
            "STAIR_UP", "STAIR_DOWN", "STAIR_UPDOWN", "RAMP"}
UNDIGGABLE_MATERIALS = {"CONSTRUCTION", "TREE_MATERIAL", "ROOT", "MAGMA",
                        "POOL", "BROOK", "RIVER", "UNDERWORLD_GATE"}

# kinds that cut into a solid tile, and kinds that work from a floor
ON_SOLID = {"dig", "updown", "up", "down", "ramp", "channel"}
ON_FLOOR = {"channel", "down", "updown"}


def eligible(kind: str, shape: str | None, material: str | None, hidden: bool) -> bool:
    """Can `kind` be designated on this tile? shape/material are tiletype
    enum names; None means no tile there (open air)."""
    if kind == "erase":
        return True
    if hidden:
        return True
    if shape is None or material in UNDIGGABLE_MATERIALS:
        return False
    if shape in SOLID:
        return kind in ON_SOLID
    if shape in FLOORISH:
        return kind in ON_FLOOR
    return False


def rect_tiles(x1: int, y1: int, x2: int, y2: int, width: int, height: int):
    """Normalise and clamp a rectangle to the map; returns (x0, y0, x1, y1)
    inclusive, or None if it misses the map."""
    ax, bx = sorted((x1, x2))
    ay, by = sorted((y1, y2))
    ax, ay = max(ax, 0), max(ay, 0)
    bx, by = min(bx, width - 1), min(by, height - 1)
    if ax > bx or ay > by:
        return None
    return ax, ay, bx, by


# -- roles -----------------------------------------------------------------------
#
# What each player may do. The hub enforces this on every message; the client
# only greys out what a role can't use. Players without a role in the token
# file are overseers, which is how everyone played before roles existed.

PERMISSIONS = ("dig", "build", "remove", "order", "labor", "military", "nobles", "pause", "chat")

ROLES: dict[str, frozenset[str]] = {
    "overseer": frozenset(PERMISSIONS),
    "builder": frozenset({"dig", "build", "remove", "pause", "chat"}),
    "miner": frozenset({"dig", "pause", "chat"}),
    "quartermaster": frozenset({"order", "labor", "pause", "chat"}),
    "commander": frozenset({"military", "pause", "chat"}),
    "visitor": frozenset({"chat"}),
}
DEFAULT_ROLE = "overseer"

# message type -> the permission it needs (anything absent needs none)
NEEDS = {"dig": "dig", "build": "build", "remove": "remove", "order": "order",
         "cancel_order": "order", "edit_order": "order", "move_order": "order",
         "edit_condition": "order", "remove_condition": "order", "add_condition": "order",
         "work_detail_member": "labor", "work_detail_mode": "labor",
         "workshop_job": "order", "workshop_worker": "order", "farm_crop": "build", "farm_fertilize": "build",
         "squad_candidates": "military", "squad_add": "military", "squad_remove": "military",
         "rename_squad": "military", "squad_station": "military", "squad_targets": "military",
         "squad_kill": "military", "squad_clear": "military", "noble_candidates": "nobles", "appoint_noble": "nobles",
         "vacate_noble": "nobles", "stockpile_category": "build", "stockpile_containers": "build",
         "rename_place": "build", "zone_candidates": "build", "zone_owner": "build", "zone_active": "build",
         "pause": "pause", "chat": "chat", "ping": "chat"}

VERB = {"dig": "dig", "build": "build", "remove": "remove things", "order": "manage work orders", "labor": "change work details",
        "military": "manage squads", "nobles": "appoint nobles", "pause": "pause the game", "chat": "chat or ping"}


PLAYER_COLORS = ["#ff5555", "#55aaff", "#ffdd33", "#55ff88", "#ff66dd", "#ff9933", "#33ffee", "#bb88ff"]


def color_for(name: str) -> str:
    """A player's colour, the same every session, so their cursor and their
    designations always match."""
    return PLAYER_COLORS[zlib.crc32(name.encode()) % len(PLAYER_COLORS)]


@dataclass
class RateLimiter:
    """Token bucket: `rate` actions per second, bursts up to `burst`."""
    rate: float = 2.0
    burst: float = 10.0
    tokens: float = 10.0
    stamp: float = field(default_factory=time.monotonic)

    def take(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class ActionLog:
    """Append-only JSON-lines record of every order, one file per save, so
    "who designated this?" survives hub restarts."""

    def __init__(self, path: Path):
        self.path = path

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def entries(self):
        try:
            with self.path.open() as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue  # a torn last line from a crash
        except FileNotFoundError:
            return


def designation_spans(tiles) -> list[list[int]]:
    """Compress coordinates on one level into sorted [x, first_y, last_y] runs."""
    spans = []
    for x, y, _ in sorted(set(tiles)):
        if spans and spans[-1][0] == x and spans[-1][2] + 1 == y:
            spans[-1][2] = y
        else:
            spans.append([x, y, y])
    return spans


class Owners:
    """Who placed the designation on each tile, and at what priority (1 first,
    7 last; DF's default is 4). Entries outlive the designation until the
    hub sees the tile's dig flag clear (dug out or cancelled in DF), then
    they are dropped."""

    def __init__(self):
        self.by_tile: dict[tuple[int, int, int], tuple[str, int]] = {}

    def apply(self, kind: str, tiles, name: str, priority: int = 4) -> None:
        if kind == "erase":
            for t in tiles:
                self.by_tile.pop(t, None)
        else:
            for t in tiles:
                self.by_tile[t] = (name, priority)

    def replay(self, log: ActionLog) -> None:
        for e in log.entries():
            if e.get("type") != "dig":
                continue
            z = e["z"]
            if "spans" in e:
                tiles = ((x, y, z) for x, first_y, last_y in e["spans"]
                         for y in range(first_y, last_y + 1))
            else:
                x0, y0, x1, y1 = e["rect"]
                tiles = ((x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
            self.apply(e["kind"], tiles, e["player"], e.get("priority", 4))

    def on_levels(self, zs) -> list[tuple[int, int, int, str, int]]:
        zs = set(zs)
        return [(x, y, z, n, p) for (x, y, z), (n, p) in self.by_tile.items() if z in zs]

    def prune(self, z: int, is_designated) -> bool:
        """Drop owners on level z whose tile no longer carries a designation.
        Returns whether anything was dropped."""
        gone = [t for t in self.by_tile if t[2] == z and is_designated(*t) is False]
        for t in gone:
            del self.by_tile[t]
        return bool(gone)
