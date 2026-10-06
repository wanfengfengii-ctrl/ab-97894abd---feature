"""Unit tests for exact timeline projection and whole-request validation."""

from __future__ import annotations

import unittest
from fractions import Fraction

from app.timeline import validate_and_project


def project(payload):
    status, body = validate_and_project(payload)
    if status != 200:
        raise AssertionError(f"projection failed: {body}")
    return body["cues"]


def errors_for(payload):
    status, body = validate_and_project(payload)
    assert status == 400, body
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return body["error"]["errors"]


BASE = {
    "ticks_per_quarter": 480,
    "sample_rate": 48000,
    "tempo_points": [
        {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
    ],
}


def cue(cid, tick):
    return {"id": cid, "tick": tick}


class ConstantTempoTests(unittest.TestCase):
    def test_basic_constant_grid(self):
        payload = {
            **BASE,
            "cues": [cue("a", 0), cue("b", 480), cue("c", 240), cue("d", 960)],
        }
        cues = project(payload)
        self.assertEqual(
            [(c["id"], c["time_nanoseconds"], c["sample_frame"]) for c in cues],
            [
                ("a", 0, 0),
                ("b", 500_000_000, 24_000),
                ("c", 250_000_000, 12_000),
                ("d", 1_000_000_000, 48_000),
            ],
        )

    def test_order_is_preserved(self):
        ticks = [480, 0, 960, 240, 480]
        payload = {**BASE, "cues": [cue(f"c{i}", tick) for i, tick in enumerate(ticks)]}
        cues = project(payload)
        self.assertEqual([c["tick"] for c in cues], ticks)
        self.assertEqual([c["id"] for c in cues], [f"c{i}" for i in range(5)])

    def test_last_point_holds_forever(self):
        # Tempo changes at 480 to 250000us and then stays there forever.
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
                {"tick": 480, "microseconds_per_quarter": 250000, "mode": "linear"},
            ],
            "cues": [cue("far", 480 + 480 * 10)],
        }
        result = project(payload)[0]
        # 0.5s for the first quarter + 10 quarters at 250000us = 3.0s
        self.assertEqual(result["time_nanoseconds"], 3_000_000_000)
        self.assertEqual(result["sample_frame"], 144_000)

    def test_no_accumulated_error_across_many_segments(self):
        # 300 constant one-tick segments at 1us/quarter, tpq=3 => 100us total.
        points = [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(300)
        ]
        payload = {
            "ticks_per_quarter": 3,
            "sample_rate": 3_000_000,
            "tempo_points": points,
            "cues": [cue("end", 300)],
        }
        result = project(payload)[0]
        self.assertEqual(result["time_nanoseconds"], 100_000)
        self.assertEqual(result["sample_frame"], 300)


