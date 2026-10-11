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
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator, model_validator

log = logging.getLogger(__name__)

# the sentence in profile.example/about.md's opening comment; `bodh doctor` and the judge
# look for it, so the example is never mistaken for a student
EXAMPLE_ABOUT_MARKER = "worked example for an invented student"


class Strict(BaseModel):
    """Every section: a key that is not known is a typo, and a typo must not load as a default."""

    model_config = ConfigDict(extra="forbid")


class FolderPolicy(Strict):
    protected: bool = False  # never cut
    cutoff: int | None = Field(default=None, ge=0, le=100)  # this folder's own cut-off; None: autocut.cutoff
    judge: bool = True  # false: never sent to the judge, never cut (comics, say)


class Models(Strict):
    judge: str = "claude-sonnet-5-5"  # reads every article once
    reviewer: str = "claude-opus-5-5"  # re-reads close calls with the full text; can only save an article
    profiler: str = "claude-opus-5-5"  # rewrites the learned profile on Sundays
    batch: bool = True  # Message Batches API: half price; judgments arrive on the next run
    batch_discount: float = Field(default=0.5, ge=0.0, le=1.0)
    cache_ttl: str = "1h"  # "5m" or "1h": how long the judge's cached prefix stays warm between runs
    daily_budget_usd: float = Field(default=8.0, ge=0.0)
    est_usd_per_article: float = Field(default=0.001, ge=0.0)  # the budget check before a batch is submitted
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
                raise ValueError(
                    f"models.prices_per_mtok.{model} must be [input, output] or [input, output, cache read], "
                    "USD per million tokens"
                )
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

    prompt_version: str = "teacher.v2"  # the prompt file in src/bodh/prompts/ (./prompts/ overrides)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "quality": 0.30,
            "learning": 0.30,
            "fit": 0.15,
            "breadth": 0.15,
            "relevance": 0.10,
        }
    )
    expansion_bonus: int = Field(
        default=10, ge=0, le=100
    )  # points for an article on one of this week's expansion topics
    home_angle: str = ""  # the student's home ground ("India", say): an article
    # with that angle gets home_bonus; empty turns the flag off
    home_bonus: int = Field(default=10, ge=0, le=100)
    articles_per_call: int = Field(default=20, ge=1, le=100)  # articles in one judge call
    text_chars: int = Field(default=1200, ge=100)  # the opening the judge sees on the first pass

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
        default=10, ge=0, le=100
    )  # a candidate within this many points of the cut-off is reviewed
    chars: int = Field(default=6000, ge=100)  # the text the reviewer sees
    max_per_run: int = Field(default=50, ge=0)  # synchronous runs: reviews per run, so a backlog of close
    # calls cannot eat the run's fifteen minutes


class CatchUp(Strict):
    """One deliberate pass over everything pending, usually with the quick judge."""

    model: str = "claude-haiku-5-5"  # the quick judge `bodh catch-up` uses by default
    rejudge_at: float | None = Field(
        default=50.0, ge=0, le=100
    )  # what the quick judge rates at or above this,
    # the judge reads again; null turns it off


class Limits(Strict):
    per_run_sync: int = Field(default=400, ge=0)  # synchronous runs: articles judged per run
    per_run_batch: int = Field(default=1200, ge=0)  # batch runs: articles submitted per run
    parallel_calls: int = Field(default=4, ge=1, le=64)  # synchronous runs: model calls running at once
    max_age_days: int | None = Field(
        default=14, ge=0
    )  # older unread articles are never judged; clear-old is for them


class Autocut(Strict):
    """The automatic cut every run makes below the cut-off, when it is enabled."""

    enabled: bool = False  # false = scoring-only mode: judge and tag, cut nothing
    cutoff: int = Field(default=35, ge=0, le=100)  # the cut-off for folders without their own
    max_per_run: int = Field(default=500, ge=0)  # a broken prompt costs one undo, not a library
    new_ground_share: float = Field(
        default=0.20, ge=0.0, le=1.0
    )  # share of what autocut leaves kept for new ground


