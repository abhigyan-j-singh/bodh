"""The whole-system status, the levers (stop, enforce, schedule) and the control room server,
driven with a stand-in `bodh` command so nothing real runs."""

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

from bodh import jobs, locks, store
from bodh.console import Console

FAKE = """
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
    print("fake bodh", *args)
"""


def _fake_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_bodh.py"
    script.write_text(FAKE, encoding="utf-8")
    return [sys.executable, str(script)]


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, payload: dict, header: bool = True) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if header:
        req.add_header("X-Bodh", "console")
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
    cfg.autocut.enabled = False
    st = jobs.status(cfg, conn)
    assert st["mode"] == "scoring-only" and st["running"] is None and st["pile"]["unread"] == 2
    assert st["jobs"]["run"]["installed"] is False and st["runs"][0]["summary"] == "unread 2 · judged 2"
    text = jobs.render_status_text(st)
    assert "idle" in text and "scoring-only" in text and "unread 2" in text
    ok, msg = jobs.set_enforce(cfg.root, True)
    assert ok and "now true" in msg
    assert "enforce: true" in (cfg.root / "config.yaml").read_text(encoding="utf-8")
    ok, msg = jobs.set_enforce(cfg.root, False)
    assert ok and "scoring-only" in msg
    # a file without the key, in either layout, gets the new one
    (cfg.root / "config.yaml").write_text("cut:\n  target_unread: 40\n", encoding="utf-8")
    ok, msg = jobs.set_enforce(cfg.root, True)
    assert ok and "autocut:\n  enabled: true" in (cfg.root / "config.yaml").read_text(encoding="utf-8")
    # launchd is not here: states say so instead of failing
    j = jobs.job_state("bodh.run")
    assert j["installed"] is False and j["state"] == "not installed"
    assert "not installed" in jobs.schedule("pause")


def test_running_process_and_stop(conn, cfg):
    data_dir = cfg.root / cfg.data_dir
    assert locks.running_process(data_dir) is None
    assert locks.stop_process(data_dir) == "nothing is running"
    # a child process holding the lock, as a blast or prune would
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys, time; sys.path.insert(0, {src!r}); from pathlib import Path; from bodh.locks import run_lock\n"
        f"with run_lock(Path({str(data_dir)!r}), 'blast'):\n"
        "    print('locked', flush=True)\n"
        "    try:\n        time.sleep(30)\n    except KeyboardInterrupt:\n        pass\n"
    )
    with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
        time.sleep(0.2)
        running = locks.running_process(data_dir)
        assert running and running["pid"] == proc.pid and running["kind"] == "blast"
        st = jobs.status(cfg, conn)
        assert st["running"]["kind"] == "blast"
        msg = locks.stop_process(data_dir, wait=10)
        assert msg.startswith("stopped blast")
        proc.wait(timeout=10)
    assert locks.running_process(data_dir) is None and not (data_dir / "bodh.pid").exists()


