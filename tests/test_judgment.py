"""The judgment as it comes back from the model: parsed leniently, mapped to articles, composed."""

from __future__ import annotations

from datetime import timedelta

from fakes import FakeLLM
from hypothesis import given, settings
from hypothesis import strategies as st

from bodh import store
from bodh.config import Config
from bodh.ids import short_to_long
from bodh.judge import Judge, _fmt_date
from bodh.judgment import (
    FLAGS,
    KINDS,
    SUBSCORES,
    JudgmentIn,
    Verdict,
    canonical_url,
    compose,
    matches_topic,
    parse_judgments,
)
from bodh.providers.anthropic import mentions_cache_ttl, mentions_structured_outputs


def test_parser_tolerates_null_fields():
    """The plain-JSON fallback sends nulls freely; a null used to drop the whole judgment, and
    with it the batch cycled as pending every run."""
    text = (
        '{"judgments": [{"n": 1, "s": [60, 60, 60, 60, 60], "lesson": null, "concept": null, "kind": null, '
        '"flags": null, "dupe": null, "conf": null}, '
        '{"n": 2, "s": ["70", 70, 70, 70, 70], "flags": [null, "india"], "dupe": "1", "conf": "LOW"}]}'
    )
    out = parse_judgments(text)
    assert [v.n for v in out] == [1, 2]
    assert (
        out[0].lesson == ""
        and out[0].kind == "other"
        and out[0].flags == []
        and out[0].confidence == "medium"
    )
    assert (
        out[1].quality == 70
        and out[1].flags == ["home_angle"]
        and out[1].dupe_of == 1
        and out[1].confidence == "low"
    )
    assert JudgmentIn(n=1, s=[1, 2, 3, 4, 5], dupe="³").to_verdict().dupe_of is None


def _v(n, dupe=None, flags=()):
    return Verdict(
        n=n, quality=80, learning=80, fit=80, breadth=80, relevance=80, dupe_of=dupe, flags=list(flags)
    )


def test_dupe_must_point_to_an_earlier_article():
    ids = [short_to_long(i) for i in range(1, 5)]
    by = Judge._by_article(
        [_v(1, dupe=2), _v(2, dupe=1), _v(3, dupe=3, flags=["duplicate"]), _v(4, dupe=2)], ids
    )
    assert (
        by[ids[0]].dupe_of is None and by[ids[0]].flags == []
    )  # points forward: dropped, the original stands
    assert by[ids[1]].dupe_of == ids[0]  # the mutual pair: the later copy is the dupe
    assert by[ids[2]].dupe_of is None and by[ids[2]].flags == []  # points at itself: dropped, flag too
    assert by[ids[3]].dupe_of is None  # points at a dupe, not an original: dropped
    # a chain: 3 is a dupe of 2, so 4 may not be a dupe of 3 (it would point at a copy)
    by = Judge._by_article([_v(2), _v(3, dupe=2), _v(4, dupe=3)], ids)
    assert by[ids[2]].dupe_of == ids[1] and by[ids[3]].dupe_of is None
    # verdicts arrive in any order; the batch order decides
    by = Judge._by_article([_v(4, dupe=1), _v(1)], ids)
    assert by[ids[3]].dupe_of == ids[0]


def test_article_block_out_of_range_published():
    assert _fmt_date(1_700_000_000) == "2023-11-14"
    for bad in (10**20, -(10**20), None, 0, "x"):
        assert _fmt_date(bad) == "unknown date"  # type: ignore[arg-type]


def test_adapt_to_error_word_boundaries():
    assert mentions_cache_ttl("cache_control: ttl is not supported")
    assert mentions_cache_ttl("the TTL value")
    assert not mentions_cache_ttl("request throttled, try later")  # "throttled" contains "ttl"
    assert mentions_structured_outputs("output_config.format is invalid")
    assert not mentions_structured_outputs("the unstructured text was too long")


