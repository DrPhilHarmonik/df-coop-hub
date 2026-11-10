"""The hub over real websockets, with a fake fortress behind it. These check
the things that matter between friends: who gets in, that everyone sees the
same fortress and each other, and that nothing hidden leaks."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("google.protobuf", reason="df-coop needs the coop extra")
pytest.importorskip("websockets", reason="df-coop needs the coop extra")

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from dfhack_rpc import DFHackRPCError, rfr
from actions import Owners
from hub import Hub, Session, add_player, load_players
from mapstate import FLAGS1_HIDDEN_IN_AMBUSH, FLAGS2_KILLED, UNIT_GONE_FLAGS1
from rpcstats import RpcStats

N = 256
TOKENS = {"tok-a": "Alice", "tok-b": "Bob"}


FLOOR, WALL, BUILT_WALL = 3, 4, 5


class FakeDF:
    """2x2 blocks (32x32 tiles), 10 levels, stone floor everywhere except on
    level 5: tile (0, 0) is hidden, and block (1, 0) holds rock walls at
    x 16-23, a constructed wall at x 24, and a hidden tile at (31, 15).
    Like DF, a block comes back from a normal poll only after it changed."""

    connected = True

    def __init__(self):
        self.block_calls = []
        self.unit_list = [
            rfr.UnitDefinition(id=1, pos_x=4, pos_y=4, pos_z=5, name="Urist"),
            rfr.UnitDefinition(id=2, pos_x=0, pos_y=0, pos_z=5, name="Lurker"),
            rfr.UnitDefinition(id=3, pos_x=6, pos_y=6, pos_z=5, flags1=FLAGS1_HIDDEN_IN_AMBUSH),
        ]
        self.fail = False
        self.is_paused = True
        self.pause_calls = []
        self.dig_calls = []
        self.designated = {}   # (x, y, z) -> dig code
        self.priorities = {}   # (x, y, z) -> priority set through set_priority
        self.dirty = set()     # (bx, by, z) changed since the last poll
        self.no_map_calls = 0  # map_info refuses this many times, like DF on the title screen
        self.lua_installed = True
        self.lua_calls = []
        self.year, self.tick = 23, 147722
        self.apply_stats = None  # override what the fake quickfort reports
        self.lua_version = "20"
        self.detail_data = {"details": [{"i": 0, "name": "Miners", "mode": "OnlySelectedDoesThis", "no_everybody": True,
                                         "labors": ["MINE", "HAUL_STONE"], "members": [7]}],
                            "people": [{"id": 7, "name": "Urist"}, {"id": 8, "name": "Bomrek"}]}
        self.detail_reply = {"ok": True}
        self.detail_marks = {}           # coop.lua detail_marks: {"z": [[x, y, kind]]}
        self.citizen_list = [
            {"id": 7, "name": "Urist, miner", "profession": "Miner", "job": "Dig", "stress": 1, "squad": "The Axes",
             "x": 3, "y": 4, "z": 5},
            {"id": 8, "name": "Bomrek", "profession": "Peasant", "job": "", "stress": -1, "squad": ""},
        ]
        self.condition_replies = {}
        self.report_list = [
            {"id": 10, "text": "A caravan from Ukosh has arrived.", "type": "CARAVAN_ARRIVAL", "year": 23, "time": 0,
             "x": 4, "y": 4, "z": 5, "hidden": False, "color": 11},
            {"id": 11, "text": "Something stirs in the depths.", "type": "X", "year": 23, "time": 1200,
             "x": 0, "y": 0, "z": 5, "hidden": True, "color": 12},   # on the hidden tile
        ]
        self.unit_details = {1: {"found": True, "id": 1, "name": "Urist McTest, miner", "profession": "miner",
                                 "job": "Dig", "stress": 0, "age": 40, "squad": "", "citizen": True,
                                 "needs": ["PrayOrMeditate", "BeWithFriends"]}}
        self.manager_orders = [{"id": 51, "job": "CustomReaction", "reaction": "BREW_DRINK_FROM_PLANT", "left": 4,
                                "total": 10, "material": "", "freq": "Daily"}]
        self.busy_orders = set()
        self.workorder_ok = True
        self.squad_list = [{"id": 2, "name": "The Axes", "alias": "", "slots": 3, "routine": "Off duty", "orders": [],
                            "members": [{"slot": 0, "leader": True, "unit": 1, "name": "Urist, captain", "away": False}]}]
        self.free_units = [{"id": 8, "name": "Bomrek, miner"}]
        self.places = {300: {"found": True, "kind": "stockpile", "id": 300, "number": 1, "name": "", "tiles": 12, "z": 5,
                             "categories": ["wood"], "barrels": 0, "bins": 0, "wheelbarrows": 0},
                       58: {"found": True, "kind": "zone", "id": 58, "number": 1, "name": "", "tiles": 9, "z": 5,
                            "zone": "Bedroom", "active": True, "ownable": True, "owner": ""},
                       400: {"found": True, "kind": "workshop", "id": 400, "name": "Stoneworker's Workshop",
                             "custom": "", "workshop": "Masons", "built": True, "z": 5, "tiles": 9,
                             "jobs": [{"id": 11, "name": "Make granite blocks", "order": 8, "worker": "",
                                       "suspended": False, "repeat": False, "now": False, "building": False}],
                             "workers": [], "general_orders": 5, "orders_here": 0},
                       500: {"found": True, "kind": "farm", "id": 500, "name": "", "tiles": 16, "z": 5,
                             "underground": True, "fertilize": False, "season": 1,
                             "crops": [{"id": 173, "name": "plump helmets"}] * 4,
                             "options": [[{"id": 173, "name": "plump helmets", "seeds": 58}]] * 4}}
        self.posts = [{"id": 16, "code": "MANAGER", "title": "manager", "rank": 160, "holder": "Urist, manager",
                       "locked": "", "duties": ["MANAGE_PRODUCTION"], "needs": {"office": 1}},
                      {"id": 17, "code": "MAYOR", "title": "lord", "rank": 60, "holder": "Zon, lady",
                       "locked": "this title passes to an heir", "duties": [], "needs": {}}]
        # (x, y, z) -> (building id, DF type, what deconstruct does)
        self.removable = {(4, 4, 5): (7, "Bed", "removed"), (9, 9, 5): (8, "Workshop", "queued")}

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def map_info(self):
        if self.no_map_calls:
            self.no_map_calls -= 1
            raise DFHackRPCError("call 23 failed", code=1)  # CR_FAILURE: !Maps::IsValid()
        return rfr.MapInfo(block_size_x=2, block_size_y=2, block_size_z=10, world_name_english="Testhold",
                           save_name="testsave", block_pos_x=21, block_pos_y=33)

    def tiletypes(self):
        tl = rfr.TiletypeList()
        tl.tiletype_list.add(id=0, caption="void", shape=rfr.NO_SHAPE)
        tl.tiletype_list.add(id=FLOOR, caption="stone floor", shape=rfr.FLOOR, material=rfr.STONE)
        tl.tiletype_list.add(id=WALL, caption="rock wall", shape=rfr.WALL, material=rfr.STONE)
        tl.tiletype_list.add(id=BUILT_WALL, caption="constructed wall", shape=rfr.WALL, material=rfr.CONSTRUCTION)
        return tl

    def tile(self, x, y, z):
        if z == 5 and 16 <= x <= 23 and y < 16:
            return WALL
        if z == 5 and x == 24 and y < 16:
            return BUILT_WALL
        return FLOOR

    def dig(self, designation, locations):
        locations = list(locations)
        self.dig_calls.append((designation, locations))
        for x, y, z in locations:
            self.designated[(x, y, z)] = designation
            self.dirty.add((x // 16, y // 16, z))

    def run_lua(self, module, function, *args):
        """A stand-in for rpc/coop.lua: same functions, same string-in,
        JSON-out contract."""
        if not self.lua_installed:
            raise DFHackRPCError("call 4 failed", code=3)  # CR_NOT_FOUND
        self.lua_calls.append((module, function, *args))
        if function == "ping":
            return [self.lua_version]
        if function == "reports":
            after = int(args[0])
            new = [r for r in self.report_list if r["id"] > after]
            newest = max([r["id"] for r in self.report_list] + [after])
            return [json.dumps({"newest": newest, "reports": new})]
        if function == "unit":
            return [json.dumps(self.unit_details.get(int(args[0]), {"found": False}))]
        if function == "set_priority":
            x1, y1, x2, y2, z, prio = (int(a) for a in args)
            self.priorities.update({(x, y, z): prio for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)
                                    if self.designated.get((x, y, z))})
            return [json.dumps({"set": 0})]
        if function == "set_priority_spans":
            encoded, z, priority = args
            z, priority = int(z), int(priority)
            changed = {(x, y, z): priority for x, first_y, last_y in json.loads(encoded)
                       for y in range(first_y, last_y + 1) if self.designated.get((x, y, z))}
            self.priorities.update(changed)
            return [json.dumps({"set": len(changed)})]
        if function == "orders":
            return [json.dumps(self.manager_orders)]
        if function == "work_details":
            return [json.dumps(self.detail_data)]
        if function in ("work_detail_member", "work_detail_mode"):
            return [json.dumps(self.detail_reply)]
        if function == "detail":
            kind, x1, y1, x2, y2, z, priority = args
            area = (int(x2) - int(x1) + 1) * (int(y2) - int(y1) + 1)
            self.detail_marks.setdefault(z, []).append([int(x1), int(y1), kind])
            return [json.dumps({"ok": True, "applied": area - 1, "skipped": 1})]
        if function == "detail_erase":
            cleared = sum(len(v) for v in self.detail_marks.values())
            self.detail_marks = {}
            return [json.dumps({"ok": True, "cleared": cleared})]
        if function == "detail_marks":
            levels = set(args[0].split(","))
            return [json.dumps({z: m for z, m in self.detail_marks.items() if z in levels} or [])]
        if function == "units":
            # As coop.lua: the levels asked for, nothing gone, dead or in ambush, nothing on a
            # hidden tile; gear and baby only when asked.
            if self.fail:
                raise DFHackRPCError("connection closed by DFHack")
            levels = {int(z) for z in args[0].split(",")}
            rows = []
            for u in self.unit_list:
                if u.pos_z not in levels or u.flags1 & UNIT_GONE_FLAGS1 or u.flags2 & FLAGS2_KILLED:
                    continue
                if u.pos_z == 5 and (u.pos_x, u.pos_y) in ((0, 0), (31, 15)):
                    continue
                row = {"i": u.id, "x": u.pos_x, "y": u.pos_y, "z": u.pos_z, "n": u.name,
                       "c": (f"#{u.profession_color.red & 255:02x}{u.profession_color.green & 255:02x}"
                             f"{u.profession_color.blue & 255:02x}" if u.HasField("profession_color") else "#ffffff"),
                       "race": u.race.mat_type, "caste": u.race.mat_index}
                if args[1] == "1":
                    row["gear"] = [[e.item.type.mat_type, e.item.type.mat_index] for e in u.inventory
                                   if e.mode in (rfr.Worn, rfr.Weapon)]
                    row["baby"] = u.HasField("age") and u.age == 0
                rows.append(row)
            return [json.dumps(rows)]
        if function == "stocks":
            return [json.dumps({"items": [{"type": "BAR", "what": "", "material": "silver", "count": 9},
                                          {"type": "WEAPON", "what": "short swords", "material": "iron", "count": 2},
                                          {"type": "DRINK", "what": "", "material": "dwarven ale", "count": 40}]})]
        if function == "citizens":
            if self.citizen_list is None:
                raise DFHackRPCError("boom")
            return [json.dumps(self.citizen_list)]
        if function in ("farm_crop", "farm_fertilize"):
            if int(args[0]) not in self.places:
                return [json.dumps({"ok": False, "why": "that farm plot is gone"})]
            return [json.dumps({"ok": True})]
        if function in ("workshop_job", "workshop_worker"):
            if int(args[0]) not in self.places:
                return [json.dumps({"ok": False, "why": "that workshop is gone"})]
            if function == "workshop_worker":
                return [json.dumps({"ok": True, "name": "Bomrek, miner" if args[1] else ""})]
            return [json.dumps({"ok": True})]
        if function == "order":
            job, reaction, material, amount, *freq = args
            if not self.workorder_ok:
                return [json.dumps({"ok": False, "output": "workorder: bad"})]
            self.manager_orders.append({"id": 600 + len(self.manager_orders), "job": job, "reaction": reaction,
                                        "left": int(amount), "total": int(amount), "material": material,
                                        "freq": freq[0] if freq else "OneTime"})
            return [json.dumps({"ok": True, "output": ""})]
        if function == "place_info":
            return [json.dumps(self.places.get(int(args[0]), {"found": False}))]
        if function == "zone_candidates":
            return [json.dumps([{"id": 8, "name": "Bomrek, miner"}])]
        if function in ("stockpile_category", "stockpile_containers", "rename_place", "zone_owner", "zone_active"):
            place = self.places.get(int(args[0]))
            if not place:
                return [json.dumps({"ok": False, "why": "that place is gone"})]
            if function == "zone_owner":
                place["owner"] = "Bomrek, miner" if args[1] else ""
                return [json.dumps({"ok": True, "name": place["owner"]})]
            if function == "zone_active":
                place["active"] = args[1] == "1"
            elif function == "rename_place":
                place["name"] = args[1]
            elif function == "stockpile_containers":
                place["barrels"], place["bins"], place["wheelbarrows"] = (int(a) for a in args[1:4])
            else:
                cats = set(place["categories"])
                (cats.add if args[2] == "1" else cats.discard)(args[1])
                place["categories"] = sorted(cats)
            return [json.dumps({"ok": True})]
        if function == "nobles":
            return [json.dumps(self.posts)]
        if function == "noble_candidates":
            return [json.dumps([{"id": 8, "name": "Bomrek, miner"}])]
        if function in ("appoint_noble", "vacate_noble"):
            post = next((p for p in self.posts if p["id"] == int(args[0])), None)
            if not post:
                return [json.dumps({"ok": False, "why": "that post is gone"})]
            if post["locked"]:
                return [json.dumps({"ok": False, "why": post["locked"]})]
            if function == "vacate_noble":
                name, post["holder"] = post["holder"], ""
                return [json.dumps({"ok": True, "name": name})]
            post["holder"] = "Bomrek, miner"
            return [json.dumps({"ok": True, "name": "Bomrek, miner"})]
        if function == "squad_targets":
            return [json.dumps([{"id": 1, "name": "Goblin", "x": 4, "y": 4, "z": 5},
                                {"id": 999, "name": "Ambusher", "x": 0, "y": 0, "z": 5}])]
        if function == "squad_station":
            self.squad_list[0]["station"] = dict(zip("xyz", (int(a) for a in args[1:4])))
            self.squad_list[0]["kill"] = 0
            return [json.dumps({"ok": True})]
        if function == "squad_kill":
            self.squad_list[0].pop("station", None)
            self.squad_list[0]["kill"] = len(args[1].split(","))
            return [json.dumps({"ok": True, "count": self.squad_list[0]["kill"]})]
        if function == "squad_clear":
            self.squad_list[0].pop("station", None)
            self.squad_list[0]["kill"] = 0
            return [json.dumps({"ok": True})]
        if function == "squads":
            return [json.dumps(self.squad_list)]
        if function == "squad_candidates":
            return [json.dumps(self.free_units)]
        if function == "squad_add":
            squad, unit = int(args[0]), int(args[1])
            found = next((u for u in self.free_units if u["id"] == unit), None)
            if not found:
                return [json.dumps({"ok": False, "why": "they are already in a squad"})]
            self.free_units.remove(found)
            self.squad_list[0]["members"].append({"slot": 1, "leader": False, "unit": unit,
                                                  "name": found["name"], "away": False})
            return [json.dumps({"ok": True, "name": found["name"]})]
        if function == "squad_remove":
            squad, unit = int(args[0]), int(args[1])
            members = self.squad_list[0]["members"]
            found = next((m for m in members if m["unit"] == unit), None)
            if not found:
                return [json.dumps({"ok": False, "why": "they are not in this squad"})]
            if found["leader"]:
                return [json.dumps({"ok": False, "why": "the leader stays"})]
            members.remove(found)
            return [json.dumps({"ok": True, "name": found["name"]})]
        if function == "rename_squad":
            self.squad_list[0]["alias"] = args[1]
            self.squad_list[0]["name"] = args[1] or "The Axes"
            return [json.dumps({"ok": True, "name": self.squad_list[0]["name"]})]
        if function == "set_order":
            oid, amount, freq, *limit = args
            for o in self.manager_orders:
                if o["id"] == int(oid):
                    if amount:
                        o["left"] = o["total"] = int(amount)
                    if freq:
                        o["freq"] = freq
                    if limit:
                        o["max_workshops"] = int(limit[0])
                    return [json.dumps({"ok": True})]
            return [json.dumps({"ok": False, "missing": True})]
        if function in ("edit_condition", "remove_condition", "add_item_condition", "add_order_condition"):
            return [json.dumps(self.condition_replies.get(function, {"ok": True}))]
        if function == "move_order":
            oid, to = args
            ids = [o["id"] for o in self.manager_orders]
            if int(oid) not in ids:
                return [json.dumps({"ok": False, "missing": True})]
            i = ids.index(int(oid))
            target = {"up": max(i - 1, 0), "down": min(i + 1, len(ids) - 1), "top": 0, "bottom": len(ids) - 1}[to]
            self.manager_orders.insert(target, self.manager_orders.pop(i))
            return [json.dumps({"ok": True})]
        if function == "cancel_order":
            oid = int(args[0])
            if oid in self.busy_orders:
                return [json.dumps({"ok": False, "busy": True})]
            before = len(self.manager_orders)
            self.manager_orders = [o for o in self.manager_orders if o["id"] != oid]
            return [json.dumps({"ok": len(self.manager_orders) < before})]
        if function == "remove":
            x, y, z = (int(a) for a in args[:3])
            want = args[3] if len(args) > 3 else ""
            hit = self.removable.get((x, y, z))
            if hit and want and str(hit[0]) != want:
                hit = None                      # that id doesn't cover this tile
            if hit:
                del self.removable[(x, y, z)]
            if hit is None:
                return [json.dumps({"found": False})]
            bid, btype, outcome = hit
            return [json.dumps({"found": True, "id": bid, "type": btype, "outcome": outcome})]
        if function == "date":
            return [json.dumps({"year": self.year, "tick": self.tick})]
        if function == "apply":
            mode, x, y, z, text, dry = args
            stats = self.apply_stats or {f"{mode}_designated": {"label": "Placed", "value": 1},
                                         f"{mode}_tiles": {"label": "Tiles", "value": 9}}
            return [json.dumps(stats)]
        raise DFHackRPCError("no such function", code=3)

    def set_paused(self, value):
        self.pause_calls.append(value)
        self.is_paused = value

    def building_defs(self):
        return rfr.BuildingList()

    def view_info(self):
        return rfr.ViewInfo(view_pos_x=0, view_pos_y=0, view_pos_z=5, view_size_x=10, view_size_y=10)

    def paused(self):
        if self.fail:
            raise DFHackRPCError("connection closed by DFHack")
        return self.is_paused

    def units(self):
        if self.fail:
            raise DFHackRPCError("connection closed by DFHack")
        ul = rfr.UnitList()
        ul.creature_list.extend(self.unit_list)
        return ul

    def blocks(self, min_x, max_x, min_y, max_y, min_z, max_z, blocks_needed=256, force_reload=False):
        if self.fail:
            raise DFHackRPCError("connection closed by DFHack")
        self.block_calls.append((min_x, max_x, min_y, max_y, min_z, max_z, blocks_needed, force_reload))
        bl = rfr.BlockList()
        for z in range(min_z, max_z):
            for bx in range(min_x, max_x):
                for by in range(min_y, max_y):
                    if not force_reload and (bx, by, z) not in self.dirty:
                        continue
                    self.dirty.discard((bx, by, z))
                    b = bl.map_blocks.add(map_x=bx * 16, map_y=by * 16, map_z=z)
                    coords = [(bx * 16 + i % 16, by * 16 + i // 16) for i in range(N)]
                    b.tiles.extend([self.tile(x, y, z) for x, y in coords])
                    b.hidden.extend([(x, y, z) in {(0, 0, 5), (31, 15, 5)} for x, y in coords])
                    b.water.extend([0] * N)
                    b.magma.extend([0] * N)
                    b.tile_dig_designation.extend([self.designated.get((x, y, z), 0) for x, y in coords])
        assert len(bl.map_blocks) <= blocks_needed, "request truncated the fake map reply"
        return bl


async def start(df=None, **kw):
    df = df or FakeDF()
    kw.setdefault("data_dir", Path(tempfile.mkdtemp(prefix="df-coop-test-")))
    hub = Hub(df, TOKENS, poll_interval=0.02, unit_interval=0.02, **kw)
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(hub.serve("127.0.0.1", 0, stop, ready=ready.set_result))
    server = await asyncio.wait_for(ready, 5)
    port = server.sockets[0].getsockname()[1]
    return hub, df, stop, task, port


async def finish(stop, task):
    stop.set()
    await asyncio.wait_for(task, 5)


async def recv_until(ws, mtype, pred=lambda m: True, timeout=3.0):
    async def loop():
        while True:
            m = json.loads(await ws.recv())
            if m["type"] == mtype and pred(m):
                return m
    return await asyncio.wait_for(loop(), timeout)


def run(coro):
    return asyncio.run(coro)


def test_unknown_token_is_refused_at_the_handshake():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            with pytest.raises(InvalidStatus) as err:
                async with connect(f"ws://127.0.0.1:{port}/ws?token=nope"):
                    pass
            assert err.value.response.status_code == 403
        finally:
            await finish(stop, task)
    run(go())


def test_static_files_are_served_and_traversal_is_not(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    (tmp_path.parent / "secret.txt").write_text("nope")

    async def get(port, path):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        writer.close()
        return head.split(b" ")[1]

    async def go():
        hub, df, stop, task, port = await start(static_dir=tmp_path)
        try:
            assert await get(port, "/") == b"200"
            assert await get(port, "/../secret.txt") == b"404"
            assert await get(port, "/missing.js") == b"404"
        finally:
            await finish(stop, task)
    run(go())


def test_join_gets_map_tiles_and_blocks_with_hidden_tile_masked():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["you"]["name"] == "Alice"
                assert hello["map"] == {"w": 32, "h": 32, "d": 10}
                assert hello["view"]["z"] == 5
                assert hello["tiles"][3][2] == "stone floor"
                blocks = {}
                while len(blocks) < 8:  # 4 blocks on each of levels 5 and 4
                    m = await recv_until(ws, "blocks")
                    for b in m["blocks"]:
                        blocks[(b["x"], b["y"], b["z"])] = b
                assert blocks[(0, 0, 5)]["t"][0] == -1
                assert blocks[(0, 0, 5)]["t"][1] == 3
                assert blocks[(0, 0, 4)]["t"][0] == 3
        finally:
            await finish(stop, task)
    run(go())


def test_units_exclude_ambushers_and_units_on_hidden_tiles():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                m = await recv_until(ws, "units", lambda m: m["units"])
                assert [u["n"] for u in m["units"]] == ["Urist"]
                assert m["paused"] is True
        finally:
            await finish(stop, task)
    run(go())


def test_players_see_each_others_cursors():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await b.send(json.dumps({"type": "view", "z": 5, "cx": 9, "cy": 11}))

                def bob_moved(m):
                    return any(p["name"] == "Bob" and (p["cx"], p["cy"]) == (9, 11) for p in m["players"])
                m = await recv_until(a, "players", bob_moved)
                assert {p["name"] for p in m["players"]} == {"Alice", "Bob"}
                assert len({p["color"] for p in m["players"]}) == 2
        finally:
            await finish(stop, task)
    run(go())


def test_changing_level_polls_the_new_levels_and_sends_them():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "view", "z": 8, "cx": 0, "cy": 0}))
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 8 for b in m["blocks"]))
                assert (0, 2, 0, 2, 7, 9, 9, True) in df.block_calls
                # Levels nobody watches any more are not polled.
                df.block_calls.clear()
                await asyncio.sleep(0.15)
                assert {z for call in df.block_calls for z in range(call[4], call[5])} <= {8, 7}
        finally:
            await finish(stop, task)
    run(go())


@pytest.fixture
def polling_hub(monkeypatch, tmp_path):
    """Drive one polling tick deterministically, with no socket or game."""
    df = FakeDF()
    hub = Hub(df, TOKENS, data_dir=tmp_path, df_dir=tmp_path)
    run(hub.setup())
    for sid, (name, z) in enumerate((("Alice", 5), ("Bob", 6)), 1):
        s = Session(object(), sid, name, "#ffffff", z=z)
        hub.sessions[s.conn] = s
    sent = []
    monkeypatch.setattr("hub.broadcast", lambda conns, raw: sent.append((tuple(conns), json.loads(raw))))
    return hub, df, sent


def test_batched_blocks_keep_each_players_levels_visibility_and_owners(polling_hub):
    hub, df, sent = polling_hub
    cells = [(1, 1, z) for z in (4, 5, 6, 8)]
    hub.owners.apply("channel", cells, "Alice")
    df.designated[(1, 1, 5)] = 3
    run(hub.poll_blocks())
    assert df.block_calls == [(0, 2, 0, 2, 4, 6, 9, True), (0, 2, 0, 2, 6, 7, 5, True)]
    assert hub.fetched_z == {4, 5, 6}
    assert set(hub.owners.by_tile) == {(1, 1, 5), (1, 1, 8)}
    for s in hub.sessions.values():
        blocks = [b for conns, m in sent if s.conn in conns and m["type"] == "blocks" for b in m["blocks"]]
        assert {b["z"] for b in blocks} == s.zs
        assert len(blocks) == 8
        hidden = next(b for b in blocks if (b["x"], b["y"], b["z"]) == (0, 0, 5))
        assert hidden["t"][0] == -1
        owner_messages = [m for conns, m in sent if s.conn in conns and m["type"] == "owners"]
        assert owner_messages
        assert all(row[2] in s.zs for m in owner_messages for row in m["owners"])
    df.block_calls.clear()
    sent.clear()
    run(hub.poll_blocks())
    assert df.block_calls == [(0, 2, 0, 2, 4, 6, 9, False), (0, 2, 0, 2, 6, 7, 5, False)]
    assert not sent


def test_batched_blocks_split_first_reads_from_warm_levels(polling_hub):
    hub, df, _ = polling_hub
    bob = next(s for s in hub.sessions.values() if s.name == "Bob")
    hub.sessions.pop(bob.conn)
    run(hub.poll_blocks())
    assert hub.fetched_z == {4, 5}
    df.block_calls.clear()
    hub.sessions[bob.conn] = bob
    run(hub.poll_blocks())
    assert df.block_calls == [(0, 2, 0, 2, 4, 6, 9, False), (0, 2, 0, 2, 6, 7, 5, True)]
    assert hub.fetched_z == {4, 5, 6}


@pytest.mark.parametrize("levels, ranges", [
    ((0, 9), [(0, 1), (8, 10)]),
    ((1, 5), [(0, 2), (4, 6)]),
    ((1, 3, 5), [(0, 2), (2, 4), (4, 6)]),
])
def test_batched_block_bounds_and_reply_budget(polling_hub, levels, ranges):
    hub, df, _ = polling_hub
    hub.sessions.clear()
    for sid, z in enumerate(levels, 1):
        s = Session(object(), sid, f"Player {sid}", "#ffffff", z=z)
        hub.sessions[s.conn] = s
    run(hub.poll_blocks())
    assert df.block_calls == [(0, 2, 0, 2, lo, hi, 4 * (hi - lo) + 1, True) for lo, hi in ranges]
    assert hub.fetched_z == {z for lo, hi in ranges for z in range(lo, hi)}


def test_time_holding_df_is_measured_and_logged(polling_hub, caplog):
    hub, df, _ = polling_hub
    hub.rpc_stats = RpcStats(every=0)
    hub.poll_interval = 0.01

    async def briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(hub.run_polling(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    with caplog.at_level(logging.INFO, logger="df-coop"):
        run(briefly())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("DF calls took")]
    assert lines
    assert any("blocks " in line for line in lines)
    assert any("lua:date " in line for line in lines)


def count_calls(df, name):
    """Calls to one of the fake's RPCs; "units" counts coop.lua's units read, which
    replaced RemoteFortressReader's from module v19."""
    calls = []
    if name == "units":
        original = df.run_lua

        def counted_lua(module, function, *args):
            if function == "units":
                calls.append(args)
            return original(module, function, *args)

        df.run_lua = counted_lua
        return calls
    original = getattr(df, name)

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    setattr(df, name, counted)
    return calls


