"""End-to-end tests against the running HTTP server (no network sockets:
the handler is driven directly over BytesIO).
"""

from __future__ import annotations

import io
import json
import unittest
from http.server import ThreadingHTTPServer

from app.server import TimelineHandler


def request(method: str, path: str, body: bytes | None = None, headers=None):
    raw = []
    raw.append(f"{method} {path} HTTP/1.1\r\n".encode())
    raw.append(b"Host: localhost\r\n")
    if body is not None:
        raw.append(f"Content-Length: {len(body)}\r\n".encode())
        raw.append(b"Content-Type: application/json\r\n")
    for key, value in (headers or {}).items():
        raw.append(f"{key}: {value}\r\n".encode())
    raw.append(b"Connection: close\r\n\r\n")
    if body is not None:
        raw.append(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), TimelineHandler)
    input_stream = io.BytesIO(b"".join(raw))
    output_stream = io.BytesIO()

    class FakeConnection:
        # BaseHTTPRequestHandler reads via makefile but (wbufsize=0) writes
        # straight through a _SocketWriter backed by sendall.
        def makefile(self, *_args, **_kwargs):
            return input_stream

        def sendall(self, data):
            output_stream.write(data)

    TimelineHandler(FakeConnection(), ("127.0.0.1", 0), server)
    server.server_close()

    response = output_stream.getvalue()
    head, _, payload = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode()
    status_code = int(status_line.split()[1])
    return status_code, json.loads(payload) if payload else None


GOOD_BODY = {
    "ticks_per_quarter": 480,
    "sample_rate": 48000,
    "tempo_points": [
        {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
    ],
    "cues": [{"id": "a", "tick": 480}],
}


class HttpApiTests(unittest.TestCase):
    def test_health(self):
        status, body = request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_projection_success(self):
        status, body = request(
            "POST", "/api/timelines/project", json.dumps(GOOD_BODY).encode()
        )
        self.assertEqual(status, 200)
        cue = body["cues"][0]
        self.assertEqual(cue["id"], "a")
        self.assertEqual(cue["time_nanoseconds"], 500_000_000)
        self.assertEqual(cue["sample_frame"], 24_000)

    def test_validation_error_is_400_without_projections(self):
        bad = dict(GOOD_BODY)
        bad["tempo_points"] = [
            {"tick": 3, "microseconds_per_quarter": 0, "mode": "weird"}
        ]
        status, body = request(
            "POST", "/api/timelines/project", json.dumps(bad).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("cues", body)
        codes = {e["code"] for e in body["error"]["errors"]}
        self.assertIn("TEMPO_GAP", codes)
        self.assertIn("NON_POSITIVE_TEMPO", codes)
        self.assertIn("INVALID_MODE", codes)

    def test_anchored_projection(self):
        body = dict(GOOD_BODY)
        body["clock_anchor"] = {
            "tick": 480,
            "time_nanoseconds": 10_000_000_000,
            "sample_frame": 480_000,
        }
        status, response = request(
            "POST", "/api/timelines/project", json.dumps(body).encode()
        )
        self.assertEqual(status, 200)
        cue = response["cues"][0]
        # The only cue sits on the anchor tick, so readings are reproduced.
        self.assertEqual(cue["time_nanoseconds"], 10_000_000_000)
        self.assertEqual(cue["sample_frame"], 480_000)

    def test_anchored_negative_projection_is_400(self):
        body = dict(GOOD_BODY)
        body["cues"] = [{"id": "before", "tick": 0}, {"id": "at", "tick": 480}]
        body["clock_anchor"] = {
            "tick": 480,
            "time_nanoseconds": 1,
            "sample_frame": 1,
        }
        status, response = request(
            "POST", "/api/timelines/project", json.dumps(body).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("cues", response)
        codes_paths = {
            (e["code"], e["path"]) for e in response["error"]["errors"]
        }
        self.assertIn(
            ("NEGATIVE_PROJECTION", "/cues/0/time_nanoseconds"), codes_paths
        )
        self.assertIn(
            ("NEGATIVE_PROJECTION", "/cues/0/sample_frame"), codes_paths
        )

    def test_anchor_field_validation(self):
        body = dict(GOOD_BODY)
        body["clock_anchor"] = {"tick": -1}
        status, response = request(
            "POST", "/api/timelines/project", json.dumps(body).encode()
        )
        self.assertEqual(status, 400)
        codes_paths = {
            (e["code"], e["path"]) for e in response["error"]["errors"]
        }
        self.assertIn(("NEGATIVE_VALUE", "/clock_anchor/tick"), codes_paths)
        self.assertIn(
            ("MISSING_FIELD", "/clock_anchor/time_nanoseconds"), codes_paths
        )
        self.assertIn(
            ("MISSING_FIELD", "/clock_anchor/sample_frame"), codes_paths
        )

    def test_malformed_json(self):
        status, body = request(
            "POST", "/api/timelines/project", b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "MALFORMED_JSON")

    def test_unknown_route(self):
        status, _ = request("POST", "/nope", b"{}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
