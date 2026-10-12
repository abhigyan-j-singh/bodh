"""`bodh models`: the roles, the providers and their keys, the prices and the capabilities;
and `bodh models compare`, which judges a sample of already-judged unread articles with the
judge and a second model, side by side, and says how far they agree, what they cost and how
fast they are. Nothing stored changes: a comparison is evidence for a switch, not the switch
(`models.judge` in config.yaml is the switch; `bodh run --rejudge` re-judges on request)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import Config, Secrets
from .errors import BodhError
from .judgment import compose
from .providers import make_provider
from .providers.base import PROVIDERS, REQUIRED_SECRET, FlagStore, ModelRef, model_name

log = logging.getLogger(__name__)


def _role_models(cfg: Config) -> list[tuple[str, str]]:
    return [
        ("judge", cfg.models.judge),
        ("reviewer", cfg.models.reviewer),
        ("profiler", cfg.models.profiler),
        ("quick judge", cfg.catch_up.model),
    ]


def _capabilities_text(provider: str, model: str, secrets: Secrets) -> str:
    """What the provider says it can do for the model, without a database or a call."""
    from .llm import secret_field

    if not secrets.is_set(secret_field(provider)):
        return "(key not set)"
    try:
        found = make_provider(provider, secrets, FlagStore(lambda _k: None, lambda _k, _v: None))
        caps = found.capabilities(model)
    except (
        Exception
    ) as exc:  # a provider that cannot be built here (no base URL, say): say so, never crash a listing
        return f"({exc})"
    parts = [
        "batch" if caps.batch else "no batch",
        "schema" if caps.structured_output else "plain JSON",
        "cache" if caps.prompt_cache else "no cache",
    ]
    return ", ".join(parts)


def render_models(cfg: Config, secrets: Secrets) -> str:
    from .llm import secret_field

    lines = ["roles:"]
    for role, text in _role_models(cfg):
        ref = ModelRef.parse(text)
        price = cfg.models.prices_per_mtok.get(ref.name)
        priced = (
            f"${price[0]:g}/{price[1]:g}" + (f"/{price[2]:g}" if len(price) > 2 else "") + " per Mtok"
            if price
            else "unpriced (charged as the dearest known)"
        )
        lines.append(
            f"  {role:<12} {ref!s:<40} {priced:<36} {_capabilities_text(ref.provider, ref.model, secrets)}"
        )
    lines.append("providers (key in .env, the environment or the keychain):")
    for name in PROVIDERS:
        state = "key set" if secrets.is_set(secret_field(name)) else f"not set ({REQUIRED_SECRET[name]})"
        lines.append(f"  {name:<18} {state}")
    lines.append("prices (models.prices_per_mtok, USD per million tokens: input, output, cache read):")
    for name, val in sorted(cfg.models.prices_per_mtok.items()):
        lines.append(f"  {name:<40} {val}")
    lines.append(f"budget: ${cfg.models.daily_budget_usd:.2f} a day across every provider")
    return "\n".join(lines)


def sample_rows(conn: Any, cfg: Config, n: int) -> list[dict[str, Any]]:
    """Unread articles the judge has judged, newest first, with their text."""
    judge = model_name(cfg.models.judge)
    rows = conn.execute(
        """SELECT a.id, a.title, a.url, a.feed_title, a.folder, a.author, a.published, a.text,
                  s.score, s.bucket, s.lesson, s.concept
           FROM articles a JOIN scores s ON s.article_id = a.id JOIN state st ON st.article_id = a.id
           WHERE st.status = 'unread' AND s.model = ? AND s.prompt_version = ? AND a.text IS NOT NULL AND a.text != ''
           ORDER BY a.crawled_msec DESC LIMIT ?""",
        (judge, cfg.judge.prompt_version, n),
    ).fetchall()
    return [dict(r) for r in rows]


def plan(ctx: Any, with_model: str, sample: int) -> dict[str, Any]:
    cfg = ctx.cfg
    other = ModelRef.parse(with_model)
    if not cfg.models.has_price(other.name) or not cfg.models.has_price(cfg.models.judge):
        raise BodhError(
            f"both models need a price in models.prices_per_mtok ({cfg.models.judge}, {with_model})"
        )
    rows = sample_rows(ctx.conn, cfg, sample)
    if not rows:
        return {"n": 0, "preview": "nothing to compare on: no unread article judged by the judge yet"}
    cost = 0.0
    for text in (cfg.models.judge, with_model):
        p_in, p_out = cfg.models.price(text)
        cost += len(rows) * (450 * p_in + 65 * p_out) / 1_000_000 * 1.2
    return {
        "n": len(rows),
        "preview": f"{len(rows)} unread articles judged by {cfg.models.judge}; judging them again with both models costs about ${cost:.2f}",
    }


def _judge_with(
    ctx: Any, model: str, rows: list[dict[str, Any]], on_progress: Any, done: list[int], total: int
) -> dict[str, Any]:
    from .judge import Judge, concept_context

    cfg = ctx.cfg.model_copy(deep=True)
    cfg.models.judge = model
    judge = Judge(cfg, ctx.llm, ctx.conn, context=concept_context(ctx.conn, cfg), purpose="compare")
    spent_before = ctx.llm.spent_today()
    started = time.monotonic()
    scores: dict[str, float] = {}
    lessons: dict[str, str] = {}
    size = cfg.judge.articles_per_call
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        verdicts = judge.judge_batch(batch, None)  # type: ignore[arg-type]  # dicts carry the row's keys
        for row in batch:
            v = verdicts.get(row["id"])
            if v is not None:
                scores[row["id"]] = compose(v, cfg, judge.expansion_topics)
                lessons[row["id"]] = v.lesson
            done[0] += 1
        if on_progress is not None:
            on_progress(done[0], total)
    seconds = time.monotonic() - started
    return {
        "model": model,
        "scores": scores,
        "lessons": lessons,
        "spend": ctx.llm.spent_today() - spent_before,
        "seconds_per_article": seconds / max(1, len(rows)),
    }


def compare(ctx: Any, with_model: str, sample: int, on_progress: Any = None) -> dict[str, Any]:
    """Judge the sample with the judge and with `with_model`; nothing is stored but the costs."""
    cfg = ctx.cfg
    rows = sample_rows(ctx.conn, cfg, sample)
    if not rows:
        raise BodhError("nothing to compare on: no unread article judged by the judge yet")
    done = [0]
    first = _judge_with(ctx, cfg.models.judge, rows, on_progress, done, 2 * len(rows))
    second = _judge_with(ctx, with_model, rows, on_progress, done, 2 * len(rows))
    both = [r for r in rows if r["id"] in first["scores"] and r["id"] in second["scores"]]
    rank = {name: i for i, (_t, name) in enumerate(sorted(cfg.tags.bands.items()))}
    pairs = []
    for r in both:
        a, b = first["scores"][r["id"]], second["scores"][r["id"]]
        pairs.append(
            {
                "id": r["id"],
                "title": r["title"],
                "stored": r["score"],
                "first": a,
                "second": b,
                "band_first": cfg.tags.band_for(a),
                "band_second": cfg.tags.band_for(b),
                "lesson_first": first["lessons"].get(r["id"], ""),
                "lesson_second": second["lessons"].get(r["id"], ""),
            }
        )
    n = len(pairs)
    agree = sum(1 for p in pairs if p["band_first"] == p["band_second"])
    near = sum(1 for p in pairs if abs(rank[p["band_first"]] - rank[p["band_second"]]) <= 1)
    mad = sum(abs(p["first"] - p["second"]) for p in pairs) / n if n else None
    disagreements = sorted(pairs, key=lambda p: -abs(p["first"] - p["second"]))[:10]
    return {
        "n": n,
        "sample": len(rows),
        "first": {k: v for k, v in first.items() if k not in ("scores", "lessons")},
        "second": {k: v for k, v in second.items() if k not in ("scores", "lessons")},
        "band_agreement": round(agree / n, 3) if n else None,
        "within_one_band": round(near / n, 3) if n else None,
        "mean_abs_difference": round(mad, 1) if mad is not None else None,
        "disagreements": disagreements,
        "per_1000": {
            first["model"]: round(first["spend"] / max(1, len(rows)) * 1000, 2),
            second["model"]: round(second["spend"] / max(1, len(rows)) * 1000, 2),
        },
    }


def render_comparison(result: dict[str, Any]) -> str:
    a, b = result["first"], result["second"]
    lines = [
        f"compared {result['n']} of {result['sample']} articles: {a['model']} against {b['model']}",
    ]
    if result["band_agreement"] is not None:
        lines.append(
            f"same band {result['band_agreement']:.0%}, within one band {result['within_one_band']:.0%}, "
            f"mean difference {result['mean_abs_difference']:.1f} points"
        )
    for side in (a, b):
        lines.append(
            f"  {side['model']}: ${result['per_1000'][side['model']]:.2f} per 1,000 articles, "
            f"{side['seconds_per_article']:.1f} s per article"
        )
    if result["disagreements"]:
        lines.append("largest disagreements (first -> second): title; the two lessons")
        for p in result["disagreements"]:
            lines.append(
                f"  {p['first']:.0f} -> {p['second']:.0f}: {p['title'][:60]}; "
                f"{p['lesson_first'][:50]} | {p['lesson_second'][:50]}"
            )
    lines.append(
        "nothing stored changed; models.judge in config.yaml is the switch, `bodh run --rejudge` re-judges"
    )
    return "\n".join(lines)
