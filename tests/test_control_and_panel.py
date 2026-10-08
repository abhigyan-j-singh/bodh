"""The whole-system status, the levers (stop, enforce, schedule) and the control room server,
driven with a stand-in `triage` command so nothing real runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from conftest import seed_scored

from triage import control, ops, store
from triage.panel import Panel

FAKE = '''
import sys, time
args = sys.argv[1:]
if args[:1] == ["run"]:
    print("run starting", flush=True)
    time.sleep(float(sys.argv[-1]) if args[-1].replace(".", "").isdigit() else 0.2)
    print("unread 10 · judged 3", flush=True)
elif args[:1] == ["undo"]:
    print("restored 3 articles from run 7")
elif args[:1] == ["prune"]:
    print("would cut 5 of 12; boundary 61" if "--dry-run" in args else "cut 5")
elif args[:1] == ["sleepy"]:
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        print("interrupted", flush=True); sys.exit(130)
else:
    print("fake triage", *args)
'''


def _fake_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_triage.py"
    script.write_text(FAKE, encoding="utf-8")
    return [sys.executable, str(script)]


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, payload: dict, header: bool = True) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if header:
        req.add_header("X-Triage", "panel")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_status_text_and_enforce_flip(conn, cfg):
    seed_scored(conn, cfg, [dict(n=1, score=90), dict(n=2, score=20)])
    run_id = store.begin_run(conn, "run")
    store.finish_run(conn, run_id, True, summary="unread 2 · judged 2")
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n  target_unread: 40\n", encoding="utf-8")
    cfg.cut.enforce = False
    st = control.status(cfg, conn)
    assert st["mode"] == "shadow" and st["running"] is None and st["pile"]["unread"] == 2
    assert st["jobs"]["run"]["installed"] is False and st["runs"][0]["summary"] == "unread 2 · judged 2"
    text = control.render_status_text(st)
    assert "idle" in text and "shadow" in text and "unread 2" in text
    assert "now true" in control.set_enforce(cfg.root, True)
    assert "enforce: true" in (cfg.root / "config.yaml").read_text(encoding="utf-8")
    assert "shadow" in control.set_enforce(cfg.root, False)
    (cfg.root / "config.yaml").write_text("cut:\n  target_unread: 40\n", encoding="utf-8")
    assert "no `enforce:`" in control.set_enforce(cfg.root, True)
    # launchd is not here: states say so instead of failing
    j = control.job_state("com.antriksh.triage")
    assert j["installed"] is False and j["state"] == "not installed"
    assert "not installed" in control.schedule("pause")


def test_running_process_and_stop(conn, cfg):
    data_dir = cfg.root / cfg.data_dir
    assert ops.running_process(data_dir) is None
    assert ops.stop_process(data_dir) == "nothing is running"
    # a child process holding the lock, as a blast or prune would
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys, time; sys.path.insert(0, {src!r}); from pathlib import Path; from triage.ops import run_lock\n"
        f"with run_lock(Path({str(data_dir)!r}), 'blast'):\n"
        "    print('locked', flush=True)\n"
        "    try:\n        time.sleep(30)\n    except KeyboardInterrupt:\n        pass\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
    time.sleep(0.2)
    running = ops.running_process(data_dir)
    assert running and running["pid"] == proc.pid and running["kind"] == "blast"
    st = control.status(cfg, conn)
    assert st["running"]["kind"] == "blast"
    msg = ops.stop_process(data_dir, wait=10)
    assert msg.startswith("stopped blast")
    proc.wait(timeout=10)
    assert ops.running_process(data_dir) is None and not (data_dir / "triage.pid").exists()


def test_panel_serves_status_actions_and_output(conn, cfg, tmp_path):
    seed_scored(conn, cfg, [dict(n=1, score=90)])
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n", encoding="utf-8")
    cfg.cut.enforce = False
    (cfg.root / cfg.dashboard_dir).mkdir(exist_ok=True)
    (cfg.root / cfg.dashboard_dir / "index.html").write_text("<p>dash</p>", encoding="utf-8")
    panel = Panel(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
    # pick a free port
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        panel.port = sock.getsockname()[1]
    threading.Thread(target=panel.serve, daemon=True).start()
    base = f"http://127.0.0.1:{panel.port}"
    for _ in range(50):
        try:
            _get(base + "/api/status")
            break
        except OSError:
            time.sleep(0.1)
    st = _get(base + "/api/status")
    assert st["mode"] == "shadow" and st["child"]["running"] is False and st["pile"]["unread"] == 1
    page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    assert "control room" in page and "/api/action" in page
    assert urllib.request.urlopen(base + "/dashboard/index.html", timeout=10).read() == b"<p>dash</p>"
    # a POST without our header is refused (another site's page cannot drive the panel)
    code, _ = _post(base + "/api/action", {"action": "undo"}, header=False)
    assert code == 403
    # a foreground action returns its output
    code, res = _post(base + "/api/action", {"action": "prune_dry", "keep": 25})
    assert code == 200 and res["ok"] and "boundary 61" in res["output"] and "--keep 25" in res["message"]
    code, res = _post(base + "/api/action", {"action": "undo"})
    assert res["ok"] and "restored 3" in res["output"]
    # a background action streams output and refuses a second one while it runs
    code, res = _post(base + "/api/action", {"action": "run"})
    assert res["ok"] and res["message"].startswith("started: triage run")
    code, res2 = _post(base + "/api/action", {"action": "run_shadow"})
    assert not res2["ok"] and "already running" in res2["message"]
    for _ in range(50):
        st = _get(base + "/api/status")
        if not st["child"]["running"]:
            break
        time.sleep(0.1)
    assert st["child"]["code"] == 0 and any("judged 3" in line for line in st["child"]["output"])
    # stop interrupts a background command
    panel.child.start(panel.command + ["sleepy"], cfg.root, panel.env)
    time.sleep(0.3)
    code, res = _post(base + "/api/action", {"action": "stop"})
    assert res["ok"] and res["message"] == "stopped"
    assert not panel.child.alive()
    # unknown actions and the enforce lever
    code, res = _post(base + "/api/action", {"action": "nope"})
    assert not res["ok"]
    code, res = _post(base + "/api/action", {"action": "enforce_on"})
    assert res["ok"] and cfg.cut.enforce is True and "enforce: true" in (cfg.root / "config.yaml").read_text()
    assert os.environ.get("TRIAGE_HOME") != str(cfg.root)  # the child env carries it, the test process does not


def test_panel_state_and_halt_without_launchd(conn, cfg, tmp_path):
    from triage.panel import panel_log

    st = control.panel_state(cfg)
    assert st["up"] is False and st["url"] == "http://127.0.0.1:8766" and st["note"] == ""
    panel_log(cfg.root / cfg.data_dir, "cannot listen on 127.0.0.1:8766: address in use")
    st = control.panel_state(cfg)
    assert "address in use" in st["note"]
    text = control.render_status_text(control.status(cfg, conn))
    assert "control room: not answering" in text and "address in use" in text
    # a live panel: panel.json points at it and the port answers
    panel = Panel(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        panel.port = sock.getsockname()[1]
    threading.Thread(target=panel.serve, daemon=True).start()
    for _ in range(50):
        if control.panel_state(cfg)["up"]:
            break
        time.sleep(0.1)
    st = control.panel_state(cfg)
    assert st["up"] and st["url"].endswith(str(panel.port)) and st["pid"] == os.getpid()
    assert "control room: up" in control.render_status_text(control.status(cfg, conn))
    # halt with no launchd and nothing running still reports cleanly
    notes = control.halt(cfg)
    assert any("not installed" in n for n in notes) and any("nothing is running" in n for n in notes)
    assert notes[-1].startswith("everything is stopped")
    assert not (cfg.root / cfg.data_dir / "panel.json").exists()