def poll_for(hub, seconds, during=None):
    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(hub.run_polling(stop))
        await asyncio.sleep(seconds)
        if during:
            during()
            await asyncio.sleep(seconds)
        stop.set()
        await task
    run(go())


def test_the_map_is_read_on_its_own_slower_timer_and_units_on_theirs(polling_hub):
    hub, df, _ = polling_hub
    blocks, units = count_calls(df, "blocks"), count_calls(df, "units")
    hub.poll_interval, hub.unit_interval = 10.0, 0.05
    poll_for(hub, 0.3)
    assert len(blocks) == 2     # one pass, over this fixture's two ranges of levels
    assert len(units) >= 4


def test_pause_state_is_read_every_other_unit_poll(polling_hub):
    hub, df, _ = polling_hub
    units, paused = count_calls(df, "units"), count_calls(df, "paused")
    hub.poll_interval, hub.unit_interval = 10.0, 0.1
    poll_for(hub, 0.65)
    assert len(units) >= 4
    assert len(paused) <= len(units) / 2 + 1


def test_a_map_read_can_be_asked_for_but_not_too_often(polling_hub):
    hub, df, _ = polling_hub
    blocks = count_calls(df, "blocks")
    hub.poll_interval, hub.unit_interval = 10.0, 10.0
    seen = []

    def ask():
        seen.append(len(blocks))
        hub._poll_blocks_soon()
        hub._poll_blocks_soon()

    poll_for(hub, 0.4, during=ask)
    assert seen == [2]
    assert len(blocks) == 4


def test_playing_or_looking_elsewhere_reads_the_map_soon():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await asyncio.sleep(0.1)
                hub.poll_interval = 100.0
                await asyncio.sleep(0.1)
                blocks = count_calls(df, "blocks")
                await asyncio.sleep(0.5)
                assert blocks == []
                await ws.send(json.dumps({"type": "dig", "kind": "dig", "x1": 3, "y1": 3, "x2": 3, "y2": 3, "z": 5}))
                await asyncio.sleep(0.5)
                assert blocks
                blocks.clear()
                await asyncio.sleep(0.5)
                assert blocks == []
                await ws.send(json.dumps({"type": "view", "z": 20, "cx": 3, "cy": 3}))
                await asyncio.sleep(0.5)
                assert blocks
        finally:
            await finish(stop, task)
    run(go())


