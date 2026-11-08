"""The tile table and the hub's map cache. The cache is where hidden tiles
are cut out, so most of these are about what a player must not see."""

from __future__ import annotations

import pytest

pytest.importorskip("google.protobuf", reason="df-coop needs the coop extra")

from dfhack_rpc import rfr
from mapstate import FLAGS1_HIDDEN_IN_AMBUSH, FLAGS1_INACTIVE, FLAGS2_KILLED, HIDDEN, MapState
from tiles import tile_style, tiletype_table

N = 256


def make_block(bx, by, z, *, tiles=None, hidden=None, water=None, dig=None, buildings=()):
    b = rfr.MapBlock(map_x=bx * 16, map_y=by * 16, map_z=z)
    if tiles is not None:
        b.tiles.extend(tiles)
    if hidden is not None:
        b.hidden.extend(hidden)
        b.water.extend(water or [0] * N)
        b.magma.extend([0] * N)
        b.tile_dig_designation.extend(dig or [0] * N)
    for bld in buildings:
        b.buildings.append(bld)
    return b


def block_list(*blocks):
    bl = rfr.BlockList()
    for b in blocks:
        bl.map_blocks.append(b)
    return bl


def state():
    return MapState(width=32, height=32, depth=10)


# -- tiles --------------------------------------------------------------------

def test_tile_styles_for_common_shapes():
    assert tile_style("WALL", "STONE", "NORMAL")[0] == "▒"
    assert tile_style("WALL", "STONE", "SMOOTH")[0] == "█"
    assert tile_style("WALL", "MINERAL", "NORMAL")[0] == "£"
    assert tile_style("WALL", "MINERAL", "SMOOTH")[0] == "\u2588"  # smoothed vein is a smooth wall
    assert tile_style("FLOOR", "GRASS_LIGHT", "NORMAL")[0] == '"'
    assert tile_style("STAIR_UPDOWN", "STONE", "NORMAL")[0] == "X"
    assert tile_style("EMPTY", "AIR", "NORMAL")[2] is True
    assert tile_style("FLOOR", "STONE", "NORMAL")[2] is False


def test_tiletype_table_is_indexed_by_id_with_gaps_filled():
    tts = [rfr.Tiletype(id=0, name="Void", caption="void", shape=rfr.NO_SHAPE, material=rfr.NO_MATERIAL),
           rfr.Tiletype(id=3, name="StoneWall", caption="stone wall", shape=rfr.WALL, material=rfr.STONE)]
    table = tiletype_table(tts)
    assert len(table) == 4
    assert table[3][2] == "stone wall"
    assert table[1] == [" ", "#000000", "", True]


# -- blocks -------------------------------------------------------------------

def test_hidden_tiles_and_their_liquids_are_masked_but_designations_show():
    s = state()
    hidden = [False] * N
    hidden[5] = True
    water = [0] * N
    water[5] = 7
    water[6] = 3
    dig = [0] * N
    dig[5] = 1
    s.apply_block_list(block_list(make_block(0, 0, 2, tiles=[42] * N, hidden=hidden, water=water, dig=dig)))
    p = s.block_payload((0, 0, 2))
    assert p["t"][5] == HIDDEN and p["t"][4] == 42
    assert p["w"][5] == 0 and p["w"][6] == 3
    assert p["d"][5] == 1  # digging into unexplored rock is normal play


def test_liquid_only_on_hidden_tiles_is_omitted():
    s = state()
    hidden = [True] * N
    water = [7] * N
    s.apply_block_list(block_list(make_block(0, 0, 2, tiles=[42] * N, hidden=hidden, water=water)))
    assert "w" not in s.block_payload((0, 0, 2))


def test_partial_updates_merge_per_field():
    s = state()
    s.apply_block_list(block_list(make_block(1, 0, 3, tiles=[7] * N, hidden=[False] * N)))
    dig = [0] * N
    dig[0] = 3
    changed, _ = s.apply_block_list(block_list(make_block(1, 0, 3, hidden=[False] * N, dig=dig)))
    assert changed == {(1, 0, 3)}
    p = s.block_payload((1, 0, 3))
    assert p["t"][0] == 7 and p["d"][0] == 3