def test_prompt_home_flag_follows_the_config(cfg, conn):
    from bodh.judge import Judge, fill_prompt, load_prompt

    raw = load_prompt("teacher.v2", cfg.root)
    assert "<<home_flag>>" in raw and "india" not in raw.lower() and "AI infrastructure" not in raw
    cfg.judge.home_angle = ""
    assert "home (" not in fill_prompt(raw, cfg) and "<<" not in fill_prompt(raw, cfg)
    cfg.judge.home_angle = "India"
    text = Judge(cfg, FakeLLM(cfg, conn), conn).system_text
    assert "home (an angle on the student's home ground, India)" in text and "<<" not in text
    assert "outside the student's own field" in text
    cfg.judge.home_angle = "  "  # whitespace is off, like "": no flag in the prompt, no bonus in compose
    assert cfg.model_validate(cfg.model_dump()).judge.home_angle == ""
    from bodh.judgment import normalize_flags

    assert normalize_flags(["india_angle", "funding_round_non_infra", "primary_source"]) == [
        "home_angle",
        "funding_offfield",
        "primary_source",
    ]
    assert normalize_flags(None) == []


# -- the parser against what a model may write --------------------------------------


def _batch(*items: dict) -> str:
    import json

    return json.dumps({"judgments": list(items)})


def _ok(n: int = 1, **over) -> dict:
    d = {
        "n": n,
        "s": [60, 60, 60, 60, 60],
        "lesson": "l",
        "concept": "c",
        "kind": "news",
        "flags": [],
        "dupe": None,
        "conf": "high",
    }
    d.update(over)
    return d


def test_parser_drops_bad_scores():
    out = parse_judgments(
        _batch(
            _ok(1, s=[60.5, 60, 60, 60, 60]),  # a float: pydantic refuses the fraction
            _ok(2, s=[60, None, 60, 60, 60]),  # a null score
            _ok(3, s=[60, 60, 60, 60, 60, 60]),  # six values
            {k: v for k, v in _ok(4).items() if k != "n"},  # no n
            _ok(5, s=["60", "70", "80", "90", "100"]),  # digit strings are read
            _ok(6, s=[-5, 150, 60, 60, 60]),  # out of range: clamped, not dropped
            _ok(7, s="60 60 60 60 60"),  # not a list
            _ok(8, s=[60, 60, 60]),  # three values
        )
    )
    assert [v.n for v in out] == [5, 6]
    assert (out[0].quality, out[0].relevance) == (60, 100) and (out[1].quality, out[1].learning) == (0, 100)


def test_parser_enum_case_and_unknown_codes():
    out = parse_judgments(
        _batch(
            _ok(1, kind="NEWS", flags=["Primary", "HOME", "nonsense", None], conf="LOW"),
            _ok(2, kind="nonsense", flags=None, conf="certain", dupe="1"),
            _ok(3, kind=None, flags="primary", conf=None, dupe="x"),
            _ok(4, kind="src", flags=["dupe"], dupe=1.0),
        )
    )
    assert [v.n for v in out] == [1, 2, 4]  # a string where a list belongs is a dropped judgment
    assert (
        out[0].kind == "reporting"
        and out[0].flags == ["primary_source", "home_angle"]
        and out[0].confidence == "low"
    )
    assert (
        out[1].kind == "other"
        and out[1].flags == []
        and out[1].confidence == "medium"
        and out[1].dupe_of == 1
    )
    assert (
        out[2].kind == "primary_source" and out[2].flags == ["duplicate"] and out[2].dupe_of == 1
    )  # 1.0 is 1


def test_parser_shapes_of_the_whole_answer():
    assert (
        parse_judgments("") is None
        and parse_judgments("   ") is None
        and parse_judgments("no braces") is None
    )
    assert parse_judgments("{not json}") is None
    assert parse_judgments('{"judgments": null}') == [] and parse_judgments('{"other": 1}') == []
    assert parse_judgments('{"judgments": [1, "x", null]}') == []  # items that are not objects
    assert parse_judgments("[1, 2]") is None  # a list, no object
    assert (
        parse_judgments("Sure! Here it is:\n```json\n" + _batch(_ok(1)) + "\n```\nHope this helps.")
        is not None
    )
    assert parse_judgments(_batch()) == []  # an empty batch
    assert [v.n for v in parse_judgments(_batch(_ok(1), _ok(1)))] == [
        1,
        1,
    ]  # the parser keeps both; _by_article drops the second
    assert [v.n for v in parse_judgments(_batch(_ok(1), _ok(99)))] == [
        1,
        99,
    ]  # extra articles are the caller's problem


