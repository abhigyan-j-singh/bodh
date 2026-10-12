"""Item ids: the two forms, the lenient readings, the model's echoes of an ordinal."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bodh.ids import LONG_PREFIX, long_to_short, normalize_item_id, resolve_ordinal, short_to_long


def test_id_round_trip():
    long_id = "tag:google.com,2005:reader/item/80484b00000e8003"
    assert long_to_short(long_id) == -9203023375158575101
    assert short_to_long(-9203023375158575101) == long_id
    assert normalize_item_id("-9203023375158575101") == long_id
    assert normalize_item_id(150177826473082) == "tag:google.com,2005:reader/item/000088960000047a"
    with pytest.raises(ValueError):
        normalize_item_id("not-an-id")


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=-(1 << 63), max_value=(1 << 63) - 1))
def test_id_round_trips(n: int):
    long_id = short_to_long(n)
    assert long_id.startswith(LONG_PREFIX) and len(long_id) == len(LONG_PREFIX) + 16
    assert long_to_short(long_id) == n
    assert normalize_item_id(long_id) == long_id == normalize_item_id(str(n)) == normalize_item_id(n)
    assert (
        normalize_item_id(long_id.upper().replace("TAG:GOOGLE.COM,2005:READER/ITEM/", LONG_PREFIX)) == long_id
    )


def test_short_to_long_rejects_out_of_range():
    for n in (1 << 63, -(1 << 63) - 1, 10**30):
        with pytest.raises(ValueError, match="out of 64-bit"):
            short_to_long(n)
    assert short_to_long((1 << 63) - 1).endswith("7fffffffffffffff")
    assert short_to_long(-(1 << 63)).endswith("8000000000000000")
    assert short_to_long("  -1 ") == LONG_PREFIX + "f" * 16  # int() takes the whitespace


def test_long_to_short_rejects_prefix_and_length():
    with pytest.raises(ValueError, match="not a long-form"):
        long_to_short("item/80484b00000e8003")
    with pytest.raises(ValueError, match="16 chars"):
        long_to_short(LONG_PREFIX + "80484b00000e80")
    with pytest.raises(ValueError):
        long_to_short(LONG_PREFIX + "80484b00000e80zz")


def test_normalize_rejects_wrong_length_and_non_hex_and_lowercases():
    assert normalize_item_id(LONG_PREFIX + "80484B00000E8003") == LONG_PREFIX + "80484b00000e8003"
    with pytest.raises(ValueError, match="16 chars"):
        normalize_item_id(LONG_PREFIX + "80484b")
    with pytest.raises(ValueError):
        normalize_item_id(LONG_PREFIX + "80484b00000e80zz")
    for bad in ("", "1.5", "0x10", "tag:google.com,2005:reader/item", [1]):
        with pytest.raises((ValueError, TypeError)):
            normalize_item_id(bad)  # type: ignore[arg-type]


def test_normalize_accepts_signed_decimal_with_whitespace():
    assert normalize_item_id(" -9203023375158575101\n") == LONG_PREFIX + "80484b00000e8003"
    assert normalize_item_id("+7") == short_to_long(7)
    assert normalize_item_id("0") == LONG_PREFIX + "0" * 16


def test_resolve_ordinal():
    ids = [short_to_long(i) for i in range(1, 4)]
    assert resolve_ordinal(2, ids) == ids[1]
    assert resolve_ordinal("[3]", ids) == ids[2]
    assert resolve_ordinal("9", ids) is None
    assert resolve_ordinal(ids[0], ids) == ids[0]


def test_resolve_ordinal_edge_echoes():
    ids = [short_to_long(i) for i in range(1, 4)]
    for raw in ("³", "①", 0, "0", None, "", "9", "x", True, "2x", -1, "-1", 1.5):
        assert resolve_ordinal(raw, ids) is None, raw  # type: ignore[arg-type]
    for raw in ("[ 2 ]", "2.", "#2", " 2 ", 2, "[2]."):
        assert resolve_ordinal(raw, ids) == ids[1], raw
    assert resolve_ordinal(ids[2] + " trailing", ids) is None
    assert resolve_ordinal("item " + ids[2][-16:], ids) == ids[2]
    assert resolve_ordinal(ids[2][-16:].upper(), ids) == ids[2]
    assert resolve_ordinal(short_to_long(9)[-16:], ids) is None  # a hex id not in the batch
    assert resolve_ordinal("1", []) is None


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.text(max_size=40), st.integers(), st.floats(allow_nan=False), st.none(), st.booleans()))
def test_resolve_ordinal_never_raises(raw):
    ids = [short_to_long(i) for i in range(1, 4)]
    out = resolve_ordinal(raw, ids)
    assert out is None or out in ids
