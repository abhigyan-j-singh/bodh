"""The evaluation set: sixty short articles with the band the example student should see them
in, judged by the configured judge, and the agreement that comes out. `bodh eval` runs it
(a few cents, capped); CI runs it weekly and when a prompt changes; the README quotes the
figure with its date. The set and the example student's profile live in the repository
(`evals/set.yaml`, `profile.example/`), not in the package."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .errors import BodhError
from .judgment import compose

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]  # a checkout; a wheel install passes --set
SET_PATH = REPO_ROOT / "evals" / "set.yaml"
EXAMPLE_PROFILE = REPO_ROOT / "profile.example"
MAX_WORDS = 200
EST_INPUT_TOKENS = 450  # per article, after the cached prefix
EST_OUTPUT_TOKENS = 65


class EvalSetError(BodhError):
    """The set does not have the shape the loader expects."""


def load_set(path: Path = SET_PATH, bands: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """The set, validated: unique integer ids, a title, a feed, a known band, a `why`, a text
    under MAX_WORDS words."""
    if not path.exists():
        raise EvalSetError(f"no evaluation set at {path}; pass --set with the path to evals/set.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise EvalSetError(f"{path}: expected a list of articles")
    known = set((bands or Config().tags.bands).values())
    seen: set[int] = set()
    out = []
    for i, item in enumerate(raw):
        where = f"{path}: article {i + 1}"
        if not isinstance(item, dict):
            raise EvalSetError(f"{where}: not a mapping")
        missing = [k for k in ("id", "title", "feed", "expected", "why", "text") if k not in item]
        if missing:
            raise EvalSetError(f"{where}: missing {', '.join(missing)}")
        if not isinstance(item["id"], int) or item["id"] in seen:
            raise EvalSetError(f"{where}: id must be an integer not used before")
        seen.add(item["id"])
        if item["expected"] not in known:
            raise EvalSetError(f"{where}: expected band {item['expected']!r} is not one of {sorted(known)}")
        words = len(str(item["text"]).split())
        if not str(item["title"]).strip() or not str(item["text"]).strip():
            raise EvalSetError(f"{where}: an empty title or text")
        if words > MAX_WORDS:
            raise EvalSetError(f"{where}: {words} words; keep an article under {MAX_WORDS}")
        out.append(dict(item))
    return out


def rows_for(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The articles as the judge's prompt reads them."""
    return [
        {
            "id": f"eval-{item['id']}",
            "title": str(item["title"]),
            "text": str(item["text"]).strip(),
            "url": "",
            "feed_title": str(item["feed"]),
            "folder": item.get("folder"),
            "author": "",
            "published": None,
        }
        for item in items
    ]


def estimate_usd(cfg: Config, n: int, model: str) -> float:
    p_in, p_out = cfg.models.price(model)
    return n * (EST_INPUT_TOKENS * p_in + EST_OUTPUT_TOKENS * p_out) / 1_000_000 * 1.2


