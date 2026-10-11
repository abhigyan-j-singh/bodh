"""config.yaml: where it is found, how it is validated, what the example promises."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from bodh.config import Config, ConfigError, find_root, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_find_root_env_ancestor_and_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a real ~/bodh on this machine must not answer
    project = tmp_path / "proj"
    (project / "deep" / "er").mkdir(parents=True)
    (project / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    assert find_root(project / "deep" / "er") == project.resolve()
    with pytest.raises(ConfigError):
        find_root(tmp_path)  # nothing above tmp_path holds a config.yaml of ours
    monkeypatch.setenv("BODH_HOME", str(project))
    assert find_root(tmp_path) == project.resolve()
    monkeypatch.setenv("BODH_HOME", str(tmp_path))
    with pytest.raises(ConfigError):
        find_root()
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    assert not (tmp_path / "data").exists()


def test_load_config_non_mapping_yaml_fails_clearly(tmp_path):
    (tmp_path / "config.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_scoring_only_is_the_default():
    assert Config().autocut.enabled is False


def test_example_matches_defaults():
    """Every value in config.example.yaml is the code's default, so the comments in the example
    describe what a stranger gets; the folders are the one thing that is the owner's. The example
    uses no old key name."""
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    from bodh.config import modernise_keys

    assert modernise_keys(raw)[1] == []
    example = Config(**raw).model_dump()
    default = Config().model_dump()
    example.pop("folders")
    default.pop("folders")
    assert example == default


def test_config_rejects_typos_bounds_and_timezone():
    import pydantic

    for raw in (
        {"prune": {"kep": 40}},                        # a typo used to load as the default
        {"autocut": {"max_per_run": -1}},              # cut all but one
        {"prune": {"keep": -1}},                       # cut everything
        {"autocut": {"cutoff": 101}},
        {"prune": {"max_share_per_concept": 1.5}},
        {"folders": {"X": {"cutoff": 200}}},
        {"timezone": "Mars/Olympus"},                  # loaded, then every run failed in budget_left
        {"tags": {"bands": {}}},
        {"tags": {"cut": "rescue"}},                   # two tags with one name
        {"tags": {"great": ""}},
        {"models": {"batch_discount": 2}},
        {"models": {"prices_per_mtok": {"m": [1.0]}}},
        {"judge": {"articles_per_call": 0}},
        {"catch_up": {"rejudge_at": 120}},
        {"safety": {"max_cut_share": 1.2}},
        {"nonsense": {}},
    ):
        with pytest.raises(pydantic.ValidationError):
            Config(**raw)
    assert Config(timezone="UTC").timezone == "UTC"
    assert Config(limits={"max_age_days": None}).limits.max_age_days is None


def test_old_key_names_map_with_a_warning(tmp_path, caplog):
    """A config.yaml from before October 2026 loads: every old key is moved to its new place,
    the load warns once, `doctor` can list the moves, and the same key under both names is
    refused. A section that is gone with an unknown key in it is refused with the section named."""
    import logging

    from bodh.config import ConfigError, modernise_keys

    old = {
        "models": {"teacher": "m1", "second_look": "m2", "learner": "m3", "batch": False},
        "scoring": {"prompt_version": "teacher.v2", "batch_size": 10, "workers": 2, "max_per_run": 100,
                    "batch_max_per_run": 200, "max_age_days": 7, "rejudge_min_score": 60, "opening_chars": 500,
                    "second_look_window": 5, "second_look_chars": 3000, "second_look_max_per_run": 9,
                    "expansion_bonus": 5, "home_angle": "India", "home_bonus": 7,
                    "weights": {"quality": 0.5, "learning": 0.5, "fit": 0, "breadth": 0, "relevance": 0}},
        "cut": {"enforce": True, "target_unread": 30, "default_floor": 25, "max_cuts_per_run": 100, "concept_cap": 0.5,
                "exploration_share": 0.1, "must_read_stream": "Keep",
                "folders": {"News": {"auto_cut_below": 70}, "Comics": {"protected": True, "score": False}}},
        "tags": {"buckets": {90: "s90", 0: "s0"}, "wrong": "wrong"},
        "breakers": {"max_cut_share": 0.9, "min_new_for_breaker": 10, "rescue_rate_week": 0.1, "lowered_floor": 20,
                     "bulk_dismiss_threshold": 15},
        "feedback": {"concept_window_days": 30},
    }
    data, moves = modernise_keys(old)
    assert len(moves) == 36 and "cut.enforce -> autocut.enabled" in moves and "cut.folders -> folders" in moves
    assert "folders.News.auto_cut_below -> folders.News.cutoff" in moves
    cfg = Config(**data)
    assert cfg.models.judge == "m1" and cfg.models.reviewer == "m2" and cfg.models.profiler == "m3"
    assert cfg.judge.articles_per_call == 10 and cfg.judge.text_chars == 500 and cfg.judge.home_angle == "India"
    assert cfg.review.window == 5 and cfg.review.chars == 3000 and cfg.review.max_per_run == 9
    assert cfg.catch_up.rejudge_at == 60 and cfg.limits.parallel_calls == 2 and cfg.limits.max_age_days == 7
    assert cfg.autocut.enabled is True and cfg.autocut.cutoff == 25 and cfg.autocut.max_per_run == 100
    assert cfg.prune.keep == 30 and cfg.prune.max_share_per_concept == 0.5 and cfg.protect.must_read_stream == "Keep"
    assert cfg.folders["News"].cutoff == 70 and cfg.folders["Comics"].judge is False and cfg.cutoff_for("News") == 70
    assert cfg.tags.bands == {90: "s90", 0: "s0"} and cfg.tags.rescue == "wrong" and cfg.tags.rescue_tags() == ["wrong"]
    assert cfg.safety.min_judged == 10 and cfg.safety.lowered_cutoff == 20 and cfg.safety.mark_all_read_threshold == 15
    assert cfg.profile.concept_window_days == 30
    # a file on disk: the warning, and doctor's list
    (tmp_path / "config.yaml").write_text("cut:\n  enforce: true\n  target_unread: 30\nbreakers:\n  lowered_floor: 20\n",
                                           encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        loaded = load_config(tmp_path)
    assert loaded.autocut.enabled is True and loaded.prune.keep == 30 and loaded.safety.lowered_cutoff == 20
    assert "3 old key name(s)" in caplog.text and loaded.old_keys == [
        "cut.enforce -> autocut.enabled", "cut.target_unread -> prune.keep", "breakers.lowered_floor -> safety.lowered_cutoff"]
    assert Config(**modernise_keys({"tags": {"wrong": "nope"}})[0]).tags.rescue_tags() == ["nope", "wrong"]
    with pytest.raises(ConfigError):
        modernise_keys({"cut": {"enforce": True}, "autocut": {"enabled": False}})
    with pytest.raises(ConfigError):
        modernise_keys({"scoring": {"typo": 1}})
    with pytest.raises(ConfigError):
        modernise_keys({"folders": {"X": {"auto_cut_below": 1, "cutoff": 2}}})


def test_secrets_never_appear_in_repr_or_str():
    from bodh.config import Secrets

    s = Secrets(bazqux_email="student@example.com", bazqux_password="hunter2", anthropic_api_key="sk-ant-xyz",
                healthcheck_url="https://hc-ping.com/uuid-secret")
    for text in (repr(s), str(s), f"{s!r}", repr(s.bazqux_password), str(s.model_dump())):
        for secret in ("student@example.com", "hunter2", "sk-ant-xyz", "uuid-secret"):
            assert secret not in text
    assert s.value("bazqux_password") == "hunter2" and s.is_set("anthropic_api_key") and not Secrets().is_set("bazqux_email")
    assert sorted(s.values()) == sorted(["student@example.com", "hunter2", "sk-ant-xyz", "https://hc-ping.com/uuid-secret"])


def test_unknown_model_warns_once(caplog):
    import logging

    from bodh.config import Models, _warned_models

    _warned_models.discard("brand-new-model")
    m = Models()
    with caplog.at_level(logging.WARNING):
        m.price("brand-new-model")
        m.price("brand-new-model")
    assert caplog.text.count("no price for 'brand-new-model'") == 1


def test_timezone_defaults_to_the_machine_and_the_example_leaves_it_out(monkeypatch):
    """A stranger's budget day ends at their midnight, not the author's; $TZ wins, then
    /etc/localtime, then UTC; a name this machine does not know is passed over."""
    from bodh import config

    monkeypatch.setenv("TZ", ":Europe/Lisbon")
    assert config.system_timezone() == "Europe/Lisbon"
    monkeypatch.setenv("TZ", "Mars/Olympus")
    monkeypatch.setattr(config, "_localtime_name", lambda: "America/Sao_Paulo")
    assert config.system_timezone() == "America/Sao_Paulo"
    monkeypatch.delenv("TZ")
    monkeypatch.setattr(config, "_localtime_name", lambda: None)
    assert config.system_timezone() == "UTC"
    assert Config().timezone == "UTC"
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert "timezone" not in raw, "the example must leave the zone to the machine"
    assert "Asia/Kolkata" not in (ROOT / "config.example.yaml").read_text(encoding="utf-8")


def test_localtime_name_reads_the_zoneinfo_link(tmp_path, monkeypatch):
    from bodh import config

    link = tmp_path / "localtime"
    target = tmp_path / "usr" / "share" / "zoneinfo" / "Europe" / "Lisbon"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"TZif2")
    link.symlink_to(target)
    real_path = config.Path
    monkeypatch.setattr(config, "Path", lambda p="": real_path(str(link)) if p == "/etc/localtime" else real_path(p))
    assert config._localtime_name() == "Europe/Lisbon"
    # macOS: /var/db/timezone/tz/<version>/zoneinfo/...; the posix/ and right/ trees; $TZ as a path
    assert config._after_zoneinfo("/private/var/db/timezone/tz/2024a.1.0/zoneinfo/Asia/Kolkata") == "Asia/Kolkata"
    assert config._after_zoneinfo("/usr/share/zoneinfo/posix/Europe/Lisbon") == "Europe/Lisbon"
    assert config._after_zoneinfo("/usr/share/zoneinfo") is None and config._after_zoneinfo("/etc/localtime") is None
    monkeypatch.setenv("TZ", ":/usr/share/zoneinfo/Europe/Lisbon")
    assert config.system_timezone() == "Europe/Lisbon"


def test_localtime_name_copied_file_falls_back_to_etc_timezone(tmp_path, monkeypatch):
    """Debian copies the zone file to /etc/localtime and names it in /etc/timezone; a machine
    with neither (Windows) says nothing and the default is UTC; junk in /etc/timezone is not a crash."""
    from bodh import config

    files = {"/etc/localtime": tmp_path / "localtime", "/etc/timezone": tmp_path / "timezone"}
    real_path = config.Path
    monkeypatch.setattr(config, "Path", lambda p="": real_path(str(files.get(p, p))))
    files["/etc/localtime"].write_bytes(b"TZif2")
    assert config._localtime_name() is None
    files["/etc/timezone"].write_text("America/Sao_Paulo\n", encoding="utf-8")
    assert config._localtime_name() == "America/Sao_Paulo"
    files["/etc/timezone"].write_bytes(b"\xff\xfe")
    assert config._localtime_name() is None
    monkeypatch.delenv("TZ", raising=False)
    files["/etc/localtime"].unlink()
    files["/etc/timezone"].unlink()
    assert config.system_timezone() == "UTC"


def test_the_example_seed_is_recognised_and_refused(tmp_path, cfg, conn):
    """profile.example/seed.md describes an invented student: `doctor` reports it, the
    teacher refuses to judge for it, and a real seed in the example's shape passes."""
    from bodh.teacher import Teacher

    example = (ROOT / "profile.example" / "seed.md").read_text(encoding="utf-8")
    cfg.about_path.write_text(example, encoding="utf-8")
    assert cfg.about_is_example()
    with pytest.raises(ConfigError, match="still the worked example"):
        Teacher(cfg, None, conn)  # type: ignore[arg-type]
    own = re.sub(r"<!--.*?-->", "", example, flags=re.S).replace("Ines, a marine biologist", "Someone else, a geologist")
    cfg.about_path.write_text(own, encoding="utf-8")
    assert not cfg.about_is_example() and "geologist" in cfg.read_about()
    Teacher(cfg, None, conn)  # type: ignore[arg-type]


