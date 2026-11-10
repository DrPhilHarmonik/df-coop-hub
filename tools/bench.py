#!/usr/bin/env python3
"""A throwaway Dwarf Fortress for trying things that need game time.

A second DF with its own copy of the install, its own save folder, its own DFHack
port and its own display, so it can't touch the real game or the real save:

    python3 tools/bench.py setup b4_coop    # copy DF and that save in
    python3 tools/bench.py start            # start it and load the save
    python3 tools/bench.py run station      # run scenarios; `list` names them
    python3 tools/bench.py stop

The save is loaded by writing a mouse click into DF's own memory from DFHack, so no
input reaches the rest of the desktop. (Tools that click through /dev/uinput and
find DF by process name aren't safe to use while someone is playing.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dfhack_rpc import DFHackRPC, DFHackRPCError  # noqa: E402

REAL_DF = Path(os.environ.get("DF_DIR", Path.home() / ".local/share/Steam/steamapps/common/Dwarf Fortress"))
REAL_USER = Path.home() / ".local/share/Bay 12 Games/Dwarf Fortress"
BENCH = Path(os.environ.get("DF_BENCH", Path.home() / ".local/share/df-coop/bench"))
INSTALL = BENCH / "install"
XDG = BENCH / "xdg"
USER = XDG / "Bay 12 Games/Dwarf Fortress"
PIDS = BENCH / "pids.json"
PORT = 5001
HUB_PORT = 8797
DISPLAY = ":98"
COPY_EXCLUDES = ("*legends*.xml", "export-cube", "gamelog.txt", "stderr.log", "*.db")
GUARD = "assert(dfhack.getDFPath():find('df-coop/bench', 1, true), 'not the bench')\n"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def log(text: str) -> None:
    print(f"[bench {time.strftime('%H:%M:%S')}] {text}", flush=True)


# -- talking to the bench ---------------------------------------------------------

def run_lua_text(code: str, timeout: float = 30.0) -> str:
    """Run Lua in the bench's DF. The guard stops it on any other DF."""
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(GUARD + code)
    try:
        result = subprocess.run([str(INSTALL / "dfhack-run"), "lua", "-f", f.name], cwd=INSTALL, timeout=timeout,
                                capture_output=True, text=True, errors="replace", env={**os.environ, "DFHACK_PORT": str(PORT)})
    finally:
        os.unlink(f.name)
    output = ANSI.sub("", result.stdout + result.stderr)
    if result.returncode != 0 or "stack traceback" in output:
        raise RuntimeError(output.strip()[-600:])
    return output


def lua_json(code: str, timeout: float = 30.0):
    """Run Lua that ends in `return <table>` and decode it."""
    wrapped = f"local json = require('json')\nlocal function body()\n{code}\nend\nprint('JSON:' .. json.encode(body()))\n"
    output = run_lua_text(wrapped, timeout)
    if "JSON:" not in output:
        raise RuntimeError("no result from Lua")
    # DFHack's encoder writes over several lines.
    return json.loads(output.split("JSON:", 1)[1])


def connect() -> DFHackRPC:
    return DFHackRPC("127.0.0.1", PORT).connect()


def coop(df: DFHackRPC, function: str, *args):
    return json.loads(df.run_lua("rpc.coop", function, *[str(a) for a in args])[0])


# -- setting up, starting, stopping -----------------------------------------------

def setup(save: str) -> None:
    if not (REAL_USER / "save" / save).is_dir():
        raise SystemExit(f"no save named {save!r} in {REAL_USER / 'save'}")
    if running():
        raise SystemExit("the bench is running; stop it first")
    INSTALL.mkdir(parents=True, exist_ok=True)
    excludes = [f"--exclude={e}" for e in COPY_EXCLUDES]
    log("copying DF")
    subprocess.run(["rsync", "-a", "--delete", *excludes, f"{REAL_DF}/", f"{INSTALL}/"], check=True)
    (INSTALL / "dfhack-config/remote-server.json").write_text(json.dumps({"allow_remote": False, "port": PORT}))
    shutil.copy(ROOT / "lua/rpc/coop.lua", INSTALL / "hack/lua/rpc/coop.lua")
    (USER / "save").mkdir(parents=True, exist_ok=True)
    for name in ("prefs", "data"):
        subprocess.run(["rsync", "-a", "--delete", f"{REAL_USER / name}/", f"{USER / name}/"], check=True)
    init = USER / "prefs/init.txt"
    text = re.sub(r"\[SOUND:YES\]", "[SOUND:NO]", init.read_text())
    init.write_text(re.sub(r"\[G_FPS_CAP:\d+\]", "[G_FPS_CAP:10]", text))
    log(f"copying save {save}")
    shutil.rmtree(USER / "save", ignore_errors=True)
    (USER / "save").mkdir()
    shutil.copytree(REAL_USER / "save" / save, USER / "save" / save)
    log(f"ready: {BENCH}")


def _is_ours(pid: int, what: str) -> bool:
    """A remembered pid may have been reused: only ever act on the one we started."""
    try:
        if what == "df":
            return Path(f"/proc/{pid}/cwd").resolve() == INSTALL.resolve()
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return cmdline[:2] == [b"Xvfb", DISPLAY.encode()]
    except OSError:
        return False


def _pids() -> dict:
    return json.loads(PIDS.read_text()) if PIDS.exists() else {}


def running() -> bool:
    return any(_is_ours(pid, what) for what, pid in _pids().items())


def _find_df_pid(launcher: int) -> int:
    """./dfhack is a shell script that execs or runs ./dwarfort in the same folder."""
    for _ in range(40):
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if (entry / "comm").read_text().strip() == "dwarfort" and _is_ours(int(entry.name), "df"):
                        return int(entry.name)
                except OSError:
                    pass
        time.sleep(0.5)
    return launcher


def launch() -> None:
    if running():
        raise SystemExit("the bench is already running")
    if not INSTALL.is_dir():
        raise SystemExit("no bench yet; run setup first")
    xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1200x800x24"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    env = {**os.environ, "XDG_DATA_HOME": str(XDG), "DISPLAY": DISPLAY, "SDL_VIDEODRIVER": "x11",
           "SDL_AUDIODRIVER": "dummy", "DFHACK_PORT": str(PORT)}
    env.pop("WAYLAND_DISPLAY", None)
    with open(BENCH / "run.log", "w") as out:
        # Low priority: whoever is playing the real game comes first.
        df = subprocess.Popen(["nice", "-n", os.environ.get("DF_BENCH_NICE", "15"), "./dfhack"], cwd=INSTALL, env=env, stdout=out, stderr=out,
                              stdin=subprocess.DEVNULL, start_new_session=True)
    PIDS.write_text(json.dumps({"xvfb": xvfb.pid, "df": _find_df_pid(df.pid)}))