def test_console_serves_status_actions_and_output(conn, cfg, tmp_path):
    seed_scored(conn, cfg, [dict(n=1, score=90)])
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n", encoding="utf-8")
    cfg.autocut.enabled = False
    (cfg.root / cfg.dashboard_dir).mkdir(exist_ok=True)
    (cfg.root / cfg.dashboard_dir / "index.html").write_text("<p>dash</p>", encoding="utf-8")
    panel = Console(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
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
    assert st["mode"] == "scoring-only" and st["child"]["running"] is False and st["pile"]["unread"] == 1
    page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    assert "bodh console" in page and "/api/action" in page
    assert urllib.request.urlopen(base + "/dashboard/index.html", timeout=10).read() == b"<p>dash</p>"
    # a POST without our header is refused (another site's page cannot drive the console)
    code, _ = _post(base + "/api/action", {"action": "undo"}, header=False)
    assert code == 403
    # a foreground action returns its output
    code, res = _post(base + "/api/action", {"action": "prune_dry", "keep": 25})
    assert code == 200 and res["ok"] and "boundary 61" in res["output"] and "--keep 25" in res["message"]
    code, res = _post(base + "/api/action", {"action": "undo"})
    assert res["ok"] and "restored 3" in res["output"]
    # a background action streams output and refuses a second one while it runs
    code, res = _post(base + "/api/action", {"action": "run"})
    assert res["ok"] and res["message"].startswith("started: bodh run")
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
    assert res["ok"] and "enforce: true" in (cfg.root / "config.yaml").read_text()
    assert _get(base + "/api/status")["mode"] == "autocut"
    # config.yaml edited by hand shows on the next refresh (mtime resolution: make it clearly later)
    os.utime(cfg.root / "config.yaml", (time.time() + 5, time.time() + 5))
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n  target_unread: 25\n", encoding="utf-8")
    os.utime(cfg.root / "config.yaml", (time.time() + 10, time.time() + 10))
    st = _get(base + "/api/status")
    assert st["mode"] == "scoring-only" and st["config"]["target"] == 25
    assert os.environ.get("BODH_HOME") != str(cfg.root)  # the child env carries it, the test process does not


def test_console_state_and_stop_all_without_launchd(conn, cfg, tmp_path):
    from bodh.console import console_log

    st = jobs.console_state(cfg)
    assert st["up"] is False and st["url"] == "http://127.0.0.1:8766" and st["note"] == ""
    console_log(cfg.root / cfg.data_dir, "cannot listen on 127.0.0.1:8766: address in use")
    st = jobs.console_state(cfg)
    assert "address in use" in st["note"]
    text = jobs.render_status_text(jobs.status(cfg, conn))
    assert "console: not answering" in text and "address in use" in text
    # a live console: console.json points at it and the port answers
    panel = Console(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        panel.port = sock.getsockname()[1]
    threading.Thread(target=panel.serve, daemon=True).start()
    for _ in range(50):
        if jobs.console_state(cfg)["up"]:
            break
        time.sleep(0.1)
    st = jobs.console_state(cfg)
    assert st["up"] and st["url"].endswith(str(panel.port)) and st["pid"] == os.getpid()
    assert "console: up" in jobs.render_status_text(jobs.status(cfg, conn))
    # halt with no launchd and nothing running still reports cleanly
    notes = jobs.stop_all(cfg)
    assert any("not installed" in n for n in notes) and any("nothing is running" in n for n in notes)
    assert notes[-1].startswith("everything is stopped")
    assert not (cfg.root / cfg.data_dir / "console.json").exists()


def test_installer_covers_every_job():
    """install-launchd.sh must install every job `bodh status` reports on, each with a template;
    the control room once went missing from the loop and the page could never answer."""
    root = Path(__file__).resolve().parents[1]
    installer = (root / "install-launchd.sh").read_text(encoding="utf-8")
    loop = next(line for line in installer.splitlines() if line.startswith("for job in "))
    labels = [word.strip(";") for word in loop.split()]
    for label in jobs.JOBS.values():
        assert label in labels, f"{label} is not installed by install-launchd.sh"
        assert (root / "launchd" / f"{label}.plist").exists()
    assert subprocess.run(["bash", "-n", str(root / "install-launchd.sh")]).returncode == 0


def test_set_autocut_every_yaml_spelling(cfg):
    """Whatever spelling the file uses, the key under `autocut:` is the one edited, comments stay,
    a missing key is added under `autocut:`, an `enabled:` elsewhere is left alone, and a file in
    the layout before October 2026 (`cut: enforce:`) is edited under its own names."""
    from bodh.config import load_config
    from bodh.jobs import _edit_flag

    path = cfg.root / "config.yaml"
    for spelling in ("true", "True", "yes", "on", '"false"', "'no'", "false   # a comment"):
        path.write_text(
            f"timezone: UTC\n# enabled: not this one\nautocut:\n  max_per_run: 40\n  enabled: {spelling}\n"
            "  cutoff: 35\nsafety:\n  lowered_cutoff: 25\n",
            encoding="utf-8",
        )
        for on in (False, True):
            ok, msg = jobs.set_autocut(cfg.root, on)
            assert ok, (spelling, on, msg)
            text = path.read_text(encoding="utf-8")
            loaded = load_config(cfg.root)
            assert (
                loaded.autocut.enabled is on
                and loaded.autocut.max_per_run == 40
                and loaded.autocut.cutoff == 35
            )
            assert (
                "# enabled: not this one" in text and "timezone: UTC" in text and "lowered_cutoff: 25" in text
            )
            if "comment" in spelling:
                assert "# a comment" in text
    # a key under another section is never the one edited (text level: the loader forbids it)
    text = "other:\n  enabled: true\nautocut:\n  cutoff: 40\n"
    assert (
        _edit_flag(text, "autocut", "enabled", False)
        == "other:\n  enabled: true\nautocut:\n  enabled: false\n  cutoff: 40\n"
    )
    # no enabled line under autocut: added there, nothing else moves
    path.write_text(
        "autocut:\n  cutoff: 40\nfolders:\n  Comics: { protected: true }\nsafety:\n  lowered_cutoff: 25\n",
        encoding="utf-8",
    )
    ok, _ = jobs.set_autocut(cfg.root, True)
    assert ok and path.read_text(encoding="utf-8").startswith("autocut:\n  enabled: true\n  cutoff: 40\n")
    assert load_config(cfg.root).folders["Comics"].protected
    # no autocut section at all: one is appended
    path.write_text("timezone: UTC\n", encoding="utf-8")
    ok, _ = jobs.set_autocut(cfg.root, True)
    assert ok and load_config(cfg.root).autocut.enabled is True
    assert path.read_text(encoding="utf-8") == "timezone: UTC\n\nautocut:\n  enabled: true\n"
    # the old layout is edited in place, under the old names, so the file keeps loading as it did
    path.write_text("cut:\n  enforce: false   # kept\n  target_unread: 40\n", encoding="utf-8")
    ok, msg = jobs.set_autocut(cfg.root, True)
    assert ok and "cut.enforce is now true" in msg
    assert path.read_text(encoding="utf-8") == "cut:\n  enforce: true # kept\n  target_unread: 40\n"
    assert load_config(cfg.root).autocut.enabled is True
    assert jobs.set_enforce is jobs.set_autocut
    # a shape the line editor cannot handle is refused, and the file is untouched
    path.write_text("autocut: { enabled: false, cutoff: 40 }\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    ok, msg = jobs.set_autocut(cfg.root, True)
    assert not ok and "by hand" in msg and path.read_text(encoding="utf-8") == before


def test_console_autocut_lever_fails_closed(conn, cfg, tmp_path):
    """When the file cannot be edited the page keeps showing the file's truth, not the wish."""
    (cfg.root / "config.yaml").write_text("cut: { enforce: false }\n", encoding="utf-8")
    cfg.autocut.enabled = False
    panel = Console(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
    res = panel.run_action("enforce_on", {})
    assert not res["ok"] and panel.cfg.autocut.enabled is False
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n", encoding="utf-8")
    res = panel.run_action("enforce_on", {})
    assert res["ok"] and panel.cfg.autocut.enabled is True


def test_halt_stops_before_pausing(conn, cfg, monkeypatch):
    """Pausing launchd first sent SIGTERM to the running job with no handler: no run row, no
    heartbeat, a pid file left behind. The run is interrupted first, then the schedule."""
    order = []
    monkeypatch.setattr(
        locks, "stop_process", lambda data_dir, wait=20.0: order.append("stop") or "nothing is running"
    )
    monkeypatch.setattr(
        jobs,
        "schedule",
        lambda action, which="all", data_dir=None: order.append(f"{action} {which}") or "paused",
    )
    jobs.stop_all(cfg)
    assert order == ["stop", "pause all"]


def test_schedule_kick_refuses_while_running(conn, cfg):
    data_dir = cfg.root / cfg.data_dir
    with locks.run_lock(data_dir, "blast"):
        msg = jobs.schedule("kick", "run", data_dir)
    assert msg.startswith("not kicked") and "blast" in msg
    assert "not installed" in jobs.schedule("kick", "run", data_dir)  # idle: launchd (absent here) is asked
    assert "console is paused too" in jobs.schedule("pause", "all", data_dir)


def test_stray_processes_only_this_checkout(tmp_path):
    r"""The old pattern (`\.venv/bin/bodh|-m bodh\.cli`, unanchored) matched another checkout's
    control room and an editor whose arguments carried the path."""
    import re

    pattern = re.compile(jobs.stray_pattern(tmp_path))
    lines = {
        101: f"{tmp_path}/.venv/bin/bodh panel --port 8766",
        102: "/Users/other/bodh/.venv/bin/bodh run",
        103: f"vim {tmp_path}/.venv/bin/bodh",
        104: "/usr/bin/python3 -m bodh.cli run",
        105: f"{tmp_path}/.venv/bin/bodh",
        106: f"{tmp_path}/.venv/bin/python {tmp_path}/.venv/bin/bodh progress --watch",  # as the shebang runs it
        107: "/Users/other/bodh/.venv/bin/python /Users/other/bodh/.venv/bin/bodh panel",
    }
    assert {pid for pid, cmd in lines.items() if pattern.search(cmd)} == {101, 104, 105, 106}


def test_next_run_uses_last_timer_run(conn, cfg, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "job_state",
        lambda label: {
            "label": label,
            "installed": True,
            "loaded": True,
            "state": "waiting",
            "last_exit": 0,
            "pid": None,
            "mode": "agent",
            "plist": "x",
            "interval": 1800,
        },
    )
    run_id = store.begin_run(conn, "run")
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        (store.days_ago_iso(0).replace("+00:00", "+00:00"), run_id),
    )
    store.finish_run(conn, run_id, True, summary="run")
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        ((store.utcnow() - __import__("datetime").timedelta(minutes=20)).isoformat(), run_id),
    )
    prune_id = store.begin_run(conn, "prune")  # a prune just now must not reset the countdown
    store.finish_run(conn, prune_id, True, summary="prune")
    st = jobs.status(cfg, conn)
    assert st["next_run_in"] is not None and 500 < st["next_run_in"] < 700


def test_console_rejects_foreign_host_and_caches_status(conn, cfg, tmp_path):
    import socket

    seed_scored(conn, cfg, [dict(n=1, score=90)])
    (cfg.root / "config.yaml").write_text("cut:\n  enforce: false\n  target_unread: 33\n", encoding="utf-8")
    cfg.autocut.enabled = False
    panel = Console(cfg, conn, command=_fake_command(tmp_path), host="127.0.0.1", port=0)
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
    for host in ("evil.example.com", "evil.example.com:8766", "127.0.0.1.evil.example"):
        req = urllib.request.Request(base + "/api/status", headers={"Host": host})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError(host)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        req = urllib.request.Request(
            base + "/api/action",
            data=b"{}",
            method="POST",
            headers={"Host": host, "X-Bodh": "panel", "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError(host)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
    for host in ("127.0.0.1", "localhost:1234", "mac.tail1234.ts.net", "[::1]:80"):
        req = urllib.request.Request(base + "/api/status", headers={"Host": host})
        assert urllib.request.urlopen(req, timeout=10).status == 200, host
    assert (
        panel.host_allowed("[::1]")
        and not panel.host_allowed("")
        and not panel.host_allowed("ts.net.evil.com")
    )
    # the page carries the configured target as the prune default
    page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    assert "id='keep' type='number' value='33'" in page
    # status is served from a short cache; an action refreshes it
    first = _get(base + "/api/status")["now"]
    assert _get(base + "/api/status")["now"] == first
    _post(base + "/api/action", {"action": "undo"})
    assert _get(base + "/api/status")["now"] != first


def test_installer_rejects_unknown_mode_and_plists_parse():
    import plistlib

    root = Path(__file__).resolve().parents[1]
    res = subprocess.run(
        ["bash", str(root / "install-launchd.sh"), "--bogus"], capture_output=True, text=True, cwd=str(root)
    )
    assert res.returncode == 2 and "usage" in res.stdout
    for label in jobs.JOBS.values():
        with (root / "launchd" / f"{label}.plist").open("rb") as fh:
            plist = plistlib.load(fh)
        assert plist["Label"] == label
        if label.endswith(".console"):
            assert plist["KeepAlive"] is True and plist["ThrottleInterval"] == 60
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    for personal in (
        ".env",
        "/config.yaml",
        "data/",
        "profile/",
        "dashboard/",
        "/prompts/",
        ".coverage",
        ".mypy_cache/",
    ):
        assert personal in ignored, personal
