"""One-shot verification entry point for the ``verify`` Compose service.

Steps, in order:
  1. wait for the application's health endpoint to report ready;
  2. byte-compile every source file (build check);
  3. run the full unit test suite;
  4. run API smoke tests covering constant and linear-ramp segments plus a
     validation-failure case;
  5. run anchored (clock_anchor) API smoke tests across constant and
     linear-ramp segments and the negative-projection rejection;
  6. exit non-zero if anything failed so ``docker compose up`` reports it.
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


def smoke_anchor_constant_segment() -> bool:
    # Session restart on a constant tempo map: one quarter before the restart
    # the recorder read 1.0 s / 48 000 frames; cues must keep their signed
    # offset from that confirmed sync point, including cues before the anchor.
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 250000, "mode": "constant"},
        ],
        "cues": [
            {"id": "before", "tick": 0},
            {"id": "sync", "tick": 480},
            {"id": "after", "tick": 1440},
            {"id": "dup", "tick": 0},
        ],
        "clock_anchor": {
            "tick": 480,
            "time_nanoseconds": 1_000_000_000,
            "sample_frame": 48_000,
        },
    }
    status, body = post(payload)
    if status != 200:
        print(f"[verify] anchor constant smoke expected 200, got {status}: {body}")
        return False
    cues = {c["id"]: c for c in body["cues"]}
    expected = {
        # score tick 0 is 0.5 s / 24 000 frames before the sync point
        "before": (500_000_000, 24_000),
        "sync": (1_000_000_000, 48_000),
        # 0.5 s + two quarters at 250000us after sync = +0.5 s / +24 000
        "after": (1_500_000_000, 72_000),
    }
    for cid, (nanos, frame) in expected.items():
        actual = (cues[cid]["time_nanoseconds"], cues[cid]["sample_frame"])
        if actual != (nanos, frame):
            print(f"[verify] anchor constant cue {cid}: {actual} != {(nanos, frame)}")
            return False
    if (
        cues["before"]["time_nanoseconds"] != cues["dup"]["time_nanoseconds"]
        or cues["before"]["sample_frame"] != cues["dup"]["sample_frame"]
    ):
        print("[verify] anchored same-tick cues projected differently")
        return False
    print("[verify] anchored constant-segment smoke OK")
    return True


def smoke_anchor_linear_segment() -> bool:
    # Anchor sits inside the linear ramp; cues on either side are reached via
    # the unrounded signed time difference to the anchor tick.
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
            {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
        ],
        "cues": [
            {"id": "start", "tick": 0},
            {"id": "mid", "tick": 240},
            {"id": "end", "tick": 480},
            {"id": "tail", "tick": 720},
        ],
        "clock_anchor": {
            "tick": 240,
            "time_nanoseconds": 5_000_000_000,
            "sample_frame": 240_000,
        },
    }
    status, body = post(payload)
    if status != 200:
        print(f"[verify] anchor linear smoke expected 200, got {status}: {body}")
        return False
    cues = {c["id"]: c for c in body["cues"]}
    # Score times: start 0s, mid 0.25s, end 0.4s, tail 0.5s; session shift
    # at mid is +4.75 s / +228 000 frames.
    expected = {
        "start": (4_750_000_000, 228_000),
        "mid": (5_000_000_000, 240_000),
        "end": (5_150_000_000, 247_200),
        "tail": (5_250_000_000, 252_000),
    }
    for cid, (nanos, frame) in expected.items():
        actual = (cues[cid]["time_nanoseconds"], cues[cid]["sample_frame"])
        if actual != (nanos, frame):
            print(f"[verify] anchor linear cue {cid}: {actual} != {(nanos, frame)}")
            return False
    print("[verify] anchored linear-segment smoke OK")
    return True


def smoke_anchor_negative_rejected() -> bool:
    # Tiny session readings at tick 480 push every earlier cue below zero on
    # both clocks; the request must fail wholesale with cue positions.
    payload = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
        ],
        "cues": [
            {"id": "before", "tick": 0},
            {"id": "ok", "tick": 960},
        ],
        "clock_anchor": {
            "tick": 480,
            "time_nanoseconds": 100,
            "sample_frame": 1,
        },
    }
    status, body = post(payload)
    if status != 400:
        print(f"[verify] anchor negative smoke expected 400, got {status}: {body}")
        return False
    if "cues" in body:
        print("[verify] anchor negative smoke leaked partial projections")
        return False
    paths = {
        error["path"]
        for error in body["error"]["errors"]
        if error["code"] == "NEGATIVE_PROJECTION"
    }
    expected = {
        "/cues/0/time_nanoseconds",
        "/cues/0/sample_frame",
    }
    if paths != expected:
        print(f"[verify] anchor negative smoke paths mismatch: {paths}")
        return False

    # Malformed anchor fields are field-level errors.
    bad = dict(payload)
    bad["clock_anchor"] = {"tick": -1, "time_nanoseconds": 0, "sample_frame": 0}
    status, body = post(bad)
    if status != 400:
        print(f"[verify] anchor field smoke expected 400, got {status}: {body}")
        return False
    codes_paths = {(e["code"], e["path"]) for e in body["error"]["errors"]}
    if ("NEGATIVE_VALUE", "/clock_anchor/tick") not in codes_paths:
        print(f"[verify] anchor field smoke missing NEGATIVE_VALUE: {codes_paths}")
        return False
    print("[verify] anchored negative-rejection smoke OK")
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
        ("anchor constant smoke", smoke_anchor_constant_segment),
        ("anchor linear smoke", smoke_anchor_linear_segment),
        ("anchor negative smoke", smoke_anchor_negative_rejected),
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
