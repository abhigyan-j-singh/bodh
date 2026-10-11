"""Phase 6 at the surface: the help in groups with examples on every command, completion, the
JSON every command prints with --json (one object, `schema` first, the documented keys), the
new commands (find, list, tags, undo --run, why and mark by URL, profile edit, init, serve,
schedule install, upgrade, version --check), the prune's dry run and confirmation text, the
root resolution that names `bodh init`, and the Python API behind them all."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import seed_scored
from fakes import FakeLLM, FakeReader, make_item
from test_cli import (
    _env,
    _fakes,
    _project,
    _secrets_out_of_the_environment,  # noqa: F401  # the autouse fixture: no key outlives a test
    invoke,
)

from bodh import api, installers, serve, store, upgrade
from bodh import init as initmod
from bodh.config import load_config, load_secrets
from bodh.errors import BodhError

GROUPS = ("Set up", "Every run", "Look", "Cut", "Teach", "Control", "Housekeeping")
VISIBLE = (
    "version", "init", "doctor", "keys", "models", "run", "catch-up", "serve", "status", "progress",
    "why", "find", "list", "prune", "undo", "clear-old", "mark", "steer", "profile", "tags", "eval",
    "stop", "schedule", "autocut", "console", "safety", "backup", "rebuild", "weekly", "report",
    "dashboard", "upgrade",
)  # fmt: skip
HIDDEN = ("score", "blast", "bankruptcy", "halt", "enforce", "panel", "label", "want", "meh")


def _loads(result):
    """The one object a --json command printed: valid JSON, `schema` first and 1."""
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert list(payload)[0] == "schema" and payload["schema"] == api.SCHEMA
    return payload


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- help and completion ----------------------------------------------------------------


def test_help_lists_every_group_and_examples():
    text = invoke(["--help"]).output
    positions = [text.index(f"\n  {g:<14}") for g in GROUPS]  # the group lines of the epilog
    assert positions == sorted(positions), "the groups come in the order they are used"
    for name in VISIBLE:
        assert f"  {name} " in text, name
        detail = invoke([name, "--help"]).output
        assert "Examples:" in detail and f"bodh {name}" in detail, name
        assert not any(ch in detail for ch in "│─╭╮╰╯┃━"), name  # plain text, Windows terminals included
    for name in HIDDEN:
        assert f"  {name} " not in text, name
    assert "--install-completion" in text and "docs/json.md" in text
    assert "Exit codes: 1 an error, 2 configuration" in text and "5 a run in progress, 130 stopped" in text


def test_completion_installs(monkeypatch):
    """Completion is on; the script comes for the shell typer detects (stubbed here, since the
    test's own shell differs per machine)."""
    import shellingham

    for shell, mark in (
        ("bash", "complete -o default -F _bodh_completion bodh"),
        ("zsh", "compdef"),
        ("fish", "complete --command bodh"),
    ):
        monkeypatch.setattr(shellingham, "detect_shell", lambda shell=shell: (shell, f"/bin/{shell}"))
        result = invoke(["--show-completion"])
        assert result.exit_code == 0 and mark in result.output, (shell, result.output)


def test_a_folder_with_no_project_names_init(tmp_path, monkeypatch):
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bodh.config.DEFAULT_HOME", str(tmp_path / "nowhere"))
    result = invoke(["status"])
    assert result.exit_code == 2 and "`bodh init` makes a new project" in result.output
    assert not (tmp_path / "data").exists()  # never a data folder where there is no config.yaml


# -- json ------------------------------------------------------------------------------------


def test_status_json_schema(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    payload = _loads(invoke(["status", "--json"]))
    st = payload["status"]
    for key, kind in (
        ("now", str), ("console", dict), ("mode", str), ("batch_mode", bool), ("jobs", dict),
        ("config", dict), ("pile", dict), ("cuts_pending", dict), ("spend", dict), ("today", dict),
        ("runs", list), ("batches", list), ("log", list),
    ):  # fmt: skip
        assert isinstance(st[key], kind), key
    assert _iso(st["now"]).tzinfo is not None
    assert st["mode"] == "scoring-only" and st["running"] is None
    assert set(st["jobs"]) == {"run", "weekly", "report", "console"}
    assert (
        st["config"]["keep"] == 40 and st["config"]["cutoff"] == 35
    )  # the glossary's names, not target/floor
    assert set(st["pile"]) == {
        "unread",
        "judged",
        "pending",
        "inflight",
        "too_old",
        "stand_in",
        "rejudge",
        "low_conf",
    }
    assert "panel" not in st and "target" not in st["config"] and "floor" not in st["config"]


def test_progress_json_schema(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nfolders:\n  Essays: {cutoff: 50}\n")
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    seed_scored(conn, cfg, [dict(n=1, score=80, folder="Essays"), dict(n=2, score=20, folder="Essays")])
    conn.close()
    payload = _loads(invoke(["progress", "--json"]))
    p = payload["progress"]
    assert _iso(p["now"]).tzinfo is not None and _iso(p["at"]).tzinfo is not None
    assert p["judge"] == "claude-sonnet-5-5" and "teacher" not in p
    assert p["unread"] == 2 and p["judged_unread"] == 2 and p["estimated_models"] == []
    folder = next(f for f in p["folders"] if f["folder"] == "Essays")
    assert folder["cutoff"] == 50 and folder["scored"] is True and "floor" not in folder
    for key in ("today", "week", "month", "all_time", "series", "runs", "batches", "log"):
        assert key in p, key
    # the live file carries the same object, schema beside the fields
    from bodh import progress as progressmod

    conn = store.connect(cfg.db_path)
    path = progressmod.write_live(cfg, conn)
    conn.close()
    live = json.loads(path.read_text(encoding="utf-8"))
    assert live["schema"] == 1 and live["judge"] == "claude-sonnet-5-5" and "log" not in live


def test_version_and_keys_json(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    _env(root)
    payload = _loads(invoke(["version", "--json"]))
    assert payload["version"] and payload["newest"] is None and payload["channel"] in ("checkout", "pip")
    payload = _loads(invoke(["keys", "--json"]))
    rows = {r["provider"]: r for r in payload["keys"]}
    assert rows["anthropic"]["set"] is True and rows["openai"]["set"] is False
    assert rows["openai-compatible"]["optional"] == ["OPENAI_COMPATIBLE_API_KEY"]
    assert "sk-ant" not in json.dumps(payload)


def test_run_json(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch, config="timezone: UTC\nmodels:\n  batch: false\n")
    _env(root)
    _fakes(root, monkeypatch, client=FakeReader([make_item(1, feed="Feed A", title="gold essay")]))
    payload = _loads(invoke(["run", "--json"]))
    r = payload["run"]
    assert r["ok"] is True and r["scored"] == 1 and r["new"] == 1 and "judged 1" in r["summary"]
    assert "scoring-only mode: nothing cut" in r["notes"]


# -- look: why, find, list, tags ---------------------------------------------------------


def _seed_three(root):
    cfg = load_config(root)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=88, title="Liquid cooling retrofits", concept="data centres", folder="Essays"),
            dict(n=2, score=72, title="Seagrass meadow recovery", concept="ecology", folder="Science"),
            dict(n=3, score=30, title="Ten cooling gadgets", concept="gadgets", folder="Essays"),
        ],
    )
    conn.execute("UPDATE scores SET lesson = 'why retrofits cost more' WHERE article_id = ?", (ids[0],))
    conn.execute("UPDATE scores SET lesson = 'cooling as a listicle' WHERE article_id = ?", (ids[2],))
    store.mark_cut(conn, [ids[2]], 1, "autocut")
    conn.commit()
    conn.close()
    return ids


def test_find_by_title_and_lesson_and_json(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    ids = _seed_three(root)
    result = invoke(["find", "cooling"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2 and "Liquid cooling retrofits" in lines[0] and "Ten cooling gadgets" in lines[1]
    assert "score-70" in lines[0] and "unread" in lines[0] and ids[0] in lines[0]
    assert "cut" in lines[1]
    result = invoke(["find", "retrofits cost"])  # the lesson, every word
    assert result.exit_code == 0 and "Liquid cooling" in result.output and "gadgets" not in result.output
    result = invoke(["find", "cooling", "--unread"])
    assert "gadgets" not in result.output and "Liquid" in result.output
    assert "nothing found" in invoke(["find", "unicorn"]).output
    payload = _loads(invoke(["find", "cooling", "--json"]))
    assert payload["query"] == "cooling" and [h["id"] for h in payload["hits"]] == [ids[0], ids[2]]
    hit = payload["hits"][0]
    assert (
        hit["band"] == "score-70" and hit["status"] == "unread" and hit["lesson"] == "why retrofits cost more"
    )
    assert set(hit) == {"id", "title", "url", "feed", "folder", "score", "band", "status", "lesson"}


def test_list_filters_and_json(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    ids = _seed_three(root)
    result = invoke(["list"])
    assert result.exit_code == 0 and "Liquid cooling" in result.output and "Seagrass" in result.output
    assert "gadgets" not in result.output  # cut, and under the band anyway
    assert result.output.index("Liquid") < result.output.index("Seagrass")  # best first
    assert "why retrofits cost more" in result.output and "(data centres)" in result.output
    result = invoke(["list", "--folder", "Science"])
    assert "Seagrass" in result.output and "Liquid" not in result.output
    result = invoke(["list", "--band", "90"])
    assert "nothing unread at 90 or above" in result.output
    payload = _loads(invoke(["list", "--band", "50", "--json"]))
    assert payload["band"] == 50.0 and [a["id"] for a in payload["articles"]] == [ids[0], ids[1]]
    row = payload["articles"][0]
    assert row["band"] == "score-70" and row["concept"] == "data centres" and _iso(row["judged_at"])


def test_why_json_and_mark_by_url(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    ids = _seed_three(root)
    _fakes(root, monkeypatch)
    payload = _loads(invoke(["why", "https://example.com/FeedA/1", "--json"]))
    a = payload["article"]
    assert a["id"] == ids[0] and a["judged"] and a["score"] == 88.0 and a["band"] == "score-70"
    assert a["lesson"] == "why retrofits cost more" and a["record"] == [] and a["cut_mode"] is None
    cut = _loads(invoke(["why", ids[2], "--json"]))["article"]
    assert cut["status"] == "cut" and cut["cut_mode"] == "autocut" and cut["cut_run_id"] == 1
    result = invoke(["mark", "https://example.com/FeedA/1", "great"])
    assert result.exit_code == 0 and "recorded great for: Liquid cooling" in result.output
    a = _loads(invoke(["why", ids[0], "--json"]))["article"]
    assert [m["kind"] for m in a["record"]] == ["great"] and _iso(a["record"][0]["at"])
    result = invoke(["mark", "https://example.com/nowhere/9", "great"])
    assert result.exit_code == 1 and "unknown article" in result.output
    result = invoke(["mark", "not-an-id", "great"])
    assert result.exit_code == 1 and "not an article id" in result.output


def test_tags_prints_vocabulary(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    result = invoke(["tags"])
    assert result.exit_code == 0, result.output
    for tag in (
        "score-90",
        "score-0",
        "cut",
        "rescue",
        "prune",
        "great",
        "bad",
        "basic",
        "heavy",
        "more",
        "less",
        "Must read",
    ):
        assert f"  {tag:<12}" in result.output, tag
    assert (
        result.output.index("bodh writes:") < result.output.index("score-90") < result.output.index("you add")
    )
    payload = _loads(invoke(["tags", "--json"]))
    by = {t["tag"]: t for t in payload["tags"]}
    assert by["score-70"] == {
        "tag": "score-70",
        "group": "band",
        "writer": "bodh",
        "meaning": "the judge's score is 70 or more",
    }
    assert (
        by["great"]["group"] == "grade" and by["basic"]["group"] == "level" and by["more"]["group"] == "steer"
    )
    assert by["Must read"]["group"] == "stream" and by["cut"]["writer"] == "bodh"


# -- cut: the prune's dry run and confirmation, undo by run --------------------------------


def test_prune_dry_run_shows_bands_folders_lessons_and_the_confirmation_names_the_run(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg, client, _ = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    specs = [dict(n=n, score=10 + 5 * n, folder="Essays" if n % 2 else "News") for n in range(1, 16)]
    seed_scored(conn, cfg, specs)  # scores 15 to 85
    conn.execute("UPDATE scores SET lesson = 'lesson ' || article_id")
    conn.commit()
    conn.close()
    result = invoke(["prune", "--keep", "2", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (
        "would cut 13 of 15 scored unread articles; boundary 80 (the lowest score that stays)"
        in result.output
    )
    assert "by band: score-70 2, score-50 4, score-30 4, score-0 3" in result.output
    assert "by folder: " in result.output and "Essays 7" in result.output and "News 6" in result.output
    assert result.output.count("lesson tag:") == 10  # the ten best, each with its lesson
    assert client.tagged == [] and client.read == []
    # the question states the number, the mode, that undo reverses it, and the run id it will create
    monkeypatch.setattr("bodh.cli._interactive", lambda: True)
    result = invoke(["prune", "--keep", "2"], input="n\n")
    assert result.exit_code == 0, result.output
    assert (
        "Cut these 13 (keep 2)? It marks them read in BazQux; `bodh undo` reverses it; this will be run 1"
        in result.output
    )
    assert client.read == []
    result = invoke(["prune", "--below", "30"], input="y\n")
    assert (
        result.exit_code == 0
        and "Cut these 3 (--below 30)?" in result.output
        and "cut 3; `bodh undo` reverses it (run 1)" in result.output
    )


def test_undo_by_run_id(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    cfg, client, _ = _fakes(root, monkeypatch)
    conn = store.connect(cfg.db_path)
    ids = seed_scored(conn, cfg, [dict(n=n, score=20) for n in range(1, 5)])
    first = store.begin_run(conn, "prune")
    store.mark_cut(conn, ids[:2], first, "prune")
    store.finish_run(conn, first, True, cut_prune=2)
    second = store.begin_run(conn, "prune")
    store.mark_cut(conn, ids[2:], second, "prune")
    store.finish_run(conn, second, True, cut_prune=2)
    conn.close()
    result = invoke(["undo", "--run", str(first)])
    assert result.exit_code == 0 and f"restored 2 articles from run {first}" in result.output
    assert sorted(client.unread_marked[0]) == sorted(ids[:2])
    result = invoke(["undo", "--run", str(first)])
    assert (
        result.exit_code == 0 and f"run {first} cut nothing (or its cut was undone already)" in result.output
    )
    result = invoke(["undo", "--run", "99"])
    assert result.exit_code == 1 and "no run 99" in result.output
    result = invoke(["undo"])  # the last cut: the second run
    assert result.exit_code == 0 and f"restored 2 articles from run {second}" in result.output
    assert "nothing to undo" in invoke(["undo"]).output


def test_clear_old_confirmation_names_the_run(tmp_path, monkeypatch):
    import time

    from test_cli import DatedReader

    root = _project(tmp_path, monkeypatch)
    item = make_item(1)
    reader = DatedReader(
        [item], timestamps={item.id: int((time.time() - 30 * 86400) * 1_000_000)}, labels={"Must read": []}
    )
    cfg, client, _ = _fakes(root, monkeypatch, reader)
    result = invoke(["clear-old", "--days", "14"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "1 unread articles older than 14 days" in result.output
    assert "Mark them read? It is reversible with `bodh undo`; this will be run 1" in result.output
    assert client.read == []


# -- teach: profile edit --------------------------------------------------------------------


def test_profile_edit_opens_the_editor_and_writes_what_changed(tmp_path, monkeypatch):
    from bodh.cli import _edit as real_edit

    root = _project(tmp_path, monkeypatch)
    seen = {}

    def edit(text):
        seen["before"] = text
        return text.replace("Student: test.", "Student: me, a tester.")

    monkeypatch.setattr("bodh.cli._edit", edit)
    result = invoke(["profile", "edit"])
    assert result.exit_code == 0 and "wrote" in result.output and "about.md" in result.output
    assert seen["before"].startswith("# About the student")
    assert "Student: me, a tester." in (root / "profile" / "about.md").read_text(encoding="utf-8")
    monkeypatch.setattr("bodh.cli._edit", lambda text: None)
    result = invoke(["profile", "edit"])
    assert result.exit_code == 0 and "unchanged" in result.output
    # the real round trip through $EDITOR: a one-line editor that appends a line
    editor = tmp_path / "editor.py"
    editor.write_text("import sys; open(sys.argv[1], 'a').write('\\nAppended.\\n')", encoding="utf-8")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    monkeypatch.delenv("VISUAL", raising=False)
    assert real_edit("one\n") == "one\n\nAppended.\n"


# -- init ------------------------------------------------------------------------------------


FLAGS = [
    "--bazqux-email", "reader@example.com", "--bazqux-password", "hunter2", "--key", "sk-ant-test-key-0123456789",
    "--who", "A tester.", "--knows", "Testing.", "--goal", "Breadth.", "--decisions", "Which fake to use.",
    "--never", "Listicles.", "--timezone", "Europe/Lisbon",
]  # fmt: skip


def _no_launchd(monkeypatch):
    monkeypatch.setattr("bodh.jobs.launchctl_available", lambda: False)


def test_init_wizard_scripted_answers_on_every_os(tmp_path, monkeypatch):
    """Every answer as a flag, the two live checks stubbed: the project is written, doctor passes,
    the secrets never show, and a second init on the same folder is refused."""
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    _no_launchd(monkeypatch)
    checks = []
    monkeypatch.setattr(
        initmod, "check_bazqux", lambda e, p: checks.append(("bazqux", e)) or "bazqux login: ok, 3 unread"
    )
    monkeypatch.setattr(
        initmod, "check_model", lambda pr, k, j: checks.append(("model", pr, j)) or f"{pr} ({j}): ok"
    )
    dest = tmp_path / "reading"
    result = invoke(["init", str(dest), *FLAGS, "--no-install-schedule"])
    assert result.exit_code == 0, result.output
    assert checks == [("bazqux", "reader@example.com"), ("model", "anthropic", "claude-sonnet-5-5")]
    assert (
        "bazqux login: ok, 3 unread" in result.output and "anthropic (claude-sonnet-5-5): ok" in result.output
    )
    assert "wrote" in result.output and "doctor: ok" in result.output and f"ready: {dest}" in result.output
    cfg = load_config(dest)
    assert (
        cfg.timezone == "Europe/Lisbon"
        and cfg.models.judge == "claude-sonnet-5-5"
        and not cfg.autocut.enabled
    )
    secrets = load_secrets(dest)
    assert secrets.value("bazqux_email") == "reader@example.com" and secrets.value(
        "anthropic_api_key"
    ).startswith("sk-ant")
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(dest / ".env").st_mode) == 0o600
    about = (dest / "profile" / "about.md").read_text(encoding="utf-8")
    for line in ("Student: A tester.", "Already knows well, at a practitioner level: Testing.", "Goal: Breadth.",
                 "Decisions this quarter: Which fake to use.", "Never worth attention: Listicles."):  # fmt: skip
        assert line in about, line
    assert not cfg.about_is_example() and (dest / "profile" / "learned.md").exists()
    assert (dest / "data").is_dir() and (dest / "dashboard").is_dir()
    result = invoke(["init", str(dest), *FLAGS, "--no-live", "--no-install-schedule"])
    assert result.exit_code == 1 and "already holds a config.yaml" in result.output


def test_init_asks_its_questions_when_nothing_is_given(tmp_path, monkeypatch):
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    _no_launchd(monkeypatch)
    monkeypatch.setattr("bodh.cli._interactive", lambda: True)
    dest = tmp_path / "mine"
    answers = "\n".join(
        [str(dest), "reader@example.com", "hunter2", "", "sk-ant-test-key-0123456789", "", "Me.", "Things.",
         "Breadth.", "Decisions.", "Noise.", "UTC", "n"]
    ) + "\n"  # fmt: skip
    from test_cli import runner

    from bodh.cli import app

    result = runner.invoke(app, ["init", "--no-live"], input=answers)  # the runner echoes typed answers
    assert result.exit_code == 0, result.output
    assert "Where does the project go?" in result.output and "BazQux password" in result.output
    assert "Schedule the jobs on this machine?" in result.output
    assert "hunter2" not in result.output and "sk-ant-test" not in result.output
    assert load_config(dest).timezone == "UTC"
    assert "Student: Me." in (dest / "profile" / "about.md").read_text(encoding="utf-8")


def test_init_with_another_provider_puts_every_role_on_the_judge(tmp_path, monkeypatch):
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    _no_launchd(monkeypatch)
    dest = tmp_path / "o"
    result = invoke(["init", str(dest), *FLAGS, "--no-live", "--no-install-schedule", "--provider", "openai"])
    assert result.exit_code == 1 and "name the judge for openai" in result.output
    result = invoke(
        [
            "init",
            str(dest),
            *FLAGS,
            "--no-live",
            "--no-install-schedule",
            "--provider",
            "openai",
            "--judge",
            "openai/gpt-5-mini",
        ]
    )
    assert result.exit_code == 0, result.output
    cfg = load_config(dest)
    assert (
        cfg.models.judge
        == cfg.models.reviewer
        == cfg.models.profiler
        == cfg.catch_up.model
        == "openai/gpt-5-mini"
    )
    assert (
        load_secrets(dest).value("openai_api_key").startswith("sk-ant")
    )  # the --key given, under OpenAI's name
    assert "openai key: set (judge, reviewer, profiler, quick judge)" in result.output
    result = invoke(["init", str(tmp_path / "p"), *FLAGS, "--no-live", "--provider", "nope"])
    assert result.exit_code == 1 and "unknown provider 'nope'" in result.output
    result = invoke(["init", str(tmp_path / "q"), *FLAGS[:-2], "--timezone", "Mars/Olympus", "--no-live"])
    assert result.exit_code == 2 and "unknown timezone" in result.output


def test_init_offers_the_schedule(tmp_path, monkeypatch):
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    _no_launchd(monkeypatch)
    calls = []
    monkeypatch.setattr(installers, "install", lambda root, **k: calls.append(root) or ["would schedule"])
    dest = tmp_path / "s"
    result = invoke(["init", str(dest), *FLAGS, "--no-live", "--install-schedule"])
    assert result.exit_code == 0 and calls == [dest] and "would schedule" in result.output


# -- serve and schedule install -------------------------------------------------------------


def test_serve_scheduler_runs_what_is_due(cfg, conn):
    spawned = []
    sched = serve.Scheduler(cfg, conn, every_minutes=30, spawn=lambda args: spawned.append(args[0]) or 0)
    monday = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc).timestamp()  # a Monday, past the report hour
    assert sched.tick(monday) == [("run", 0), ("report", 0)]  # the first tick: a run, and October's report
    assert sched.tick(monday + 600) == []  # ten minutes later: nothing
    assert sched.tick(monday + 1800) == [("run", 0)]
    sunday_early = datetime(2026, 10, 18, 6, 40, tzinfo=timezone.utc).timestamp()
    assert [j for j, _ in sched.tick(sunday_early)] == ["run"]  # before seven: no weekly yet
    sunday = datetime(2026, 10, 18, 7, 5, tzinfo=timezone.utc).timestamp()
    assert [j for j, _ in sched.tick(sunday)] == ["weekly"]  # 25 min on: no run yet, the weekly is due
    assert [j for j, _ in sched.tick(sunday + 1800)] == ["run"]  # the weekly ran this week already
    november = datetime(2026, 11, 1, 7, 45, tzinfo=timezone.utc).timestamp()
    assert [j for j, _ in sched.tick(november)] == ["run", "weekly", "report"]  # a Sunday and the first
    assert spawned.count("report") == 2 and spawned.count("weekly") == 2


def test_serve_runs_the_schedule_until_stopped(cfg, conn):
    import threading

    spawned = []
    stop = threading.Event()

    def spawn(args):
        spawned.append(args[0])
        stop.set()
        return 0

    serve.serve(cfg, conn, every_minutes=30, console=False, stop=stop, spawn=spawn, tick_seconds=0.01)
    assert spawned[0] == "run"


def test_serve_command_prints_where_and_calls_serve(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(serve, "serve", lambda cfg, conn, **k: seen.update(k))
    result = invoke(["serve", "--every", "15", "--no-console"])
    assert (
        result.exit_code == 0
        and "a run every 15 min" in result.output
        and "console" not in result.output.split("(")[0]
    )
    assert seen["every_minutes"] == 15 and seen["console"] is False


@pytest.mark.parametrize("kind", ["launchd", "systemd", "taskschd"])
def test_schedule_install_dry_run_changes_nothing(tmp_path, monkeypatch, kind):
    root = _project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    ran = []
    lines = installers.install(
        root, kind=kind, dry_run=True, runner=lambda a: ran.append(a) or (0, ""), home=home
    )
    assert ran == [] and not any(home.rglob("*")), "a dry run writes and runs nothing"
    text = "\n".join(lines)
    assert "would run" in text and sys.executable in text
    if kind == "launchd":
        assert "would write" in text and "bodh.run.plist" in text and "launchctl bootstrap" in text
    elif kind == "systemd":
        assert "bodh-run.timer" in text and "systemctl --user enable --now bodh-console.service" in text
    else:
        assert "schtasks /Create" in text and "ONLOGON" in text and "serve" in text
    result = invoke(["schedule", "install", "--kind", kind, "--dry-run"])
    assert result.exit_code == 0 and "would run" in result.output


def test_schedule_install_and_uninstall_launchd_and_systemd(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    ran = []
    runner = lambda a: ran.append(a) or (0, "")  # noqa: E731
    lines = installers.install(root, kind="launchd", runner=runner, home=home)
    agents = home / "Library" / "LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == sorted(
        f"{label}.plist" for label in installers.JOBS.values()
    )
    plist = plistlib.loads((agents / "bodh.run.plist").read_bytes())
    assert (
        plist["ProgramArguments"] == [sys.executable, "-m", "bodh.cli", "run"]
        and plist["StartInterval"] == 1800
    )
    assert plist["EnvironmentVariables"]["BODH_HOME"] == str(root) and plist["WorkingDirectory"] == str(root)
    weekly = plistlib.loads((agents / "bodh.weekly.plist").read_bytes())
    assert weekly["StartCalendarInterval"] == {"Weekday": 0, "Hour": 7, "Minute": 0}
    console = plistlib.loads((agents / "bodh.console.plist").read_bytes())
    assert console["KeepAlive"] is True and "--port" in console["ProgramArguments"]
    assert [a[:2] for a in ran].count(["launchctl", "bootstrap"]) == 4 and any(
        "loaded bodh.run" in line for line in lines
    )
    lines = installers.uninstall(root, kind="launchd", runner=runner, home=home)
    assert not list(agents.iterdir()) and any("removed" in line for line in lines)
    assert installers.uninstall(root, kind="launchd", runner=runner, home=home) == ["no bodh jobs installed"]
    # systemd: the units, then enable --now
    ran.clear()
    lines = installers.install(root, kind="systemd", runner=runner, home=home)
    units = home / ".config" / "systemd" / "user"
    assert (units / "bodh-run.timer").exists() and "OnUnitActiveSec=30min" in (
        units / "bodh-run.timer"
    ).read_text()
    service = (units / "bodh-run.service").read_text()
    assert (
        f"ExecStart={sys.executable} -m bodh.cli run" in service
        and f"Environment=BODH_HOME={root}" in service
    )
    assert "OnCalendar=Sun *-*-* 07:00:00" in (units / "bodh-weekly.timer").read_text()
    assert "Restart=always" in (units / "bodh-console.service").read_text()
    assert ["systemctl", "--user", "daemon-reload"] in ran and [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "bodh-run.timer",
    ] in ran
    lines = installers.uninstall(root, kind="systemd", runner=runner, home=home)
    assert not any(units.iterdir()) and any("disabled bodh-run.timer" in line for line in lines)
    # a failing command is a line, not a crash
    lines = installers.install(root, kind="taskschd", runner=lambda a: (1, "access denied"))
    assert any(line.startswith("failed (1): schtasks") and "access denied" in line for line in lines)
    with pytest.raises(BodhError, match="unknown scheduler"):
        installers.install(root, kind="cron", runner=runner, home=home)


# -- upgrade and version --check -----------------------------------------------------------


def test_upgrade_channel_detection(monkeypatch):
    assert upgrade.channel("/home/me/.local/pipx/venvs/bodh/bin/python").name == "pipx"
    assert upgrade.channel("/home/me/.local/share/uv/tools/bodh/bin/python").command == [
        "uv",
        "tool",
        "upgrade",
        "bodh",
    ]
    assert upgrade.channel("/opt/homebrew/Cellar/bodh/1.0.0/libexec/bin/python").name == "brew"
    assert upgrade.channel("/x/python", frozen=True).command is None
    here = upgrade.channel(sys.executable)
    assert here.name == "checkout" and here.command[:3] == ["git", "-C", str(upgrade.PACKAGE_DIR.parents[1])]
    monkeypatch.setattr(upgrade, "PACKAGE_DIR", Path("/usr/lib/python3/site-packages/bodh"))
    assert upgrade.channel("/usr/bin/python3").command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "bodh",
    ]


def test_version_check_and_upgrade_command(tmp_path, monkeypatch):
    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    monkeypatch.setattr(upgrade.httpx, "get", lambda url, **k: Response(200, {"info": {"version": "99.0.0"}}))
    result = invoke(["version", "--check"])
    assert (
        result.exit_code == 0
        and "newest on PyPI: 99.0.0 (`bodh upgrade` shows how to get it)" in result.output
    )
    monkeypatch.setattr(upgrade.httpx, "get", lambda url, **k: Response(200, {"info": {"version": "0.0.1"}}))
    assert "this is the newest" in invoke(["version", "--check"]).output
    monkeypatch.setattr(upgrade.httpx, "get", lambda url, **k: Response(404, {}))
    assert "newest on PyPI: unknown" in invoke(["version", "--check"]).output

    def down(url, **k):
        raise upgrade.httpx.ConnectError("down")

    monkeypatch.setattr(upgrade.httpx, "get", down)
    assert upgrade.newest_on_pypi() is None
    _project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        upgrade,
        "channel",
        lambda: upgrade.Channel("checkout", ["git", "-C", "/x", "pull", "--ff-only"], "a checkout"),
    )
    result = invoke(["upgrade"])
    assert result.exit_code == 0 and "a checkout" in result.output and "to upgrade: git -C" in result.output
    ran = []
    monkeypatch.setattr(
        upgrade.subprocess, "run", lambda args, check: ran.append(args) or type("R", (), {"returncode": 0})()
    )
    result = invoke(["upgrade", "--run"])
    assert (
        result.exit_code == 0
        and "upgraded; run `bodh doctor` next" in result.output
        and ran[0][:2] == ["git", "-C"]
    )
    monkeypatch.setattr(
        upgrade, "channel", lambda: upgrade.Channel("binary", None, "a one-file executable: download it")
    )
    result = invoke(["upgrade", "--run"])
    assert result.exit_code == 0 and "download it" in result.output and len(ran) == 1


# -- the python api --------------------------------------------------------------------------


def test_the_api_mirrors_the_commands(cfg, conn):
    ids = seed_scored(
        conn,
        cfg,
        [
            dict(n=1, score=88, title="Liquid cooling retrofits", folder="Essays"),
            dict(n=2, score=20, title="Junk"),
        ],
    )
    found = api.App(cfg=cfg, conn=conn, log_to_file=False)
    found.ctx._client = FakeReader()
    found.ctx._llm = FakeLLM(cfg, conn)
    assert [h.id for h in found.find("cooling")] == [ids[0]]
    assert [r.id for r in found.reading_list()] == [ids[0]] and found.reading_list(band=10)[1].id == ids[1]
    assert found.why("https://example.com/FeedA/1").title == "Liquid cooling retrofits"
    assert found.why("nope") is None and found.resolve("1") == ids[0]
    assert {t.group for t in found.tags()} == {"band", "cut", "command", "grade", "level", "steer", "stream"}
    plan = found.prune_plan(keep=1)
    assert (
        plan.cut == 1
        and plan.by_band == {"score-0": 1}
        and plan.best[0].title == "Junk"
        and plan.next_run_id == 1
    )
    n, _sel = found.prune(keep=1, confirmed=set(plan.cut_ids))
    assert n == 1 and found.undo() == (1, 1) and found.undo() == (None, 0)
    assert (
        found.mark(ids[0], "great").startswith("recorded great")
        and found.why(ids[0]).record[0]["kind"] == "great"
    )
    with pytest.raises(BodhError, match="the verdict must be one of"):
        found.mark(ids[0], "superb")
    assert found.status()["pile"]["unread"] == 2 and found.progress()["unread"] == 2
    assert found.keys()[0]["provider"] == "anthropic" and found.next_run_id() == 3
    found.close()