def run_eval(
    ctx: Any,
    items: list[dict[str, Any]],
    *,
    model: str | None = None,
    cap_usd: float = 0.5,
    profile_dir: Path = EXAMPLE_PROFILE,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Judge the set for the example student with the configured judge (or `model`), under
    `cap_usd`; returns the agreement and every article's result."""
    from .judge import Judge

    cfg, conn = ctx.cfg, ctx.conn
    judge_model = model or cfg.models.judge
    if not cfg.models.has_price(judge_model):
        raise BodhError(f"no price for {judge_model} in models.prices_per_mtok; add it to config.yaml first")
    estimate = estimate_usd(cfg, len(items), judge_model)
    if estimate > cap_usd:
        raise BodhError(
            f"the set would cost about ${estimate:.2f} with {judge_model}, over the cap of ${cap_usd:.2f}"
        )
    about = (profile_dir / "about.md").read_text(encoding="utf-8").strip()
    learned = (profile_dir / "learned.md").read_text(encoding="utf-8").strip()
    cfg.models.judge = judge_model
    judge = Judge(
        cfg,
        ctx.llm,
        conn,
        context="(no reading history yet)",
        about_text=about,
        learned_text=learned,
        purpose="eval",
    )
    rows = rows_for(items)
    by_id = {r["id"]: item for r, item in zip(rows, items, strict=True)}
    spent_before = ctx.llm.spent_today()
    results: list[dict[str, Any]] = []
    size = cfg.judge.articles_per_call
    for start in range(0, len(rows), size):
        if ctx.llm.spent_today() - spent_before > cap_usd:
            log.warning("eval: the cap of $%.2f was reached after %d articles", cap_usd, len(results))
            break
        batch = rows[start : start + size]
        verdicts = judge.judge_batch(batch, None)  # type: ignore[arg-type]  # dicts carry the row's keys
        results.extend(_result(cfg, by_id[row["id"]], verdicts.get(row["id"])) for row in batch)
        if on_progress is not None:
            on_progress(len(results), len(rows))
    spent = ctx.llm.spent_today() - spent_before
    return summarise(results, cfg, judge_model, spent, len(rows))


def _result(cfg: Config, item: dict[str, Any], v: Any) -> dict[str, Any]:
    """One article's line: what the set expected, what the judge gave."""
    score = compose(v, cfg) if v is not None else None
    return {
        "id": item["id"],
        "title": item["title"],
        "expected": item["expected"],
        "got": cfg.tags.band_for(score) if score is not None else None,
        "score": score,
        "lesson": v.lesson if v is not None else "",
        "kind": v.kind if v is not None else "",
        "flags": list(v.flags) if v is not None else [],
        "why": item["why"],
    }


def _band_rank(cfg: Config) -> dict[str, int]:
    return {name: i for i, (_t, name) in enumerate(sorted(cfg.tags.bands.items()))}


def summarise(
    results: list[dict[str, Any]], cfg: Config, model: str, spent: float, total: int
) -> dict[str, Any]:
    rank = _band_rank(cfg)
    judged = [r for r in results if r["got"] is not None]
    exact = sum(1 for r in judged if r["got"] == r["expected"])
    near = sum(1 for r in judged if abs(rank[r["got"]] - rank[r["expected"]]) <= 1)
    per_band: dict[str, dict[str, Any]] = {}
    for _t, name in sorted(cfg.tags.bands.items(), reverse=True):
        rows = [r for r in judged if r["expected"] == name]
        per_band[name] = {
            "n": len(rows),
            "exact": sum(1 for r in rows if r["got"] == name),
            "mean_score": round(sum(r["score"] for r in rows) / len(rows), 1) if rows else None,
            "got": dict(
                sorted(
                    ((r["got"], sum(1 for x in rows if x["got"] == r["got"])) for r in rows),
                    key=lambda kv: -rank[kv[0]],
                )
            ),
        }
    disagreements = sorted(
        (r for r in judged if r["got"] != r["expected"]),
        key=lambda r: -abs(rank[r["got"]] - rank[r["expected"]]),
    )
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "model": model,
        "prompt_version": cfg.judge.prompt_version,
        "articles": total,
        "judged": len(judged),
        "agreement": round(exact / len(judged), 3) if judged else None,
        "within_one_band": round(near / len(judged), 3) if judged else None,
        "per_band": per_band,
        "disagreements": disagreements[:10],
        "spend_usd": round(spent, 4),
        "results": results,
    }


def render_text(summary: dict[str, Any]) -> str:
    lines = [
        f"evals: {summary['judged']} of {summary['articles']} judged by {summary['model']} "
        f"({summary['prompt_version']}), ${summary['spend_usd']:.3f}",
    ]
    if summary["agreement"] is not None:
        lines.append(
            f"agreement: {summary['agreement']:.0%} exact, {summary['within_one_band']:.0%} within one band"
        )
    lines.append("by expected band: n, exact, mean score, what the judge gave")
    for name, b in summary["per_band"].items():
        got = ", ".join(f"{k} {v}" for k, v in b["got"].items()) or "-"
        mean = f"{b['mean_score']:.0f}" if b["mean_score"] is not None else "-"
        lines.append(f"  {name:<9} {b['n']:>3} {b['exact']:>5} {mean:>5}   {got}")
    if summary["disagreements"]:
        lines.append("largest disagreements (expected -> got, score): title; why the band")
        for r in summary["disagreements"]:
            lines.append(
                f"  {r['expected']} -> {r['got']} ({r['score']:.0f}): {r['title'][:60]}; {r['why'][:70]}"
            )
    return "\n".join(lines)


def write_results(summary: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
