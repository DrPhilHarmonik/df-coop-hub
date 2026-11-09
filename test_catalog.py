"""The build catalog: the whitelist between players and quickfort."""

from __future__ import annotations

import re

from catalog import BY_ID, ITEMS, MAX_AREA_SIDE, STOCK_GROUPS, game_date, stock_rows, summarize

KEY = re.compile(r"^~?[A-Za-z]{1,10}$")   # what lua/rpc/coop.lua accepts, minus the extent


def test_ids_are_unique_and_keys_are_what_the_lua_side_accepts():
    assert len(BY_ID) == len(ITEMS)
    for item in ITEMS:
        assert item.key == "" or KEY.match(item.key), item
        assert item.mode in {"build", "place", "zone"}


def test_area_items_carry_their_extent_and_fixed_ones_never_do():
    assert BY_ID["sp_food"].text(5, 3) == "f(5x3)"
    assert BY_ID["z_meeting"].text(MAX_AREA_SIDE, 1) == "m(31x1)"
    assert BY_ID["carpenter"].text(5, 3) == "wc"


def test_footprints_match_quickfort():
    # From quickfort's building table (tools/quickfort_build.tsv).
    assert {i.id for i in ITEMS if (i.w, i.h) == (5, 5)} == {"siege", "kennels", "depot"}
    for one in ("quern", "millstone", "screw_press", "bed", "door"):
        assert (BY_ID[one].w, BY_ID[one].h) == (1, 1)
    assert (BY_ID["carpenter"].w, BY_ID["carpenter"].h) == (3, 3)
    assert (BY_ID["water_wheel"].w, BY_ID["water_wheel"].h) == (1, 3)   # not every building is square
    assert BY_ID["farm"].area and BY_ID["wall"].area and not BY_ID["well"].area


def test_build_menu_is_dfs():
    """The paths DF players type: b o f p is a farm plot, b p r a door."""
    from catalog import PAGES

    def follow(*keys):
        page = "MAIN"
        for k in keys:
            entry = next(e for e in PAGES[page] if e["key"] == k)
            if "page" in entry:
                page = entry["page"]
            else:
                return entry["item"]
        return page

    assert follow("o", "f", "p") == "farm"
    assert follow("p", "r") == "door"
    assert follow("o", "u", "l") == "smelter"
    assert follow("o", "p") == "carpenter"
    assert follow("o", "t") == "mason"           # DF calls it Stoneworker
    assert follow("f", "b") == "bed"
    assert follow("T") == "depot"
    for page, entries in PAGES.items():            # DF never repeats a key on a page
        keys = [e["key"] for e in entries if e["key"]]
        assert len(keys) == len(set(keys)), page
        for e in entries:
            assert "page" in e and e["page"] in PAGES or e.get("item") in BY_ID, (page, e)


def test_only_what_quickfort_cannot_place_is_unavailable():
    assert [i.id for i in ITEMS if not i.available] == ["windmill"]


def test_game_date():
    assert game_date(23, 0) == "1st Granite, year 23"
    assert game_date(23, 147722) == "12th Malachite, year 23"
    assert game_date(5, 11 * 33600 + 27 * 1200) == "28th Obsidian, year 5"
    assert game_date(5, 3 * 1200 + 33600) == "4th Slate, year 5"
    assert game_date(5, 20 * 1200) == "21st Granite, year 5"


def test_summarize_reports_placed_and_problems_but_not_tile_counts():
    stats = {"build_designated": {"label": "Buildings designated", "value": 1},
             "build_tiles": {"label": "Tiles", "value": 9},
             "build_unsuitable": {"label": "Unsuitable tiles", "value": 3},
             "out_of_bounds": {"label": "Out of bounds", "value": 0}}
    assert summarize("build", stats) == (1, ["Unsuitable tiles: 3"])


# -- work orders ---------------------------------------------------------------

from pathlib import Path

from catalog import ORDER_FREQUENCIES, ORDERS, ORDERS_BY_ID, condition_text, order_label

