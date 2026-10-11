"""The judgment as it comes back from the model: parsed leniently, mapped to articles, composed."""

from __future__ import annotations

from triage.ids import resolve_ordinal, short_to_long
from triage.teacher import (
    JudgmentIn,
    Teacher,
    Verdict,
    _fmt_date,
    _mentions_cache_ttl,
    _mentions_structured_outputs,
    parse_judgments,
)


def test_parser_tolerates_null_fields():
    """The plain-JSON fallback sends nulls freely; a null used to drop the whole judgment, and
    with it the batch cycled as pending every run."""
    text = ('{"judgments": [{"n": 1, "s": [60, 60, 60, 60, 60], "lesson": null, "concept": null, "kind": null, '
            '"flags": null, "dupe": null, "conf": null}, '
            '{"n": 2, "s": ["70", 70, 70, 70, 70], "flags": [null, "india"], "dupe": "1", "conf": "LOW"}]}')
    out = parse_judgments(text)
    assert [v.n for v in out] == [1, 2]
    assert out[0].lesson == "" and out[0].kind == "other" and out[0].flags == [] and out[0].confidence == "medium"
    assert out[1].quality == 70 and out[1].flags == ["india_angle"] and out[1].dupe_of == 1 and out[1].confidence == "low"
    assert JudgmentIn(n=1, s=[1, 2, 3, 4, 5], dupe="³").to_verdict().dupe_of is None


def _v(n, dupe=None, flags=()):
    return Verdict(n=n, quality=80, learning=80, fit=80, breadth=80, relevance=80, dupe_of=dupe, flags=list(flags))


def test_dupe_must_point_to_an_earlier_article():
    ids = [short_to_long(i) for i in range(1, 5)]
    by = Teacher._by_article([_v(1, dupe=2), _v(2, dupe=1), _v(3, dupe=3, flags=["duplicate"]), _v(4, dupe=2)], ids)
    assert by[ids[0]].dupe_of is None and by[ids[0]].flags == []        # points forward: dropped, the original stands
    assert by[ids[1]].dupe_of == ids[0]                                  # the mutual pair: the later copy is the dupe
    assert by[ids[2]].dupe_of is None and by[ids[2]].flags == []        # points at itself: dropped, flag too
    assert by[ids[3]].dupe_of is None                                    # points at a dupe, not an original: dropped
    # a chain: 3 is a dupe of 2, so 4 may not be a dupe of 3 (it would point at a copy)
    by = Teacher._by_article([_v(2), _v(3, dupe=2), _v(4, dupe=3)], ids)
    assert by[ids[2]].dupe_of == ids[1] and by[ids[3]].dupe_of is None
    # verdicts arrive in any order; the batch order decides
    by = Teacher._by_article([_v(4, dupe=1), _v(1)], ids)
    assert by[ids[3]].dupe_of == ids[0]


def test_resolve_ordinal_edge_echoes():
    ids = [short_to_long(i) for i in range(1, 4)]
    for raw in ("³", "①", 0, "0", None, "", "9", "x", True, "2x"):
        assert resolve_ordinal(raw, ids) is None, raw
    for raw in ("[ 2 ]", "2.", "#2", " 2 ", 2):
        assert resolve_ordinal(raw, ids) == ids[1], raw
    assert resolve_ordinal(ids[2] + " trailing", ids) is None and resolve_ordinal("item " + ids[2][-16:], ids) == ids[2]


def test_article_block_out_of_range_published():
    assert _fmt_date(1_700_000_000) == "2023-11-14"
    for bad in (10**20, -10**20, None, 0, "x"):
        assert _fmt_date(bad) == "unknown date"  # type: ignore[arg-type]


def test_adapt_to_error_word_boundaries():
    assert _mentions_cache_ttl("cache_control: ttl is not supported")
    assert _mentions_cache_ttl("the TTL value")
    assert not _mentions_cache_ttl("request throttled, try later")   # "throttled" contains "ttl"
    assert _mentions_structured_outputs("output_config.format is invalid")
    assert not _mentions_structured_outputs("the unstructured text was too long")