def test_polling_slows_only_when_every_tab_is_hidden(polling_hub):
    hub, df, _ = polling_hub
    first, second = hub.sessions.values()
    assert hub._slowdown() == 1
    hub._on_visibility(first, True)
    assert hub._slowdown() == 1
    hub._on_visibility(second, True)
    assert hub._slowdown() == 4

    units = count_calls(df, "units")
    hub.poll_interval, hub.unit_interval = 0.05, 0.05
    poll_for(hub, 0.5)
    slow = len(units)
    units.clear()
    hub._on_visibility(first, False)
    assert hub._slowdown() == 1
    assert hub._blocks_due == hub._units_due == hub._paused_due == 0.0     # catches up at once
    poll_for(hub, 0.5)
    assert len(units) > slow * 2


def test_malformed_visibility_is_ignored():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "visibility", "hidden": "yes"}))
                await ws.send(json.dumps({"type": "visibility"}))
                await ws.send(json.dumps({"type": "visibility", "hidden": True}))
                await asyncio.sleep(0.1)
                assert hub._slowdown() == 4
        finally:
            await finish(stop, task)
    run(go())


def test_citizens_are_readable_and_any_role_can_ask():
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "visitor"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert (await recv_until(ws, "hello"))["can_citizens"]
                await ws.send(json.dumps({"type": "citizens"}))
                r = await recv_until(ws, "citizens")
                assert r["ok"]
                assert r["citizens"] == [
                    {"id": 7, "name": "Urist, miner", "profession": "Miner", "job": "Dig", "stress": 1,
                     "squad": "The Axes", "mood": "Very unhappy", "x": 3, "y": 4, "z": 5},
                    {"id": 8, "name": "Bomrek", "profession": "Peasant", "job": "", "stress": -1,
                     "squad": "", "mood": ""},
                ]
        finally:
            await finish(stop, task)
    run(go())


def test_players_share_one_read_of_the_citizens():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                for ws in (a, b, a):
                    await ws.send(json.dumps({"type": "citizens"}))
                    assert (await recv_until(ws, "citizens"))["ok"]
                assert len([c for c in df.lua_calls if c[1] == "citizens"]) == 1
                hub._citizens_read = float("-inf")
                await a.send(json.dumps({"type": "citizens"}))
                await recv_until(a, "citizens")
                assert len([c for c in df.lua_calls if c[1] == "citizens"]) == 2
        finally:
            await finish(stop, task)
    run(go())


def test_citizens_need_the_new_module_and_survive_a_failed_read():
    async def go():
        df = FakeDF()
        df.lua_version = "13"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert not (await recv_until(ws, "hello"))["can_citizens"]
                await ws.send(json.dumps({"type": "citizens"}))
                assert "install-lua" in (await recv_until(ws, "citizens"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "citizens"]
        finally:
            await finish(stop, task)
        df = FakeDF()
        df.citizen_list = None
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "citizens"}))
                r = await recv_until(ws, "citizens")
                assert not r["ok"] and "refused" in r["error"]
                assert hub.df_online
        finally:
            await finish(stop, task)
    run(go())


def test_work_details_are_readable_by_anyone_and_shared():
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "visitor"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_work_details"]
                assert {"id": "NobodyDoesThis", "label": "Nobody"} in hello["work_detail_modes"]
                for _ in range(2):
                    await ws.send(json.dumps({"type": "work_details"}))
                    r = await recv_until(ws, "work_details")
                    assert r["ok"] and r["details"][0]["labors"] == ["Mine", "Haul stone"]
                    assert r["people"][1]["name"] == "Bomrek"
                assert len([c for c in df.lua_calls if c[1] == "work_details"]) == 1
        finally:
            await finish(stop, task)
    run(go())


def test_changing_a_work_detail_reaches_df_is_logged_and_refreshes():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "work_details"}))
                await recv_until(ws, "work_details")
                await ws.send(json.dumps({"type": "work_detail_member", "detail": 0, "name": "Miners", "unit": 8, "on": True}))
                assert (await recv_until(ws, "work_detail_result"))["ok"]
                assert lua_call(df, "work_detail_member") == ("rpc.coop", "work_detail_member", "0", "Miners", "8", "1")
                await ws.send(json.dumps({"type": "work_detail_mode", "detail": 0, "name": "Miners", "mode": "NobodyDoesThis"}))
                assert (await recv_until(ws, "work_detail_result"))["ok"]
                assert lua_call(df, "work_detail_mode") == ("rpc.coop", "work_detail_mode", "0", "Miners", "NobodyDoesThis")
                logged = [e for e in hub.action_log.entries() if e["type"].startswith("work_detail")]
                assert [(e["player"], e["detail"]) for e in logged] == [("Alice", "Miners")] * 2
                assert logged[0]["unit"] == 8 and logged[0]["on"] is True and logged[1]["mode"] == "NobodyDoesThis"
                await ws.send(json.dumps({"type": "work_details"}))           # the change isn't hidden by the cache
                await recv_until(ws, "work_details")
                assert len([c for c in df.lua_calls if c[1] == "work_details"]) == 2
                df.detail_reply = {"ok": False, "why": "they already use a tool"}
                await ws.send(json.dumps({"type": "work_detail_member", "detail": 0, "name": "Miners", "unit": 8, "on": True}))
                r = await recv_until(ws, "work_detail_result")
                assert not r["ok"] and "tool" in r["error"]
                assert len([e for e in hub.action_log.entries() if e["type"].startswith("work_detail")]) == 2
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    ({"type": "work_detail_member", "detail": "0", "name": "Miners", "unit": 8, "on": True}, "malformed work detail"),
    ({"type": "work_detail_member", "detail": 0, "unit": 8, "on": True}, "malformed work detail"),
    ({"type": "work_detail_member", "detail": 0, "name": "x" * 101, "unit": 8, "on": True}, "malformed work detail"),
    ({"type": "work_detail_member", "detail": 0, "name": "Miners", "unit": "8", "on": True}, "malformed change"),
    ({"type": "work_detail_member", "detail": 0, "name": "Miners", "unit": 8, "on": 1}, "malformed change"),
    ({"type": "work_detail_mode", "detail": 0, "name": "Miners", "mode": "Default"}, "isn't a setting"),
    ({"type": "work_detail_mode", "detail": 0, "name": "Miners", "mode": ["NobodyDoesThis"]}, "isn't a setting"),
])
def test_bad_work_detail_changes_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                r = await recv_until(ws, "work_detail_result")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if c[1].startswith("work_detail_")]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("overseer", True), ("quartermaster", True), ("builder", False),
                                          ("miner", False), ("commander", False), ("visitor", False)])
def test_work_details_need_the_labor_permission(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "work_detail_mode", "detail": 0, "name": "Miners", "mode": "NobodyDoesThis"}))
                if allowed:
                    assert (await recv_until(ws, "work_detail_result"))["ok"]
                else:
                    assert (await recv_until(ws, "denied"))["action"] == "work_detail_mode"
                    assert not [c for c in df.lua_calls if c[1] == "work_detail_mode"]
        finally:
            await finish(stop, task)
    run(go())


def test_work_details_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "14"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert not (await recv_until(ws, "hello"))["can_work_details"]
                await ws.send(json.dumps({"type": "work_details"}))
                assert "install-lua" in (await recv_until(ws, "work_details"))["error"]
                await ws.send(json.dumps({"type": "work_detail_mode", "detail": 0, "name": "Miners", "mode": "NobodyDoesThis"}))
                assert "install-lua" in (await recv_until(ws, "work_detail_result"))["error"]
                assert not [c for c in df.lua_calls if c[1].startswith("work_detail")]
        finally:
            await finish(stop, task)
    run(go())


def test_a_failed_df_call_is_still_timed(polling_hub):
    hub, df, _ = polling_hub

    def broken():
        raise DFHackRPCError("gone")

    hub.rpc_stats = RpcStats()
    with pytest.raises(DFHackRPCError):
        run(hub._df(broken))
    assert hub.rpc_stats.calls["broken"][0] == 1


def test_buildings_broadcast_once_after_all_polled_ranges(polling_hub):
    hub, df, sent = polling_hub
    blocks = df.blocks

    def with_buildings(*args, **kwargs):
        reply = blocks(*args, **kwargs)
        for bid, z in ((10, 5), (11, 6)):
            b = reply.map_blocks[0].buildings.add(index=bid)
            if args[4] <= z < args[5]:
                b.pos_x_min = b.pos_x_max = 2
                b.pos_y_min = b.pos_y_max = 2
                b.pos_z_min = b.pos_z_max = z
        return reply

    df.blocks = with_buildings
    run(hub.poll_blocks())
    assert set(hub.state.buildings) == {10, 11}
    for s in hub.sessions.values():
        messages = [m for conns, m in sent if s.conn in conns and m["type"] == "buildings"]
        assert len(messages) == 1
        assert {b["i"] for b in messages[0]["buildings"]} == ({10} if s.z == 5 else {10, 11})


def test_unit_json_shared_by_level_set_but_delivery_stays_per_session(polling_hub, monkeypatch):
    hub, df, sent = polling_hub
    run(hub.poll_blocks())
    df.unit_list.append(rfr.UnitDefinition(id=4, pos_x=4, pos_y=4, pos_z=6, name="Domas"))
    third = Session(object(), 3, "Carol", "#ffffff", z=5)
    hub.sessions[third.conn] = third
    dumps = json.dumps
    encoded_units = []

    def count_units(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("type") == "units":
            encoded_units.append(value)
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr("hub.json.dumps", count_units)
    sent.clear()
    run(hub.poll_units())
    assert len(encoded_units) == 2
    for s in hub.sessions.values():
        messages = [m for conns, m in sent if s.conn in conns and m["type"] == "units"]
        assert len(messages) == 1
        assert {u["i"] for u in messages[0]["units"]} == s.visible_units == ({1} if s.z == 5 else {1, 4})
    sent.clear()
    encoded_units.clear()
    run(hub.poll_units())
    assert len(encoded_units) == 2
    assert not [m for _, m in sent if m["type"] == "units"]
    third.last_units = ""
    run(hub.poll_units())
    assert [(conns, m["type"]) for conns, m in sent if m["type"] == "units"] == [((third.conn,), "units")]


def test_malformed_view_messages_are_ignored():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                for bad in ("not json", json.dumps([1]), json.dumps({"type": "view", "z": True, "cx": 0, "cy": 0}),
                            json.dumps({"type": "view", "z": "5", "cx": 0, "cy": 0}), "x" * 5000):
                    await ws.send(bad)
                await ws.send(json.dumps({"type": "view", "z": 999, "cx": 1, "cy": 2}))
                m = await recv_until(ws, "players", lambda m: m["players"][0]["cx"] == 1)
                assert m["players"][0]["z"] == 9  # clamped to the map
        finally:
            await finish(stop, task)
    run(go())


def test_losing_dfhack_tells_players():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                df.fail = True
                m = await recv_until(ws, "status")
                assert m["df"] == "offline"
        finally:
            await finish(stop, task)
    run(go())


def test_hub_starts_before_a_fortress_is_loaded_and_connects_when_one_is():
    async def go():
        df = FakeDF()
        df.no_map_calls = 2
        hub = Hub(df, TOKENS, poll_interval=0.02, unit_interval=0.02, data_dir=Path(tempfile.mkdtemp()))
        hub._offline_wait = 0.05
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(hub.serve("127.0.0.1", 0, stop, ready=ready.set_result))
        server = await asyncio.wait_for(ready, 5)
        port = server.sockets[0].getsockname()[1]
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                first = await recv_until(ws, "hello")
                assert first["df"] == "offline" and first["map"]["w"] == 0
                await recv_until(ws, "status", lambda m: m["df"] == "online")
                second = await recv_until(ws, "hello")
                assert second["map"] == {"w": 32, "h": 32, "d": 10}
                await recv_until(ws, "blocks")
        finally:
            await finish(stop, task)
    run(go())


def test_players_added_while_the_hub_runs_can_join(tmp_path):
    path = tmp_path / "players.json"
    add_player("Alice", path)

    async def go():
        df = FakeDF()
        hub = Hub(df, load_players(path), players_path=path, poll_interval=0.02, unit_interval=0.02,
                  data_dir=tmp_path)
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(hub.serve("127.0.0.1", 0, stop, ready=ready.set_result))
        server = await asyncio.wait_for(ready, 5)
        port = server.sockets[0].getsockname()[1]
        try:
            token = add_player("Jeremy", path)
            async with connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["you"]["name"] == "Jeremy"
        finally:
            await finish(stop, task)
    run(go())


def test_add_player_writes_a_private_token_file(tmp_path):
    path = tmp_path / "players.json"
    token = add_player("Carol", path)
    assert load_players(path) == {token: "Carol"}
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit):
        add_player("Carol", path)


# -- M1: digging ----------------------------------------------------------------

def dig_msg(kind, x1, y1, x2, y2, z=5):
    return json.dumps({"type": "dig", "kind": kind, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z})


def test_dig_applies_only_where_it_fits_and_reports_counts():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                # x 20-26 on row 0: rock walls at 20-23, a constructed wall at 24,
                # floor at 25-26. Only the rock walls can be dug.
                await ws.send(dig_msg("dig", 20, 0, 26, 0))
                r = await recv_until(ws, "dig_result")
                assert r["ok"] and (r["applied"], r["skipped"]) == (4, 3)
                designation, tiles = df.dig_calls[-1]
                assert designation == rfr.DEFAULT_DIG
                assert sorted(tiles) == [(20, 0, 5), (21, 0, 5), (22, 0, 5), (23, 0, 5)]
        finally:
            await finish(stop, task)
    run(go())


def test_hidden_tiles_take_designations_without_revealing_what_they_are():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(dig_msg("ramp", 31, 15, 31, 15))   # hidden floor
                r = await recv_until(ws, "dig_result")
                assert (r["applied"], r["skipped"]) == (1, 0)
        finally:
            await finish(stop, task)
    run(go())


def test_designations_are_attributed_to_everyone_on_that_level_and_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await recv_until(b, "hello")
                await a.send(dig_msg("channel", 2, 2, 3, 2))
                m = await recv_until(b, "owners", lambda m: m["owners"])
                assert sorted((x, y, z, n, p) for x, y, z, n, _, p in m["owners"]) == [(2, 2, 5, "Alice", 4), (3, 2, 5, "Alice", 4)]
                # The designation reaches the map the next poll, owner-coloured on the client.
                blk = await recv_until(b, "blocks", lambda m: any(x.get("d") for x in m["blocks"]))
                assert blk["blocks"][0]["d"][2 * 16 + 2] == rfr.CHANNEL_DIG
                entries = list(hub.action_log.entries())
                assert entries[-1]["player"] == "Alice" and entries[-1]["rect"] == [2, 2, 3, 2]
        finally:
            await finish(stop, task)
    run(go())


def test_finished_designations_lose_their_owner():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(dig_msg("dig", 20, 1, 20, 1))
                await recv_until(ws, "owners", lambda m: m["owners"])
                # A dwarf digs it out: DF clears the designation.
                df.designated.pop((20, 1, 5))
                df.dirty.add((1, 0, 5))
                await recv_until(ws, "owners", lambda m: not m["owners"])
                assert (20, 1, 5) not in hub.owners.by_tile
        finally:
            await finish(stop, task)
    run(go())


