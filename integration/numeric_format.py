"""Shared numeric constants and the NUMERIC reply-formatting rule (issue #1353, item 6).

Integer boundaries are expressions, never digit literals. Stored values are strings; the engine
parses them to a double, so a boundary constant names the stored spelling and the comment on
its use names the double it becomes.
"""
import math

INT64_MAX = (1 << 63) - 1   # parses to 2^63: not representable, rounds up
INT64_MIN = -(1 << 63)      # exactly representable
TWO_POW_53 = 1 << 53        # largest range of consecutive exact integers in a double


def ulp_above(n: int) -> int:
    """Smallest double strictly above the double nearest n, as an exact int."""
    f = math.nextafter(float(n), math.inf)
    assert f.is_integer()
    return int(f)


def ulp_below(n: int) -> int:
    """Largest double strictly below the double nearest n, as an exact int."""
    f = math.nextafter(float(n), -math.inf)
    assert f.is_integer()
    return int(f)


def redis_sort_key(stored: str) -> bytes:
    """Sort key: parsed double at 17 significant digits; -0 keeps its sign."""
    return b"#%.17g" % float(stored)


def redis_return_value(stored: str) -> bytes:
    """RETURN value: 0 for any zero, integer digits when integral in
    [-2^63, 2^63), otherwise 12 significant digits."""
    v = float(stored)
    if v == 0:
        return b"0"
    if INT64_MIN <= v < (1 << 63) and v.is_integer():
        return b"%.0f" % v
    return b"%.12g" % v
