"""Exact projection of musical-tick cues onto the audio sample clock.

All accumulation is done with :class:`fractions.Fraction` so that tempo
automation across many segments cannot accumulate rounding error.  The two
reported integers (nanosecond time and zero-based sample frame) are each
rounded once, at the end, using round-half-to-even ("banker's rounding").
"""

from __future__ import annotations

from bisect import bisect_right
from fractions import Fraction
from typing import Any

MICROS_PER_QUARTER_FIELD = "microseconds_per_quarter"
TICK_FIELD = "tick"
MODE_FIELD = "mode"
ID_FIELD = "id"

MODE_CONSTANT = "constant"
MODE_LINEAR = "linear"
VALID_MODES = (MODE_CONSTANT, MODE_LINEAR)

MAX_TEMPO_POINTS = 500
MAX_CUES = 2000


class TimelineError(Exception):
    """Raised for logic errors that should never occur on validated input."""


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python; JSON booleans are not integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _round_half_even(value: Fraction) -> int:
    """Round a non-negative Fraction to the nearest integer, halves to even."""
    if value < 0:
        raise TimelineError("negative duration cannot be projected")
    floor, remainder = divmod(value.numerator, value.denominator)
    denominator = value.denominator
    doubled = 2 * remainder
    if doubled > denominator or (doubled == denominator and floor % 2 == 1):
        return floor + 1
    return floor