def test_owners_survive_a_hub_restart_through_the_log(tmp_path):
    async def go():
        df = FakeDF()
        df.designated[(21, 3, 5)] = rfr.DEFAULT_DIG
        (tmp_path / "Testhold-21x33.actions.jsonl").write_text(json.dumps(
            {"type": "dig", "player": "Bob", "kind": "dig", "rect": [21, 3, 21, 3], "z": 5}) + "\n")
        hub, df, stop, task, port = await start(df, data_dir=tmp_path)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                m = await recv_until(ws, "owners", lambda m: m["owners"])
                assert [(x, y, z, n) for x, y, z, n, _, _ in m["owners"]] == [(21, 3, 5, "Bob")]
        finally:
            await finish(stop, task)
    run(go())


def test_logged_orders_already_dug_out_are_dropped_on_restart(tmp_path):
    async def go():
        df = FakeDF()   # nothing designated: the order was finished while the hub was down
        (tmp_path / "Testhold-21x33.actions.jsonl").write_text(json.dumps(
            {"type": "dig", "player": "Bob", "kind": "dig", "rect": [21, 3, 21, 3], "z": 5}) + "\n")
        hub, df, stop, task, port = await start(df, data_dir=tmp_path)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await recv_until(ws, "owners", lambda m: not m["owners"])
                assert hub.owners.by_tile == {}
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    (dig_msg("dig", 0, 0, 31, 31, z=99), "outside the map"),
    (dig_msg("explode", 0, 0, 1, 1), "malformed"),
    (json.dumps({"type": "dig", "kind": "dig", "x1": "0", "y1": 0, "x2": 1, "y2": 1, "z": 5}), "malformed"),
    (dig_msg("dig", 100, 100, 200, 200), "outside the map"),
])
def test_bad_orders_are_refused(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(msg)
                r = await recv_until(ws, "dig_result")
                assert not r["ok"] and error in r["error"]
                assert df.dig_calls == []
        finally:
            await finish(stop, task)
    run(go())


def test_orders_over_the_size_limit_are_refused(monkeypatch):
    import hub as hub_module
    monkeypatch.setattr(hub_module, "MAX_DIG_TILES", 50)

    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(dig_msg("dig", 0, 0, 9, 9))
                r = await recv_until(ws, "dig_result")
                assert not r["ok"] and "too big" in r["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_spamming_orders_is_rate_limited():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                for _ in range(15):
                    await ws.send(dig_msg("erase", 0, 0, 0, 0))
                results = [await recv_until(ws, "dig_result") for _ in range(15)]
                refused = [r for r in results if not r["ok"]]
                assert refused and all("slow down" in r["error"] for r in refused)
        finally:
            await finish(stop, task)
    run(go())


# -- M1: pausing ----------------------------------------------------------------

def test_game_resumes_only_when_everyone_who_paused_is_ready():
    async def go():
        df = FakeDF()
        df.is_paused = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "pause", "value": True}))
                await recv_until(b, "pause", lambda m: m["holders"] == ["Alice"])
                await b.send(json.dumps({"type": "pause", "value": True}))
                await recv_until(a, "pause", lambda m: m["holders"] == ["Alice", "Bob"])
                await a.send(json.dumps({"type": "pause", "value": False}))
                m = await recv_until(b, "pause", lambda m: m["holders"] == ["Bob"])
                assert m["paused"] and df.is_paused
                await b.send(json.dumps({"type": "pause", "value": False}))
                m = await recv_until(a, "pause", lambda m: not m["paused"])
                assert df.pause_calls == [True, False] and not df.is_paused
        finally:
            await finish(stop, task)
    run(go())


def test_a_player_who_leaves_while_holding_does_not_strand_the_others():
    async def go():
        df = FakeDF()
        df.is_paused = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:
                await recv_until(a, "hello")
                async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                    await recv_until(b, "hello")
                    await b.send(json.dumps({"type": "pause", "value": True}))
                    await recv_until(a, "pause", lambda m: m["holders"] == ["Bob"])
                await recv_until(a, "pause", lambda m: not m["paused"])
                assert not df.is_paused
        finally:
            await finish(stop, task)
    run(go())


def test_anyone_can_resume_a_pause_nobody_is_holding():
    async def go():
        hub, df, stop, task, port = await start()   # DF starts paused on its own
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(b, "pause", lambda m: m["paused"] and not m["holders"])
                await b.send(json.dumps({"type": "pause", "value": False}))
                await recv_until(b, "pause", lambda m: not m["paused"])
                assert df.pause_calls == [False]
        finally:
            await finish(stop, task)
    run(go())


def test_unpausing_in_df_itself_overrides_holds():
    async def go():
        df = FakeDF()
        df.is_paused = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:
                await recv_until(a, "hello")
                await a.send(json.dumps({"type": "pause", "value": True}))
                await recv_until(a, "pause", lambda m: m["holders"] == ["Alice"])
                df.is_paused = False          # the host hits space in DF
                await recv_until(a, "pause", lambda m: not m["paused"])
                assert hub.holders == {} and not hub.hold_active
        finally:
            await finish(stop, task)
    run(go())


# -- M2: building ---------------------------------------------------------------

def build_msg(item, x1, y1, x2=None, y2=None, z=5):
    m = {"type": "build", "item": item, "x1": x1, "y1": y1, "z": z}
    if x2 is not None:
        m.update(x2=x2, y2=y2)
    return json.dumps(m)


def test_hello_carries_the_catalog_and_whether_building_works():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_build"] is True
                ids = {i["id"] for i in hello["catalog"]["items"]}
                assert {"bed", "carpenter", "sp_food", "z_meeting"} <= ids
                assert hello["date"] == "12th Malachite, year 23"
        finally:
            await finish(stop, task)
    run(go())


def test_fixed_item_is_sent_as_its_bare_key_at_the_clicked_tile():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(build_msg("carpenter", 10, 11))
                r = await recv_until(ws, "build_result")
                assert r["ok"] and r["placed"] == 1 and r["label"] == "Carpenter"
                assert lua_call(df, "apply") == ("rpc.coop", "apply", "build", "10", "11", "5", "wc", "0")
                entry = list(hub.action_log.entries())[-1]
                assert entry["type"] == "build" and entry["item"] == "carpenter" and entry["player"] == "Alice"
        finally:
            await finish(stop, task)
    run(go())


def test_area_item_is_anchored_top_left_with_its_size():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(build_msg("sp_food", 9, 7, 5, 4))     # corners in any order
                await recv_until(ws, "build_result")
                assert lua_call(df, "apply") == ("rpc.coop", "apply", "place", "5", "4", "5", "f(5x4)", "0")
        finally:
            await finish(stop, task)
    run(go())


def test_quickfort_problems_reach_the_player():
    async def go():
        df = FakeDF()
        df.apply_stats = {"build_designated": {"label": "Buildings designated", "value": 0},
                          "build_unsuitable": {"label": "Unsuitable tiles", "value": 9}}
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(build_msg("mason", 3, 3))
                r = await recv_until(ws, "build_result")
                assert r["ok"] and r["placed"] == 0 and r["problems"] == ["Unsuitable tiles: 9"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    (build_msg("sp_food", 0, 0, 31, 31), "too big"),
    (build_msg("nuke", 1, 1), "unknown item"),
    (build_msg("bed", 1, 1, z=99), "outside the map"),
    (json.dumps({"type": "build", "item": "bed", "x1": 1.5, "y1": 1, "z": 5}), "malformed"),
    (build_msg("sp_food", 1, 1), "malformed"),     # area items need both corners
])
def test_bad_build_orders_are_refused_before_dfhack(msg, error, monkeypatch):
    import hub as hub_module
    monkeypatch.setattr(hub_module, "MAX_AREA_SIDE", 20)   # the 32x32 test map would fit a 31x31 order

    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(msg)
                r = await recv_until(ws, "build_result")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if c[1] == "apply"]
        finally:
            await finish(stop, task)
    run(go())


def test_without_the_lua_module_digging_works_and_building_says_why_not():
    async def go():
        df = FakeDF()
        df.lua_installed = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_build"] is False and hello["date"] == ""
                await ws.send(build_msg("bed", 1, 1))
                r = await recv_until(ws, "build_result")
                assert not r["ok"] and "install-lua" in r["error"]
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(dig_msg("dig", 20, 0, 20, 0))
                assert (await recv_until(ws, "dig_result"))["ok"]
        finally:
            await finish(stop, task)
    run(go())


def test_date_changes_reach_players():
    async def go():
        hub, df, stop, task, port = await start()
        hub._date_due = 0
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                df.tick += 1200
                hub._date_due = 0
                m = await recv_until(ws, "date", lambda m: m["date"].startswith("13th"))
                assert m["date"] == "13th Malachite, year 23"
        finally:
            await finish(stop, task)
    run(go())


# -- M2: removing ---------------------------------------------------------------

def remove_msg(x, y, z=5):
    return json.dumps({"type": "remove", "x": x, "y": y, "z": z})


def test_remove_reports_what_happened_and_logs_it():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(remove_msg(4, 4))
                r = await recv_until(ws, "remove_result")
                assert r == {"type": "remove_result", "ok": True, "what": "Bed", "outcome": "removed"}
                await ws.send(remove_msg(9, 9))
                r = await recv_until(ws, "remove_result")
                assert r["outcome"] == "queued"          # a finished building gets a deconstruct job
                await ws.send(remove_msg(9, 9))
                r = await recv_until(ws, "remove_result")
                assert r["outcome"] == "nothing"
                entries = [e for e in hub.action_log.entries() if e["type"] == "remove"]
                assert [(e["player"], e["what"], e["outcome"]) for e in entries] == [
                    ("Alice", "Bed", "removed"), ("Alice", "Workshop", "queued")]
        finally:
            await finish(stop, task)
    run(go())


def test_remove_on_a_hidden_tile_says_nothing_without_asking_df():
    async def go():
        hub, df, stop, task, port = await start()
        df.removable[(0, 0, 5)] = (9, "Coffin", "removed")   # under the hidden tile
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(remove_msg(0, 0))
                r = await recv_until(ws, "remove_result")
                assert r["outcome"] == "nothing"
                assert not [c for c in df.lua_calls if c[1] == "remove"]
                assert (0, 0, 5) in df.removable
        finally:
            await finish(stop, task)
    run(go())


def test_an_old_lua_module_keeps_building_but_asks_for_an_update_to_remove():
    async def go():
        df = FakeDF()
        df.lua_version = "1"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_build"]
                await ws.send(remove_msg(4, 4))
                r = await recv_until(ws, "remove_result")
                assert not r["ok"] and "install-lua" in r["error"]
                await ws.send(build_msg("bed", 2, 2))
                assert (await recv_until(ws, "build_result"))["ok"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    (remove_msg(99, 1), "outside the map"),
    (json.dumps({"type": "remove", "x": "4", "y": 4, "z": 5}), "malformed"),
    (json.dumps({"type": "remove", "x": 4, "y": 4}), "malformed"),
])
def test_bad_remove_orders_are_refused(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(msg)
                r = await recv_until(ws, "remove_result")
                assert not r["ok"] and error in r["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_installing_the_module_while_the_hub_runs_turns_building_on():
    async def go():
        df = FakeDF()
        df.lua_installed = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert (await recv_until(ws, "hello"))["can_build"] is False
                df.lua_installed = True          # the host runs install-lua
                hub._lua_recheck_due = 0
                assert (await recv_until(ws, "hello", lambda m: m["can_build"]))["can_build"]
        finally:
            await finish(stop, task)
    run(go())


def test_remove_by_id_takes_only_that_building():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(json.dumps({"type": "remove", "x": 4, "y": 4, "z": 5, "id": 99}))
                assert (await recv_until(ws, "remove_result"))["outcome"] == "nothing"
                assert (4, 4, 5) in df.removable
                assert lua_call(df, "remove") == ("rpc.coop", "remove", "4", "4", "5", "99")
                await ws.send(json.dumps({"type": "remove", "x": 4, "y": 4, "z": 5, "id": 7}))
                assert (await recv_until(ws, "remove_result"))["outcome"] == "removed"
        finally:
            await finish(stop, task)
    run(go())


# -- M2: work orders ------------------------------------------------------------

def order_msg(oid, amount, material=None):
    m = {"type": "order", "id": oid, "amount": amount}
    if material is not None:
        m["material"] = material
    return json.dumps(m)


def test_join_shows_the_queue_with_readable_names():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_order"]
                assert hello["orders"] == [{"id": 51, "label": "Brew drink (plants)", "material": "",
                                            "left": 4, "total": 10, "freq": "Daily",
                                            "status": "waiting", "max_workshops": 0,
                                            "conds": [], "cond_info": []}]
                assert hello["can_edit_orders"]
                assert hello["can_edit_conditions"]
                assert {"id": "Daily", "label": "Daily"} in hello["order_catalog"]["freqs"]
                assert {"o_bed", "o_brew"} <= {o["id"] for o in hello["order_catalog"]["orders"]}
        finally:
            await finish(stop, task)
    run(go())


def test_ordering_sends_only_catalog_pieces_and_the_queue_updates():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(order_msg("o_table", 5, "stone"))
                r = await recv_until(a, "order_result")
                assert r["ok"] and r["label"] == "Tables" and r["amount"] == 5
                assert lua_call(df, "order") == ("rpc.coop", "order", "ConstructTable", "", "stone", "5")
                m = await recv_until(b, "orders", lambda m: len(m["orders"]) == 2)
                assert m["orders"][-1]["label"] == "Tables" and m["orders"][-1]["material"] == "stone"
                await a.send(order_msg("o_bed", 3))                  # default material
                await recv_until(a, "order_result")
                assert lua_call(df, "order") == ("rpc.coop", "order", "ConstructBed", "", "wood", "3")
                await a.send(order_msg("o_brew", 20))                 # a reaction, no material
                await recv_until(a, "order_result")
                assert lua_call(df, "order") == ("rpc.coop", "order", "CustomReaction", "BREW_DRINK_FROM_PLANT", "", "20")
                entry = [e for e in hub.action_log.entries() if e["type"] == "order"][0]
                assert (entry["player"], entry["order"], entry["material"], entry["amount"]) == ("Alice", "o_table", "stone", 5)
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    (order_msg("o_nuke", 5), "unknown order"),
    (order_msg("o_bed", 0), "1 to 999"),
    (order_msg("o_bed", 1000), "1 to 999"),
    (order_msg("o_bed", "5"), "1 to 999"),
    (order_msg("o_bed", 5, "stone"), "material"),           # beds are wood only
    (order_msg("o_brew", 5, "wood"), "material"),           # brewing takes no material
])
def test_bad_orders_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(msg)
                r = await recv_until(ws, "order_result")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if c[1] == "order"]
        finally:
            await finish(stop, task)
    run(go())


def test_df_refusing_an_order_is_reported_not_swallowed():
    async def go():
        df = FakeDF()
        df.workorder_ok = False
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(order_msg("o_meal", 10))
                r = await recv_until(ws, "order_result")
                assert not r["ok"] and "refused" in r["error"]
                assert not [e for e in hub.action_log.entries() if e["type"] == "order"]
        finally:
            await finish(stop, task)
    run(go())


async def wait_for_visible(hub, unit):
    """Until the units poll has shown this unit to the connected player."""
    for _ in range(200):
        if any(unit in s.visible_units for s in hub.sessions.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"unit {unit} never became visible")


def lua_call(df, function):
    """The newest call to one Lua function; background polls land in lua_calls
    at any moment, so lua_calls[-1] may be a poll."""
    return [c for c in df.lua_calls if c[1] == function][-1]


def two_orders():
    df = FakeDF()
    df.manager_orders.append({"id": 52, "job": "ConstructBed", "reaction": "", "left": 2, "total": 5,
                              "material": "wood", "freq": "OneTime"})
    df.manager_orders.append({"id": 53, "job": "MakeBarrel", "reaction": "", "left": 1, "total": 1,
                              "material": "wood", "freq": "OneTime"})
    return df