class Prune(Strict):
    """The student's own cut."""

    keep: int = Field(default=40, ge=0)  # `bodh prune` with no arguments keeps this many
    max_share_per_concept: float = Field(
        default=0.25, ge=0.0, le=1.0
    )  # no concept may hold more of the survivors


class Protect(Strict):
    must_read_stream: str = Field(default="Must read", min_length=1)  # the BazQux smart stream never cut


class Tags(Strict):
    bands: dict[int, str] = Field(
        default_factory=lambda: {90: "score-90", 70: "score-70", 50: "score-50", 30: "score-30", 0: "score-0"}
    )
    cut: str = "cut"
    # commands: the run acts on them and removes them
    rescue: str = (
        "rescue"  # a cut article you wanted: back to unread, never cut again (was `wrong`, still read)
    )
    prune: str = "prune"  # on any article: the next run with autocut prunes to prune.keep
    # verdicts: your opinion of an article, left on it as your record, recorded once
    great: str = "great"  # the best kind: more like this, and the eval gate protects it
    good: str = "good"  # worth the time
    meh: str = "meh"  # not worth it
    bad: str = "bad"  # junk; the judge was wrong to keep it
    basic: str = "basic"  # too elementary for you
    heavy: str = "heavy"  # too dense or too specialist for you now
    more: str = "more"  # more of this article's concept, from the next run
    less: str = "less"  # less of it

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
        default=0.95, ge=0.0, le=1.0
    )  # a run that would cut more than this share of...
    min_judged: int = Field(default=50, ge=1)  # ...at least this many newly judged articles stops
    rescue_rate_week: float = Field(
        default=0.05, ge=0.0, le=1.0
    )  # rescues above this share of a week's cuts...
    lowered_cutoff: int = Field(default=25, ge=0, le=100)  # ...lower the cut-off to this until they fall
    mark_all_read_threshold: int = Field(default=30, ge=1)  # this many kept articles going read in one run is
    # mark-all-as-read, not that many reads


class Profile(Strict):
    concept_window_days: int = Field(
        default=90, ge=1
    )  # how far back stars, reads and verdicts count as what you know


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
    timezone: str = Field(default_factory=system_timezone)  # the day boundary for the budget and the counts
    data_dir: Path = Path("./data")
    profile_dir: Path = Path("./profile")
    dashboard_dir: Path = Path("./dashboard")
    obsidian_note: str = ""
    models: Models = Field(default_factory=Models)
    judge: Judge = Field(default_factory=Judge)
    review: Review = Field(default_factory=Review)
    catch_up: CatchUp = Field(default_factory=CatchUp)
    limits: Limits = Field(default_factory=Limits)
    autocut: Autocut = Field(default_factory=Autocut)
    prune: Prune = Field(default_factory=Prune)
    protect: Protect = Field(default_factory=Protect)
    folders: dict[str, FolderPolicy] = Field(
        default_factory=dict
    )  # your BazQux folders, by their exact names
    safety: Safety = Field(default_factory=Safety)
    tags: Tags = Field(default_factory=Tags)
    profile: Profile = Field(default_factory=Profile)

    # populated by load_config
    root: Path = Path(".")
    _timezone_from_file: bool = PrivateAttr(default=False)
    _old_keys: list[str] = PrivateAttr(default_factory=list)

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
    healthcheck_url: SecretStr = SecretStr("")

    def value(self, name: str) -> str:
        return getattr(self, name).get_secret_value()

    def is_set(self, name: str) -> bool:
        return bool(self.value(name))

    def values(self) -> list[str]:
        """The non-empty values, for the log redactor; never for printing."""
        return [
            v
            for v in (
                self.value(n)
                for n in ("bazqux_email", "bazqux_password", "anthropic_api_key", "healthcheck_url")
            )
            if v
        ]


class ConfigError(Exception):
    """No usable config.yaml: nothing runs, nothing is created."""


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
        "run from the bodh folder, or set BODH_HOME"
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
