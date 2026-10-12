"""Configuration: config.yaml (behaviour) + .env (secrets). Validated at start;
a run refuses to do anything with a broken config.

Every field carries its help and its place in the Settings window as `Field` metadata
(`description`, and `json_schema_extra` with `group`, `control`, `unit`, `advanced`, `choices`),
so `bodh settings schema`, `CONFIG.md` and the Mac app's panes read one source and cannot
disagree (`bodh/settings.py`)."""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator, model_validator

from .errors import ConfigError

log = logging.getLogger(__name__)

# the sentence in profile.example/about.md's opening comment; `bodh doctor` and the judge
# look for it, so the example is never mistaken for a student
EXAMPLE_ABOUT_MARKER = "worked example for an invented student"


def ui(
    group: str,
    control: str | None = None,
    unit: str | None = None,
    advanced: bool = False,
    choices: list[str] | None = None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """The Settings metadata of one field: the pane it belongs to (`group`), the control the
    app draws (derived from the type when not given), the unit shown after the value, whether
    it hides behind Advanced, and the choices of a picker."""
    extra: dict[str, Any] = {"group": group, "advanced": advanced}
    if control:
        extra["control"] = control
    if unit:
        extra["unit"] = unit
    if choices:
        extra["choices"] = choices
    if columns:
        extra["columns"] = columns
    return extra


class Strict(BaseModel):
    """Every section: a key that is not known is a typo, and a typo must not load as a default."""

    model_config = ConfigDict(extra="forbid")


class FolderPolicy(Strict):
    protected: bool = Field(
        default=False,
        description="Never cut: articles in this folder stay unread whatever their score.",
        json_schema_extra=ui("folders"),
    )
    cutoff: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="This folder's own cut-off; empty means the default (autocut.cutoff).",
        json_schema_extra=ui("folders", unit="points"),
    )
    judge: bool = Field(
        default=True,
        description="Sent to the judge at all. Off for folders whose articles are never reading material (comics, say): never judged, never cut.",
        json_schema_extra=ui("folders"),
    )


