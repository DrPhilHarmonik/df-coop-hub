"""The hub's copy of the fortress, built from RemoteFortressReader replies and
cut down to what players are allowed to see.

Three things about RFR replies shape this module:

- A block arrives only when something in it changed, and *which* parts
  arrive depends on what changed: tiletypes come with CopyBlock, hidden /
  liquids / dig designations with CopyDesignation. A reply can carry one
  without the other, so the cache merges per field.
- All-air blocks are never sent (except the first block of a reply). A block
  the hub has never received is therefore open air, not unknown.
- The building list rides on the first block of every reply: buildings in the
  requested range in full, every other building as a bare index.

Players never see hidden tiles, or the units and buildings standing on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dfhack_rpc import rfr
from game_tiles import building_sprite, item_sprite

BLOCK = 16
HIDDEN = -1

# df-structures unit_flags1 / unit_flags2 bits.
FLAGS1_INACTIVE = 1 << 1          # dead or otherwise removed
FLAGS1_LEFT = 1 << 8              # left the map
FLAGS1_HIDDEN_IN_AMBUSH = 1 << 18 # an ambusher DF itself is not showing
FLAGS2_KILLED = 1 << 7

UNIT_GONE_FLAGS1 = FLAGS1_INACTIVE | FLAGS1_LEFT | FLAGS1_HIDDEN_IN_AMBUSH


@dataclass
class Block:
    tiles: list[int] | None = None
    hidden: list[bool] | None = None
    water: list[int] | None = None
    magma: list[int] | None = None
    dig: list[int] | None = None
    items: list = field(default_factory=list)
    flows: list = field(default_factory=list)


@dataclass
class MapState:
    width: int = 0   # tiles
    height: int = 0
    depth: int = 0
    blocks: dict[tuple[int, int, int], Block] = field(default_factory=dict)
    buildings: dict[int, dict] = field(default_factory=dict)
    building_names: dict[tuple[int, int, int], str] = field(default_factory=dict)
    building_kinds: dict[int, str] = field(default_factory=dict)
    building_ids: dict[tuple[int, int, int], str] = field(default_factory=dict)
    item_defs: dict[tuple[int, int], str] = field(default_factory=dict)
    creature_defs: dict[int, tuple[str, dict[int, str]]] = field(default_factory=dict)
    # tiletype id -> (shape, material) enum names, for deciding what can be dug
    tile_kinds: dict[int, tuple[str, str]] = field(default_factory=dict)

    # -- ingest -------------------------------------------------------------

    def apply_block_list(self, block_list, *, visual_levels=()) -> tuple[set[tuple[int, int, int]], bool]:
        """Merge a GetBlockList reply. Returns (changed block keys, whether
        the building set changed)."""
        changed: set[tuple[int, int, int]] = set()
        buildings_changed = False
        levels = set(visual_levels)
        unseen = {key for key, block in self.blocks.items() if key[2] in levels and (block.items or block.flows)}
        for i, nb in enumerate(block_list.map_blocks):
            key = (nb.map_x // BLOCK, nb.map_y // BLOCK, nb.map_z)
            unseen.discard(key)
            if i == 0 and len(nb.buildings):
                buildings_changed = self._apply_buildings(nb.buildings)
            block = self.blocks.get(key)
            if block is None:
                block = self.blocks[key] = Block()
            touched = False
            for name in ("tiles", "hidden", "water", "magma"):
                values = getattr(nb, name)
                if len(values) == BLOCK * BLOCK:
                    setattr(block, name, list(values))
                    touched = True
            if len(nb.tile_dig_designation) == BLOCK * BLOCK:
                block.dig = list(nb.tile_dig_designation)
                touched = True
            items, flows = list(nb.items), list(nb.flows)
            if items != block.items or flows != block.flows:
                block.items, block.flows = items, flows
                touched = True
            if touched:
                changed.add(key)
        for key in unseen:
            self.blocks[key].items, self.blocks[key].flows = [], []
            changed.add(key)
        return changed, buildings_changed

    def _apply_buildings(self, instances) -> bool:
        before = dict(self.buildings)
        present = set()
        for b in instances:
            present.add(b.index)
            if not b.HasField("pos_x_min"):
                continue  # out of the requested range; keep what we know
            bt = b.building_type
            self.buildings[b.index] = {
                "i": b.index,
                "x1": b.pos_x_min, "y1": b.pos_y_min, "z1": b.pos_z_min,
                "x2": b.pos_x_max, "y2": b.pos_y_max, "z2": b.pos_z_max,
                "name": self.building_name(bt.building_type, bt.building_subtype, bt.building_custom),
                # The main type ("Civzone", "Workshop", ...) tells areas from objects.
                "kind": self.building_kinds.get(bt.building_type, ""),
            }
            definition = self.building_ids.get((bt.building_type, bt.building_subtype, bt.building_custom),
                         self.building_ids.get((bt.building_type, -1, -1), ""))
            token = building_sprite(definition)
            if token == "WATER_WHEEL":
                token += "_NS_1" if b.pos_y_max > b.pos_y_min else "_WE_1"
            elif token == "SCREWPUMP":
                direction = rfr.BuildingDirection.Name(b.direction) if b.HasField("direction") else "NORTH"
                token += f"_{direction[0]}_1"
            elif token == "AXLE_HORIZONTAL_WE_1" and b.pos_y_max > b.pos_y_min:
                token = "AXLE_HORIZONTAL_NS_1"
            elif token in {"CATAPULT_N", "BALLISTA_N", "BOLT_THROWER_READY_N"} and b.HasField("direction"):
                direction = rfr.BuildingDirection.Name(b.direction)
                if direction != "NONE":
                    token = token.rsplit("_", 1)[0] + "_" + direction.replace("NORTH", "N").replace("SOUTH", "S").replace("EAST", "E").replace("WEST", "W")
            if token:
                self.buildings[b.index]["g"] = token
        for index in list(self.buildings):
            if index not in present:
                del self.buildings[index]
        return self.buildings != before

    def add_building_def(self, btype: int, subtype: int, custom: int, def_id: str, name: str) -> None:
        """Register one GetBuildingDefList entry. The list can repeat a key
        (type 30 is both "Zone" and "NONE"), so the first name wins, and the
        main type comes from the id's prefix ("Civzone/MeetingHall")."""
        self.building_names.setdefault((btype, subtype, custom), name)
        self.building_ids.setdefault((btype, subtype, custom), def_id)
        self.building_kinds.setdefault(btype, def_id.split("/", 1)[0])

    def building_name(self, btype: int, subtype: int, custom: int) -> str:
        names = self.building_names
        return (names.get((btype, subtype, custom))
                or names.get((btype, subtype, -1))
                or names.get((btype, -1, -1))
                or f"building {btype}")

    # -- visibility ---------------------------------------------------------

    def is_hidden(self, x: int, y: int, z: int) -> bool:
        block = self.blocks.get((x // BLOCK, y // BLOCK, z))
        if block is None or block.hidden is None:
            return False  # never-sent blocks are open air
        return bool(block.hidden[(y % BLOCK) * BLOCK + (x % BLOCK)])

    def tile_info(self, x: int, y: int, z: int) -> tuple[str, str, bool] | None:
        """(shape, material, hidden) for a tile, or None where there is no
        tile data: a block never sent (open air) or not yet fetched."""
        block = self.blocks.get((x // BLOCK, y // BLOCK, z))
        if block is None or block.tiles is None:
            return None
        i = (y % BLOCK) * BLOCK + (x % BLOCK)
        shape, material = self.tile_kinds.get(block.tiles[i], (None, None))
        hidden = bool(block.hidden[i]) if block.hidden else False
        return shape, material, hidden

    def dig_at(self, x: int, y: int, z: int) -> int | None:
        """The tile's dig designation, or None if the hub hasn't seen it."""
        block = self.blocks.get((x // BLOCK, y // BLOCK, z))
        if block is None or block.dig is None:
            return None
        return block.dig[(y % BLOCK) * BLOCK + (x % BLOCK)]

    # -- outgoing payloads --------------------------------------------------

    def block_payload(self, key: tuple[int, int, int]) -> dict | None:
        """A block as players see it: tiletype ids with hidden tiles replaced
        by HIDDEN and liquids only where visible. Dig designations always show."""
        block = self.blocks.get(key)
        if block is None or block.tiles is None:
            return None
        hidden = block.hidden or [False] * (BLOCK * BLOCK)
        bx, by, z = key
        out: dict = {
            "x": bx, "y": by, "z": z,
            "t": [HIDDEN if h else t for t, h in zip(block.tiles, hidden)],
        }
        for short, values in (("w", block.water), ("m", block.magma)):
            if values:
                masked = [0 if h else v for v, h in zip(values, hidden)]
                if any(masked):
                    out[short] = masked
        # Designations are the players' own orders, and digging into unexplored
        # rock is how a fort grows, so they show on hidden tiles too.
        if block.dig and any(block.dig):
            out["d"] = list(block.dig)
        objects = []
        for item in block.items:
            x, y = item.pos.x, item.pos.y
            if (not item.HasField("pos") or item.pos.z != z or x // BLOCK != bx or y // BLOCK != by
                    or hidden[(y % BLOCK) * BLOCK + x % BLOCK] or not item.flags1 & 1
                    or item.flags1 & ((1 << 3) | (1 << 4) | (1 << 5) | (1 << 10) | (1 << 11))):
                continue
            name = self.item_defs.get((item.type.mat_type, item.type.mat_index),
                                     self.item_defs.get((item.type.mat_type, -1), ""))
            if name:
                objects.append([x % BLOCK, y % BLOCK, item_sprite(name)])
        if objects:
            out["o"] = objects
        flows = []
        names = {"Miasma": "MIASMA", "Steam": "BOILING", "Mist": "WATER_MIST", "MaterialDust": "DUST",
                 "MagmaMist": "LAVA_MIST", "Smoke": "SMOKE", "Dragonfire": "DRAGONFIRE", "Fire": "FIRE",
                 "Web": "WEB", "MaterialGas": "GAS", "MaterialVapor": "VAPOR", "CampFire": "FIRE"}
        for flow in block.flows:
            x, y = flow.pos.x, flow.pos.y
            if (flow.dead or not flow.HasField("pos") or flow.pos.z != z or x // BLOCK != bx or y // BLOCK != by
                    or hidden[(y % BLOCK) * BLOCK + x % BLOCK] or flow.density <= 0):
                continue
            name = names.get(rfr.FlowType.Name(flow.type))
            if name:
                flows.append([x % BLOCK, y % BLOCK, f"FLOW_{name}:1", min(1, flow.density / 100)])
        if flows:
            out["f"] = flows
        return out

    def blocks_on(self, z: int) -> list[dict]:
        out = []
        for key in self.blocks:
            if key[2] == z:
                payload = self.block_payload(key)
                if payload:
                    out.append(payload)
        return out

    def buildings_on(self, zs) -> list[dict]:
        zs = set(zs)
        return [
            b for b in self.buildings.values()
            if any(b["z1"] <= z <= b["z2"] for z in zs)
            and not self.is_hidden(b["x1"], b["y1"], b["z1"])
        ]

    def visible_units(self, units) -> list[dict]:
        out = []
        for u in units:
            if u.flags1 & UNIT_GONE_FLAGS1 or u.flags2 & FLAGS2_KILLED:
                continue
            if not (0 <= u.pos_x < self.width and 0 <= u.pos_y < self.height
                    and 0 <= u.pos_z < self.depth):
                continue
            if self.is_hidden(u.pos_x, u.pos_y, u.pos_z):
                continue
            pc = u.profession_color
            color = (f"#{pc.red & 255:02x}{pc.green & 255:02x}{pc.blue & 255:02x}"
                     if u.HasField("profession_color") else "#ffffff")
            out.append({"i": u.id, "x": u.pos_x, "y": u.pos_y, "z": u.pos_z,
                        "n": u.name, "c": color})
            if u.HasField("race"):
                out[-1]["race"] = u.race.mat_type
                out[-1]["caste"] = u.race.mat_index
        return out