def test_queueing_a_repeating_order_passes_the_frequency():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "order", "id": "o_bed", "amount": 2, "freq": "Monthly"}))
                assert (await recv_until(ws, "order_result"))["ok"]
                assert lua_call(df, "order") == ("rpc.coop", "order", "ConstructBed", "", "wood", "2", "Monthly")
                m = await recv_until(ws, "orders", lambda m: len(m["orders"]) == 2)
                assert m["orders"][-1]["freq"] == "Monthly"
                assert [e for e in hub.action_log.entries() if e["type"] == "order"][0]["freq"] == "Monthly"
        finally:
            await finish(stop, task)
    run(go())


def test_editing_amount_and_frequency_reaches_everyone():
    async def go():
        hub, df, stop, task, port = await start(two_orders())
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "edit_order", "order": 52, "amount": 7, "freq": "Yearly"}))
                r = await recv_until(a, "edit_result")
                assert r["ok"] and r["label"] == "Beds"
                assert lua_call(df, "set_order") == ("rpc.coop", "set_order", "52", "7", "Yearly")
                m = await recv_until(b, "orders", lambda m: m["orders"][1]["total"] == 7)
                assert (m["orders"][1]["left"], m["orders"][1]["freq"]) == (7, "Yearly")
                await a.send(json.dumps({"type": "edit_order", "order": 52, "freq": "Daily"}))   # only one field
                await recv_until(a, "edit_result")
                assert lua_call(df, "set_order") == ("rpc.coop", "set_order", "52", "", "Daily")
                await a.send(json.dumps({"type": "edit_order", "order": 999, "amount": 3}))
                assert "already gone" in (await recv_until(a, "edit_result"))["error"]
                logged = [e for e in hub.action_log.entries() if e["type"] == "edit_order"]
                assert (logged[0]["player"], logged[0]["amount"], logged[0]["freq"]) == ("Alice", 7, "Yearly")
                assert len(logged) == 2
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    ({"order": "52", "amount": 3}, "malformed"),
    ({"order": 52}, "nothing to change"),
    ({"order": 52, "amount": 0}, "1 to 999"),
    ({"order": 52, "amount": 1000}, "1 to 999"),
    ({"order": 52, "amount": True}, "1 to 999"),
    ({"order": 52, "freq": "Hourly"}, "repeat option"),
    ({"order": 52, "freq": "NONE"}, "repeat option"),
    ({"order": 52, "freq": ["Daily"]}, "repeat option"),
])
def test_bad_order_edits_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start(two_orders())
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "edit_order", **msg}))
                r = await recv_until(ws, "edit_result")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if c[1] == "set_order"]
        finally:
            await finish(stop, task)
    run(go())


def test_workshop_limit_reaches_df_and_needs_the_new_module():
    async def go():
        hub, df, stop, task, port = await start(two_orders())
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "edit_order", "order": 52, "max_workshops": 2}))
                assert (await recv_until(ws, "edit_result"))["ok"]
                assert lua_call(df, "set_order") == ("rpc.coop", "set_order", "52", "", "", "2")
                m = await recv_until(ws, "orders", lambda m: m["orders"][1]["max_workshops"] == 2)
                assert m["orders"][1]["max_workshops"] == 2
                for bad in (-1, 100, True, "2"):
                    await ws.send(json.dumps({"type": "edit_order", "order": 52, "max_workshops": bad}))
                    assert "workshop limit" in (await recv_until(ws, "edit_result"))["error"]
                df.lua_version = "12"
                hub.lua_version = 12
                await ws.send(json.dumps({"type": "edit_order", "order": 52, "max_workshops": 1}))
                assert not (await recv_until(ws, "edit_result"))["ok"]
                await ws.send(json.dumps({"type": "edit_order", "order": 52, "amount": 3}))   # the rest still works
                assert (await recv_until(ws, "edit_result"))["ok"]
        finally:
            await finish(stop, task)
    run(go())


def test_conditions_reach_df_and_everyone_can_edit_them():
    async def go():
        df = two_orders()
        df.manager_orders[1].update(conds=[{"cmp": "AtLeast", "val": 10, "item": "BAR", "flags": [], "mat": "iron",
                                            "key": "i:7"},
                                           {"after": 53, "when": "Completed", "key": "o:53:1"}])
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                orders = (await recv_until(ws, "hello"))["orders"]
                assert orders[1]["cond_info"] == [{"key": "i:7", "cmp": "AtLeast", "val": 10, "what": "iron bar"},
                                                  {"key": "o:53:1"}]
                for msg, call in [
                    ({"type": "edit_condition", "order": 52, "key": "i:7", "cmp": "LessThan", "val": 3},
                     ("rpc.coop", "edit_condition", "52", "i:7", "LessThan", "3")),
                    ({"type": "remove_condition", "order": 52, "key": "o:53:1"},
                     ("rpc.coop", "remove_condition", "52", "o:53:1")),
                    ({"type": "add_condition", "order": 52, "item": "BED", "cmp": "AtLeast", "val": 5},
                     ("rpc.coop", "add_item_condition", "52", "BED", "AtLeast", "5")),
                    ({"type": "add_condition", "order": 52, "after": 53, "when": "Activated"},
                     ("rpc.coop", "add_order_condition", "52", "53", "Activated")),
                ]:
                    await ws.send(json.dumps(msg))
                    r = await recv_until(ws, "condition_result")
                    assert r["ok"] and r["label"] == "Beds"
                    assert lua_call(df, call[1]) == call
                logged = [e for e in hub.action_log.entries() if e["type"] == "order_conditions"]
                assert [e["change"] for e in logged] == ["edit_condition", "remove_condition",
                                                         "add_condition", "add_condition"]
                assert {e["order_id"] for e in logged} == {52}
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("reply,error", [
    ({"ok": False, "missing": True}, "already gone"),
    ({"ok": False, "stale": True}, "has changed"),
    ({"ok": False, "duplicate": True}, "already has"),
    ({"ok": False, "loop": True}, "wait for each other"),
    ({"ok": False}, "refused"),
])
def test_df_refusing_a_condition_says_why_and_isnt_logged(reply, error):
    async def go():
        df = two_orders()
        df.condition_replies["add_order_condition"] = reply
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "add_condition", "order": 52, "after": 53, "when": "Completed"}))
                r = await recv_until(ws, "condition_result")
                assert not r["ok"] and error in r["error"]
                assert not [e for e in hub.action_log.entries() if e["type"] == "order_conditions"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    ({"type": "edit_condition", "order": "52", "key": "i:1", "cmp": "AtLeast", "val": 1}, "malformed order"),
    ({"type": "edit_condition", "order": 52, "cmp": "AtLeast", "val": 1}, "malformed condition"),
    ({"type": "edit_condition", "order": 52, "key": "", "cmp": "AtLeast", "val": 1}, "malformed condition"),
    ({"type": "edit_condition", "order": 52, "key": "x" * 201, "cmp": "AtLeast", "val": 1}, "malformed condition"),
    ({"type": "edit_condition", "order": 52, "key": "i:1", "cmp": "NONE", "val": 1}, "comparison"),
    ({"type": "edit_condition", "order": 52, "key": "i:1", "cmp": "AtLeast", "val": -1}, "0 to 999999"),
    ({"type": "edit_condition", "order": 52, "key": "i:1", "cmp": "AtLeast", "val": 1000000}, "0 to 999999"),
    ({"type": "edit_condition", "order": 52, "key": "i:1", "cmp": "AtLeast", "val": True}, "0 to 999999"),
    ({"type": "remove_condition", "order": 52, "key": 7}, "malformed condition"),
    ({"type": "add_condition", "order": 52, "item": "SPOON", "cmp": "AtLeast", "val": 1}, "can't be used"),
    ({"type": "add_condition", "order": 52, "cmp": "AtLeast", "val": 1}, "can't be used"),
    ({"type": "add_condition", "order": 52, "after": "53", "when": "Completed"}, "malformed order"),
    ({"type": "add_condition", "order": 52, "after": 53, "when": "NONE"}, "order state"),
])
def test_bad_condition_changes_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start(two_orders())
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                r = await recv_until(ws, "condition_result")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if "condition" in c[1]]
        finally:
            await finish(stop, task)
    run(go())


def test_condition_changes_need_the_new_module():
    async def go():
        df = two_orders()
        df.lua_version = "12"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert not hello["can_edit_conditions"] and hello["can_edit_orders"]
                await ws.send(json.dumps({"type": "remove_condition", "order": 52, "key": "i:1"}))
                r = await recv_until(ws, "condition_result")
                assert not r["ok"] and "install-lua" in r["error"]
                assert not [c for c in df.lua_calls if "condition" in c[1]]
        finally:
            await finish(stop, task)
    run(go())


def test_moving_orders_changes_the_queue_for_everyone():
    async def go():
        hub, df, stop, task, port = await start(two_orders())
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "move_order", "order": 53, "to": "up"}))
                assert (await recv_until(a, "move_result"))["ok"]
                await recv_until(b, "orders", lambda m: [o["id"] for o in m["orders"]] == [51, 53, 52])
                await a.send(json.dumps({"type": "move_order", "order": 53, "to": "top"}))
                await recv_until(a, "move_result")
                await recv_until(b, "orders", lambda m: [o["id"] for o in m["orders"]] == [53, 51, 52])
                await a.send(json.dumps({"type": "move_order", "order": 53, "to": "bottom"}))
                await recv_until(a, "move_result")
                await recv_until(b, "orders", lambda m: [o["id"] for o in m["orders"]] == [51, 52, 53])
                assert len([e for e in hub.action_log.entries() if e["type"] == "move_order"]) == 3
                for bad in ({"order": 53, "to": "sideways"}, {"order": 53, "to": ["up"]}, {"order": "53", "to": "up"}):
                    await a.send(json.dumps({"type": "move_order", **bad}))
                    assert "malformed" in (await recv_until(a, "move_result"))["error"]
                await a.send(json.dumps({"type": "move_order", "order": 999, "to": "up"}))
                assert "already gone" in (await recv_until(a, "move_result"))["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_order_status_and_conditions_are_readable():
    async def go():
        df = two_orders()
        df.manager_orders[1].update(
            active=True, validated=True,
            conds=[{"cmp": "AtLeast", "val": 10, "item": "BAR", "flags": ["unrotten"], "mat": "iron"},
                   {"after": 53, "when": "Completed"}])
        df.manager_orders[2].update(validated=True)
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                orders = (await recv_until(ws, "hello"))["orders"]
                assert [o["status"] for o in orders] == ["waiting", "active", "validated"]
                assert orders[1]["conds"] == ["at least 10 unrotten iron bar", "after Barrels is completed"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("message", [
    {"type": "edit_order", "order": 51, "amount": 5},
    {"type": "move_order", "order": 51, "to": "top"},
    {"type": "edit_condition", "order": 51, "key": "i:1", "cmp": "AtLeast", "val": 1},
    {"type": "remove_condition", "order": 51, "key": "i:1"},
    {"type": "add_condition", "order": 51, "after": 52, "when": "Completed"},
    {"type": "workshop_job", "id": 400, "job": 11, "action": "cancel"},
    {"type": "workshop_worker", "id": 400, "unit": 8},
])
def test_roles_without_order_permission_cant_change_orders(message):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "miner"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(message))
                assert (await recv_until(ws, "denied"))["action"] == message["type"]
                assert not [c for c in df.lua_calls if c[1] in ("set_order", "move_order", "workshop_job",
                                                                 "workshop_worker")]
        finally:
            await finish(stop, task)
    run(go())


def test_order_changes_need_the_new_module_but_the_queue_still_works():
    async def go():
        df = FakeDF()
        df.lua_version = "7"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_order"] and not hello["can_edit_orders"]
                await ws.send(json.dumps({"type": "edit_order", "order": 51, "amount": 5}))
                assert "install-lua" in (await recv_until(ws, "edit_result"))["error"]
                await ws.send(json.dumps({"type": "move_order", "order": 51, "to": "top"}))
                assert "install-lua" in (await recv_until(ws, "move_result"))["error"]
                await ws.send(json.dumps({"type": "order", "id": "o_bed", "amount": 2, "freq": "Daily"}))
                assert "install-lua" in (await recv_until(ws, "order_result"))["error"]
                await ws.send(order_msg("o_bed", 2))
                assert (await recv_until(ws, "order_result"))["ok"]
        finally:
            await finish(stop, task)
    run(go())


# -- M3: squads -----------------------------------------------------------------

def test_join_shows_squads():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_squads"]
                assert hello["squads"][0]["name"] == "The Axes"
                assert hello["squads"][0]["members"][0]["leader"]
        finally:
            await finish(stop, task)
    run(go())


def test_adding_and_removing_a_member_reaches_everyone_and_is_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "squad_candidates"}))
                assert (await recv_until(a, "squad_candidates"))["units"] == [{"id": 8, "name": "Bomrek, miner"}]
                await a.send(json.dumps({"type": "squad_add", "squad": 2, "unit": 8}))
                r = await recv_until(a, "squad_result")
                assert r["ok"] and r["action"] == "squad_add" and r["name"] == "Bomrek, miner"
                assert lua_call(df, "squad_add") == ("rpc.coop", "squad_add", "2", "8")
                await recv_until(b, "squads", lambda m: len(m["squads"][0]["members"]) == 2)
                await a.send(json.dumps({"type": "squad_remove", "squad": 2, "unit": 8}))
                assert (await recv_until(a, "squad_result"))["ok"]
                await recv_until(b, "squads", lambda m: len(m["squads"][0]["members"]) == 1)
                logged = [e for e in hub.action_log.entries() if e["type"] in ("squad_add", "squad_remove")]
                assert [(e["type"], e["player"], e["squad"], e["name"]) for e in logged] == [
                    ("squad_add", "Alice", 2, "Bomrek, miner"), ("squad_remove", "Alice", 2, "Bomrek, miner")]
        finally:
            await finish(stop, task)
    run(go())


def test_df_side_refusals_are_passed_on_and_not_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "squad_remove", "squad": 2, "unit": 1}))
                r = await recv_until(ws, "squad_result")
                assert not r["ok"] and r["error"] == "the leader stays"
                assert not [e for e in hub.action_log.entries() if e["type"] == "squad_remove"]
        finally:
            await finish(stop, task)
    run(go())


def test_renaming_a_squad_cleans_the_text_and_can_clear_it():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "rename_squad", "squad": 2, "name": "  Iron\n\x07Axes "}))
                assert (await recv_until(ws, "squad_result"))["name"] == "Iron Axes"
                assert lua_call(df, "rename_squad") == ("rpc.coop", "rename_squad", "2", "Iron Axes")
                await ws.send(json.dumps({"type": "rename_squad", "squad": 2, "name": ""}))
                assert (await recv_until(ws, "squad_result"))["name"] == "The Axes"
                await ws.send(json.dumps({"type": "rename_squad", "squad": 2, "name": "x" * 41}))
                assert "40 characters" in (await recv_until(ws, "squad_result"))["error"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg", [
    {"type": "squad_add", "squad": "2", "unit": 8},
    {"type": "squad_add", "squad": 2},
    {"type": "squad_add", "squad": 2, "unit": True},
    {"type": "squad_remove", "squad": 2, "unit": "8"},
    {"type": "rename_squad", "squad": 2},
    {"type": "rename_squad", "squad": 2, "name": 5},
])
def test_malformed_squad_messages_never_reach_df(msg):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                assert "malformed" in (await recv_until(ws, "squad_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == msg["type"]]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("commander", True), ("overseer", True), ("quartermaster", False),
                                          ("miner", False), ("visitor", False)])
