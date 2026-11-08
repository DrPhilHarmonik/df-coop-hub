"""The hub over real websockets, with a fake fortress behind it. These check
the things that matter between friends: who gets in, that everyone sees the
same fortress and each other, and that nothing hidden leaks."""

from __future__ import annotations

import asyncio
import json
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
from mapstate import FLAGS1_HIDDEN_IN_AMBUSH

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
        self.lua_version = "10"
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
        if function == "order":
            job, reaction, material, amount, *freq = args
            if not self.workorder_ok:
                return [json.dumps({"ok": False, "output": "workorder: bad"})]
            self.manager_orders.append({"id": 600 + len(self.manager_orders), "job": job, "reaction": reaction,
                                        "left": int(amount), "total": int(amount), "material": material,
                                        "freq": freq[0] if freq else "OneTime"})
            return [json.dumps({"ok": True, "output": ""})]
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
            oid, amount, freq = args
            for o in self.manager_orders:
                if o["id"] == int(oid):
                    if amount:
                        o["left"] = o["total"] = int(amount)
                    if freq:
                        o["freq"] = freq
                    return [json.dumps({"ok": True})]
            return [json.dumps({"ok": False, "missing": True})]
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
                                            "status": "waiting", "conds": []}]
                assert hello["can_edit_orders"]
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
])
def test_roles_without_order_permission_cant_change_orders(message):
    async def go():
        hub, df, stop, task, port = await start_with_roles({"Alice": "miner"})
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=tok-a") as ws:
                await recv_until(ws, "hello")
                await ws.send(json.dumps(message))
                assert (await recv_until(ws, "denied"))["action"] == message["type"]
                assert not [c for c in df.lua_calls if c[1] in ("set_order", "move_order")]
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
