"""The dashboard's document, its page in three modes, and the console that serves them live:
every number on the page is in the document, the empty states teach, the modes differ only in
the known nodes, no secret reaches the page, the budgets hold, and the console's routes
answer (the page, the document, the ticks, the rows, the plans, a verdict, the events)."""

from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import seed_scored

from bodh import dashboard, locks, store
from bodh.console import Console
from bodh.web import document, panels, templates
from bodh.web.document import DashboardDocument

MODES = ("console", "export", "app")


def seeded(conn, cfg) -> list[str]:
    """A small world with every panel populated: bands, folders, feeds, a run with cuts, reads,
    stars, grades, spend over several days, a batch with the model."""
    specs = []
    for i in range(1, 13):
        specs.append(
            dict(
                n=i,
                score=8 * i,
                folder="News" if i % 2 else "Essays",
                feed="Feed One" if i % 3 else "Feed Two",
                concept=f"concept {i % 4}",
                crawled_msec=1_700_000_000_000 + i * 3_600_000,
            )
        )
    ids = seed_scored(conn, cfg, specs)
    run_id = store.begin_run(conn, "run")
    store.mark_cut(conn, ids[:3], run_id, "floor")
    store.finish_run(conn, run_id, True, summary="judged 12 · cut 3")
    store.add_feedback(conn, ids[11], "read")
    store.add_feedback(conn, ids[10], "star")
    store.add_feedback(conn, ids[9], "read")
    store.set_verdict(conn, ids[11], "great", store.GRADES)
    store.set_verdict(conn, ids[4], "bad", store.GRADES)
    store.set_verdict(conn, ids[1], "good", store.GRADES)  # a 16 the reader liked: a disagreement
    for days_ago in range(0, 5):
        store.record_cost(
            conn, run_id, "claude-haiku-5-5", "judge", {"input_tokens": 1000, "output_tokens": 100}, 0.12
        )
        conn.execute(
            "UPDATE costs SET created_at = ? WHERE id = (SELECT MAX(id) FROM costs)",
            ((datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),),
        )
    store.add_batch(conn, "msgbatch_test", run_id, "judge", "claude-haiku-5-5", [("r1", ids[:2])])
    return ids


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d[\d,]*(?:\.\d+)?", text)