LUA = (Path(__file__).parent / "lua" / "rpc" / "coop.lua").read_text()


def test_order_ids_unique_and_names_safe():
    assert len(ORDERS_BY_ID) == len(ORDERS)
    for o in ORDERS:
        assert re.fullmatch(r"[A-Za-z]+", o.job), o
        assert re.fullmatch(r"[A-Z0-9_]*", o.reaction), o
        assert (o.job == "CustomReaction") == bool(o.reaction), o


def test_order_materials_are_ones_the_lua_side_knows():
    block = LUA[LUA.index("local ORDER_MATERIALS"):LUA.index("}", LUA.index("local ORDER_MATERIALS"))]
    lua_materials = set(re.findall(r"(\w+)=", block))
    for o in ORDERS:
        assert set(o.materials) <= lua_materials, o


def test_order_labels_cover_ours_and_df_native_orders():
    assert order_label("ConstructBed", "") == "Beds"
    assert order_label("CustomReaction", "BREW_DRINK_FROM_PLANT") == "Brew drink (plants)"
    assert order_label("CollectSand", "") == "Collect sand"
    assert order_label("CustomReaction", "MAKE_WAX_CRAFTS") == "Make wax crafts"


def test_conditions_read_as_words():
    cond = {"cmp": "GreaterThan", "val": 10, "item": "PLANT", "product": "DRINK_MAT", "flags": ["unrotten"]}
    assert condition_text(cond, {}) == "more than 10 unrotten drink plant"
    assert condition_text({"cmp": "LessThan", "val": 5, "item": "WEAPON", "sub": "short sword"}, {}) == "fewer than 5 short sword"
    assert condition_text({"cmp": "AtLeast", "val": 0, "flags": ["non_absorbent"]}, {}) == "at least 0 non absorbent items"
    assert condition_text({"after": 7, "when": "Completed"}, {7: "Beds"}) == "after Beds is completed"
    assert condition_text({"after": 8, "when": "Activated"}, {}) == "after order 8 is activated"


def test_frequencies_are_df_names_without_none():
    assert "NONE" not in ORDER_FREQUENCIES
    assert all(re.fullmatch(r"[A-Za-z]+", name) for name in ORDER_FREQUENCIES)


def test_humanize_and_stress_labels():
    from catalog import STRESS_LABELS, humanize
    assert humanize("PrayOrMeditate") == "Pray or meditate"
    assert humanize("Wander") == "Wander"
    assert STRESS_LABELS[0] == "Miserable" and STRESS_LABELS[6] == "Ecstatic"


def test_stock_rows_are_named_grouped_and_biggest_first():
    rows = stock_rows([
        {"type": "BAR", "what": "", "material": "silver", "count": 3},
        {"type": "BAR", "what": "", "material": "coal", "count": 9},
        {"type": "BAR", "what": "", "material": "silver", "count": 2},      # same name: added up
        {"type": "PLANT", "what": "", "material": "quarry bush plant", "count": 4},
        {"type": "SEEDS", "what": "", "material": "", "count": 1},         # no material: the label
        {"type": "TOOL", "what": "jugs", "material": "granite", "count": 7},
        {"type": "EGG", "what": "", "material": "hen", "count": 5},
        {"type": "NEW_THING", "what": "", "material": "iron", "count": 1},
    ])
    assert [g["name"] for g in rows] == ["Raw materials", "Food and drink", "Tools and containers", "Other"]
    assert rows[0] == {"name": "Raw materials", "total": 14,
                       "rows": [{"name": "coal bars", "count": 9}, {"name": "silver bars", "count": 5}]}
    assert rows[1]["rows"] == [{"name": "hen eggs", "count": 5}, {"name": "quarry bush plant", "count": 4},
                               {"name": "seeds", "count": 1}]
    assert rows[2]["rows"] == [{"name": "granite jugs", "count": 7}]
    assert rows[3]["rows"] == [{"name": "iron new thing", "count": 1}]
    assert STOCK_GROUPS[-1] == "Other"