class LinearRampTests(unittest.TestCase):
    def test_ramp_midpoint_and_endpoint(self):
        # Tempo ramps linearly from 600000 to 200000 us over one quarter.
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
            ],
            "cues": [cue("mid", 240), cue("end", 480), cue("start", 0)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # Full segment: trapezoid (600000+200000)/2 = 400000us = 0.4s
        self.assertEqual(cues["end"]["time_nanoseconds"], 400_000_000)
        self.assertEqual(cues["end"]["sample_frame"], 19_200)
        # Half segment: average tempo at midpoint (600000+400000)/2=500000us,
        # over half a quarter => 250000us = 0.25s
        self.assertEqual(cues["mid"]["time_nanoseconds"], 250_000_000)
        self.assertEqual(cues["mid"]["sample_frame"], 12_000)
        self.assertEqual(cues["start"]["time_nanoseconds"], 0)

    def test_ramp_matches_independent_fraction_calculation(self):
        tpq = 480
        a_tempo, b_tempo = 499_999, 500_007
        a_tick, b_tick = 120, 840
        points = [
            {"tick": 0, "microseconds_per_quarter": 333_333, "mode": "constant"},
            {"tick": a_tick, "microseconds_per_quarter": a_tempo, "mode": "linear"},
            {"tick": b_tick, "microseconds_per_quarter": b_tempo, "mode": "constant"},
        ]
        ticks = [0, 60, 120, 121, 480, 839, 840, 1000]
        payload = {
            "ticks_per_quarter": tpq,
            "sample_rate": 96_000,
            "tempo_points": points,
            "cues": [cue(str(t), t) for t in ticks],
        }
        cues = {c["id"]: c for c in project(payload)}

        def half_even(frac: Fraction) -> int:
            q, r = divmod(frac.numerator, frac.denominator)
            if 2 * r > frac.denominator or (2 * r == frac.denominator and q % 2):
                return q + 1
            return q

        for tick in ticks:
            if tick <= a_tick:
                elapsed = Fraction(333_333 * tick, tpq)
            elif tick < b_tick:
                elapsed = Fraction(333_333 * a_tick, tpq)
                d = tick - a_tick
                avg = a_tempo + Fraction((b_tempo - a_tempo) * d, 2 * (b_tick - a_tick))
                elapsed += avg * Fraction(d, tpq)
            else:
                elapsed = Fraction(333_333 * a_tick, tpq)
                elapsed += Fraction((a_tempo + b_tempo) * (b_tick - a_tick), 2 * tpq)
                elapsed += Fraction(b_tempo * (tick - b_tick), tpq)
            expected_ns = half_even(elapsed * 1000)
            expected_frame = half_even(elapsed * 96_000 / Fraction(1_000_000))
            self.assertEqual(cues[str(tick)]["time_nanoseconds"], expected_ns, tick)
            self.assertEqual(cues[str(tick)]["sample_frame"], expected_frame, tick)

    def test_ramp_with_no_cues_inside_is_exact(self):
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1, "mode": "linear"},
                {"tick": 1, "microseconds_per_quarter": 2, "mode": "constant"},
            ],
            "cues": [cue("a", 0), cue("b", 1)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # (1+2)/2 * (1/480) us = 1.5/480 us = 3.125 ns -> half-even 3 ns
        self.assertEqual(cues["b"]["time_nanoseconds"], 3)


class HalfEvenRoundingTests(unittest.TestCase):
    def test_nanosecond_halves_round_to_even(self):
        # tpq=16, tempo 1us: tick 1 => 62.5ns -> 62; tick 3 => 187.5ns -> 188
        payload = {
            "ticks_per_quarter": 16,
            "sample_rate": 1,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
            ],
            "cues": [cue("one", 1), cue("three", 3), cue("two", 2)],
        }
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(cues["one"]["time_nanoseconds"], 62)    # 62.5 -> 62
        self.assertEqual(cues["three"]["time_nanoseconds"], 188)  # 187.5 -> 188
        self.assertEqual(cues["two"]["time_nanoseconds"], 125)

    def test_sample_frame_halves_round_to_even(self):
        # tpq=1, tempo 1us, tick 1 => elapsed 1us.
        # sr 500000 -> 0.5 frames -> 0; sr 1500000 -> 1.5 frames -> 2.
        for sr, expected in ((500_000, 0), (1_500_000, 2), (2_500_000, 2)):
            payload = {
                "ticks_per_quarter": 1,
                "sample_rate": sr,
                "tempo_points": [
                    {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
                ],
                "cues": [cue("x", 1)],
            }
            self.assertEqual(project(payload)[0]["sample_frame"], expected, sr)


class SameTickTests(unittest.TestCase):
    def test_cues_at_same_tick_are_identical(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 240000, "mode": "constant"},
            ],
            "cues": [
                cue("light", 240),
                cue("subtitle", 240),
                cue("record", 240),
            ],
        }
        cues = project(payload)
        ns = {c["time_nanoseconds"] for c in cues}
        frames = {c["sample_frame"] for c in cues}
        self.assertEqual(len(ns), 1)
        self.assertEqual(len(frames), 1)

    def test_cue_exactly_on_tempo_tick_matches_cumulative(self):
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 500000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
                {"tick": 960, "microseconds_per_quarter": 100000, "mode": "constant"},
            ],
            "cues": [cue("at480", 480), cue("at960", 960)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # Ramp 500000->200000 across one quarter = 350000us.
        self.assertEqual(cues["at480"]["time_nanoseconds"], 350_000_000)
        # + one quarter at 200000us = 550000us.
        self.assertEqual(cues["at960"]["time_nanoseconds"], 550_000_000)


class ValidationTests(unittest.TestCase):
    def assert_code_at(self, payload, code, path):
        errors = errors_for(payload)
        matches = [e for e in errors if e["code"] == code and e["path"] == path]
        self.assertTrue(matches, f"expected {code} at {path}, got {errors}")

    def test_first_point_must_be_at_zero(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 12, "microseconds_per_quarter": 500000, "mode": "constant"}
            ],
            "cues": [cue("a", 0)],
        }
        self.assert_code_at(payload, "TEMPO_GAP", "/tempo_points/0/tick")

    def test_duplicate_and_unordered_tempo_ticks(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 400000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 300000, "mode": "linear"},
            {"tick": 200, "microseconds_per_quarter": 200000, "mode": "constant"},
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        errors = errors_for(payload)
        codes_paths = {(e["code"], e["path"]) for e in errors}
        self.assertIn(
            ("DUPLICATE_TEMPO_TICK", "/tempo_points/2/tick"), codes_paths
        )
        self.assertIn(
            ("TEMPO_NOT_ORDERED", "/tempo_points/3/tick"), codes_paths
        )

    def test_invalid_mode(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "rubato"}
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        self.assert_code_at(payload, "INVALID_MODE", "/tempo_points/0/mode")

    def test_non_positive_tempo_with_position(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 0, "mode": "constant"},
            {"tick": 960, "microseconds_per_quarter": -7, "mode": "linear"},
        ]
        payload = {**BASE, "tempo_points": points, "cues": [cue("a", 0)]}
        errors = errors_for(payload)
        paths = {e["path"] for e in errors if e["code"] == "NON_POSITIVE_TEMPO"}
        self.assertEqual(
            paths,
            {
                "/tempo_points/1/microseconds_per_quarter",
                "/tempo_points/2/microseconds_per_quarter",
            },
        )

    def test_negative_cue_tick(self):
        payload = {**BASE, "cues": [cue("ok", 0), cue("bad", -1)]}
        self.assert_code_at(payload, "CUE_OUT_OF_RANGE", "/cues/1/tick")

    def test_duplicate_cue_ids_report_both_positions(self):
        payload = {**BASE, "cues": [cue(7, 0), cue(7, 480), cue("x", 12)]}
        errors = errors_for(payload)
        dup = [e for e in errors if e["code"] == "DUPLICATE_CUE_ID"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["path"], "/cues/1/id")
        self.assertIn("/cues/0", dup[0]["message"])

    def test_count_limits(self):
        too_many_points = [
            {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
        ] + [
            {"tick": i, "microseconds_per_quarter": 1, "mode": "constant"}
            for i in range(1, 501)
        ]
        self.assertEqual(len(too_many_points), 501)
        payload = {**BASE, "tempo_points": too_many_points, "cues": [cue("a", 0)]}
        self.assert_code_at(payload, "INVALID_COUNT", "/tempo_points")

        payload = {**BASE, "cues": []}
        self.assert_code_at(payload, "INVALID_COUNT", "/cues")

        too_many_cues = [cue(i, i) for i in range(2001)]
        payload = {**BASE, "cues": too_many_cues}
        self.assert_code_at(payload, "INVALID_COUNT", "/cues")

    def test_type_errors_including_bool_rejection(self):
        payload = {
            "ticks_per_quarter": True,
            "sample_rate": "48000",
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1.5, "mode": "constant"}
            ],
            "cues": [{"id": None, "tick": 0}],
        }
        errors = errors_for(payload)
        paths = {e["path"] for e in errors if e["code"] == "INVALID_TYPE"}
        self.assertIn("/ticks_per_quarter", paths)
        self.assertIn("/sample_rate", paths)
        self.assertIn("/tempo_points/0/microseconds_per_quarter", paths)
        self.assertIn("/cues/0/id", paths)

    def test_missing_and_unknown_fields(self):
        payload = {"sample_rate": 48000}
        errors = errors_for(payload)
        missing = {e["path"] for e in errors if e["code"] == "MISSING_FIELD"}
        self.assertEqual(missing, {"/ticks_per_quarter", "/tempo_points", "/cues"})

        payload = {**BASE, "cues": [cue("a", 0)], "extra": 1}
        self.assert_code_at(payload, "UNKNOWN_FIELD", "/extra")

    def test_errors_never_carry_partial_projections(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 0, "mode": "constant"}
            ],
            "cues": [cue("a", 0), cue("b", 480)],
        }
        status, body = validate_and_project(payload)
        self.assertEqual(status, 400)
        self.assertNotIn("cues", body)
        self.assertIn("errors", body["error"])

    def test_non_object_body(self):
        for payload in ([], "x", 42, None):
            status, body = validate_and_project(payload)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "INVALID_REQUEST")

    def test_error_codes_are_stable_strings(self):
        payload = {**BASE, "cues": [cue("a", -1)]}
        errors = errors_for(payload)
        self.assertTrue(all(isinstance(e["code"], str) for e in errors))
        # A second identical request yields byte-identical error content.
        _, body1 = validate_and_project(payload)
        _, body2 = validate_and_project(payload)
        self.assertEqual(body1, body2)


