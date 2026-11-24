import struct
import unittest

import msgpack
from fastapi.encoders import jsonable_encoder

from app.max_web_client import MaxWebClient


class MaxWebClientTest(unittest.TestCase):
    def test_qr_expiry_extension_encodes_as_integer_in_api_response(self):
        expires_at = 1_795_080_000_000
        timestamp = msgpack.ExtType(1, b"\xd3" + expires_at.to_bytes(8, "big", signed=True))
        payload = msgpack.packb({"expiresAt": timestamp})
        wire = struct.pack(">BBhhI", 10, 1, 1, 288, len(payload)) + payload

        decoded = MaxWebClient._parse_json(wire)

        self.assertEqual(jsonable_encoder(decoded["payload"]), {"expiresAt": expires_at})
