"""A client for DFHack's remote protocol, the socket DFHack listens on (port
5000 by default) for tools like Armok Vision and dfhack-run.

The wire format, from DFHack's library/include/RemoteClient.h:

- Handshake: the client sends b"DFHack?\\n" and a little-endian int32 version
  (1); the server answers b"DFHack!\\n" and its own version.
- Every message after that is an 8-byte header, int16 id, 2 bytes padding,
  int32 size, followed by `size` bytes of protobuf.
- Method 0 is BindMethod, which turns a (method, input type, output type,
  plugin) name into the numeric id used for later calls. Ids are per
  connection, so they are cached here per connection.
- A call is answered by any number of RPC_REPLY_TEXT messages (console output
  the method printed) and then exactly one RPC_REPLY_RESULT carrying the reply,
  or RPC_REPLY_FAIL, whose size field is the command_result code and which has
  no body.

One connection serves one call at a time, so calls are serialised with a lock.
The co-op hub owns a single connection and fans the results out to players.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

import CoreProtocol_pb2 as core  # noqa: E402
import RemoteFortressReader_pb2 as rfr  # noqa: E402

REQUEST_MAGIC = b"DFHack?\n"
RESPONSE_MAGIC = b"DFHack!\n"
PROTOCOL_VERSION = 1

RPC_REPLY_RESULT = -1
RPC_REPLY_FAIL = -2
RPC_REPLY_TEXT = -3
RPC_REQUEST_QUIT = -4

BIND_METHOD_ID = 0
MAX_MESSAGE_SIZE = 64 * 1048576

HEADER = struct.Struct("<hxxi")
HANDSHAKE = struct.Struct("<8si")

RFR_PLUGIN = "RemoteFortressReader"

# DFHack's command_result codes, for readable errors.
RESULT_NAMES = {
    -3: "CR_LINK_FAILURE",
    -2: "CR_WOULD_BREAK",
    -1: "CR_NOT_IMPLEMENTED",
    0: "CR_OK",
    1: "CR_FAILURE",
    2: "CR_WRONG_USAGE",
    3: "CR_NOT_FOUND",
}


class DFHackRPCError(RuntimeError):
    """A call DFHack refused or could not complete."""

    def __init__(self, message: str, code: int | None = None, text: str = ""):
        detail = message
        if code is not None:
            detail += f" ({RESULT_NAMES.get(code, code)})"
        if text:
            detail += f": {text.strip()}"
        super().__init__(detail)
        self.code = code
        self.text = text


class DFHackRPC:
    def __init__(self, host: str = "127.0.0.1", port: int = 5000, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._ids: dict[tuple[str, str | None], int] = {}
        self._lock = threading.Lock()
        self.last_text = ""

    # -- connection ---------------------------------------------------------

    def connect(self) -> "DFHackRPC":
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock.sendall(HANDSHAKE.pack(REQUEST_MAGIC, PROTOCOL_VERSION))
            magic, version = HANDSHAKE.unpack(_recv_exact(sock, HANDSHAKE.size))
        except BaseException:
            sock.close()
            raise
        if magic != RESPONSE_MAGIC or version != PROTOCOL_VERSION:
            sock.close()
            raise DFHackRPCError(f"not a DFHack server (magic={magic!r}, version={version})")
        self._sock = sock
        self._ids.clear()
        return self

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(HEADER.pack(RPC_REQUEST_QUIT, 0))
        except OSError:
            pass
        sock.close()

    def __enter__(self) -> "DFHackRPC":
        return self.connect() if self._sock is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- calls --------------------------------------------------------------

    def call(self, method: str, request, reply_cls, plugin: str | None = None):
        """Call `method` with a protobuf `request`, returning a `reply_cls`."""
        with self._lock:
            method_id = self._bind(method, request, reply_cls, plugin)
            return self._roundtrip(method_id, request, reply_cls)

    def _bind(self, method, request, reply_cls, plugin) -> int:
        key = (method, plugin)
        if key not in self._ids:
            bind = core.CoreBindRequest(
                method=method,
                input_msg=request.DESCRIPTOR.full_name,
                output_msg=reply_cls.DESCRIPTOR.full_name,
            )
            if plugin:
                bind.plugin = plugin
            reply = self._roundtrip(BIND_METHOD_ID, bind, core.CoreBindReply,
                                    what=f"bind {plugin + '::' if plugin else ''}{method}")
            self._ids[key] = reply.assigned_id
        return self._ids[key]

    def _roundtrip(self, method_id, request, reply_cls, what: str | None = None):
        sock = self._require_sock()
        body = request.SerializeToString()
        sock.sendall(HEADER.pack(method_id, len(body)) + body)
        texts: list[str] = []
        while True:
            reply_id, size = HEADER.unpack(_recv_exact(sock, HEADER.size))
            if reply_id == RPC_REPLY_FAIL:
                self.last_text = "".join(texts)
                raise DFHackRPCError(what or f"call {method_id} failed", size, self.last_text)
            if not 0 <= size <= MAX_MESSAGE_SIZE:
                self.close()
                raise DFHackRPCError(f"bad message size {size}")
            payload = _recv_exact(sock, size)
            if reply_id == RPC_REPLY_TEXT:
                note = core.CoreTextNotification.FromString(payload)
                texts.extend(f.text for f in note.fragments)
                continue
            if reply_id == RPC_REPLY_RESULT:
                self.last_text = "".join(texts)
                return reply_cls.FromString(payload)
            self.close()
            raise DFHackRPCError(f"unexpected reply id {reply_id}")

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise DFHackRPCError("not connected")
        return self._sock

    # -- core helpers -------------------------------------------------------

    def run_command(self, command: str, *args: str) -> str:
        """Run a DFHack console command; returns what it printed."""
        req = core.CoreRunCommandRequest(command=command, arguments=list(args))
        self.call("RunCommand", req, core.EmptyMessage)
        return self.last_text

    def run_lua(self, module: str, function: str, *args: str) -> list[str]:
        """Call `module.function(*args)`. DFHack only allows modules named
        rpc.*, so co-op's own Lua lives at hack/lua/rpc/coop.lua."""
        req = core.CoreRunLuaRequest(module=module, function=function, arguments=list(args))
        return list(self.call("RunLua", req, core.StringListMessage).value)

    # -- RemoteFortressReader helpers ----------------------------------------

    def rfr(self, method: str, request, reply_cls):
        return self.call(method, request, reply_cls, plugin=RFR_PLUGIN)

    def version_info(self) -> "rfr.VersionInfo":
        return self.rfr("GetVersionInfo", core.EmptyMessage(), rfr.VersionInfo)

    def map_info(self) -> "rfr.MapInfo":
        return self.rfr("GetMapInfo", core.EmptyMessage(), rfr.MapInfo)

    def view_info(self) -> "rfr.ViewInfo":
        return self.rfr("GetViewInfo", core.EmptyMessage(), rfr.ViewInfo)

    def tiletypes(self) -> "rfr.TiletypeList":
        return self.rfr("GetTiletypeList", core.EmptyMessage(), rfr.TiletypeList)

    def blocks(self, min_x, max_x, min_y, max_y, min_z, max_z,
               blocks_needed: int = 256, force_reload: bool = False) -> "rfr.BlockList":
        """Map blocks (16x16 tiles, one z-level) in the given *block* range.
        Upper bounds are exclusive. DFHack remembers per connection what it
        has sent and only returns blocks that changed, unless force_reload."""
        req = rfr.BlockRequest(
            blocks_needed=blocks_needed, min_x=min_x, max_x=max_x,
            min_y=min_y, max_y=max_y, min_z=min_z, max_z=max_z,
            force_reload=force_reload,
        )
        return self.rfr("GetBlockList", req, rfr.BlockList)

    def building_defs(self) -> "rfr.BuildingList":
        return self.rfr("GetBuildingDefList", core.EmptyMessage(), rfr.BuildingList)

    def units(self) -> "rfr.UnitList":
        return self.rfr("GetUnitList", core.EmptyMessage(), rfr.UnitList)

    def item_defs(self) -> "rfr.MaterialList":
        return self.rfr("GetItemList", core.EmptyMessage(), rfr.MaterialList)

    def creature_raws(self, first: int, end: int) -> "rfr.CreatureRawList":
        return self.rfr("GetPartialCreatureRaws", rfr.ListRequest(list_start=first, list_end=end), rfr.CreatureRawList)

    def paused(self) -> bool:
        return self.rfr("GetPauseState", core.EmptyMessage(), rfr.SingleBool).Value

    def set_paused(self, value: bool) -> None:
        self.rfr("SetPauseState", rfr.SingleBool(Value=value), core.EmptyMessage)

    def dig(self, designation: int, locations) -> None:
        """Designate tiles. `designation` is an rfr.TileDigDesignation value,
        `locations` an iterable of (x, y, z) in tile coordinates."""
        req = rfr.DigCommand(designation=designation)
        for x, y, z in locations:
            req.locations.add(x=x, y=y, z=z)
        self.rfr("SendDigCommand", req, core.EmptyMessage)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise DFHackRPCError("connection closed by DFHack")
        buf += chunk
    return bytes(buf)


if __name__ == "__main__":
    # Smoke check against a running DF: python3 dfhack_rpc.py [host] [port]
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    with DFHackRPC(host, port) as df:
        v = df.version_info()
        print(f"DF {v.dwarf_fortress_version}, DFHack {v.dfhack_version}")
        m = df.map_info()
        print(f"map {m.block_size_x}x{m.block_size_y}x{m.block_size_z} blocks, "
              f"fort {m.world_name_english or m.world_name!r}, paused={df.paused()}")
        print(f"{len(df.units().creature_list)} units")