def _err(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def validate_and_project(payload: Any) -> tuple[int, dict[str, Any]]:
    """Validate a decoded request payload and project every cue.

    Returns ``(http_status, response_body)``.  On failure the body contains
    *all* deterministic validation errors and never any partial projections.
    """
    errors: list[dict[str, str]] = []

    if not isinstance(payload, dict):
        return 400, {
            "error": {
                "code": "INVALID_REQUEST",
                "message": "request body must be a JSON object",
                "errors": [_err("INVALID_TYPE", "", "expected object")],
            }
        }

    known_fields = {"ticks_per_quarter", "sample_rate", "tempo_points", "cues"}
    for field in payload:
        if field not in known_fields:
            errors.append(_err("UNKNOWN_FIELD", f"/{field}", "unexpected field"))

    ticks_per_quarter = payload.get("ticks_per_quarter")
    sample_rate = payload.get("sample_rate")

    if "ticks_per_quarter" not in payload:
        errors.append(
            _err("MISSING_FIELD", "/ticks_per_quarter", "field is required")
        )
    elif not _is_int(ticks_per_quarter):
        errors.append(
            _err("INVALID_TYPE", "/ticks_per_quarter", "expected integer")
        )
    elif ticks_per_quarter < 1:
        errors.append(
            _err(
                "NON_POSITIVE_VALUE",
                "/ticks_per_quarter",
                "ticks per quarter note must be a positive integer",
            )
        )

    if "sample_rate" not in payload:
        errors.append(_err("MISSING_FIELD", "/sample_rate", "field is required"))
    elif not _is_int(sample_rate):
        errors.append(_err("INVALID_TYPE", "/sample_rate", "expected integer"))
    elif sample_rate < 1:
        errors.append(
            _err(
                "NON_POSITIVE_VALUE",
                "/sample_rate",
                "sample rate must be a positive integer",
            )
        )

    raw_points = payload.get("tempo_points")
    if "tempo_points" not in payload:
        errors.append(_err("MISSING_FIELD", "/tempo_points", "field is required"))
        raw_points = None
    elif not isinstance(raw_points, list):
        errors.append(
            _err("INVALID_TYPE", "/tempo_points", "expected array")
        )
        raw_points = None
    elif not 1 <= len(raw_points) <= MAX_TEMPO_POINTS:
        errors.append(
            _err(
                "INVALID_COUNT",
                "/tempo_points",
                f"expected between 1 and {MAX_TEMPO_POINTS} tempo points",
            )
        )

    raw_cues = payload.get("cues")
    if "cues" not in payload:
        errors.append(_err("MISSING_FIELD", "/cues", "field is required"))
        raw_cues = None
    elif not isinstance(raw_cues, list):
        errors.append(_err("INVALID_TYPE", "/cues", "expected array"))
        raw_cues = None
    elif not 1 <= len(raw_cues) <= MAX_CUES:
        errors.append(
            _err(
                "INVALID_COUNT",
                "/cues",
                f"expected between 1 and {MAX_CUES} cues",
            )
        )

    # Parse tempo points.
    points: list[tuple[int, int, str] | None] = []
    if isinstance(raw_points, list) and 1 <= len(raw_points) <= MAX_TEMPO_POINTS:
        point_fields = {TICK_FIELD, MICROS_PER_QUARTER_FIELD, MODE_FIELD}
        for index, raw_point in enumerate(raw_points):
            base = f"/tempo_points/{index}"
            if not isinstance(raw_point, dict):
                errors.append(_err("INVALID_TYPE", base, "expected object"))
                points.append(None)
                continue
            for field in raw_point:
                if field not in point_fields:
                    errors.append(
                        _err("UNKNOWN_FIELD", f"{base}/{field}", "unexpected field")
                    )

            tick = raw_point.get(TICK_FIELD)
            if TICK_FIELD not in raw_point:
                errors.append(
                    _err("MISSING_FIELD", f"{base}/{TICK_FIELD}", "field is required")
                )
            elif not _is_int(tick):
                errors.append(
                    _err(
                        "INVALID_TYPE", f"{base}/{TICK_FIELD}", "expected integer"
                    )
                )
            # Range/ordering checks happen after the whole map has been parsed.

            micros = raw_point.get(MICROS_PER_QUARTER_FIELD)
            if MICROS_PER_QUARTER_FIELD not in raw_point:
                errors.append(
                    _err(
                        "MISSING_FIELD",
                        f"{base}/{MICROS_PER_QUARTER_FIELD}",
                        "field is required",
                    )
                )
            elif not _is_int(micros):
                errors.append(
                    _err(
                        "INVALID_TYPE",
                        f"{base}/{MICROS_PER_QUARTER_FIELD}",
                        "expected integer",
                    )
                )
            elif micros < 1:
                errors.append(
                    _err(
                        "NON_POSITIVE_TEMPO",
                        f"{base}/{MICROS_PER_QUARTER_FIELD}",
                        "microseconds per quarter note must be a positive integer",
                    )
                )

            mode = raw_point.get(MODE_FIELD)
            if MODE_FIELD not in raw_point:
                errors.append(
                    _err("MISSING_FIELD", f"{base}/{MODE_FIELD}", "field is required")
                )
            elif not isinstance(mode, str) or mode not in VALID_MODES:
                errors.append(
                    _err(
                        "INVALID_MODE",
                        f"{base}/{MODE_FIELD}",
                        f"mode must be one of {list(VALID_MODES)}",
                    )
                )

            if _is_int(tick) and _is_int(micros) and mode in VALID_MODES:
                points.append((tick, micros, mode))
            else:
                points.append(None)

        # Tempo map geometry: gap at the origin, duplicates, ordering.
        # These checks read raw ticks so a broken sibling field cannot mask
        # a structural error on the same or another point.
        previous_tick: int | None = None
        for index, raw_point in enumerate(raw_points):
            if not isinstance(raw_point, dict):
                continue
            tick = raw_point.get(TICK_FIELD)
            if not _is_int(tick):
                continue
            base = f"/tempo_points/{index}/{TICK_FIELD}"
            if previous_tick is None:
                if tick != 0:
                    errors.append(
                        _err(
                            "TEMPO_GAP",
                            base,
                            "the first tempo point with a tick must be at tick 0",
                        )
                    )
            elif tick == previous_tick:
                errors.append(
                    _err(
                        "DUPLICATE_TEMPO_TICK",
                        base,
                        "tempo point ticks must be strictly increasing",
                    )
                )
            elif tick < previous_tick:
                errors.append(
                    _err(
                        "TEMPO_NOT_ORDERED",
                        base,
                        "tempo point ticks must be strictly increasing",
                    )
                )
            previous_tick = tick
            previous_index = index

    # Parse cues.
    cues: list[tuple[Any, int] | None] = []
    seen_ids: dict[Any, int] = {}
    if isinstance(raw_cues, list) and 1 <= len(raw_cues) <= MAX_CUES:
        cue_fields = {ID_FIELD, TICK_FIELD}
        for index, raw_cue in enumerate(raw_cues):
            base = f"/cues/{index}"
            if not isinstance(raw_cue, dict):
                errors.append(_err("INVALID_TYPE", base, "expected object"))
                cues.append(None)
                continue
            for field in raw_cue:
                if field not in cue_fields:
                    errors.append(
                        _err("UNKNOWN_FIELD", f"{base}/{field}", "unexpected field")
                    )

            cue_id = raw_cue.get(ID_FIELD)
            if ID_FIELD not in raw_cue:
                errors.append(
                    _err("MISSING_FIELD", f"{base}/{ID_FIELD}", "field is required")
                )
            elif not (_is_int(cue_id) or isinstance(cue_id, str)) or cue_id == "":
                errors.append(
                    _err(
                        "INVALID_TYPE",
                        f"{base}/{ID_FIELD}",
                        "cue id must be a non-empty string or integer",
                    )
                )
            else:
                first_index = seen_ids.get(cue_id)
                if first_index is not None:
                    errors.append(
                        _err(
                            "DUPLICATE_CUE_ID",
                            f"{base}/{ID_FIELD}",
                            f"cue id {cue_id!r} was already used at /cues/{first_index}",
                        )
                    )
                else:
                    seen_ids[cue_id] = index

            tick = raw_cue.get(TICK_FIELD)
            valid_tick = False
            if TICK_FIELD not in raw_cue:
                errors.append(
                    _err("MISSING_FIELD", f"{base}/{TICK_FIELD}", "field is required")
                )
            elif not _is_int(tick):
                errors.append(
                    _err("INVALID_TYPE", f"{base}/{TICK_FIELD}", "expected integer")
                )
            elif tick < 0:
                errors.append(
                    _err(
                        "CUE_OUT_OF_RANGE",
                        f"{base}/{TICK_FIELD}",
                        "cue tick must be non-negative",
                    )
                )
            else:
                valid_tick = True

            if valid_tick and (_is_int(cue_id) or (isinstance(cue_id, str) and cue_id)):
                cues.append((cue_id, tick))
            else:
                cues.append(None)

    if errors:
        return 400, {
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "request validation failed; no projections were produced",
                "errors": errors,
            }
        }

    # Validation guarantees these are now well-formed.
    assert _is_int(ticks_per_quarter) and ticks_per_quarter >= 1
    assert _is_int(sample_rate) and sample_rate >= 1
    assert isinstance(raw_points, list)
    assert isinstance(raw_cues, list)

    projected = _project(
        ticks_per_quarter=ticks_per_quarter,
        sample_rate=sample_rate,
        points=[point for point in points if point is not None],
        cues=[cue for cue in cues if cue is not None],
    )
    return 200, {"ticks_per_quarter": ticks_per_quarter, "cues": projected}


