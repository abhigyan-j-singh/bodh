"""`bodh rescore`: stored sub-scores recomposed with the current weights, band tags moved."""

from __future__ import annotations

from conftest import FakeClient, seed_scored

from bodh import rescoring, store


def _heavy_on_quality(cfg):
    weights = {"quality": 0.8, "learning": 0.05, "fit": 0.05, "breadth": 0.05, "relevance": 0.05}
    return cfg.model_copy(update={"judge": cfg.judge.model_copy(update={"weights": weights})})


def test_plan_recomposes_unread_scores_and_keeps_a_second_look(conn, cfg):
    """Equal sub-scores compose to the same number under any weights; a quality that differs
    moves with the weights. A cut article is left out unless asked for; the review's higher
    second look stays the score."""
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=50, quality=90),
            dict(n=2, score=60),
            dict(n=3, score=50, quality=90),
            dict(n=4, score=40, quality=10),
        ],
    )
    store.mark_cut(conn, [ids[2]], store.begin_run(conn, "run"), "autocut")
    store.record_review(conn, ids[1], 75.0, "review", 75.0, cfg.tags.band_for(75.0))
    heavy = _heavy_on_quality(cfg)
    plan = rescoring.plan(conn, heavy, [], include_cut=False)
    assert plan.examined == 3 and [c.id for c in plan.changes] == [ids[0], ids[3]]
    c = plan.changes[0]
    assert c.old == 50 and c.new == c.base == 82 and (c.old_band, c.new_band) == ("score-50", "score-70")
    assert plan.changes[1].new == 16  # 0.8 * 10 + 0.2 * 40: under the cut-off now
    assert plan.moves == {"score-50 → score-70": 1, "score-30 → score-0": 1}
    assert plan.now_below == 1 and plan.now_above == 0
    everything = rescoring.plan(conn, heavy, [], include_cut=True)
    assert everything.examined == 4 and sorted(c.id for c in everything.changes) == sorted(
        [ids[0], ids[2], ids[3]]
    )
    assert not [c for c in everything.changes if c.id == ids[2]][0].unread
    # the reviewed article: base moves, the second look still wins, so nothing changes
    low = cfg.model_copy(update={"judge": cfg.judge.model_copy(update={"weights": {**cfg.judge.weights}})})
    assert all(c.id != ids[1] for c in rescoring.plan(conn, low, [], include_cut=False).changes)


def test_apply_writes_the_scores_and_moves_the_band_tags(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=50, quality=90), dict(n=2, score=50, quality=90)])
    client = FakeClient()
    assert rescoring.apply(conn, client, []) == 0
    from bodh import cutting

    cutting.tag_bands(client, conn)  # score-50 is on both in BazQux
    store.mark_cut(conn, [ids[1]], store.begin_run(conn, "run"), "autocut")
    client.tagged.clear()
    plan = rescoring.plan(conn, _heavy_on_quality(cfg), [], include_cut=True)
    assert rescoring.apply(conn, client, plan.changes) == 2
    row = conn.execute(
        "SELECT score, base_score, bucket, bucket_tagged FROM scores WHERE article_id = ?", (ids[0],)
    ).fetchone()
    assert tuple(row) == (82.0, 82.0, "score-70", 1)
    # the unread article's old band comes off and the new one goes on; the cut one is only re-tagged
    assert client.untagged == [("score-50", [ids[0]])]
    assert client.tagged == [("score-70", [ids[0], ids[1]])]
    assert rescoring.plan(conn, _heavy_on_quality(cfg), [], include_cut=True).changes == []
