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


def test_verdicts_panel_and_disagreements(conn, cfg):
    ids = seed_scored(conn, cfg, [dict(n=1, score=95, concept="x"), dict(n=2, score=85), dict(n=3, score=20), dict(n=4, score=55)])
    store.set_verdict(conn, ids[0], "great", store.GRADES)
    store.set_verdict(conn, ids[1], "bad", store.GRADES)      # the teacher's 85 the student calls junk
    store.set_verdict(conn, ids[2], "good", store.GRADES)     # a 20 the student liked
    store.set_verdict(conn, ids[3], "meh", store.GRADES)      # a 55: no disagreement
    verd = {v["band"]: v for v in dashboard.verdicts(conn)}
    assert verd["90 to 100"]["great"] == 1 and verd["70 to 89"]["bad"] == 1 and verd["0 to 29"]["good"] == 1
    dis = dashboard.disagreements(conn)
    assert {r["id"] for r in dis} == {ids[1], ids[2]}
    html = dashboard.render(cfg, conn).read_text(encoding="utf-8")
    assert "Your verdicts" in html and "disagree most" in html and "Article 2" in html
