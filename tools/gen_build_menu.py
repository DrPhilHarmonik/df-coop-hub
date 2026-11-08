"""Generate buildmenu.py: DF's real build menu, mapped onto quickfort keys.

Inputs, all in tools/:
  build_menu.json       DF's build-menu pages (watch_build_menu.py, captured
                        from DF 53.16 on 2026-11-07)
  quickfort_build.tsv   quickfort's building table (--refresh-quickfort
                        reloads it from a running DFHack)
and DF's data/init/interface.txt for the default key of each hotkey.

Each DF button is matched to quickfort by building type and subtype (and
custom-building id), not by label. Buildings quickfort can place in several
orientations get its default one. A button quickfort can't place at all is
kept, marked unavailable, so the menu still looks like DF's.

    python3 tools/gen_build_menu.py [--refresh-quickfort]
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DF_DIR = Path(os.environ.get("DF_DIR", Path.home() / ".local/share/Steam/steamapps/common/Dwarf Fortress"))
PAGE_ORDER = ["MAIN", "WORKSHOPS", "WORKSHOPS_FURNACES", "WORKSHOPS_CLOTHING", "WORKSHOPS_FARMING", "FURNITURE",
              "DOORS_HATCHES", "WALLS_FLOORS", "MACHINES_FLUIDS", "CAGES_RESTRAINTS", "TRAPS", "MILITARY"]

# Where several quickfort keys place the same building in different
# orientations, the one quickfort itself treats as the default.
DEFAULT_ORIENTATION = {"Bridge": "gs", "Track stop": "CS", "Screw pump": "Ms", "Water wheel": "Mw",
                       "Horizontal axle": "Mh", "Rollers": "Mr"}

# Ids the catalog used before the menu was captured, kept so logs and links
# stay stable. quickfort key -> id.
OLD_IDS = {
    "b": "bed", "t": "table", "c": "chair", "d": "door", "f": "cabinet", "h": "container", "n": "coffin",
    "s": "statue", "r": "weapon_rack", "a": "armor_stand", "l": "well", "H": "hatch", "Tl": "lever",
    "Tc": "cage_trap", "A": "archery_target", "F": "display", "N": "nest_box", "R": "traction_bench",
    "I": "instrument", "y": "window", "wc": "carpenter", "wm": "mason", "wr": "craftsdwarf", "wt": "mechanic",
    "wl": "still", "wz": "kitchen", "ww": "farmer", "wu": "butcher", "wn": "tanner", "we": "leather",
    "wo": "loom", "wk": "clothier", "wj": "jeweler", "wf": "forge", "wb": "bowyer", "wy": "ashery",
    "wd": "dyer", "wS": "soap", "ws": "siege", "wh": "fishery", "k": "kennels", "es": "smelter",
    "ew": "wood_furnace", "ek": "kiln", "eg": "glass_furnace", "wq": "quern", "wM": "millstone",
    "wp": "screw_press", "D": "depot", "Cf": "floor", "Cw": "wall", "Cr": "ramp_c", "Cx": "stair_updown_c",
    "CF": "fortification", "p": "farm", "o": "road",
}


def refresh_quickfort() -> None:
    sys.path.insert(0, str(HERE.parent))
    from dfhack_rpc import DFHackRPC
    lua = r"""
local f = io.open('hack/scripts/internal/quickfort/build.lua') local src = f:read('*a') f:close()
local env = setmetatable({dfhack_flags={module=true}}, {__index=_G})
local db = assert(load(src .. '\nreturn building_db_raw', '=quickfort-build', 't', env))()
for key, e in pairs(db) do
  print(('QF\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s'):format(key, e.label or '', tostring(e.type), tostring(e.subtype),
    tostring(e.custom), tostring(e.min_width), tostring(e.max_width), tostring(e.min_height), tostring(e.max_height)))
