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


class ClockAnchorTests(unittest.TestCase):
    """Projection re-rooted at a confirmed session-clock sync point."""

    CONSTANT_TWO_SEGMENT = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"},
            {"tick": 480, "microseconds_per_quarter": 250000, "mode": "constant"},
        ],
    }

    RAMP = {
        "ticks_per_quarter": 480,
        "sample_rate": 48000,
        "tempo_points": [
            {"tick": 0, "microseconds_per_quarter": 600000, "mode": "linear"},
            {"tick": 480, "microseconds_per_quarter": 200000, "mode": "constant"},
        ],
    }

    def test_anchor_spans_constant_segments(self):
        payload = {
            **self.CONSTANT_TWO_SEGMENT,
            "clock_anchor": {
                "tick": 480,
                "time_nanoseconds": 5_000_000_000,
                "sample_frame": 240_000,
            },
            "cues": [
                cue("t0", 0),
                cue("t240", 240),
                cue("t480", 480),
                cue("t960", 960),
                cue("t1440", 1440),
            ],
        }
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(
            [(c["time_nanoseconds"], c["sample_frame"]) for c in cues.values()],
            [
                (4_500_000_000, 216_000),  # 0.5s before the anchor
                (4_750_000_000, 228_000),  # 0.25s before the anchor
                (5_000_000_000, 240_000),  # the anchor tick itself
                (5_250_000_000, 252_000),  # 0.25s after (slower segment)
                (5_500_000_000, 264_000),  # 0.5s after (slower segment)
            ],
        )

    def test_anchor_inside_linear_ramp(self):
        payload = {
            **self.RAMP,
            "clock_anchor": {
                "tick": 240,
                "time_nanoseconds": 3_000_000_000,
                "sample_frame": 144_000,
            },
            "cues": [cue("t0", 0), cue("t120", 120), cue("t480", 480), cue("t720", 720)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # elapsed(240) = 250000us; offsets are exact signed trapezoid areas.
        self.assertEqual(
            [(c["time_nanoseconds"], c["sample_frame"]) for c in cues.values()],
            [
                (2_750_000_000, 132_000),  # -250000us across the ramp
                (2_887_500_000, 138_600),  # -112500us across the ramp
                (3_150_000_000, 151_200),  # +150000us to the ramp end
                (3_250_000_000, 156_000),  # +250000us into the constant tail
            ],
        )

    def test_zero_anchor_at_origin_matches_unanchored(self):
        cues_list = [cue("a", 0), cue("b", 240), cue("c", 480), cue("d", 721)]
        plain = project({**self.RAMP, "cues": cues_list})
        anchored = project(
            {
                **self.RAMP,
                "clock_anchor": {"tick": 0, "time_nanoseconds": 0, "sample_frame": 0},
                "cues": cues_list,
            }
        )
        self.assertEqual(plain, anchored)

    def test_anchor_after_cue_rounds_final_sum_half_even(self):
        # tpq=2000, tempo 3us/quarter: one tick is exactly 1.5ns.
        # Anchor sits after the cues; rounding must apply to the final
        # signed sum (101 - 1.5 = 99.5 -> 100), never to the delta alone
        # (which would give 101 - 2 = 99).
        payload = {
            "ticks_per_quarter": 2000,
            "sample_rate": 1_000_000,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 3, "mode": "constant"}
            ],
            "clock_anchor": {"tick": 1, "time_nanoseconds": 101, "sample_frame": 10},
            "cues": [cue("before", 0), cue("at", 1), cue("after", 2)],
        }
        cues = {c["id"]: c for c in project(payload)}
        self.assertEqual(cues["before"]["time_nanoseconds"], 100)  # 99.5 -> 100
        self.assertEqual(cues["at"]["time_nanoseconds"], 101)
        self.assertEqual(cues["after"]["time_nanoseconds"], 102)  # 102.5 -> 102
        self.assertEqual(cues["before"]["sample_frame"], 10)  # 9.9985 -> 10
        self.assertEqual(cues["after"]["sample_frame"], 10)  # 10.0015 -> 10

    def test_same_tick_cues_stay_identical_with_anchor(self):
        payload = {
            **self.RAMP,
            "clock_anchor": {
                "tick": 480,
                "time_nanoseconds": 9_000_000_000,
                "sample_frame": 432_000,
            },
            "cues": [cue("light", 240), cue("subtitle", 240), cue("record", 240)],
        }
        cues = project(payload)
        self.assertEqual(len({c["time_nanoseconds"] for c in cues}), 1)
        self.assertEqual(len({c["sample_frame"] for c in cues}), 1)

    def test_anchor_beyond_last_tempo_point(self):
        payload = {
            **BASE,
            "clock_anchor": {
                "tick": 9600,
                "time_nanoseconds": 20_000_000_000,
                "sample_frame": 1_000_000,
            },
            "cues": [cue("start", 0), cue("later", 19200)],
        }
        cues = {c["id"]: c for c in project(payload)}
        # Anchor sits at elapsed 10s in the held-tempo region.
        self.assertEqual(cues["start"]["time_nanoseconds"], 10_000_000_000)
        self.assertEqual(cues["start"]["sample_frame"], 520_000)
        self.assertEqual(cues["later"]["time_nanoseconds"], 30_000_000_000)
        self.assertEqual(cues["later"]["sample_frame"], 1_480_000)

    def test_response_shape_is_unchanged_by_anchor(self):
        anchored = validate_and_project(
            {
                **BASE,
                "clock_anchor": {"tick": 0, "time_nanoseconds": 5, "sample_frame": 3},
                "cues": [cue("a", 480)],
            }
        )[1]
        plain = validate_and_project({**BASE, "cues": [cue("a", 480)]})[1]
        self.assertEqual(set(anchored), set(plain))
        self.assertEqual(set(anchored["cues"][0]), set(plain["cues"][0]))

    def test_negative_projection_rejects_whole_request(self):
        payload = {
            **BASE,
            "clock_anchor": {"tick": 480, "time_nanoseconds": 1000, "sample_frame": 5},
            "cues": [cue("early", 0), cue("mid", 240), cue("ok", 480)],
        }
        status, body = validate_and_project(payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("cues", body)
        negative = [e for e in body["error"]["errors"] if e["code"] == "NEGATIVE_PROJECTION"]
        self.assertEqual(
            [e["path"] for e in negative],
            [
                "/cues/0/time_nanoseconds",
                "/cues/0/sample_frame",
                "/cues/1/time_nanoseconds",
                "/cues/1/sample_frame",
            ],
        )
        # Deterministic: an identical request yields an identical body.
        self.assertEqual(validate_and_project(payload)[1], body)

    def test_negative_projection_on_one_clock_only(self):
        base_cues = [cue("x", 0)]
        only_frame = {
            **BASE,
            "clock_anchor": {
                "tick": 480,
                "time_nanoseconds": 5_000_000_000,
                "sample_frame": 5,
            },
            "cues": base_cues,
        }
        errors = errors_for(only_frame)
        self.assertEqual(
            [(e["code"], e["path"]) for e in errors],
            [("NEGATIVE_PROJECTION", "/cues/0/sample_frame")],
        )
        only_ns = {
            **BASE,
            "clock_anchor": {"tick": 480, "time_nanoseconds": 5, "sample_frame": 240_000},
            "cues": base_cues,
        }
        errors = errors_for(only_ns)
        self.assertEqual(
            [(e["code"], e["path"]) for e in errors],
            [("NEGATIVE_PROJECTION", "/cues/0/time_nanoseconds")],
        )

    def test_anchor_must_be_an_object(self):
        for bad in (42, "x", [], None, True):
            payload = {**BASE, "clock_anchor": bad, "cues": [cue("a", 0)]}
            errors = errors_for(payload)
            matches = [
                e
                for e in errors
                if e["code"] == "INVALID_TYPE" and e["path"] == "/clock_anchor"
            ]
            self.assertTrue(matches, f"anchor={bad!r}: {errors}")

    def test_anchor_field_validation(self):
        payload = {**BASE, "clock_anchor": {}, "cues": [cue("a", 0)]}
        errors = errors_for(payload)
        missing = {e["path"] for e in errors if e["code"] == "MISSING_FIELD"}
        self.assertEqual(
            missing,
            {
                "/clock_anchor/tick",
                "/clock_anchor/time_nanoseconds",
                "/clock_anchor/sample_frame",
            },
        )

        payload = {
            **BASE,
            "clock_anchor": {
                "tick": -1,
                "time_nanoseconds": 1.5,
                "sample_frame": True,
                "extra": 0,
            },
            "cues": [cue("a", 0)],
        }
        errors = errors_for(payload)
        codes_paths = {(e["code"], e["path"]) for e in errors}
        self.assertIn(("ANCHOR_OUT_OF_RANGE", "/clock_anchor/tick"), codes_paths)
        self.assertIn(("INVALID_TYPE", "/clock_anchor/time_nanoseconds"), codes_paths)
        self.assertIn(("INVALID_TYPE", "/clock_anchor/sample_frame"), codes_paths)
        self.assertIn(("UNKNOWN_FIELD", "/clock_anchor/extra"), codes_paths)

    def test_anchor_errors_come_after_tempo_and_cue_errors(self):
        payload = {
            **BASE,
            "tempo_points": [
                {"tick": 0, "microseconds_per_quarter": 0, "mode": "constant"}
            ],
            "cues": [cue("a", -3)],
            "clock_anchor": {"tick": -1},
        }
        errors = errors_for(payload)
        codes = [e["code"] for e in errors]
        self.assertEqual(
            codes,
            [
                "NON_POSITIVE_TEMPO",
                "CUE_OUT_OF_RANGE",
                "ANCHOR_OUT_OF_RANGE",
                "MISSING_FIELD",
                "MISSING_FIELD",
            ],
        )

    def test_omitted_anchor_leaves_behavior_untouched(self):
        payload = {**BASE, "cues": [cue("a", 0), cue("b", 480), cue("c", 240)]}
        status, body = validate_and_project(payload)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ticks_per_quarter", "cues"})
        self.assertEqual(
            [(c["time_nanoseconds"], c["sample_frame"]) for c in body["cues"]],
            [(0, 0), (500_000_000, 24_000), (250_000_000, 12_000)],
        )


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


if __name__ == "__main__":
    unittest.main()