@st.composite
def judgment_like(draw):
    """Roughly the shape of a judgment, with every field free to be wrong."""
    value = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-10, max_value=200),
        st.floats(allow_nan=False),
        st.text(max_size=12),
    )
    d = {}
    for key in ("n", "s", "lesson", "concept", "kind", "flags", "dupe", "conf"):
        if draw(st.booleans()):
            if key == "s" and draw(st.booleans()):
                d[key] = draw(st.lists(value, max_size=7))
            elif key == "flags" and draw(st.booleans()):
                d[key] = draw(st.lists(st.one_of(st.none(), st.text(max_size=10)), max_size=4))
            else:
                d[key] = draw(value)
    return d


@settings(max_examples=150, deadline=None)
@given(st.lists(judgment_like(), max_size=5), st.text(max_size=20), st.text(max_size=20))
def test_parser_fuzz(items, before, after):
    """Never raises; every verdict kept is valid: scores in range, names from the lists, a
    confidence from the three."""
    import json

    text = before + json.dumps({"judgments": items}) + after
    out = parse_judgments(text)
    if out is None:  # the padding broke the braces: fine, as long as nothing raised
        return
    for v in out:
        assert all(0 <= getattr(v, k) <= 100 for k in SUBSCORES)
        assert (
            v.kind in KINDS and all(f in FLAGS for f in v.flags) and v.confidence in ("high", "medium", "low")
        )
        assert isinstance(v.n, int)


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=200))
def test_parser_never_raises_on_any_text(text):
    out = parse_judgments(text)
    assert out is None or isinstance(out, list)


def test_by_article_out_of_range_and_duplicate_n():
    ids = [short_to_long(i) for i in range(1, 4)]
    by = Judge._by_article([_v(0), _v(4), _v(2), _v(2, flags=["primary_source"]), _v(-1)], ids)
    assert (
        list(by) == [ids[1]] and by[ids[1]].flags == []
    )  # out of range dropped; the first of a repeated n stands


# -- compose: the rules and their properties ---------------------------------------


def base_verdict(**over) -> Verdict:
    d = dict(
        n=1,
        quality=80,
        learning=80,
        fit=80,
        breadth=80,
        relevance=80,
        lesson="x",
        concept="nuclear power",
        kind="original_analysis",
        flags=[],
        dupe_of=None,
        confidence="high",
    )
    d.update(over)
    return Verdict(**d)


def test_compose_weights_and_rules():
    cfg = Config()
    assert compose(base_verdict(), cfg) == 80.0
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 80.0  # no home ground configured: no bonus
    cfg.judge.home_angle = "India"
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 90.0
    assert compose(base_verdict(flags=["india_angle"]), cfg) == 90.0  # the old name, read as today's
    assert compose(base_verdict(kind="promo"), cfg) == 30.0
    assert compose(base_verdict(flags=["vendor_marketing_no_data"]), cfg) == 30.0
    assert compose(base_verdict(flags=["funding_offfield"]), cfg) == 40.0
    assert compose(base_verdict(flags=["funding_round_non_infra"]), cfg) == 40.0
    assert compose(base_verdict(dupe_of=3), cfg) == 20.0
    hundred = base_verdict(
        quality=100, learning=100, fit=100, breadth=100, relevance=100, flags=["home_angle"]
    )
    assert compose(hundred, cfg) == 100.0
    cfg.judge.home_bonus = 5
    assert compose(base_verdict(flags=["home_angle"]), cfg) == 85.0
    assert compose(base_verdict().model_dump(), cfg) == 80.0  # a dict works too


def test_compose_expansion_bonus_and_topic_matching():
    cfg = Config()
    assert compose(base_verdict(concept="small modular reactors"), cfg, ["nuclear power"]) == 80.0
    assert compose(base_verdict(concept="nuclear power economics"), cfg, ["nuclear power"]) == 90.0
    assert matches_topic("medieval trade history", ["medieval history"])
    assert not matches_topic("liquid cooling", ["medieval history"])
    assert not matches_topic("", ["x"]) and not matches_topic("x", ["", "  "])
    assert matches_topic("ai", ["ai safety"])  # one inside the other


