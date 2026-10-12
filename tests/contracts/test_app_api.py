"""The service contract: every route the Mac app calls, against the console with a token,
with a stand-in `bodh` command for the levers, the fake BazQux server for the live checks and
an in-memory keychain. The same run writes the JSON fixtures the Swift client's tests decode
(`tests/fixtures/api/*.json`, with BODH_UPDATE_FIXTURES=1), so a change on either side fails
one suite or the other. The last tests start `bodh serve --app` as a real process: the port
line, the token, the end on stdin's close and on SIGTERM."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_scored
from fakes import FakeBazQuxServer, make_item

from bodh import service, settings, store
from bodh.console import Console

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "api"
TOKEN = "f" * 64
FAKE = """
import sys, time
args = sys.argv[1:]
if args[:1] == ["run"]:
    print("run starting", flush=True)
    time.sleep(0.3)
    print("unread 10 · judged 3", flush=True)
elif args[:1] == ["undo"]:
    print("restored 3 articles from run 7")
elif args[:1] == ["prune"]:
    print("cut 5")
elif args[:1] == ["clear-old"]:
    print("marked 4 read")
elif args[:1] == ["safety"]:
    print("safety reset")
else:
    print("fake bodh", *args)
"""


class MemoryKeyring:
    """keyring's interface over a dict, so the keychain tests touch no real keychain."""

    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.items.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.items[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.items.pop((service_name, username), None)


@pytest.fixture
def memory_keyring(monkeypatch):
    import keyring

    ring = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_password", ring.get_password)
    monkeypatch.setattr(keyring, "set_password", ring.set_password)
    monkeypatch.setattr(keyring, "delete_password", ring.delete_password)
    return ring


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Client:
    """urllib with the token, JSON in and out, the status code kept."""

    def __init__(self, base: str, token: str | None = TOKEN) -> None:
        self.base, self.token = base, token

    def call(
        self, method: str, path: str, payload: Any = None, token: str | None = "default"
    ) -> tuple[int, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        tok = self.token if token == "default" else token
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8")
                return r.status, (
                    json.loads(body)
                    if r.headers.get("Content-Type", "").startswith("application/json")
                    else body
                )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, (json.loads(body) if body.startswith("{") else body)

    def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.call("GET", path, **kw)

    def post(self, path: str, payload: Any = None, **kw: Any) -> tuple[int, Any]:
        return self.call("POST", path, payload or {}, **kw)

    def put(self, path: str, payload: Any = None, **kw: Any) -> tuple[int, Any]:
        return self.call("PUT", path, payload or {}, **kw)

    def delete(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.call("DELETE", path, **kw)


@pytest.fixture
def served(cfg, conn, tmp_path):
    """A console with a token, in app mode, serving a seeded store with the stand-in command."""
    seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=92, folder="Essays", concept="reefs"),
            dict(n=2, score=75, folder="Essays"),
            dict(n=3, score=40, folder="News"),
            dict(n=4, score=12, folder="News"),
        ],
    )
    (cfg.root / "config.yaml").write_text(
        'autocut:\n  enabled: false   # scoring only\n  cutoff: 35\nfolders:\n  "News": { cutoff: 70 }\n',
        encoding="utf-8",
    )
    script = tmp_path / "fake_bodh.py"
    script.write_text(FAKE, encoding="utf-8")
    console = Console(
        cfg,
        conn,
        command=[sys.executable, str(script)],
        host="127.0.0.1",
        port=_free_port(),
        token=TOKEN,
        app_mode=True,
    )
    threading.Thread(target=console.serve, daemon=True).start()
    base = f"http://127.0.0.1:{console.port}"
    client = Client(base)
    for _ in range(100):
        try:
            client.get("/api/health")
            break
        except OSError:
            time.sleep(0.05)
    yield console, client
    if console.server is not None:
        console.server.shutdown()


# -- auth --------------------------------------------------------------------------


