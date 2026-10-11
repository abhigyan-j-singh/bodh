"""The evaluation set and `bodh eval`: the set is well formed (offline), the run computes the
agreement against a fake judge, the cap holds, the command prints and writes."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeReader

from bodh import evals, run, store
from bodh.config import Config, Secrets
from bodh.errors import BodhError

REPO = Path(__file__).resolve().parents[1]


def test_eval_set_is_well_formed():
    items = evals.load_set(REPO / "evals" / "set.yaml")
    assert len(items) == 60 and [i["id"] for i in items] == list(range(1, 61))
    bands = Counter(i["expected"] for i in items)
    assert set(bands) == set(Config().tags.bands.values())  # every band is represented
    assert min(bands.values()) >= 8
    for item in items:
        assert len(item["text"].split()) <= evals.MAX_WORDS and len(item["text"].split()) >= 30, item["id"]
        assert item["why"] and item["feed"] and item["title"]
        assert item.get("folder") in (None, "Science", "News", "Essays", "Aggregators")
    titles = [i["title"] for i in items]
    assert len(set(titles)) == 60
    rows = evals.rows_for(items)
    assert rows[0]["id"] == "eval-1" and rows[0]["feed_title"] == items[0]["feed"] and rows[0]["url"] == ""
    assert (REPO / "profile.example" / "about.md").exists() and (
        REPO / "profile.example" / "learned.md"
    ).exists()


def test_eval_set_loader_refuses_bad_shapes(tmp_path):
    path = tmp_path / "set.yaml"
    for text, message in (
        ("{}", "expected a list"),
        ("- 1", "not a mapping"),
        ("- {id: 1, title: t, feed: f, expected: score-90, why: w}", "missing text"),
        ("- {id: x, title: t, feed: f, expected: score-90, why: w, text: t}", "id must be an integer"),
        ("- {id: 1, title: t, feed: f, expected: score-95, why: w, text: t}", "not one of"),
        ("- {id: 1, title: '', feed: f, expected: score-90, why: w, text: t}", "empty title"),
        (
            "- {id: 1, title: t, feed: f, expected: score-90, why: w, text: t}\n- {id: 1, title: t, feed: f, expected: score-90, why: w, text: t}",
            "not used before",
        ),
        (
            "- {id: 1, title: t, feed: f, expected: score-90, why: w, text: '" + "word " * 201 + "'}",
            "keep an article under",
        ),
    ):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(evals.EvalSetError, match=message):
            evals.load_set(path)
    with pytest.raises(evals.EvalSetError, match="no evaluation set"):
        evals.load_set(tmp_path / "missing.yaml")


def _ctx(cfg, conn, scores):
    ctx = run.Context(cfg, Secrets(), conn)
    ctx._client = FakeReader([])
    ctx._llm = FakeLLM(cfg, conn, scores=scores)
    return ctx


def test_eval_computes_the_agreement_against_a_scripted_judge(cfg, conn):
    items = evals.load_set(REPO / "evals" / "set.yaml")
    expected = {i["title"]: i["expected"] for i in items}
    threshold = {name: t for t, name in cfg.tags.bands.items()}

    def scripted(title: str, purpose: str) -> int:
        # the band's threshold plus five: the right band for every article but the first two,
        # which the judge puts one band too high and two bands too low
        band = expected[title]
        if title == items[0]["title"]:
            return 100
        if title == items[1]["title"]:
            return 100  # expected score-90: right
        if title == items[8]["title"]:  # expected score-70: judged score-0
            return 5
        return threshold[band] + 5

    ctx = _ctx(cfg, conn, scripted)
    cfg.judge.articles_per_call = 7
    seen = []
    summary = evals.run_eval(ctx, items, cap_usd=0.5, on_progress=lambda d, t: seen.append((d, t)))
    assert summary["articles"] == 60 and summary["judged"] == 60 and seen[-1] == (60, 60)
    assert summary["model"] == cfg.models.judge and summary["prompt_version"] == cfg.judge.prompt_version
    assert summary["agreement"] == pytest.approx(59 / 60, abs=0.001)
    assert summary["within_one_band"] == pytest.approx(59 / 60, abs=0.001)
    assert summary["per_band"]["score-70"]["n"] == 18 and summary["per_band"]["score-70"]["exact"] == 17
    assert summary["per_band"]["score-70"]["got"] == {"score-70": 17, "score-0": 1}
    assert summary["per_band"]["score-90"]["mean_score"] > 90
    assert [d["id"] for d in summary["disagreements"]] == [9]
    assert summary["spend_usd"] > 0 and summary["date"]
    assert (
        conn.execute("SELECT COUNT(*) FROM costs WHERE purpose = 'eval'").fetchone()[0] == 9
    )  # 60 articles, 7 per call
    assert ctx.llm.calls[0] == (cfg.models.judge, "eval")
    # the judge saw the example student, not the project's about.md
    assert (
        "Ines" in ctx.llm.api.calls[0]["system"][0]["text"]
        and "Student: test." not in ctx.llm.api.calls[0]["system"][0]["text"]
    )
    text = evals.render_text(summary)
    assert "agreement: 98% exact, 98% within one band" in text and "score-70 -> score-0" in text
    out = evals.write_results(summary, cfg.root / "evals" / "results.json")
    assert out.exists() and '"agreement": 0.983' in out.read_text(encoding="utf-8")


def test_eval_cap_and_price_refusals(cfg, conn):
    items = evals.load_set(REPO / "evals" / "set.yaml")
    ctx = _ctx(cfg, conn, lambda title, purpose: 50)
    with pytest.raises(BodhError, match="over the cap"):
        evals.run_eval(ctx, items, cap_usd=0.0001)
    with pytest.raises(BodhError, match="no price"):
        evals.run_eval(ctx, items, model="unpriced-model", cap_usd=5.0)
    # the cap also stops a run that turns out dearer than the estimate
    ctx.llm.api.usage.input_tokens = 2_000_000
    summary = evals.run_eval(ctx, items, cap_usd=0.5)
    assert summary["judged"] < 60 and summary["spend_usd"] > 0.5
    assert len(ctx.llm.calls) == 1


def test_eval_command_prints_and_writes(cfg, conn, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bodh.cli import app

    ctx = _ctx(cfg, conn, lambda title, purpose: 95)
    llm, client = ctx.llm, ctx.client
    conn.close()
    (cfg.root / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(cfg.root))
    monkeypatch.setattr(run.Context, "client", property(lambda self: client))
    monkeypatch.setattr(run.Context, "llm", property(lambda self: llm))
    out = tmp_path / "results.json"
    result = CliRunner().invoke(app, ["eval", "--write", str(out)])
    assert result.exit_code == 0, result.output
    assert (
        "60 articles; about $" in result.output
        and "agreement:" in result.output
        and "written:" in result.output
    )
    assert out.exists()
    result = CliRunner().invoke(app, ["eval", "--set", str(tmp_path / "none.yaml")])
    assert result.exit_code == 1 and "no evaluation set" in result.output
    assert store.SCHEMA_VERSION >= 5