def test_compose_cap_order_and_bounds():
    """Decided in Phase 3 (October 2026): a bonus never lifts a capped article over the cut-off.
    Vendor marketing on an expansion topic used to score 40 and clear a cut-off of 35."""
    cfg = Config()
    cfg.judge.home_angle = "India"
    promo = base_verdict(kind="promo", concept="nuclear power economics", flags=["home_angle"])
    assert compose(promo, cfg, ["nuclear power"]) == 30.0
    funding = base_verdict(flags=["funding_offfield", "home_angle"], concept="nuclear power economics")
    assert compose(funding, cfg, ["nuclear power"]) == 40.0
    assert compose(base_verdict(dupe_of=2, flags=["home_angle"]), cfg) == 20.0
    assert (
        compose(base_verdict(kind="promo", flags=["funding_offfield"], dupe_of=1), cfg) == 20.0
    )  # the lowest cap wins
    good = base_verdict(flags=["home_angle"], concept="nuclear power economics")
    assert compose(good, cfg, ["nuclear power"]) == 100.0  # both bonuses, bounded at 100
    assert compose(base_verdict(quality=0, learning=0, fit=0, breadth=0, relevance=0), cfg) == 0.0


SCORE = st.integers(min_value=0, max_value=100)


@settings(max_examples=200, deadline=None)
@given(
    SCORE,
    SCORE,
    SCORE,
    SCORE,
    SCORE,
    st.lists(st.sampled_from(FLAGS), max_size=3),
    st.sampled_from(KINDS),
    st.booleans(),
)
def test_compose_properties(q, le, f, b, r, flags, kind, home):
    cfg = Config()
    cfg.judge.home_angle = "India" if home else ""
    v = base_verdict(quality=q, learning=le, fit=f, breadth=b, relevance=r, flags=flags, kind=kind)
    s = compose(v, cfg, ["nuclear power"])
    assert 0.0 <= s <= 100.0
    weighted = sum(cfg.judge.weights[k] * getattr(v, k) for k in cfg.judge.weights)
    capped = (
        "vendor_marketing_no_data" in flags
        or kind == "promo"
        or "funding_offfield" in flags
        or "duplicate" in flags
    )
    if capped:
        cap = min(
            [30] * ("vendor_marketing_no_data" in flags or kind == "promo")
            + [40] * ("funding_offfield" in flags)
            + [20] * ("duplicate" in flags)
        )
        assert s <= cap
    else:
        assert s >= round(weighted, 1) - 0.05  # bonuses only add
    # monotone in each sub-score
    for k in SUBSCORES:
        if getattr(v, k) < 100:
            higher = base_verdict(**{**v.model_dump(), k: getattr(v, k) + 1})
            assert compose(higher, cfg, ["nuclear power"]) >= s


# -- duplicates --------------------------------------------------------------------


def test_canonical_url_edges():
    a = canonical_url("https://www.Example.com/post/1/?utm_source=x&id=2#frag")
    b = canonical_url("https://example.com/post/1?id=2")
    assert a == b == "https://example.com/post/1?id=2"
    assert canonical_url("") == "" and canonical_url("   ") == "https:///"  # a blank is not a URL
    assert canonical_url("HTTP://EXAMPLE.COM") == "http://example.com/"
    assert (
        canonical_url("https://example.com/a?fbclid=1&gclid=2&ref=3&source=4&mc_cid=5&keep=6")
        == "https://example.com/a?keep=6"
    )
    assert canonical_url("https://example.com/a?b=&c") == "https://example.com/a?b=&c="  # blanks kept
    assert canonical_url("https://example.com/a?z=1&a=2") != canonical_url(
        "https://example.com/a?a=2&z=1"
    )  # order kept, on purpose
    assert (
        canonical_url("http://[::1") == "http://[::1"
    )  # an invalid netloc: lower-cased as it is, never raised