class Models(Strict):
    """The roles, each a `provider/model-id` string (`anthropic/claude-sonnet-5-5`, `openai/gpt-5`,
    `gemini/gemini-2.5-pro`, `openrouter/qwen/qwen3-235b`, `openai-compatible/llama3.3`); a bare
    model id means Anthropic, as before October 2026."""

    judge: str = Field(
        default="claude-sonnet-5-5",
        description="Reads every article once: the judgment. Changing it re-judges every unread article another model rated catch_up.rejudge_at or more; `bodh models compare` tries a model first.",
        json_schema_extra=ui("models", control="model"),
    )
    reviewer: str = Field(
        default="claude-opus-5-5",
        description="Re-reads close calls with the full text; it can only save an article, never cut one.",
        json_schema_extra=ui("models", control="model"),
    )
    profiler: str = Field(
        default="claude-opus-5-5",
        description="Rewrites the learned profile on Sundays.",
        json_schema_extra=ui("models", control="model"),
    )
    batch: bool = Field(
        default=True,
        description="Message Batches API where the provider has one: half price, judgments arrive on the next run. Off: every call at once, at full price.",
        json_schema_extra=ui("models"),
    )
    batch_discount: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="What the API charges for a batch, as a share of the full price; for the spend figures.",
        json_schema_extra=ui("models", advanced=True),
    )
    cache_ttl: str = Field(
        default="1h",
        description="How long the judge's cached prompt prefix stays warm between runs. An hour costs more to write and less to read: a batch may take the hour, and the next run reads the same prefix.",
        json_schema_extra=ui("models", control="choice", choices=["5m", "1h"], advanced=True),
    )
    daily_budget_usd: float = Field(
        default=8.0,
        ge=0.0,
        description="Judging pauses when a day's spend passes this; nothing is cut because of it.",
        json_schema_extra=ui("providers", unit="USD a day"),
    )
    est_usd_per_article: float = Field(
        default=0.001,
        ge=0.0,
        description="The budget check before a batch is submitted, before its usage is known.",
        json_schema_extra=ui("providers", unit="USD", advanced=True),
    )
    prices_per_mtok: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "claude-sonnet-5-5": [2.0, 10.0, 0.10],
            "claude-opus-5-5": [4.0, 20.0, 0.20],
            "claude-haiku-5-5": [0.10, 0.50, 0.01],
            "claude-haiku-4-5": [1.0, 5.0, 0.10],
        },
        description="USD per million tokens: input, output and cache read, per model (a bare id for Anthropic, provider/model-id for the rest). A model not listed is charged as the dearest one here, with a warning.",
        json_schema_extra=ui("providers", control="prices", columns=["input", "output", "cache read"]),
    )

    @field_validator("cache_ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError("models.cache_ttl must be '5m' or '1h'")
        return v

    @field_validator("judge", "reviewer", "profiler")
    @classmethod
    def _model(cls, v: str) -> str:
        from .providers.base import ModelRef

        ModelRef.parse(v)  # a bad provider name or an empty model is refused at load
        return v.strip()

    @field_validator("prices_per_mtok")
    @classmethod
    def _prices(cls, v: dict[str, list[float]]) -> dict[str, list[float]]:
        from .providers.base import ModelRef

        out: dict[str, list[float]] = {}
        for model, val in v.items():
            if len(val) not in (2, 3) or any(x < 0 for x in val):
                raise ValueError(
                    f"models.prices_per_mtok.{model} must be [input, output] or [input, output, cache read], "
                    "USD per million tokens"
                )
            out[ModelRef.parse(model).name] = val  # keyed as the database stores the model
        return out

    def _entry(self, model: str) -> list[float] | None:
        from .providers.base import model_name

        name = model_name(model)
        for key, val in self.prices_per_mtok.items():
            if name.startswith(key):
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
            log.warning(
                "models: no price for %r in models.prices_per_mtok; charging it as the most expensive known "
                "model (%s) until one is added",
                model,
                dearest,
            )
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


class Judge(Strict):
    """What the judge reads and how its judgment becomes a score."""

    prompt_version: str = Field(
        default="teacher.v2",
        description="The judge's prompt in the package's prompts folder (a prompts/ folder next to config.yaml overrides it). Changing it re-judges everything unread.",
        json_schema_extra=ui("judging", advanced=True),
    )
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "quality": 0.30,
            "learning": 0.30,
            "fit": 0.15,
            "breadth": 0.15,
            "relevance": 0.10,
        },
        description="How the five sub-scores become the score; they must sum to 1.",
        json_schema_extra=ui("judging", control="weights"),
    )
    expansion_bonus: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Points for an article on one of this week's expansion topics (from the learned profile).",
        json_schema_extra=ui("judging", unit="points"),
    )
    home_angle: str = Field(
        default="",
        description='Your home ground ("India", "Brazil", "the Pacific Northwest"): an article with that angle gets the home bonus. Empty turns it off.',
        json_schema_extra=ui("judging"),
    )
    home_bonus: int = Field(
        default=10,
        ge=0,
        le=100,
        description="The points a home-angle article gets.",
        json_schema_extra=ui("judging", unit="points"),
    )
    articles_per_call: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Articles in one judge call.",
        json_schema_extra=ui("models", unit="articles", advanced=True),
    )
    text_chars: int = Field(
        default=1200,
        ge=100,
        description="The opening of each article the judge sees on the first pass.",
        json_schema_extra=ui("models", unit="characters", advanced=True),
    )

    @field_validator("home_angle")
    @classmethod
    def _home_angle(cls, v: str) -> str:
        return v.strip()  # "  " is off, like ""

    @field_validator("weights")
    @classmethod
    def _weights_sum(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"judge.weights must sum to 1.0, got {total:.3f}")
        return v


class Review(Strict):
    """The second reading of a close call by the reviewer, with the full text."""

    window: int = Field(
        default=10,
        ge=0,
        le=100,
        description="An autocut candidate within this many points of the cut-off is reviewed; 0 turns the review off.",
        json_schema_extra=ui("cutting", unit="points"),
    )
    chars: int = Field(
        default=6000,
        ge=100,
        description="The text the reviewer sees; a review happens only when the article has more than the opening.",
        json_schema_extra=ui("models", unit="characters", advanced=True),
    )
    max_per_run: int = Field(
        default=50,
        ge=0,
        description="Synchronous runs: at most this many reviews per run, so a backlog of close calls cannot eat the run's fifteen minutes.",
        json_schema_extra=ui("models", unit="reviews", advanced=True),
    )


