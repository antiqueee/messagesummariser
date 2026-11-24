"""Binary transport for the current MAX web client protocol."""

import asyncio
import json
import struct

import lz4.block
import msgpack
import websockets
from pymax import MaxClient
from pymax.payloads import UserAgentPayload
from pymax.static.constant import WEBSOCKET_ORIGIN


MAX_WEB_SOCKET_URI = "wss://api.oneme.ru/websocket"
_HEADER = struct.Struct(">BBhhI")
_LENGTH_MASK = (1 << 24) - 1
_MAX_DECOMPRESSED_SIZE = 32 * 1024 * 1024
_VERSION = 10


def _decode_extension(code: int, data: bytes):
    # MAX wraps 64-bit IDs and millisecond timestamps in extension type 1.
    if code == 1:
        value = msgpack.unpackb(data)
        if type(value) is int:
            return value
    return msgpack.ExtType(code, data)


class _BinaryConnection(websockets.ClientConnection):
    async def send(self, message: str):
        # pymax still serializes its outbound messages as JSON.
        frame = json.loads(message)
        payload = msgpack.packb(frame["payload"], use_bin_type=True)
        if len(payload) > _LENGTH_MASK:
            raise ValueError("MAX web payload is too large")
        header = _HEADER.pack(_VERSION, frame["cmd"], frame["seq"], frame["opcode"], len(payload))
        await super().send(header + payload)


class MaxWebClient(MaxClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, uri=MAX_WEB_SOCKET_URI, **kwargs)

    async def connect(self, user_agent: UserAgentPayload | None = None):
        if self._ws is not None or self.is_connected:
            return None

        user_agent = user_agent or self.user_agent
        self._ws = await websockets.connect(
            self.uri,
            origin=WEBSOCKET_ORIGIN,
            user_agent_header=user_agent.header_user_agent,
            proxy=self.proxy,
            create_connection=_BinaryConnection,
        )
        self.is_connected = True
        self._incoming = asyncio.Queue()
        self._outgoing = asyncio.Queue()
        self._pending = {}
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._outgoing_task = asyncio.create_task(self._outgoing_loop())
        try:
            return await self._handshake(user_agent)
        except BaseException:
            await self._cleanup_client()
            raise

    def _make_message(self, opcode, payload, cmd=0):
        if self._seq == 32767:
            self._seq = -1
        return super()._make_message(opcode, payload, cmd)

    @staticmethod
    def _parse_json(raw):
        # pymax's receive loop calls this decoder for each WebSocket message.
        if not isinstance(raw, bytes) or len(raw) < _HEADER.size:
            raise ValueError("Invalid MAX web frame")
        version, cmd, seq, opcode, packed_length = _HEADER.unpack_from(raw)
        size = packed_length & _LENGTH_MASK
        if version != _VERSION or len(raw) != _HEADER.size + size:
            raise ValueError("Invalid MAX web frame header")

        payload = raw[_HEADER.size:]
        compression_factor = packed_length >> 24
        if compression_factor:
            buffer_size = compression_factor * size
            if buffer_size > _MAX_DECOMPRESSED_SIZE:
                raise ValueError("MAX web payload is too large to decompress")
            payload = lz4.block.decompress(payload, uncompressed_size=buffer_size)
        decoded = (
            msgpack.unpackb(payload, raw=False, strict_map_key=False, ext_hook=_decode_extension)
            if payload else {}
        )
        return {
            "ver": version,
            "cmd": cmd,
            "seq": seq,
            "opcode": opcode,
            "payload": decoded,
        }