def stop() -> None:
    pids = _pids()
    # DF first: it treats losing its display as a crash.
    for what, pid in sorted(pids.items()):
        if _is_ours(pid, what):
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not _is_ours(pid, what):
                    break
                time.sleep(0.5)
            if _is_ours(pid, what):
                os.kill(pid, signal.SIGKILL)
    PIDS.unlink(missing_ok=True)
    log("stopped")


def wait_for_dfhack(seconds: float = 120.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            run_lua_text("print('up')", timeout=5)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise SystemExit("DFHack never answered on the bench")


SCREEN = """
local gps = df.global.gps
local rows = {}
for y = 0, gps.dimy - 1 do
    local s = {}
    for x = 0, gps.dimx - 1 do
        local p = dfhack.screen.readTile(x, y)
        local c = p and p.ch or 32
        s[#s + 1] = (c > 32 and c < 127) and string.char(c) or ' '
    end
    rows[#rows + 1] = table.concat(s)
end
local scr = dfhack.gui.getCurViewscreen()
return {rows=rows, mode=df.viewscreen_titlest:is_instance(scr) and scr.mode or -1}
"""


def find_text(rows: list[str], text: str) -> tuple[int, int] | None:
    """Where `text` is on screen, as the tile in its middle."""
    for y, row in enumerate(rows):
        x = row.find(text)
        if x >= 0:
            return x + len(text) // 2, y
    return None


def click(x: int, y: int) -> None:
    """A left click, written where DF's screens read it. Nothing outside this DF sees it."""
    run_lua_text(f"""
local gps, en = df.global.gps, df.global.enabler
gps.mouse_x, gps.mouse_y = {int(x)}, {int(y)}
en.mouse_lbut, en.mouse_lbut_down = 1, 0
""")


def click_until_mode(text: str, want_mode: int, seconds: float = 20.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        screen = lua_json(SCREEN)
        if screen["mode"] == want_mode:
            return
        at = find_text(screen["rows"], text)
        if at:
            click(*at)
        time.sleep(1.5)
    raise SystemExit(f"never reached title mode {want_mode} clicking {text!r}")


def load_save(folder: str | None = None) -> None:
    folder = folder or newest_save()
    end = time.time() + 60
    while time.time() < end:
        try:
            if lua_json(SCREEN)["mode"] == 0 and find_text(lua_json(SCREEN)["rows"], "Continue active game"):
                break
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(1)
    click_until_mode("Continue active game", 2)
    click_until_mode("World:", 3)
    at = find_text(lua_json(SCREEN)["rows"], f"Folder: {folder}")
    if not at:
        raise SystemExit(f"save {folder!r} isn't on DF's list")
    click(*at)
    end = time.time() + 300
    while time.time() < end:
        if "true" in run_lua_text("print(dfhack.isMapLoaded())"):
            log(f"loaded {folder}")
            return
        time.sleep(2)
    raise SystemExit("the fort never finished loading")


def start(folder: str | None = None) -> None:
    shutil.copy(ROOT / "lua/rpc/coop.lua", INSTALL / "hack/lua/rpc/coop.lua")   # always the current module
    launch()
    wait_for_dfhack()
    load_save(folder)
    print(run_lua_text("""
print('path', dfhack.getDFPath())
print('save', df.global.world.cur_savegame.save_dir, 'citizens', #dfhack.units.getCitizens(true),
      'orders', #df.global.world.manager_orders.all, 'paused', df.global.pause_state)
"""))


def saves() -> dict[str, float]:
    """Saves on disk and when each last changed. `current` is DF's working folder, not a save."""
    return {p.name: max((f.stat().st_mtime for f in p.iterdir()), default=0.0)
            for p in (USER / "save").iterdir() if p.name != "current"}


def newest_save() -> str:
    found = saves()
    return max(found, key=found.__getitem__)


def save_now() -> float:
    """Save the way DF's autosave does, wait for it, and say how long DF was unreachable.
    DF only acts on the request while running, and answers nothing while it saves, so
    this goes through fresh dfhack-run calls that may each time out."""
    before = saves()
    run_lua_text("dfhack.run_command('quicksave')")
    end, started, longest, last = time.time() + 240, time.time(), 0.0, time.time()
    while time.time() < end:
        time.sleep(1)
        try:
            # True until the save ends; unpausing is what lets DF start it.
            state = run_lua_text("df.global.pause_state = false; print(df.global.plotinfo.main.autosave_request)", timeout=5)
        except (RuntimeError, subprocess.TimeoutExpired):
            longest = max(longest, time.time() - last)
            continue
        longest, last = max(longest, time.time() - last), time.time()
        if saves() != before and "false" in state:
            run_lua_text("df.global.pause_state = true")
            log(f"saved to {newest_save()} in {time.time() - started:.0f}s; DF silent for {longest:.0f}s at most")
            return longest
    raise RuntimeError("the save never finished")


def reload_fort() -> None:
    """Stop DF and load what's on disk: the only real test of what got saved."""
    stop()
    start(newest_save())


# -- scenarios: each returns (ok, what was actually seen) --------------------------

MEMBERS = """
local function members(squad)
    local out = {}
    for i, p in ipairs(squad.positions) do
        local fig = p.occupant ~= -1 and df.historical_figure.find(p.occupant)
        local u = fig and df.unit.find(fig.unit_id)
        if u then table.insert(out, {slot=i, id=u.id, name=dfhack.df2utf(dfhack.units.getReadableName(u)),
                                     x=u.pos.x, y=u.pos.y, z=u.pos.z}) end
    end
    return out
end
"""

PICK_SQUAD = MEMBERS + """
local best
for _, id in ipairs(df.global.plotinfo.main.fortress_entity.squads) do
    local s = df.squad.find(id)
    local m = s and members(s) or {}
    if #m > 0 and (not best or #m > #best.members) then best = {id=id, members=m} end
end
return best
"""

# A walkable, seen floor a few tiles from someone, in the same connected area, so
# walking there is possible.
PICK_TILE = """
local u = df.unit.find({uid})
local group = dfhack.maps.getWalkableGroup(u.pos)
for r = {near}, {far} do
    for dx = -r, r do
        for dy = -r, r do
            if math.max(math.abs(dx), math.abs(dy)) == r then
                local x, y, z = u.pos.x + dx, u.pos.y + dy, u.pos.z
                local tile = dfhack.maps.getTileType(x, y, z)
                local flags = dfhack.maps.getTileFlags(x, y, z)
                if tile and flags and not flags.hidden and df.tiletype_shape.attrs[df.tiletype.attrs[tile].shape].walkable
                        and dfhack.maps.getWalkableGroup(xyz2pos(x, y, z)) == group then
                    return {{x=x, y=y, z=z}}
                end
            end
        end
    end
end
return {{}}
"""


def distances(squad_id: int, x: int, y: int, z: int) -> list[dict]:
    return lua_json(MEMBERS + f"""
local out = {{}}
for _, m in ipairs(members(df.squad.find({squad_id}))) do
    local u = df.unit.find(m.id)
    table.insert(out, {{name=m.name, d=math.max(math.abs(m.x - {x}), math.abs(m.y - {y})) + math.abs(m.z - {z}) * 3,
                       job=u.job.current_job and dfhack.df2utf(dfhack.job.getName(u.job.current_job)) or ''}})
end
return out
""")


def closest(rows: list[dict]) -> int:
    return min(r["d"] for r in rows)


def game_tick() -> int:
    return lua_json("return {t=df.global.cur_year * 403200 + df.global.cur_year_tick}")["t"]


def run_game(df: DFHackRPC, seconds: float, done, every: float = 5.0) -> int:
    """Let game time pass until done() or time runs out, and say how many ticks did.
    DF pauses itself for some announcements, so keep unpausing."""
    start_tick, end = game_tick(), time.time() + seconds
    while time.time() < end:
        if df.paused():
            df.set_paused(False)
        time.sleep(every)
        if done():
            break
    df.set_paused(True)
    return game_tick() - start_tick


NEAR_STATION = 4
CONVERGED = 3


def near(rows: list[dict], tiles: int = NEAR_STATION) -> int:
    return sum(r["d"] <= tiles for r in rows)


def scenario_station(df: DFHackRPC) -> tuple[bool, str]:
    """Several squad members gather at a station given as the browser gives it; Cancel ends it."""
    squad = lua_json(PICK_SQUAD)
    if not squad:
        return False, "no squad with members on this fort"
    tile = lua_json(PICK_TILE.format(uid=squad["members"][0]["id"], near=20, far=45))
    if "x" not in tile:
        return False, "no reachable tile 20 to 45 away"
    x, y, z = tile["x"], tile["y"], tile["z"]
    before = distances(squad["id"], x, y, z)
    wanted = max(CONVERGED, near(before) + 2)
    log(f"squad {squad['id']} ({len(before)} members) to {x},{y},{z}; {near(before)} within {NEAR_STATION} tiles, need {wanted}")
    answer = coop(df, "squad_station", squad["id"], x, y, z)
    if not answer.get("ok"):
        return False, f"station refused: {answer}"
    seen = {"most": near(before), "rows": before}

    def arrived() -> bool:
        seen["rows"] = distances(squad["id"], x, y, z)
        seen["most"] = max(seen["most"], near(seen["rows"]))
        log(f"{near(seen['rows'])} within {NEAR_STATION} tiles; jobs {sorted({r['job'] for r in seen['rows']})[:4]}")
        return seen["most"] >= wanted

    ticks = run_game(df, 240, arrived)
    answer = coop(df, "squad_clear", squad["id"])
    left = lua_json(f"return {{n=#df.squad.find({squad['id']}).orders}}")["n"]
    at_cancel = near(distances(squad["id"], x, y, z))
    rows = {"now": []}

    def dispersed() -> bool:
        rows["now"] = distances(squad["id"], x, y, z)
        log(f"after Cancel: {near(rows['now'])} near; jobs {sorted({r['job'] for r in rows['now']})[:5]}")
        return near(rows["now"]) <= at_cancel // 2

    after = run_game(df, 200, dispersed, every=10)
    still = near(rows["now"])
    jobs = sorted({r["job"] for r in rows["now"]})[:5]
    ok = seen["most"] >= wanted and answer.get("ok") and left == 0
    return ok, (f"{ticks} ticks: within {NEAR_STATION} tiles went {near(before)} to {seen['most']} of {len(before)}; "
                f"after Cancel {left} orders left, and {at_cancel} near became {still} over {after} ticks; jobs {jobs}")


# modtools/create-unit fails on this DFHack, so the enemy is a real dangerous creature
# from elsewhere on the map, moved next to the squad. Only ever on the bench.
BRING_ENEMY = """
local best
for _, u in ipairs(df.global.world.units.active) do
    if dfhack.units.isDanger(u) and dfhack.units.isActive(u) and not dfhack.units.isDead(u)
            and not u.flags1.caged and not u.flags1.chained then best = u; break end
end
if not best then return {{error='no dangerous creature on this map'}} end
dfhack.units.teleport(best, xyz2pos({x}, {y}, {z}))
best.flags1.hidden_ambusher = false
return {{id=best.id, race=dfhack.df2utf(dfhack.units.getReadableName(best)), danger=dfhack.units.isDanger(best),
        hidden=dfhack.units.isHidden(best), x=best.pos.x, y=best.pos.y, z=best.pos.z}}
"""

UNIT_STATE = """
local u = df.unit.find({id})
if not u then return {{gone=true, dead=true}} end
local out = {{dead=dfhack.units.isDead(u), nearest=999, jobs={{}}, in_squad=false}}
local ids = {{}}
for _, m in ipairs(members(df.squad.find({squad}))) do
    ids[m.id] = true
    out.nearest = math.min(out.nearest, math.max(math.abs(m.x - u.pos.x), math.abs(m.y - u.pos.y)) + math.abs(m.z - u.pos.z) * 3)
    local mu = df.unit.find(m.id)
    local job = mu.job.current_job and dfhack.df2utf(dfhack.job.getName(mu.job.current_job)) or ''
    out.jobs[job] = (out.jobs[job] or 0) + 1
end
local incident = u.counters.death_id ~= -1 and df.incident.find(u.counters.death_id)
if incident then
    local killer = df.unit.find(incident.criminal)
    out.cause = df.death_type[incident.death_cause] or ''
    out.killer = killer and dfhack.df2utf(dfhack.units.getReadableName(killer)) or ''
    out.in_squad = ids[incident.criminal] == true
end
return out
"""


# Far from everyone but reachable, so nobody else gets there first.
PICK_QUIET_TILE = """
local leader = df.unit.find({uid})
local group = dfhack.maps.getWalkableGroup(leader.pos)
local squad_ids = {{}}
for _, m in ipairs(members(df.squad.find({squad}))) do squad_ids[m.id] = true end
local others = {{}}
for _, u in ipairs(dfhack.units.getCitizens(true)) do
    if not squad_ids[u.id] then table.insert(others, u) end
end
local best, best_gap
for x = 1, df.global.world.map.x_count - 2, 3 do
    for y = 1, df.global.world.map.y_count - 2, 3 do
        local z = leader.pos.z
        local tile = dfhack.maps.getTileType(x, y, z)
        local flags = dfhack.maps.getTileFlags(x, y, z)
        local d = math.max(math.abs(x - leader.pos.x), math.abs(y - leader.pos.y))
        if tile and flags and not flags.hidden and d >= 15 and d <= 70
                and df.tiletype_shape.attrs[df.tiletype.attrs[tile].shape].walkable
                and dfhack.maps.getWalkableGroup(xyz2pos(x, y, z)) == group then
            local gap = 999
            for _, u in ipairs(others) do
                gap = math.min(gap, math.max(math.abs(u.pos.x - x), math.abs(u.pos.y - y)) + math.abs(u.pos.z - z) * 3)
            end
            if not best or gap > best_gap then best, best_gap = {{x=x, y=y, z=z, gap=gap, from_leader=d}}, gap end
        end
    end
end
return best or {{}}
"""


def scenario_kill(df: DFHackRPC) -> tuple[bool, str]:
    """A dangerous creature in sight shows up as a target; a kill order sends the squad after it."""
    squad = lua_json(PICK_SQUAD)
    tile = lua_json(MEMBERS + PICK_QUIET_TILE.format(uid=squad["members"][0]["id"], squad=squad["id"]))
    if "x" not in tile:
        return False, "no reachable tile to bring an enemy to"
    log(f"quiet tile {tile}")
    enemy = lua_json(BRING_ENEMY.format(x=tile["x"], y=tile["y"], z=tile["z"]))
    if "error" in enemy:
        return False, enemy["error"]
    log(f"enemy {enemy}")
    targets = coop(df, "squad_targets")
    ids = [t["id"] for t in targets] if isinstance(targets, list) else []
    if enemy["id"] not in ids:
        return False, f"the enemy isn't offered as a target: {str(targets)[:200]}"
    answer = coop(df, "squad_kill", squad["id"], enemy["id"])
    if not answer.get("ok"):
        return False, f"kill order refused: {answer}"
    state = lambda: lua_json(MEMBERS + UNIT_STATE.format(id=enemy["id"], squad=squad["id"]))  # noqa: E731
    start = state()
    seen = {"nearest": start.get("nearest", 999), "last": start}

    def finished() -> bool:
        now = state()
        seen["last"] = now
        seen["nearest"] = min(seen["nearest"], now.get("nearest", 999))
        log(f"dead {now['dead']}; nearest member {now.get('nearest')} tiles; jobs {now.get('jobs')}")
        return now["dead"]

    ticks = run_game(df, 240, finished, every=2)
    orders = lua_json(f"return {{n=#df.squad.find({squad['id']}).orders}}")["n"]
    coop(df, "squad_clear", squad["id"])
    last = seen["last"]
    # Dying near the squad isn't enough: it could have died of anything.
    ok = (last["dead"] and last.get("in_squad")) or seen["nearest"] <= 1
    return bool(ok), (f"{ticks} ticks; {enemy['race']} started {start.get('nearest')} tiles from the squad, nearest "
                      f"{seen['nearest']}; dead {last['dead']}, cause {last.get('cause')!r}, killer {last.get('killer')!r} "
                      f"(in this squad: {last.get('in_squad')}); {orders} orders left on the squad afterwards")


def scenario_squad(df: DFHackRPC) -> tuple[bool, str]:
    """Remove a real member and add them back, with game time and a save and reload after."""
    squad = lua_json(PICK_SQUAD)
    member = next((m for m in squad["members"] if m["slot"] != 0), None)
    if not member:
        return False, "no member but the leader"
    uid, sid = member["id"], squad["id"]
    state = f"local u = df.unit.find({uid}); return {{squad=u.military.squad_id, slot=u.military.squad_position, " \
            f"dead=dfhack.units.isDead(u)}}"
    answer = coop(df, "squad_remove", sid, uid)
    out = lua_json(state)
    if not answer.get("ok") or out["squad"] != -1:
        return False, f"remove: {answer}, then {out}"
    run_game(df, 15, lambda: False)
    answer = coop(df, "squad_add", sid, uid)
    back = lua_json(state)
    if not answer.get("ok") or back["squad"] != sid:
        return False, f"add: {answer}, then {back}"
    ticks = run_game(df, 15, lambda: False)
    save_now()
    reload_fort()
    fresh = connect()
    try:
        after = lua_json(state)
        survives = coop(fresh, "squads")
    finally:
        fresh.close()
    listed = any(m.get("id") == uid or m.get("unit") == uid or m.get("i") == uid
                 for q in survives if q["id"] == sid for m in q.get("members", []))
    ok = after["squad"] == sid and after["slot"] == back["slot"] and listed
    return ok, f"removed, {ticks} ticks, added to slot {back['slot']}; after save and reload slot {after['slot']}, listed {listed}"


def order_state(ids: list[int]) -> dict:
    wanted = ",".join(str(i) for i in ids)
    return lua_json(f"""
local out = {{}}
for _, id in ipairs{{{wanted}}} do
    for _, o in ipairs(df.global.world.manager_orders.all) do
        if o.id == id then out[tostring(id)] = {{active=o.status.active, validated=o.status.validated,
                                                conds=#o.item_conditions + #o.order_conditions, left=o.amount_left}} end
    end
end
return out
""")


def scenario_conditions(df: DFHackRPC) -> tuple[bool, str]:
    """The manager acts on conditions we wrote: an easy one starts, an impossible one doesn't."""
    next_id = lua_json("return {n=df.global.world.manager_orders.manager_order_next_id}")["n"]
    created = []
    for _ in range(3):
        answer = coop(df, "order", "ConstructBed", "", "wood", 1)
        if not answer.get("ok"):
            return False, f"queueing refused: {answer}"
    created = list(range(next_id, next_id + 3))
    easy, impossible, waiting = created
    for oid, args in ((easy, ("BED", "AtLeast", 0)), (impossible, ("BED", "AtLeast", 999999))):
        answer = coop(df, "add_item_condition", oid, *args)
        if not answer.get("ok"):
            return False, f"condition refused: {answer}"
    answer = coop(df, "add_order_condition", waiting, impossible, "Completed")
    if not answer.get("ok"):
        return False, f"after-order refused: {answer}"
    for oid in reversed(created):
        coop(df, "move_order", oid, "top")
    log(f"easy {easy}, impossible {impossible}, waiting on impossible {waiting}")
    seen = {"easy_started": False, "state": {}}

    def manager_decided() -> bool:
        state = order_state(created)
        seen["state"] = state
        record = state.get(str(easy))
        started = not record or record["active"] or record["left"] == 0
        seen["easy_started"] = seen["easy_started"] or started
        log(f"{state}")
        checked = state.get(str(impossible), {}).get("validated") and state.get(str(waiting), {}).get("validated")
        return seen["easy_started"] and bool(checked)

    ticks = run_game(df, 400, manager_decided, every=10)
    state = seen["state"]
    bad_started = any(state.get(str(o), {}).get("active") for o in (impossible, waiting))
    for oid in created:
        try:
            coop(df, "cancel_order", oid)
        except DFHackRPCError:
            pass
    decided = seen["easy_started"] and state.get(str(impossible), {}).get("validated")
    return bool(decided and not bad_started), (f"{ticks} ticks; easy started {seen['easy_started']}; "
                                               f"impossible and waiting {state}")


def scenario_noble(df: DFHackRPC) -> tuple[bool, str]:
    """Appoint and vacate a real post, with a save and reload after each."""
    posts = coop(df, "nobles")
    post = next((p for p in posts if not p.get("locked") and not p.get("holder")), None)
    if not post:
        return False, "no vacant post that can be changed"
    candidates = coop(df, "noble_candidates")
    units = candidates if isinstance(candidates, list) else candidates.get("units", [])
    if not units:
        return False, "nobody could hold a post"
    uid = units[0]["id"]
    answer = coop(df, "appoint_noble", post["id"], uid)
    if not answer.get("ok"):
        return False, f"appoint refused: {answer}"
    holder = lambda d: next((p.get("holder") for p in coop(d, "nobles") if p["id"] == post["id"]), None)  # noqa: E731
    named = holder(df)
    ticks = run_game(df, 20, lambda: False)
    save_now()
    reload_fort()
    fresh = connect()
    try:
        reloaded = holder(fresh)
        answer = coop(fresh, "vacate_noble", post["id"])
        vacated = holder(fresh)
        run_game(fresh, 15, lambda: False)
        save_now()
    finally:
        fresh.close()
    reload_fort()
    fresh = connect()
    try:
        final = holder(fresh)
    finally:
        fresh.close()
    ok = bool(named) and named == reloaded and answer.get("ok") and not vacated and not final
    return ok, (f"{post['title']}: appointed {named!r}, {ticks} ticks; after reload {reloaded!r}; "
                f"vacated {not vacated}; after second reload {final!r}")


DETAIL_CHECK = """
-- Every adult's labors against what the details say, DF's own rule.
local li = df.global.plotinfo.labor_info
local bad = 0
for _, u in ipairs(dfhack.units.getCitizens(true)) do
    if dfhack.units.isAdult(u) then
        for labor = df.unit_labor._first_item, df.unit_labor._last_item do
            local covered, want = false, false
            if labor >= 0 then
                for _, d in ipairs(li.work_details) do
                    if d.allowed_labors[labor] then
                        covered = true
                        local member = false
                        for _, id in ipairs(d.assigned_units) do member = member or id == u.id end
                        if d.flags.mode == df.work_detail_mode.EverybodyDoesThis
                                or (d.flags.mode == df.work_detail_mode.OnlySelectedDoesThis and member) then want = true end
                    end
                end
                if covered and u.status.labors[labor] ~= want then bad = bad + 1 end
            end
        end
    end
end
return {bad=bad}
"""

def scenario_workdetail(df: DFHackRPC) -> tuple[bool, str]:
    """What the panel writes matches DF's rule for every adult and sticks: through game time, and
    through a save and reload. It doesn't show that job choice follows: see ROADMAP.md."""
    details = {d["name"]: d for d in coop(df, "work_details")["details"]}
    if lua_json(DETAIL_CHECK)["bad"]:
        return False, f"labors already differ from details before starting: {lua_json(DETAIL_CHECK)}"
    engravers, planters = details["Engravers"], details["Planters"]
    picked = lua_json("""
local out = {}
for _, u in ipairs(dfhack.units.getCitizens(true)) do
    if dfhack.units.isAdult(u) and not u.flags1.inactive then table.insert(out, u.id) end
end
return out
""")
    picked = [u for u in picked if u not in engravers["members"]][:3]
    for uid in picked:
        answer = coop(df, "work_detail_member", engravers["i"], engravers["name"], uid, 1)
        if not answer.get("ok"):
            return False, f"picking {uid}: {answer}"
    for uid in engravers["members"]:
        answer = coop(df, "work_detail_member", engravers["i"], engravers["name"], uid, 0)
        if not answer.get("ok"):
            return False, f"taking {uid} off: {answer}"
    for mode in ("NobodyDoesThis", "EverybodyDoesThis", "OnlySelectedDoesThis"):
        answer = coop(df, "work_detail_mode", planters["i"], planters["name"], mode)
        if not answer.get("ok"):
            return False, f"mode {mode}: {answer}"
    answer = coop(df, "work_detail_mode", planters["i"], planters["name"], "NobodyDoesThis")
    counts = """
local function holding(labor)
    local n = 0
    for _, u in ipairs(dfhack.units.getCitizens(true)) do if u.status.labors[labor] then n = n + 1 end end
    return n
end
return {engrave=holding('ENGRAVER'), plant=holding('PLANT')}
"""
    now = lua_json(counts)
    mismatched = lua_json(DETAIL_CHECK)["bad"]
    ticks = run_game(df, 120, lambda: False)
    played = lua_json(counts)
    played_bad = lua_json(DETAIL_CHECK)["bad"]
    save_now()
    reload_fort()
    fresh = connect()
    try:
        after = {d["name"]: d for d in coop(fresh, "work_details")["details"]}
    finally:
        fresh.close()
    loaded = lua_json(counts)
    loaded_bad = lua_json(DETAIL_CHECK)["bad"]
    want = {"engrave": len(picked), "plant": 0}
    ok = (now == want and played == want and loaded == want and mismatched == played_bad == loaded_bad == 0
          and sorted(after["Engravers"]["members"]) == sorted(picked) and after["Planters"]["mode"] == "NobodyDoesThis")
    return ok, (f"labor holders {now}; after {ticks} ticks {played}; after reload {loaded}. Mismatches with DF's rule: "
                f"{mismatched}, {played_bad}, {loaded_bad}. Engravers after reload {after['Engravers']['members']}, "
                f"Planters {after['Planters']['mode']}")


# Who is working on a job at this workshop right now.
WORKING_HERE = """
local out = {{}}
for _, j in ipairs(df.building.find({wid}).jobs) do
    local w = dfhack.job.getWorker(j)
    if w then table.insert(out, w.id) end
end
return out
"""

JOB_FLAGS = """
for _, j in ipairs(df.building.find({wid}).jobs) do
    if j.id == {job} then
        local w = dfhack.job.getWorker(j)
        return {{here=true, suspend=j.flags.suspend, now=j.flags.do_now, rep=j.flags['repeat'], worker=w and w.id or -1}}
    end
end
return {{here=false}}
"""


def watch_workers_here(df: DFHackRPC, wid: int, seconds: float) -> tuple[set, int]:
    who: set = set()

    def look() -> bool:
        who.update(lua_json(WORKING_HERE.format(wid=wid)))
        return False

    return who, run_game(df, seconds, look, every=2)


def scenario_workshop(df: DFHackRPC) -> tuple[bool, str]:
    """A real job in a busy Masons workshop takes suspend, resume, do now, repeat and cancel as the
    panel sends them; one assigned worker, and who works there before and after; the assignment
    survives a save and reload. (Orders tied to a workshop by workshop_id are accepted by DF but
    never worked: see ROADMAP.md.)"""
    pick = lua_json("""
local best, most = nil, 0
for _, b in ipairs(df.global.world.buildings.other.WORKSHOP_ANY) do
    if b.type == df.workshop_type.Masons and b:getBuildStage() == b:getMaxBuildStage() then
        local n = 0
        for _, j in ipairs(b.jobs) do
            if df.job_type[j.job_type] ~= 'DestroyBuilding' then n = n + 1 end
        end
        if n > most then best, most = b, n end
    end
end
if not best then return {} end
return {id=best.id, x=best.centerx, y=best.centery, z=best.z, jobs=most}
""")
    if not pick:
        return False, "no Masons workshop with a task"
    wid = pick["id"]
    info = coop(df, "place_info", wid, pick["x"], pick["y"], pick["z"])
    if info.get("kind") != "workshop" or info.get("workshop") != "Masons" or not info.get("jobs"):
        return False, f"place_info says {info}"
    job = next(j for j in info["jobs"] if not j["building"])
    log(f"workshop {wid} ({info['name']}): {[j['name'] for j in info['jobs']]}; using {job['id']} {job['name']}")

    steps = {}
    for action in ("now", "repeat", "suspend"):
        answer = coop(df, "workshop_job", wid, job["id"], action)
        if not answer.get("ok"):
            return False, f"{action} refused: {answer}"
    steps["set"] = lua_json(JOB_FLAGS.format(wid=wid, job=job["id"]))
    run_game(df, 20, lambda: False)
    steps["after play"] = lua_json(JOB_FLAGS.format(wid=wid, job=job["id"]))     # suspended: nobody takes it
    for action in ("resume", "notnow", "once"):
        coop(df, "workshop_job", wid, job["id"], action)
    steps["cleared"] = lua_json(JOB_FLAGS.format(wid=wid, job=job["id"]))
    cancel = coop(df, "workshop_job", wid, job["id"], "cancel")
    steps["cancelled"] = lua_json(JOB_FLAGS.format(wid=wid, job=job["id"]))
    log(f"{steps}")

    before, ticks_before = watch_workers_here(df, wid, 60)
    chosen = lua_json(f"""
local out = {{}}
for _, u in ipairs(dfhack.units.getCitizens(true)) do
    if dfhack.units.isAdult(u) and not u.flags1.inactive and u.status.labors.MASON and u.military.squad_id == -1 then
        table.insert(out, u.id)
    end
end
return out
""")
    others = [u for u in chosen if u not in before]
    if not others:
        return False, f"no other mason to assign; before {sorted(before)}"
    worker = others[0]
    answer = coop(df, "workshop_worker", wid, worker)
    if not answer.get("ok"):
        return False, f"assigning {worker} refused: {answer}"
    log(f"before: {sorted(before)} worked here in {ticks_before} ticks; assigned {worker} ({answer.get('name')})")
    run_game(df, 30, lambda: False)               # jobs already under way may finish
    after, ticks_after = watch_workers_here(df, wid, 90)
    log(f"after: {sorted(after)} in {ticks_after} ticks")
    save_now()
    reload_fort()
    fresh = connect()
    try:
        reloaded = coop(fresh, "place_info", wid, pick["x"], pick["y"], pick["z"])
        freed = coop(fresh, "workshop_worker", wid, "")
    finally:
        fresh.close()
    s_set, s_play, s_clear, s_gone = steps["set"], steps["after play"], steps["cleared"], steps["cancelled"]
    ok = (s_set["suspend"] and s_set["now"] and s_set["rep"] and s_set["worker"] == -1
          and s_play["suspend"] and s_play["worker"] == -1
          and not s_clear["suspend"] and not s_clear["now"] and not s_clear["rep"]
          and cancel.get("ok") and not s_gone["here"]
          and not (after - {worker})
          and [w["id"] for w in reloaded.get("workers", [])] == [worker] and freed.get("ok"))
    return ok, (f"job {job['id']}: set {s_set}, after play {s_play}, cleared {s_clear}, cancelled {s_gone}. "
                f"Workers here before {sorted(before)} in {ticks_before} ticks; with {worker} assigned "
                f"{sorted(after)} in {ticks_after} ticks. After reload workers {reloaded.get('workers')}")


# Planting at one plot: the seed in each PlantSeeds job there, and the seeds the plot holds.
PLANTING = """
local b = df.building.find({wid})
local jobs, held = {{}}, {{}}
local link = df.global.world.jobs.list.next
while link do
    local j = link.item
    if j and j.job_type == df.job_type.PlantSeeds then
        for _, ref in ipairs(j.general_refs) do
            if df.general_ref_building_holderst:is_instance(ref) and ref.building_id == {wid} then
                for _, it in ipairs(j.items) do
                    if it.item:getType() == df.item_type.SEEDS then table.insert(jobs, it.item:getMaterialIndex()) end
                end
            end
        end
    end
    link = link.next
end
for _, i in ipairs(b.contained_items) do
    if i.item:getType() == df.item_type.SEEDS then
        local k = tostring(i.item:getMaterialIndex())
        held[k] = (held[k] or 0) + 1
    end
end
return {{jobs=jobs, held=held, crop=b.plant_id[df.global.cur_season], season=df.global.cur_season}}
"""


def planting(wid: int) -> dict:
    now = lua_json(PLANTING.format(wid=wid))
    now["held"] = now["held"] or {}       # an empty Lua table arrives as []
    now["jobs"] = now["jobs"] or []
    return now


def scenario_farm(df: DFHackRPC) -> tuple[bool, str]:
    """A crop picked for this season through the panel's function is the seed dwarves take to plant
    there (every planting job that carried seeds carried those); the choice and the fertilize
    switch survive a save and reload. Finished planting is reported, not required: the busiest
    plot in b4_coop is deep and slow to get planted."""
    plots = lua_json("""
local waiting = {}
local link = df.global.world.jobs.list.next
while link do
    local j = link.item
    if j and j.job_type == df.job_type.PlantSeeds then
        for _, ref in ipairs(j.general_refs) do
            if df.general_ref_building_holderst:is_instance(ref) then
                waiting[ref.building_id] = (waiting[ref.building_id] or 0) + 1
            end
        end
    end
    link = link.next
end
local out = {}
for _, b in ipairs(df.global.world.buildings.other.FARM_PLOT) do
    table.insert(out, {id=b.id, x=b.centerx, y=b.centery, z=b.z, waiting=waiting[b.id] or 0})
end
return out
""")
    # A plot DF is planting right now: in b4_coop the surface plot gets no planting jobs at all.
    plots.sort(key=lambda p: -p["waiting"])
    if not plots:
        return False, "no farm plots"
    # DFHack's autofarm is on in b4_coop and replaces every crop choice; the panel says so.
    # Switched off here (on the bench only) so what dwarves plant follows our choice alone.
    first = coop(df, "place_info", plots[0]["id"], plots[0]["x"], plots[0]["y"], plots[0]["z"])
    autofarm_seen = first.get("autofarm")
    run_lua_text("dfhack.run_command_silent('disable', 'autofarm')")
    autofarm_after = coop(df, "place_info", plots[0]["id"], plots[0]["x"], plots[0]["y"], plots[0]["z"])["autofarm"]
    log(f"autofarm on: panel said {autofarm_seen}; after disabling it on the bench {autofarm_after}")
    chosen = None
    for plot in plots:
        info = coop(df, "place_info", plot["id"], plot["x"], plot["y"], plot["z"])
        season = info["season"]
        current = info["crops"][season]["id"]
        others = [o for o in info["options"][season] if o["seeds"] > 0 and o["id"] != current]
        if others:
            chosen = (plot, info, season, current, max(others, key=lambda o: o["seeds"]))
            break
    if not chosen:
        return False, f"no plot with another crop that has seeds: {plots}"
    plot, info, season, current, crop = chosen
    wid = plot["id"]
    before = planting(wid)
    log(f"plot {wid} ({info['tiles']} tiles, underground {info['underground']}), season {season}: "
        f"{info['crops'][season]['name']} -> {crop['name']} ({crop['seeds']} seeds); now {before}")
    answer = coop(df, "farm_crop", wid, season, crop["id"])
    if not answer.get("ok"):
        return False, f"farm_crop refused: {answer}"
    fert = coop(df, "farm_fertilize", wid, 0 if info["fertilize"] else 1)
    planted: list = []

    def look() -> bool:
        now = planting(wid)
        planted.extend(now["jobs"])
        seen["now"] = now
        return now["held"].get(str(crop["id"]), 0) > before["held"].get(str(crop["id"]), 0)

    seen: dict = {}
    ticks = run_game(df, 400, look, every=4)
    after = seen.get("now", {})
    log(f"after {ticks} ticks: planting jobs used seeds {sorted(set(planted))}; plot holds {after.get('held')}")
    save_now()
    reload_fort()
    fresh = connect()
    try:
        reloaded = coop(fresh, "place_info", wid, plot["x"], plot["y"], plot["z"])
    finally:
        fresh.close()
    gained = after.get("held", {}).get(str(crop["id"]), 0) - before["held"].get(str(crop["id"]), 0)
    wrong = sorted(set(planted) - {crop["id"]})   # jobs made before the change may still carry the old seed
    ok = (autofarm_seen is True and autofarm_after is False
          and fert.get("ok") and set(planted) == {crop["id"]} and after.get("crop") == crop["id"]
          and reloaded["crops"][season]["id"] == crop["id"] and reloaded["fertilize"] != info["fertilize"])
    return ok, (f"autofarm seen {autofarm_seen}, then off. "
                f"plot {wid} season {season}: {info['crops'][season]['name']} -> {crop['name']}. In {ticks} ticks "
                f"{gained} more {crop['name']} planted; planting jobs used plants {sorted(set(planted))} "
                f"(other than chosen: {wrong}). After reload crop {reloaded['crops'][season]}, fertilize "
                f"{info['fertilize']} -> {reloaded['fertilize']}")


# Tiles near a citizen that one of the smoothing kinds applies to, as quickfort would judge:
# rough stone floor (smooth), smooth unengraved floor (engrave), smooth wall (fortify).
DETAIL_TARGETS = """
local engraved = {}
for _, e in ipairs(df.global.world.event.engravings) do
    engraved[e.pos.x .. ',' .. e.pos.y .. ',' .. e.pos.z] = true
end
local function kind_of(x, y, z)
    local tt = dfhack.maps.getTileType(x, y, z)
    local f = dfhack.maps.getTileFlags(x, y, z)
    if not tt or not f or f.hidden or f.smooth ~= 0 or f.dig ~= 0 then return nil end
    if dfhack.buildings.findAtTile(xyz2pos(x, y, z)) then return nil end
    local a = df.tiletype.attrs[tt]
    if a.material ~= df.tiletype_material.STONE and a.material ~= df.tiletype_material.MINERAL then return nil end
    if a.shape == df.tiletype_shape.FLOOR and a.special == df.tiletype_special.NORMAL then return 'smooth' end
    if a.shape == df.tiletype_shape.FLOOR and a.special == df.tiletype_special.SMOOTH
            and not engraved[x .. ',' .. y .. ',' .. z] then return 'engrave' end
    if a.shape == df.tiletype_shape.WALL and a.special == df.tiletype_special.SMOOTH then return 'fortify' end
end
local found = {}
for _, u in ipairs(dfhack.units.getCitizens(true)) do
    local group = dfhack.maps.getWalkableGroup(u.pos)
    for dx = -12, 12 do for dy = -12, 12 do
        local x, y, z = u.pos.x + dx, u.pos.y + dy, u.pos.z
        local k = kind_of(x, y, z)
        if k and not found[k] then
            local near = false
            for ax = -1, 1 do for ay = -1, 1 do
                if dfhack.maps.getWalkableGroup(xyz2pos(x + ax, y + ay, z)) == group then near = true end
            end end
            if near then found[k] = {x=x, y=y, z=z} end
        end
    end end
    if found.smooth and found.engrave and found.fortify then break end
end
return found
"""

DETAIL_JOBS_AT = """
local want = {{}}
for _, t in ipairs({tiles}) do want[t[1] .. ',' .. t[2] .. ',' .. t[3]] = true end
local out = {{}}
local link = df.global.world.jobs.list.next
while link do
    local j = link.item
    if j and want[j.pos.x .. ',' .. j.pos.y .. ',' .. j.pos.z] then
        out[#out + 1] = {{j.pos.x, j.pos.y, j.pos.z, df.job_type[j.job_type]}}
    end
    link = link.next
end
return out
"""

DETAIL_JOB_KIND = {"SmoothFloor": "smooth", "SmoothWall": "smooth", "DetailFloor": "engrave",
                   "DetailWall": "engrave", "CarveFortification": "fortify"}


def scenario_detail(df: DFHackRPC) -> tuple[bool, str]:
    """Smooth, engrave and fortify marks made through coop.lua become DF's own jobs of the right
    kind on those tiles, the hub's mark reader sees them, they survive a save and reload, and
    erase cancels them for good. (Whether dwarves do them can't be shown here: no job from DF's
    posting board is ever claimed on the bench; see ROADMAP.md.)"""
    targets = lua_json(DETAIL_TARGETS)
    if not isinstance(targets, dict) or not targets:
        return False, f"no tiles to try: {targets}"
    log(f"targets {targets}")
    tiles = []
    for kind, t in targets.items():
        answer = coop(df, "detail", kind, t["x"], t["y"], t["x"], t["y"], t["z"], 3)
        if not answer.get("ok") or answer.get("applied") != 1:
            return False, f"{kind} at {t}: {answer}"
        tiles.append((t["x"], t["y"], t["z"], kind))
    spec = "{" + ",".join(f"{{{x},{y},{z}}}" for x, y, z, _ in tiles) + "}"
    run_game(df, 3, lambda: False, every=1)
    jobs = lua_json(DETAIL_JOBS_AT.format(tiles=spec)) or []
    made = {(x, y, z): DETAIL_JOB_KIND.get(name, name) for x, y, z, name in jobs}
    want = {(x, y, z): kind for x, y, z, kind in tiles}
    levels = ",".join(sorted({str(z) for _, _, z, _ in tiles}))
    marks = coop(df, "detail_marks", levels)
    seen = {(x, y, int(z)): k for z, rows in (marks.items() if isinstance(marks, dict) else []) for x, y, k in rows}
    log(f"jobs {made}; marks read back {seen}")
    save_now()
    reload_fort()
    fresh = connect()
    try:
        after = coop(fresh, "detail_marks", levels)
        reloaded = {(x, y, int(z)): k for z, rows in (after.items() if isinstance(after, dict) else [])
                    for x, y, k in rows}
        xs, ys = [x for x, _, _, _ in tiles], [y for _, y, _, _ in tiles]
        z0 = tiles[0][2]
        cleared = []
        for z in sorted({z for _, _, z, _ in tiles}):
            same = [(x, y) for x, y, tz, _ in tiles if tz == z]
            cleared.append(coop(fresh, "detail_erase", min(x for x, _ in same), min(y for _, y in same),
                                max(x for x, _ in same), max(y for _, y in same), z).get("cleared", 0))
        run_game(fresh, 3, lambda: False, every=1)
        left = lua_json(DETAIL_JOBS_AT.format(tiles=spec)) or []
        left_marks = coop(fresh, "detail_marks", levels)
    finally:
        fresh.close()
    gone = not [j for j in left if j[3] in DETAIL_JOB_KIND] and not (isinstance(left_marks, dict) and
                                                                      any(left_marks.values()))
    ok = (made == want and all(seen.get(k) == v for k, v in want.items())
          and all(reloaded.get(k) == v for k, v in want.items()) and sum(cleared) >= len(want) and gone)
    return ok, (f"marked {want}; DF made jobs {made}; read back {seen}; after reload {reloaded}; erase cleared "
                f"{cleared}, jobs left {left}, marks left {left_marks} (z{z0}, x {min(xs)}-{max(xs)}, "
                f"y {min(ys)}-{max(ys)})")


def scenario_hub(df: DFHackRPC) -> tuple[bool, str]:
    """A real hub and browser on the bench: Space pauses and resumes DF, and a save doesn't
    knock players offline."""
    from playwright.sync_api import sync_playwright
    config, data = BENCH / "players.json", BENCH / "hubdata"
    config.unlink(missing_ok=True)
    env = {**os.environ, "DF_COOP_CONFIG": str(config), "DF_COOP_DATA": str(data)}
    hub_py = str(ROOT / "hub.py")
    subprocess.run([sys.executable, hub_py, "add-player", "Bench"], env=env, check=True, capture_output=True)
    token = json.loads(config.read_text())["players"][0]["token"]
    df.set_paused(False)
    hub_log = BENCH / "hub.log"
    with open(hub_log, "w") as out:
        hub = subprocess.Popen([sys.executable, hub_py, "serve", "--port", str(HUB_PORT), "--df-port", str(PORT)],
                               env=env, stdout=out, stderr=out, start_new_session=True)
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            for _ in range(20):
                try:
                    page.goto(f"http://127.0.0.1:{HUB_PORT}/?token={token}")
                    break
                except Exception:  # the hub is still starting
                    time.sleep(1)
            page.wait_for_function("st.df === 'online' && st.squads && st.squads.length > 0", timeout=60000)
            page.evaluate("$('away').hidden = true")
            paused = lambda: "true" in run_lua_text("print(df.global.pause_state)")  # noqa: E731
            page.keyboard.press("Space")
            time.sleep(1.5)
            held = paused()
            page.keyboard.press("Space")
            time.sleep(1.5)
            released = not paused()
            page.evaluate("window.offline = 0; setInterval(() => { if (st.df !== 'online') window.offline++; }, 200)")
            silent = save_now()
            time.sleep(3)
            offline_ticks = page.evaluate("window.offline")
            still = page.evaluate("st.df")
            browser.close()
    finally:
        hub.terminate()
        hub.wait(timeout=15)
    dropped = "offline" in hub_log.read_text().lower() or "lost" in hub_log.read_text().lower()
    ok = held and released and still == "online" and offline_ticks == 0 and not errors and not dropped
    return ok, (f"Space paused {held}, Space resumed {released}; save silent {silent:.0f}s, browser offline for "
                f"{offline_ticks * 0.2:.1f}s, hub log shows a drop: {dropped}; page errors {errors}")


SCENARIOS = {"hub": scenario_hub, "detail": scenario_detail, "farm": scenario_farm, "workshop": scenario_workshop, "workdetail": scenario_workdetail, "station": scenario_station, "kill": scenario_kill, "squad": scenario_squad,
             "conditions": scenario_conditions, "noble": scenario_noble}


def run_scenarios(names: list[str]) -> int:
    failed = 0
    for name in names:
        df = connect()
        log(f"--- {name}")
        try:
            ok, seen = SCENARIOS[name](df)
        except (DFHackRPCError, RuntimeError, OSError, ValueError, SystemExit, subprocess.TimeoutExpired) as exc:
            ok, seen = False, f"error: {exc}"
        finally:
            df.close()
        print(f"{'PASS' if ok else 'FAIL'} {name}: {seen}", flush=True)
        failed += not ok
    return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup").add_argument("save")
    sub.add_parser("start").add_argument("save", nargs="?")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("run").add_argument("names", nargs="+", choices=sorted(SCENARIOS))
    args = parser.parse_args(argv)
    if args.command == "setup":
        setup(args.save)
    elif args.command == "start":
        start(args.save)
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        print("running" if running() else "stopped")
    elif args.command == "list":
        for name, fn in SCENARIOS.items():
            print(f"{name}: {fn.__doc__}")
    elif args.command == "run":
        return 1 if run_scenarios(args.names) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
