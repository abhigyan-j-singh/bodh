from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import FakeClient, make_item, seed_scored

from bodh import cutting, store
from bodh.config import FolderPolicy
from bodh.readers.bazqux import ItemRef


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
    assert {(r["concept"], r["score"]) for r in sel.cut} == {
        ("gpus", 86.0),
        ("gpus", 85.0),
        ("cooling", 60.0),
    }
    assert sel.boundary == 70.0


def test_prune_fraction_and_below_and_protection(conn, cfg):
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=10, starred=True),
            dict(n=2, score=20),
            dict(n=3, score=50),
            dict(n=4, score=80, protected=True),
            dict(n=5, score=90),
        ],
    )
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
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=10),  # cut
            dict(n=2, score=20, confidence="low"),  # left alone
            dict(n=3, score=30, folder="Always read"),  # protected folder
            dict(n=4, score=60, folder="Firehose"),  # below 70 -> cut
            dict(n=5, score=15),  # cut, but capped out (lowest two go first)
            dict(n=6, score=50),  # stays
        ],
    )
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
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=30, quality=70, concept="unknown field"),  # novel, decent quality -> reserved
            dict(n=2, score=30, quality=20, concept="junk topic"),  # low quality -> cut
            dict(n=3, score=80, concept="gpus"),  # survivor
            dict(n=4, score=80, concept="gpus"),
        ],
    )
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
    store.restore_unread(conn, ids[:1], rescued=True)  # the student marked it unread again
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
    assert cutting.select_autocut(conn, cfg, judge=None, run_id=None).cut_ids == []  # not cut again
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0  # an undo is not a rescue
    cfg.prune.keep = 0
    assert set(cutting.select_prune(conn, cfg).cut_ids) == set(ids)  # a prune may still take them


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
    store.set_flags(conn, ids[:1], starred=1)  # starred since
    store.set_flags(conn, ids[1:2], protected=1)  # moved into Must read since
    client = FakeClient()
    untagged, unmarked = cutting.finish_pending_cuts(client, conn, cfg)
    assert (untagged, unmarked) == (1, 1) and client.read == [[ids[2]]]
    assert store.status_ids(conn, "unread") == {ids[0], ids[1]} and store.status_ids(conn, "cut") == {ids[2]}
    assert (cfg.tags.cut, sorted(ids[:2])) in [(n, sorted(i)) for n, i in client.untagged]
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


# -- the cut-off, the safety limit and the blocked run ---------------------------------


class RecordingJudge:
    """A judge that records what it was asked to review and answers as scripted: ids missing
    from ``answers`` were never examined."""

    batch_mode = False

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def reviews(self, rows, run_id, client=None, stop_after=None):
        ids = [r["id"] for r in rows]
        self.calls.append((ids, run_id, client, stop_after))
        return {aid: self.answers[aid] for aid in ids if aid in self.answers}


def _long_text(conn, ids):
    """More text than the first pass saw: a close call worth a review."""
    conn.executemany("UPDATE articles SET text = ? WHERE id = ?", [("word " * 1000, aid) for aid in ids])


def test_score_exactly_at_the_cutoff_is_not_cut(conn, cfg):
    """A candidate sits strictly below its cut-off; the boundary is the lowest surviving score."""
    cfg.autocut.cutoff = 35
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=34), dict(n=2, score=35), dict(n=3, score=50)])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    assert sel.cut_ids == [ids[0]]
    assert {r["id"] for r in sel.survivors} == {ids[1], ids[2]} and sel.boundary == 35.0


def test_low_confidence_is_never_a_candidate(conn, cfg):
    """A low-confidence score leaves the article alone, with no per-run cap to hide behind."""
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=10, confidence="low"), dict(n=2, score=10)])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    assert sel.cut_ids == [ids[1]] and ids[0] in {r["id"] for r in sel.survivors}


def test_folder_cutoff_is_honoured_without_the_cap(conn, cfg):
    """A folder's own cut-off cuts what the default would keep; a protected folder cuts nothing."""
    cfg.autocut.cutoff = 35
    cfg.autocut.new_ground_share = 0.0
    cfg.folders = {"Always read": FolderPolicy(protected=True), "Firehose": FolderPolicy(cutoff=70)}
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=60, folder="Firehose"),
            dict(n=2, score=10, folder="Always read"),
            dict(n=3, score=60),
        ],
    )
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    assert sel.cut_ids == [ids[0]] and {r["id"] for r in sel.survivors} == {ids[1], ids[2]}