class CatchUp(Strict):
    """One deliberate pass over everything pending, usually with the quick judge."""

    model: str = Field(
        default="claude-haiku-5-5",
        description="The quick judge `bodh catch-up` uses unless told otherwise (provider/model-id).",
        json_schema_extra=ui("models", control="model"),
    )
    rejudge_at: float | None = Field(
        default=50.0,
        ge=0,
        le=100,
        description="What the quick judge rated at least this, the judge reads again; empty: never. What it rated below keeps the quick judge's verdict: a catch-up's junk is final.",
        json_schema_extra=ui("models", unit="points"),
    )

    @field_validator("model")
    @classmethod
    def _model(cls, v: str) -> str:
        from .providers.base import ModelRef

        ModelRef.parse(v)
        return v.strip()


class Limits(Strict):
    per_run_sync: int = Field(
        default=400,
        ge=0,
        description="Synchronous runs: articles judged per run.",
        json_schema_extra=ui("models", unit="articles", advanced=True),
    )
    per_run_batch: int = Field(
        default=1200,
        ge=0,
        description="Batch runs: articles submitted per run.",
        json_schema_extra=ui("models", unit="articles", advanced=True),
    )
    parallel_calls: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Synchronous runs: judge calls and reviews running at once.",
        json_schema_extra=ui("models", unit="calls", advanced=True),
    )
    max_age_days: int | None = Field(
        default=14,
        ge=0,
        description="Unread articles older than this are never judged; `bodh clear-old` is for them. Empty: no age limit.",
        json_schema_extra=ui("cutting", unit="days"),
    )


class Autocut(Strict):
    """The automatic cut every run makes below the cut-off, when it is enabled."""

    enabled: bool = Field(
        default=False,
        description="Off is scoring-only mode: judge and tag, cut nothing. On: every run marks read what scores under its folder's cut-off, within the safety limits.",
        json_schema_extra=ui("cutting"),
    )
    cutoff: int = Field(
        default=35,
        ge=0,
        le=100,
        description="The cut-off for folders without their own.",
        json_schema_extra=ui("cutting", unit="points"),
    )
    max_per_run: int = Field(
        default=500,
        ge=0,
        description="Most cuts in one run: a broken prompt costs one undo, not a library.",
        json_schema_extra=ui("cutting", unit="articles"),
    )
    new_ground_share: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="The share of what autocut leaves that is kept for new ground (a concept you have not read).",
        json_schema_extra=ui("cutting", unit="share"),
    )


class Prune(Strict):
    """The student's own cut."""

    keep: int = Field(
        default=40,
        ge=0,
        description="`bodh prune` with no arguments keeps this many.",
        json_schema_extra=ui("cutting", unit="articles"),
    )
    max_share_per_concept: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="No concept may hold more than this share of the survivors.",
        json_schema_extra=ui("cutting", unit="share"),
    )


class Protect(Strict):
    must_read_stream: str = Field(
        default="Must read",
        min_length=1,
        description="The BazQux smart stream whose unread members are never cut (README: how to create it).",
        json_schema_extra=ui("account"),
    )


