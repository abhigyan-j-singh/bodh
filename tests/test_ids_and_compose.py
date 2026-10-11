from __future__ import annotations

import pytest

from viveka.config import Config
from viveka.ids import long_to_short, normalize_item_id, resolve_ordinal, short_to_long
from viveka.learner import expansion_topics
from viveka.teacher import Verdict, canonical_url, compose, matches_topic


def test_id_round_trip():
    long_id = "tag:google.com,2005:reader/item/80484b00000e8003"
    assert long_to_short(long_id) == -9203023375158575101
    assert short_to_long(-9203023375158575101) == long_id
    assert normalize_item_id("-9203023375158575101") == long_id
    assert normalize_item_id(150177826473082) == "tag:google.com,2005:reader/item/000088960000047a"
    with pytest.raises(ValueError):
        normalize_item_id("not-an-id")


def test_resolve_ordinal():
    ids = [short_to_long(i) for i in range(1, 4)]
    assert resolve_ordinal(2, ids) == ids[1]
    assert resolve_ordinal("[3]", ids) == ids[2]
    assert resolve_ordinal("9", ids) is None
    assert resolve_ordinal(ids[0], ids) == ids[0]


def base_verdict(**over) -> Verdict:
    d = dict(n=1, quality=80, learning=80, fit=80, breadth=80, relevance=80, lesson="x", concept="nuclear power",
             kind="original_analysis", flags=[], dupe_of=None, confidence="high")
    d.update(over)
    return Verdict(**d)


def test_compose_weights_and_rules():
    cfg = Config()
    assert compose(base_verdict(), cfg) == 80.0
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 80.0          # no home ground configured: no bonus
    cfg.scoring.home_angle = "India"
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 90.0
    assert compose(base_verdict(flags=["india_angle"]), cfg) == 90.0         # the old name, read as today's
    assert compose(base_verdict(kind="promo"), cfg) == 30.0
    assert compose(base_verdict(flags=["vendor_marketing_no_data"]), cfg) == 30.0
    assert compose(base_verdict(flags=["funding_offfield"]), cfg) == 40.0
    assert compose(base_verdict(flags=["funding_round_non_infra"]), cfg) == 40.0
    assert compose(base_verdict(dupe_of=3), cfg) == 20.0
    assert compose(base_verdict(quality=100, learning=100, fit=100, breadth=100, relevance=100, flags=["home_angle"]), cfg) == 100.0
    cfg.scoring.home_bonus = 5
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 85.0


def test_compose_expansion_bonus_and_topic_matching():
    cfg = Config()
    assert compose(base_verdict(concept="small modular reactors"), cfg, ["nuclear power"]) == 80.0
    assert compose(base_verdict(concept="nuclear power economics"), cfg, ["nuclear power"]) == 90.0
    assert matches_topic("medieval trade history", ["medieval history"])
    assert not matches_topic("liquid cooling", ["medieval history"])


def test_expansion_topics_parsing():
    text = "## Expansion topics for next week\n- small modular reactors: adjacent to the power thread\n- medieval trade, because it is far away\n\n## Notes to the teacher\nx"
    assert expansion_topics(text) == ["small modular reactors", "medieval trade"]
    assert expansion_topics("## Expansion topics for next week\n(none yet)") == []


def test_canonical_url():
    a = canonical_url("https://www.Example.com/post/1/?utm_source=x&id=2#frag")
    b = canonical_url("https://example.com/post/1?id=2")
    assert a == b
