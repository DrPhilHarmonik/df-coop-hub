"""How each DF tiletype is drawn: a glyph, a colour, and whether the level
below shows through it.

DFHack describes every tiletype by shape, material and special (smooth,
worn, ...). Each entry includes a classic glyph and an installed-game sprite
token. The table is built once per session and
sent to each player on connect; blocks then carry tiletype ids only.
"""

from __future__ import annotations

from dfhack_rpc import rfr
from game_tiles import terrain_sprite

SHAPE_GLYPH = {
    "NO_SHAPE": " ",
    "EMPTY": " ",
    "FLOOR": ".",
    "BOULDER": "•",       # •
    "PEBBLES": ",",
    "WALL": "▒",          # ▒ rough; smooth and built walls differ below
    "FORTIFICATION": "╬", # ╬
    "STAIR_UP": "<",
    "STAIR_DOWN": ">",
    "STAIR_UPDOWN": "X",
    "RAMP": "▲",          # ▲
    "RAMP_TOP": "▼",      # ▼
    "BROOK_BED": "≈",     # ≈
    "BROOK_TOP": "≈",
    "TREE_SHAPE": "O",
    "SAPLING": "τ",       # τ
    "SHRUB": "♣",         # ♣
    "ENDLESS_PIT": " ",
    "BRANCH": "─",        # ─
    "TRUNK_BRANCH": "┼",  # ┼
    "TWIG": ";",
}

MATERIAL_COLOR = {
    "NO_MATERIAL": "#000000",
    "AIR": "#303030",
    "SOIL": "#8b6a3e",
    "STONE": "#9a9a9a",
    "FEATURE": "#a070c0",
    "LAVA_STONE": "#6e6e6e",
    "MINERAL": "#d4af37",
    "FROZEN_LIQUID": "#a0e0ff",
    "CONSTRUCTION": "#d0d0d0",
    "GRASS_LIGHT": "#6cbf3a",
    "GRASS_DARK": "#3d8a2a",
    "GRASS_DRY": "#b5a642",
    "GRASS_DEAD": "#8a7a50",
    "PLANT": "#4caf50",
    "HFS": "#ff40ff",
    "CAMPFIRE": "#ff8020",
    "FIRE": "#ff6010",
    "ASHES": "#777777",
    "MAGMA": "#ff3300",
    "DRIFTWOOD": "#a07040",
    "POOL": "#3070ff",
    "BROOK": "#3070ff",
    "RIVER": "#3070ff",
    "ROOT": "#8b5a2b",
    "TREE_MATERIAL": "#a0522d",
    "MUSHROOM": "#b080b0",
    "UNDERWORLD_GATE": "#ff00ff",
}

GRASS = {"GRASS_LIGHT", "GRASS_DARK", "GRASS_DRY", "GRASS_DEAD"}
WATERY = {"POOL", "BROOK", "RIVER", "FROZEN_LIQUID"}

# Shapes with nothing solid at eye level: the client draws the level below,
# dimmed, the way DF itself does.
SEE_BELOW = {"NO_SHAPE", "EMPTY", "RAMP_TOP", "BROOK_TOP", "ENDLESS_PIT"}


def tile_style(shape: str, material: str, special: str) -> tuple[str, str, bool]:
    """(glyph, colour, see_below) for one tiletype, by enum names."""
    glyph = SHAPE_GLYPH.get(shape, "?")
    color = MATERIAL_COLOR.get(material, "#ff00ff")
    smooth = special in ("SMOOTH", "SMOOTH_DEAD")

    if shape == "WALL":
        if material == "CONSTRUCTION":
            glyph = "#"
        elif material == "MINERAL" and not smooth:
            glyph = "£"  # DF's glyph for raw vein; smoothed veins fall through
        elif material == "TREE_MATERIAL":
            glyph = "O"
        elif smooth:
            glyph = "█"  # █
    elif shape == "FLOOR":
        if material in GRASS:
            glyph = '"'
        elif material in WATERY:
            glyph = "≈"
        elif material == "CONSTRUCTION" or smooth:
            glyph = "+"
        elif special == "FURROWED":
            glyph = "≡"  # ≡ farm plot soil
        elif material == "MAGMA":
            glyph = "~"
    return glyph, color, shape in SEE_BELOW


def tiletype_table(tiletypes) -> list[list]:
    """Index-by-id table of [glyph, colour, caption, see_below, sprite, shape] for a
    GetTiletypeList reply. Ids DFHack skipped get an empty entry."""
    size = max((t.id for t in tiletypes), default=-1) + 1
    table: list[list] = [[" ", "#000000", "", True] for _ in range(size)]
    for t in tiletypes:
        glyph, color, below = tile_style(
            rfr.TiletypeShape.Name(t.shape),
            rfr.TiletypeMaterial.Name(t.material),
            rfr.TiletypeSpecial.Name(t.special),
        )
        sprite = terrain_sprite(rfr.TiletypeShape.Name(t.shape), rfr.TiletypeMaterial.Name(t.material),
                                rfr.TiletypeSpecial.Name(t.special))
        table[t.id] = [glyph, color, t.caption, below, sprite, rfr.TiletypeShape.Name(t.shape)]
    return table
