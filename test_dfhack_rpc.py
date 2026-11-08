"""The DFHack remote-protocol client, against a fake server that speaks the
same framing. The fake is built from RemoteClient.h, not from the client, so a
misread of the header layout or the bind step fails here rather than live."""

from __future__ import annotations

import socket
import threading

import pytest

pytest.importorskip("google.protobuf", reason="df-coop needs the coop extra")

from dfhack_rpc import (
    HANDSHAKE,
    HEADER,
    RESPONSE_MAGIC,
    RPC_REPLY_FAIL,
    RPC_REPLY_RESULT,
    RPC_REPLY_TEXT,
    RPC_REQUEST_QUIT,
    DFHackRPC,
    DFHackRPCError,
    core,
    rfr,
)


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


class FakeDFHack:
    """One-connection DFHack stand-in. `handlers` maps (plugin, method) to a
    function taking the raw request bytes and returning a list of
    (reply_id, payload_or_code) frames to send back."""

    def __init__(self, handlers, magic=RESPONSE_MAGIC, version=1):
        self.handlers = handlers
        self.magic = magic
        self.version = version
        self.binds = []
        self.calls = []
        self.got_quit = False
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            magic, version = HANDSHAKE.unpack(recv_exact(conn, HANDSHAKE.size))
            assert magic == b"DFHack?\n" and version == 1
            conn.sendall(HANDSHAKE.pack(self.magic, self.version))
            ids = {}
            try:
                while True:
                    method_id, size = HEADER.unpack(recv_exact(conn, HEADER.size))
                    if method_id == RPC_REQUEST_QUIT:
                        self.got_quit = True
                        return
                    body = recv_exact(conn, size)
                    if method_id == 0:
                        req = core.CoreBindRequest.FromString(body)
                        key = (req.plugin or None, req.method)
                        self.binds.append((key, req.input_msg, req.output_msg))
                        if key not in self.handlers:
                            conn.sendall(HEADER.pack(RPC_REPLY_FAIL, 3))
                            continue
                        ids[len(ids) + 100] = key
                        reply = core.CoreBindReply(assigned_id=len(ids) + 99).SerializeToString()
                        conn.sendall(HEADER.pack(RPC_REPLY_RESULT, len(reply)) + reply)
                        continue
                    key = ids[method_id]
                    self.calls.append(key)
                    for reply_id, data in self.handlers[key](body):
                        if reply_id == RPC_REPLY_FAIL:
                            conn.sendall(HEADER.pack(reply_id, data))
                        else:
                            conn.sendall(HEADER.pack(reply_id, len(data)) + data)
            except EOFError:
                return

    def close(self):
        self.listener.close()


def result(msg):
    return (RPC_REPLY_RESULT, msg.SerializeToString())


def text(s):
    note = core.CoreTextNotification()
    note.fragments.add(text=s)
    return (RPC_REPLY_TEXT, note.SerializeToString())


@pytest.fixture
def fake():
    servers = []

    def make(handlers, **kw):
        server = FakeDFHack(handlers, **kw)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_header_is_eight_bytes_little_endian_with_padding():
    assert HEADER.pack(-1, 0x01020304) == b"\xff\xff\x00\x00\x04\x03\x02\x01"


def test_handshake_rejects_a_server_that_is_not_dfhack(fake):
    server = fake({}, magic=b"HTTP/1.1")
    with pytest.raises(DFHackRPCError, match="not a DFHack server"):
        DFHackRPC(port=server.port).connect()


def test_plugin_call_binds_with_full_message_names_then_reuses_the_id(fake):
    version = rfr.VersionInfo(dwarf_fortress_version="53.16", dfhack_version="53.16-r3")
    server = fake({("RemoteFortressReader", "GetVersionInfo"): lambda body: [result(version)]})
    with DFHackRPC(port=server.port) as df:
        assert df.version_info().dfhack_version == "53.16-r3"
        assert df.version_info().dwarf_fortress_version == "53.16"
    server.thread.join(2)
    assert server.binds == [(("RemoteFortressReader", "GetVersionInfo"),
                             "dfproto.EmptyMessage", "RemoteFortressReader.VersionInfo")]
    assert len(server.calls) == 2


def test_close_sends_quit(fake):
    server = fake({})
    DFHackRPC(port=server.port).connect().close()
    server.thread.join(2)
    assert server.got_quit


def test_text_notifications_are_collected_before_the_result(fake):
    def run(body):
        req = core.CoreRunCommandRequest.FromString(body)
        assert (req.command, list(req.arguments)) == ("ls", ["-a"])
        return [text("one\n"), text("two\n"), result(core.EmptyMessage())]

    server = fake({(None, "RunCommand"): run})
    with DFHackRPC(port=server.port) as df:
        assert df.run_command("ls", "-a") == "one\ntwo\n"