def test_safety_limit_share_and_minimum_are_exact(conn, cfg):
    """The share is compared strictly, over exactly ``min_judged`` newly scored articles."""
    cfg.autocut.new_ground_share = 0.0
    cfg.safety.min_judged = 10
    cfg.safety.max_cut_share = 0.8
    ids = seed_scored(conn, cfg, [dict(n=i, score=80 if i <= 2 else 5) for i in range(1, 11)])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=1, newly_scored=set(ids))
    assert len(sel.cut_ids) == 8 and cutting.safety_tripped(conn) is None  # 8 of 10: at the share, not over
    extra = seed_scored(conn, cfg, [dict(n=11, score=5)])
    with pytest.raises(cutting.SafetyTripped):  # 9 of 10, and 10 is the minimum, not one more
        cutting.select_autocut(conn, cfg, judge=None, run_id=2, newly_scored=set(ids[1:] + extra))
    assert cutting.safety_tripped(conn) == "2"


def test_safety_trip_is_recorded_under_both_keys_and_reset_clears_both(conn, cfg):
    """The trip is written under today's key and the one the old code reads, with the run id or
    a question mark; a reset removes both."""
    cfg.safety.min_judged = 2
    ids = seed_scored(conn, cfg, [dict(n=1, score=5), dict(n=2, score=5)])
    with pytest.raises(cutting.SafetyTripped):
        cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored=set(ids))
    assert store.kv_get(conn, store.SAFETY_KEY) == "?" and store.kv_get(conn, store.LEGACY_SAFETY_KEY) == "?"
    assert cutting.reset_safety(conn)
    assert (
        store.kv_get(conn, store.SAFETY_KEY) is None and store.kv_get(conn, store.LEGACY_SAFETY_KEY) is None
    )
    with pytest.raises(cutting.SafetyTripped):
        cutting.select_autocut(conn, cfg, judge=None, run_id=7, newly_scored=set(ids))
    assert store.kv_get(conn, store.SAFETY_KEY) == "7" and store.kv_get(conn, store.LEGACY_SAFETY_KEY) == "7"


def test_blocked_run_reports_the_boundary_of_what_it_kept(conn, cfg):
    """While the trip stands the run keeps everything and still reports the lowest eligible
    score, or none when nothing eligible is left."""
    store.kv_set(conn, store.SAFETY_KEY, "3")
    ids = seed_scored(conn, cfg, [dict(n=1, score=20), dict(n=2, score=10, starred=True)])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=4)
    assert sel.blocked and sel.cut_ids == [] and sel.boundary == 20.0
    store.set_flags(conn, ids[:1], starred=1)
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=5)
    assert sel.blocked and len(sel.survivors) == 2 and sel.boundary is None


# -- the review of close calls ---------------------------------------------------------


def test_review_receives_the_run_id_the_client_and_the_deadline(conn, cfg):
    """What the run knows (its id, the reader client, the deadline) reaches the reviewer."""
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=30)])
    _long_text(conn, ids)
    judge = RecordingJudge(answers={ids[0]: True})
    client = object()
    sel = cutting.select_autocut(conn, cfg, judge, run_id=5, client=client, stop_after=99.5)  # type: ignore[arg-type]
    assert judge.calls == [([ids[0]], 5, client, 99.5)]
    assert sel.cut_ids == [] and sel.reviews == 1 and sel.saved_by_review == 1


def test_close_call_the_review_never_reached_stays_unread(conn, cfg):
    """A close call the reviewer did not get to (budget, deadline) is kept, and the ones after
    it are still decided."""
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=30), dict(n=2, score=31)])
    _long_text(conn, ids)
    judge = RecordingJudge(answers={ids[1]: False})
    sel = cutting.select_autocut(conn, cfg, judge, run_id=None)  # type: ignore[arg-type]
    assert sel.cut_ids == [ids[1]] and sel.reviews == 1 and sel.saved_by_review == 1
    assert ids[0] in {r["id"] for r in sel.survivors} and sel.boundary == 30.0


def test_close_call_with_nothing_more_to_read_is_cut_unasked(conn, cfg):
    """When the first pass saw the whole text there is nothing a review could add."""
    cfg.autocut.new_ground_share = 0.0
    ids = seed_scored(conn, cfg, [dict(n=1, score=30)])
    judge = RecordingJudge()
    sel = cutting.select_autocut(conn, cfg, judge, run_id=None)  # type: ignore[arg-type]
    assert judge.calls == [] and sel.cut_ids == [ids[0]] and sel.reviews == 0


