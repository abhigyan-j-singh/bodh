from __future__ import annotations

import pytest
from conftest import FakeClient, seed_scored

from bodh import cutting, store
from bodh.config import FolderPolicy


def test_prune_keep_with_concept_cap(conn, cfg):
    cfg.prune.keep = 4
    cfg.prune.max_share_per_concept = 0.5  # at most 2 per concept among 4 survivors
    specs = [dict(n=i, score=90 - i, concept="gpus") for i in range(1, 6)]  # five gpu articles, 89..85
    specs += [dict(n=10, score=70, concept="history"), dict(n=11, score=60, concept="cooling")]
    seed_scored(conn, cfg, specs)
    sel = cutting.select_prune(conn, cfg)
    survivors = {(r["concept"], r["score"]) for r in sel.survivors}
    # the cap lets history (70) in ahead of the third gpu article (87), a gap inside one band;
    # cooling (60) cannot displace an 87, so the cap yields and the 87 takes the last seat
    assert survivors == {("gpus", 89.0), ("gpus", 88.0), ("gpus", 87.0), ("history", 70.0)}
    assert {(r["concept"], r["score"]) for r in sel.cut} == {("gpus", 86.0), ("gpus", 85.0), ("cooling", 60.0)}
    assert sel.boundary == 70.0


def test_prune_fraction_and_below_and_protection(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10, starred=True), dict(n=2, score=20), dict(n=3, score=50),
                                  dict(n=4, score=80, protected=True), dict(n=5, score=90)])
    sel = cutting.select_prune(conn, cfg, fraction=0.5)
    assert {r["id"] for r in sel.cut} == {ids[1], ids[2]}  # 3 movable, ceil(1.5)=2 lowest
    sel = cutting.select_prune(conn, cfg, below=60)
    assert {r["id"] for r in sel.cut} == {ids[1], ids[2]}  # starred 10 never moves
    sel = cutting.select_prune(conn, cfg, keep=1)
    assert ids[4] in {r["id"] for r in sel.survivors} and ids[0] in {r["id"] for r in sel.survivors}


def test_floor_respects_folders_confidence_and_cap(conn, cfg):
    cfg.autocut.cutoff = 35
    cfg.folders = {"Always read": FolderPolicy(protected=True), "Firehose": FolderPolicy(cutoff=70)}
    cfg.autocut.max_per_run = 2
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [
        dict(n=1, score=10),                                   # cut
        dict(n=2, score=20, confidence="low"),                 # left alone
        dict(n=3, score=30, folder="Always read"),             # protected folder
        dict(n=4, score=60, folder="Firehose"),                # below 70 -> cut
        dict(n=5, score=15),                                   # cut, but capped out (lowest two go first)
        dict(n=6, score=50),                                   # stays
    ])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    cut_ids = sel.cut_ids
    assert ids[0] in cut_ids and ids[4] in cut_ids and len(cut_ids) == 2
    assert ids[1] not in cut_ids and ids[2] not in cut_ids and ids[5] not in cut_ids
    # the Firehose article is a candidate but the per-run cap left it for the next run
    assert ids[3] not in cut_ids


def test_floor_breaker_trips_on_mass_cut(conn, cfg):
    cfg.safety.min_judged = 5
    ids = seed_scored(conn, cfg, [dict(n=i, score=5) for i in range(1, 11)])
    with pytest.raises(cutting.SafetyTripped):
        cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored=set(ids))


def test_floor_exploration_reserves_novel_quality(conn, cfg):
    cfg.autocut.new_ground_share = 0.5
    ids = seed_scored(conn, cfg, [
        dict(n=1, score=30, quality=70, concept="unknown field"),   # novel, decent quality -> reserved
        dict(n=2, score=30, quality=20, concept="junk topic"),      # low quality -> cut
        dict(n=3, score=80, concept="gpus"),                        # survivor
        dict(n=4, score=80, concept="gpus"),
    ])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored=set(ids))
    assert ids[0] not in sel.cut_ids and ids[1] in sel.cut_ids
    assert sel.reserved_for_new_ground == 1
    assert conn.execute("SELECT exploration FROM state WHERE article_id = ?", (ids[0],)).fetchone()[0] == 1


