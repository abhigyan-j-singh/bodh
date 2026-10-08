"""Configuration: config.yaml (behaviour) + .env (secrets). Validated at start;
a run refuses to do anything with a broken config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


class FolderPolicy(BaseModel):
    protected: bool = False
    auto_cut_below: int | None = None
    score: bool = True              # false: never sent to the teacher, never cut (comics, say)


class Models(BaseModel):
    teacher: str = "claude-sonnet-5-5"
    second_look: str = "claude-opus-5-5"
    learner: str = "claude-opus-5-5"
    batch: bool = True              # Message Batches API: half price; judgments arrive on the next run
    batch_discount: float = 0.5
    cache_ttl: str = "1h"           # "5m" or "1h": how long the teacher's cached prefix stays warm between runs
    daily_budget_usd: float = 8.0
    est_usd_per_article: float = 0.001   # the budget check before a batch is submitted, before its usage is known
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

    def price(self, model: str) -> tuple[float, float]:
        """(input, output) USD per million tokens."""
        for key, val in self.prices_per_mtok.items():
            if model.startswith(key) and len(val) >= 2:
                return float(val[0]), float(val[1])
        return 0.0, 0.0

    def cache_read_price(self, model: str) -> float:
        """USD per million cached input tokens read; the third list entry, else a tenth of input.
        Cache writes follow the published multipliers: 1.25x input (5 minutes), 2x (1 hour)."""
        for key, val in self.prices_per_mtok.items():
            if model.startswith(key) and len(val) >= 2:
                return float(val[2]) if len(val) >= 3 else float(val[0]) * 0.1
        return 0.0


class Scoring(BaseModel):
    prompt_version: str = "teacher.v2"
    weights: dict[str, float] = Field(
        default_factory=lambda: {"quality": 0.30, "learning": 0.30, "fit": 0.15, "breadth": 0.15, "relevance": 0.10}
    )
    expansion_bonus: int = 10
    batch_size: int = 20             # articles per teacher call
    workers: int = 4                 # synchronous runs: teacher calls and second looks in flight at once
    max_per_run: int = 400           # synchronous runs: a backlog drains at this rate per run
    batch_max_per_run: int = 1200    # batch runs: articles submitted per run
    max_age_days: int | None = 14    # older unread articles are never scored; `triage bankruptcy` is for them
    rejudge_min_score: float | None = 50.0   # an article judged by another model (a blast with a faster one, say)
                                             # scoring at least this is judged again by the teacher
    opening_chars: int = 1200
    second_look_window: int = 10
    second_look_chars: int = 6000

    @field_validator("weights")
    @classmethod
    def _weights_sum(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"scoring.weights must sum to 1.0, got {total:.3f}")
        return v


class Cut(BaseModel):
    enforce: bool = True            # false = shadow mode: score and tag, cut nothing
    target_unread: int = 40
    default_floor: int = 35
    max_cuts_per_run: int = 500
    concept_cap: float = 0.25
    exploration_share: float = 0.20
    must_read_stream: str = "Must read"
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


class Tags(BaseModel):
    buckets: dict[int, str] = Field(
        default_factory=lambda: {90: "score-90", 70: "score-70", 50: "score-50", 30: "score-30", 0: "score-0"}
    )
    cut: str = "cut"
    wrong: str = "wrong"
    meh: str = "meh"
    prune: str = "prune"

    def bucket_for(self, score: float) -> str:
        for threshold in sorted(self.buckets, reverse=True):
            if score >= threshold:
                return self.buckets[threshold]
        return self.buckets[min(self.buckets)]


class Breakers(BaseModel):
    max_cut_share: float = 0.95
    min_new_for_breaker: int = 50
    rescue_rate_week: float = 0.05
    lowered_floor: int = 25
    bulk_dismiss_threshold: int = 30


class Feedback(BaseModel):
    concept_window_days: int = 90


class Config(BaseModel):
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
    bazqux_email: str = ""
    bazqux_password: str = ""
    anthropic_api_key: str = ""
    healthcheck_url: str = ""


def find_root(start: Path | None = None) -> Path:
    """The project root: the first ancestor holding config.yaml, else cwd."""
    env_root = os.environ.get("TRIAGE_HOME")
    if env_root:
        return Path(env_root).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config.yaml").exists():
            return candidate
    return here


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    path = root / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config(**raw)
    cfg.root = root
    (cfg.root / cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg


def load_secrets(root: Path | None = None) -> Secrets:
    root = root or find_root()
    # the project's own .env wins over whatever the shell happens to export (a stale key in ~/.zshrc, say)
    load_dotenv(root / ".env", override=True)
    return Secrets(
        bazqux_email=os.environ.get("BAZQUX_EMAIL", ""),
        bazqux_password=os.environ.get("BAZQUX_PASSWORD", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL", ""),
    )