def test_squads_need_the_military_permission(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "squad_add", "squad": 2, "unit": 8}))
                if allowed:
                    assert (await recv_until(ws, "squad_result"))["ok"]
                else:
                    assert "manage squads" in (await recv_until(ws, "denied"))["error"]
                    assert not [c for c in df.lua_calls if c[1] == "squad_add"]
        finally:
            await finish(stop, task)
    run(go())


def test_squads_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "8"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert not hello["can_squads"] and hello["can_edit_orders"]
                await ws.send(json.dumps({"type": "squad_candidates"}))
                assert "install-lua" in (await recv_until(ws, "squad_candidates"))["error"]
                await ws.send(json.dumps({"type": "squad_add", "squad": 2, "unit": 8}))
                assert "install-lua" in (await recv_until(ws, "squad_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "squads"]
        finally:
            await finish(stop, task)
    run(go())


# -- M3: nobles -----------------------------------------------------------------

def test_join_shows_posts():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_nobles"]
                assert [p["title"] for p in hello["nobles"]] == ["manager", "lord"]
        finally:
            await finish(stop, task)
    run(go())


def test_appointing_and_removing_reach_everyone_and_are_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "noble_candidates"}))
                assert (await recv_until(a, "noble_candidates"))["units"][0]["id"] == 8
                await a.send(json.dumps({"type": "appoint_noble", "post": 16, "unit": 8}))
                r = await recv_until(a, "noble_result")
                assert r["ok"] and r["title"] == "manager" and r["name"] == "Bomrek, miner"
                assert lua_call(df, "appoint_noble") == ("rpc.coop", "appoint_noble", "16", "8")
                await recv_until(b, "nobles", lambda m: m["nobles"][0]["holder"] == "Bomrek, miner")
                await a.send(json.dumps({"type": "vacate_noble", "post": 16}))
                assert (await recv_until(a, "noble_result"))["ok"]
                assert lua_call(df, "vacate_noble") == ("rpc.coop", "vacate_noble", "16")
                await recv_until(b, "nobles", lambda m: m["nobles"][0]["holder"] == "")
                logged = [e for e in hub.action_log.entries() if e["type"] in ("appoint_noble", "vacate_noble")]
                assert [(e["type"], e["player"], e["title"]) for e in logged] == [
                    ("appoint_noble", "Alice", "manager"), ("vacate_noble", "Alice", "manager")]
        finally:
            await finish(stop, task)
    run(go())


def test_locked_posts_are_refused_by_df_and_not_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "appoint_noble", "post": 17, "unit": 8}))
                assert "heir" in (await recv_until(ws, "noble_result"))["error"]
                assert not [e for e in hub.action_log.entries() if e["type"] == "appoint_noble"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg", [
    {"type": "appoint_noble", "post": "16", "unit": 8},
    {"type": "appoint_noble", "post": 16},
    {"type": "appoint_noble", "post": 16, "unit": True},
    {"type": "vacate_noble", "post": "16"},
    {"type": "vacate_noble"},
])
def test_malformed_noble_messages_never_reach_df(msg):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                assert "malformed" in (await recv_until(ws, "noble_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == msg["type"]]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role", ["builder", "miner", "quartermaster", "commander", "visitor"])
def test_only_overseers_appoint_nobles(role):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["nobles"]                  # everyone can read them
                await ws.send(json.dumps({"type": "vacate_noble", "post": 16}))
                assert "appoint nobles" in (await recv_until(ws, "denied"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "vacate_noble"]
        finally:
            await finish(stop, task)
    run(go())


def test_nobles_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "9"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert not hello["can_nobles"] and hello["can_squads"]
                await ws.send(json.dumps({"type": "vacate_noble", "post": 16}))
                assert "install-lua" in (await recv_until(ws, "noble_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "nobles"]
        finally:
            await finish(stop, task)
    run(go())


# -- M3: stockpiles and zones ----------------------------------------------------

def place_msg(pid, x=4, y=4, z=5):
    return json.dumps({"type": "inspect_place", "id": pid, "x": x, "y": y, "z": z})


def test_inspecting_a_place_and_changing_it_tells_everyone_to_look_again():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                hello = await recv_until(a, "hello")
                assert hello["can_places"] and {"id": "sheet", "label": "Sheets"} in hello["stock_categories"]
                await recv_until(b, "hello")
                await recv_until(a, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await a.send(place_msg(300))
                place = (await recv_until(a, "place_info"))["place"]
                assert place["kind"] == "stockpile" and place["categories"] == ["wood"]
                assert (place["x"], place["y"], place["z"]) == (4, 4, 5) and "found" not in place
                await a.send(json.dumps({"type": "stockpile_category", "id": 300, "category": "food", "on": True}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "stockpile_category") == ("rpc.coop", "stockpile_category", "300", "food", "1")
                assert (await recv_until(b, "place_changed"))["id"] == 300
                await a.send(json.dumps({"type": "stockpile_containers", "id": 300, "barrels": 1, "bins": 2, "wheelbarrows": 0}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "stockpile_containers") == ("rpc.coop", "stockpile_containers", "300", "1", "2", "0")
                await a.send(json.dumps({"type": "rename_place", "id": 300, "name": " Bar\nrels "}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "rename_place") == ("rpc.coop", "rename_place", "300", "Bar rels")
                logged = [e for e in hub.action_log.entries() if e["type"] == "stockpile_category"]
                assert (logged[0]["player"], logged[0]["place"], logged[0]["args"]) == ("Alice", 300, ["food", "1"])
        finally:
            await finish(stop, task)
    run(go())


def test_assigning_and_freeing_a_room():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "zone_candidates"}))
                assert (await recv_until(ws, "zone_candidates"))["units"][0]["id"] == 8
                await ws.send(json.dumps({"type": "zone_owner", "id": 58, "unit": 8}))
                r = await recv_until(ws, "place_result")
                assert r["ok"] and r["name"] == "Bomrek, miner"
                assert lua_call(df, "zone_owner") == ("rpc.coop", "zone_owner", "58", "8")
                await ws.send(json.dumps({"type": "zone_owner", "id": 58}))
                assert (await recv_until(ws, "place_result"))["ok"]
                assert lua_call(df, "zone_owner") == ("rpc.coop", "zone_owner", "58", "")
                await ws.send(json.dumps({"type": "zone_active", "id": 58, "on": False}))
                assert (await recv_until(ws, "place_result"))["ok"]
                assert lua_call(df, "zone_active") == ("rpc.coop", "zone_active", "58", "0")
        finally:
            await finish(stop, task)
    run(go())


def test_places_on_hidden_tiles_stay_secret():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await recv_until(ws, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await ws.send(place_msg(300, 0, 0, 5))        # FakeDF's hidden tile
                assert "nothing to inspect" in (await recv_until(ws, "place_info"))["error"]
                await ws.send(place_msg(300, 999, 0, 5))      # off the map
                assert "nothing to inspect" in (await recv_until(ws, "place_info"))["error"]
                await ws.send(place_msg(999))                 # no such place
                assert "nothing to inspect" in (await recv_until(ws, "place_info"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "place_info" and c[2] == "300"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    ({"type": "stockpile_category", "id": "300", "category": "food", "on": True}, "malformed"),
    ({"type": "stockpile_category", "id": 300, "category": "../../secrets", "on": True}, "unknown category"),
    ({"type": "stockpile_category", "id": 300, "category": ["food"], "on": True}, "unknown category"),
    ({"type": "stockpile_category", "id": 300, "category": "food", "on": "yes"}, "malformed"),
    ({"type": "stockpile_containers", "id": 300, "barrels": 1, "bins": "2", "wheelbarrows": 0}, "malformed"),
    ({"type": "stockpile_containers", "id": 300, "barrels": 1, "bins": 2}, "malformed"),
    ({"type": "rename_place", "id": 300, "name": 5}, "malformed"),
    ({"type": "rename_place", "id": 300, "name": "x" * 41}, "40 characters"),
    ({"type": "zone_owner", "id": 58, "unit": "8"}, "malformed"),
    ({"type": "zone_active", "id": 58, "on": 1}, "malformed"),
])
def test_bad_place_changes_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                assert error in (await recv_until(ws, "place_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == msg["type"]]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("overseer", True), ("builder", True), ("miner", False),
                                          ("quartermaster", False), ("commander", False), ("visitor", False)])
def test_changing_places_needs_the_build_permission_but_anyone_can_look(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await recv_until(ws, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await ws.send(place_msg(58))
                assert (await recv_until(ws, "place_info"))["ok"]
                await ws.send(json.dumps({"type": "zone_owner", "id": 58, "unit": 8}))
                if allowed:
                    assert (await recv_until(ws, "place_result"))["ok"]
                else:
                    await recv_until(ws, "denied")
                    assert not [c for c in df.lua_calls if c[1] == "zone_owner"]
        finally:
            await finish(stop, task)
    run(go())


def test_places_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "10"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert not hello["can_places"] and hello["can_nobles"]
                await ws.send(place_msg(300))
                assert "install-lua" in (await recv_until(ws, "place_info"))["error"]
                await ws.send(json.dumps({"type": "zone_owner", "id": 58}))
                assert "install-lua" in (await recv_until(ws, "place_result"))["error"]
        finally:
            await finish(stop, task)
    run(go())


# -- M3: squad orders ------------------------------------------------------------

def station_msg(squad=2, x=4, y=4, z=5):
    return json.dumps({"type": "squad_station", "squad": squad, "x": x, "y": y, "z": z})


def test_stationing_and_cancelling_reach_everyone_and_are_logged():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                assert (await recv_until(a, "hello"))["can_squad_orders"]
                await recv_until(b, "hello")
                await recv_until(a, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await a.send(station_msg())
                r = await recv_until(a, "squad_result")
                assert r["ok"] and r["action"] == "squad_station"
                assert lua_call(df, "squad_station") == ("rpc.coop", "squad_station", "2", "4", "4", "5")
                await recv_until(b, "squads", lambda m: m["squads"][0].get("station") == {"x": 4, "y": 4, "z": 5})
                await a.send(json.dumps({"type": "squad_clear", "squad": 2}))
                assert (await recv_until(a, "squad_result"))["ok"]
                await recv_until(b, "squads", lambda m: "station" not in m["squads"][0])
                logged = [e for e in hub.action_log.entries() if e["type"] in ("squad_station", "squad_clear")]
                assert [(e["type"], e["player"], e["squad"]) for e in logged] == [
                    ("squad_station", "Alice", 2), ("squad_clear", "Alice", 2)]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,error", [
    (station_msg(x=0, y=0), "can't station"),         # hidden tile
    (station_msg(x=999), "can't station"),            # off the map
    (json.dumps({"type": "squad_station", "squad": 2, "x": "4", "y": 4, "z": 5}), "malformed"),
    (json.dumps({"type": "squad_station", "squad": "2", "x": 4, "y": 4, "z": 5}), "malformed"),
])
def test_bad_stations_never_reach_df(msg, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await recv_until(ws, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await ws.send(msg)
                assert error in (await recv_until(ws, "squad_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "squad_station"]
        finally:
            await finish(stop, task)
    run(go())


def test_a_failed_target_lookup_is_reported_and_the_hub_stays_up():
    async def go():
        df = FakeDF()
        real = df.run_lua

        def broken(module, function, *args):
            if function == "squad_targets":
                raise DFHackRPCError("call 4 failed", code=1)
            return real(module, function, *args)

        df.run_lua = broken
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "squad_targets"}))
                assert "refused" in (await recv_until(ws, "squad_targets"))["error"]
                assert hub.df_online
        finally:
            await finish(stop, task)
    run(go())


def test_kill_orders_only_name_what_the_player_can_see():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await wait_for_visible(hub, 1)
                await ws.send(json.dumps({"type": "squad_targets"}))
                units = (await recv_until(ws, "squad_targets"))["units"]
                assert [u["id"] for u in units] == [1]       # the hidden ambusher stays hidden
                await ws.send(json.dumps({"type": "squad_kill", "squad": 2, "units": [999]}))
                assert "can't see" in (await recv_until(ws, "squad_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "squad_kill"]
                await ws.send(json.dumps({"type": "squad_kill", "squad": 2, "units": [1]}))
                assert (await recv_until(ws, "squad_result"))["ok"]
                assert lua_call(df, "squad_kill") == ("rpc.coop", "squad_kill", "2", "1")
                await recv_until(ws, "squads", lambda m: m["squads"][0]["kill"] == 1)
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("units,error", [
    ([], "malformed"), ("1", "malformed"), (["1"], "malformed"), ([True], "malformed"),
    (list(range(51)), "at most"),
])
def test_bad_kill_lists_never_reach_df(units, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "squad_kill", "squad": 2, "units": units}))
                assert error in (await recv_until(ws, "squad_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "squad_kill"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("overseer", True), ("commander", True), ("builder", False),
                                          ("miner", False), ("quartermaster", False), ("visitor", False)])
def test_squad_orders_need_the_military_permission(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "squad_clear", "squad": 2}))
                if allowed:
                    assert (await recv_until(ws, "squad_result"))["ok"]
                else:
                    assert "manage squads" in (await recv_until(ws, "denied"))["error"]
                    assert not [c for c in df.lua_calls if c[1] == "squad_clear"]
        finally:
            await finish(stop, task)
    run(go())


def test_squad_orders_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "11"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_squads"] and not hello["can_squad_orders"]
                await ws.send(json.dumps({"type": "squad_clear", "squad": 2}))
                assert "install-lua" in (await recv_until(ws, "squad_result"))["error"]
                await ws.send(json.dumps({"type": "squad_targets"}))
                assert "install-lua" in (await recv_until(ws, "squad_targets"))["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_cancelling_an_order_and_not_while_a_dwarf_works_on_it():
    async def go():
        df = FakeDF()
        df.manager_orders.append({"id": 52, "job": "ConstructBed", "reaction": "", "left": 2, "total": 5,
                                  "material": "wood", "freq": "OneTime"})
        df.busy_orders.add(51)
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "cancel_order", "order": 51}))
                r = await recv_until(ws, "cancel_result")
                assert not r["ok"] and "working on it" in r["error"]
                await ws.send(json.dumps({"type": "cancel_order", "order": 52}))
                r = await recv_until(ws, "cancel_result")
                assert r["ok"] and r["label"] == "Beds"
                await recv_until(ws, "orders", lambda m: [o["id"] for o in m["orders"]] == [51])
                await ws.send(json.dumps({"type": "cancel_order", "order": 52}))
                assert "already gone" in (await recv_until(ws, "cancel_result"))["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_orders_need_the_v3_module_but_building_still_works_on_v2():
    async def go():
        df = FakeDF()
        df.lua_version = "2"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_build"] and not hello["can_order"] and hello["orders"] == []
                await ws.send(order_msg("o_bed", 1))
                assert "install-lua" in (await recv_until(ws, "order_result"))["error"]
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(remove_msg(4, 4))
                assert (await recv_until(ws, "remove_result"))["outcome"] == "removed"
        finally:
            await finish(stop, task)
    run(go())


# -- M2: announcements and the unit inspector ------------------------------------

def test_announcements_arrive_once_and_hidden_places_lose_their_location():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_inspect"]
                caravan, stirs = hello["reports"]
                assert caravan["text"].startswith("A caravan") and (caravan["x"], caravan["y"], caravan["z"]) == (4, 4, 5)
                assert caravan["date"] == "1st Granite, year 23"
                assert "x" not in stirs                        # no zooming into unexplored rock
                df.report_list.append({"id": 12, "text": "Urist has become a legendary miner.", "type": "Y",
                                       "year": 23, "time": 2400, "x": 4, "y": 4, "z": 5, "hidden": False,
                                       "color": 14})
                hub._reports_due = 0
                m = await recv_until(ws, "reports")
                assert [r["id"] for r in m["reports"]] == [12]
                hub._reports_due = 0
                await asyncio.sleep(0.2)
                assert [r["id"] for r in hub.reports] == [10, 11, 12]
        finally:
            await finish(stop, task)
    run(go())


def test_inspecting_a_visible_dwarf():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "units", lambda m: m["units"])
                await ws.send(json.dumps({"type": "inspect", "unit": 1}))
                r = await recv_until(ws, "unit_info")
                assert r["ok"] and r["unit"]["mood"] == "Miserable"
                assert r["unit"]["needs"] == ["Pray or meditate", "Be with friends"]
                assert r["unit"]["job"] == "Dig"
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("uid,error", [
    (2, "can't see"),       # standing on the hidden tile
    (3, "can't see"),       # an ambusher DF isn't showing
    (999, "can't see"),     # a made-up id
    ("1", "malformed"),
])
def test_players_cannot_inspect_what_they_cannot_see(uid, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "units", lambda m: m["units"])
                await ws.send(json.dumps({"type": "inspect", "unit": uid}))
                r = await recv_until(ws, "unit_info")
                assert not r["ok"] and error in r["error"]
                assert not [c for c in df.lua_calls if c[1] == "unit"]
        finally:
            await finish(stop, task)
    run(go())


def test_announcement_without_a_hidden_verdict_loses_its_location():
    async def go():
        df = FakeDF()
        df.report_list = [{"id": 5, "text": "Old module, no hidden flag.", "type": "X", "year": 23, "time": 0,
                           "x": 4, "y": 4, "z": 5, "color": 7}]
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert "x" not in (await recv_until(ws, "hello"))["reports"][0]
        finally:
            await finish(stop, task)
    run(go())


# -- M3: chat and pings ------------------------------------------------------------

def test_chat_reaches_everyone_cleaned_up_and_latecomers_see_history():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "chat", "text": "  dig\x00 the\nmoat\t now  " + "x" * 400}))
                m = await recv_until(b, "chat")
                e = m["entry"]
                assert e["from"] == "Alice" and e["kind"] == "chat"
                assert e["text"].startswith("dig the moat now ") and len(e["text"]) == 300
                assert e["date"] == "12th Malachite, year 23"
                await a.send(json.dumps({"type": "chat", "text": " \n\t "}))     # nothing left: ignored
                await a.send(json.dumps({"type": "chat", "text": 5}))
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as late:
                hello = await recv_until(late, "hello")
                assert [x["text"][:16] for x in hello["chat"]] == ["dig the moat now"]
        finally:
            await finish(stop, task)
    run(go())


def test_chat_is_rate_limited():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                for i in range(8):
                    await ws.send(json.dumps({"type": "chat", "text": f"spam {i}"}))
                refused = await recv_until(ws, "chat_refused")
                assert "slow down" in refused["error"]
                assert len(hub.chat) < 8
        finally:
            await finish(stop, task)
    run(go())


def test_pings_reach_everyone_and_land_in_the_chat_log():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await b.send(json.dumps({"type": "ping", "x": 300, "y": 1, "z": 5}))     # off the map: ignored
                await b.send(json.dumps({"type": "ping", "x": "3", "y": 1, "z": 5}))     # malformed: ignored
                await b.send(json.dumps({"type": "ping", "x": 7, "y": 8, "z": 5}))
                p = await recv_until(a, "ping")
                assert (p["from"], p["x"], p["y"], p["z"]) == ("Bob", 7, 8, 5) and p["color"].startswith("#")
                e = (await recv_until(a, "chat"))["entry"]
                assert e["kind"] == "ping" and (e["x"], e["y"], e["z"]) == (7, 8, 5)
                assert len(hub.chat) == 1
        finally:
            await finish(stop, task)
    run(go())


# -- M3: roles ---------------------------------------------------------------------

from actions import ROLES
from hub import load_roles, set_role


async def start_with_roles(roles, df=None, **kw):
    return await start(df, roles=roles, **kw)


@pytest.mark.parametrize("role", list(ROLES))
def test_each_role_is_refused_exactly_what_it_lacks(role):
    probes = {
        "dig": dig_msg("dig", 20, 0, 20, 0),
        "build": build_msg("bed", 2, 2),
        "remove": remove_msg(4, 4),
        "order": order_msg("o_bed", 1),
        "chat": json.dumps({"type": "chat", "text": "hi"}),
    }

    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["you"]["role"] == role and set(hello["you"]["perms"]) == set(ROLES[role])
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                for perm, msg in probes.items():
                    await ws.send(msg)
                    if perm in ROLES[role]:
                        m = await recv_until(ws, "chat" if perm == "chat" else f"{perm}_result")
                        assert m.get("ok", True), (perm, m)
                    else:
                        m = await recv_until(ws, "denied")
                        assert f"your role ({role})" in m["error"], m
                assert ("dig" in ROLES[role]) == bool(df.dig_calls)
                assert ("order" in ROLES[role]) == any(c[1] == "order" for c in df.lua_calls)
        finally:
            await finish(stop, task)
    run(go())


def test_set_role_reaches_a_connected_player_without_reconnecting(tmp_path):
    path = tmp_path / "players.json"
    from hub import add_player
    token = add_player("Carol", path)

    async def go():
        df = FakeDF()
        hub = Hub(df, load_players(path), roles=load_roles(path), players_path=path,
                  poll_interval=0.02, unit_interval=0.02, data_dir=tmp_path)
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(hub.serve("127.0.0.1", 0, stop, ready=ready.set_result))
        server = await asyncio.wait_for(ready, 5)
        port = server.sockets[0].getsockname()[1]
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                assert (await recv_until(ws, "hello"))["you"]["role"] == "overseer"
                set_role("Carol", "visitor", path)
                m = await recv_until(ws, "role")
                assert m["role"] == "visitor" and m["perms"] == ["chat"]
                await ws.send(dig_msg("dig", 20, 0, 20, 0))
                assert "can't dig" in (await recv_until(ws, "denied"))["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_a_demoted_holder_can_still_release_the_pause():
    async def go():
        df = FakeDF()
        df.is_paused = False
        hub, df, stop, task, port = await start_with_roles({"Alice": "overseer"}, df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "pause", "value": True}))
                await recv_until(ws, "pause", lambda m: m["holders"] == ["Alice"])
                hub.roles["Alice"] = "visitor"
                hub._apply_roles()
                await recv_until(ws, "role")
                await ws.send(json.dumps({"type": "pause", "value": True}))      # can't pause again...
                assert "can't pause" in (await recv_until(ws, "denied"))["error"]
                await ws.send(json.dumps({"type": "pause", "value": False}))     # ...but can let go
                await recv_until(ws, "pause", lambda m: not m["paused"])
        finally:
            await finish(stop, task)
    run(go())