def test_review_window_sits_under_the_folder_cutoff(conn, cfg):
    """The window is measured from the article's own folder cut-off, and a score exactly one
    window below it is still a close call."""
    cfg.autocut.new_ground_share = 0.0
    cfg.autocut.cutoff = 35
    cfg.review.window = 10
    cfg.folders = {"Firehose": FolderPolicy(cutoff=70)}
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=59, folder="Firehose"),  # under the window: cut without a review
            dict(n=2, score=60, folder="Firehose"),  # exactly one window below 70: reviewed
            dict(n=3, score=24),  # under the default window
            dict(n=4, score=25),  # exactly one window below 35: reviewed
        ],
    )
    _long_text(conn, ids)
    judge = RecordingJudge()
    sel = cutting.select_autocut(conn, cfg, judge, run_id=None)  # type: ignore[arg-type]
    assert [sorted(call[0]) for call in judge.calls] == [sorted([ids[1], ids[3]])]
    assert sorted(sel.cut_ids) == sorted([ids[0], ids[2]])


# -- the new-ground quota ------------------------------------------------------------


def test_new_ground_quota_is_a_share_of_what_was_newly_scored_and_kept(conn, cfg):
    """The quota counts the newly scored articles autocut leaves, not the whole pool: when every
    new article is a candidate there is nothing to reserve against."""
    cfg.autocut.new_ground_share = 0.5
    ids = seed_scored(
        conn,
        cfg,
        [dict(n=i, score=80, concept="gpus") for i in range(1, 5)]
        + [dict(n=5, score=30, quality=70, concept="fresh ground")],
    )
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored={ids[4]})
    assert sel.cut_ids == [ids[4]] and sel.reserved_for_new_ground == 0


def test_new_ground_takes_the_best_decent_article_on_an_unknown_concept(conn, cfg):
    """Quality 60 is decent, 59 is not; among the novel candidates the highest score is kept."""
    cfg.autocut.new_ground_share = 0.5
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=80, concept="gpus"),
            dict(n=2, score=80, concept="gpus"),
            dict(n=3, score=30, quality=60, concept="alpha"),
            dict(n=4, score=32, quality=59, concept="beta"),  # the best score, but not decent
            dict(n=5, score=28, quality=70, concept="gamma"),
        ],
    )
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored=set(ids))
    assert sel.reserved_for_new_ground == 1  # ceil(0.5 * (5 newly scored - 3 candidates))
    assert sorted(sel.cut_ids) == sorted([ids[3], ids[4]])
    assert conn.execute("SELECT exploration FROM state WHERE article_id = ?", (ids[2],)).fetchone()[0] == 1


def test_new_ground_knows_concepts_from_stars_reads_and_rescues_within_the_window(conn, cfg):
    """A concept starred, read or rescued inside the window is known, whatever its case; one
    read before the window is new ground again."""
    cfg.autocut.new_ground_share = 0.5
    cfg.profile.concept_window_days = 90
    known = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=80, concept="Known Star"),
            dict(n=2, score=80, concept="known read"),
            dict(n=3, score=80, concept="known rescue"),
            dict(n=4, score=80, concept="old concept"),
        ],
    )
    store.add_feedback(conn, known[0], "star")
    store.add_feedback(conn, known[1], "read")
    store.add_feedback(conn, known[2], "rescue")
    long_ago = (store.utcnow() - timedelta(days=100)).isoformat()
    conn.execute("INSERT INTO feedback (article_id, kind, at) VALUES (?, 'read', ?)", (known[3], long_ago))
    cands = seed_scored(
        conn,
        cfg,
        [
            dict(n=11, score=30, quality=70, concept="known star"),
            dict(n=12, score=30, quality=70, concept="known read"),
            dict(n=13, score=30, quality=70, concept="known rescue"),
            dict(n=14, score=30, quality=70, concept="old concept"),
        ],
    )
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None, newly_scored=set(known + cands))
    assert sel.reserved_for_new_ground == 1 and sorted(sel.cut_ids) == sorted(cands[:3])


def test_new_ground_known_concepts_are_not_capped_at_forty(conn, cfg):
    """The store lists the forty most common concepts by default; the quota must see them all,
    or a concept the student reads a little would look like new ground."""
    cfg.autocut.new_ground_share = 0.2
    crowd = seed_scored(conn, cfg, [dict(n=i, score=80, concept=f"topic {i}") for i in range(1, 41)])
    for aid in crowd:
        for kind in ("star", "read", "rescue"):
            store.add_feedback(conn, aid, kind)
    quiet = seed_scored(conn, cfg, [dict(n=41, score=80, concept="read once")])
    store.add_feedback(conn, quiet[0], "read")
    cand = seed_scored(conn, cfg, [dict(n=42, score=30, quality=70, concept="read once")])
    sel = cutting.select_autocut(conn, cfg, judge=None, run_id=None)
    assert sel.cut_ids == cand and sel.reserved_for_new_ground == 0


