"""Fractional ordering keys: strings that sort lexicographically and always
admit a new key strictly between two neighbours (§4.1)."""

from __future__ import annotations

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)
_MIN, _MAX = _ALPHABET[0], _ALPHABET[-1]
_MID = "U"  # _ALPHABET[30]; note _BASE // 2 == 31 would give "V", not "U"


def _digit(c: str) -> int:
    return _ALPHABET.index(c)


def between(a: str | None, b: str | None) -> str:
    """Return a key k with a < k < b. ``None`` means unbounded on that side."""
    if a is None and b is None:
        return _MID
    if a is not None and b is not None and a >= b:
        raise ValueError(f"order keys not increasing: {a!r} >= {b!r}")

    lo = a or ""
    hi = b

    out: list[str] = []
    i = 0
    while True:
        lc = _digit(lo[i]) if i < len(lo) else 0
        if hi is None:
            hc = _BASE
        elif i < len(hi):
            hc = _digit(hi[i])
        else:
            # hi is a prefix of lo (or exhausted): treat as unbounded above
            # from here on, so the new key can extend past this point.
            hc = _BASE

        if hc - lc > 1:
            out.append(_ALPHABET[(lc + hc) // 2])
            return "".join(out)

        # Digits equal or adjacent: copy the low digit and keep going,
        # narrowing the window at the next position.
        out.append(_ALPHABET[lc])
        i += 1