def test_block_with_only_designations_is_not_sent_until_tiles_arrive():
    s = state()
    s.apply_block_list(block_list(make_block(0, 1, 0, hidden=[False] * N)))
    assert s.block_payload((0, 1, 0)) is None


def test_blocks_never_sent_count_as_open_air():
    assert state().is_hidden(3, 3, 3) is False


def test_blocks_on_returns_only_that_level():
    s = state()
    s.apply_block_list(block_list(make_block(0, 0, 1, tiles=[1] * N, hidden=[False] * N),
                                  make_block(0, 0, 2, tiles=[2] * N, hidden=[False] * N)))
    assert [b["z"] for b in s.blocks_on(2)] == [2]


def test_items_and_flows_are_masked_and_skip_contained_or_removed_items():
    cache = state()
    cache.item_defs[(4, -1)] = "BOULDER"
    block = make_block(0, 0, 5, tiles=[1] * N, hidden=[True] + [False] * (N - 1))
    for index, x, flags in ((1, 1, 1), (2, 0, 1), (3, 2, 1 | (1 << 3)), (4, 3, 1 | (1 << 4)),
                            (5, 4, 1 | (1 << 5)), (6, 5, 1 | (1 << 10)), (7, 6, 1 | (1 << 11))):
        item = block.items.add(id=index, flags1=flags)
        item.pos.x, item.pos.y, item.pos.z = x, 0, 5
        item.type.mat_type, item.type.mat_index = 4, -1
    for x, dead in ((0, False), (1, False), (2, True)):
        flow = block.flows.add(type=rfr.Miasma, density=50, dead=dead)
        flow.pos.x, flow.pos.y, flow.pos.z = x, 0, 5
    cache.apply_block_list(block_list(block), visual_levels=[5])
    payload = cache.block_payload((0, 0, 5))
    assert payload["o"] == [[1, 0, "ITEM_BOULDER"]]
    assert payload["f"] == [[1, 0, "FLOW_MIASMA:1", 0.5]]


def test_complete_visual_slice_clears_last_item_without_disturbing_other_levels():
    cache = state()
    cache.item_defs[(4, -1)] = "BOULDER"
    blocks = []
    for level in (4, 5):
        block = make_block(0, 0, level, tiles=[1] * N, hidden=[False] * N)
        item = block.items.add(id=level, flags1=1)
        item.pos.x, item.pos.y, item.pos.z = 1, 1, level
        item.type.mat_type, item.type.mat_index = 4, -1
        flow = block.flows.add(type=rfr.Smoke, density=80)
        flow.pos.x, flow.pos.y, flow.pos.z = 1, 1, level
        blocks.append(block)
    cache.apply_block_list(block_list(*blocks), visual_levels=[4, 5])
    changed, _ = cache.apply_block_list(block_list(), visual_levels=[5])
    assert changed == {(0, 0, 5)}
    assert "o" not in cache.block_payload((0, 0, 5))
    assert "f" not in cache.block_payload((0, 0, 5))
    assert cache.block_payload((0, 0, 4))["o"]
    assert cache.block_payload((0, 0, 4))["f"]
    assert cache.apply_block_list(block_list(), visual_levels=[5])[0] == set()


def test_unchanged_visuals_do_not_resend_block_and_visual_only_change_preserves_terrain():
    cache = state()
    block = make_block(0, 0, 5, tiles=[7] * N, hidden=[False] * N)
    flow = block.flows.add(type=rfr.Fire, density=80)
    flow.pos.x, flow.pos.y, flow.pos.z = 1, 1, 5
    cache.apply_block_list(block_list(block), visual_levels=[5])
    delta = make_block(0, 0, 5)
    delta.flows.append(flow)
    assert cache.apply_block_list(block_list(delta), visual_levels=[5])[0] == set()
    delta.flows[0].density = 25
    assert cache.apply_block_list(block_list(delta), visual_levels=[5])[0] == {(0, 0, 5)}
    assert cache.block_payload((0, 0, 5))["t"] == [7] * N