# -- the manual prune ----------------------------------------------------------------


def test_prune_keep_argument_overrides_the_config_and_ties_keep_the_newer(conn, cfg):
    """``keep`` given on the command line wins over the config; two equal scores rank the newer
    article first, so it is the one that stays."""
    cfg.prune.keep = 40
    ids = seed_scored(conn, cfg, [dict(n=1, score=50), dict(n=2, score=50), dict(n=3, score=40)])
    sel = cutting.select_prune(conn, cfg, keep=1)
    assert [r["id"] for r in sel.survivors] == [ids[1]] and sorted(sel.cut_ids) == sorted(ids[:1] + ids[2:])


def test_prune_below_keeps_a_score_at_the_line(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=49), dict(n=2, score=50), dict(n=3, score=51)])
    sel = cutting.select_prune(conn, cfg, below=50)
    assert sel.cut_ids == [ids[0]] and {r["id"] for r in sel.survivors} == {ids[1], ids[2]}


def test_prune_fraction_is_clamped_and_splits_the_ranking(conn, cfg):
    """A fraction above one cuts every movable article; a half keeps the better half."""
    ids = seed_scored(
        conn, cfg, [dict(n=1, score=10), dict(n=2, score=20), dict(n=3, score=30, starred=True)]
    )
    sel = cutting.select_prune(conn, cfg, fraction=1.5)
    assert sorted(sel.cut_ids) == sorted(ids[:2]) and [r["id"] for r in sel.survivors] == [ids[2]]
    sel = cutting.select_prune(conn, cfg, fraction=0.5)
    assert sel.cut_ids == [ids[0]] and {r["id"] for r in sel.survivors} == {ids[1], ids[2]}


def test_prune_concept_cap_counts_exactly(conn, cfg):
    """With two seats and a cap of one per concept the second gpu article yields its seat to
    history, and nothing else is let in once the seats are full."""
    cfg.prune.keep = 2
    cfg.prune.max_share_per_concept = 0.5
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=90, concept="gpus"),
            dict(n=2, score=85, concept="gpus"),
            dict(n=3, score=70, concept="history"),
            dict(n=4, score=40, concept="cooling"),
        ],
    )
    sel = cutting.select_prune(conn, cfg)
    assert {r["id"] for r in sel.survivors} == {ids[0], ids[2]} and sorted(sel.cut_ids) == sorted(
        [ids[1], ids[3]]
    )


def test_prune_deferred_article_needs_more_than_one_band_to_win_its_seat_back(conn, cfg):
    """Exactly one band better is not enough: the cap still yields to the other concept."""
    cfg.prune.keep = 2
    cfg.prune.max_share_per_concept = 0.5
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=90, concept="gpus"),
            dict(n=2, score=80, concept="gpus"),
            dict(n=3, score=60, concept="history"),
        ],
    )
    sel = cutting.select_prune(conn, cfg)
    assert {r["id"] for r in sel.survivors} == {ids[0], ids[2]} and sel.cut_ids == [ids[1]]


def test_prune_fills_empty_seats_with_the_best_deferred_articles(conn, cfg):
    """When the cap leaves seats empty the best of what it deferred takes them, in order."""
    cfg.prune.keep = 3
    cfg.prune.max_share_per_concept = 0.3  # ceil(0.9): one per concept
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=90, concept="gpus"),
            dict(n=2, score=85, concept="gpus"),
            dict(n=3, score=80, concept="gpus"),
            dict(n=4, score=70, concept="history"),
        ],
    )
    sel = cutting.select_prune(conn, cfg)
    assert {r["id"] for r in sel.survivors} == {ids[0], ids[1], ids[3]} and sel.cut_ids == [ids[2]]


# -- writing to BazQux --------------------------------------------------------------


def test_apply_cut_records_the_run_and_the_mode(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=10)])
    run_id = store.begin_run(conn, "run")
    cutting.apply_cut(FakeClient(), conn, cfg, ids, run_id, "autocut")
    row = store.article(conn, ids[0])
    assert (row["status"], row["cut_mode"], row["cut_run_id"]) == ("cut", "autocut", run_id)


