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
