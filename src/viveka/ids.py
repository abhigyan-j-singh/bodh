"""BazQux / Google Reader item ID normalization (carried over from FeedBrain).

Two representations of the same 64-bit id:
  - long form:  ``tag:google.com,2005:reader/item/<16-char zero-padded hex>``
                (hex is the *unsigned* two's-complement representation)
  - short form: signed base-10 decimal (may be negative)

Example: ``tag:google.com,2005:reader/item/80484b00000e8003``
     ==  ``-9203023375158575101``

The database stores the long form everywhere.
"""

from __future__ import annotations

import re

LONG_PREFIX = "tag:google.com,2005:reader/item/"
_MASK64 = (1 << 64) - 1


def short_to_long(short_id: int | str) -> str:
    """Signed decimal (int or str) -> long-form tag URI."""
    n = int(short_id)
    if not (-(1 << 63) <= n < (1 << 63)):
        raise ValueError(f"item id out of 64-bit signed range: {short_id!r}")
    return f"{LONG_PREFIX}{n & _MASK64:016x}"


def long_to_short(long_id: str) -> int:
    """Long-form tag URI -> signed decimal int."""
    if not long_id.startswith(LONG_PREFIX):
        raise ValueError(f"not a long-form item id: {long_id!r}")
    hex_part = long_id[len(LONG_PREFIX):]
    if len(hex_part) != 16:
        raise ValueError(f"long-form hex must be 16 chars, got {len(hex_part)}: {long_id!r}")
    unsigned = int(hex_part, 16)
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def normalize_item_id(item_id: int | str) -> str:
    """Accept any form BazQux may emit; return the canonical long form."""
    if isinstance(item_id, int):
        return short_to_long(item_id)
    s = str(item_id).strip()
    if s.startswith(LONG_PREFIX):
        hex_part = s[len(LONG_PREFIX):]
        if len(hex_part) != 16:
            raise ValueError(f"long-form hex must be 16 chars: {s!r}")
        int(hex_part, 16)  # validates
        return LONG_PREFIX + hex_part.lower()
    try:
        return short_to_long(int(s, 10))
    except ValueError:
        raise ValueError(f"unrecognized item id format: {item_id!r}") from None


def resolve_ordinal(raw: int | str, ordered_ids: list[str]) -> str | None:
    """Map a model-echoed article reference (1-based ordinal) back to an id.

    Accepts "3", "[3]", "3." or an int. Returns None when nothing resolves;
    callers drop the result rather than guess.
    """
    if raw is None or isinstance(raw, bool):
        return None
    s = str(raw).strip().strip("[].# ")
    if s.isdecimal():  # isdigit() admits "³", which int() then refuses
        try:
            idx = int(s)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(ordered_ids):
            return ordered_ids[idx - 1]
    m = re.search(r"([0-9a-fA-F]{16})\s*$", str(raw or ""))
    if m:
        candidate = LONG_PREFIX + m.group(1).lower()
        if candidate in ordered_ids:
            return candidate
    return None