def test_finish_pending_cuts_sets_both_flags_to_one(conn, cfg):
    """The flags are what the next run and the rescue inference read: exactly 1, not merely set."""
    ids = seed_scored(conn, cfg, [dict(n=1, score=10)])
    store.mark_cut(conn, ids, store.begin_run(conn, "run"), "autocut")
    assert cutting.finish_pending_cuts(FakeClient(), conn, cfg) == (1, 1)
    flags = conn.execute(
        "SELECT cut_tagged, read_marked FROM state WHERE article_id = ?", (ids[0],)
    ).fetchone()
    assert tuple(flags) == (1, 1)


def test_restored_pending_cut_is_not_a_rescue(conn, cfg):
    """An article starred before its cut reached BazQux goes back unread without a rescue
    stamp: the student never reversed a cut, so clear-old and the rescue rate must not see one."""
    ids = seed_scored(conn, cfg, [dict(n=1, score=10)])
    store.mark_cut(conn, ids, store.begin_run(conn, "run"), "autocut")
    store.set_flags(conn, ids, starred=1)
    assert cutting.finish_pending_cuts(FakeClient(), conn, cfg) == (0, 0)
    row = store.article(conn, ids[0])
    assert row["status"] == "unread" and row["rescued_at"] is None and store.rescued_ids(conn) == set()


def test_untag_stale_and_tag_bands_count_what_they_did(conn, cfg):
    """Both return how many articles they touched; a band whose untag failed is not counted,
    and the stale list is cleared either way."""

    class Flaky(FakeClient):
        def untag(self, ids, name):
            if name == "score-30":
                raise RuntimeError("down")
            return super().untag(ids, name)

    stale = {"score-50": ["a", "b"], "score-30": ["c"], "score-0": ["d"]}
    assert cutting.untag_stale(Flaky(), stale) == 3 and stale == {}
    assert cutting.untag_stale(FakeClient(), {}) == 0
    seed_scored(conn, cfg, [dict(n=1, score=55), dict(n=2, score=56), dict(n=3, score=20)])
    client = FakeClient()
    assert cutting.tag_bands(client, conn) == 3 and {n for n, _ in client.tagged} == {"score-50", "score-0"}
    assert cutting.tag_bands(client, conn) == 0


def test_undo_with_nothing_to_restore_returns_zero(conn, cfg):
    client = FakeClient()
    assert cutting.undo(client, conn, cfg, run_id=99) == 0
    assert client.unread_marked == [] and client.untagged == []


class TimedReader(FakeClient):
    """A reader whose unread list carries crawl timestamps, as BazQux's does."""

    def __init__(self, stamps, **kw):
        super().__init__(unread=list(stamps), **kw)
        self.stamps = stamps

    def unread_ids(self):
        return [ItemRef(id=i, timestamp_usec=self.stamps[i]) for i in self.unread]


def test_clear_old_cuts_strictly_older_than_the_cutoff_with_each_exemption(conn, cfg, monkeypatch):
    """The cut-off is ``older_than_days`` before now, in microseconds; an article exactly at it
    stays. Stars, protections and rescues are each exempt on their own, an article BazQux
    knows and we do not is recorded and cut, and a dry run writes nothing."""
    now = 1_760_000_000.0
    monkeypatch.setattr("time.time", lambda: now)
    cutoff = int((now - 7 * 86400) * 1_000_000)
    day = 86400 * 1_000_000
    ids = seed_scored(conn, cfg, [dict(n=i, score=50) for i in range(1, 8)])
    old, at_cutoff, just_older, recent, starred, protected, rescued = ids
    store.restore_unread(conn, [rescued], rescued=True)
    unknown = make_item(99).id
    stamps = {
        old: cutoff - 10 * day,
        at_cutoff: cutoff,
        just_older: cutoff - 1,
        recent: cutoff + 3 * day,
        starred: cutoff - 10 * day,
        protected: cutoff - 10 * day,
        rescued: cutoff - 10 * day,
        unknown: cutoff - 10 * day,
        make_item(98).id: None,
    }
    client = TimedReader(stamps, starred=[starred])
    run_id = store.begin_run(conn, "clear-old")
    assert cutting.clear_old(client, conn, cfg, 7, {protected}, run_id, dry_run=True) == (3, 0)
    assert client.read == [] and store.status_ids(conn, "cut") == set()
    assert cutting.clear_old(client, conn, cfg, 7, {protected}, run_id) == (3, 3)
    expected = {old, just_older, unknown}
    assert store.status_ids(conn, "cut") == expected and set(store.run_cut_ids(conn, run_id)) == expected
    assert set(client.read[0]) == expected
    row = store.article(conn, unknown)
    assert row["cut_mode"] == "clear-old" and row["fetched_at"] and row["title"] == "(clear-old: not fetched)"
