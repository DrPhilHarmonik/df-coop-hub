"""The co-op hub: one DFHack connection in, many players out.

    python3 hub.py add-player NAME [--role R]   # prints a token and join link
    python3 hub.py set-role NAME ROLE     # overseer builder miner quartermaster commander visitor
    python3 hub.py players                # who has a token, and their role
    python3 hub.py serve [--host H] [--port P]
    python3 hub.py install-lua            # copy rpc/coop.lua into DFHack

The hub serves the browser client and a websocket on the same port. Players
join with a token (from `add-player`) in the link; tokens live outside the
repo, in ~/.config/df-coop/players.json, and the hub refuses to start with
none. The hub re-reads that file when it changes, so new players can join
without a restart.

Players never talk to DFHack. The hub polls only the z-levels someone is
looking at (each player's level and the one below it, which shows through
open space), keeps the last state of every block it has seen, and sends each
player what changed on their levels. Hidden tiles, and the units and
buildings on them, are cut out before anything leaves the hub; see
mapstate.py.

Messages, hub to player (JSON):
    hello      {you, map: {w, h, d}, tiles, view: {x, y, z}, paused}
    blocks     {blocks: [{x, y, z, t, w?, m?, d?}]}   block coords, 256 entries
    buildings  {buildings: [...]}                        on the player's levels
    units      {units: [{i, x, y, z, n, c}], paused}
    players    {players: [{id, name, color, z, cx, cy}]}
    status     {df: "online" | "offline"}
    role       {role, perms}  sent when the host changes this player's role
    denied     {action, error}             the player's role doesn't allow that
    pause      {paused, holders: [names]}
    owners     {owners: [[x, y, z, name, color, priority]]}   who designated what, on the
                                                    player's levels
    dig_result {ok, kind, applied, skipped, error?}
    build_result {ok, item, label, placed, problems: [str], error?}
    remove_result {ok, what?, outcome?: removed|queued|already|nothing, error?}
    orders     {orders: [{id, label, material, left, total, freq, status, conds}]}   the
                                            manager queue, in queue order; status:
                                            active|validated|waiting, conds: [str]
    order_result {ok, label, amount, error?}
    cancel_result {ok, label?, error?}
    edit_result {ok, label?, error?}
    move_result {ok, error?}
    squads     {squads: [{id, name, alias, slots, routine, orders: [str],
                members: [{slot, leader, unit, name, away}]}]}
    squad_candidates {ok, units?: [{id, name}], error?}   adult citizens in no squad
    squad_result {ok, action, name?, error?}
    nobles     {nobles: [{id, code, title, holder, locked, duties: [str], needs: {..}}]}
                                            in order of rank; locked says why a
                                            post can't be changed here, or is ""
    noble_candidates {ok, units?: [{id, name}], error?}
    noble_result {ok, action, title?, name?, error?}
    reports    {reports: [{id, text, color, date, x?, y?, z?}]}   new announcements
    unit_info  {ok, unit?: {name, profession, job, mood, age, squad, needs}, error?}
    chat       {entry: {kind: chat|ping, from, color, text?, x?, y?, z?, date}}
    away       {since, now, actions: [str], reports: [...]}   on return after a while
    ping       {from, color, x, y, z}         draw a marker there for a few seconds
    date       {date}         e.g. "12th Malachite, year 23"
Player to hub:
    view       {z, cx, cy}    level being viewed and cursor tile
    dig        {kind, x1, y1, x2, y2, z, priority?}   kind: dig updown up down
                                           ramp channel erase (actions.py);
                                           priority 1 (first) to 7, default 4
    pause      {value}        true: pause and hold; false: ready to resume
    order      {id, amount, material?, freq?}   id: a catalog.py ORDERS id;
                                           freq: a catalog.ORDER_FREQUENCIES key
    cancel_order {order}      a manager order id (refused while being worked on)
    edit_order {order, amount?, freq?}   set either or both; a new amount restarts the count
    move_order {order, to}    to: up down top bottom
    squad_candidates {}       who could join a squad
    squad_add  {squad, unit}  into the first open slot
    squad_remove {squad, unit}   not the leader, not while away
    rename_squad {squad, name}   empty restores the generated name
    noble_candidates {}       who could hold a post
    appoint_noble {post, unit}   post: a nobles id; replaces any current holder
    vacate_noble {post}
    inspect    {unit}         a unit id the player can currently see
    chat       {text}         up to 300 characters
    ping       {x, y, z}      point everyone at a tile
    remove     {x, y, z, id?} the building, stockpile or zone on that tile; with
                              id, exactly that one (what the player saw outlined)
    build      {item, x1, y1, z, x2?, y2?}   item: a catalog.py id. Fixed items
                                           centre on x1,y1; area items take
                                           two corners.

Building goes through the in-game module lua/rpc/coop.lua (quickfort under
the hood); `hub.py install-lua` puts it in DFHack. Without it the hub runs
and players can dig, but the build tools are off.

Pausing: anyone can pause, which puts them on hold. The game resumes when
everyone holding has said they're ready. With nobody holding (DF paused
itself), anyone can resume. Unpausing in DF itself overrides every hold.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from away import AWAY_MIN_SECONDS, SeenStore, summarize_actions
from actions import (DEFAULT_ROLE, DIG_KINDS, MAX_DIG_TILES, NEEDS, ROLES, VERB, ActionLog,
                     Owners, RateLimiter, color_for, designation_spans, eligible, rect_tiles)
from catalog import (BY_ID, PAGE_LABELS, PAGES, ITEMS, MAX_AREA_SIDE, MAX_ORDER_AMOUNT, ORDER_FREQUENCIES,
                     ORDER_GROUPS, ORDER_MOVES, ORDERS, ORDERS_BY_ID, STRESS_LABELS, condition_text,
                     game_date, humanize, order_label,
                     summarize)
from dfhack_rpc import DFHackRPC, DFHackRPCError, rfr
from game_tiles import GameTileset
from mapstate import BLOCK, MapState
from tiles import tiletype_table

log = logging.getLogger("df-coop")

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
CONFIG_PATH = Path(os.environ.get("DF_COOP_CONFIG", Path.home() / ".config/df-coop/players.json"))
DATA_DIR = Path(os.environ.get("DF_COOP_DATA", Path.home() / ".local/share/df-coop"))
DF_DIR = Path(os.environ.get("DF_DIR", Path.home() / ".local/share/Steam/steamapps/common/Dwarf Fortress"))
LUA_SOURCE = HERE / "lua" / "rpc" / "coop.lua"
LUA_VERSION = 10
LUA_FOR_REMOVE = 2     # the oldest module each feature works with
LUA_FOR_ORDERS = 3
LUA_FOR_INFO = 4       # announcements and the unit inspector
LUA_FOR_LONG_KEYS = 5  # quickfort keys like "~h" (hive) or "trackNSEW"
LUA_FOR_PRIORITY = 7
LUA_FOR_ORDER_EDITS = 8  # amounts, repeats and queue position
LUA_FOR_SQUADS = 9
LUA_FOR_NOBLES = 10
MAX_SQUAD_NAME = 40
KEEP_REPORTS = 100
KEEP_CHAT = 50
MAX_CHAT = 300
MAX_POLL_LEVELS = 2


def install_lua(df_dir: Path = DF_DIR) -> Path:
    """Copy the in-game module where DFHack's RunLua can find it. A DFHack
    update can remove it again; the hub says so when the module is missing."""
    hack = df_dir / "hack" / "lua"
    if not hack.is_dir():
        raise SystemExit(f"no DFHack Lua folder at {hack}; set DF_DIR to your Dwarf Fortress folder")
    target = hack / "rpc" / "coop.lua"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(LUA_SOURCE.read_bytes())
    # DFHack caches required modules; drop the old copy so a running DF loads
    # this one on the next call. Harmless if DF isn't running.
    runner = df_dir / "dfhack-run"
    if runner.exists():
        import subprocess
        subprocess.run([str(runner), "lua", "package.loaded['rpc.coop'] = nil"], cwd=df_dir,
                       capture_output=True, timeout=15, check=False)
    return target

MAX_INBOUND = 4096  # players only send small messages


# -- player tokens ------------------------------------------------------------

def load_players(path: Path = CONFIG_PATH) -> dict[str, str]:
    """token -> player name"""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {p["token"]: p["name"] for p in data.get("players", [])}


def load_roles(path: Path = CONFIG_PATH) -> dict[str, str]:
    """player name -> role; players without one are overseers"""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {p["name"]: p.get("role", DEFAULT_ROLE) for p in data.get("players", [])}


def _save_players(entries: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"players": entries}, indent=2))
    path.chmod(0o600)


def _entries(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text()).get("players", [])
    except FileNotFoundError:
        return []


def add_player(name: str, path: Path = CONFIG_PATH, role: str = DEFAULT_ROLE) -> str:
    if role not in ROLES:
        raise SystemExit(f"unknown role {role!r}; one of: {', '.join(ROLES)}")
    entries = _entries(path)
    if any(e["name"] == name for e in entries):
        raise SystemExit(f"{name!r} already has a token; see `hub.py players`")
    token = secrets.token_urlsafe(18)
    entries.append({"name": name, "token": token, "role": role})
    _save_players(entries, path)
    return token


def set_role(name: str, role: str, path: Path = CONFIG_PATH) -> None:
    if role not in ROLES:
        raise SystemExit(f"unknown role {role!r}; one of: {', '.join(ROLES)}")
    entries = _entries(path)
    for e in entries:
        if e["name"] == name:
            e["role"] = role
            _save_players(entries, path)
            return
    raise SystemExit(f"no player named {name!r}; see `hub.py players`")


# -- sessions -----------------------------------------------------------------

@dataclass
class Session:
    conn: ServerConnection
    id: int
    name: str
    color: str
    z: int
    cx: int = -1
    cy: int = -1
    last_units: str = field(default="", repr=False)
    role: str = DEFAULT_ROLE
    graphics: bool = False
    limiter: RateLimiter = field(default_factory=RateLimiter, repr=False)
    visible_units: set[int] = field(default_factory=set, repr=False)
    chat_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(rate=1.0, burst=5.0, tokens=5.0),
                                      repr=False)

    @property
    def zs(self) -> set[int]:
        return {self.z, self.z - 1}

    @property
    def perms(self) -> frozenset[str]:
        return ROLES.get(self.role, frozenset())

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color, "role": self.role,
                "perms": sorted(self.perms), "z": self.z, "cx": self.cx, "cy": self.cy}


class Hub:
    def __init__(self, df, players: dict[str, str], *, roles: dict[str, str] | None = None,
                 players_path: Path | None = None,
                 poll_interval: float = 0.25, unit_interval: float = 0.5,
                 static_dir: Path = STATIC_DIR, data_dir: Path = DATA_DIR, df_dir: Path = DF_DIR):
        self.df = df
        self.players = players
        self.roles = roles or {}
        # When set, the token file is re-read whenever it changes, so
        # `add-player` works without restarting the hub.
        self.players_path = players_path
        self._players_mtime = self._mtime()
        self.poll_interval = poll_interval
        self.unit_interval = unit_interval
        self.static_dir = static_dir
        self.game_tileset = GameTileset(df_dir)
        self._graphics_races: set[int] = set()
        self._graphics_rpc = True
        self.data_dir = data_dir
        self.action_log: ActionLog | None = None
        self.seen: SeenStore | None = None
        self.owners = Owners()
        self.holders: dict[int, str] = {}  # session id -> name, for players holding the pause
        self.hold_active = False           # the current pause is the players', not DF's
        self.lua_ok = False                # rpc/coop.lua answers: building is available
        self.lua_version = 0
        self._lua_recheck_due = 0.0
        self._lua_warned = False
        self.date = ""
        self._date_due = 0.0
        self.orders: list[dict] = []
        self._orders_due = 0.0
        self.squads: list[dict] = []
        self._squads_due = 0.0
        self.nobles: list[dict] = []
        self._nobles_due = 0.0
        self.reports: list[dict] = []
        self.chat: list[dict] = []   # memory only; never written to disk
        self._report_after = -1
        self._reports_due = 0.0
        self.state = MapState()
        self._graphics_races.clear()
        self._graphics_rpc = True
        self.tiles: list = []
        self.view = {"x": 0, "y": 0, "z": 0}
        self.paused = True
        self.df_online = False
        self.sessions: dict[ServerConnection, Session] = {}
        self.fetched_z: set[int] = set()
        self._ids = itertools.count(1)
        self._df_lock = asyncio.Lock()
        self._offline_reason = ""
        self._offline_wait = 5.0  # seconds between reconnect attempts

    # -- DFHack side ----------------------------------------------------------

    async def _df(self, fn, *args, **kwargs):
        """Run one blocking DFHack call off the event loop, one at a time."""
        async with self._df_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def setup(self) -> None:
        """Connect if needed and load the fortress's fixed data. Raises
        DFHackRPCError / OSError when DFHack is unreachable or no fortress is
        loaded; the caller decides whether to wait."""
        if not getattr(self.df, "connected", True):
            await self._df(self.df.connect)
        info = await self._df(self.df.map_info)
        # Fresh state every time: after a reload this may be a different fort.
        self.state = MapState()
        self.state.width = info.block_size_x * BLOCK
        self.state.height = info.block_size_y * BLOCK
        self.state.depth = info.block_size_z
        tiletypes = (await self._df(self.df.tiletypes)).tiletype_list
        self.tiles = tiletype_table(tiletypes)
        self.state.tile_kinds = {t.id: (rfr.TiletypeShape.Name(t.shape), rfr.TiletypeMaterial.Name(t.material))
                                 for t in tiletypes}
        if hasattr(self.df, "item_defs"):
            try:
                definitions = (await self._df(self.df.item_defs)).material_list
                self.state.item_defs = {(entry.mat_pair.mat_type, entry.mat_pair.mat_index): entry.id
                                        for entry in definitions}
            except DFHackRPCError as exc:
                log.warning("item graphics definitions unavailable: %s", exc)
        self.reports, self._report_after = [], -1
        fort = fort_key(info)
        self.action_log = ActionLog(self.data_dir / f"{fort}.actions.jsonl")
        self.seen = SeenStore(self.data_dir / f"{fort}.seen.json")
        self.owners = Owners()
        self.owners.replay(self.action_log)
        for d in (await self._df(self.df.building_defs)).building_list:
            bt = d.building_type
            self.state.add_building_def(bt.building_type, bt.building_subtype, bt.building_custom, d.id, d.name)
        v = await self._df(self.df.view_info)
        self.view = {"x": v.view_pos_x + v.view_size_x // 2,
                     "y": v.view_pos_y + v.view_size_y // 2,
                     "z": v.view_pos_z}
        self.paused = await self._df(self.df.paused)
        await self._check_lua()
        self.fetched_z.clear()
        self.df_online = True
        log.info("fortress %s: %dx%dx%d tiles, %d tiletypes",
                 getattr(info, "world_name_english", "") or getattr(info, "save_name", "?"),
                 self.state.width, self.state.height, self.state.depth, len(self.tiles))

    async def _check_lua(self) -> None:
        try:
            version = (await self._df(self.df.run_lua, "rpc.coop", "ping"))[0]
            self.lua_ok = True
            new_version = int(version) if version.isdigit() else 0
            changed = new_version != self.lua_version
            self.lua_version = new_version
            if changed:
                log.info("in-game module rpc/coop.lua v%s found; building is on", version)
            if changed and self.lua_version < LUA_VERSION:
                log.warning("the installed module is older than this hub (v%s < v%d); "
                            "run `python3 hub.py install-lua` for everything to work", version, LUA_VERSION)
        except DFHackRPCError:
            if self.lua_ok or not self._lua_warned:
                log.warning("in-game module missing; building is off until you run: python3 hub.py install-lua")
            self.lua_ok = False
            self._lua_warned = True
            return
        await self._poll_date()
        await self._poll_orders()
        await self._poll_squads()
        await self._poll_nobles()
        await self._poll_reports()

    async def _poll_reports(self) -> None:
        if not self.lua_ok or self.lua_version < LUA_FOR_INFO:
            return
        raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "reports", str(self._report_after)))[0])
        self._report_after = max(self._report_after, int(raw.get("newest", self._report_after)))
        new = [self._shape_report(r) for r in raw.get("reports") or []]
        if new:
            self.reports = (self.reports + new)[-KEEP_REPORTS:]
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "reports", "reports": new}))

    def _shape_report(self, r: dict) -> dict:
        item = {"id": r["id"], "text": r["text"], "color": r.get("color", 7),
                "date": game_date(int(r["year"]), int(r["time"]))}
        x, y, z = r.get("x", -1), r.get("y", -1), r.get("z", -1)
        # Keep "zoom to" only for places the players can see. DF says whether
        # the tile is unexplored (the module checks), because the hub's map may
        # not have that level loaded; a missing verdict counts as hidden.
        if x >= 0 and r.get("hidden") is False and 0 <= z < self.state.depth:
            item.update(x=x, y=y, z=z)
        return item

    async def _send_away(self, s: Session) -> None:
        """What this player missed, if they've been gone a while."""
        if self.seen is None or self.action_log is None:
            return
        last = self.seen.get(s.name)
        if not last or _time() - last["t"] < AWAY_MIN_SECONDS:
            return
        actions = summarize_actions(self.action_log.entries(), last["t"], s.name)
        reports = []
        if self.lua_ok and self.lua_version >= LUA_FOR_INFO:
            try:
                # Nobody was polling while the fort was empty: the date is stale.
                await self._poll_date()
                raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "reports", str(last["report"])))[0])
                reports = [self._shape_report(r) for r in raw.get("reports") or []]
            except DFHackRPCError:
                pass
        if actions or reports or last.get("date") != self.date:
            self._send(s, {"type": "away", "since": last.get("date", ""), "now": self.date,
                           "actions": actions, "reports": reports})

    async def _poll_date(self) -> None:
        if not self.lua_ok:
            return
        raw = (await self._df(self.df.run_lua, "rpc.coop", "date"))[0]
        d = json.loads(raw)
        date = game_date(int(d["year"]), int(d["tick"]))
        if date != self.date:
            self.date = date
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "date", "date": date}))

    async def _poll_orders(self) -> None:
        if not self.lua_ok or self.lua_version < LUA_FOR_ORDERS:
            return
        raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "orders"))[0])
        raw = raw if isinstance(raw, list) else []
        labels = {o["id"]: order_label(o["job"], o["reaction"]) for o in raw}
        orders = [{"id": o["id"], "label": labels[o["id"]], "material": o["material"],
                   "left": o["left"], "total": o["total"], "freq": o["freq"],
                   "status": "active" if o.get("active") else "validated" if o.get("validated") else "waiting",
                   "conds": [condition_text(c, labels) for c in o.get("conds") or []]}
                  for o in raw]
        if orders != self.orders:
            self.orders = orders
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "orders", "orders": orders}))

    async def _poll_squads(self) -> None:
        if not self.lua_ok or self.lua_version < LUA_FOR_SQUADS:
            return
        raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "squads"))[0])
        squads = raw if isinstance(raw, list) else []
        if squads != self.squads:
            self.squads = squads
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "squads", "squads": squads}))

    async def _poll_nobles(self) -> None:
        if not self.lua_ok or self.lua_version < LUA_FOR_NOBLES:
            return
        raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "nobles"))[0])
        nobles = raw if isinstance(raw, list) else []
        if nobles != self.nobles:
            self.nobles = nobles
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "nobles", "nobles": nobles}))

    async def poll_blocks(self) -> None:
        wanted = set()
        for s in self.sessions.values():
            wanted |= {z for z in s.zs if 0 <= z < self.state.depth}
        bx, by = self.state.width // BLOCK, self.state.height // BLOCK
        ranges = []
        for z in sorted(wanted):
            first = z not in self.fetched_z
            if (ranges and ranges[-1][1] == z and ranges[-1][2] == first
                    and z - ranges[-1][0] < MAX_POLL_LEVELS):
                start, _, _ = ranges[-1]
                ranges[-1] = (start, z + 1, first)
            else:
                ranges.append((z, z + 1, first))
        buildings_changed = False
        for start, end, first in ranges:
            reply = await self._df(self.df.blocks, 0, bx, 0, by, start, end,
                                   blocks_needed=bx * by * (end - start) + 1, force_reload=first)
            self.fetched_z.update(range(start, end))
            changed, new_buildings = self.state.apply_block_list(reply, visual_levels=range(start, end))
            buildings_changed |= new_buildings
            changed_by_z = {}
            for key in sorted(changed):
                changed_by_z.setdefault(key[2], []).append(key)
            for z, keys in changed_by_z.items():
                payloads = [p for p in map(self.state.block_payload, keys) if p]
                interested = [s for s in self.sessions.values() if z in s.zs]
                if payloads and interested:
                    broadcast([s.conn for s in interested], json.dumps({"type": "blocks", "blocks": payloads}))
                if self.owners.prune(z, self._is_designated):
                    for s in interested:
                        self._send(s, self._owners_msg(s))
        if buildings_changed:
            for s in self.sessions.values():
                self._send(s, self._buildings_msg(s))

    async def poll_units(self) -> None:
        self.current_players()   # pick up role changes from `set-role` without a reconnect
        units = (await self._df(self.df.units)).creature_list
        paused = await self._df(self.df.paused)
        if self.hold_active and not paused:
            # Someone unpaused in DF itself: that overrides every hold.
            log.info("game unpaused in DF; clearing holds")
            self.holders.clear()
            self.hold_active = False
        if paused != self.paused:
            self.paused = paused
            self._broadcast_pause()
        now = asyncio.get_running_loop().time()
        if (not self.lua_ok or self.lua_version < LUA_VERSION) and now >= self._lua_recheck_due:
            # Pick up `install-lua` without a hub restart.
            self._lua_recheck_due = now + 10.0
            was = (self.lua_ok, self.lua_version)
            await self._check_lua()
            if (self.lua_ok, self.lua_version) != was:
                for s in self.sessions.values():
                    self._send(s, self._hello(s))
        if now >= self._date_due:
            self._date_due = now + 2.0
            await self._poll_date()
        if now >= self._orders_due:
            self._orders_due = now + 3.0
            await self._poll_orders()
        if now >= self._squads_due:
            self._squads_due = now + 5.0
            await self._poll_squads()
        if now >= self._nobles_due:
            self._nobles_due = now + 10.0
            await self._poll_nobles()
        if now >= self._reports_due:
            self._reports_due = now + 2.0
            await self._poll_reports()
        visible = self.state.visible_units(units)
        await self._unit_graphics(visible, units)
        by_levels = {}
        for s in list(self.sessions.values()):
            by_levels.setdefault(frozenset(s.zs), []).append(s)
        for zs, sessions in by_levels.items():
            mine = [u for u in visible if u["z"] in zs]
            visible_ids = {u["i"] for u in mine}
            msg = json.dumps({"type": "units", "units": mine, "paused": self.paused})
            for s in sessions:
                s.visible_units = visible_ids.copy()
                if msg != s.last_units:
                    s.last_units = msg
                    self._send_raw(s, msg)

    async def _unit_graphics(self, visible, units) -> None:
        levels = {z for session in self.sessions.values() if session.graphics for z in session.zs}
        visible = [unit for unit in visible if unit["z"] in levels]
        if not visible:
            return
        missing = sorted({unit["race"] for unit in visible if "race" in unit and unit["race"] >= 0}
                         - self._graphics_races)
        if self._graphics_rpc and hasattr(self.df, "creature_raws"):
            for race in missing[:4]:
                try:
                    raws = (await self._df(self.df.creature_raws, race, race + 1)).creature_raws
                    for raw in raws:
                        self.state.creature_defs[raw.index] = (raw.creature_id,
                                                              {caste.index: caste.caste_id for caste in raw.caste})
                    self._graphics_races.add(race)
                except DFHackRPCError as exc:
                    log.warning("creature graphics definitions unavailable: %s", exc)
                    self._graphics_rpc = False
                    break
        source_units = {unit.id: unit for unit in units}
        for unit in visible:
            raw = self.state.creature_defs.get(unit.get("race"))
            if not raw:
                continue
            creature, castes = raw
            source = source_units[unit["i"]]
            equipment = []
            for entry in source.inventory:
                if entry.mode not in (rfr.Worn, rfr.Weapon):
                    continue
                kind = entry.item.type
                token = self.state.item_defs.get((kind.mat_type, kind.mat_index), "")
                if token:
                    equipment.append(token.split(":")[-1].split("/")[-1])
            layers = self.game_tileset.unit_sprites(creature, castes.get(unit["caste"], ""), equipment,
                                                   "BABY" if source.HasField("age") and source.age == 0 else "DEFAULT")
            if layers:
                unit["g"] = layers

    async def run_polling(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_units = 0.0
        while not stop.is_set():
            try:
                if not self.df_online:
                    await self._reconnect()
                if self.sessions:
                    await self.poll_blocks()
                    if loop.time() >= next_units:
                        await self.poll_units()
                        next_units = loop.time() + self.unit_interval
            except (DFHackRPCError, OSError) as exc:
                self._note_offline(exc)
                await self._go_offline()
            try:
                await asyncio.wait_for(stop.wait(), self.poll_interval if self.df_online else self._offline_wait)
            except asyncio.TimeoutError:
                pass

    async def _go_offline(self) -> None:
        if self.df_online:
            self.df_online = False
            broadcast([s.conn for s in self.sessions.values()], json.dumps({"type": "status", "df": "offline"}))
        try:
            await self._df(self.df.close)
        except Exception:  # noqa: BLE001 - closing a dead socket
            pass

    def _note_offline(self, exc: Exception) -> None:
        """Log why the fortress is unavailable, once per distinct reason, so
        waiting for DF to start doesn't print a line every 5 seconds."""
        if isinstance(exc, DFHackRPCError) and exc.code == 1:
            reason = "DFHack is up but no fortress is loaded; waiting for one"
        elif isinstance(exc, (ConnectionRefusedError, TimeoutError)):
            host, port = getattr(self.df, "host", "?"), getattr(self.df, "port", "?")
            reason = f"can't reach DFHack at {host}:{port} (is DF running?); waiting"
        else:
            reason = f"lost DFHack: {exc}; waiting"
        if reason != self._offline_reason:
            self._offline_reason = reason
            log.warning(reason)

    async def _reconnect(self) -> None:
        await self.setup()
        self._offline_reason = ""
        log.info("connected to the fortress")
        for s in self.sessions.values():
            self._send(s, {"type": "status", "df": "online"})
            self._send(s, self._hello(s))

    # -- player side ----------------------------------------------------------

    def _mtime(self) -> float | None:
        try:
            return self.players_path.stat().st_mtime_ns if self.players_path else None
        except OSError:
            return None

    def current_players(self) -> dict[str, str]:
        mtime = self._mtime()
        if mtime is not None and mtime != self._players_mtime:
            self._players_mtime = mtime
            try:
                self.players = load_players(self.players_path)
                self.roles = load_roles(self.players_path)
                log.info("player list reloaded: %s", ", ".join(sorted(self.players.values())))
                self._apply_roles()
            except (OSError, ValueError, KeyError) as exc:
                log.warning("could not reload %s (%s); keeping the old list", self.players_path, exc)
        return self.players

    def _apply_roles(self) -> None:
        """After the token file changes: tell players whose role changed."""
        changed = False
        for s in self.sessions.values():
            role = self.roles.get(s.name, DEFAULT_ROLE)
            if role != s.role:
                s.role = role
                changed = True
                log.info("%s is now %s", s.name, role)
                self._send(s, {"type": "role", "role": role, "perms": sorted(s.perms)})
        if changed:
            self._broadcast_players()

    def process_request(self, conn: ServerConnection, request: Request) -> Response | None:
        url = urlsplit(request.path)
        if url.path == "/ws":
            token = parse_qs(url.query).get("token", [""])[0]
            if token not in self.current_players():
                return conn.respond(HTTPStatus.FORBIDDEN, "unknown token\n")
            return None  # carry on with the websocket handshake
        return self._static(url.path)

    def _static(self, path: str) -> Response:
        if path == "/tileset/manifest.json":
            return _response(HTTPStatus.OK, json.dumps(self.game_tileset.load()).encode(), "application/json")
        if path.startswith("/tileset/page/"):
            page = self.game_tileset.page(path.removeprefix("/tileset/page/"))
            if page is None:
                return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
            try:
                return _response(HTTPStatus.OK, page.read_bytes(), mimetypes.guess_type(page.name)[0])
            except OSError:
                return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (self.static_dir / name).resolve()
        if self.static_dir.resolve() not in target.parents or not target.is_file():
            return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return _response(HTTPStatus.OK, target.read_bytes(), ctype)

    async def handle(self, conn: ServerConnection) -> None:
        token = parse_qs(urlsplit(conn.request.path).query).get("token", [""])[0]
        name = self.current_players().get(token)
        if name is None:  # process_request already refused; belt and braces
            await conn.close(1008, "unknown token")
            return
        sid = next(self._ids)
        session = Session(conn, sid, name, color_for(name),
                          z=self.view["z"], cx=self.view["x"], cy=self.view["y"],
                          role=self.roles.get(name, DEFAULT_ROLE))
        self.sessions[conn] = session
        log.info("%s joined (%d playing)", name, len(self.sessions))
        try:
            self._send(session, self._hello(session))
            self._send_snapshot(session, session.zs)
            self._send(session, self._pause_msg())
            self._broadcast_players()
            await self._send_away(session)
            async for raw in conn:
                await self._on_message(session, raw)
        except ConnectionClosed:
            pass
        finally:
            self.sessions.pop(conn, None)
            log.info("%s left (%d playing)", name, len(self.sessions))
            # Remember when they left, once their last tab closes.
            if self.seen is not None and not any(x.name == name for x in self.sessions.values()):
                self.seen.left(name, _time(), self._report_after, self.date)
            self._broadcast_players()
            if self.holders.pop(sid, None) is not None:
                # A player holding the pause left; don't strand everyone else.
                await self._maybe_resume()
                self._broadcast_pause()

    async def _on_message(self, s: Session, raw) -> None:
        if not isinstance(raw, str) or len(raw) > MAX_INBOUND:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        need = NEEDS.get(kind)
        # Releasing your own pause hold is always allowed, even if your role
        # changed while you held it; otherwise you could strand everyone.
        releasing = kind == "pause" and msg.get("value") is False and s.id in self.holders
        if need and need not in s.perms and not releasing:
            return self._send(s, {"type": "denied", "action": kind,
                                  "error": f"your role ({s.role}) can't {VERB[need]}"})
        try:
            if kind == "view":
                self._on_view(s, msg)
            elif kind == "dig":
                await self._on_dig(s, msg)
            elif kind == "build":
                await self._on_build(s, msg)
            elif kind == "remove":
                await self._on_remove(s, msg)
            elif kind == "order":
                await self._on_order(s, msg)
            elif kind == "cancel_order":
                await self._on_cancel_order(s, msg)
            elif kind == "edit_order":
                await self._on_edit_order(s, msg)
            elif kind == "move_order":
                await self._on_move_order(s, msg)
            elif kind == "squad_candidates":
                await self._on_squad_candidates(s)
            elif kind in ("squad_add", "squad_remove", "rename_squad"):
                await self._on_squad_change(s, kind, msg)
            elif kind == "noble_candidates":
                await self._on_noble_candidates(s)
            elif kind in ("appoint_noble", "vacate_noble"):
                await self._on_noble_change(s, kind, msg)
            elif kind == "inspect":
                await self._on_inspect(s, msg)
            elif kind == "chat":
                self._on_chat(s, msg)
            elif kind == "ping":
                self._on_ping(s, msg)
            elif kind == "pause" and isinstance(msg.get("value"), bool):
                await self._on_pause(s, msg["value"])
        except (DFHackRPCError, OSError) as exc:
            self._note_offline(exc)
            await self._go_offline()

    def _on_view(self, s: Session, msg: dict) -> None:
        z, cx, cy = msg.get("z"), msg.get("cx"), msg.get("cy")
        if not all(_is_int(v) for v in (z, cx, cy)):
            return
        z = max(0, min(self.state.depth - 1, z)) if self.state.depth else z
        old = s.zs
        s.z, s.cx, s.cy = z, cx, cy
        graphics = msg.get("graphics") is True
        if graphics != s.graphics:
            s.graphics = graphics
            s.last_units = ""
        new_levels = s.zs - old
        if new_levels:
            s.last_units = ""  # force a units refresh for the new levels
            self._send_snapshot(s, new_levels)
            self._send(s, self._owners_msg(s))
        self._broadcast_players()

    # -- digging ----------------------------------------------------------------

    async def _on_dig(self, s: Session, msg: dict) -> None:
        kind = msg.get("kind")
        coords = [msg.get(k) for k in ("x1", "y1", "x2", "y2", "z")]
        priority = msg.get("priority", 4)

        def reply(ok, applied=0, skipped=0, error=None):
            out = {"type": "dig_result", "ok": ok, "kind": kind, "applied": applied, "skipped": skipped,
                   "priority": priority}
            if error:
                out["error"] = error
            self._send(s, out)

        if kind not in DIG_KINDS or not all(_is_int(v) for v in coords):
            return reply(False, error="malformed order")
        if not _is_int(priority) or not 1 <= priority <= 7:
            return reply(False, error="priority must be 1 to 7")
        can_prioritise = self.lua_ok and self.lua_version >= LUA_FOR_PRIORITY
        if priority != 4 and kind != "erase" and not can_prioritise:
            return reply(False, error="priorities other than 4 need the host to update the in-game module (hub.py install-lua)")
        if not self.df_online:
            return reply(False, error="the fortress isn't loaded")
        x1, y1, x2, y2, z = coords
        if not 0 <= z < self.state.depth:
            return reply(False, error="that level is outside the map")
        rect = rect_tiles(x1, y1, x2, y2, self.state.width, self.state.height)
        if rect is None:
            return reply(False, error="that area is outside the map")
        ax, ay, bx, by = rect
        area = (bx - ax + 1) * (by - ay + 1)
        if area > MAX_DIG_TILES:
            return reply(False, error=f"too big: {area} tiles, the limit is {MAX_DIG_TILES}")
        if not s.limiter.take():
            return reply(False, error="slow down a little")

        tiles, skipped = [], 0
        for x in range(ax, bx + 1):
            for y in range(ay, by + 1):
                info = self.state.tile_info(x, y, z)
                shape, material, hidden = info if info else (None, None, False)
                if eligible(kind, shape, material, hidden):
                    tiles.append((x, y, z))
                else:
                    skipped += 1
        priority_note = None
        if tiles:
            spans = designation_spans(tiles)
            await self._df(self.df.dig, DIG_KINDS[kind], tiles)
            if kind != "erase" and can_prioritise:
                try:
                    await self._df(self.df.run_lua, "rpc.coop", "set_priority_spans",
                                   json.dumps(spans, separators=(",", ":")), str(z), str(priority))
                except DFHackRPCError as exc:
                    log.warning("%s: setting priority %d failed: %s", s.name, priority, exc)
                    priority_note = "designated, but the priority couldn't be set"
            self.owners.apply(kind, tiles, s.name, priority)
            self.action_log.append({"type": "dig", "t": round(_time(), 1), "player": s.name, "kind": kind,
                                    "rect": [ax, ay, bx, by], "z": z, "applied": len(tiles),
                                    "priority": priority, "spans": spans})
            for other in self.sessions.values():
                if z in other.zs:
                    self._send(other, self._owners_msg(other))
        log.info("%s: %s %dx%d at z%d, priority %d, %d tiles (%d skipped)",
                 s.name, kind, bx - ax + 1, by - ay + 1, z, priority, len(tiles), skipped)
        reply(True, len(tiles), skipped, priority_note)

    # -- building -----------------------------------------------------------------

    async def _on_build(self, s: Session, msg: dict) -> None:
        item = BY_ID.get(msg.get("item")) if isinstance(msg.get("item"), str) else None

        def reply(ok, placed=0, problems=(), error=None):
            out = {"type": "build_result", "ok": ok, "item": item.id if item else None,
                   "label": item.label if item else None, "placed": placed, "problems": list(problems)}
            if error:
                out["error"] = error
            self._send(s, out)

        if item is None:
            return reply(False, error="unknown item")
        x1, y1, z = msg.get("x1"), msg.get("y1"), msg.get("z")
        x2, y2 = (msg.get("x2"), msg.get("y2")) if item.area else (x1, y1)
        if not all(_is_int(v) for v in (x1, y1, x2, y2, z)):
            return reply(False, error="malformed order")
        if not self.df_online:
            return reply(False, error="the fortress isn't loaded")
        if not self.lua_ok:
            return reply(False, error="building isn't set up on the host (hub.py install-lua)")
        if not item.available:
            return reply(False, error=f"{item.label} can't be placed from here yet (quickfort doesn't support it)")
        if not re.fullmatch(r"[A-Za-z]{1,2}", item.key) and self.lua_version < LUA_FOR_LONG_KEYS:
            return reply(False, error="the host needs to update the in-game module (hub.py install-lua)")
        if not 0 <= z < self.state.depth:
            return reply(False, error="that level is outside the map")
        rect = rect_tiles(x1, y1, x2, y2, self.state.width, self.state.height)
        if rect is None:
            return reply(False, error="that spot is outside the map")
        ax, ay, bx, by = rect
        w, h = bx - ax + 1, by - ay + 1
        if item.area and max(w, h) > MAX_AREA_SIDE:
            return reply(False, error=f"too big: {w}x{h}, the limit is {MAX_AREA_SIDE} on a side")
        if not s.limiter.take():
            return reply(False, error="slow down a little")

        text = item.text(w, h)
        try:
            raw = (await self._df(self.df.run_lua, "rpc.coop", "apply", item.mode,
                                  str(ax), str(ay), str(z), text, "0"))[0]
        except DFHackRPCError as exc:
            log.warning("%s: build %s failed in DF: %s", s.name, item.id, exc)
            return reply(False, error="DF refused that")
        placed, problems = summarize(item.mode, json.loads(raw))
        self.action_log.append({"type": "build", "t": round(_time(), 1), "player": s.name, "item": item.id,
                                "rect": [ax, ay, bx, by], "z": z, "placed": placed})
        log.info("%s: %s %s at %d,%d,%d: %d placed%s", s.name, item.label, text, ax, ay, z, placed,
                 f" ({'; '.join(problems)})" if problems else "")
        reply(True, placed, problems)

    async def _on_remove(self, s: Session, msg: dict) -> None:
        def reply(ok, **extra):
            self._send(s, {"type": "remove_result", "ok": ok, **extra})

        x, y, z, bid = msg.get("x"), msg.get("y"), msg.get("z"), msg.get("id")
        if not all(_is_int(v) for v in (x, y, z)) or not (bid is None or _is_int(bid)):
            return reply(False, error="malformed order")
        if not self.df_online:
            return reply(False, error="the fortress isn't loaded")
        if not self.lua_ok:
            return reply(False, error="building isn't set up on the host (hub.py install-lua)")
        if self.lua_version < LUA_FOR_REMOVE:
            return reply(False, error="the host needs to update the in-game module (hub.py install-lua)")
        if not (0 <= x < self.state.width and 0 <= y < self.state.height and 0 <= z < self.state.depth):
            return reply(False, error="that spot is outside the map")
        if self.state.is_hidden(x, y, z):
            # Answering here would tell players what's in unexplored rock.
            return reply(True, outcome="nothing")
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        try:
            raw = (await self._df(self.df.run_lua, "rpc.coop", "remove", str(x), str(y), str(z),
                                  "" if bid is None else str(bid)))[0]
        except DFHackRPCError as exc:
            log.warning("%s: remove at %d,%d,%d failed in DF: %s", s.name, x, y, z, exc)
            return reply(False, error="DF refused that")
        r = json.loads(raw)
        if not r.get("found"):
            return reply(True, outcome="nothing")
        known = self.state.buildings.get(r["id"])
        what = known["name"] if known else r.get("type", "building")
        self.action_log.append({"type": "remove", "t": round(_time(), 1), "player": s.name, "id": r["id"],
                                "what": what, "pos": [x, y, z], "outcome": r["outcome"]})
        log.info("%s: remove %s (%s) at %d,%d,%d: %s", s.name, what, r["id"], x, y, z, r["outcome"])
        reply(True, what=what, outcome=r["outcome"])

    # -- work orders ---------------------------------------------------------------

    def _orders_ready(self, minimum: int = LUA_FOR_ORDERS) -> str | None:
        if not self.df_online:
            return "the fortress isn't loaded"
        if not self.lua_ok or self.lua_version < minimum:
            return "the host needs to update the in-game module (hub.py install-lua)"
        return None

    async def _on_order(self, s: Session, msg: dict) -> None:
        o = ORDERS_BY_ID.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
        amount, material, freq = msg.get("amount"), msg.get("material"), msg.get("freq", "OneTime")

        def reply(ok, error=None):
            out = {"type": "order_result", "ok": ok, "label": o.label if o else None, "amount": amount}
            if error:
                out["error"] = error
            self._send(s, out)

        if o is None:
            return reply(False, "unknown order")
        if not _is_int(amount) or not 1 <= amount <= MAX_ORDER_AMOUNT:
            return reply(False, f"the amount must be 1 to {MAX_ORDER_AMOUNT}")
        if material is None:
            material = o.materials[0] if o.materials else ""
        if material not in (o.materials or ("",)):
            return reply(False, "that material isn't an option for this order")
        if not isinstance(freq, str) or freq not in ORDER_FREQUENCIES:
            return reply(False, "that isn't a repeat option")
        repeats = freq != "OneTime"
        if (problem := self._orders_ready(LUA_FOR_ORDER_EDITS if repeats else LUA_FOR_ORDERS)):
            return reply(False, problem)
        if not s.limiter.take():
            return reply(False, "slow down a little")
        args = [o.job, o.reaction, material, str(amount)] + ([freq] if repeats else [])
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "order", *args))[0])
        except DFHackRPCError as exc:
            log.warning("%s: order %s failed in DF: %s", s.name, o.id, exc)
            return reply(False, "DF refused that")
        if not r.get("ok"):
            log.warning("%s: workorder refused %s: %s", s.name, o.id, r.get("output", "").strip())
            return reply(False, "DF refused that order")
        self.action_log.append({"type": "order", "t": round(_time(), 1), "player": s.name, "order": o.id,
                                "material": material, "amount": amount, "freq": freq})
        log.info("%s: ordered %d %s%s (%s)", s.name, amount, o.label, f" ({material})" if material else "", freq)
        self._orders_due = 0.0
        reply(True)

    async def _on_cancel_order(self, s: Session, msg: dict) -> None:
        oid = msg.get("order")

        def reply(ok, label=None, error=None):
            out = {"type": "cancel_result", "ok": ok, "label": label}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(oid):
            return reply(False, error="malformed order")
        if (problem := self._orders_ready()):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        label = next((o["label"] for o in self.orders if o["id"] == oid), "that order")
        r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "cancel_order", str(oid)))[0])
        if r.get("busy"):
            return reply(False, label, "a dwarf is working on it right now; try again shortly")
        if not r.get("ok"):
            return reply(False, label, "that order is already gone")
        self.action_log.append({"type": "cancel_order", "t": round(_time(), 1), "player": s.name,
                                "order_id": oid, "label": label})
        log.info("%s: cancelled order %d (%s)", s.name, oid, label)
        self._orders_due = 0.0
        reply(True, label)

    async def _on_edit_order(self, s: Session, msg: dict) -> None:
        oid, amount, freq = msg.get("order"), msg.get("amount"), msg.get("freq")

        def reply(ok, label=None, error=None):
            out = {"type": "edit_result", "ok": ok, "label": label}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(oid):
            return reply(False, error="malformed order")
        if amount is None and freq is None:
            return reply(False, error="nothing to change")
        if amount is not None and (not _is_int(amount) or not 1 <= amount <= MAX_ORDER_AMOUNT):
            return reply(False, error=f"the amount must be 1 to {MAX_ORDER_AMOUNT}")
        if freq is not None and (not isinstance(freq, str) or freq not in ORDER_FREQUENCIES):
            return reply(False, error="that isn't a repeat option")
        if (problem := self._orders_ready(LUA_FOR_ORDER_EDITS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        label = next((o["label"] for o in self.orders if o["id"] == oid), "that order")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "set_order", str(oid),
                                           "" if amount is None else str(amount), freq or ""))[0])
        except DFHackRPCError as exc:
            log.warning("%s: editing order %d failed in DF: %s", s.name, oid, exc)
            return reply(False, label, "DF refused that")
        if not r.get("ok"):
            return reply(False, label, "that order is already gone")
        entry = {"type": "edit_order", "t": round(_time(), 1), "player": s.name, "order_id": oid, "label": label}
        if amount is not None:
            entry["amount"] = amount
        if freq is not None:
            entry["freq"] = freq
        self.action_log.append(entry)
        log.info("%s: changed order %d (%s): amount=%s freq=%s", s.name, oid, label, amount, freq)
        self._orders_due = 0.0
        reply(True, label)

    async def _on_move_order(self, s: Session, msg: dict) -> None:
        oid, to = msg.get("order"), msg.get("to")

        def reply(ok, error=None):
            out = {"type": "move_result", "ok": ok}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(oid) or not isinstance(to, str) or to not in ORDER_MOVES:
            return reply(False, "malformed order")
        if (problem := self._orders_ready(LUA_FOR_ORDER_EDITS)):
            return reply(False, problem)
        if not s.limiter.take():
            return reply(False, "slow down a little")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "move_order", str(oid), to))[0])
        except DFHackRPCError as exc:
            log.warning("%s: moving order %d failed in DF: %s", s.name, oid, exc)
            return reply(False, "DF refused that")
        if not r.get("ok"):
            return reply(False, "that order is already gone")
        self.action_log.append({"type": "move_order", "t": round(_time(), 1), "player": s.name,
                                "order_id": oid, "to": to})
        log.info("%s: moved order %d %s", s.name, oid, to)
        self._orders_due = 0.0
        reply(True)

    # -- squads ---------------------------------------------------------------------

    async def _on_squad_candidates(self, s: Session) -> None:
        def reply(ok, units=None, error=None):
            out = {"type": "squad_candidates", "ok": ok}
            if units is not None:
                out["units"] = units
            if error:
                out["error"] = error
            self._send(s, out)

        if (problem := self._orders_ready(LUA_FOR_SQUADS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        reply(True, json.loads((await self._df(self.df.run_lua, "rpc.coop", "squad_candidates"))[0]))

    async def _on_squad_change(self, s: Session, kind: str, msg: dict) -> None:
        squad = msg.get("squad")

        def reply(ok, name=None, error=None):
            out = {"type": "squad_result", "ok": ok, "action": kind}
            if name:
                out["name"] = name
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(squad):
            return reply(False, error="malformed request")
        if kind == "rename_squad":
            text = msg.get("name")
            if not isinstance(text, str):
                return reply(False, error="malformed request")
            # One printable line, as chat; DF stores CP437, so anything it can't
            # hold comes back as a substitute rather than an error.
            text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
            if len(text) > MAX_SQUAD_NAME:
                return reply(False, error=f"keep the name to {MAX_SQUAD_NAME} characters")
            arg = text
        else:
            if not _is_int(msg.get("unit")):
                return reply(False, error="malformed request")
            arg = str(msg["unit"])
        if (problem := self._orders_ready(LUA_FOR_SQUADS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", kind, str(squad), arg))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s failed in DF: %s", s.name, kind, exc)
            return reply(False, error="DF refused that")
        if not r.get("ok"):
            return reply(False, error=r.get("why") or "DF refused that")
        self.action_log.append({"type": kind, "t": round(_time(), 1), "player": s.name, "squad": squad,
                                "value": arg, "name": r.get("name", "")})
        log.info("%s: %s squad %d: %s", s.name, kind, squad, r.get("name", ""))
        self._squads_due = 0.0
        reply(True, r.get("name"))

    # -- nobles ---------------------------------------------------------------------

    async def _on_noble_candidates(self, s: Session) -> None:
        def reply(ok, units=None, error=None):
            out = {"type": "noble_candidates", "ok": ok}
            if units is not None:
                out["units"] = units
            if error:
                out["error"] = error
            self._send(s, out)

        if (problem := self._orders_ready(LUA_FOR_NOBLES)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        reply(True, json.loads((await self._df(self.df.run_lua, "rpc.coop", "noble_candidates"))[0]))

    async def _on_noble_change(self, s: Session, kind: str, msg: dict) -> None:
        post, unit = msg.get("post"), msg.get("unit")
        title = next((n["title"] for n in self.nobles if n["id"] == post), None) if _is_int(post) else None

        def reply(ok, name=None, error=None):
            out = {"type": "noble_result", "ok": ok, "action": kind, "title": title}
            if name:
                out["name"] = name
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(post) or (kind == "appoint_noble" and not _is_int(unit)):
            return reply(False, error="malformed request")
        if (problem := self._orders_ready(LUA_FOR_NOBLES)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        args = [str(post)] + ([str(unit)] if kind == "appoint_noble" else [])
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", kind, *args))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s failed in DF: %s", s.name, kind, exc)
            return reply(False, error="DF refused that")
        if not r.get("ok"):
            return reply(False, error=r.get("why") or "DF refused that")
        self.action_log.append({"type": kind, "t": round(_time(), 1), "player": s.name, "post": post,
                                "title": title, "name": r.get("name", "")})
        log.info("%s: %s %s: %s", s.name, kind, title, r.get("name", ""))
        self._nobles_due = 0.0
        reply(True, r.get("name"))

    # -- chat and pings --------------------------------------------------------------

    def _post(self, entry: dict) -> None:
        self.chat = (self.chat + [entry])[-KEEP_CHAT:]
        broadcast([x.conn for x in self.sessions.values()], json.dumps({"type": "chat", "entry": entry}))

    def _on_chat(self, s: Session, msg: dict) -> None:
        text = msg.get("text")
        if not isinstance(text, str):
            return
        # Printable text on one line; the client renders it as text, never HTML.
        # Line breaks and tabs become spaces; other control characters go.
        text = "".join(ch if ch.isprintable() else (" " if ch.isspace() else "") for ch in text)
        text = " ".join(text.split())[:MAX_CHAT]
        if not text:
            return
        if not s.chat_limiter.take():
            return self._send(s, {"type": "chat_refused", "error": "slow down a little"})
        self._post({"kind": "chat", "from": s.name, "color": s.color, "text": text, "date": self.date})

    def _on_ping(self, s: Session, msg: dict) -> None:
        x, y, z = msg.get("x"), msg.get("y"), msg.get("z")
        if not all(_is_int(v) for v in (x, y, z)):
            return
        if not (0 <= x < self.state.width and 0 <= y < self.state.height and 0 <= z < self.state.depth):
            return
        if not s.chat_limiter.take():
            return self._send(s, {"type": "chat_refused", "error": "slow down a little"})
        broadcast([x_.conn for x_ in self.sessions.values()],
                  json.dumps({"type": "ping", "from": s.name, "color": s.color, "x": x, "y": y, "z": z}))
        self._post({"kind": "ping", "from": s.name, "color": s.color, "x": x, "y": y, "z": z, "date": self.date})

    # -- unit inspector ------------------------------------------------------------

    async def _on_inspect(self, s: Session, msg: dict) -> None:
        uid = msg.get("unit")

        def reply(ok, unit=None, error=None):
            out = {"type": "unit_info", "ok": ok}
            if unit:
                out["unit"] = unit
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(uid):
            return reply(False, error="malformed request")
        if not self.df_online or not self.lua_ok or self.lua_version < LUA_FOR_INFO:
            return reply(False, error="the host needs to update the in-game module (hub.py install-lua)")
        if uid not in s.visible_units:
            # Only units this player is being shown: no looking up hidden
            # ambushers or units elsewhere by guessing ids.
            return reply(False, error="you can't see that creature")
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        u = json.loads((await self._df(self.df.run_lua, "rpc.coop", "unit", str(uid)))[0])
        if not u.get("found"):
            return reply(False, error="that creature is gone")
        stress = u.get("stress", -1)
        reply(True, {"id": u["id"], "name": u["name"], "profession": u["profession"],
                     "job": u["job"] or "No job", "citizen": bool(u.get("citizen")),
                     "mood": STRESS_LABELS[stress] if 0 <= stress < len(STRESS_LABELS) else "",
                     "age": u.get("age", -1), "squad": u.get("squad", ""),
                     "needs": [humanize(n) for n in u.get("needs") or []]})

    def _is_designated(self, x: int, y: int, z: int) -> bool | None:
        dig = self.state.dig_at(x, y, z)
        return None if dig is None else dig != 0

    def _owners_msg(self, s: Session) -> dict:
        # No filtering against the map here: a fresh order isn't in the map
        # until the next poll. Finished designations are pruned when their
        # block changes (including the first fetch of every level).
        cells = [[x, y, z, n, color_for(n), p] for x, y, z, n, p in self.owners.on_levels(s.zs)]
        return {"type": "owners", "owners": cells}

    # -- pausing ----------------------------------------------------------------

    async def _on_pause(self, s: Session, want_pause: bool) -> None:
        if want_pause:
            self.holders[s.id] = s.name
            self.hold_active = True
            if not self.paused:
                await self._df(self.df.set_paused, True)
                self.paused = True
            log.info("%s paused the game", s.name)
        else:
            self.holders.pop(s.id, None)
            log.info("%s is ready", s.name)
            await self._maybe_resume()
        self._broadcast_pause()

    async def _maybe_resume(self) -> None:
        if self.paused and not self.holders and self.df_online:
            await self._df(self.df.set_paused, False)
            self.paused = False
            self.hold_active = False
            log.info("everyone is ready; game resumed")

    def _pause_msg(self) -> dict:
        return {"type": "pause", "paused": self.paused, "holders": sorted(set(self.holders.values()))}

    def _broadcast_pause(self) -> None:
        broadcast([s.conn for s in self.sessions.values()], json.dumps(self._pause_msg()))

    def _hello(self, s: Session) -> dict:
        return {"type": "hello", "you": s.public(),
                "map": {"w": self.state.width, "h": self.state.height, "d": self.state.depth},
                "tiles": self.tiles, "view": {"x": s.cx, "y": s.cy, "z": s.z},
                "paused": self.paused, "df": "online" if self.df_online else "offline",
                "catalog": {"pages": PAGES, "page_labels": PAGE_LABELS, "items": [i.public() for i in ITEMS]},
                "can_build": self.lua_ok, "date": self.date,
                "order_catalog": {"groups": ORDER_GROUPS, "orders": [o.public() for o in ORDERS],
                                  "freqs": [{"id": k, "label": v} for k, v in ORDER_FREQUENCIES.items()]},
                "can_order": self.lua_ok and self.lua_version >= LUA_FOR_ORDERS, "orders": self.orders,
                "can_edit_orders": self.lua_ok and self.lua_version >= LUA_FOR_ORDER_EDITS,
                "can_squads": self.lua_ok and self.lua_version >= LUA_FOR_SQUADS, "squads": self.squads,
                "can_nobles": self.lua_ok and self.lua_version >= LUA_FOR_NOBLES, "nobles": self.nobles,
                "can_inspect": self.lua_ok and self.lua_version >= LUA_FOR_INFO,
                "reports": self.reports[-50:], "chat": self.chat}

    def _send_snapshot(self, s: Session, levels) -> None:
        blocks = [p for z in sorted(levels) for p in self.state.blocks_on(z)]
        if blocks:
            self._send(s, {"type": "blocks", "blocks": blocks})
        self._send(s, self._buildings_msg(s))
        self._send(s, self._owners_msg(s))

    def _buildings_msg(self, s: Session) -> dict:
        return {"type": "buildings", "buildings": self.state.buildings_on(s.zs)}

    def _broadcast_players(self) -> None:
        msg = json.dumps({"type": "players", "players": [s.public() for s in self.sessions.values()]})
        broadcast([s.conn for s in self.sessions.values()], msg)

    def _send(self, s: Session, msg: dict) -> None:
        self._send_raw(s, json.dumps(msg))

    def _send_raw(self, s: Session, raw: str) -> None:
        broadcast([s.conn], raw)  # never blocks on a slow player

    # -- running ----------------------------------------------------------------

    async def serve(self, host: str, port: int, stop: asyncio.Event, ready=None) -> None:
        # Start serving even if DF isn't ready: players get "waiting for the
        # fortress" and the polling loop connects once a map is loaded.
        try:
            await self.setup()
        except (DFHackRPCError, OSError) as exc:
            self._note_offline(exc)
            await self._go_offline()
        async with serve(self.handle, host, port, process_request=self.process_request,
                         max_size=MAX_INBOUND * 4) as server:
            if ready is not None:
                ready(server)
            await self.run_polling(stop)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def fort_key(info) -> str:
    """A name for this fortress's files that survives saving. Not DF's save
    name: after an autosave DF reports "autosave 1" for the same fort, which
    would split the action log. The world's name and where the fort sits in
    it don't change, and two forts in one world differ."""
    world = getattr(info, "world_name_english", "") or getattr(info, "world_name", "") or "world"
    return f"{_safe_name(world)}-{info.block_pos_x}x{info.block_pos_y}"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "unknown"


def _time() -> float:
    return time.time()


def _response(status: HTTPStatus, body: bytes, ctype: str) -> Response:
    headers = Headers({"Content-Type": ctype, "Content-Length": str(len(body)),
                       "Cache-Control": "no-cache"})
    return Response(status.value, status.phrase, headers, body)


# -- CLI ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hub.py", description="Multiplayer Dwarf Fortress hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the hub")
    sp.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; your Tailscale/VPN address to let friends in")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--df-host", default="127.0.0.1")
    sp.add_argument("--df-port", type=int, default=5000)
    ap_add = sub.add_parser("add-player", help="issue a join token")
    ap_add.add_argument("name")
    ap_add.add_argument("--role", default=DEFAULT_ROLE, choices=list(ROLES),
                        help="what they may do (default: %(default)s)")
    ap_role = sub.add_parser("set-role", help="change a player's role; a running hub applies it within seconds")
    ap_role.add_argument("name")
    ap_role.add_argument("role", choices=list(ROLES))
    ap_add.add_argument("--host", default="127.0.0.1", help="address to put in the printed link")
    ap_add.add_argument("--port", type=int, default=8765)
    sub.add_parser("players", help="list players with tokens")
    sub.add_parser("install-lua", help="install the in-game module (needed for building)")
    args = ap.parse_args(argv)

    if args.cmd == "add-player":
        token = add_player(args.name, role=args.role)
        print(f"{args.name} ({args.role}): http://{args.host}:{args.port}/?token={token}")
        return 0
    if args.cmd == "set-role":
        set_role(args.name, args.role)
        print(f"{args.name} is now {args.role}: {', '.join(sorted(ROLES[args.role]))}")
        return 0
    if args.cmd == "install-lua":
        target = install_lua()
        print(f"installed {target}; DF picks it up without a restart")
        return 0
    if args.cmd == "players":
        roles = load_roles()
        for token, name in load_players().items():
            print(f"{name}\t{token}\t{roles.get(name, DEFAULT_ROLE)}")
        return 0

    logging.basicConfig(level=logging.INFO, format="[hub %(asctime)s] %(message)s", datefmt="%H:%M:%S")
    # websockets logs every plain page load as a "rejected connection".
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    players = load_players()
    if not players:
        print("No players yet. Run: python3 hub.py add-player YOURNAME", file=sys.stderr)
        return 2
    hub = Hub(DFHackRPC(args.df_host, args.df_port, timeout=30), players, roles=load_roles(),
              players_path=CONFIG_PATH)

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in ("SIGINT", "SIGTERM"):
            import signal
            loop.add_signal_handler(getattr(signal, sig), stop.set)
        await hub.serve(args.host, args.port, stop,
                        ready=lambda _: log.info("listening on http://%s:%d/", args.host, args.port))

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