def _project(
    *,
    ticks_per_quarter: int,
    sample_rate: int,
    points: list[tuple[int, int, str]],
    cues: list[tuple[Any, int]],
) -> list[dict[str, Any]]:
    """Project cues.  ``points`` are validated, ordered and start at tick 0."""
    tpq = Fraction(ticks_per_quarter)
    ticks = [point[0] for point in points]
    tempos = [point[1] for point in points]
    modes = [point[2] for point in points]

    # Exact cumulative microseconds at every tempo point.
    cumulative: list[Fraction] = [Fraction(0)]
    for i in range(len(points) - 1):
        a, b = ticks[i], ticks[i + 1]
        span = b - a
        if modes[i] == MODE_CONSTANT:
            segment = Fraction(tempos[i] * span, ticks_per_quarter)
        else:
            # Trapezoid: tempo ramps linearly from tempos[i] to tempos[i+1].
            segment = Fraction((tempos[i] + tempos[i + 1]) * span, 2 * ticks_per_quarter)
        cumulative.append(cumulative[-1] + segment)

    last_index = len(points) - 1
    results: list[dict[str, Any]] = []

    for cue_id, cue_tick in cues:
        index = bisect_right(ticks, cue_tick) - 1
        a = ticks[index]
        tempo_a = tempos[index]
        elapsed: Fraction = cumulative[index]

        if cue_tick > a:
            distance = cue_tick - a
            if index == last_index or modes[index] == MODE_CONSTANT:
                # Last point holds its tempo forever.
                elapsed += Fraction(tempo_a * distance, ticks_per_quarter)
            else:
                b = ticks[index + 1]
                tempo_b = tempos[index + 1]
                # A cue exactly at b is handled by the next segment (its
                # cumulative value); here cue_tick is strictly inside (a, b).
                # Average tempo over [a, cue_tick] for a linear tempo ramp:
                # (tempo(a) + tempo(cue_tick)) / 2.
                average = tempo_a + Fraction(
                    (tempo_b - tempo_a) * distance, 2 * (b - a)
                )
                elapsed += average * Fraction(distance, tpq)

        time_nanoseconds = _round_half_even(elapsed * 1000)
        sample_frame = _round_half_even(
            elapsed * sample_rate / Fraction(1_000_000)
        )
        results.append(
            {
                ID_FIELD: cue_id,
                TICK_FIELD: cue_tick,
                "time_nanoseconds": time_nanoseconds,
                "sample_frame": sample_frame,
            }
        )

    return results