# -- buildings ----------------------------------------------------------------

def bld(index, x, y, z, btype=1, full=True):
    b = rfr.BuildingInstance(index=index)
    if full:
        b.pos_x_min, b.pos_y_min, b.pos_z_min = x, y, z
        b.pos_x_max, b.pos_y_max, b.pos_z_max = x, y, z
        b.building_type.building_type = btype
        b.building_type.building_subtype = -1
        b.building_type.building_custom = -1
    return b


def test_buildings_keep_out_of_range_entries_and_drop_removed_ones():
    s = state()
    s.add_building_def(1, -1, -1, "Bed", "Bed")
    first = make_block(0, 0, 2, tiles=[1] * N, hidden=[False] * N,
                       buildings=[bld(10, 1, 1, 2), bld(11, 2, 2, 5)])
    s.apply_block_list(block_list(first))
    assert s.buildings[10]["name"] == "Bed"
    assert s.buildings[10]["kind"] == "Bed"
    # A reply for level 5 sends 10 as a bare index: keep it.
    _, changed = s.apply_block_list(block_list(
        make_block(0, 0, 5, tiles=[1] * N, hidden=[False] * N, buildings=[bld(10, 0, 0, 0, full=False), bld(11, 2, 2, 5)])))
    assert not changed and 10 in s.buildings
    # 11 is gone from the next reply entirely: drop it.
    _, changed = s.apply_block_list(block_list(
        make_block(0, 0, 2, tiles=[1] * N, hidden=[False] * N, buildings=[bld(10, 1, 1, 2)])))
    assert changed and 11 not in s.buildings


def test_repeated_building_defs_keep_the_first_name_and_kind_comes_from_the_id():
    s = state()
    s.add_building_def(30, -1, -1, "Civzone", "Zone")
    s.add_building_def(30, -1, -1, "Civzone/NONE", "NONE")
    s.add_building_def(30, 5, -1, "Civzone/MeetingHall", "MeetingHall")
    s.apply_block_list(block_list(make_block(0, 0, 2, tiles=[1] * N, hidden=[False] * N,
                                             buildings=[bld(10, 1, 1, 2, btype=30)])))
    assert s.building_name(30, -1, -1) == "Zone"
    assert s.buildings[10]["kind"] == "Civzone"


def test_buildings_on_hidden_tiles_are_not_shown():
    s = state()
    hidden = [False] * N
    hidden[1 * 16 + 1] = True
    s.apply_block_list(block_list(make_block(0, 0, 2, tiles=[1] * N, hidden=hidden,
                                             buildings=[bld(10, 1, 1, 2), bld(12, 3, 3, 2)])))
    assert [b["i"] for b in s.buildings_on({2})] == [12]


# -- units --------------------------------------------------------------------

def unit(uid, x, y, z, flags1=0, flags2=0, name=""):
    return rfr.UnitDefinition(id=uid, pos_x=x, pos_y=y, pos_z=z, flags1=flags1, flags2=flags2, name=name)


def test_units_dead_gone_ambushing_offmap_or_on_hidden_tiles_are_filtered():
    s = state()
    hidden = [False] * N
    hidden[0] = True
    s.apply_block_list(block_list(make_block(0, 0, 2, tiles=[1] * N, hidden=hidden)))
    units = [
        unit(1, 5, 5, 2, name="Urist"),
        unit(2, 5, 5, 2, flags1=FLAGS1_INACTIVE),
        unit(3, 5, 5, 2, flags2=FLAGS2_KILLED),
        unit(4, 5, 5, 2, flags1=FLAGS1_HIDDEN_IN_AMBUSH),
        unit(5, 0, 0, 2),                 # standing on a hidden tile
        unit(6, -30000, -30000, -30000),  # off the map
    ]
    assert [u["i"] for u in s.visible_units(units)] == [1]


def test_unit_colour_comes_from_profession_color():
    s = state()
    u = unit(1, 1, 1, 1)
    u.profession_color.red, u.profession_color.green, u.profession_color.blue = 255, 128, 0
    assert s.visible_units([u])[0]["c"] == "#ff8000"