def test_failed_call_raises_with_the_result_code_and_printed_text(fake):
    server = fake({(None, "RunCommand"): lambda body: [text("no such command\n"), (RPC_REPLY_FAIL, 3)]})
    with DFHackRPC(port=server.port) as df:
        with pytest.raises(DFHackRPCError) as err:
            df.run_command("nope")
    assert err.value.code == 3
    assert "CR_NOT_FOUND" in str(err.value) and "no such command" in str(err.value)


def test_unknown_method_fails_at_bind(fake):
    server = fake({})
    with DFHackRPC(port=server.port) as df:
        with pytest.raises(DFHackRPCError, match="bind RemoteFortressReader::GetMapInfo"):
            df.map_info()


def test_connection_survives_a_failed_call(fake):
    server = fake({
        (None, "RunCommand"): lambda body: [(RPC_REPLY_FAIL, 1)],
        ("RemoteFortressReader", "GetPauseState"): lambda body: [result(rfr.SingleBool(Value=True))],
    })
    with DFHackRPC(port=server.port) as df:
        with pytest.raises(DFHackRPCError):
            df.run_command("x")
        assert df.paused() is True


def test_run_lua_passes_module_function_and_string_args(fake):
    def lua(body):
        req = core.CoreRunLuaRequest.FromString(body)
        return [result(core.StringListMessage(value=[req.module, req.function, *req.arguments]))]

    server = fake({(None, "RunLua"): lua})
    with DFHackRPC(port=server.port) as df:
        assert df.run_lua("rpc.coop", "build", "bed", "3") == ["rpc.coop", "build", "bed", "3"]


def test_graphics_definitions_use_item_list_and_bounded_creature_requests(fake):
    ranges = []
    def creatures(body):
        request = rfr.ListRequest.FromString(body)
        ranges.append((request.list_start, request.list_end))
        reply = rfr.CreatureRawList()
        reply.creature_raws.add(index=request.list_start, creature_id="CAT")
        return [result(reply)]
    def items(body):
        reply = rfr.MaterialList()
        entry = reply.material_list.add(id="ARMOR/ITEM_ARMOR_SHIRT")
        entry.mat_pair.mat_type, entry.mat_pair.mat_index = 25, 0
        return [result(reply)]
    server = fake({("RemoteFortressReader", "GetPartialCreatureRaws"): creatures,
                   ("RemoteFortressReader", "GetItemList"): items})
    with DFHackRPC(port=server.port) as df:
        assert df.creature_raws(7, 8).creature_raws[0].creature_id == "CAT"
        assert df.item_defs().material_list[0].id == "ARMOR/ITEM_ARMOR_SHIRT"
    assert ranges == [(7, 8)]


def test_dig_sends_every_location(fake):
    seen = []

    def dig(body):
        seen.append(rfr.DigCommand.FromString(body))
        return [result(core.EmptyMessage())]

    server = fake({("RemoteFortressReader", "SendDigCommand"): dig})
    with DFHackRPC(port=server.port) as df:
        df.dig(rfr.CHANNEL_DIG, [(1, 2, 3), (4, 5, 6)])
    assert seen[0].designation == rfr.CHANNEL_DIG
    assert [(p.x, p.y, p.z) for p in seen[0].locations] == [(1, 2, 3), (4, 5, 6)]


def test_block_request_carries_the_range(fake):
    seen = []

    def blocks(body):
        seen.append(rfr.BlockRequest.FromString(body))
        reply = rfr.BlockList()
        reply.map_blocks.add(map_x=16, map_y=32, map_z=100)
        return [result(reply)]

    server = fake({("RemoteFortressReader", "GetBlockList"): blocks})
    with DFHackRPC(port=server.port) as df:
        got = df.blocks(1, 3, 2, 4, 100, 101, force_reload=True)
    assert [(b.map_x, b.map_y, b.map_z) for b in got.map_blocks] == [(16, 32, 100)]
    req = seen[0]
    assert (req.min_x, req.max_x, req.min_y, req.max_y, req.min_z, req.max_z) == (1, 3, 2, 4, 100, 101)
    assert req.force_reload


def test_oversized_reply_drops_the_connection():
    """A frame claiming more than DFHack's 64 MiB cap is a desync or a hostile
    peer; the client must not try to allocate it."""
    client = DFHackRPC()
    ours, theirs = socket.socketpair()
    client._sock = ours
    theirs.sendall(HEADER.pack(RPC_REPLY_RESULT, 64 * 1048576 + 1))
    with pytest.raises(DFHackRPCError, match="bad message size"):
        client._roundtrip(5, core.EmptyMessage(), core.EmptyMessage)
    assert not client.connected
    theirs.close()


def test_calls_before_connect_fail_cleanly():
    with pytest.raises(DFHackRPCError, match="not connected"):
        DFHackRPC().paused()
