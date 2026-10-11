"""`bodh init`: a new project in one sitting. The directory, the BazQux login and a provider's
key (each checked live unless told not to), five questions that become profile/about.md, the
timezone; then the files are written, `doctor` runs, and the schedule is offered. Every answer
has a flag, so a script can do it without a keyboard. Nothing here touches an existing project:
a directory that already holds config.yaml is refused."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from .config import DEFAULT_HOME, Secrets, system_timezone, write_env_value
from .errors import BodhError, ConfigError
from .providers.base import PROVIDERS, REQUIRED_SECRET, ModelRef
from .templates import template_text

log = logging.getLogger(__name__)

DEFAULT_JUDGE = "claude-sonnet-5-5"

# the five paragraphs of profile/about.md, as questions
QUESTIONS = (
    (
        "who",
        "Who are you? Your work, what you run or build, what you read to decide (two or three sentences)",
    ),
    (
        "knows",
        "What do you already know well, at a practitioner level? (routine news in these teaches you nothing)",
    ),
    ("goal", "What is reading for this year? Which fields do you want to read fluently?"),
    ("decisions", "Which decisions are in front of you this quarter?"),
    ("never", "What is never worth your attention?"),
)
HEADINGS = {
    "who": "Student:",
    "knows": "Already knows well, at a practitioner level:",
    "goal": "Goal:",
    "decisions": "Decisions this quarter:",
    "never": "Never worth attention:",
}


@dataclass
class Answers:
    directory: Path
    bazqux_email: str = ""
    bazqux_password: str = ""
    provider: str = "anthropic"
    key: str = ""  # the API key, or for openai-compatible the base URL
    judge: str = ""  # provider/model-id; empty: the default for the provider
    about: dict[str, str] = field(default_factory=dict)  # the five answers by name
    timezone: str = ""
    live: bool = True
    install_schedule: bool = False


@dataclass
class Outcome:
    root: Path
    lines: list[str] = field(default_factory=list)
    doctor_ok: bool = False
    schedule: list[str] = field(default_factory=list)


def check_bazqux(email: str, password: str) -> str:
    """Log in once; returns a line to print, raises on failure."""
    from .readers.bazqux import BazQuxClient

    client = BazQuxClient(email, password)
    client.user_info()
    unread = client.unread_ids()
    return f"bazqux login: ok, {len(unread)} unread"


def check_model(provider: str, key: str, judge: str) -> str:
    """One five-token completion through the provider; returns a line, raises on failure."""
    from .providers import make_provider
    from .providers.base import FlagStore

    secrets = secrets_for(provider, key)
    found = make_provider(provider, secrets, FlagStore(lambda _k: None, lambda _k, _v: None))
    ref = ModelRef.parse(judge)
    answer = found.complete(ref.model, "Answer with one word.", "Say ok.", 5)
    if not answer.text.strip():
        raise BodhError(f"{judge} answered with nothing")
    return f"{provider} ({judge}): ok"


def secrets_for(provider: str, key: str) -> Secrets:
    """The one secret a provider needs, as a `Secrets` (the base URL for a local service)."""
    field_name = REQUIRED_SECRET[provider].lower()
    return Secrets(**{field_name: SecretStr(key)})


def default_judge(provider: str) -> str:
    return DEFAULT_JUDGE if provider == "anthropic" else ""


def validate(answers: Answers) -> None:
    """Everything a script could get wrong, before any file is written."""
    root = answers.directory.expanduser()
    if (root / "config.yaml").exists():
        raise BodhError(
            f"{root} already holds a config.yaml: this is a bodh project; `bodh doctor` checks it"
        )
    if root.exists() and any(root.iterdir()) and (root / "data").exists():
        raise BodhError(
            f"{root} holds a data folder but no config.yaml: an older install? restore its config.yaml"
        )
    if answers.provider not in PROVIDERS:
        raise BodhError(f"unknown provider {answers.provider!r}; one of {', '.join(PROVIDERS)}")
    judge = answers.judge or default_judge(answers.provider)
    if not judge:
        raise BodhError(f"name the judge for {answers.provider} as provider/model-id (--judge)")
    ref = ModelRef.parse(judge)
    if ref.provider != answers.provider:
        raise BodhError(f"the judge {judge} is not a {answers.provider} model")
    answers.judge = judge
    if answers.timezone:
        try:
            ZoneInfo(answers.timezone)
        except Exception:
            raise ConfigError(
                f"unknown timezone {answers.timezone!r} (an IANA name such as Europe/Lisbon)"
            ) from None
    else:
        answers.timezone = system_timezone()
    for name, _question in QUESTIONS:
        if not answers.about.get(name, "").strip():
            raise BodhError(f"the profile needs an answer for {name!r} (--{name})")


def about_text(about: dict[str, str]) -> str:
    """profile/about.md from the five answers, in the example's shape."""
    parts = ["# About the student", ""]
    for name, _q in QUESTIONS:
        parts.append(f"{HEADINGS[name]} {about[name].strip()}")
        parts.append("")
    return "\n".join(parts)


