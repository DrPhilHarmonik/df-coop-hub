"""Everything players can build, and how each maps onto a quickfort key.

Players never send quickfort text: they pick an id from this catalog, and
the hub builds the cell text from it. Two kinds of item:

- fixed: one click places it. Buildings bigger than a tile (workshops are
  3x3, the trade depot 5x5) are centred on the clicked tile, which is what
  quickfort does with a single-cell key.
- area: two corners, like digging. The text becomes "key(WxH)" anchored at
  the top-left corner, up to MAX_AREA_SIDE on each side.

The build menu is DF's own (buildmenu.py, generated from the running game);
stockpile and zone keys are from DFHack 53.16-r3's quickfort place/zone.lua.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_AREA_SIDE = 31  # quickfort's own limit for extents of most buildings


@dataclass(frozen=True)
class Item:
    id: str
    label: str
    group: str       # the menu page it lives on
    mode: str        # quickfort mode: build, place (stockpiles) or zone
    key: str         # quickfort key; "" if quickfort can't place it
    w: int = 1       # footprint of fixed items
    h: int = 1
    area: bool = False

    @property
    def available(self) -> bool:
        return bool(self.key)

    def text(self, w: int = 1, h: int = 1) -> str:
        return f"{self.key}({w}x{h})" if self.area else self.key

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "group": self.group,
                "w": self.w, "h": self.h, "area": self.area, "available": self.available}


def _area(group, mode, rows):
    return [Item(i, label, group, mode, key, area=True) for i, key, label in rows]


# The build menu itself is DF's, captured from the running game and matched
# to quickfort by building type: see buildmenu.py and tools/gen_build_menu.py.
from buildmenu import BUILD_ITEMS, PAGES as BUILD_PAGES  # noqa: E402

STOCKPILES = _area("STOCKPILES", "place", [
    ("sp_food", "f", "Food"), ("sp_furniture", "u", "Furniture"), ("sp_stone", "s", "Stone"),
    ("sp_wood", "w", "Wood"), ("sp_bars", "b", "Bars & blocks"), ("sp_gems", "e", "Gems"),
    ("sp_goods", "g", "Finished goods"), ("sp_cloth", "h", "Cloth"), ("sp_leather", "l", "Leather"),
    ("sp_refuse", "r", "Refuse"), ("sp_corpses", "y", "Corpses"), ("sp_animals", "a", "Animals"),
    ("sp_ammo", "z", "Ammo"), ("sp_weapons", "p", "Weapons"), ("sp_armor", "d", "Armor"),
    ("sp_coins", "n", "Coins"), ("sp_sheets", "S", "Sheets"),
])
ZONES = _area("ZONES", "zone", [
    ("z_meeting", "m", "Meeting area"), ("z_bedroom", "b", "Bedroom"), ("z_dining", "h", "Dining hall"),
    ("z_dormitory", "D", "Dormitory"), ("z_office", "o", "Office"), ("z_barracks", "B", "Barracks"),
    ("z_pasture", "n", "Pen / pasture"), ("z_water", "w", "Water source"), ("z_fishing", "f", "Fishing"),
    ("z_dump", "d", "Garbage dump"), ("z_tomb", "T", "Tomb"), ("z_dungeon", "j", "Dungeon"),
    ("z_gather", "g", "Gather fruit"), ("z_sand", "s", "Sand"), ("z_clay", "c", "Clay"),
    ("z_training", "t", "Animal training"), ("z_archery", "a", "Archery range"),
])

ITEMS: list[Item] = [
    *(Item(i, label, page, "build", key, w=w, h=h, area=area) for i, label, key, w, h, area, page in BUILD_ITEMS),
    *STOCKPILES,
    *ZONES,
]

# Menu pages for the client: DF's build menu (opened with b), plus stockpiles
# (p) and zones (z), which DF keeps outside the build menu too. Each entry is
# a sub-page or an item, with DF's key on that page ("" = no default key).
PAGE_LABELS = {"MAIN": "Build", "WORKSHOPS": "Workshops", "WORKSHOPS_FURNACES": "Furnaces",
               "WORKSHOPS_CLOTHING": "Clothing and leather", "WORKSHOPS_FARMING": "Farming",
               "FURNITURE": "Furniture", "DOORS_HATCHES": "Doors/hatches", "WALLS_FLOORS": "Constructions",
               "MACHINES_FLUIDS": "Machines/fluids", "CAGES_RESTRAINTS": "Cages/restraints", "TRAPS": "Traps",
               "MILITARY": "Military", "STOCKPILES": "Stockpiles", "ZONES": "Zones"}
PAGES: dict[str, list[dict]] = {
    page: [{"page": target, "label": label, "key": key} if kind == "page" else {"item": target, "key": key}
           for kind, target, label, key in entries]
    for page, entries in BUILD_PAGES.items()
}
PAGES["STOCKPILES"] = [{"item": i.id, "key": ""} for i in STOCKPILES]
PAGES["ZONES"] = [{"item": i.id, "key": ""} for i in ZONES]
GROUPS = list(PAGES)

BY_ID: dict[str, Item] = {item.id: item for item in ITEMS}


# -- work orders ---------------------------------------------------------------
#
# DFHack's `workorder` JSON can carry Lua code (amount_total), so no player text
# reaches it: players pick an order id, a material from that order's list and
# a number, and lua/rpc/coop.lua builds the JSON from validated pieces. Job and
# reaction names are checked against DF 53.16 (df.job_type and DFHack's own
# order library in hack/data/orders/).

MAX_ORDER_AMOUNT = 999
# df.workquota_frequency_type name -> what players read
ORDER_FREQUENCIES = {"OneTime": "One time", "Daily": "Daily", "Monthly": "Monthly",
                     "Seasonally": "Seasonally", "Yearly": "Yearly"}
ORDER_MOVES = ("up", "down", "top", "bottom")

_COMPARE_WORDS = {"AtLeast": "at least", "AtMost": "at most", "GreaterThan": "more than",
                  "LessThan": "fewer than", "Exactly": "exactly", "Not": "not"}


@dataclass(frozen=True)
class Order:
    id: str
    label: str
    group: str
    job: str
    reaction: str = ""
    materials: tuple[str, ...] = ()   # first is the default; empty = DF chooses

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "group": self.group, "materials": list(self.materials)}


def _orders(group, rows):
    return [Order(i, label, group, job, reaction, mats) for i, label, job, reaction, mats in rows]


WOOD, STONE, WOOD_OR_STONE = ("wood",), ("stone",), ("wood", "stone")

ORDERS: list[Order] = [
    *_orders("Furniture & containers", [
        ("o_bed", "Beds", "ConstructBed", "", WOOD),
        ("o_table", "Tables", "ConstructTable", "", WOOD_OR_STONE),
        ("o_chair", "Chairs", "ConstructThrone", "", WOOD_OR_STONE),
        ("o_door", "Doors", "ConstructDoor", "", WOOD_OR_STONE),
        ("o_cabinet", "Cabinets", "ConstructCabinet", "", WOOD_OR_STONE),
        ("o_chest", "Chests / boxes", "ConstructChest", "", WOOD_OR_STONE),
        ("o_coffin", "Coffins", "ConstructCoffin", "", WOOD_OR_STONE),
        ("o_statue", "Statues", "ConstructStatue", "", STONE),
        ("o_barrel", "Barrels", "MakeBarrel", "", WOOD),
        ("o_bin", "Bins", "ConstructBin", "", WOOD),
        ("o_bucket", "Buckets", "MakeBucket", "", WOOD),
        ("o_cage", "Cages", "MakeCage", "", WOOD),
    ]),
    *_orders("Food & drink", [
        ("o_meal", "Prepare meals", "PrepareMeal", "", ()),
        ("o_brew", "Brew drink (plants)", "CustomReaction", "BREW_DRINK_FROM_PLANT", ()),
        ("o_brew_fruit", "Brew drink (fruit)", "CustomReaction", "BREW_DRINK_FROM_PLANT_GROWTH", ()),
        ("o_mill", "Mill plants", "MillPlants", "", ()),
        ("o_process", "Process plants", "ProcessPlants", "", ()),
    ]),
    *_orders("Industry", [
        ("o_blocks", "Stone blocks", "ConstructBlocks", "", STONE),
        ("o_mechanisms", "Mechanisms", "ConstructMechanisms", "", STONE),
        ("o_crafts", "Stone crafts", "MakeCrafts", "", STONE),
        ("o_charcoal", "Charcoal", "MakeCharcoal", "", ()),
        ("o_ash", "Ash", "MakeAsh", "", ()),
        ("o_lye", "Lye", "MakeLye", "", ()),
        ("o_pearlash", "Pearlash", "CustomReaction", "MAKE_PEARLASH", ()),
        ("o_soap", "Soap (tallow)", "CustomReaction", "MAKE_SOAP_FROM_TALLOW", ()),
        ("o_cloth", "Weave plant cloth", "WeaveCloth", "", ("plant",)),
    ]),
    *_orders("Medical & gear", [
        ("o_splint", "Splints", "ConstructSplint", "", WOOD),
        ("o_crutch", "Crutches", "ConstructCrutch", "", WOOD),
        ("o_backpack", "Backpacks", "MakeBackpack", "", ("leather",)),
        ("o_quiver", "Quivers", "MakeQuiver", "", ("leather",)),
    ]),
]

ORDERS_BY_ID: dict[str, Order] = {o.id: o for o in ORDERS}
ORDER_GROUPS = list(dict.fromkeys(o.group for o in ORDERS))
_ORDER_LABELS = {(o.job, o.reaction): o.label for o in ORDERS}


def humanize(name: str) -> str:
    """"PrayOrMeditate" -> "Pray or meditate"."""
    words, word = [], ""
    for ch in name:
        if ch.isupper() and word:
            words.append(word)
            word = ""
        word += ch
    words.append(word)
    return " ".join([words[0]] + [w.lower() for w in words[1:]])


def order_label(job: str, reaction: str) -> str:
    """A readable name for any manager order, ours or not."""
    if (job, reaction) in _ORDER_LABELS:
        return _ORDER_LABELS[(job, reaction)]
    if reaction:
        return reaction.replace("_", " ").capitalize()
    return humanize(job)


def _words(token: str) -> str:
    return token.replace("_", " ").lower()


def condition_text(cond: dict, labels: dict) -> str:
    """One condition of a manager order, as coop.lua reports it, in words.
    `labels` maps order ids to names, for "after that order" conditions."""
    if "after" in cond:
        other = labels.get(cond["after"], f"order {cond['after']}")
        return f"after {other} is {_words(cond.get('when', '')) or 'done'}"
    compare = _COMPARE_WORDS.get(cond.get("cmp", ""), "")
    what = [_words(flag) for flag in cond.get("flags") or []]
    for key in ("mat", "product", "class"):
        if cond.get(key):
            what.append(cond[key] if key == "mat" else _words(cond[key].removesuffix("_MAT")))
    what.append(cond.get("sub") or _words(cond.get("item") or "") or "items")
    return " ".join(str(p) for p in (compare, cond.get("val", ""), *what) if p != "")


# dfhack.units.getStressCategory: 0 is the most stressed, 6 the least.
STRESS_LABELS = ["Miserable", "Very unhappy", "Unhappy", "Fine", "Content", "Happy", "Ecstatic"]

GROUPS = list(dict.fromkeys(item.group for item in ITEMS))

# Game calendar, for the status bar.
MONTHS = ["Granite", "Slate", "Felsite", "Hematite", "Malachite", "Galena",
          "Limestone", "Sandstone", "Timber", "Moonstone", "Opal", "Obsidian"]
TICKS_PER_DAY = 1200
TICKS_PER_MONTH = 28 * TICKS_PER_DAY


def game_date(year: int, tick: int) -> str:
    month = min(tick // TICKS_PER_MONTH, 11)
    day = (tick % TICKS_PER_MONTH) // TICKS_PER_DAY + 1
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix} {MONTHS[month]}, year {year}"


def summarize(mode: str, stats: dict) -> tuple[int, list[str]]:
    """(things placed, human-readable problems) from quickfort's stats."""
    placed = int(stats.get(f"{mode}_designated", {}).get("value", 0))
    problems = [f"{s['label']}: {s['value']}" for sid, s in sorted(stats.items())
                if s.get("value") and sid != f"{mode}_designated" and not sid.endswith("_tiles")]
    return placed, problems
