from __future__ import annotations

import pytest
from conftest import FakeClient, seed_scored

from triage import cut, store
from triage.config import FolderPolicy


def test_prune_keep_with_concept_cap(conn, cfg):
    cfg.cut.target_unread = 4
    cfg.cut.concept_cap = 0.5  # at most 2 per concept among 4 survivors
    specs = [dict(n=i, score=90 - i, concept="gpus") for i in range(1, 6)]  # five gpu articles, 89..85
    specs += [dict(n=10, score=70, concept="history"), dict(n=11, score=60, concept="cooling")]
    seed_scored(conn, cfg, specs)
    sel = cut.select_prune(conn, cfg)
    survivors = {(r["concept"], r["score"]) for r in sel.survivors}
    # the cap lets history (70) in ahead of the third gpu article (87), a gap inside one band;
    # cooling (60) cannot displace an 87, so the cap yields and the 87 takes the last seat
    assert survivors == {("gpus", 89.0), ("gpus", 88.0), ("gpus", 87.0), ("history", 70.0)}
    assert {(r["concept"], r["score"]) for r in sel.cut} == {("gpus", 86.0), ("gpus", 85.0), ("cooling", 60.0)}
    assert sel.boundary == 70.0


def test_prune_fraction_and_below_and_protection(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10, starred=True), dict(n=2, score=20), dict(n=3, score=50),
                                  dict(n=4, score=80, protected=True), dict(n=5, score=90)])
    sel = cut.select_prune(conn, cfg, fraction=0.5)
    assert {r["id"] for r in sel.cut} == {ids[1], ids[2]}  # 3 movable, ceil(1.5)=2 lowest
    sel = cut.select_prune(conn, cfg, below=60)
    assert {r["id"] for r in sel.cut} == {ids[1], ids[2]}  # starred 10 never moves
    sel = cut.select_prune(conn, cfg, keep=1)
    assert ids[4] in {r["id"] for r in sel.survivors} and ids[0] in {r["id"] for r in sel.survivors}


def test_floor_respects_folders_confidence_and_cap(conn, cfg):
    cfg.cut.default_floor = 35
    cfg.cut.folders = {"Always read": FolderPolicy(protected=True), "Firehose": FolderPolicy(auto_cut_below=70)}
    cfg.cut.max_cuts_per_run = 2
    cfg.cut.exploration_share = 0.0
    ids = seed_scored(conn, cfg, [
        dict(n=1, score=10),                                   # cut
        dict(n=2, score=20, confidence="low"),                 # left alone
        dict(n=3, score=30, folder="Always read"),             # protected folder
        dict(n=4, score=60, folder="Firehose"),                # below 70 -> cut
        dict(n=5, score=15),                                   # cut, but capped out (lowest two go first)
        dict(n=6, score=50),                                   # stays
    ])
    sel = cut.select_floor(conn, cfg, teacher=None, run_id=None)
    cut_ids = sel.cut_ids
    assert ids[0] in cut_ids and ids[4] in cut_ids and len(cut_ids) == 2
    assert ids[1] not in cut_ids and ids[2] not in cut_ids and ids[5] not in cut_ids
    # the Firehose article is a candidate but the per-run cap left it for the next run
    assert ids[3] not in cut_ids


def test_floor_breaker_trips_on_mass_cut(conn, cfg):
    cfg.breakers.min_new_for_breaker = 5
    ids = seed_scored(conn, cfg, [dict(n=i, score=5) for i in range(1, 11)])
    with pytest.raises(cut.BreakerTripped):
        cut.select_floor(conn, cfg, teacher=None, run_id=None, newly_scored=set(ids))


def test_floor_exploration_reserves_novel_quality(conn, cfg):
    cfg.cut.exploration_share = 0.5
    ids = seed_scored(conn, cfg, [
        dict(n=1, score=30, quality=70, concept="unknown field"),   # novel, decent quality -> reserved
        dict(n=2, score=30, quality=20, concept="junk topic"),      # low quality -> cut
        dict(n=3, score=80, concept="gpus"),                        # survivor
        dict(n=4, score=80, concept="gpus"),
    ])
    sel = cut.select_floor(conn, cfg, teacher=None, run_id=None, newly_scored=set(ids))
    assert ids[0] not in sel.cut_ids and ids[1] in sel.cut_ids
    assert sel.reserved_for_exploration == 1
    assert conn.execute("SELECT exploration FROM state WHERE article_id = ?", (ids[0],)).fetchone()[0] == 1


def test_apply_cut_tags_before_read_and_undo(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=12)])
    client = FakeClient()
    run_id = store.begin_run(conn, "run")
    n = cut.apply_cut(client, conn, cfg, ids, run_id, "floor")
    assert n == 2
    assert client.tagged[0][0] == cfg.tags.cut and set(client.tagged[0][1]) == set(ids)
    assert set(client.read[0]) == set(ids)
    assert store.cut_pending(conn) == ([], [])
    restored = cut.undo(client, conn, cfg, run_id)
    assert restored == 2
    assert set(client.unread_marked[0]) == set(ids)
    assert store.status_ids(conn, "unread") == set(ids)


def test_crash_between_tag_and_read_is_finished_next_run(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids, run_id, "floor")
    store.set_flags(conn, ids, cut_tagged=1)  # crashed after tagging, before marking read
    client = FakeClient()
    untagged, unmarked = cut.finish_pending_cuts(client, conn, cfg)
    assert (untagged, unmarked) == (0, 1)
    assert client.read == [ids]
