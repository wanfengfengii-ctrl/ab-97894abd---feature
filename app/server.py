"""HTTP front end for the timeline projection service.

Pure standard library so the container image needs nothing beyond Python.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.timeline import validate_and_project

DEFAULT_PORT = 8080
MAX_BODY_BYTES = 8 * 1024 * 1024


class TimelineHandler(BaseHTTPRequestHandler):
    server_version = "TimelineService/1.0"

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path in ("/health", "/healthz", "/ready"):
            self._write_json(HTTPStatus.OK, {"status": "ok"})
        else:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "NOT_FOUND", "message": self.path}},
            )

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/api/timelines/project":
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "NOT_FOUND", "message": self.path}},
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "code": "INVALID_HEADERS",
                        "message": "Content-Length must be an integer",
                    }
                },
            )
            return

        if length <= 0:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": {"code": "EMPTY_BODY", "message": "request body is empty"}},
            )
            return
        if length > MAX_BODY_BYTES:
            self._write_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": {
                        "code": "BODY_TOO_LARGE",
                        "message": f"body must not exceed {MAX_BODY_BYTES} bytes",
                    }
                },
            )
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "code": "MALFORMED_JSON",
                        "message": f"request body is not valid JSON: {exc}",
                    }
                },
            )
            return

        status, body = validate_and_project(payload)
        self._write_json(status, body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write(
            "%s - - %s\n" % (self.address_string(), fmt % args)
        )


def main() -> int:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("HOST", "0.0.0.0")
    httpd = ThreadingHTTPServer((host, port), TimelineHandler)

    stop_event = threading.Event()

    def handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print(f"timeline service listening on {host}:{port}", flush=True)

    stop_event.wait()
    httpd.shutdown()
    httpd.server_close()
    server_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