def _json_values(obj) -> set[float]:
    out: set[float] = set()

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            out.add(float(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            out.add(float(len(x)))
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for n in _numbers(x):
                out.add(float(n.replace(",", "")))

    walk(obj)
    return out


def test_document_round_trips_and_its_schema_is_the_published_one(conn, cfg):
    seeded(conn, cfg)
    doc = document.build(cfg, conn)
    again = DashboardDocument.model_validate_json(doc.to_json())
    assert again == doc and doc.to_dict()["schema"] == document.SCHEMA
    schema = document.json_schema()
    for key in ("state", "today", "list", "verdicts", "learning", "feeds", "money", "machinery", "footer"):
        assert key in schema["properties"]
    assert schema["properties"]["schema"]["default"] == 1
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import gen_schema

    assert gen_schema.OUT.read_text(encoding="utf-8") == gen_schema.text(), "run python scripts/gen_schema.py"


@pytest.mark.parametrize("mode", MODES)
def test_every_number_on_the_page_is_in_the_document(conn, cfg, mode):
    """The content test of the plan: the page invents no number. A number on the page is in
    the JSON as itself, as the value it rounds (a score 92.3 shown as 92), as the share it
    prints (0.26 shown as 26%), or as the size of a list (showing 50 of ...)."""
    seeded(conn, cfg)
    doc = document.build(cfg, conn, mode)
    page = panels.render(doc, mode)
    body = re.sub(
        r"<script.*?</script>|<style.*?</style>|<svg class=\"(columns|spark)\".*?</svg>",
        " ",
        page,
        flags=re.S,
    )
    body = re.sub(r"<[^>]+>", " ", body)
    values = _json_values(doc.to_dict())
    rounded = {round(v) for v in values} | {round(100 * v) for v in values}
    missing = []
    for token in _numbers(body):
        n = float(token.replace(",", ""))
        if n in values or n in rounded:
            continue
        missing.append(token)
    assert not missing, missing


def test_empty_states_teach_and_promise(conn, cfg):
    doc = document.build(cfg, conn)
    page = panels.render(doc, "console")
    for text in (
        doc.list.empty_text,
        doc.verdicts.grid_sentence,
        doc.verdicts.disagreements_empty,
        doc.verdicts.calibration_empty,
        doc.verdicts.near_misses_empty,
        doc.learning.concepts_empty,
        doc.learning.cloud_empty,
        doc.feeds.empty_text,
        doc.money.empty_text,
        doc.machinery.batches_empty,
        doc.machinery.runs_empty,
    ):
        assert text and templates.esc(text) in page, text
    assert "No data" not in page and ">–<" not in page
    assert doc.state.sentence.startswith("Idle") and "no run yet" in doc.state.sentence
    assert doc.today.unread == 0 and "Nothing yet today" in doc.today.lead


def test_the_three_modes_differ_only_in_the_known_nodes(conn, cfg):
    seeded(conn, cfg)
    now = datetime.now(timezone.utc)
    console = panels.render(document.build(cfg, conn, "console", now), "console", generation=7)
    export_doc = document.build(cfg, conn, "export", now)
    export = panels.render(export_doc, "export")
    app = panels.render(document.build(cfg, conn, "app", now), "app", theme="dark")
    # the console: levers, the verdict words, the other actions, the log, the live script, the stamp
    for node in (
        'data-action="run"',
        'data-verdicts="great good',
        'class="more js-only"',
        'id="log"',
        "EventSource",
        'id="stamp" data-as-of',
        'name="bodh-generation" content="7"',
    ):
        assert node in console, node
    # the export: none of those, the document embedded, the snapshot stamp, no path
    for node in ("data-action=", "data-verdicts=", 'id="log"', "<script>", 'id="stamp"', str(cfg.root)):
        assert node not in export, node
    embedded = re.search(r'<script type="application/json" id="bodh-document">(.*?)</script>', export, re.S)
    assert embedded and json.loads(embedded.group(1).replace("<\\/", "</")) == export_doc.to_dict()
    assert "open the console for live data" in export
    assert 'data-band="50"' in console and 'data-band="50"' not in export  # no server to ask
    # the app: the console without the page chrome, with the theme stamped
    assert 'data-theme="dark"' in app and 'data-mode="app"' in app
    for node in ("<header", "<nav", "<footer", 'class="skip"'):
        assert node not in app, node
    assert 'data-action="run"' in app and 'id="log"' in app
    # every mode: the six sections in order, each with its question
    for page in (console, export, app):
        positions = [page.index(f'id="{anchor}"') for anchor, _ in panels.SECTIONS]
        assert positions == sorted(positions)
        for _anchor, title in panels.SECTIONS:
            assert f">{title}</h2>" in page


def test_no_secret_path_or_bad_link_reaches_the_page(conn, cfg):
    (cfg.root / ".env").write_text(
        "BAZQUX_EMAIL=reader@example.test\nBAZQUX_PASSWORD=hunter2-sekrit\nANTHROPIC_API_KEY=sk-ant-very-secret\n",
        encoding="utf-8",
    )
    ids = seed_scored(
        conn,
        cfg,
        [dict(n=1, score=95, url="javascript:alert(1)"), dict(n=2, score=90, url="https://ok.test/a")],
    )
    store.add_feedback(conn, ids[0], "read")
    for mode in MODES:
        page = panels.render(document.build(cfg, conn, mode), mode)
        for secret in ("hunter2-sekrit", "sk-ant-very-secret", "reader@example.test"):
            assert secret not in page
        assert "javascript:" not in page and 'href="https://ok.test/a"' in page
        assert re.search(r'<(script|link)[^>]+(src|href)="https?://', page) is None  # no request to any host
    export = panels.render(document.build(cfg, conn, "export"), "export")
    assert str(cfg.root / cfg.data_dir) not in export


def test_budgets_on_a_small_world(conn, cfg):
    seeded(conn, cfg)
    for mode in MODES:
        page = panels.render(document.build(cfg, conn, mode), mode)
        assert len(page.encode("utf-8")) < 200_000
    script = templates.script()
    assert len(script.splitlines()) < 400 and "innerHTML" not in script.replace("tpl.innerHTML", "")
    assert len(templates.STYLESHEET.read_text(encoding="utf-8").splitlines()) < 600


def test_list_rows_pages_and_bands_and_the_more_control(conn, cfg):
    seed_scored(conn, cfg, [dict(n=i, score=50 + i, folder="News" if i % 2 else None) for i in range(1, 61)])
    now = datetime.now(timezone.utc)
    rows, total = document.list_rows(cfg, conn, now, 70, 0, 50)
    assert total == 41 and len(rows) == 41 and rows[0].score == 110 and rows[0].band == 90
    rows, total = document.list_rows(cfg, conn, now, 50, 50, 50)
    assert total == 60 and len(rows) == 10 and rows[-1].score == 51
    rows, _ = document.list_rows(cfg, conn, now, 70, 0, 5, folder="News")
    assert len(rows) == 5 and {r.folder for r in rows} == {"News"}
    assert rows[0].arrived_at and rows[0].arrived_at.startswith("2023-")
    doc = document.build(cfg, conn, "console")
    assert doc.list.total == 41 and len(doc.list.articles) == 41 and doc.list.bands == [90, 70, 50]
    page = panels.render(doc, "console")
    assert 'class="list-more' not in page  # everything fits
    monkeypatch_cap = document.LIST_CAP
    try:
        document.LIST_CAP = 10
        doc = document.build(cfg, conn, "console")
        page = panels.render(doc, "console")
        assert (
            len(doc.list.articles) == 10
            and "Showing 10 of 41." in page
            and '<button type="button" class="button quiet js-only" data-action="list_more"' in page
        )
        export = panels.render(document.build(cfg, conn, "export"), "export")
        assert "Showing 10 of 41." in export and "Show 50 more" not in export
    finally:
        document.LIST_CAP = monkeypatch_cap


def test_live_slice_and_the_state_while_running(conn, cfg, monkeypatch):
    seeded(conn, cfg)
    quiet = document.live(cfg, conn)
    assert (
        set(quiet) == {"as_of", "state"}
        and quiet["state"]["running"] is False
        and quiet["state"]["levers"] == ["run", "pause"]
    )
    monkeypatch.setattr(
        locks, "running_process", lambda data_dir: {"pid": 1, "kind": "run", "started": time.time() - 60}
    )
    store.kv_set(conn, store.PHASE_KEY, "judging 240 of 1,200")
    busy = document.live(cfg, conn)
    st = busy["state"]
    assert (
        st["running"]
        and st["progress_done"] == 240
        and st["progress_total"] == 1200
        and st["levers"] == ["stop"]
    )
    assert st["sentence"].startswith("Judging 240 of 1,200")
    assert busy["today"]["unread"] == 9 and [t["key"] for t in busy["today"]["tiles"]] == [
        "judged",
        "cut",
        "read",
        "spend",
    ]
    page = panels.render(document.build(cfg, conn), "console")
    assert 'role="meter"' in page and 'aria-valuenow="240"' in page and 'data-action="stop"' in page
    assert 'data-action="run" hidden' in page and 'data-action="stop">' in page


def test_attention_the_most_severe_wins(conn, cfg):
    seeded(conn, cfg)
    now = datetime.now(timezone.utc)
    assert document._attention(cfg, conn, now) is None
    for _ in range(80):  # the budget: $0.12 a record, well past the default
        store.record_cost(conn, None, "claude-haiku-5-5", "judge", {}, 0.12)
    budget = document._attention(cfg, conn, now)
    assert budget is not None and budget.key == "budget" and budget.level == "warning" and budget.dismissable
    failed = store.begin_run(conn, "run")
    store.finish_run(conn, failed, False, error="BazQux login failed: 403 Forbidden")
    signin = document._attention(cfg, conn, now)
    assert signin is not None and signin.key == "signin" and signin.level == "critical"
    store.kv_set(conn, store.SAFETY_KEY, "77")
    safety = document._attention(cfg, conn, now)
    assert safety is not None and safety.key == "safety" and "77" in safety.text
    page = panels.render(document.build(cfg, conn), "console")
    assert page.count('class="banner') == 1 and 'data-key="safety"' in page


def test_export_writes_the_page_and_the_document(conn, cfg):
    seeded(conn, cfg)
    path = dashboard.render(cfg, conn)
    assert path.name == "index.html" and 'data-mode="export"' in path.read_text(encoding="utf-8")
    data = json.loads((path.parent / "data.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["list"]["total"] == 4 and data["footer"]["exported_text"]


# -- the console ----------------------------------------------------------------------------

FAKE = """
import sys
args = sys.argv[1:]
print("fake bodh", *args)
"""


def _serve(cfg, conn, tmp_path) -> tuple[Console, str]:
    script = tmp_path / "fake_bodh.py"
    script.write_text(FAKE, encoding="utf-8")
    console = Console(cfg, conn, command=[sys.executable, str(script)], host="127.0.0.1", port=0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        console.port = sock.getsockname()[1]
    threading.Thread(target=console.serve, daemon=True).start()
    base = f"http://127.0.0.1:{console.port}"
    for _ in range(50):
        try:
            with urllib.request.urlopen(base + "/api/tick", timeout=5):
                break
        except OSError:
            time.sleep(0.1)
    return console, base


def _get(url: str, **headers: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-Bodh": "console"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_console_serves_the_page_the_document_the_ticks_the_rows_and_a_verdict(conn, cfg, tmp_path):
    (cfg.root / ".env").write_text(
        "BAZQUX_EMAIL=a@b.c\nBAZQUX_PASSWORD=x\nANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8"
    )
    ids = seeded(conn, cfg)
    console, base = _serve(cfg, conn, tmp_path)
    code, page = _get(base + "/")
    assert code == 200 and b'data-mode="console"' in page and b'name="bodh-generation" content="1"' in page
    code, app = _get(base + "/?mode=app&theme=light")
    assert b'data-mode="app"' in app and b'data-theme="light"' in app and b"<nav" not in app
    code, body = _get(base + "/api/dashboard")
    doc = json.loads(body)
    assert doc["schema"] == 1 and doc["list"]["verdicts"] and doc["machinery"]["log"] == []
    assert json.loads(_get(base + "/api/dashboard?mode=export")[1])["list"]["verdicts"] == []
    tick = json.loads(_get(base + "/api/tick")[1])
    assert (
        tick["generation"] == 1
        and tick["state"]["running"] is False
        and "today" not in tick
        and "child" not in tick
    )
    rows = json.loads(_get(base + "/list?band=50&offset=2")[1])
    assert (
        rows["total"] == 6
        and rows["count"] == 4
        and rows["rows"].count('class="list-row"') == 4
        and "data-verdicts=" in rows["rows"]
    )
    # the plans, with their numbers
    plan = json.loads(_get(base + "/api/plan?action=undo")[1])
    assert (
        plan["ok"]
        and "Undo run 1" in plan["sentence"]
        and "3 articles" in plan["sentence"]
        and plan["button"] == "Undo run 1"
    )
    plan = json.loads(_get(base + "/api/plan?action=autocut_on")[1])
    assert plan["ok"] and plan["button"] == "Cut from the next run"
    assert not json.loads(_get(base + "/api/plan?action=weekly")[1])["ok"]
    # a verdict recorded in process, which moves the generation and the document
    res = _post(base + "/api/mark", {"id": ids[11], "kind": "meh"})
    assert res["ok"] and res["message"].startswith("recorded meh")
    assert store.verdicts(conn, store.GRADES)[ids[11]] == "meh"
    assert json.loads(_get(base + "/api/tick")[1])["generation"] == 2
    assert not _post(base + "/api/mark", {"id": ids[0], "kind": "lovely"})["ok"]
    # an action moves it too, and the page carries the new generation
    assert _post(base + "/api/action", {"action": "doctor"})["ok"]
    assert b'content="3"' in _get(base + "/")[1]
    # the events: the first tick arrives at once, as an SSE event
    stream = http.client.HTTPConnection("127.0.0.1", console.port, timeout=10)
    try:
        stream.request("GET", "/api/events")
        r = stream.getresponse()
        assert r.headers["Content-Type"].startswith("text/event-stream")
        assert r.readline().decode() == "event: tick\n"
        data = json.loads(r.readline().decode()[len("data: ") :])
        assert data["generation"] == 3 and data["schema"] == 1
        r.close()
    finally:
        stream.close()
    # a foreign Host gets nothing, as before
    req = urllib.request.Request(base + "/api/tick", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403
    exc.value.close()


def test_console_ticks_show_a_running_child_and_notice_a_run_ending(conn, cfg, tmp_path, monkeypatch):
    seeded(conn, cfg)
    console, base = _serve(cfg, conn, tmp_path)
    assert json.loads(_get(base + "/api/tick")[1])["generation"] == 1
    monkeypatch.setattr(
        locks, "running_process", lambda data_dir: {"pid": 1, "kind": "run", "started": time.time()}
    )
    console._tick_cache = None
    tick = json.loads(_get(base + "/api/tick")[1])
    assert (
        tick["state"]["running"] and tick["today"]["unread"] == 9 and tick["generation"] == 2
    )  # a run started: refetch
    monkeypatch.setattr(locks, "running_process", lambda data_dir: None)
    console._tick_cache = None
    tick = json.loads(_get(base + "/api/tick")[1])
    assert not tick["state"]["running"] and tick["generation"] == 3  # and ended
    assert _post(base + "/api/action", {"action": "weekly"})["ok"]
    for _ in range(50):
        tick = json.loads(_get(base + "/api/tick")[1])
        if "child" in tick and not tick["child"]["running"]:
            break
        time.sleep(0.1)
    assert (
        tick["child"]["args"] == ["weekly"]
        and tick["child"]["code"] == 0
        and "fake bodh weekly" in tick["child"]["output"]
    )