@settings(max_examples=150, deadline=None)
@given(st.text(max_size=60))
def test_canonical_url_is_idempotent_and_never_raises(url):
    once = canonical_url(url)
    assert canonical_url(once) == once


def test_dup_index_window_and_same_feed(conn, cfg):
    from conftest import seed_scored

    from bodh.judgment import DupIndex

    long_title = "a long enough title to be matched by the index"
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=70, title=long_title, url="https://a.example/x?utm_source=1", feed="Feed A"),
            dict(n=2, score=70, title="short", url="https://b.example/y", feed="Feed B"),
        ],
    )
    index = DupIndex(conn, window_days=7)
    # the same canonical URL: a duplicate, whatever the feed
    hit = index.find(
        {"id": "new", "url": "https://A.example/x/?utm_medium=2", "title": "other", "feed_title": "Feed A"}
    )
    assert hit and hit["id"] == ids[0]
    # the same long title from another feed: a duplicate; from the same feed: not (an update, a series)
    assert (
        index.find({"id": "new", "url": "", "title": long_title.upper() + "!", "feed_title": "Feed B"})["id"]
        == ids[0]
    )
    assert index.find({"id": "new", "url": "", "title": long_title, "feed_title": "Feed A"}) is None
    # a short title never matches by title; an article never matches itself
    assert index.find({"id": "new", "url": "", "title": "short", "feed_title": "Feed C"}) is None
    assert (
        index.find({"id": ids[0], "url": "https://a.example/x", "title": long_title, "feed_title": "Feed A"})
        is None
    )
    # outside the window: not in the index
    conn.execute("UPDATE articles SET fetched_at = '2020-01-01T00:00:00+00:00'")
    assert (
        DupIndex(conn, window_days=7).find(
            {"id": "new", "url": "https://a.example/x", "title": "", "feed_title": "x"}
        )
        is None
    )
    # a known duplicate is not an original others can point at
    conn.execute("UPDATE articles SET fetched_at = ?", (store.utcnow_iso(),))
    conn.execute("UPDATE scores SET dupe_of = ? WHERE article_id = ?", (ids[1], ids[0]))
    assert (
        DupIndex(conn, window_days=7).find(
            {"id": "new", "url": "https://a.example/x", "title": "", "feed_title": "x"}
        )
        is None
    )


# -- the article block and the prompt --------------------------------------------------


def test_article_block_truncation_and_escaping():
    from bodh.judge import article_block

    row = {
        "text": "x" * 50 + "<script>alert(1)</script>" + "y" * 50,
        "url": "https://WWW.Example.com/p",
        "feed_title": "F <b>",
        "folder": None,
        "title": "T [1]\nsecond line",
        "author": None,
        "published": 1_700_000_000,
    }
    block = article_block(3, row, 60)
    assert block.startswith("[3]\nfeed: F <b> · folder: none\ntitle: T [1]\nsecond line\n")
    assert "site: example.com" in block and "published: 2023-11-14" in block and "author: unknown" in block
    assert block.endswith("text: " + "x" * 50 + "<script>al […]\n")  # cut at 60 characters, marked
    assert "(no text in the feed)" in article_block(1, {**row, "text": "   "}, 60)
    assert "title: (untitled)" in article_block(1, {**row, "title": ""}, 60) and "site: \n" in article_block(
        1, {**row, "url": ""}, 60
    )


