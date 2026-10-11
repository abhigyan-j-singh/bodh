"""The learner model: its git log, the weekly rewrite's guardrails."""

from __future__ import annotations

import subprocess

from triage import learner


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
    assert "triage@localhost" in _git(profile, "log", "-1", "--format=%ae")
    # git itself failing is a warning with git's words, and False
    monkeypatch.setattr(learner, "GIT_IDENTITY", ["-c", "core.hooksPath=/nonexistent", "-c", "user.name=", "-c", "user.email="])
    (profile / "learner.md").write_text("y\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        ok = learner.git_commit(profile, "v2")
    assert not ok and "git commit" in caplog.text