class Tags(Strict):
    bands: dict[int, str] = Field(
        default_factory=lambda: {
            90: "score-90",
            70: "score-70",
            50: "score-50",
            30: "score-30",
            0: "score-0",
        },
        description="Score band to tag name: the tag the judge writes for a score at or above each threshold.",
        json_schema_extra=ui("learning", control="bands"),
    )
    cut: str = Field(
        default="cut",
        description="On every article bodh marked read; mark it unread anywhere to rescue it.",
        json_schema_extra=ui("learning", control="tag"),
    )
    # commands: the run acts on them and removes them
    rescue: str = Field(
        default="rescue",
        description="On a cut article you wanted: back to unread, never cut again (the old name `wrong` is still read).",
        json_schema_extra=ui("learning", control="tag"),
    )
    prune: str = Field(
        default="prune",
        description="On any article: the next run with autocut on prunes to prune.keep.",
        json_schema_extra=ui("learning", control="tag"),
    )
    # verdicts: your opinion of an article, left on it as your record, recorded once
    great: str = Field(
        default="great",
        description="The best kind: more like this; the Sunday rewrite may not lose it.",
        json_schema_extra=ui("learning", control="tag"),
    )
    good: str = Field(
        default="good", description="Worth the time.", json_schema_extra=ui("learning", control="tag")
    )
    meh: str = Field(
        default="meh", description="Not worth it.", json_schema_extra=ui("learning", control="tag")
    )
    bad: str = Field(
        default="bad",
        description="Junk; the judge was wrong to keep it.",
        json_schema_extra=ui("learning", control="tag"),
    )
    basic: str = Field(
        default="basic",
        description="Too elementary for you.",
        json_schema_extra=ui("learning", control="tag"),
    )
    heavy: str = Field(
        default="heavy",
        description="Too dense or too specialist for you now.",
        json_schema_extra=ui("learning", control="tag"),
    )
    more: str = Field(
        default="more",
        description="More of this article's concept, from the next run.",
        json_schema_extra=ui("learning", control="tag"),
    )
    less: str = Field(
        default="less", description="Less of it.", json_schema_extra=ui("learning", control="tag")
    )

    @model_validator(mode="after")
    def _distinct(self) -> Tags:
        if not self.bands:
            raise ValueError("tags.bands must name at least one band")
        names = [*self.bands.values(), self.cut, self.rescue, self.prune, *self.verdict_tags().values()]
        if any(not n or not n.strip() for n in names):
            raise ValueError("every tag name must be non-empty")
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"tag names must be distinct; used more than once: {dupes}")
        return self

    def verdict_tags(self) -> dict[str, str]:
        """feedback kind -> tag name, for the tags that carry an opinion."""
        return {
            k: getattr(self, k) for k in ("great", "good", "meh", "bad", "basic", "heavy", "more", "less")
        }

    def band_for(self, score: float) -> str:
        for threshold in sorted(self.bands, reverse=True):
            if score >= threshold:
                return self.bands[threshold]
        return self.bands[min(self.bands)]

    def rescue_tags(self) -> list[str]:
        """The rescue tag and, for as long as people may have it on articles, its old name."""
        return [self.rescue] + ([LEGACY_RESCUE_TAG] if self.rescue != LEGACY_RESCUE_TAG else [])


LEGACY_RESCUE_TAG = "wrong"  # the rescue tag's name before October 2026; read forever, written never


class Safety(Strict):
    """The limits that stop a broken judge from cutting."""

    max_cut_share: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="A run that would cut more than this share of at least min_judged newly judged articles stops before writing, and autocut stays off until `bodh safety reset`.",
        json_schema_extra=ui("cutting", unit="share"),
    )
    min_judged: int = Field(
        default=50,
        ge=1,
        description="The fewest newly judged articles the share above is measured on.",
        json_schema_extra=ui("cutting", unit="articles"),
    )
    rescue_rate_week: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Rescues above this share of a week's cuts (20 cuts at least) lower the cut-off to the lowered value until they fall.",
        json_schema_extra=ui("cutting", unit="share"),
    )
    lowered_cutoff: int = Field(
        default=25,
        ge=0,
        le=100,
        description="The cut-off while the rescue rate is high.",
        json_schema_extra=ui("cutting", unit="points"),
    )
    mark_all_read_threshold: int = Field(
        default=30,
        ge=1,
        description="This many kept articles going read in one run is mark-all-as-read, not that many reads.",
        json_schema_extra=ui("cutting", unit="articles", advanced=True),
    )


class Profile(Strict):
    concept_window_days: int = Field(
        default=90,
        ge=1,
        description="How far back stars, reads and verdicts count as what you know.",
        json_schema_extra=ui("learning", unit="days"),
    )