def test_role_cli_and_old_token_files(tmp_path):
    from hub import add_player
    path = tmp_path / "players.json"
    path.write_text(json.dumps({"players": [{"name": "Old", "token": "t1"}]}))   # before roles existed
    assert load_roles(path) == {"Old": "overseer"}
    add_player("New", path, role="miner")
    set_role("Old", "builder", path)
    assert load_roles(path) == {"Old": "builder", "New": "miner"}
    assert load_players(path)["t1"] == "Old"
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit):
        set_role("Old", "king", path)
    with pytest.raises(SystemExit):
        set_role("Nobody", "miner", path)
    with pytest.raises(SystemExit):
        add_player("X", path, role="king")


# -- M3: while you were away -------------------------------------------------------

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_returning_after_a_while_shows_what_others_did_and_what_was_announced(monkeypatch, tmp_path):
    import hub as hub_module
    clock = Clock()
    monkeypatch.setattr(hub_module, "_time", clock)

    async def go():
        hub, df, stop, task, port = await start(data_dir=tmp_path)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:
                await recv_until(a, "hello")
            await asyncio.sleep(0.05)
            assert hub.seen.get("Alice")["report"] == 11       # newest announcement when she left
            clock.t += 600
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(b, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await b.send(dig_msg("dig", 20, 0, 23, 0))
                await recv_until(b, "dig_result")
                await b.send(build_msg("bed", 2, 2))
                await recv_until(b, "build_result")
            df.report_list.append({"id": 12, "text": "A vile force of darkness has arrived!", "type": "X",
                                   "year": 23, "time": 5000, "x": 4, "y": 4, "z": 5, "hidden": False, "color": 12})
            df.tick += 1200 * 3
            hub._date_due = 0
            await asyncio.sleep(0.1)
            clock.t += 10
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:
                m = await recv_until(a, "away")
                assert m["since"] == "12th Malachite, year 23"
                assert m["now"] == "15th Malachite, year 23"
                assert m["actions"] == ["Bob designated 4 tiles for digging; placed Bed"]
                assert [r["text"] for r in m["reports"]] == ["A vile force of darkness has arrived!"]
                assert (m["reports"][0]["x"], m["reports"][0]["z"]) == (4, 5)
        finally:
            await finish(stop, task)
    run(go())


def test_short_absences_and_first_visits_get_no_summary(monkeypatch, tmp_path):
    import hub as hub_module
    clock = Clock()
    monkeypatch.setattr(hub_module, "_time", clock)

    async def go():
        hub, df, stop, task, port = await start(data_dir=tmp_path)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:   # first visit ever
                await recv_until(a, "hello")
                with pytest.raises(asyncio.TimeoutError):
                    await recv_until(a, "away", timeout=0.4)
            clock.t += 60                                      # one minute: not "away"
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a:
                await recv_until(a, "hello")
                with pytest.raises(asyncio.TimeoutError):
                    await recv_until(a, "away", timeout=0.4)
        finally:
            await finish(stop, task)
    run(go())


def test_closing_one_of_two_tabs_is_not_leaving(monkeypatch, tmp_path):
    import hub as hub_module
    clock = Clock()
    monkeypatch.setattr(hub_module, "_time", clock)

    async def go():
        hub, df, stop, task, port = await start(data_dir=tmp_path)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as tab1:
                await recv_until(tab1, "hello")
                async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as tab2:
                    await recv_until(tab2, "hello")
                await asyncio.sleep(0.05)
                assert hub.seen.get("Alice") is None
            await asyncio.sleep(0.05)
            assert hub.seen.get("Alice") is not None
        finally:
            await finish(stop, task)
    run(go())



def test_fort_files_keep_their_name_across_autosaves():
    from hub import fort_key
    region = rfr.MapInfo(world_name_english="The Absolute Planet", save_name="region10", block_pos_x=96, block_pos_y=48)
    autosaved = rfr.MapInfo(world_name_english="The Absolute Planet", save_name="autosave 1", block_pos_x=96, block_pos_y=48)
    other_fort = rfr.MapInfo(world_name_english="The Absolute Planet", save_name="region10", block_pos_x=12, block_pos_y=48)
    assert fort_key(region) == fort_key(autosaved) == "The_Absolute_Planet-96x48"
    assert fort_key(other_fort) != fort_key(region)


def test_buildings_quickfort_cannot_place_are_refused_with_the_reason():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["catalog"]["pages"]["MAIN"][0] == {"page": "WORKSHOPS", "label": "Workshops", "key": "o"}
                await ws.send(build_msg("windmill", 3, 3))
                r = await recv_until(ws, "build_result")
                assert not r["ok"] and "quickfort" in r["error"]
                assert not [c for c in df.lua_calls if c[1] == "apply"]
        finally:
            await finish(stop, task)
    run(go())


def test_newer_quickfort_keys_need_module_v5():
    async def go():
        df = FakeDF()
        df.lua_version = "4"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(build_msg("hive", 3, 3))                 # quickfort key "~h"
                assert "install-lua" in (await recv_until(ws, "build_result"))["error"]
                await ws.send(build_msg("carpenter", 3, 3))            # "wc" works on v4
                assert (await recv_until(ws, "build_result"))["ok"]
            df.lua_version = "5"
        finally:
            await finish(stop, task)
    run(go())


# -- designation priority ----------------------------------------------------------

def test_mixed_rectangle_preserves_skipped_orders_after_replay(polling_hub):
    hub, df, _ = polling_hub
    original_tile = df.tile
    df.tile = lambda x, y, z: FLOOR if (x, y, z) == (16, 1, 5) else original_tile(x, y, z)
    run(hub.poll_blocks())
    alice, bob = sorted(hub.sessions.values(), key=lambda session: session.name)
    run(hub._on_dig(bob, {"kind": "channel", "x1": 15, "y1": 0, "x2": 16, "y2": 2,
                           "z": 5, "priority": 6}))
    run(hub._on_dig(alice, {"kind": "dig", "x1": 15, "y1": 0, "x2": 17, "y2": 2,
                             "z": 5, "priority": 1}))
    skipped = {(15, y, 5) for y in range(3)} | {(16, 1, 5)}
    applied = {(16, 0, 5), (16, 2, 5)} | {(17, y, 5) for y in range(3)}
    assert all(df.priorities[tile] == 6 and df.designated[tile] == 3 for tile in skipped)
    assert all(df.priorities[tile] == 1 and df.designated[tile] == 1 for tile in applied)
    entry = list(hub.action_log.entries())[-1]
    assert entry["applied"] == 5
    assert entry["spans"] == [[16, 0, 0], [16, 2, 2], [17, 0, 2]]
    calls = [call for call in df.lua_calls if call[1] == "set_priority_spans"]
    assert len(calls) == 2
    assert not any(call[1] == "set_priority" for call in df.lua_calls)
    restored = Owners()
    restored.replay(hub.action_log)
    assert restored.by_tile == hub.owners.by_tile
    assert all(restored.by_tile[tile] == ("Bob", 6) for tile in skipped)
    assert all(restored.by_tile[tile] == ("Alice", 1) for tile in applied)


def test_unit_graphics_resolve_visible_species_only_and_cache_raws(polling_hub):
    hub, df, _ = polling_hub
    run(hub.poll_blocks())
    df.unit_list[0].race.mat_type, df.unit_list[0].race.mat_index = 0, 0
    df.unit_list[1].race.mat_type, df.unit_list[1].race.mat_index = 9, 0
    calls = []
    def creature_raws(first, end):
        calls.append((first, end))
        result = rfr.CreatureRawList()
        raw = result.creature_raws.add(index=0, creature_id="CAT")
        raw.caste.add(index=0, caste_id="FEMALE")
        return result
    df.creature_raws = creature_raws
    hub.game_tileset.unit_sprites = lambda creature, caste, equipment, state: [f"{creature}:{caste}"]
    run(hub.poll_units())
    assert calls == []
    alice = next(session for session in hub.sessions.values() if session.name == "Alice")
    hub._on_view(alice, {"z": 5, "cx": 4, "cy": 4, "graphics": True})
    run(hub.poll_units())
    assert calls == [(0, 1)]
    assert json.loads(alice.last_units)["units"][0]["g"] == ["CAT:FEMALE"]
    run(hub.poll_units())
    assert calls == [(0, 1)]


def test_optional_creature_graphics_failure_keeps_hub_online(polling_hub):
    hub, df, _ = polling_hub
    run(hub.poll_blocks())
    df.unit_list[0].race.mat_type, df.unit_list[0].race.mat_index = 0, 0
    for session in hub.sessions.values():
        session.graphics = True
    def unavailable(*args):
        raise DFHackRPCError("not supported", code=3)
    df.creature_raws = unavailable
    run(hub.poll_units())
    assert hub.df_online and not hub._graphics_rpc
    assert hub.sessions


def test_polling_sends_clear_when_last_ground_item_disappears(polling_hub):
    hub, df, sent = polling_hub
    run(hub.poll_blocks())
    hub.state.item_defs[(4, -1)] = "BOULDER"
    update = rfr.BlockList()
    block = update.map_blocks.add(map_x=0, map_y=0, map_z=5)
    item = block.items.add(id=99, flags1=1)
    item.pos.x, item.pos.y, item.pos.z = 4, 4, 5
    item.type.mat_type, item.type.mat_index = 4, -1
    hub.state.apply_block_list(update)
    assert hub.state.block_payload((0, 0, 5))["o"]
    sent.clear()
    run(hub.poll_blocks())
    blocks = [entry for _, message in sent if message["type"] == "blocks" for entry in message["blocks"]]
    assert len(blocks) == 1 and blocks[0]["z"] == 5 and "o" not in blocks[0]


def test_dig_priority_reaches_df_and_is_recorded():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(json.dumps({"type": "dig", "kind": "dig", "x1": 20, "y1": 0, "x2": 21, "y2": 0,
                                          "z": 5, "priority": 1}))
                r = await recv_until(ws, "dig_result")
                assert r["ok"] and r["priority"] == 1 and "error" not in r
                assert ("rpc.coop", "set_priority_spans", "[[20,0,0],[21,0,0]]", "5", "1") in df.lua_calls
                assert df.priorities == {(20, 0, 5): 1, (21, 0, 5): 1}
                # (owners goes out before dig_result, so check the hub's record)
                assert {(x, p) for x, _, _, _, p in hub.owners.on_levels({5})} == {(20, 1), (21, 1)}
                assert [e["priority"] for e in hub.action_log.entries() if e["type"] == "dig"] == [1]
                await ws.send(dig_msg("erase", 20, 0, 21, 0))
                await recv_until(ws, "dig_result")
                assert sum(1 for c in df.lua_calls if c[1] == "set_priority_spans") == 1
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("priority", [0, 8, "1", True])
def test_bad_priorities_are_refused(priority):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "dig", "kind": "dig", "x1": 20, "y1": 0, "x2": 20, "y2": 0,
                                          "z": 5, "priority": priority}))
                r = await recv_until(ws, "dig_result")
                assert not r["ok"] and "priority" in r["error"] and not df.dig_calls
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("version", ["5", "6"])
def test_old_module_digs_at_default_priority_only(version):
    async def go():
        df = FakeDF()
        df.lua_version = version
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(json.dumps({"type": "dig", "kind": "dig", "x1": 20, "y1": 0, "x2": 20, "y2": 0,
                                          "z": 5, "priority": 2}))
                assert "install-lua" in (await recv_until(ws, "dig_result"))["error"]
                await ws.send(dig_msg("dig", 20, 0, 20, 0))
                assert (await recv_until(ws, "dig_result"))["ok"]
                assert not [c for c in df.lua_calls if c[1] in {"set_priority", "set_priority_spans"}]
        finally:
            await finish(stop, task)
    run(go())