def test_every_request_needs_the_token(served):
    console, client = served
    assert client.get("/api/health", token=None)[0] == 401
    assert client.get("/api/health", token="wrong")[0] == 401
    assert client.get("/", token=None)[0] == 401
    assert client.post("/api/run", {"mode": "shadow"}, token=None)[0] == 401
    assert client.get("/api/health")[0] == 200
    # the page's own event source carries it as a query parameter
    req = urllib.request.Request(f"{client.base}/api/tick?token={TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
    # a POST with the token needs no X-Bodh header (the page's own protection without a token)
    code, body = client.post("/api/mark", {"id": "nope", "kind": "great"})
    assert code == 200 and body["ok"] is False and "not an article id" in body["message"]


# -- the reads ---------------------------------------------------------------------


def test_health_status_and_the_page(served):
    console, client = served
    code, health = client.get("/api/health")
    assert code == 200 and health["schema"] == 1 and health["engine"] == "bodh" and health["app_mode"] is True
    assert health["port"] == console.port and health["mode"] == "scoring-only" and health["config_ok"]
    assert health["setup_needed"] == ["reader", "key"]  # no secrets in this project; the profile is written
    assert health["uptime_s"] >= 0 and health["last_run"] is None and health["paused_until"] is None
    code, st = client.get("/api/status")
    assert code == 200 and st["pile"]["unread"] == 4 and st["attention"] == [] and st["paused_until"] is None
    assert st["serve"] is None  # no `bodh serve` wrote its state
    # the app's page: no chrome, one section, the token stamped for the page's own fetches
    code, page = client.get("/?section=list")
    assert (
        code == 200
        and 'data-mode="app"' in page
        and 'name="bodh-token"' in page
        and 'name="bodh-section"' in page
    )
    assert "<nav" not in page and 'id="list"' in page and 'id="feeds"' not in page
    code, page = client.get("/?mode=console")
    assert 'data-mode="console"' in page and 'id="feeds"' in page
    _fixture("health", health)
    _fixture("status", st)


def test_list_why_find_tags(served):
    console, client = served
    code, body = client.get("/api/list?band=70")
    assert code == 200 and [a["score"] for a in body["articles"]] == [92.0, 75.0]
    assert body["articles"][0]["band"] == "score-90" and body["articles"][0]["folder"] == "Essays"
    assert client.get("/api/list?band=70&folder=News")[1]["articles"] == []
    aid = body["articles"][0]["id"]
    code, why = client.get("/api/why/" + urllib.request.quote(aid, safe=""))
    assert (
        code == 200
        and why["article"]["id"] == aid
        and why["article"]["score"] == 92.0
        and why["article"]["judged"]
    )
    assert (
        client.get("/api/why?ref=" + urllib.request.quote(aid, safe=""))[1]["article"]["concept"] == "reefs"
    )
    assert client.get("/api/why/nothing-here")[0] == 404
    assert client.get("/api/why")[0] == 400
    code, found = client.get("/api/find?q=Article&unread=1")
    assert code == 200 and found["query"] == "Article" and found["unread"] is True and len(found["hits"]) == 4
    assert client.get("/api/find?q=")[1]["hits"] == []
    code, tags = client.get("/api/tags")
    assert code == 200 and {t["tag"] for t in tags["tags"]} >= {"score-90", "cut", "rescue", "great"}
    _fixture("list", body)
    _fixture("why", why)
    _fixture("find", found)
    _fixture("tags", tags)


def test_settings_schema_values_and_writes(served):
    console, client = served
    code, schema = client.get("/api/settings/schema")
    assert code == 200 and schema["version"] == settings.SCHEMA_VERSION
    keys = {s["key"] for s in schema["settings"]}
    assert {"autocut.cutoff", "models.judge", "judge.weights", "tags.bands", "folders", "timezone"} <= keys
    groups = {g["key"] for g in schema["groups"]}
    assert all(s["group"] in groups for s in schema["settings"])
    cutoff = next(s for s in schema["settings"] if s["key"] == "autocut.cutoff")
    assert (
        cutoff["control"] == "number"
        and cutoff["min"] == 0
        and cutoff["max"] == 100
        and cutoff["value"] == 35
    )
    assert cutoff["source"] == "config.yaml" and cutoff["help"]
    folders = next(s for s in schema["settings"] if s["key"] == "folders")
    assert folders["control"] == "folders" and [f["key"] for f in folders["fields"]] == [
        "protected",
        "cutoff",
        "judge",
    ]
    code, current = client.get("/api/settings")
    assert (
        code == 200
        and current["settings"]["autocut.cutoff"] == 35
        and current["sources"]["prune.keep"] == "default"
    )
    assert current["settings"]["folders"] == {"News": {"protected": False, "cutoff": 70, "judge": True}}
    # one change, by dotted key; the comment survives; the console reads the new file
    code, body = client.put("/api/settings", {"changes": {"autocut.cutoff": 40, "prune.keep": 25}})
    assert code == 200 and body["ok"] and body["changed"] == ["autocut.cutoff", "prune.keep"]
    text = (cfg_root(console) / "config.yaml").read_text(encoding="utf-8")
    assert "cutoff: 40" in text and "# scoring only" in text and "keep: 25" in text
    assert client.get("/api/status")[1]["config"]["keep"] == 25
    # the whole object: only the differences are written
    whole = client.get("/api/settings")[1]["settings"]
    whole["models.daily_budget_usd"] = 3.5
    code, body = client.put("/api/settings", {"settings": whole})
    assert (
        body["changed"] == ["models.daily_budget_usd"] and body["settings"]["models.daily_budget_usd"] == 3.5
    )
    # a refusal names the key and writes nothing
    before = (cfg_root(console) / "config.yaml").read_text(encoding="utf-8")
    code, body = client.put("/api/settings", {"changes": {"autocut.cutoff": 140}})
    assert code == 422 and body["ok"] is False and body["errors"][0]["key"] == "autocut.cutoff"
    code, body = client.put("/api/settings", {"changes": {"nope": 1}})
    assert code == 422 and body["errors"][0]["key"] == "nope"
    assert (cfg_root(console) / "config.yaml").read_text(encoding="utf-8") == before
    _fixture("settings_schema", schema)
    _fixture("settings", current)


def test_folders_profile_and_attention(served, monkeypatch):
    console, client = served
    code, body = client.get("/api/folders")
    assert code == 200 and body["live"] is False
    news = next(f for f in body["folders"] if f["name"] == "News")
    assert (
        news["cutoff"] == 70
        and news["in_config"]
        and news["in_account"] is None
        and news["articles_per_day"] > 0
    )
    essays = next(f for f in body["folders"] if f["name"] == "Essays")
    assert essays["cutoff"] is None and essays["cutoff_in_effect"] == 35 and not essays["in_config"]
    code, prof = client.get("/api/profile")
    assert code == 200 and prof["about"].startswith("# About the student") and prof["version"] is None
    assert [q["key"] for q in prof["questions"]] == ["who", "knows", "goal", "decisions", "never"]
    code, body = client.put(
        "/api/profile",
        {"answers": {"who": "A", "knows": "B", "goal": "C", "decisions": "D", "never": "E"}},
    )
    assert code == 200 and body["written"] == ["about"] and "Student: A" in body["about"]
    code, body = client.put("/api/profile", {"learned": "## What the student has been learning\nreefs\n"})
    assert code == 200 and body["written"] == ["learned"] and body["version"] == 2  # the initial copy is v1
    assert client.get("/api/profile/diff")[1]["version"] == 2
    assert client.put("/api/profile", {"answers": {"who": "A"}})[0] == 400
    assert client.put("/api/profile", {})[0] == 400
    # attention: a failed run shows, can be dismissed, and the next failure comes back
    run_id = store.begin_run(console.conn, "run")
    store.finish_run(console.conn, run_id, False, error="BazQuxAuthError: BazQux login failed: HTTP 401")
    console._tick_cache = None
    code, body = client.get("/api/attention")
    assert (
        code == 200
        and [a["key"] for a in body["attention"]] == ["signin"]
        and body["attention"][0]["fingerprint"] == str(run_id)
    )
    assert client.get("/api/status")[1]["attention"][0]["key"] == "signin"
    code, body = client.post("/api/attention/dismiss", {"key": "signin"})
    assert code == 200 and body["attention"] == []
    assert client.post("/api/attention/dismiss", {"key": "signin"})[0] == 404
    run_id = store.begin_run(console.conn, "run")
    store.finish_run(console.conn, run_id, False, error="ModelConfigError: model call rejected: HTTP 401")
    body = client.get("/api/attention")[1]
    assert [a["key"] for a in body["attention"]] == ["provider"] and "model" in body["attention"][0][
        "word"
    ].lower()
    _fixture("folders", client.get("/api/folders")[1])
    _fixture("profile", client.get("/api/profile")[1])
    _fixture("attention", body)


# -- the keys -------------------------------------------------------------------------


def test_keys_in_the_keychain_and_env_import(served, memory_keyring):
    console, client = served
    code, body = client.get("/api/keys")
    assert code == 200 and body["env_file"] is False and body["keychain_service"] == "bodh"
    anthropic = next(k for k in body["keys"] if k["provider"] == "anthropic")
    assert anthropic["set"] is False and anthropic["required"] is True
    assert next(k for k in body["keys"] if k["provider"] == "openai")["required"] is False
    code, body = client.put("/api/keys/anthropic", {"key": "sk-ant-test-123"})
    assert (
        code == 200
        and body["ok"]
        and memory_keyring.items[("bodh", "ANTHROPIC_API_KEY")] == "sk-ant-test-123"
    )
    anthropic = next(k for k in body["keys"] if k["provider"] == "anthropic")
    assert anthropic["set"] is True and anthropic["variables"][0]["source"] == "keychain"
    assert "sk-ant-test-123" not in json.dumps(body)  # never a value, never a prefix
    code, body = client.put("/api/keys/bazqux", {"email": "me@example.com", "password": "pw"})
    assert code == 200 and memory_keyring.items[("bodh", "BAZQUX_PASSWORD")] == "pw"
    assert "pw" not in json.dumps(body["keys"])
    assert client.get("/api/health")[1]["setup_needed"] == []
    assert client.put("/api/keys/anthropic", {"key": ""})[0] == 400
    assert client.put("/api/keys/nope", {"key": "x"})[0] == 404
    # .env import: the values move to the keychain, the file goes when asked
    (cfg_root(console) / ".env").write_text(
        "OPENAI_API_KEY=sk-oa-1\n# a comment\nGEMINI_API_KEY='g-1'\n", encoding="utf-8"
    )
    assert client.get("/api/keys")[1]["env_file"] is True
    code, body = client.post("/api/keys/import-env", {"delete": True})
    assert (
        code == 200
        and sorted(body["imported"]) == ["GEMINI_API_KEY", "OPENAI_API_KEY"]
        and body["env_deleted"]
    )
    assert memory_keyring.items[("bodh", "GEMINI_API_KEY")] == "g-1"
    assert client.post("/api/keys/import-env", {})[0] == 404
    code, body = client.delete("/api/keys/openai")
    assert code == 200 and body["removed"] == ["OPENAI_API_KEY: keychain"]
    assert ("bodh", "OPENAI_API_KEY") not in memory_keyring.items
    # .env as the place, when asked
    code, body = client.put("/api/keys/openrouter", {"key": "or-1", "where": "env"})
    assert code == 200 and "OPENROUTER_API_KEY=or-1" in (cfg_root(console) / ".env").read_text(
        encoding="utf-8"
    )
    _fixture("keys", client.get("/api/keys")[1])


# -- the live checks ----------------------------------------------------------------


def test_check_reader_and_provider(served, monkeypatch):
    console, client = served
    fake = FakeBazQuxServer(
        [make_item(1, feed="Feed A"), make_item(2, feed="Feed B")],
        folders={"Feed A": "Essays", "Feed B": "News"},
        labels={"Must read": set()},
    )
    console.service.reader_transport = fake.transport()
    code, body = client.post("/api/check/reader", {"email": fake.email, "password": fake.password})
    assert code == 200 and body["ok"] and body["feeds"] == 2 and body["folders"] == ["Essays", "News"]
    assert body["unread"] == 2 and body["must_read_stream_present"] is True and "signed in" in body["message"]
    code, body = client.post("/api/check/reader", {"email": fake.email, "password": "wrong"})
    assert code == 200 and body["ok"] is False and "wrong" not in body["message"]
    assert client.post("/api/check/reader", {})[1]["ok"] is False
    from bodh import init as initmod

    monkeypatch.setattr(initmod, "check_model", lambda provider, key, judge: f"{provider} ({judge}): ok")
    code, body = client.post("/api/check/provider", {"provider": "anthropic", "key": "sk-1"})
    assert code == 200 and body["ok"] and body["model"] == "claude-sonnet-5-5" and body["cents"] > 0
    code, body = client.post("/api/check/provider", {"provider": "openai", "key": "sk-2"})
    assert body["ok"] is False and "name a model" in body["message"]
    code, body = client.post(
        "/api/check/provider", {"provider": "openai", "key": "sk-2", "model": "openai/gpt-5-mini"}
    )
    assert body["ok"] and body["model"] == "openai/gpt-5-mini"

    def refuse(provider, key, judge):
        raise RuntimeError(f"HTTP 401 for key {key}")

    monkeypatch.setattr(initmod, "check_model", refuse)
    body = client.post("/api/check/provider", {"provider": "anthropic", "key": "sk-secret"})[1]
    assert body["ok"] is False and "sk-secret" not in body["message"] and "[secret]" in body["message"]
    assert client.post("/api/check/provider", {"provider": "nope"})[0] == 400
    _fixture(
        "check_reader",
        {
            "schema": 1,
            "ok": True,
            "message": "signed in: 2 feeds in 2 folders, 2 unread",
            "email": "reader@example.com",
            "feeds": 2,
            "folders": ["Essays", "News"],
            "unread": 2,
            "must_read_stream": "Must read",
            "must_read_stream_present": True,
        },
    )
    _fixture(
        "check_provider",
        {
            "schema": 1,
            "ok": True,
            "message": "anthropic (claude-sonnet-5-5): ok",
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "cents": 0.0065,
        },
    )


# -- the levers --------------------------------------------------------------------


def test_run_stop_pause_and_the_events(served):
    console, client = served
    # pause: the status says so, the scheduler runs nothing
    code, body = client.post("/api/pause", {"minutes": 60})
    assert code == 200 and body["paused_until"]
    assert client.get("/api/status")[1]["paused_until"] == body["paused_until"]
    from bodh import serve as servemod

    sched = servemod.Scheduler(console.cfg, console.conn, every_minutes=30, spawn=lambda a: 0)
    assert sched.due(time.time()) == []
    code, body = client.post("/api/pause", {"until": None})
    assert body["paused_until"] is None and sched.due(time.time())[0] == "run"
    assert client.post("/api/pause", {"until": "tomorrow"})[1]["paused_until"]
    assert client.post("/api/pause", {"until": "soon-ish"})[0] == 400
    client.post("/api/pause", {"until": None})
    # a run, through the stand-in command, with its output in the child; a second one is refused
    code, body = client.post("/api/run", {"mode": "shadow", "limit": 5})
    assert code == 200 and body["ok"] and body["message"] == "started: bodh run --shadow --limit 5"
    assert client.post("/api/run", {"mode": "normal"})[0] == 409
    assert client.post("/api/run", {"mode": "sideways"})[0] == 400
    for _ in range(60):
        st = client.get("/api/status")[1]
        if not st["child"]["running"]:
            break
        time.sleep(0.1)
    assert st["child"]["code"] == 0 and any("judged 3" in line for line in st["child"]["output"])
    code, body = client.post("/api/stop", {})
    assert code == 200 and body["ok"]
    # the named events: a settings change lands in the stream with an id
    before = console.service.seq
    client.put("/api/settings", {"changes": {"prune.keep": 30}})
    events = console.service.events_since(before)
    assert [e[1] for e in events] == ["settings"] and events[0][2]["changed"] == ["prune.keep"]
    frames = _read_events(client, seconds=1.5, since=before)  # a reconnect replays what it missed
    assert any(f.startswith("event: tick") for f in frames)
    assert any("event: settings" in f and f"id: {events[0][0]}" in f for f in frames)
    fresh = _read_events(client, seconds=1.0)  # a new stream starts now: the old event is not replayed
    assert not any("event: settings" in f for f in fresh)
    _fixture("events", "\n\n".join(frames[:3]) + "\n\n", ext="txt")


def test_prune_undo_clear_old_catch_up_steer_safety(served, monkeypatch):
    console, client = served
    code, body = client.post("/api/prune", {"keep": 2, "dry_run": True})
    assert code == 200 and body["ok"] and body["plan"]["cut"] == 2 and body["plan"]["unread_scored"] == 4
    assert body["sentence"].startswith("Cut 2 of 4") and body["button"] == "Cut 2"
    assert [r["score"] for r in body["plan"]["best"]] == [40.0, 12.0]
    code, body = client.post("/api/prune", {"keep": 2, "confirm": True})
    assert code == 200 and body["message"] == "started: bodh prune --yes --keep 2"
    for _ in range(60):
        if not console.child.alive():
            break
        time.sleep(0.1)
    code, body = client.post("/api/undo", {"run_id": 7})
    assert code == 200 and body["ok"] and "restored 3" in body["output"]
    code, body = client.post("/api/clear_old", {"days": 30, "dry_run": True})
    assert (
        code == 400 and body["ok"] is False and "Must read" in body["message"]
    )  # no reader sign-in: refused, not guessed
    code, body = client.post("/api/clear_old", {"days": 30, "confirm": True})
    assert code == 200 and body["message"] == "started: bodh clear-old --yes --days 30"
    for _ in range(60):
        if not console.child.alive():
            break
        time.sleep(0.1)
    code, body = client.post("/api/catch_up", {"dry_run": True})
    assert code == 200 and body["plan"]["pending"] == 0 and body["plan"]["judge"] == "claude-haiku-5-5"
    code, body = client.post("/api/steer", {"direction": "more", "phrase": "coral reefs"})
    assert code == 200 and body["ok"] and "coral reefs" in body["message"]
    assert client.post("/api/steer", {"direction": "more"})[0] == 400
    assert client.post("/api/steer", {"direction": "sideways", "phrase": "x"})[0] == 400
    code, body = client.post("/api/safety/reset", {})
    assert code == 200 and "safety reset" in body["output"]
    aid = client.get("/api/list?band=70")[1]["articles"][0]["id"]
    code, body = client.post("/api/mark", {"id": aid, "kind": "great"})
    assert code == 200 and body["ok"] and body["message"].startswith("recorded great")
    assert (
        client.get("/api/why/" + urllib.request.quote(aid, safe=""))[1]["article"]["record"][0]["kind"]
        == "great"
    )
    _fixture("prune_plan", client.post("/api/prune", {"keep": 2, "dry_run": True})[1])


# -- `bodh serve --app` as a process --------------------------------------------------


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    (root / "config.yaml").write_text("autocut:\n  enabled: false\n", encoding="utf-8")
    (root / "profile").mkdir()
    (root / "profile" / "about.md").write_text("# About the student\nStudent: test.\n", encoding="utf-8")
    (root / "profile" / "learned.md").write_text("## What the student has been learning\n", encoding="utf-8")
    (root / "data").mkdir()
    conn = store.connect(root / "data" / "bodh.db")
    service.set_paused_until(
        conn,
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        + __import__("datetime").timedelta(hours=1),
    )
    conn.close()
    return root


def _start(tmp_path: Path, monkeypatch) -> tuple[subprocess.Popen[str], dict[str, Any], Path]:
    root = _project(tmp_path)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("BODH_HOME", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bodh.cli",
            "serve",
            "--app",
            "--port",
            "0",
            "--token-file",
            str(token_file),
            "--home",
            str(root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    info = json.loads(line)
    assert info["port"] > 0 and info["pid"] == proc.pid and info["url"].startswith("http://127.0.0.1:")
    return proc, info, root


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and pipes")
def test_serve_app_announces_the_port_and_ends_when_stdin_closes(tmp_path, monkeypatch):
    proc, info, root = _start(tmp_path, monkeypatch)
    try:
        client = Client(info["url"])
        code, health = client.get("/api/health")
        assert code == 200 and health["app_mode"] and health["pid"] == proc.pid and health["paused_until"]
        assert client.get("/api/health", token=None)[0] == 401
        written = json.loads((root / "data" / "serve.json").read_text(encoding="utf-8"))
        assert written["port"] == info["port"] and written["pid"] == proc.pid
        st = client.get("/api/status")[1]
        assert (
            st["serve"]["pid"] == proc.pid and st["next_run_in"] is None and st["runs"] == []
        )  # paused: no run
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not (root / "data" / "serve.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and pipes")
def test_serve_app_ends_on_sigterm_and_refuses_without_a_token(tmp_path, monkeypatch):
    proc, info, root = _start(tmp_path, monkeypatch)
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert proc.returncode in (0, -signal.SIGTERM, 130)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    env.pop("BODH_HOME", None)
    res = subprocess.run(
        [sys.executable, "-m", "bodh.cli", "serve", "--app", "--port", "0", "--home", str(root)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert res.returncode == 2 and "--token-file" in res.stderr


# -- the fixtures the Swift client decodes -----------------------------------------------


def _fixture(name: str, body: Any, ext: str = "json") -> None:
    """Write the fixture when asked (BODH_UPDATE_FIXTURES=1); otherwise check that the one
    committed still has the shape served now (the same top-level keys), so the Swift tests
    decode what the engine really sends."""
    path = FIXTURES / f"{name}.{ext}"
    if os.environ.get("BODH_UPDATE_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        text = (
            body
            if isinstance(body, str)
            else json.dumps(_scrub_paths(body), indent=2, sort_keys=False, default=str) + "\n"
        )
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"missing fixture {path.name}: run the suite with BODH_UPDATE_FIXTURES=1"
    if ext != "json":
        return
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert set(committed) == set(body), f"{name}: the fixture's keys differ; run with BODH_UPDATE_FIXTURES=1"


def _scrub_paths(body: Any) -> Any:
    """Temporary paths out of the fixtures, so they are the same on every machine."""
    text = json.dumps(body, default=str)
    import re

    text = re.sub(r'"/[^"]*?/(home|project|data|dashboard)([^"]*)"', r'"/Users/you/bodh/\1\2"', text)
    text = re.sub(r'"/tmp/[^"]*"', '"/Users/you/bodh"', text)
    return json.loads(text)


def _read_events(client: Client, seconds: float, since: int | None = None) -> list[str]:
    req = urllib.request.Request(f"{client.base}/api/events?token={TOKEN}")
    if since is not None:
        req.add_header("Last-Event-ID", str(since))
    frames: list[str] = []
    deadline = time.time() + seconds
    with urllib.request.urlopen(req, timeout=seconds + 5) as r:
        buf = ""
        while time.time() < deadline:
            chunk = r.read1(4096).decode("utf-8") if hasattr(r, "read1") else r.read(1).decode("utf-8")
            if not chunk:
                break
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                frames.append(frame)
            if len(frames) >= 2 and any("event: settings" in f for f in frames):
                break
    return frames


def cfg_root(console: Console) -> Path:
    return Path(console.cfg.root)
