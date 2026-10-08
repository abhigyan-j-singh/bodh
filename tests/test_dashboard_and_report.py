from __future__ import annotations

from conftest import seed_scored

from triage import dashboard, report, store


def test_dashboard_and_weekly_note_render(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=i, score=10 * i, concept=f"c{i % 3}") for i in range(1, 10)])
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[:3], run_id, "floor")
    store.add_feedback(conn, ids[8], "read")
    store.add_feedback(conn, ids[7], "star")
    store.finish_run(conn, run_id, True, summary="ok")
    path = dashboard.render(cfg, conn)
    html = path.read_text(encoding="utf-8")
    assert "Near misses" in html and "Calibration" in html and "Article 9" in html
    note = dashboard.weekly_note(cfg, conn, {"reason": "kept", "diff": ""})
    assert note is not None and "Learnings this week" in note.read_text(encoding="utf-8")
    rep = report.render(cfg, conn)
    assert "Feed report" in rep.read_text(encoding="utf-8")


def test_calibration_bands(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=95), dict(n=2, score=75), dict(n=3, score=20)])
    conn.execute("UPDATE scores SET scored_at = '2000-01-01T00:00:00+00:00'")
    store.add_feedback(conn, ids[0], "read")
    cal = {c["band"]: c for c in dashboard.calibration(conn)}
    assert cal["90 to 100"]["n"] == 1 and cal["90 to 100"]["read_share"] == 1.0
    assert cal["70 to 89"]["read_share"] == 0.0