def _zone_name_ok(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return False
    return True


def _after_zoneinfo(path: str) -> str | None:
    """…/zoneinfo/Europe/Lisbon → Europe/Lisbon (the posix/ and right/ variants too)."""
    parts = Path(path).parts
    if "zoneinfo" not in parts:
        return None
    tail = list(parts[len(parts) - 1 - parts[::-1].index("zoneinfo") + 1 :])
    if tail and tail[0] in ("posix", "right"):
        tail = tail[1:]
    return "/".join(tail) or None


def _localtime_name() -> str | None:
    """Where /etc/localtime points, as an IANA name (macOS keeps its copy under
    /var/db/timezone/zoneinfo). A copied file instead of a link: Debian's /etc/timezone."""
    try:
        name = _after_zoneinfo(str(Path("/etc/localtime").resolve()))
        if name:
            return name
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        return text or None
    except (OSError, ValueError, RuntimeError):  # missing, unreadable, not text, a link loop
        return None


def system_timezone() -> str:
    """The machine's zone: $TZ when it names one (a name, or a path into a zoneinfo tree),
    else /etc/localtime, else UTC (Windows has neither; a `timezone` line in config.yaml is
    the way there)."""
    env = (os.environ.get("TZ") or "").lstrip(":")
    if "zoneinfo" in env:
        env = _after_zoneinfo(env) or ""
    for candidate in (env, _localtime_name()):
        if _zone_name_ok(candidate):
            return str(candidate)
    return "UTC"


class Config(Strict):
    timezone: str = Field(
        default_factory=system_timezone,
        description="IANA name (Europe/Lisbon, say): the day boundary for the budget and the daily counts. Empty: this computer's zone.",
        json_schema_extra=ui("schedule", control="timezone"),
    )
    data_dir: Path = Field(
        default=Path("./data"),
        description="The database, the run logs, progress.json and the lock; relative to config.yaml.",
        json_schema_extra=ui("data", control="path", advanced=True),
    )
    profile_dir: Path = Field(
        default=Path("./profile"),
        description="about.md (yours) and learned.md (rewritten weekly); its own git repository.",
        json_schema_extra=ui("data", control="path", advanced=True),
    )
    dashboard_dir: Path = Field(
        default=Path("./dashboard"),
        description="The exported dashboard page, the weekly note and the feed report.",
        json_schema_extra=ui("data", control="path", advanced=True),
    )
    obsidian_note: str = Field(
        default="",
        description='Also write the weekly note here (a Markdown file in an Obsidian vault, say: "~/Obsidian/Main/Bodh/Weekly.md"); empty writes it to the dashboard folder only.',
        json_schema_extra=ui("connections", control="path"),
    )
    models: Models = Field(default_factory=Models)
    judge: Judge = Field(default_factory=Judge)
    review: Review = Field(default_factory=Review)
    catch_up: CatchUp = Field(default_factory=CatchUp)
    limits: Limits = Field(default_factory=Limits)
    autocut: Autocut = Field(default_factory=Autocut)
    prune: Prune = Field(default_factory=Prune)
    protect: Protect = Field(default_factory=Protect)
    folders: dict[str, FolderPolicy] = Field(
        default_factory=dict,
        description="Your BazQux folders by their exact names, each with its own policy; a folder not listed is judged and cut at the default cut-off.",
        json_schema_extra=ui("folders", control="folders"),
    )
    safety: Safety = Field(default_factory=Safety)
    tags: Tags = Field(default_factory=Tags)
    profile: Profile = Field(default_factory=Profile)

    # populated by load_config
    root: Path = Path(".")
    _timezone_from_file: bool = PrivateAttr(default=False)
    _old_keys: list[str] = PrivateAttr(default_factory=list)
    _from_file: set[str] = PrivateAttr(default_factory=set)  # the dotted keys config.yaml set itself

    def cutoff_for(self, folder: str | None) -> int | None:
        """The automatic cut-off for a folder; None means never cut automatically."""
        policy = self.folders.get(folder or "")
        if policy is None:
            return self.autocut.cutoff
        if policy.protected:
            return None
        return policy.cutoff if policy.cutoff is not None else self.autocut.cutoff

    def is_protected(self, folder: str | None) -> bool:
        policy = self.folders.get(folder or "")
        return bool(policy and policy.protected)

    def unjudged_folders(self) -> list[str]:
        """Folders the judge never reads (``judge: false``)."""
        return [name for name, policy in self.folders.items() if not policy.judge]

    def values_in_effect(self) -> list[tuple[str, Any, str]]:
        """Every setting as (dotted key, value, source), source being "config.yaml" or "default";
        what `bodh doctor --values` prints."""
        out: list[tuple[str, Any, str]] = []

        def walk(model: BaseModel, prefix: str) -> None:
            for name in type(model).model_fields:
                if name == "root":
                    continue
                value = getattr(model, name)
                key = f"{prefix}{name}"
                if isinstance(value, BaseModel):
                    walk(value, key + ".")
                elif (
                    isinstance(value, dict)
                    and value
                    and all(isinstance(v, BaseModel) for v in value.values())
                ):
                    for sub, policy in value.items():
                        walk(policy, f"{key}.{sub}.")
                else:
                    out.append((key, value, "config.yaml" if key in self._from_file else "default"))

        walk(self, "")
        return out

    @property
    def old_keys(self) -> list[str]:
        """The old key names config.yaml still uses, as "old -> new", for `bodh doctor`."""
        return list(self._old_keys)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return system_timezone()  # "" in the file means this computer's zone, like no line at all
        if not _zone_name_ok(v):
            raise ValueError(
                f"timezone {v!r} is not a known zone (IANA names, 'Europe/Lisbon' say; empty for "
                "this computer's zone)"
            )
        return v

    @property
    def timezone_source(self) -> str:
        """Where the zone came from, for `bodh doctor`: config.yaml or this computer."""
        return "config.yaml" if self._timezone_from_file else "this computer"

    @property
    def db_path(self) -> Path:
        """data/bodh.db; an install from before the rename keeps its data/triage.db as it is."""
        data = (self.root / self.data_dir).resolve()
        old = data / "triage.db"
        new = data / "bodh.db"
        return old if old.exists() and not new.exists() else new

    def _profile_file(self, name: str, old_name: str) -> Path:
        """profile/<name>, or profile/<old_name> while an install from before the rename still has
        it under the old name (install.sh renames both on the next update)."""
        folder = (self.root / self.profile_dir).resolve()
        if not (folder / name).exists() and (folder / old_name).exists():
            return folder / old_name
        return folder / name

    @property
    def about_path(self) -> Path:
        """profile/about.md: who the student is, written by the student (was seed.md)."""
        return self._profile_file("about.md", "seed.md")

    @property
    def learned_path(self) -> Path:
        """profile/learned.md: what the profiler has learned, rewritten weekly (was learner.md)."""
        return self._profile_file("learned.md", "learner.md")

    @property
    def obsidian_path(self) -> Path | None:
        return Path(os.path.expanduser(self.obsidian_note)).resolve() if self.obsidian_note else None

    def read_about(self) -> str:
        return self.about_path.read_text(encoding="utf-8").strip() if self.about_path.exists() else ""

    def check_about(self) -> None:
        """Before any work for the student: an about file that is still the worked example is refused."""
        if self.about_is_example():
            raise ConfigError(
                "profile/about.md is still the worked example: the judge will not judge for an "
                "invented student. Replace it with your own (`bodh doctor` explains)"
            )

    def about_is_example(self) -> bool:
        """profile.example/about.md describes an invented student and says so in a comment at the
        top; a file that still carries that comment is nobody's, and the judge must not judge for
        it. The comment, not the text: a student may well keep the example's shape."""
        return EXAMPLE_ABOUT_MARKER in " ".join(self.read_about().split())  # however an editor wrapped it

    def read_learned(self) -> str:
        return self.learned_path.read_text(encoding="utf-8").strip() if self.learned_path.exists() else ""


class Secrets(BaseModel):
    """The values from .env. SecretStr: a repr, a str, an assertion message or a log line with
    %r in it shows '**********', never the value; code that needs one asks for it by name."""

    bazqux_email: SecretStr = SecretStr("")
    bazqux_password: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    openai_compatible_api_key: SecretStr = SecretStr("")
    openai_compatible_base_url: SecretStr = SecretStr("")  # where a local or self-hosted service is
    healthcheck_url: SecretStr = SecretStr("")

    def value(self, name: str) -> str:
        return str(getattr(self, name).get_secret_value())

    def is_set(self, name: str) -> bool:
        return bool(self.value(name))

    def values(self) -> list[str]:
        """The non-empty secret values, for the log redactor; never for printing. The base URL
        of a local service is not one: a log line may name it."""
        return [v for v in (self.value(n) for n in SECRET_FIELDS) if v]


SECRET_FIELDS = (
    "bazqux_email",
    "bazqux_password",
    "anthropic_api_key",
    "openai_api_key",
    "gemini_api_key",
    "openrouter_api_key",
    "openai_compatible_api_key",
    "healthcheck_url",
)


# ConfigError lives in errors.py with the other exit codes; it is imported here for the callers that
# always found it in this module


DEFAULT_HOME = "~/bodh"  # tried last, when nothing above the working directory holds a config.yaml


def find_root(start: Path | None = None) -> Path:
    """The project root: BODH_HOME, else the first ancestor of the working directory that
    holds config.yaml, else ~/bodh when it holds one. Never a guess: a run in a folder with no
    config.yaml must not invent one with default folders and cut-offs and start cutting."""
    env_root = os.environ.get("BODH_HOME")
    if not env_root and os.environ.get("TRIAGE_HOME"):
        env_root = os.environ["TRIAGE_HOME"]
        log.warning(
            "TRIAGE_HOME is the old name; set BODH_HOME instead (TRIAGE_HOME stops working in the next release)"
        )
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not (root / "config.yaml").exists():
            raise ConfigError(f"BODH_HOME is {root} but it holds no config.yaml")
        return root
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config.yaml").exists():
            return candidate
    default = Path(DEFAULT_HOME).expanduser()
    if (default / "config.yaml").exists():
        return default.resolve()
    raise ConfigError(
        f"no config.yaml in {here} or above it, and none in {DEFAULT_HOME}; "
        "`bodh init` makes a new project; for an existing one, run from its folder or set BODH_HOME"
    )


# The key names before October 2026 and where each moved. Read for one release: a file that
# still uses them loads with a warning, and `bodh doctor` lists every one with its new name.
OLD_KEYS: dict[tuple[str, str], tuple[str | None, str]] = {
    ("models", "teacher"): ("models", "judge"),
    ("models", "second_look"): ("models", "reviewer"),
    ("models", "learner"): ("models", "profiler"),
    ("scoring", "prompt_version"): ("judge", "prompt_version"),
    ("scoring", "weights"): ("judge", "weights"),
    ("scoring", "expansion_bonus"): ("judge", "expansion_bonus"),
    ("scoring", "home_angle"): ("judge", "home_angle"),
    ("scoring", "home_bonus"): ("judge", "home_bonus"),
    ("scoring", "batch_size"): ("judge", "articles_per_call"),
    ("scoring", "opening_chars"): ("judge", "text_chars"),
    ("scoring", "second_look_window"): ("review", "window"),
    ("scoring", "second_look_chars"): ("review", "chars"),
    ("scoring", "second_look_max_per_run"): ("review", "max_per_run"),
    ("scoring", "rejudge_min_score"): ("catch_up", "rejudge_at"),
    ("scoring", "max_per_run"): ("limits", "per_run_sync"),
    ("scoring", "batch_max_per_run"): ("limits", "per_run_batch"),
    ("scoring", "workers"): ("limits", "parallel_calls"),
    ("scoring", "max_age_days"): ("limits", "max_age_days"),
    ("cut", "enforce"): ("autocut", "enabled"),
    ("cut", "default_floor"): ("autocut", "cutoff"),
    ("cut", "max_cuts_per_run"): ("autocut", "max_per_run"),
    ("cut", "exploration_share"): ("autocut", "new_ground_share"),
    ("cut", "target_unread"): ("prune", "keep"),
    ("cut", "concept_cap"): ("prune", "max_share_per_concept"),
    ("cut", "must_read_stream"): ("protect", "must_read_stream"),
    ("cut", "folders"): (None, "folders"),
    ("breakers", "max_cut_share"): ("safety", "max_cut_share"),
    ("breakers", "min_new_for_breaker"): ("safety", "min_judged"),
    ("breakers", "rescue_rate_week"): ("safety", "rescue_rate_week"),
    ("breakers", "lowered_floor"): ("safety", "lowered_cutoff"),
    ("breakers", "bulk_dismiss_threshold"): ("safety", "mark_all_read_threshold"),
    ("tags", "buckets"): ("tags", "bands"),
    ("tags", "wrong"): ("tags", "rescue"),
    ("feedback", "concept_window_days"): ("profile", "concept_window_days"),
}
OLD_FOLDER_KEYS = {"auto_cut_below": "cutoff", "score": "judge"}
OLD_SECTIONS = ("scoring", "cut", "breakers", "feedback")


def modernise_keys(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The mapping with every old key name moved to its new place. Returns it with the list of
    moves made ("cut.enforce -> autocut.enabled"). A key given under both names is an error:
    the file must say one thing."""
    import copy

    data = copy.deepcopy(raw)
    moves: list[str] = []

    def _place(section: str | None, key: str, value: Any, origin: str) -> None:
        target = data if section is None else data.setdefault(section, {})
        if not isinstance(target, dict):
            raise ConfigError(
                f"config.yaml: `{section}` must be a mapping to hold `{key}` (moved from `{origin}`)"
            )
        if key in target:
            raise ConfigError(
                f"config.yaml sets both `{origin}` and its new name "
                f"`{section + '.' if section else ''}{key}`; keep one"
            )
        target[key] = value

    for (old_section, old_key), (section, key) in OLD_KEYS.items():
        holder = data.get(old_section)
        if isinstance(holder, dict) and old_key in holder:
            value = holder.pop(old_key)
            _place(section, key, value, f"{old_section}.{old_key}")
            moves.append(f"{old_section}.{old_key} -> {section + '.' if section else ''}{key}")
    for old_section in OLD_SECTIONS:
        holder = data.get(old_section)
        if isinstance(holder, dict) and not holder:
            del data[old_section]
        elif holder is not None:
            keys = ", ".join(sorted(holder)) if isinstance(holder, dict) else str(holder)
            raise ConfigError(
                f"config.yaml: the section `{old_section}` is gone (its keys moved; see docs/glossary.md "
                f"and CHANGELOG.md); these are not known: {keys}"
            )
    folders = data.get("folders")
    if isinstance(folders, dict):
        for name, policy in folders.items():
            if not isinstance(policy, dict):
                continue
            for old_key, key in OLD_FOLDER_KEYS.items():
                if old_key in policy:
                    if key in policy:
                        raise ConfigError(
                            f"config.yaml: folder {name!r} sets both `{old_key}` and `{key}`; keep one"
                        )
                    policy[key] = policy.pop(old_key)
                    moves.append(f"folders.{name}.{old_key} -> folders.{name}.{key}")
    return data, moves


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    path = root / "config.yaml"
    if not path.exists():
        raise ConfigError(f"no config.yaml in {root}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold a mapping of sections, not {type(raw).__name__}")
    raw, old_keys = modernise_keys(raw)
    if old_keys:
        shown = ", ".join(old_keys[:3]) + (f" and {len(old_keys) - 3} more" if len(old_keys) > 3 else "")
        log.warning(
            "config.yaml uses %d old key name(s): %s; `bodh doctor` lists them all, and they stop "
            "working in the next release",
            len(old_keys),
            shown,
        )
    cfg = Config(**raw)
    cfg.root = root
    cfg._timezone_from_file = bool(str(raw.get("timezone") or "").strip())
    cfg._old_keys = old_keys
    cfg._from_file = _dotted_keys(raw)
    (cfg.root / cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg


def _dotted_keys(raw: dict[str, Any], prefix: str = "") -> set[str]:
    """Every leaf key of a mapping, dotted: {"judge": {"weights": {...}}} gives "judge.weights"."""
    out: set[str] = set()
    for key, value in raw.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and name not in ("judge.weights", "tags.bands", "models.prices_per_mtok"):
            out |= _dotted_keys(value, name + ".")
        else:
            out.add(name)
    return out


KEYCHAIN_SERVICE = "bodh"


def load_secrets(root: Path | None = None) -> Secrets:
    """The secrets, in this order: the project's `.env`, then the environment, then the
    keychain (`pip install bodh[keychain]`; the macOS Keychain, the Linux secret service or
    the Windows Credential Manager, under the service "bodh" and the variable's name)."""
    root = root or find_root()
    # the project's own .env wins over whatever the shell happens to export (a stale key in ~/.zshrc, say)
    load_dotenv(root / ".env", override=True)
    values: dict[str, str] = {}
    for name in (*SECRET_FIELDS, "openai_compatible_base_url"):
        values[name] = os.environ.get(name.upper(), "") or keychain_get(name.upper()) or ""
    return Secrets(**{name: SecretStr(value) for name, value in values.items()})


def keychain_get(env_name: str) -> str | None:
    """A value from the keychain, or None: without `keyring` installed, without a backend, or
    with nothing stored, the same None."""
    try:
        import keyring

        value = keyring.get_password(KEYCHAIN_SERVICE, env_name)
        return str(value) if value else None
    except Exception:  # no keyring, no backend, a locked keychain: the other sources still answer
        return None


def keychain_set(env_name: str, value: str) -> None:
    """Store a value in the keychain; raises ConfigError when there is no keyring to store it in."""
    try:
        import keyring
    except ImportError:
        raise ConfigError("the keychain needs `pip install bodh[keychain]` (the keyring package)") from None
    try:
        keyring.set_password(KEYCHAIN_SERVICE, env_name, value)
    except Exception as exc:
        raise ConfigError(f"the keychain refused the value: {exc}") from exc


def write_env_value(root: Path, env_name: str, value: str) -> Path:
    """Set one `NAME=value` line in `<root>/.env` (replacing an existing one, keeping every
    other line and comment), with the file readable by its owner alone."""
    path = root / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == env_name:
            lines[i] = f"{env_name}={value}"
            written = True
    if not written:
        lines.append(f"{env_name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # a file system without modes: the file is still written
        os.chmod(path, 0o600)
    return path
