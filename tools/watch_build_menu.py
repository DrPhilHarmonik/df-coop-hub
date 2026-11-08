"""Capture DF's build menu, read-only, while a player browses it.

DF only builds a build-menu page when someone opens it, so this polls DFHack
a few times a second and records every page it sees (labels, hotkeys,
building types) into tools/build_menu.json. Run it, open each submenu once
in DF (b; o and its u/l/f; f; p; n; m; r; t; y), then run gen_build_menu.py.

    python3 tools/watch_build_menu.py [seconds]
"""
import json, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dfhack_rpc import DFHackRPC

OUT = Path(__file__).with_name("build_menu.json")
LUA = r"""
local c = df.global.game.main_interface.construction
local out = {}
for _, p in ipairs(c.page) do
  local btns = {}
  for _, b in ipairs(p.bb_button) do
    table.insert(btns, string.format('%d|%d|%d|%d|%s|%s', b.category, b.type, b.subtype, b.custom_building_id,
      df.interface_key[b.hotkey] or tostring(b.hotkey), dfhack.df2utf(b.str)))
  end
  table.insert(out, 'PAGE ' .. (df.construction_category_type[p.category] or p.category) .. '\t' .. table.concat(btns, '\t'))
end
print(table.concat(out, '\n'))
"""
seen = json.loads(OUT.read_text()) if OUT.exists() else {}
deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 900)
with DFHackRPC() as df:
    while time.time() < deadline:
        text = df.run_command("lua", LUA)
        for line in text.splitlines():
            if not line.startswith("PAGE "):
                continue
            name, *btns = line[5:].split("\t")
            entries = []
            for b in btns:
                cat, typ, sub, custom, key, label = b.split("|", 5)
                entries.append({"category": int(cat), "type": int(typ), "subtype": int(sub),
                                "custom": int(custom), "hotkey": key, "label": label})
            if seen.get(name) != entries:
                seen[name] = entries
                OUT.write_text(json.dumps(seen, indent=1))
                print(f"captured {name}: " + ", ".join(e["label"] for e in entries), flush=True)
        time.sleep(0.4)
