"""config.yaml: where it is found, how it is validated, what the example promises."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from triage.config import Config, ConfigError, find_root, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_find_root_env_ancestor_and_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
    project = tmp_path / "proj"
    (project / "deep" / "er").mkdir(parents=True)
    (project / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    assert find_root(project / "deep" / "er") == project.resolve()
    with pytest.raises(ConfigError):
        find_root(tmp_path)  # nothing above tmp_path holds a config.yaml of ours
    monkeypatch.setenv("TRIAGE_HOME", str(project))
    assert find_root(tmp_path) == project.resolve()
    monkeypatch.setenv("TRIAGE_HOME", str(tmp_path))
    with pytest.raises(ConfigError):
        find_root()
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    assert not (tmp_path / "data").exists()


def test_load_config_non_mapping_yaml_fails_clearly(tmp_path):
    (tmp_path / "config.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_shadow_is_the_default():
    assert Config().cut.enforce is False


def test_example_matches_defaults():
    """Every value in config.example.yaml is the code's default, so the comments in the example
    describe what a stranger gets; the folders are the one thing that is the owner's."""
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    example = Config(**raw).model_dump()
    default = Config().model_dump()
    example["cut"].pop("folders")
    default["cut"].pop("folders")
    assert example == default


def test_config_rejects_typos_bounds_and_timezone():
    import pydantic

    for raw in (
        {"cut": {"targe_unread": 40}},                 # a typo used to load as the default
        {"cut": {"max_cuts_per_run": -1}},             # cut all but one
        {"cut": {"target_unread": -1}},                # cut everything
        {"cut": {"default_floor": 101}},
        {"cut": {"concept_cap": 1.5}},
        {"cut": {"folders": {"X": {"auto_cut_below": 200}}}},
        {"timezone": "Mars/Olympus"},                  # loaded, then every run failed in budget_left
        {"tags": {"buckets": {}}},
        {"tags": {"cut": "wrong"}},                    # two tags with one name
        {"tags": {"great": ""}},
        {"models": {"batch_discount": 2}},
        {"models": {"prices_per_mtok": {"m": [1.0]}}},
        {"scoring": {"batch_size": 0}},
        {"scoring": {"rejudge_min_score": 120}},
        {"breakers": {"max_cut_share": 1.2}},
        {"nonsense": {}},
    ):
        with pytest.raises(pydantic.ValidationError):
            Config(**raw)
    assert Config(timezone="UTC").timezone == "UTC"
    assert Config(scoring={"max_age_days": None}).scoring.max_age_days is None


def test_secrets_never_appear_in_repr_or_str():
    from triage.config import Secrets

    s = Secrets(bazqux_email="student@example.com", bazqux_password="hunter2", anthropic_api_key="sk-ant-xyz",
                healthcheck_url="https://hc-ping.com/uuid-secret")
    for text in (repr(s), str(s), f"{s!r}", repr(s.bazqux_password), str(s.model_dump())):
        for secret in ("student@example.com", "hunter2", "sk-ant-xyz", "uuid-secret"):
            assert secret not in text
    assert s.value("bazqux_password") == "hunter2" and s.is_set("anthropic_api_key") and not Secrets().is_set("bazqux_email")
    assert sorted(s.values()) == sorted(["student@example.com", "hunter2", "sk-ant-xyz", "https://hc-ping.com/uuid-secret"])


def test_unknown_model_warns_once(caplog):
    import logging

    from triage.config import Models, _warned_models

    _warned_models.discard("brand-new-model")
    m = Models()
    with caplog.at_level(logging.WARNING):
        m.price("brand-new-model")
        m.price("brand-new-model")
    assert caplog.text.count("no price for 'brand-new-model'") == 1