def test_a_failed_priority_keeps_the_designation_and_the_hub_online():
    async def go():
        df = FakeDF()
        real = df.run_lua

        def flaky(module, function, *args):
            if function == "set_priority_spans":
                raise DFHackRPCError("set_priority failed", code=1)
            return real(module, function, *args)
        df.run_lua = flaky
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "blocks", lambda m: any(b["z"] == 5 for b in m["blocks"]))
                await ws.send(json.dumps({"type": "dig", "kind": "dig", "x1": 20, "y1": 0, "x2": 20, "y2": 0,
                                          "z": 5, "priority": 2}))
                r = await recv_until(ws, "dig_result")
                assert r["ok"] and r["applied"] == 1 and "priority" in r["error"]
                assert hub.df_online and df.dig_calls
        finally:
            await finish(stop, task)
    run(go())


def test_a_workshop_shows_its_tasks_and_takes_changes_from_quartermasters():
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "quartermaster"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                assert (await recv_until(a, "hello"))["can_workshops"]
                await recv_until(b, "hello")
                await recv_until(a, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await a.send(place_msg(400))
                place = (await recv_until(a, "place_info"))["place"]
                assert place["kind"] == "workshop" and place["jobs"][0]["name"] == "Make granite blocks"
                await a.send(json.dumps({"type": "workshop_job", "id": 400, "job": 11, "action": "suspend"}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "workshop_job") == ("rpc.coop", "workshop_job", "400", "11", "suspend")
                assert (await recv_until(b, "place_changed"))["id"] == 400
                await a.send(json.dumps({"type": "workshop_worker", "id": 400, "unit": 8}))
                assert (await recv_until(a, "place_result"))["name"] == "Bomrek, miner"
                assert lua_call(df, "workshop_worker") == ("rpc.coop", "workshop_worker", "400", "8")
                await a.send(json.dumps({"type": "workshop_worker", "id": 400}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "workshop_worker") == ("rpc.coop", "workshop_worker", "400", "")
                logged = [e for e in hub.action_log.entries() if e["type"] in ("workshop_job", "workshop_worker")]
                assert logged[0]["args"] == ["11", "suspend"] and [e["place"] for e in logged] == [400, 400, 400]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg,reply,error", [
    ({"type": "workshop_job", "id": 400, "job": "11", "action": "cancel"}, "place_result", "malformed"),
    ({"type": "workshop_job", "id": 400, "job": 11, "action": "delete"}, "place_result", "malformed"),
    ({"type": "workshop_job", "id": 400, "job": 11, "action": ["cancel"]}, "place_result", "malformed"),
    ({"type": "workshop_worker", "id": 400, "unit": "8"}, "place_result", "malformed"),
    ({"type": "workshop_worker", "id": "400"}, "place_result", "malformed"),
])
def test_bad_workshop_changes_never_reach_df(msg, reply, error):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                assert error in (await recv_until(ws, reply))["error"]
                assert not [c for c in df.lua_calls if c[1] in ("workshop_job", "workshop_worker")]
        finally:
            await finish(stop, task)
    run(go())


def test_a_workshop_that_is_gone_is_refused_by_df():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "workshop_job", "id": 999, "job": 1, "action": "cancel"}))
                assert "gone" in (await recv_until(ws, "place_result"))["error"]
                assert not [e for e in hub.action_log.entries() if e["type"] == "workshop_job"]
        finally:
            await finish(stop, task)
    run(go())


def test_workshops_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "15"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_places"] and not hello["can_workshops"]
                await ws.send(json.dumps({"type": "workshop_job", "id": 400, "job": 11, "action": "cancel"}))
                assert "install-lua" in (await recv_until(ws, "place_result"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "workshop_job"]
        finally:
            await finish(stop, task)
    run(go())


def test_stocks_are_counted_once_for_everyone_within_a_minute():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                assert (await recv_until(a, "hello"))["can_stocks"]
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "stocks"}))
                await b.send(json.dumps({"type": "stocks"}))
                ra, rb = await recv_until(a, "stocks"), await recv_until(b, "stocks")
                assert ra["ok"] and rb["groups"] == ra["groups"] and ra["age"] == 0
                names = [g["name"] for g in ra["groups"]]
                assert names == ["Raw materials", "Food and drink", "Weapons and armor"]
                assert ra["groups"][1]["rows"] == [{"name": "dwarven ale", "count": 40}]
                assert ra["groups"][2]["rows"] == [{"name": "iron short swords", "count": 2}]
                assert len([c for c in df.lua_calls if c[1] == "stocks"]) == 1
                hub._stocks_read -= 61                       # a minute later DF is counted again
                await a.send(json.dumps({"type": "stocks"}))
                await recv_until(a, "stocks")
                assert len([c for c in df.lua_calls if c[1] == "stocks"]) == 2
        finally:
            await finish(stop, task)
    run(go())


def test_stocks_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "16"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                assert not (await recv_until(ws, "hello"))["can_stocks"]
                await ws.send(json.dumps({"type": "stocks"}))
                assert "install-lua" in (await recv_until(ws, "stocks"))["error"]
                assert not [c for c in df.lua_calls if c[1] == "stocks"]
        finally:
            await finish(stop, task)
    run(go())


def test_farm_crops_and_fertilizing_go_to_df_and_everyone_looks_again():
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "builder"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                assert (await recv_until(a, "hello"))["can_farms"]
                await recv_until(b, "hello")
                await recv_until(a, "blocks", lambda m: any(x["z"] == 5 for x in m["blocks"]))
                await a.send(place_msg(500))
                assert (await recv_until(a, "place_info"))["place"]["kind"] == "farm"
                await a.send(json.dumps({"type": "farm_crop", "id": 500, "season": 2, "plant": -1}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "farm_crop") == ("rpc.coop", "farm_crop", "500", "2", "-1")
                assert (await recv_until(b, "place_changed"))["id"] == 500
                await a.send(json.dumps({"type": "farm_fertilize", "id": 500, "on": True}))
                assert (await recv_until(a, "place_result"))["ok"]
                assert lua_call(df, "farm_fertilize") == ("rpc.coop", "farm_fertilize", "500", "1")
                await a.send(json.dumps({"type": "farm_crop", "id": 999, "season": 0, "plant": 173}))
                assert "gone" in (await recv_until(a, "place_result"))["error"]
                logged = [e["type"] for e in hub.action_log.entries() if e["type"].startswith("farm_")]
                assert logged == ["farm_crop", "farm_fertilize"]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("msg", [
    {"type": "farm_crop", "id": 500, "season": 4, "plant": 173},
    {"type": "farm_crop", "id": 500, "season": -1, "plant": 173},
    {"type": "farm_crop", "id": 500, "season": "0", "plant": 173},
    {"type": "farm_crop", "id": 500, "season": 0, "plant": -2},
    {"type": "farm_crop", "id": 500, "season": 0, "plant": "BARLEY"},
    {"type": "farm_fertilize", "id": 500, "on": 1},
    {"type": "farm_fertilize", "id": "500", "on": True},
])
def test_bad_farm_changes_never_reach_df(msg):
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(msg))
                assert "malformed" in (await recv_until(ws, "place_result"))["error"]
                assert not [c for c in df.lua_calls if c[1].startswith("farm_")]
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("overseer", True), ("builder", True), ("quartermaster", False),
                                          ("visitor", False)])
def test_farm_changes_need_the_build_permission(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "farm_crop", "id": 500, "season": 0, "plant": 173}))
                if allowed:
                    assert (await recv_until(ws, "place_result"))["ok"]
                else:
                    assert (await recv_until(ws, "denied"))["action"] == "farm_crop"
                    assert not [c for c in df.lua_calls if c[1] == "farm_crop"]
        finally:
            await finish(stop, task)
    run(go())


def test_farms_need_the_new_module():
    async def go():
        df = FakeDF()
        df.lua_version = "17"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                hello = await recv_until(ws, "hello")
                assert hello["can_workshops"] and not hello["can_farms"]
                await ws.send(json.dumps({"type": "farm_crop", "id": 500, "season": 0, "plant": 173}))
                assert "install-lua" in (await recv_until(ws, "place_result"))["error"]
        finally:
            await finish(stop, task)
    run(go())


def test_units_come_from_coop_lua_for_the_players_levels_and_rfr_for_older_modules(polling_hub):
    hub, df, _ = polling_hub
    run(hub.poll_blocks())      # the RFR path checks hidden tiles against the hub's own map
    rfr_calls = []
    original = df.units
    df.units = lambda: (rfr_calls.append(1), original())[1]
    run(hub.poll_units())
    lua = [c for c in df.lua_calls if c[1] == "units"]
    levels = sorted({z for s in hub.sessions.values() for z in s.zs})
    assert lua and lua[-1][2:] == (",".join(map(str, levels)), "0") and not rfr_calls
    alice = next(s for s in hub.sessions.values() if s.name == "Alice")
    shown = {u["i"] for u in json.loads(alice.last_units)["units"]}
    assert shown == {1}                 # not the one on a hidden tile, not the one in ambush
    alice.graphics = True
    run(hub.poll_units())
    assert [c for c in df.lua_calls if c[1] == "units"][-1][3] == "1"     # gear only for graphics
    hub.lua_version = 18
    alice.last_units = None
    run(hub.poll_units())
    assert rfr_calls and {u["i"] for u in json.loads(alice.last_units)["units"]} == {1}


def test_smoothing_goes_through_coop_lua_shows_its_marks_and_erase_clears_them():
    async def go():
        hub, df, stop, task, port = await start()
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as a, \
                       connect(f"ws://127.0.0.1:{port}/ws?token=tok-b") as b:
                await recv_until(a, "hello")
                await recv_until(b, "hello")
                await a.send(json.dumps({"type": "dig", "kind": "smooth", "x1": 3, "y1": 2, "x2": 1, "y2": 4, "z": 5,
                                         "priority": 2}))
                r = await recv_until(a, "dig_result")
                assert r["ok"] and r["kind"] == "smooth" and (r["applied"], r["skipped"]) == (8, 1)
                assert lua_call(df, "detail") == ("rpc.coop", "detail", "smooth", "1", "2", "3", "4", "5", "2")
                assert not [c for c in df.lua_calls if c[1] == "set_priority_spans"]   # quickfort set it
                m = await recv_until(b, "detail_marks", lambda m: m["marks"])
                assert m["marks"] == [[1, 2, 5, "s"]]
                await a.send(json.dumps({"type": "dig", "kind": "erase", "x1": 0, "y1": 0, "x2": 5, "y2": 5, "z": 5}))
                assert (await recv_until(a, "dig_result"))["ok"]
                assert lua_call(df, "detail_erase") == ("rpc.coop", "detail_erase", "0", "0", "5", "5", "5")
                assert (await recv_until(b, "detail_marks", lambda m: not m["marks"]))["marks"] == []
                kinds = [(e["kind"], e["applied"]) for e in hub.action_log.entries() if e["type"] == "detail"]
                assert kinds == [("smooth", 8), ("erase", 1)]
                hub.owners.by_tile.clear()
                hub.owners.replay(hub.action_log)
                assert not any(t[2] == 5 and t[0] < 4 and t[1] < 5 for t in hub.owners.by_tile)
        finally:
            await finish(stop, task)
    run(go())


@pytest.mark.parametrize("role,allowed", [("miner", True), ("builder", True), ("quartermaster", False), ("visitor", False)])
def test_smoothing_needs_the_dig_permission(role, allowed):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": role})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "dig", "kind": "engrave", "x1": 1, "y1": 1, "x2": 2, "y2": 2, "z": 5}))
                if allowed:
                    assert (await recv_until(ws, "dig_result"))["ok"]
                else:
                    assert (await recv_until(ws, "denied"))["action"] == "dig"
                    assert not [c for c in df.lua_calls if c[1] == "detail"]
        finally:
            await finish(stop, task)
    run(go())


def test_smoothing_needs_the_new_module_and_erase_still_works_without_it():
    async def go():
        df = FakeDF()
        df.lua_version = "19"
        hub, df, stop, task, port = await start(df)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps({"type": "dig", "kind": "fortify", "x1": 1, "y1": 1, "x2": 2, "y2": 2, "z": 5}))
                assert "install-lua" in (await recv_until(ws, "dig_result"))["error"]
                await ws.send(json.dumps({"type": "dig", "kind": "erase", "x1": 1, "y1": 1, "x2": 2, "y2": 2, "z": 5}))
                assert (await recv_until(ws, "dig_result"))["ok"]
                assert not [c for c in df.lua_calls if c[1] in ("detail", "detail_erase", "detail_marks")]
        finally:
            await finish(stop, task)
    run(go())