def config_text(answers: Answers) -> str:
    """config.example.yaml with the timezone set and, for a provider other than Anthropic, every
    role on the chosen judge (one key is all the new project has)."""
    text = template_text("config.yaml")
    text = re.sub(
        r'^# timezone: "Europe/Lisbon"(\s*)#',
        lambda m: f'timezone: "{answers.timezone}"{m.group(1)}#',
        text,
        count=1,
        flags=re.M,
    )
    if answers.provider != "anthropic":
        for key in ("judge", "reviewer", "profiler"):
            text = re.sub(rf'^(  {key}: )"[^"]*"', rf'\g<1>"{answers.judge}"', text, count=1, flags=re.M)
        text = re.sub(
            r'^(  model: )"claude-haiku-5-5"', rf'\g<1>"{answers.judge}"', text, count=1, flags=re.M
        )
    return text


def write_project(answers: Answers, say: Callable[[str], None]) -> Path:
    """The files: config.yaml, .env (owner-only), profile/about.md and learned.md, data/ and
    dashboard/. Nothing is overwritten: `validate` refused a project that exists."""
    root = answers.directory.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(config_text(answers), encoding="utf-8")
    say(f"wrote {root / 'config.yaml'} (scoring-only mode: nothing is cut until `bodh autocut on`)")
    env_path = root / ".env"
    env_path.write_text(template_text(".env"), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    write_env_value(root, "BAZQUX_EMAIL", answers.bazqux_email)
    write_env_value(root, "BAZQUX_PASSWORD", answers.bazqux_password)
    write_env_value(root, REQUIRED_SECRET[answers.provider], answers.key)
    say(f"wrote {env_path} (owner-only; the values are never printed)")
    profile = root / "profile"
    profile.mkdir(exist_ok=True)
    (profile / "about.md").write_text(about_text(answers.about), encoding="utf-8")
    (profile / "learned.md").write_text(template_text("profile/learned.md"), encoding="utf-8")
    say(f"wrote {profile / 'about.md'} from your five answers, and the starting learned profile")
    (root / "data").mkdir(exist_ok=True)
    (root / "dashboard").mkdir(exist_ok=True)
    return root


def init(
    answers: Answers,
    say: Callable[[str], None],
    bazqux_check: Callable[[str, str], str] | None = None,
    model_check: Callable[[str, str, str], str] | None = None,
) -> Outcome:
    """The whole thing, for a script: validate, check live, write, doctor, schedule. The
    command line asks its questions before calling this."""
    validate(answers)
    if answers.live:
        say((bazqux_check or check_bazqux)(answers.bazqux_email, answers.bazqux_password))
        say((model_check or check_model)(answers.provider, answers.key, answers.judge))
    root = write_project(answers, say)
    outcome = Outcome(root=root)
    from .api import App

    app = App(root, log_to_file=False)
    report = app.doctor(say=say)
    outcome.doctor_ok = report.ok
    say("doctor: ok" if report.ok else f"doctor: {len(report.problems)} problem(s); see above")
    app.close()
    if answers.install_schedule:
        from . import installers

        outcome.schedule = installers.install(root)
        for line in outcome.schedule:
            say(line)
    return outcome


def default_directory() -> Path:
    return Path(DEFAULT_HOME).expanduser()
