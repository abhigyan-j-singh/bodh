"""The learner model: a short seed the student writes once, and learner.md, which
the system rewrites every week from what the student actually read, starred,
rescued and cut. Guardrails: bounded movement, an eval gate on the starred set,
a git commit per version, and a one-command revert."""

from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

from . import store
from .config import Config
from .teacher import LLM, Teacher, compose, concept_context, load_prompt

log = logging.getLogger(__name__)

MAX_WORDS = 450
GATE_SAMPLE = 30


def expansion_topics(learner_text: str) -> list[str]:
    """The phrases under '## Expansion topics for next week', one per line."""
    section = _section(learner_text, "Expansion topics")
    topics = []
    for line in section.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line or line.startswith("("):
            continue
        phrase = re.split(r"[:;,.—–-]| because | to ", line, maxsplit=1)[0].strip().strip("*_`")
        if phrase:
            topics.append(phrase.lower())
    return topics[:3]


def _section(text: str, heading_prefix: str) -> str:
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith(heading_prefix.lower())
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def nudge(cfg: Config, conn: sqlite3.Connection, kind: str, phrase: str) -> str:
    """`triage want X` / `triage meh X`: a dated line the teacher sees immediately."""
    text = cfg.read_learner()
    stamp = store.utcnow().strftime("%Y-%m-%d")
    line = f"- {stamp}: the student {'wants more on' if kind == 'want' else 'wants less of'} {phrase.strip()}"
    if "## Nudges" in text:
        text = text.rstrip() + "\n" + line + "\n"
    else:
        text = text.rstrip() + "\n\n## Nudges\n" + line + "\n"
    _write_version(cfg, conn, text, f"nudge: {kind} {phrase.strip()}")
    return line


def _write_version(cfg: Config, conn: sqlite3.Connection, text: str, reason: str) -> int:
    cfg.learner_path.parent.mkdir(parents=True, exist_ok=True)
    if not store.learner_versions(conn, 1) and cfg.learner_path.exists():
        store.save_learner_version(conn, cfg.read_learner(), "initial")
    cfg.learner_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    version = store.save_learner_version(conn, text, reason)
    git_commit(cfg.learner_path.parent, f"learner v{version}: {reason}")
    return version


def git_commit(repo_dir: Path, message: str) -> bool:
    """Commit the profile folder if it is a git repository; never fail the run over it."""
    try:
        if not (repo_dir / ".git").exists() and not _in_git_worktree(repo_dir):
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
        result = subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, capture_output=True, text=True)
        return result.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log.warning("learner: git commit skipped (%s)", exc)
        return False


def _in_git_worktree(path: Path) -> bool:
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "true"


def revert(cfg: Config, conn: sqlite3.Connection) -> int | None:
    """Back one version: the previous learner text is written and committed again."""
    versions = store.learner_versions(conn, n=2)
    if len(versions) < 2:
        return None
    previous = versions[1]
    return _write_version(cfg, conn, previous["text"], f"revert to v{previous['version']}")


def diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "last week", "this week", lineterm="", n=1))


# -- the weekly rewrite ---------------------------------------------------------

def weekly_rewrite(cfg: Config, llm: LLM, conn: sqlite3.Connection, run_id: int | None,
                   dry_run: bool = False) -> dict:
    """Rewrite learner.md from the week's evidence; keep it only if the eval gate passes."""
    result = {"applied": False, "reason": "", "diff": "", "words": 0}
    since = store.days_ago_iso(7)
    positives = store.feedback_since(conn, ["star", "read", "rescue"], since)
    negatives = store.feedback_since(conn, ["prune", "dismiss", "meh"], since)
    if len(positives) < 5:
        result["reason"] = f"only {len(positives)} positives this week; learner model kept as is"
        return result

    current = cfg.read_learner()
    seed = cfg.read_seed()
    evidence = ["# Positives (read, starred, rescued)"]
    evidence += [_evidence_line(r) for r in positives[:120]]
    evidence += ["", "# Negatives (pruned, dismissed, meh)"]
    evidence += [_evidence_line(r) for r in negatives[:120]]
    user_text = (
        f"# Seed\n{seed}\n\n# Current learner model\n{current}\n\n# This week's evidence\n" + "\n".join(evidence)
    )
    new_text = llm.complete(cfg.models.learner, load_prompt("learner.v1", cfg.root), user_text, run_id, "learner_rewrite",
                            max_tokens=1500)
    new_text = _strip_fences(new_text)
    words = len(new_text.split())
    result["words"] = words
    if words > MAX_WORDS or "## Expansion topics" not in new_text:
        result["reason"] = f"rewrite rejected: {words} words or missing sections"
        return result
    for forbidden in ("# Seed", "already knows well"):
        if forbidden.lower() in new_text.lower() and forbidden == "# Seed":
            result["reason"] = "rewrite rejected: it repeated the seed"
            return result

    gate = gate_check(cfg, llm, conn, run_id, new_text)
    result["gate"] = gate
    if not gate["passed"]:
        result["reason"] = f"rewrite rejected by the eval gate: {gate['detail']}"
        return result

    result["diff"] = diff(current, new_text)
    if dry_run:
        result["reason"] = "dry run: rewrite passed the gate but was not written"
        return result
    _write_version(cfg, conn, new_text, "weekly rewrite")
    result["applied"] = True
    result["reason"] = "rewrite applied"
    return result


def _evidence_line(r: sqlite3.Row) -> str:
    score = f"{r['score']:.0f}" if r["score"] is not None else "?"
    return (f"- [{r['kind']}] {(r['title'] or '')[:90]} ({(r['feed_title'] or '?')[:30]}; score {score}; "
            f"concept: {r['concept'] or '?'}) — {r['lesson'] or ''}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def gate_check(cfg: Config, llm: LLM, conn: sqlite3.Connection, run_id: int | None, new_learner: str) -> dict:
    """With the proposed learner model in place, no recently starred article may score
    below its folder's automatic floor. Up to 30 starred articles, one or two calls."""
    rows = conn.execute(
        """SELECT a.* FROM feedback f JOIN articles a ON a.id = f.article_id
           WHERE f.kind = 'star' AND a.text IS NOT NULL AND a.text != ''
           ORDER BY f.at DESC LIMIT ?""",
        (GATE_SAMPLE,),
    ).fetchall()
    if not rows:
        return {"passed": True, "detail": "no starred articles to check against", "checked": 0, "below": []}
    original = cfg.read_learner()
    try:
        cfg.learner_path.write_text(new_learner + "\n", encoding="utf-8")
        teacher = Teacher(cfg, llm, conn, context=concept_context(conn, cfg), expansion_topics=expansion_topics(new_learner))
        below: list[str] = []
        for i in range(0, len(rows), cfg.scoring.batch_size):
            batch = rows[i: i + cfg.scoring.batch_size]
            verdicts = teacher.judge_batch(batch, run_id)
            for row in batch:
                v = verdicts.get(row["id"])
                if v is None:
                    continue
                floor = cfg.cut.floor_for(row["folder"]) or cfg.cut.default_floor
                if compose(v, cfg, teacher.expansion_topics) < floor:
                    below.append(row["title"][:80])
    finally:
        cfg.learner_path.write_text(original + "\n", encoding="utf-8")
    passed = not below
    detail = "all starred articles stay above the floor" if passed else f"{len(below)} starred below the floor: " + "; ".join(below[:5])
    return {"passed": passed, "detail": detail, "checked": len(rows), "below": below}
