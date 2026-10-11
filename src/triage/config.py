"""Configuration: config.yaml (behaviour) + .env (secrets). Validated at start;
a run refuses to do anything with a broken config."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

log = logging.getLogger(__name__)


class Strict(BaseModel):
    """Every section: a key that is not known is a typo, and a typo must not load as a default."""

    model_config = ConfigDict(extra="forbid")


class FolderPolicy(Strict):
    protected: bool = False
    auto_cut_below: int | None = Field(default=None, ge=0, le=100)
    score: bool = True              # false: never sent to the teacher, never cut (comics, say)


class Models(Strict):
    teacher: str = "claude-sonnet-5-5"
    second_look: str = "claude-opus-5-5"
    learner: str = "claude-opus-5-5"
    batch: bool = True              # Message Batches API: half price; judgments arrive on the next run
    batch_discount: float = Field(default=0.5, ge=0.0, le=1.0)
    cache_ttl: str = "1h"           # "5m" or "1h": how long the teacher's cached prefix stays warm between runs
    daily_budget_usd: float = Field(default=8.0, ge=0.0)
    est_usd_per_article: float = Field(default=0.001, ge=0.0)   # the budget check before a batch is submitted
    prices_per_mtok: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "claude-sonnet-5-5": [2.0, 10.0, 0.10],
            "claude-opus-5-5": [4.0, 20.0, 0.20],
            "claude-haiku-5-5": [0.10, 0.50, 0.01],
            "claude-haiku-4-5": [1.0, 5.0, 0.10],
        }
    )

    @field_validator("cache_ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError("models.cache_ttl must be '5m' or '1h'")
        return v

    @field_validator("prices_per_mtok")
    @classmethod
    def _prices(cls, v: dict[str, list[float]]) -> dict[str, list[float]]:
        for model, val in v.items():
            if len(val) not in (2, 3) or any(x < 0 for x in val):
                raise ValueError(f"models.prices_per_mtok.{model} must be [input, output] or [input, output, cache read], "
                                 "USD per million tokens")
        return v

    def _entry(self, model: str) -> list[float] | None:
        for key, val in self.prices_per_mtok.items():
            if model.startswith(key):
                return val
        return None

    def has_price(self, model: str) -> bool:
        return self._entry(model) is not None

    def _priced_as(self, model: str) -> list[float]:
        """The price list for a model; an unknown model is charged as the most expensive known
        one, so a renamed or new model never slips past the daily budget at $0."""
        entry = self._entry(model)
        if entry is not None:
            return entry
        if not self.prices_per_mtok:
            return [0.0, 0.0, 0.0]
        dearest = max(self.prices_per_mtok.values(), key=lambda val: (val[0], val[1]))
        if model not in _warned_models:
            _warned_models.add(model)
            log.warning("models: no price for %r in models.prices_per_mtok; charging it as the most expensive known "
                        "model (%s) until one is added", model, dearest)
        return dearest

    def price(self, model: str) -> tuple[float, float]:
        """(input, output) USD per million tokens."""
        val = self._priced_as(model)
        return float(val[0]), float(val[1])

    def cache_read_price(self, model: str) -> float:
        """USD per million cached input tokens read; the third list entry, else a tenth of input.
        Cache writes follow the published multipliers: 1.25x input (5 minutes), 2x (1 hour)."""
        val = self._priced_as(model)
        return float(val[2]) if len(val) >= 3 else float(val[0]) * 0.1


_warned_models: set[str] = set()


class Scoring(Strict):
    prompt_version: str = "teacher.v2"
    weights: dict[str, float] = Field(
        default_factory=lambda: {"quality": 0.30, "learning": 0.30, "fit": 0.15, "breadth": 0.15, "relevance": 0.10}
    )
    expansion_bonus: int = Field(default=10, ge=0, le=100)
    batch_size: int = Field(default=20, ge=1, le=100)        # articles per teacher call
    workers: int = Field(default=4, ge=1, le=64)             # synchronous runs: calls in flight at once
    max_per_run: int = Field(default=400, ge=0)              # synchronous runs: a backlog drains at this rate per run
    batch_max_per_run: int = Field(default=1200, ge=0)       # batch runs: articles submitted per run
    max_age_days: int | None = Field(default=14, ge=0)       # older unread articles are never scored
    rejudge_min_score: float | None = Field(default=50.0, ge=0, le=100)  # another model's judgment at or above
                                                                         # this is judged again by the teacher
    opening_chars: int = Field(default=1200, ge=100)
    second_look_window: int = Field(default=10, ge=0, le=100)
    second_look_chars: int = Field(default=6000, ge=100)
    second_look_max_per_run: int = Field(default=50, ge=0)   # synchronous runs: looks per run, so a backlog of
                                                             # close calls cannot eat the run's fifteen minutes

    @field_validator("weights")
    @classmethod
    def _weights_sum(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"scoring.weights must sum to 1.0, got {total:.3f}")
        return v


class Cut(Strict):
    enforce: bool = False           # false = shadow mode: score and tag, cut nothing; the safe default
    target_unread: int = Field(default=40, ge=0)
    default_floor: int = Field(default=35, ge=0, le=100)
    max_cuts_per_run: int = Field(default=500, ge=0)
    concept_cap: float = Field(default=0.25, ge=0.0, le=1.0)
    exploration_share: float = Field(default=0.20, ge=0.0, le=1.0)
    must_read_stream: str = Field(default="Must read", min_length=1)
    folders: dict[str, FolderPolicy] = Field(default_factory=dict)

    def floor_for(self, folder: str | None) -> int | None:
        """The automatic floor for a folder; None means never cut automatically."""
        policy = self.folders.get(folder or "")
        if policy is None:
            return self.default_floor
        if policy.protected:
            return None
        return policy.auto_cut_below if policy.auto_cut_below is not None else self.default_floor

    def is_protected(self, folder: str | None) -> bool:
        policy = self.folders.get(folder or "")
        return bool(policy and policy.protected)

    def unscored_folders(self) -> list[str]:
        """Folders the teacher never reads (``score: false``)."""
        return [name for name, policy in self.folders.items() if not policy.score]


class Tags(Strict):
    buckets: dict[int, str] = Field(
        default_factory=lambda: {90: "score-90", 70: "score-70", 50: "score-50", 30: "score-30", 0: "score-0"}
    )
    cut: str = "cut"
    # commands: the run acts on them and removes them
    wrong: str = "wrong"             # a cut article you wanted: back to unread, and a strong positive
    prune: str = "prune"             # on any article: the next enforcing run prunes to the target
    # verdicts: your opinion of an article, left on it as your record, recorded once
    great: str = "great"             # the best kind: more like this, and the eval gate protects it
    good: str = "good"               # worth the time
    meh: str = "meh"                 # not worth it
    bad: str = "bad"                 # junk; the teacher was wrong to keep it
    basic: str = "basic"             # too elementary for you
    heavy: str = "heavy"             # too dense or too specialist for you now
    more: str = "more"               # more of this article's concept, from the next run
    less: str = "less"               # less of it

    @model_validator(mode="after")
    def _distinct(self) -> Tags:
        if not self.buckets:
            raise ValueError("tags.buckets must name at least one bucket")
        names = [*self.buckets.values(), self.cut, self.wrong, self.prune, *self.verdict_tags().values()]
        if any(not n or not n.strip() for n in names):
            raise ValueError("every tag name must be non-empty")
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"tag names must be distinct; used more than once: {dupes}")
        return self

    def verdict_tags(self) -> dict[str, str]:
        """feedback kind -> tag name, for the tags that carry an opinion."""
        return {k: getattr(self, k) for k in ("great", "good", "meh", "bad", "basic", "heavy", "more", "less")}

    def bucket_for(self, score: float) -> str:
        for threshold in sorted(self.buckets, reverse=True):
            if score >= threshold:
                return self.buckets[threshold]
        return self.buckets[min(self.buckets)]


class Breakers(Strict):
    max_cut_share: float = Field(default=0.95, ge=0.0, le=1.0)
    min_new_for_breaker: int = Field(default=50, ge=1)
    rescue_rate_week: float = Field(default=0.05, ge=0.0, le=1.0)
    lowered_floor: int = Field(default=25, ge=0, le=100)
    bulk_dismiss_threshold: int = Field(default=30, ge=1)


class Feedback(Strict):
    concept_window_days: int = Field(default=90, ge=1)


class Config(Strict):
    timezone: str = "Asia/Kolkata"
    data_dir: Path = Path("./data")
    profile_dir: Path = Path("./profile")
    dashboard_dir: Path = Path("./dashboard")
    obsidian_note: str = ""
    models: Models = Field(default_factory=Models)
    scoring: Scoring = Field(default_factory=Scoring)
    cut: Cut = Field(default_factory=Cut)
    tags: Tags = Field(default_factory=Tags)
    breakers: Breakers = Field(default_factory=Breakers)
    feedback: Feedback = Field(default_factory=Feedback)

    # populated by load_config
    root: Path = Path(".")

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, ValueError, OSError) as exc:  # ZoneInfoNotFoundError is a KeyError
            raise ValueError(f"timezone {v!r} is not a known zone (IANA names, 'Asia/Kolkata' say)") from exc
        return v

    @property
    def db_path(self) -> Path:
        return (self.root / self.data_dir / "triage.db").resolve()

    @property
    def seed_path(self) -> Path:
        return (self.root / self.profile_dir / "seed.md").resolve()

    @property
    def learner_path(self) -> Path:
        return (self.root / self.profile_dir / "learner.md").resolve()

    @property
    def obsidian_path(self) -> Path | None:
        return Path(os.path.expanduser(self.obsidian_note)).resolve() if self.obsidian_note else None

    def read_seed(self) -> str:
        return self.seed_path.read_text(encoding="utf-8").strip() if self.seed_path.exists() else ""

    def read_learner(self) -> str:
        return self.learner_path.read_text(encoding="utf-8").strip() if self.learner_path.exists() else ""


class Secrets(BaseModel):
    """The values from .env. SecretStr: a repr, a str, an assertion message or a log line with
    %r in it shows '**********', never the value; code that needs one asks for it by name."""

    bazqux_email: SecretStr = SecretStr("")
    bazqux_password: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    healthcheck_url: SecretStr = SecretStr("")

    def value(self, name: str) -> str:
        return getattr(self, name).get_secret_value()

    def is_set(self, name: str) -> bool:
        return bool(self.value(name))

    def values(self) -> list[str]:
        """The non-empty values, for the log redactor; never for printing."""
        return [v for v in (self.value(n) for n in ("bazqux_email", "bazqux_password", "anthropic_api_key",
                                                    "healthcheck_url")) if v]


class ConfigError(Exception):
    """No usable config.yaml: nothing runs, nothing is created."""


def find_root(start: Path | None = None) -> Path:
    """The project root: TRIAGE_HOME, else the first ancestor of the working directory that
    holds config.yaml. Never a guess: a run in a folder with no config.yaml must not invent
    one with default folders and floors and start cutting."""
    env_root = os.environ.get("TRIAGE_HOME")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not (root / "config.yaml").exists():
            raise ConfigError(f"TRIAGE_HOME is {root} but it holds no config.yaml")
        return root
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config.yaml").exists():
            return candidate
    raise ConfigError(f"no config.yaml in {here} or above it; run from the triage folder, or set TRIAGE_HOME")


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    path = root / "config.yaml"
    if not path.exists():
        raise ConfigError(f"no config.yaml in {root}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a mapping of sections, not {type(raw).__name__}")
    cfg = Config(**raw)
    cfg.root = root
    (cfg.root / cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg


def load_secrets(root: Path | None = None) -> Secrets:
    root = root or find_root()
    # the project's own .env wins over whatever the shell happens to export (a stale key in ~/.zshrc, say)
    load_dotenv(root / ".env", override=True)
    return Secrets(
        bazqux_email=SecretStr(os.environ.get("BAZQUX_EMAIL", "")),
        bazqux_password=SecretStr(os.environ.get("BAZQUX_PASSWORD", "")),
        anthropic_api_key=SecretStr(os.environ.get("ANTHROPIC_API_KEY", "")),
        healthcheck_url=SecretStr(os.environ.get("HEALTHCHECK_URL", "")),
    )