def test_apply_cut_tags_before_read_and_undo(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=12)])
    client = FakeClient()
    run_id = store.begin_run(conn, "run")
    n = cutting.apply_cut(client, conn, cfg, ids, run_id, "floor")
    assert n == 2
    assert client.tagged[0][0] == cfg.tags.cut and set(client.tagged[0][1]) == set(ids)
    assert set(client.read[0]) == set(ids)
    assert store.cut_pending(conn) == ([], [])
    restored = cutting.undo(client, conn, cfg, run_id)
    assert restored == 2
    assert set(client.unread_marked[0]) == set(ids)
    assert store.status_ids(conn, "unread") == set(ids)


def test_crash_between_tag_and_read_is_finished_next_run(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids, run_id, "floor")
    store.set_flags(conn, ids, cut_tagged=1)  # crashed after tagging, before marking read
    client = FakeClient()
    untagged, unmarked = cutting.finish_pending_cuts(client, conn, cfg)
    assert (untagged, unmarked) == (0, 1)
    assert client.read == [ids]


def test_rescued_article_survives_floor_and_prune(conn, cfg):
    cfg.autocut.new_ground_share = 0.0
    cfg.prune.keep = 1
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=12), dict(n=3, score=90)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[:1], run_id, "floor")
    store.restore_unread(conn, ids[:1], rescued=True)   # the student marked it unread again
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    assert sel.cut_ids == [ids[1]]
    sel = cutting.select_prune(conn, cfg)
    assert ids[0] not in sel.cut_ids and ids[1] in sel.cut_ids


def test_undo_is_not_reversed_by_the_next_floor(conn, cfg):
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=12)])
    client = FakeClient()
    run_id = store.begin_run(conn, "run")
    cutting.apply_cut(client, conn, cfg, ids, run_id, "floor")
    assert cutting.undo(client, conn, cfg, run_id) == 2
    assert cutting.select_autocut(conn, cfg, judge=None, run_id=None).cut_ids == []   # not cut again
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0        # an undo is not a rescue
    cfg.prune.keep = 0
    assert set(cutting.select_prune(conn, cfg).cut_ids) == set(ids)                  # a prune may still take them


def test_breaker_blocks_the_floor_until_cleared(conn, cfg):
    cfg.safety.min_judged = 5
    ids = seed_scored(conn, cfg, [dict(n=i, score=5) for i in range(1, 11)])
    with pytest.raises(cutting.SafetyTripped):
        cutting.select_autocut(conn, cfg, judge=None, run_id=7, newly_scored=set(ids))
    assert cutting.safety_tripped(conn) == "7"
    # the next run sees nothing new, so the share test cannot trip again: the trip must stand
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=8, newly_scored=set())
    assert sel.cut_ids == [] and "safety reset" in sel.blocked and len(sel.survivors) == 10
    assert cutting.reset_safety(conn) and cutting.safety_tripped(conn) is None
    assert len(cutting.select_autocut(conn, cfg, judge=None, run_id=9, newly_scored=set()).cut_ids) == 10


def test_saved_count_excludes_old_failures(conn, cfg):
    """An article kept after a failed second look in an earlier run was counted as a save on
    every later run."""
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=30), dict(n=2, score=10)])
    store.record_review_failure(conn, ids[0])

    class NoLooks:
        batch_mode = False

        def reviews(self, *a, **k):
            raise AssertionError("nothing should be looked at")

    sel = cutting.select_autocut(conn, cfg, judge=NoLooks(), run_id=None)  # type: ignore[arg-type]
    assert sel.cut_ids == [ids[1]] and sel.saved_by_review == 0 and sel.kept_after_failed_look == 1
    assert ids[0] in {r["id"] for r in sel.survivors}


def test_finish_pending_cuts_respects_a_new_star(conn, cfg):
    """A star added after mark_cut (a crash in between, or the student was quicker) used to be
    tagged and marked read anyway."""
    ids = seed_scored(conn, cfg, [dict(n=1, score=10), dict(n=2, score=12), dict(n=3, score=14)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids, run_id, "floor")
    store.set_flags(conn, ids[:1], starred=1)        # starred since
    store.set_flags(conn, ids[1:2], protected=1)     # moved into Must read since
    client = FakeClient()
    untagged, unmarked = cutting.finish_pending_cuts(client, conn, cfg)
    assert (untagged, unmarked) == (1, 1) and client.read == [[ids[2]]]
    assert store.status_ids(conn, "unread") == {ids[0], ids[1]} and store.status_ids(conn, "cut") == {ids[2]}
    assert (cfg.tags.cut, sorted(ids[:2])) in [(n, sorted(i)) for n, i in client.untagged]
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
