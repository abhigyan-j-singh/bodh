"""The student profile: profile/about.md, which the student writes once, and
profile/learned.md, which the profiler rewrites every week from what the student
actually read, starred, rescued and cut. Guardrails: bounded movement, an eval gate
on the starred set, a git commit per version, and a one-command revert."""

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
    """`bodh want X` / `bodh meh X`: a dated line the teacher sees immediately."""
    return nudges(cfg, conn, [(kind, phrase)])[0]


def nudges(cfg: Config, conn: sqlite3.Connection, items: list[tuple[str, str]]) -> list[str]:
    """Several nudges (the `more` and `less` tags of one run, say) in one version."""
    text = cfg.read_learned()
    stamp = store.utcnow().strftime("%Y-%m-%d")
    lines = [f"- {stamp}: the student {'wants more on' if kind == 'want' else 'wants less of'} {phrase.strip()}"
             for kind, phrase in items]
    if "## Steers" not in text and "## Nudges" not in text:   # "Nudges": the heading before October 2026
        text = text.rstrip() + "\n\n## Steers\n"
    text = text.rstrip() + "\n" + "\n".join(lines) + "\n"
    reason = "; ".join(f"{kind} {phrase.strip()[:40]}" for kind, phrase in items)
    _write_version(cfg, conn, text, f"nudge: {reason}")
    return lines


def _write_version(cfg: Config, conn: sqlite3.Connection, text: str, reason: str) -> int:
    cfg.learned_path.parent.mkdir(parents=True, exist_ok=True)
    if not store.learner_versions(conn, 1) and cfg.learned_path.exists():
        store.save_learner_version(conn, cfg.read_learned(), "initial")
    cfg.learned_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    version = store.save_learner_version(conn, text, reason)
    git_commit(cfg.learned_path.parent, f"learned profile v{version}: {reason}")
    return version


GIT_IDENTITY = ["-c", "user.name=bodh", "-c", "user.email=bodh@localhost"]  # when git has none to use


def git_commit(repo_dir: Path, message: str) -> bool:
    """Commit the profile folder as a git repository of its own; never fail the run over it.
    The folder gets its own repository when it has none, even inside a larger checkout:
    staging from a subfolder of the parent repository would commit the parent's files."""
    try:
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "-A", "--", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
        identity = subprocess.run(["git", "config", "user.email"], cwd=repo_dir, capture_output=True, text=True)
        extra = [] if identity.returncode == 0 and identity.stdout.strip() else GIT_IDENTITY
        result = subprocess.run(["git", *extra, "commit", "-q", "-m", message], cwd=repo_dir,
                                capture_output=True, text=True)
        if result.returncode == 0 or "nothing to commit" in (result.stdout + result.stderr):
            return True
        log.warning("profile: git commit in %s failed: %s", repo_dir, (result.stderr or result.stdout).strip()[:300])
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("profile: git commit skipped (%s)", exc)
        return False


def revert(cfg: Config, conn: sqlite3.Connection) -> int | None:
    """Back one version: the previous learned profile is written and committed again."""
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
    """Rewrite profile/learned.md from the week's evidence; keep it only if the eval gate passes."""
    result = {"applied": False, "reason": "", "diff": "", "words": 0}
    since = store.days_ago_iso(7)
    positives = store.feedback_since(conn, store.POSITIVE_KINDS, since)
    negatives = store.feedback_since(conn, store.NEGATIVE_KINDS, since)
    levels = store.feedback_since(conn, store.LEVELS, since)
    steers = store.feedback_since(conn, store.STEERS, since)
    if len(positives) < 5:
        result["reason"] = f"only {len(positives)} positives this week; learned profile kept as is"
        return result

    current = cfg.read_learned()
    about = cfg.read_about()
    evidence = ["# Positives (starred, great, good, read, rescued)"]
    evidence += [_evidence_line(r) for r in positives[:120]]
    evidence += ["", "# Negatives (bad, meh, dismissed)"]
    evidence += [_evidence_line(r) for r in negatives[:120]]
    if levels:
        evidence += ["", "# Level missed (basic: too elementary; heavy: too dense)"]
        evidence += [_evidence_line(r) for r in levels[:40]]
    if steers:
        evidence += ["", "# Steers (more: the student asked for more of this; less: for less)"]
        evidence += [_evidence_line(r) for r in steers[:40]]
    user_text = (
        f"# About the student\n{about}\n\n# Current learned profile\n{current}\n\n# This week's evidence\n"
        + "\n".join(evidence)
    )
    new_text = llm.complete(cfg.models.profiler, load_prompt("profiler.v1", cfg.root), user_text, run_id, "learner_rewrite",
                            max_tokens=1500)
    new_text = _strip_fences(new_text)
    words = len(new_text.split())
    result["words"] = words
    if words > MAX_WORDS or "## Expansion topics" not in new_text:
        result["reason"] = f"rewrite rejected: {words} words or missing sections"
        return result
    if repeats_about(new_text, about):
        result["reason"] = "rewrite rejected: it repeated the student's own description"
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


def repeats_about(new_text: str, about: str) -> bool:
    """The rewrite may not carry the student's own description into the learned profile: its
    heading, or any full line of it (the prompt forbids both; a copy doubles the cached prefix
    for nothing)."""
    low = new_text.lower()
    if "# about the student" in low or "# seed" in low:
        return True
    return any(len(line) >= 40 and line in low for line in (ln.strip().lower() for ln in about.splitlines()))


repeats_seed = repeats_about   # the name before October 2026


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
    """With the proposed learned profile in place, no recently loved article (starred, `great`,
    rescued with `wrong`) may score below its folder's automatic floor. Up to 30 articles,
    one or two calls."""
    rows = conn.execute(
        f"""SELECT a.* FROM feedback f JOIN articles a ON a.id = f.article_id
            WHERE f.kind IN ({",".join("?" * len(store.LOVED_KINDS))}) AND a.text IS NOT NULL AND a.text != ''
            GROUP BY a.id ORDER BY MAX(f.at) DESC LIMIT ?""",
        (*store.LOVED_KINDS, GATE_SAMPLE),
    ).fetchall()
    if not rows:
        return {"passed": True, "detail": "no starred articles to check against", "checked": 0, "below": []}
    # the proposed profile is handed to the judge in memory: learned.md is not touched, so a
    # kill or an overlapping run during the check cannot leave the ungated text as the live model
    teacher = Teacher(cfg, llm, conn, context=concept_context(conn, cfg), expansion_topics=expansion_topics(new_learner),
                      learner_text=new_learner)
    below: list[str] = []
    for i in range(0, len(rows), cfg.judge.articles_per_call):
        batch = rows[i: i + cfg.judge.articles_per_call]
        verdicts = teacher.judge_batch(batch, run_id)
        for row in batch:
            v = verdicts.get(row["id"])
            if v is None:
                continue
            floor = cfg.cutoff_for(row["folder"]) or cfg.autocut.cutoff
            if compose(v, cfg, teacher.expansion_topics) < floor:
                below.append(row["title"][:80])
    passed = not below
    detail = "all starred articles stay above the floor" if passed else f"{len(below)} starred below the floor: " + "; ".join(below[:5])
    return {"passed": passed, "detail": detail, "checked": len(rows), "below": below}