def test_prompt_snapshot(cfg, conn, monkeypatch):
    """The rendered system and user text for a fixed profile and batch equals the stored
    snapshot: a prompt change shows as a diff in the pull request, and the changelog says
    whether it re-judges. Regenerate with BODH_UPDATE_SNAPSHOTS=1 after a deliberate change."""
    import os
    from pathlib import Path

    from bodh.judge import Judge

    cfg.judge.home_angle = "India"
    judge = Judge(
        cfg, FakeLLM(cfg, conn), conn, context="Loved: coral reefs: 3", expansion_topics=["nuclear power"]
    )
    rows = [
        {
            "id": short_to_long(1),
            "text": "Reef fish follow the current. " * 10,
            "url": "https://example.com/reefs",
            "feed_title": "Reef News",
            "folder": "Science",
            "title": "Where the fish go",
            "author": "A. Author",
            "published": 1_700_000_000,
        },
        {
            "id": short_to_long(2),
            "text": "",
            "url": "",
            "feed_title": "",
            "folder": None,
            "title": "",
            "author": "",
            "published": None,
        },
    ]
    rendered = f"{judge.system_text}\n\n===== user =====\n{judge._user_text(rows, 120)}\n\n===== review =====\n{judge.second_look_text}"
    path = Path(__file__).parent / "snapshots" / "prompt_teacher_v2.txt"
    if os.environ.get("BODH_UPDATE_SNAPSHOTS"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert rendered == path.read_text(encoding="utf-8"), (
        "the prompt changed: BODH_UPDATE_SNAPSHOTS=1 to accept, and say in CHANGELOG whether it re-judges"
    )


# -- the verdict's fields, the parser's brace search, the topic rules -------------------


def test_verdict_keeps_the_lesson_and_reads_a_null_concept_as_blank():
    v = JudgmentIn(n=1, s=[1, 2, 3, 4, 5], lesson="  Learn this  ", concept=None).to_verdict()
    assert v.lesson == "Learn this" and v.concept == ""


def test_parser_takes_exactly_up_to_the_last_brace():
    """Prose right after the closing brace (no space) must not be swallowed into the JSON."""
    out = parse_judgments("Here: " + _batch(_ok(1)) + ".")
    assert out is not None and [v.n for v in out] == [1]


def test_topic_matching_counts_words_of_three_letters_and_half_the_union():
    """Two-letter words are noise, three-letter words count, and half the words in common is
    a match; an empty topic in the list is skipped, not the end of the list."""
    assert matches_topic("gpu cooling loops", ["gpu cooling design"])  # 2 of 4 words: exactly half
    assert not matches_topic("gpu cooling", ["gpu design"])  # 1 of 3
    assert matches_topic("ai gpus", ["ml gpus"])  # the two-letter words do not dilute the match
    assert matches_topic("reefs", ["", "coral reefs"])


def test_compose_rounds_to_one_decimal_and_reads_a_missing_subscore_as_zero():
    """The same on every Python: 80.45 is summed exactly (math.fsum) and rounds to 80.5 on
    3.10 and 3.13 alike; a naive sum gave 80.44999999999999 on 3.10 and 80.45 on 3.13."""
    cfg = Config()
    assert compose(base_verdict(quality=81, fit=81), cfg) == 80.5
    assert compose({"quality": 80}, cfg) == 24.0  # 0.30 * 80, nothing for the rest


def test_canonical_url_strips_only_the_trailing_slash():
    assert canonical_url("https://example.com/OSX/") == "https://example.com/OSX"


def test_dup_index_title_threshold_and_default_window(conn, cfg):
    """A normalised title of 26 characters is matched by title, 25 is not; the index built with
    no window argument covers seven days."""
    from conftest import seed_scored

    from bodh.judgment import DupIndex

    t26, t25 = "abcdefghij abcdefghij abcd", "abcdefghij abcdefghij abc"
    assert len(t26) == 26 and len(t25) == 25
    ids = seed_scored(
        conn,
        cfg,
        [dict(n=1, score=70, title=t26, feed="Feed A"), dict(n=2, score=70, title=t25, feed="Feed A")],
    )
    index = DupIndex(conn)
    assert index.find({"id": "new", "url": "", "title": t26, "feed_title": "Feed B"})["id"] == ids[0]
    assert index.find({"id": "new", "url": "", "title": t25, "feed_title": "Feed B"}) is None
    probe = {"id": "new", "url": "https://example.com/FeedA/1", "title": "", "feed_title": "x"}
    conn.execute("UPDATE articles SET fetched_at = ?", ((store.utcnow() - timedelta(days=6.5)).isoformat(),))
    assert DupIndex(conn).find(probe)["id"] == ids[0]
    conn.execute("UPDATE articles SET fetched_at = ?", ((store.utcnow() - timedelta(days=7.5)).isoformat(),))
    assert DupIndex(conn).find(probe) is None
