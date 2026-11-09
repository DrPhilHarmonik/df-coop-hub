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
                                            active|validated|waiting, conds: [str], cond_info: [{key, cmp?, val?, what?}]
                                            (same order as conds), max_workshops: 0 = no limit
    order_result {ok, label, amount, error?}
    cancel_result {ok, label?, error?}
    edit_result {ok, label?, error?}
    move_result {ok, error?}
    squads     {squads: [{id, name, alias, slots, routine, orders: [str], station?: {x, y, z}, kill,
                members: [{slot, leader, unit, name, away}]}]}
    squad_candidates {ok, units?: [{id, name}], error?}   adult citizens in no squad
    squad_result {ok, action, name?, error?}
    squad_targets {ok, units?: [{id, name, x, y, z}], error?}   enemies this player can see
    nobles     {nobles: [{id, code, title, holder, locked, duties: [str], needs: {..}}]}
                                            in order of rank; locked says why a
                                            post can't be changed here, or is ""
    noble_candidates {ok, units?: [{id, name}], error?}
    citizens   {ok, citizens?: [{id, name, profession, job, mood, stress, squad, x?, y?, z?}], error?}
                                           no location for those on unseen tiles
    stocks     {ok, groups?: [{name, total, rows: [{name, count}]}], age?, error?}
                                            age: seconds since DF was counted
    work_details {ok, details?: [{i, name, mode, no_everybody, labors: [str], members: [ids]}],
                  people?: [{id, name}], error?}   members: living only; people: adults
    work_detail_result {ok, error?}
    noble_result {ok, action, title?, name?, error?}
    place_info {ok, place?: {kind: stockpile|zone|workshop, id, name, tiles, z, x, y, ...}, error?}
                                            stockpile: number, categories, barrels, bins,
                                            wheelbarrows; zone: number, zone, active, ownable,
                                            owner; workshop: workshop (its kind), custom, built,
                                            jobs [{id, name, order, worker, suspended, repeat,
                                            now, building}], workers [{id, name}] (one at most),
                                            general_orders, orders_here; farm: underground,
                                            fertilize, autofarm (DFHack's, picks crops itself),
                                            season (0 spring), crops [{id, name}] x4,
                                            options [[{id, name, seeds}]] x4
    place_changed {id}        someone changed it; ask again if it's on screen
    zone_candidates {ok, units?: [{id, name}], error?}
    place_result {ok, action, name?, error?}
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
    visibility {hidden}       their tab went to the background or came back
    order      {id, amount, material?, freq?}   id: a catalog.py ORDERS id;
                                           freq: a catalog.ORDER_FREQUENCIES key
    cancel_order {order}      a manager order id (refused while being worked on)
    edit_order {order, amount?, freq?, max_workshops?}   a new amount restarts the count;
                                           0 max_workshops = no limit
    edit_condition {order, key, cmp, val}   key from cond_info; cmp a catalog.CONDITION_COMPARES key
    remove_condition {order, key}
    add_condition {order, item, cmp, val} or {order, after, when}   -> condition_result {ok, label, error?}
    move_order {order, to}    to: up down top bottom
    squad_candidates {}       who could join a squad
    squad_add  {squad, unit}  into the first open slot
    squad_remove {squad, unit}   not the leader, not while away
    rename_squad {squad, name}   empty restores the generated name
    squad_station {squad, x, y, z}   replaces its station and kill orders
    squad_targets {}          enemies a kill order could name
    squad_kill {squad, units: [ids]}   enemies this player can see; replaces as above
    squad_clear {squad}       cancels its station and kill orders
    noble_candidates {}       who could hold a post
    citizens   {}             everyone in the fort; the client asks while its panel is open
    stocks     {}             what the fort has; likewise, and counted at most once a minute
    work_details {}           likewise
    work_detail_member {detail, name, unit, on}   detail: its i; name guards a stale list
    work_detail_mode {detail, name, mode}         mode: a catalog.WORK_DETAIL_MODES key
    appoint_noble {post, unit}   post: a nobles id; replaces any current holder
    vacate_noble {post}
    inspect_place {id, x, y, z}   a stockpile or zone on a visible tile
    stockpile_category {id, category, on}   category: a catalog.STOCK_CATEGORIES key
    stockpile_containers {id, barrels, bins, wheelbarrows}
    rename_place {id, name}   a stockpile or zone; empty clears it
    zone_candidates {}        who could own a room
    zone_owner {id, unit?}    no unit frees the room
    zone_active {id, on}
    workshop_job {id, job, action}   action: a catalog.WORKSHOP_JOB_ACTIONS entry
    workshop_worker {id, unit?}      no unit lets anyone work there
    farm_crop  {id, season, plant}   season 0-3 (spring first); plant from options, -1 fallow
    farm_fertilize {id, on}
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
from catalog import (BY_ID, CONDITION_COMPARES, CONDITION_ITEMS, CONDITION_WHENS, MAX_CONDITION_VALUE, MAX_WORKSHOPS,
                     PAGE_LABELS, PAGES, ITEMS, MAX_AREA_SIDE, MAX_ORDER_AMOUNT, ORDER_FREQUENCIES,
                     WORK_DETAIL_MODES, WORKSHOP_JOB_ACTIONS, condition_what, labor_label, stock_rows,
                     STOCK_CATEGORIES,
                     ORDER_GROUPS, ORDER_MOVES, ORDERS, ORDERS_BY_ID, STRESS_LABELS, condition_text,
                     game_date, humanize, order_label,
                     summarize)
from dfhack_rpc import DFHackRPC, DFHackRPCError, rfr
from game_tiles import GameTileset
from mapstate import BLOCK, MapState
from rpcstats import RpcStats, call_name
from tiles import tiletype_table

log = logging.getLogger("df-coop")

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
CONFIG_PATH = Path(os.environ.get("DF_COOP_CONFIG", Path.home() / ".config/df-coop/players.json"))
DATA_DIR = Path(os.environ.get("DF_COOP_DATA", Path.home() / ".local/share/df-coop"))
DF_DIR = Path(os.environ.get("DF_DIR", Path.home() / ".local/share/Steam/steamapps/common/Dwarf Fortress"))
LUA_SOURCE = HERE / "lua" / "rpc" / "coop.lua"
LUA_VERSION = 19
LUA_FOR_REMOVE = 2     # the oldest module each feature works with
LUA_FOR_ORDERS = 3
LUA_FOR_INFO = 4       # announcements and the unit inspector
LUA_FOR_LONG_KEYS = 5  # quickfort keys like "~h" (hive) or "trackNSEW"
LUA_FOR_PRIORITY = 7
LUA_FOR_ORDER_EDITS = 8  # amounts, repeats and queue position
LUA_FOR_SQUADS = 9
LUA_FOR_NOBLES = 10
LUA_FOR_PLACES = 11
LUA_FOR_SQUAD_ORDERS = 12
LUA_FOR_CONDITIONS = 13
LUA_FOR_CITIZENS = 14
LUA_FOR_WORK_DETAILS = 15
LUA_FOR_WORKSHOPS = 16
LUA_FOR_STOCKS = 17
LUA_FOR_FARMS = 18
LUA_FOR_UNITS = 19   # creatures through coop.lua: about a quarter of RFR's cost
MAX_KILL_TARGETS = 50
MAX_SQUAD_NAME = 40
KEEP_REPORTS = 100
KEEP_CHAT = 50
MAX_CHAT = 300
MAX_POLL_LEVELS = 2
# Seconds between polls. DF stops for every call and a call's size barely changes
# its cost (a sixth of a level costs about the same as all of it), so these keep
# the number of calls down. Changes players make are read straight away instead.
POLL_DATE = 10.0
POLL_REPORTS = 3.0
POLL_ORDERS = 5.0
POLL_SQUADS = 10.0
POLL_NOBLES = 30.0
POLL_TICK = 0.1         # how often the loop looks at what's due; costs DF nothing
POLL_SOON_GAP = 0.25    # the shortest gap between map reads, however busy players are
HIDDEN_SLOWDOWN = 4     # when every player's tab is in the background
CITIZENS_CACHE = 3.0    # players share one read of the citizens list this long
STOCKS_CACHE = 60.0     # and of stocks: one count holds DF for about half a second
MAX_CONDITION_KEY = 200
MAX_DETAIL_NAME = 100


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
    hidden: bool = False  # their browser tab is in the background
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
                 poll_interval: float = 1.0, unit_interval: float = 0.5,
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
        self._citizens: list[dict] = []
        self._citizens_read = float("-inf")
        self._work_details: dict = {}
        self._work_details_read = float("-inf")
        self._stocks: list[dict] = []
        self._stocks_read = float("-inf")
        self._stocks_lock = asyncio.Lock()
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
        self._blocks_due = 0.0
        self._last_blocks = 0.0
        self._units_due = 0.0
        self._paused_due = 0.0
        self._ids = itertools.count(1)
        self._df_lock = asyncio.Lock()
        self.rpc_stats = RpcStats()
        self._offline_reason = ""
        self._offline_wait = 5.0  # seconds between reconnect attempts

    # -- DFHack side ----------------------------------------------------------

    async def _df(self, fn, *args, **kwargs):
        """Run one blocking DFHack call off the event loop, one at a time."""
        async with self._df_lock:
            started = time.perf_counter()
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            finally:
                self.rpc_stats.record(call_name(fn, args), time.perf_counter() - started)

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
                   "max_workshops": o.get("max_workshops", 0),
                   "conds": [condition_text(c, labels) for c in o.get("conds") or []],
                   "cond_info": [_condition_info(c) for c in o.get("conds") or []]}
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

    def _poll_blocks_soon(self) -> None:
        """Read the map soon, for a change a player is waiting to see."""
        self._blocks_due = min(self._blocks_due, self._last_blocks + POLL_SOON_GAP)

    def _slowdown(self) -> int:
        """How much less often to poll: nobody's looking if every tab is hidden."""
        if self.sessions and all(s.hidden for s in self.sessions.values()):
            return HIDDEN_SLOWDOWN
        return 1

    def _on_visibility(self, s: Session, hidden: bool) -> None:
        was_slow = self._slowdown() > 1
        s.hidden = hidden
        if was_slow and self._slowdown() == 1:
            # Someone came back: catch them up now, not at the slower pace.
            self._blocks_due = self._units_due = self._paused_due = 0.0

    async def _poll_paused(self, now: float) -> None:
        if now < self._paused_due:
            return
        self._paused_due = now + self.unit_interval * 2 * self._slowdown()
        paused = await self._df(self.df.paused)
        if self.hold_active and not paused:
            # Someone unpaused in DF itself: that overrides every hold.
            log.info("game unpaused in DF; clearing holds")
            self.holders.clear()
            self.hold_active = False
        if paused != self.paused:
            self.paused = paused
            self._broadcast_pause()

    async def _read_units(self) -> tuple[list[dict], dict[int, tuple[list, bool]]]:
        """Creatures players can see on their levels, and for graphics each one's worn and
        wielded items as (item type, subtype) and whether it's a baby. coop.lua sends only
        what's asked; older modules fall back to RemoteFortressReader's list of every unit."""
        if self.lua_ok and self.lua_version >= LUA_FOR_UNITS:
            levels = sorted({z for s in self.sessions.values() for z in s.zs if 0 <= z < self.state.depth})
            if not levels:
                return [], {}
            gear = any(s.graphics for s in self.sessions.values())
            rows = json.loads((await self._df(self.df.run_lua, "rpc.coop", "units", ",".join(map(str, levels)),
                                              "1" if gear else "0"))[0])
            rows = rows if isinstance(rows, list) else []
            equipment = {r["i"]: ([tuple(g) for g in r.get("gear", [])], bool(r.get("baby"))) for r in rows}
            visible = [{k: r[k] for k in ("i", "x", "y", "z", "n", "c", "race", "caste")} for r in rows
                       if 0 <= r["x"] < self.state.width and 0 <= r["y"] < self.state.height]
            return visible, equipment
        units = (await self._df(self.df.units)).creature_list
        visible = self.state.visible_units(units)
        ids = {u["i"] for u in visible}
        equipment = {u.id: ([(e.item.type.mat_type, e.item.type.mat_index) for e in u.inventory
                             if e.mode in (rfr.Worn, rfr.Weapon)],
                            u.HasField("age") and u.age == 0)
                     for u in units if u.id in ids}
        return visible, equipment

    async def poll_units(self) -> None:
        self.current_players()   # pick up role changes from `set-role` without a reconnect
        visible, equipment = await self._read_units()
        now = asyncio.get_running_loop().time()
        await self._poll_paused(now)
        slow = self._slowdown()
        if (not self.lua_ok or self.lua_version < LUA_VERSION) and now >= self._lua_recheck_due:
            # Pick up `install-lua` without a hub restart.
            self._lua_recheck_due = now + 10.0
            was = (self.lua_ok, self.lua_version)
            await self._check_lua()
            if (self.lua_ok, self.lua_version) != was:
                for s in self.sessions.values():
                    self._send(s, self._hello(s))
        if now >= self._date_due:
            self._date_due = now + POLL_DATE * slow
            await self._poll_date()
        if now >= self._orders_due:
            self._orders_due = now + POLL_ORDERS * slow
            await self._poll_orders()
        if now >= self._squads_due:
            self._squads_due = now + POLL_SQUADS * slow
            await self._poll_squads()
        if now >= self._nobles_due:
            self._nobles_due = now + POLL_NOBLES * slow
            await self._poll_nobles()
        if now >= self._reports_due:
            self._reports_due = now + POLL_REPORTS * slow
            await self._poll_reports()
        await self._unit_graphics(visible, equipment)
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

    async def _unit_graphics(self, visible, equipment) -> None:
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
        for unit in visible:
            raw = self.state.creature_defs.get(unit.get("race"))
            if not raw:
                continue
            creature, castes = raw
            items, baby = equipment.get(unit["i"], ([], False))
            worn = []
            for kind in items:
                token = self.state.item_defs.get(tuple(kind), "")
                if token:
                    worn.append(token.split(":")[-1].split("/")[-1])
            layers = self.game_tileset.unit_sprites(creature, castes.get(unit["caste"], ""), worn,
                                                   "BABY" if baby else "DEFAULT")
            if layers:
                unit["g"] = layers

    async def run_polling(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                if not self.df_online:
                    await self._reconnect()
                if self.sessions:
                    now = loop.time()
                    if now >= self._blocks_due:
                        # Set first: a player acting during the read asks for another.
                        self._last_blocks = now
                        self._blocks_due = now + self.poll_interval * self._slowdown()
                        await self.poll_blocks()
                    if loop.time() >= self._units_due:
                        await self.poll_units()
                        self._units_due = loop.time() + self.unit_interval * self._slowdown()
            except (DFHackRPCError, OSError) as exc:
                self._note_offline(exc)
                await self._go_offline()
            summary = self.rpc_stats.report(loop.time())
            if summary:
                log.info("%s", summary)
            try:
                tick = min(POLL_TICK, self.poll_interval, self.unit_interval)
                await asyncio.wait_for(stop.wait(), tick if self.df_online else self._offline_wait)
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
        self._poll_blocks_soon()
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
            elif kind in ("edit_condition", "remove_condition", "add_condition"):
                await self._on_condition(s, kind, msg)
            elif kind == "squad_candidates":
                await self._on_squad_candidates(s)
            elif kind in ("squad_add", "squad_remove", "rename_squad"):
                await self._on_squad_change(s, kind, msg)
            elif kind == "squad_targets":
                await self._on_squad_targets(s)
            elif kind in ("squad_station", "squad_kill", "squad_clear"):
                await self._on_squad_order(s, kind, msg)
            elif kind == "inspect_place":
                await self._on_inspect_place(s, msg)
            elif kind == "zone_candidates":
                await self._on_zone_candidates(s)
            elif kind in ("stockpile_category", "stockpile_containers", "rename_place", "zone_owner", "zone_active",
                          "workshop_job", "workshop_worker", "farm_crop", "farm_fertilize"):
                await self._on_place_change(s, kind, msg)
            elif kind == "noble_candidates":
                await self._on_noble_candidates(s)
            elif kind == "citizens":
                await self._on_citizens(s)
            elif kind == "stocks":
                await self._on_stocks(s)
            elif kind == "work_details":
                await self._on_work_details(s)
            elif kind in ("work_detail_member", "work_detail_mode"):
                await self._on_work_detail_change(s, kind, msg)
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
            elif kind == "visibility" and isinstance(msg.get("hidden"), bool):
                self._on_visibility(s, msg["hidden"])
            if need in ("dig", "build", "remove"):
                self._poll_blocks_soon()
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
            self._poll_blocks_soon()
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
        limit = msg.get("max_workshops")

        def reply(ok, label=None, error=None):
            out = {"type": "edit_result", "ok": ok, "label": label}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(oid):
            return reply(False, error="malformed order")
        if amount is None and freq is None and limit is None:
            return reply(False, error="nothing to change")
        if amount is not None and (not _is_int(amount) or not 1 <= amount <= MAX_ORDER_AMOUNT):
            return reply(False, error=f"the amount must be 1 to {MAX_ORDER_AMOUNT}")
        if freq is not None and (not isinstance(freq, str) or freq not in ORDER_FREQUENCIES):
            return reply(False, error="that isn't a repeat option")
        if limit is not None and (not _is_int(limit) or not 0 <= limit <= MAX_WORKSHOPS):
            return reply(False, error=f"the workshop limit must be 0 to {MAX_WORKSHOPS}")
        if (problem := self._orders_ready(LUA_FOR_ORDER_EDITS if limit is None else LUA_FOR_CONDITIONS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        label = next((o["label"] for o in self.orders if o["id"] == oid), "that order")
        args = [str(oid), "" if amount is None else str(amount), freq or ""]
        if limit is not None:
            args.append(str(limit))
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "set_order", *args))[0])
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
        if limit is not None:
            entry["max_workshops"] = limit
        self.action_log.append(entry)
        log.info("%s: changed order %d (%s): amount=%s freq=%s max_workshops=%s", s.name, oid, label, amount, freq, limit)
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

    async def _on_condition(self, s: Session, kind: str, msg: dict) -> None:
        oid = msg.get("order")

        def reply(ok, label=None, error=None):
            out = {"type": "condition_result", "ok": ok, "label": label}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(oid):
            return reply(False, error="malformed order")
        args = _condition_args(kind, msg)
        if isinstance(args, str):
            return reply(False, error=args)
        function, *rest = args
        if (problem := self._orders_ready(LUA_FOR_CONDITIONS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        label = next((o["label"] for o in self.orders if o["id"] == oid), "that order")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", function, str(oid), *rest))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s on order %d failed in DF: %s", s.name, kind, oid, exc)
            return reply(False, label, "DF refused that")
        if not r.get("ok"):
            self._orders_due = 0.0  # a stale list catches up straight away
            problem = ("that order is already gone" if r.get("missing")
                       else "that condition has changed; check the list and try again" if r.get("stale")
                       else "it already has that condition" if r.get("duplicate")
                       else "those orders would wait for each other forever" if r.get("loop")
                       else "DF refused that")
            return reply(False, label, problem)
        self.action_log.append({"type": "order_conditions", "t": round(_time(), 1), "player": s.name,
                                "order_id": oid, "label": label, "change": kind})
        log.info("%s: %s on order %d (%s)", s.name, kind, oid, label)
        self._orders_due = 0.0
        reply(True, label)

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

    async def _on_squad_targets(self, s: Session) -> None:
        def reply(ok, units=None, error=None):
            out = {"type": "squad_targets", "ok": ok}
            if units is not None:
                out["units"] = units
            if error:
                out["error"] = error
            self._send(s, out)

        if (problem := self._orders_ready(LUA_FOR_SQUAD_ORDERS)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        try:
            raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "squad_targets"))[0])
        except DFHackRPCError as exc:
            log.warning("%s: squad_targets failed in DF: %s", s.name, exc)
            return reply(False, error="DF refused that")
        # Only what this player is being shown: no finding ambushers through the list.
        reply(True, [u for u in raw if u["id"] in s.visible_units])

    def _squad_order_args(self, s: Session, kind: str, msg: dict) -> list[str] | str:
        """The Lua arguments after the squad id, or why the message is no good."""
        if kind == "squad_clear":
            return []
        if kind == "squad_station":
            x, y, z = msg.get("x"), msg.get("y"), msg.get("z")
            if not all(_is_int(v) for v in (x, y, z)):
                return "malformed request"
            if not self.df_online:
                return "the fortress isn't loaded"
            if not (0 <= x < self.state.width and 0 <= y < self.state.height and 0 <= z < self.state.depth) \
                    or self.state.is_hidden(x, y, z):
                return "you can't station a squad there"
            return [str(x), str(y), str(z)]
        units = msg.get("units")
        if not isinstance(units, list) or not units or not all(_is_int(u) for u in units):
            return "malformed request"
        if len(units) > MAX_KILL_TARGETS:
            return f"name {MAX_KILL_TARGETS} targets at most"
        if not all(u in s.visible_units for u in units):
            return "you can't see that creature"
        return [",".join(str(u) for u in units)]

    async def _on_squad_order(self, s: Session, kind: str, msg: dict) -> None:
        squad = msg.get("squad")

        def reply(ok, error=None):
            out = {"type": "squad_result", "ok": ok, "action": kind}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(squad):
            return reply(False, "malformed request")
        args = self._squad_order_args(s, kind, msg)
        if isinstance(args, str):
            return reply(False, args)
        if (problem := self._orders_ready(LUA_FOR_SQUAD_ORDERS)):
            return reply(False, problem)
        if not s.limiter.take():
            return reply(False, "slow down a little")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", kind, str(squad), *args))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s failed in DF: %s", s.name, kind, exc)
            return reply(False, "DF refused that")
        if not r.get("ok"):
            return reply(False, r.get("why") or "DF refused that")
        self.action_log.append({"type": kind, "t": round(_time(), 1), "player": s.name, "squad": squad, "args": args})
        log.info("%s: %s squad %d %s", s.name, kind, squad, args)
        self._squads_due = 0.0
        reply(True)

    # -- nobles ---------------------------------------------------------------------

    async def _on_citizens(self, s: Session) -> None:
        def reply(ok, citizens=None, error=None):
            out = {"type": "citizens", "ok": ok}
            if ok:
                out["citizens"] = citizens
            if error:
                out["error"] = error
            self._send(s, out)

        if not self.df_online or not self.lua_ok or self.lua_version < LUA_FOR_CITIZENS:
            return reply(False, error="the host needs to update the in-game module (hub.py install-lua)")
        now = asyncio.get_running_loop().time()
        if now - self._citizens_read >= CITIZENS_CACHE:
            try:
                raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "citizens"))[0])
            except DFHackRPCError as exc:
                log.warning("reading citizens failed in DF: %s", exc)
                return reply(False, error="DF refused that")
            self._citizens = [_citizen(c) for c in raw] if isinstance(raw, list) else []
            self._citizens_read = now
        reply(True, self._citizens)

    async def _on_stocks(self, s: Session) -> None:
        """Everyone shares one count; a request while DF is being counted waits for it."""
        if not self.df_online or not self.lua_ok or self.lua_version < LUA_FOR_STOCKS:
            self._send(s, {"type": "stocks", "ok": False,
                           "error": "the host needs to update the in-game module (hub.py install-lua)"})
            return
        loop = asyncio.get_running_loop()
        async with self._stocks_lock:
            if loop.time() - self._stocks_read >= STOCKS_CACHE:
                try:
                    raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "stocks"))[0])
                except DFHackRPCError as exc:
                    log.warning("counting stocks failed in DF: %s", exc)
                    self._send(s, {"type": "stocks", "ok": False, "error": "DF refused that"})
                    return
                self._stocks = stock_rows(raw.get("items") or [])
                self._stocks_read = loop.time()
        self._send(s, {"type": "stocks", "ok": True, "groups": self._stocks,
                       "age": round(loop.time() - self._stocks_read)})

    async def _on_work_details(self, s: Session) -> None:
        def reply(ok, error=None):
            out = {"type": "work_details", "ok": ok}
            if ok:
                out.update(self._work_details)
            if error:
                out["error"] = error
            self._send(s, out)

        if not self.df_online or not self.lua_ok or self.lua_version < LUA_FOR_WORK_DETAILS:
            return reply(False, "the host needs to update the in-game module (hub.py install-lua)")
        now = asyncio.get_running_loop().time()
        if now - self._work_details_read >= CITIZENS_CACHE:
            try:
                raw = json.loads((await self._df(self.df.run_lua, "rpc.coop", "work_details"))[0])
            except DFHackRPCError as exc:
                log.warning("reading work details failed in DF: %s", exc)
                return reply(False, "DF refused that")
            for d in raw.get("details") or []:
                d["labors"] = [labor_label(t) for t in d.get("labors") or []]
            self._work_details = {"details": raw.get("details") or [], "people": raw.get("people") or []}
            self._work_details_read = now
        reply(True)

    async def _on_work_detail_change(self, s: Session, kind: str, msg: dict) -> None:
        index, name = msg.get("detail"), msg.get("name")

        def reply(ok, error=None):
            out = {"type": "work_detail_result", "ok": ok}
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(index) or not isinstance(name, str) or not name or len(name) > MAX_DETAIL_NAME:
            return reply(False, "malformed work detail")
        if kind == "work_detail_member":
            unit, on = msg.get("unit"), msg.get("on")
            if not _is_int(unit) or not isinstance(on, bool):
                return reply(False, "malformed change")
            args = [str(unit), "1" if on else "0"]
        else:
            mode = msg.get("mode")
            if not isinstance(mode, str) or mode not in WORK_DETAIL_MODES:
                return reply(False, "that isn't a setting")
            args = [mode]
        if not self.df_online or not self.lua_ok or self.lua_version < LUA_FOR_WORK_DETAILS:
            return reply(False, "the host needs to update the in-game module (hub.py install-lua)")
        if not s.limiter.take():
            return reply(False, "slow down a little")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", kind, str(index), name, *args))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s failed in DF: %s", s.name, kind, exc)
            return reply(False, "DF refused that")
        self._work_details_read = float("-inf")
        if not r.get("ok"):
            return reply(False, r.get("why") or "DF refused that")
        entry = {"type": kind, "t": round(_time(), 1), "player": s.name, "detail": name}
        entry.update({"unit": msg["unit"], "on": msg["on"]} if kind == "work_detail_member" else {"mode": msg["mode"]})
        self.action_log.append(entry)
        log.info("%s: %s %s %s", s.name, kind, name, args)
        reply(True)

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

    # -- stockpiles and zones --------------------------------------------------------

    async def _on_inspect_place(self, s: Session, msg: dict) -> None:
        pid, x, y, z = msg.get("id"), msg.get("x"), msg.get("y"), msg.get("z")

        def reply(ok, place=None, error=None):
            out = {"type": "place_info", "ok": ok}
            if place:
                out["place"] = place
            if error:
                out["error"] = error
            self._send(s, out)

        if not all(_is_int(v) for v in (pid, x, y, z)):
            return reply(False, error="malformed request")
        if (problem := self._orders_ready(LUA_FOR_PLACES)):
            return reply(False, error=problem)
        if not (0 <= x < self.state.width and 0 <= y < self.state.height and 0 <= z < self.state.depth) \
                or self.state.is_hidden(x, y, z):
            # Same answer as an empty tile: nothing here about unexplored rock.
            return reply(False, error="nothing to inspect there")
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        r = json.loads((await self._df(self.df.run_lua, "rpc.coop", "place_info",
                                       str(pid), str(x), str(y), str(z)))[0])
        if not r.get("found"):
            return reply(False, error="nothing to inspect there")
        r.pop("found")
        reply(True, {**r, "x": x, "y": y})

    async def _on_zone_candidates(self, s: Session) -> None:
        def reply(ok, units=None, error=None):
            out = {"type": "zone_candidates", "ok": ok}
            if units is not None:
                out["units"] = units
            if error:
                out["error"] = error
            self._send(s, out)

        if (problem := self._orders_ready(LUA_FOR_PLACES)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        reply(True, json.loads((await self._df(self.df.run_lua, "rpc.coop", "zone_candidates"))[0]))

    def _place_args(self, kind: str, msg: dict) -> list[str] | str:
        """The Lua arguments after the id, or why the message is no good."""
        if kind == "stockpile_category":
            category = msg.get("category")
            if not isinstance(category, str) or category not in STOCK_CATEGORIES:
                return "unknown category"
            if not isinstance(msg.get("on"), bool):
                return "malformed request"
            return [category, "1" if msg["on"] else "0"]
        if kind == "stockpile_containers":
            counts = [msg.get(k) for k in ("barrels", "bins", "wheelbarrows")]
            if not all(_is_int(n) for n in counts):
                return "malformed request"
            return [str(n) for n in counts]
        if kind == "rename_place":
            name = msg.get("name")
            if not isinstance(name, str):
                return "malformed request"
            name = " ".join("".join(c if c.isprintable() else " " for c in name).split())
            if len(name) > MAX_SQUAD_NAME:
                return f"keep the name to {MAX_SQUAD_NAME} characters"
            return [name]
        if kind == "farm_crop":
            season, plant = msg.get("season"), msg.get("plant")
            if not _is_int(season) or not 0 <= season <= 3 or not _is_int(plant) or plant < -1:
                return "malformed request"
            return [str(season), str(plant)]
        if kind == "workshop_job":
            job, action = msg.get("job"), msg.get("action")
            if not _is_int(job) or not isinstance(action, str) or action not in WORKSHOP_JOB_ACTIONS:
                return "malformed request"
            return [str(job), action]
        if kind in ("zone_owner", "workshop_worker"):
            unit = msg.get("unit")
            if unit is not None and not _is_int(unit):
                return "malformed request"
            return ["" if unit is None else str(unit)]
        if not isinstance(msg.get("on"), bool):       # zone_active, farm_fertilize
            return "malformed request"
        return ["1" if msg["on"] else "0"]

    async def _on_place_change(self, s: Session, kind: str, msg: dict) -> None:
        pid = msg.get("id")

        def reply(ok, name=None, error=None):
            out = {"type": "place_result", "ok": ok, "action": kind}
            if name:
                out["name"] = name
            if error:
                out["error"] = error
            self._send(s, out)

        if not _is_int(pid):
            return reply(False, error="malformed request")
        args = self._place_args(kind, msg)
        if isinstance(args, str):
            return reply(False, error=args)
        needs = (LUA_FOR_WORKSHOPS if kind.startswith("workshop_") else LUA_FOR_FARMS if kind.startswith("farm_")
                 else LUA_FOR_PLACES)
        if (problem := self._orders_ready(needs)):
            return reply(False, error=problem)
        if not s.limiter.take():
            return reply(False, error="slow down a little")
        try:
            r = json.loads((await self._df(self.df.run_lua, "rpc.coop", kind, str(pid), *args))[0])
        except DFHackRPCError as exc:
            log.warning("%s: %s failed in DF: %s", s.name, kind, exc)
            return reply(False, error="DF refused that")
        if not r.get("ok"):
            return reply(False, error=r.get("why") or "DF refused that")
        self.action_log.append({"type": kind, "t": round(_time(), 1), "player": s.name, "place": pid, "args": args})
        log.info("%s: %s %d %s", s.name, kind, pid, args)
        reply(True, r.get("name"))
        broadcast([x.conn for x in self.sessions.values()], json.dumps({"type": "place_changed", "id": pid}))

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
                                  "freqs": [{"id": k, "label": v} for k, v in ORDER_FREQUENCIES.items()],
                                  "compares": [{"id": k, "label": v} for k, v in CONDITION_COMPARES.items()],
                                  "cond_items": [{"id": k, "label": v} for k, v in CONDITION_ITEMS.items()],
                                  "cond_whens": [{"id": k, "label": v} for k, v in CONDITION_WHENS.items()]},
                "can_order": self.lua_ok and self.lua_version >= LUA_FOR_ORDERS, "orders": self.orders,
                "can_edit_orders": self.lua_ok and self.lua_version >= LUA_FOR_ORDER_EDITS,
                "can_edit_conditions": self.lua_ok and self.lua_version >= LUA_FOR_CONDITIONS,
                "can_squads": self.lua_ok and self.lua_version >= LUA_FOR_SQUADS, "squads": self.squads,
                "can_squad_orders": self.lua_ok and self.lua_version >= LUA_FOR_SQUAD_ORDERS,
                "can_citizens": self.lua_ok and self.lua_version >= LUA_FOR_CITIZENS,
                "can_stocks": self.lua_ok and self.lua_version >= LUA_FOR_STOCKS,
                "can_work_details": self.lua_ok and self.lua_version >= LUA_FOR_WORK_DETAILS,
                "work_detail_modes": [{"id": k, "label": v} for k, v in WORK_DETAIL_MODES.items()],
                "can_nobles": self.lua_ok and self.lua_version >= LUA_FOR_NOBLES, "nobles": self.nobles,
                "can_places": self.lua_ok and self.lua_version >= LUA_FOR_PLACES,
                "can_workshops": self.lua_ok and self.lua_version >= LUA_FOR_WORKSHOPS,
                "can_farms": self.lua_ok and self.lua_version >= LUA_FOR_FARMS,
                "stock_categories": [{"id": k, "label": v} for k, v in STOCK_CATEGORIES.items()],
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


