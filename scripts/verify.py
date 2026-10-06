"""One-shot verification entry point for the ``verify`` Compose service.

Steps, in order:
  1. wait for the application's health endpoint to report ready;
  2. byte-compile every source file (build check);
  3. run the full unit test suite;
  4. run API smoke tests covering constant and linear-ramp segments plus a
     validation-failure case;
  5. exit non-zero if anything failed so ``docker compose up`` reports it.
"""

from __future__ import annotations

import json
import os
import py_compile
import sys
import time
import unittest
import urllib.error
import urllib.request

BASE_URL = os.environ.get("APP_URL", "http://localhost:8080")
HEALTH_URL = f"{BASE_URL}/health"
PROJECT_URL = f"{BASE_URL}/api/timelines/project"


def wait_for_ready(timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    body = json.loads(response.read().decode())
                    if body.get("status") == "ok":
                        return True
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = exc
        time.sleep(0.5)
    print(f"[verify] application never became ready: {last_error}", file=sys.stderr)
    return False


def build_check() -> bool:
    try:
        import compileall

        ok = compileall.compile_dir("app", quiet=1, force=True)
        ok = compileall.compile_dir("tests", quiet=1, force=True) and ok
        ok = py_compile.compile("scripts/verify.py", doraise=True) is not None and ok
    except py_compile.PyCompileError as exc:
        print(f"[verify] build failed: {exc}", file=sys.stderr)
        return False
    if not ok:
        print("[verify] build (byte-compile) failed", file=sys.stderr)
        return False
    print("[verify] build (byte-compile) OK")
    return True


def run_unit_tests() -> bool:
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def post(payload: dict):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        PROJECT_URL, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def smoke_constant_segment() -> bool:
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 250000, "mode": "constant"},
        ],
        "cues": [
            {"id": "start", "tick": 0},
            {"id": "q1", "tick": 480},
            {"id": "later", "tick": 1440},
            {"id": "dup", "tick": 480},
        ],
    }
    status, body = post(payload)
    if status != 200:
        print(f"[verify] constant smoke expected 200, got {status}: {body}")
        return False
    cues = {c["id"]: c for c in body["cues"]}
    expected = {
        "start": (0, 0),
        "q1": (500_000_000, 24_000),
        # 0.5s + two quarters at 250000us = 1.0s
        "later": (1_000_000_000, 48_000),
    }
    for cid, (nanos, frame) in expected.items():
        actual = (cues[cid]["time_nanoseconds"], cues[cid]["sample_frame"])
        if actual != (nanos, frame):
            print(f"[verify] constant cue {cid}: {actual} != {(nanos, frame)}")
            return False
    if (
        cues["q1"]["time_nanoseconds"] != cues["dup"]["time_nanoseconds"]
        or cues["q1"]["sample_frame"] != cues["dup"]["sample_frame"]
    ):
        print("[verify] same-tick cues projected differently")
        return False
    print("[verify] constant-segment smoke OK")
    return True


def smoke_linear_segment() -> bool:
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
            {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
        ],
        "cues": [
            {"id": "mid", "tick": 240},
            {"id": "end", "tick": 480},
            {"id": "tail", "tick": 720},
        ],
    }
    status, body = post(payload)
    if status != 200:
        print(f"[verify] linear smoke expected 200, got {status}: {body}")
        return False
    cues = {c["id"]: c for c in body["cues"]}
    # Ramp trapezoid: (600000+200000)/2 = 400000us over the quarter = 0.4s.
    # Midpoint: average (600000+400000)/2 over half a quarter = 250000us = 0.25s.
    # Tail: 0.4s + half a quarter at 200000us = 0.5s.
    expected = {
        "mid": (250_000_000, 12_000),
        "end": (400_000_000, 19_200),
        "tail": (500_000_000, 24_000),
    }
    for cid, (nanos, frame) in expected.items():
        actual = (cues[cid]["time_nanoseconds"], cues[cid]["sample_frame"])
        if actual != (nanos, frame):
            print(f"[verify] linear cue {cid}: {actual} != {(nanos, frame)}")
            return False
    print("[verify] linear-segment smoke OK")
    return True


def smoke_validation_rejected() -> bool:
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 0, "mode": "accelerando"},
        ],
        "cues": [{"id": "a", "tick": 0}, {"id": "a", "tick": 12}],
    }
    status, body = post(payload)
    if status != 400:
        print(f"[verify] validation smoke expected 400, got {status}: {body}")
        return False
    if "cues" in body:
        print("[verify] error response leaked partial projections")
        return False
    codes = {error["code"] for error in body["error"]["errors"]}
    for required in ("NON_POSITIVE_TEMPO", "INVALID_MODE", "DUPLICATE_CUE_ID"):
        if required not in codes:
            print(f"[verify] validation smoke missing code {required}: {codes}")
            return False
    print("[verify] validation-failure smoke OK")
    return True


def main() -> int:
    steps = [
        ("readiness", wait_for_ready),
        ("build", build_check),
        ("unit tests", run_unit_tests),
        ("constant smoke", smoke_constant_segment),
        ("linear smoke", smoke_linear_segment),
        ("validation smoke", smoke_validation_rejected),
    ]
    failures = []
    for name, step in steps:
        print(f"\n[verify] === {name} ===", flush=True)
        if not step():
            failures.append(name)

    print()
    if failures:
        print(f"[verify] FAILED steps: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("[verify] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