def test_find_root_tries_the_default_home_last(tmp_path, monkeypatch):
    """With no variable and no config.yaml above the working directory, ~/bodh is tried last;
    it answers only when it holds a config.yaml."""
    monkeypatch.delenv("BODH_HOME", raising=False)
    monkeypatch.delenv("TRIAGE_HOME", raising=False)
    home = tmp_path / "home"
    (home / "bodh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(ConfigError):
        find_root(tmp_path)
    (home / "bodh" / "config.yaml").write_text("cut:\n  enforce: false\n", encoding="utf-8")
    assert find_root(tmp_path) == (home / "bodh").resolve()


def test_empty_timezone_means_this_computer_and_doctor_knows_the_source(tmp_path, monkeypatch):
    from bodh.config import system_timezone

    monkeypatch.setenv("TZ", "Europe/Lisbon")
    assert Config(timezone="").timezone == system_timezone() == "Europe/Lisbon"
    assert Config(timezone=" Asia/Kolkata ").timezone == "Asia/Kolkata"
    (tmp_path / "config.yaml").write_text("timezone: Asia/Kolkata\n", encoding="utf-8")
    assert load_config(tmp_path).timezone_source == "config.yaml"
    (tmp_path / "config.yaml").write_text('timezone: ""\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.timezone_source == "this computer" and cfg.timezone == "Europe/Lisbon"