end"""
    with DFHackRPC() as df:
        text = df.run_command("lua", lua)
    lines = [ln for ln in text.splitlines() if ln.startswith("QF\t")]
    (HERE / "quickfort_build.tsv").write_text("\n".join(lines) + "\n")


def df_keys() -> dict[str, str]:
    text = (DF_DIR / "data/init/interface.txt").read_text(encoding="cp437")
    out = {}
    for m in re.finditer(r"\[BIND:([A-Z0-9_]+):[^\]]*\]((?:\s*\[(?:KEY|SYM|BUTTON):[^\n]*\])*)", text):
        ks = re.findall(r"\[KEY:(.)\]", m.group(2))
        if ks:
            out[m.group(1)] = ks[0]
    return out


def quickfort() -> list[dict]:
    num = lambda v: None if v == "nil" else int(v)
    rows = []
    for line in (HERE / "quickfort_build.tsv").read_text().splitlines():
        _, key, label, t, sub, custom, minw, maxw, minh, maxh = line.split("\t")
        rows.append(dict(key=key, label=label, type=num(t), subtype=num(sub), custom=num(custom),
                         minw=num(minw), maxw=num(maxw), minh=num(minh), maxh=num(maxh)))
    return rows


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def main() -> None:
    if "--refresh-quickfort" in sys.argv:
        refresh_quickfort()
    menu = json.loads((HERE / "build_menu.json").read_text())
    keys, qf = df_keys(), quickfort()
    pages, items, used_ids = {}, [], set()
    for page in PAGE_ORDER:
        entries = []
        for b in menu[page]:
            key = keys.get(b["hotkey"], "")
            if key == "":
                m = re.fullmatch(r"CUSTOM_SHIFT_([A-Z])", b["hotkey"])
                key = m.group(1) if m else ""
            if b["category"] >= 0:
                entries.append(("page", PAGE_ORDER[b["category"]], b["label"], key))
                continue
            cand = [q for q in qf if q["type"] == b["type"]
                    and ((b["subtype"] == -1 and q["subtype"] in (None, -1)) or q["subtype"] == b["subtype"])
                    and (b["custom"] < 0 or q["custom"] == b["custom"])]
            if len(cand) > 1:
                cand = [q for q in cand if q["key"] == DEFAULT_ORIENTATION.get(b["label"])]
            q = cand[0] if len(cand) == 1 else None
            base = OLD_IDS.get(q["key"]) if q else None
            item_id = base or slug(b["label"])
            if item_id in used_ids:
                item_id = f"{item_id}_{slug(page)}"
            used_ids.add(item_id)
            if q is None:
                items.append(dict(id=item_id, label=b["label"], qf="", w=1, h=1, area=False, page=page))
            else:
                area = q["type"] == 34 or q["minw"] != q["maxw"] or q["minh"] != q["maxh"]
                items.append(dict(id=item_id, label=b["label"], qf=q["key"], w=q["minw"] or 1, h=q["minh"] or 1,
                                  area=area, page=page))
            entries.append(("item", item_id, b["label"], key))
        pages[page] = entries

    out = ['"""DF\'s build menu, mapped onto quickfort keys. Generated by',
           'tools/gen_build_menu.py from DF 53.16; do not edit by hand."""', "",
           "# page id -> [(\"page\", page id, label, DF key) | (\"item\", item id, label, DF key)]",
           "PAGES = {"]
    for page in PAGE_ORDER:
        out.append(f"    {page!r}: [")
        out += [f"        {e!r}," for e in pages[page]]
        out.append("    ],")
    out += ["}", "", "# item id, label, quickfort key ('' = quickfort can't place it), w, h, area, page",
            "BUILD_ITEMS = ["]
    out += [f"    ({i['id']!r}, {i['label']!r}, {i['qf']!r}, {i['w']}, {i['h']}, {i['area']}, {i['page']!r}),"
            for i in items]
    out += ["]", ""]
    (HERE.parent / "buildmenu.py").write_text("\n".join(out))
    print(f"wrote buildmenu.py: {len(pages)} pages, {len(items)} buildings, "
          f"{sum(1 for i in items if not i['qf'])} quickfort can't place")


if __name__ == "__main__":
    main()
