"""The settings surface: the schema every pane is drawn from, CONFIG.md generated from it,
the writer that keeps the file's comments, and `bodh settings` on the command line."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bodh import settings
from bodh.cli import app
from bodh.config import Config, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def test_schema_covers_every_field_and_every_group_is_known():
    keys = {s.key for s in settings.schema()}
    # every field of every section is a setting
    for section in settings.SECTIONS:
        model = Config.model_fields[section].annotation
        for name in model.model_fields:
            assert f"{section}.{name}" in keys
    for name in settings.SCALAR_TOP:
        assert name in keys
    assert "folders" in keys
    for s in settings.schema():
        assert s.group in settings.GROUP_KEYS and s.help and s.title and s.control
    controls = {s.key: s.control for s in settings.schema()}
    assert controls["autocut.enabled"] == "switch" and controls["autocut.cutoff"] == "number"
    assert controls["models.judge"] == "model" and controls["models.cache_ttl"] == "choice"
    assert controls["judge.weights"] == "weights" and controls["tags.bands"] == "bands"
    assert controls["models.prices_per_mtok"] == "prices" and controls["folders"] == "folders"
    assert (
        controls["timezone"] == "timezone"
        and controls["data_dir"] == "path"
        and controls["tags.cut"] == "tag"
    )
    cutoff = next(s for s in settings.schema() if s.key == "autocut.cutoff")
    assert (cutoff.min, cutoff.max, cutoff.unit, cutoff.default) == (0, 100, "points", 35)
    age = next(s for s in settings.schema() if s.key == "limits.max_age_days")
    assert age.nullable and age.default == 14
    ttl = next(s for s in settings.schema() if s.key == "models.cache_ttl")
    assert ttl.choices == ["5m", "1h"] and ttl.advanced


def test_schema_json_carries_values_and_sources(cfg):
    (cfg.root / "config.yaml").write_text("prune:\n  keep: 25\n", encoding="utf-8")
    loaded = load_config(cfg.root)
    doc = settings.schema_json(loaded)
    assert doc["schema"] == 1 and doc["version"] == settings.SCHEMA_VERSION
    assert [g["key"] for g in doc["groups"]] == list(settings.GROUP_KEYS)
    keep = next(s for s in doc["settings"] if s["key"] == "prune.keep")
    assert keep["value"] == 25 and keep["source"] == "config.yaml"
    judge = next(s for s in doc["settings"] if s["key"] == "models.judge")
    assert judge["value"] == "claude-sonnet-5-5" and judge["source"] == "default"
    folders = next(s for s in doc["settings"] if s["key"] == "folders")
    assert folders["value"] == {} and [f["key"] for f in folders["fields"]] == [
        "protected",
        "cutoff",
        "judge",
    ]


def test_config_md_is_generated_and_current():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import gen_config_md

    text = gen_config_md.text()
    assert text.startswith("# Every setting") and "| `autocut.cutoff` |" in text and "## Cutting" in text
    assert "`folders.<name>.cutoff`" in text
    assert gen_config_md.OUT.read_text(encoding="utf-8") == text, "run python scripts/gen_config_md.py"


def test_example_file_names_only_known_keys():
    """config.example.yaml and the schema are two spellings of one list."""
    import yaml

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    known = settings.known_keys()
    for section, body in raw.items():
        if section == "folders":
            continue
        if isinstance(body, dict):
            for key in body:
                assert f"{section}.{key}" in known, f"{section}.{key} in the example is not a setting"
        else:
            assert section in known


def test_apply_keeps_comments_and_refuses_bad_values(tmp_path):
    (tmp_path / "config.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "profile").mkdir()
    cfg, notes = settings.apply(
        tmp_path, {"autocut.cutoff": 40, "limits.max_age_days": None, "timezone": "Asia/Kolkata"}
    )
    assert (
        notes == []
        and cfg.autocut.cutoff == 40
        and cfg.limits.max_age_days is None
        and cfg.timezone == "Asia/Kolkata"
    )
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "cutoff: 40                       # the cut-off for folders without their own" in text
    assert "# bodh configuration." in text and "max_age_days:" in text
    before = text
    with pytest.raises(settings.SettingsError) as exc:
        settings.apply(tmp_path, {"autocut.cutoff": 101})
    assert exc.value.errors == [
        {"key": "autocut.cutoff", "message": "Input should be less than or equal to 100"}
    ]
    with pytest.raises(settings.SettingsError) as exc:
        settings.apply(tmp_path, {"judge.weights": {"quality": 0.9, "learning": 0.9}})
    assert exc.value.errors[0]["key"] == "judge.weights" and "sum to 1.0" in exc.value.errors[0]["message"]
    with pytest.raises(settings.SettingsError) as exc:
        settings.apply(tmp_path, {"models.nope": 1})
    assert exc.value.errors[0]["key"] == "models.nope"
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_apply_writes_the_whole_value_keys_in_the_example_style(tmp_path):
    (tmp_path / "config.yaml").write_text("models:\n  judge: claude-sonnet-5-5\n", encoding="utf-8")
    (tmp_path / "profile").mkdir()
    cfg, _ = settings.apply(
        tmp_path,
        {
            "judge.weights": {"quality": 0.4, "learning": 0.3, "fit": 0.1, "breadth": 0.1, "relevance": 0.1},
            "tags.bands": {"90": "top", "50": "mid", "0": "low"},
            "folders": {
                "Always read": {"protected": True},
                "News": {"cutoff": 70, "judge": True, "protected": False},
            },
            "models.prices_per_mtok": {"claude-sonnet-5-5": [2, 10, 0.1], "openai/gpt-5-mini": [0.25, 2.0]},
        },
    )
    assert cfg.judge.weights["quality"] == 0.4 and cfg.tags.bands == {90: "top", 50: "mid", 0: "low"}
    assert cfg.folders["Always read"].protected and cfg.folders["News"].cutoff == 70
    assert cfg.models.prices_per_mtok["openai/gpt-5-mini"] == [0.25, 2.0]
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "weights: {quality: 0.4, learning: 0.3, fit: 0.1, breadth: 0.1, relevance: 0.1}" in text
    assert "bands: {90: top, 50: mid, 0: low}" in text
    assert (
        "Always read: {protected: true}" in text
        and "News: {cutoff: 70, judge: true, protected: false}" in text
    )
    # idempotent: writing the same values again changes nothing
    cfg2, _ = settings.apply(tmp_path, {"folders": settings.values(cfg)["folders"]})
    assert settings.values(cfg2) == settings.values(cfg)


def test_apply_moves_old_key_names_with_their_comments(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "cut:\n  enforce: true   # keep cutting\n  default_floor: 30\nscoring:\n  workers: 2\n",
        encoding="utf-8",
    )
    (tmp_path / "profile").mkdir()
    cfg, notes = settings.apply(tmp_path, {"prune.keep": 25})
    assert notes == [
        "scoring.workers -> limits.parallel_calls",
        "cut.enforce -> autocut.enabled",
        "cut.default_floor -> autocut.cutoff",
    ]
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "enabled: true   # keep cutting" in text and "\ncut:" not in text and "scoring:" not in text
    assert (
        cfg.autocut.enabled
        and cfg.autocut.cutoff == 30
        and cfg.limits.parallel_calls == 2
        and cfg.prune.keep == 25
    )
    assert cfg.old_keys == []


def test_values_and_sources(cfg):
    (cfg.root / "config.yaml").write_text(
        'folders:\n  "News": { cutoff: 70 }\nautocut:\n  cutoff: 40\n', encoding="utf-8"
    )
    loaded = load_config(cfg.root)
    vals, srcs = settings.values(loaded), settings.sources(loaded)
    assert vals["autocut.cutoff"] == 40 and srcs["autocut.cutoff"] == "config.yaml"
    assert vals["prune.keep"] == 40 and srcs["prune.keep"] == "default"
    assert (
        vals["folders"] == {"News": {"protected": False, "cutoff": 70, "judge": True}}
        and srcs["folders"] == "config.yaml"
    )
    assert vals["data_dir"] == "data" and vals["tags.bands"] == {
        "90": "score-90",
        "70": "score-70",
        "50": "score-50",
        "30": "score-30",
        "0": "score-0",
    }


def test_parse_cli_value():
    assert settings.parse_cli_value("true") is True and settings.parse_cli_value("35") == 35
    assert settings.parse_cli_value("0.2") == 0.2 and settings.parse_cli_value("null") is None
    assert (
        settings.parse_cli_value('"Must read"') == "Must read"
        and settings.parse_cli_value("Must read") == "Must read"
    )
    assert settings.parse_cli_value("{quality: 0.3}") == {"quality": 0.3}


def test_settings_command(tmp_path, monkeypatch):
    root = tmp_path / "p"
    root.mkdir()
    (root / "config.yaml").write_text("autocut:\n  cutoff: 35   # the default\n", encoding="utf-8")
    (root / "profile").mkdir()
    (root / "profile" / "about.md").write_text("# About the student\nStudent: t\n", encoding="utf-8")
    monkeypatch.setenv("BODH_HOME", str(root))
    runner = CliRunner()
    res = runner.invoke(app, ["settings"])
    assert (
        res.exit_code == 0
        and "autocut.cutoff" in res.output
        and "(config.yaml)" in res.output
        and "(default)" in res.output
    )
    res = runner.invoke(app, ["settings", "get", "autocut.cutoff", "--json"])
    assert res.exit_code == 0 and '"value": 35' in res.output and '"source": "config.yaml"' in res.output
    res = runner.invoke(app, ["settings", "set", "autocut.cutoff", "40"])
    assert res.exit_code == 0 and "autocut.cutoff = 40" in res.output and "comments kept" in res.output
    assert "cutoff: 40   # the default" in (root / "config.yaml").read_text(encoding="utf-8")
    res = runner.invoke(app, ["settings", "set", "autocut.cutoff", "140"])
    assert res.exit_code == 2 and "autocut.cutoff" in res.output
    res = runner.invoke(app, ["settings", "set", "nope", "1"])
    assert res.exit_code == 1 and "not a setting" in res.output
    res = runner.invoke(app, ["settings", "get"])
    assert res.exit_code == 1 and "which key" in res.output
    res = runner.invoke(app, ["settings", "schema", "--json"])
    assert res.exit_code == 0 and '"control": "folders"' in res.output
    res = runner.invoke(app, ["settings", "schema"])
    assert res.exit_code == 0 and "autocut.cutoff" in res.output
    res = runner.invoke(app, ["settings", "dance"])
    assert res.exit_code == 1
