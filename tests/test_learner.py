"""The learner model: its git log, the weekly rewrite's guardrails."""

from __future__ import annotations

import subprocess

from bodh import learner


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_git_commit_stays_inside_profile_dir(tmp_path):
    """profile/ inside a larger checkout with no repository of its own used to stage and
    commit the parent's files; now it gets a repository of its own and commits only itself."""
    parent = tmp_path
    _git(parent, "init", "-q")
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    _git(parent, "add", "-A")
    _git(parent, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "parent")
    head = _git(parent, "rev-parse", "HEAD")
    (parent / "uncommitted.txt").write_text("not staged\n", encoding="utf-8")
    profile = parent / "profile"
    profile.mkdir()
    (profile / "learner.md").write_text("## What the student has been learning\nx\n", encoding="utf-8")
    assert learner.git_commit(profile, "learner v1: test")
    assert (profile / ".git").is_dir()
    assert _git(parent, "rev-parse", "HEAD") == head                      # the parent did not move
    assert "uncommitted.txt" in _git(parent, "status", "--porcelain")      # and was not staged
    assert _git(profile, "log", "--oneline").count("\n") == 0 and "learner v1" in _git(profile, "log", "--oneline")
    assert _git(profile, "ls-files") == "learner.md"
    # nothing changed: still fine, no warning worth a failure
    assert learner.git_commit(profile, "learner v1 again")


def test_git_commit_without_identity_logs_and_returns_false(tmp_path, monkeypatch, caplog):
    import logging

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "learner.md").write_text("x\n", encoding="utf-8")
    # no identity anywhere: the fallback identity is used and the commit lands
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-system-gitconfig"))
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    assert learner.git_commit(profile, "v1")
    assert "bodh@localhost" in _git(profile, "log", "-1", "--format=%ae")
    # git itself failing is a warning with git's words, and False
    monkeypatch.setattr(learner, "GIT_IDENTITY", ["-c", "core.hooksPath=/nonexistent", "-c", "user.name=", "-c", "user.email="])
    (profile / "learner.md").write_text("y\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        ok = learner.git_commit(profile, "v2")
    assert not ok and "git commit" in caplog.text


def test_gate_check_never_writes_the_learner_file(cfg, conn):
    """The gate used to write the proposed text to learner.md and rely on a finally to put the
    old one back; a kill in between made the ungated text the live model."""
    from bodh.teacher import TeacherError

    class ExplodingLLM:
        plain_json = False

        def judge(self, *a, **k):
            raise TeacherError("model down")

        def request_params(self, *a, **k):
            return {}

    from conftest import seed_scored

    ids = seed_scored(conn, cfg, [dict(n=1, score=90)])
    from bodh import store

    store.add_feedback(conn, ids[0], "star")
    path = cfg.learner_path
    before, mtime = path.read_text(encoding="utf-8"), path.stat().st_mtime_ns
    import pytest

    with pytest.raises(TeacherError):
        learner.gate_check(cfg, ExplodingLLM(), conn, None, "## What the student has been learning\nproposed\n")  # type: ignore[arg-type]
    assert path.read_text(encoding="utf-8") == before and path.stat().st_mtime_ns == mtime


def test_gate_judges_with_the_proposed_model(cfg, conn):
    """The teacher in the gate sees the proposed text, not the file."""
    seen = {}

    class RecordingLLM:
        plain_json = False

        def judge(self, model, system_text, user_text, run_id, purpose):
            seen["system"] = system_text
            return []

        def request_params(self, *a, **k):
            return {}

    from conftest import seed_scored

    from bodh import store

    ids = seed_scored(conn, cfg, [dict(n=1, score=90)])
    store.add_feedback(conn, ids[0], "star")
    result = learner.gate_check(cfg, RecordingLLM(), conn, None, "## What the student has been learning\nPROPOSED TEXT\n")  # type: ignore[arg-type]
    assert result["passed"] and "PROPOSED TEXT" in seen["system"]
    assert "# Learner model\n## What the student has been learning\nnothing" not in seen["system"]


def test_repeats_seed():
    seed = "# Seed\n\nStudent: a marine biologist who reads to decide where the lab's next decade goes.\n\nAlready knows well: coral reef ecology.\n"
    assert learner.repeats_seed("## What\n# Seed\n", seed)
    assert learner.repeats_seed("## What\nStudent: a marine biologist who reads to decide where the lab's next decade goes.\n", seed)
    assert not learner.repeats_seed("## What\nThe student already knows well that reefs are in trouble.\n", seed)
    assert not learner.repeats_seed("## What\nAlready knows well: coral reef ecology.\n", seed)  # a short line is not proof


def test_weekly_rewrite_rejects_seed_repeat(cfg, conn):
    from conftest import seed_scored

    from bodh import store

    (cfg.root / "profile" / "seed.md").write_text(
        "# Seed\n\nStudent: a marine biologist who reads to decide where the lab's next decade goes.\n", encoding="utf-8")
    ids = seed_scored(conn, cfg, [dict(n=i, score=90) for i in range(1, 7)])
    for i in ids:
        store.add_feedback(conn, i, "star")

    class CopyingLLM:
        def complete(self, *a, **k):
            return ("## What the student has been learning\nStudent: a marine biologist who reads to decide where the "
                    "lab's next decade goes.\n\n## Expansion topics for next week\n- x\n")

    result = learner.weekly_rewrite(cfg, CopyingLLM(), conn, None)  # type: ignore[arg-type]
    assert not result["applied"] and "repeated the seed" in result["reason"]