class ClockAnchorTests(unittest.TestCase):
    def anchor_payload(self, anchor, cues, **overrides):
        payload = {**BASE, "cues": [cue(cid, tick) for cid, tick in cues]}
        if anchor is not None:
            payload["clock_anchor"] = anchor
        payload.update(overrides)
        return payload

    def test_anchor_at_origin_shifts_every_cue(self):
        payload = self.anchor_payload(
            {"tick": 0, "time_nanoseconds": 1_000_000_000, "sample_frame": 48_000},
            [("a", 0), ("b", 480), ("c", 240)],
        )
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(
            (cues["a"]["time_nanoseconds"], cues["a"]["sample_frame"]),
            (1_000_000_000, 48_000),
        )
        self.assertEqual(
            (cues["b"]["time_nanoseconds"], cues["b"]["sample_frame"]),
            (1_500_000_000, 72_000),
        )
        self.assertEqual(
            (cues["c"]["time_nanoseconds"], cues["c"]["sample_frame"]),
            (1_250_000_000, 60_000),
        )

    def test_cue_at_anchor_tick_reproduces_anchor_readings_exactly(self):
        # Anchor readings that do not match the score-clock projection must
        # still come back byte-for-byte for a cue sitting on the anchor tick.
        payload = self.anchor_payload(
            {"tick": 480, "time_nanoseconds": 777, "sample_frame": 3},
            [("at", 480), ("later", 960)],
        )
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(
            (cues["at"]["time_nanoseconds"], cues["at"]["sample_frame"]), (777, 3)
        )
        self.assertEqual(
            (cues["later"]["time_nanoseconds"], cues["later"]["sample_frame"]),
            (777 + 500_000_000, 3 + 24_000),
        )

    def test_projection_is_independent_of_anchor_position(self):
        # Both anchors describe the same restarted session clock; a cue that
        # lies between them must project identically whether the signed delta
        # is added (anchor earlier) or subtracted (anchor later).
        cues = [("before", 0), ("mid", 240), ("after", 960)]
        anchored_at_origin = self.anchor_payload(
            {"tick": 0, "time_nanoseconds": 1_000_000_000, "sample_frame": 48_000},
            cues,
        )
        anchored_at_q1 = self.anchor_payload(
            {"tick": 480, "time_nanoseconds": 1_500_000_000, "sample_frame": 72_000},
            cues,
        )
        self.assertEqual(project(anchored_at_origin), project(anchored_at_q1))

    def test_half_even_rounding_uses_final_anchored_value(self):
        # tpq=1, tempo 1us/quarter, sr=1_500_000:
        # tick 1 -> 1000 ns, 1.5 frames.  Session frame offset of 1 turns the
        # cue frame into 2.5 -> half-even 2 from either side of the anchor.
        points = [
            {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
        ]
        common = {
            "ticks_per_quarter": 1,
            "sample_rate": 1_500_000,
            "tempo_points": points,
            "cues": [cue("c", 1)],
        }
        earlier = {**common, "clock_anchor": {"tick": 0, "time_nanoseconds": 62, "sample_frame": 1}}
        later = {**common, "clock_anchor": {"tick": 2, "time_nanoseconds": 2062, "sample_frame": 4}}
        for payload in (earlier, later):
            result = project(payload)[0]
            self.assertEqual(result["time_nanoseconds"], 1062)
            self.assertEqual(result["sample_frame"], 2)  # 2.5 -> even 2

        # An anchor whose integer reading pulls a cue onto a half in the other
        # direction rounds to the even integer as well.
        payload = {
            "ticks_per_quarter": 16,
            "sample_rate": 1,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 1, "mode": "constant"}
            ],
            "cues": [cue("x", 2)],
            "clock_anchor": {
                "tick": 1,  # score projection 62.5 ns
                "time_nanoseconds": 62,
                "sample_frame": 0,
            },
        }
        # 125 + (62 - 62.5) = 124.5 -> 124
        self.assertEqual(project(payload)[0]["time_nanoseconds"], 124)

    def test_anchor_across_linear_ramp(self):
        points = [
            {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
            {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
        ]
        payload = {
            "ticks_per_quarter": 480,
            "sample_rate": 48000,
            "tempo_points": points,
            "cues": [cue(str(tick), tick) for tick in (0, 240, 480, 720)],
            "clock_anchor": {
                "tick": 240,
                "time_nanoseconds": 5_000_000_000,
                "sample_frame": 240_000,
            },
        }
        cues = {c["id"]: c for c in project(payload)}
        expected = {
            "0": (4_750_000_000, 228_000),
            "240": (5_000_000_000, 240_000),
            "480": (5_150_000_000, 247_200),
            "720": (5_250_000_000, 252_000),
        }
        for cid, (ns, frame) in expected.items():
            self.assertEqual(
                (cues[cid]["time_nanoseconds"], cues[cid]["sample_frame"]),
                (ns, frame),
                cid,
            )

    def test_anchor_tick_beyond_last_tempo_point(self):
        payload = self.anchor_payload(
            {"tick": 100_000, "time_nanoseconds": 9, "sample_frame": 9},
            [("far", 100_048)],
        )
        result = project(payload)[0]
        self.assertEqual(result["time_nanoseconds"], 9 + 50_000_000)
        self.assertEqual(result["sample_frame"], 9 + 2_400)

    def test_same_tick_cues_still_identical_with_anchor(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
                {"tick": 480, "microseconds_per_quarter": 240000, "mode": "constant"},
            ],
            "cues": [cue("light", 240), cue("subtitle", 240), cue("record", 240)],
            "clock_anchor": {
                "tick": 960,
                "time_nanoseconds": 12_000_000_000,
                "sample_frame": 576_000,
            },
        }
        cues = project(payload)
        self.assertEqual(len({c["time_nanoseconds"] for c in cues}), 1)
        self.assertEqual(len({c["sample_frame"] for c in cues}), 1)


class AnchorValidationTests(unittest.TestCase):
    def assert_code_at(self, payload, code, path):
        errors = errors_for(payload)
        matches = [e for e in errors if e["code"] == code and e["path"] == path]
        self.assertTrue(matches, f"expected {code} at {path}, got {errors}")

    def base_anchor(self, **changes):
        anchor = {"tick": 480, "time_nanoseconds": 1, "sample_frame": 1}
        anchor.update(changes)
        return {**BASE, "cues": [cue("a", 0)], "clock_anchor": anchor}

    def test_anchor_must_be_object(self):
        payload = {**BASE, "cues": [cue("a", 0)], "clock_anchor": [1, 2, 3]}
        self.assert_code_at(payload, "INVALID_TYPE", "/clock_anchor")

    def test_anchor_missing_fields(self):
        payload = {
            **BASE,
            "cues": [cue("a", 0)],
            "clock_anchor": {"tick": 12},
        }
        errors = errors_for(payload)
        paths = {e["path"] for e in errors if e["code"] == "MISSING_FIELD"}
        self.assertEqual(
            paths,
            {"/clock_anchor/time_nanoseconds", "/clock_anchor/sample_frame"},
        )

    def test_anchor_field_types(self):
        for field in ("tick", "time_nanoseconds", "sample_frame"):
            for bad in ("1", 1.5, True, None):
                payload = self.base_anchor(**{field: bad})
                self.assert_code_at(
                    payload, "INVALID_TYPE", f"/clock_anchor/{field}"
                )

    def test_anchor_negative_fields_rejected(self):
        for field in ("tick", "time_nanoseconds", "sample_frame"):
            payload = self.base_anchor(**{field: -1})
            self.assert_code_at(
                payload, "NEGATIVE_VALUE", f"/clock_anchor/{field}"
            )

    def test_anchor_unknown_field(self):
        anchor = {
            "tick": 0,
            "time_nanoseconds": 0,
            "sample_frame": 0,
            "extra": 1,
        }
        payload = {**BASE, "cues": [cue("a", 0)], "clock_anchor": anchor}
        self.assert_code_at(payload, "UNKNOWN_FIELD", "/clock_anchor/extra")

    def test_anchor_errors_join_other_errors_in_stable_order(self):
        payload = {
            **BASE,
            "sample_rate": "48000",
            "cues": [cue("a", 0)],
            "clock_anchor": {"tick": -1, "time_nanoseconds": 0},
        }
        errors = errors_for(payload)
        codes_paths = [(e["code"], e["path"]) for e in errors]
        # Deterministic positions and no projections leaked.
        self.assertIn(("INVALID_TYPE", "/sample_rate"), codes_paths)
        self.assertIn(("NEGATIVE_VALUE", "/clock_anchor/tick"), codes_paths)
        self.assertIn(
            ("MISSING_FIELD", "/clock_anchor/sample_frame"), codes_paths
        )

    def test_negative_projection_reports_each_clock_and_cue(self):
        # Anchor one quarter in with tiny readings: tick 0 is negative on
        # both clocks, tick 240 is negative only in nanoseconds.
        payload = {
            **BASE,
            "cues": [cue("a", 0), cue("b", 240), cue("c", 480)],
            "clock_anchor": {
                "tick": 480,
                "time_nanoseconds": 100,
                "sample_frame": 12_000,
            },
        }
        status, body = validate_and_project(payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("cues", body)
        errors = body["error"]["errors"]
        paths = [e["path"] for e in errors if e["code"] == "NEGATIVE_PROJECTION"]
        self.assertEqual(
            paths,
            [
                "/cues/0/time_nanoseconds",
                "/cues/0/sample_frame",
                "/cues/1/time_nanoseconds",
            ],
        )

    def test_projection_at_exactly_zero_is_accepted(self):
        # Cue tick 0 reproduces the anchor readings shifted by the full
        # pre-anchor span; choose readings so the frame lands exactly at zero.
        payload = {
            **BASE,
            "cues": [cue("zero", 0), cue("ok", 480)],
            "clock_anchor": {
                "tick": 480,
                "time_nanoseconds": 500_000_000,
                "sample_frame": 24_000,
            },
        }
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(
            (cues["zero"]["time_nanoseconds"], cues["zero"]["sample_frame"]),
            (0, 0),
        )

    def test_omitting_anchor_leaves_response_unchanged(self):
        payload = {**BASE, "cues": [cue("a", 480)]}
        status, body = validate_and_project(payload)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ticks_per_quarter", "cues"})
        self.assertEqual(body["cues"][0]["sample_frame"], 24_000)


if __name__ == "__main__":
    unittest.main()