def _citizen(c: dict) -> dict:
    stress = c.get("stress", -1)
    out = {"id": c["id"], "name": c.get("name", ""), "profession": c.get("profession", ""),
           "job": c.get("job", ""), "stress": stress, "squad": c.get("squad", ""),
           "mood": STRESS_LABELS[stress] if 0 <= stress < len(STRESS_LABELS) else ""}
    if "x" in c:
        out.update(x=c["x"], y=c["y"], z=c["z"])
    return out


def _condition_info(cond: dict) -> dict:
    """What the client needs to edit one condition. No key means read-only."""
    info = {"key": cond.get("key", "")}
    if "after" in cond:
        return info
    return {**info, "cmp": cond.get("cmp", ""), "val": cond.get("val", 0), "what": condition_what(cond)}


def _condition_args(kind: str, msg: dict) -> list | str:
    """The Lua function and arguments for a condition change, or what's wrong
    with it. Nothing from a player reaches DF unchecked."""
    key = msg.get("key")
    if kind != "add_condition" and (not isinstance(key, str) or not key or len(key) > MAX_CONDITION_KEY):
        return "malformed condition"
    if kind == "remove_condition":
        return ["remove_condition", key]
    if kind == "add_condition" and "after" in msg:
        after, when = msg.get("after"), msg.get("when")
        if not _is_int(after):
            return "malformed order"
        if not isinstance(when, str) or when not in CONDITION_WHENS:
            return "that isn't an order state"
        return ["add_order_condition", str(after), when]
    cmp, val = msg.get("cmp"), msg.get("val")
    if not isinstance(cmp, str) or cmp not in CONDITION_COMPARES:
        return "that isn't a comparison"
    if not _is_int(val) or not 0 <= val <= MAX_CONDITION_VALUE:
        return f"the number must be 0 to {MAX_CONDITION_VALUE}"
    if kind == "edit_condition":
        return ["edit_condition", key, cmp, str(val)]
    item = msg.get("item")
    if not isinstance(item, str) or item not in CONDITION_ITEMS:
        return "that item can't be used in a condition"
    return ["add_item_condition", item, cmp, str(val)]


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
