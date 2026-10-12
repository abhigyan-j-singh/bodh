"""Settings: everything config.yaml can say, from one source. The pydantic models in
`config.py` carry each field's help and its place in the Settings window; this module turns
them into the schema the Mac app draws its panes from (`bodh settings schema`, `GET
/api/settings/schema`), into `CONFIG.md`, and into a writer that changes one key in
config.yaml with the comments and the order of the file kept (`bodh settings set`, `PUT
/api/settings`). A value is validated by the same models before anything is written, so a
bad one is refused with the key named and the file untouched."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin

import yaml
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import OLD_FOLDER_KEYS, OLD_KEYS, Config, FolderPolicy, modernise_keys
from .errors import ConfigError

SCHEMA_VERSION = 1  # the settings schema's own version: moves when a control type or a group changes meaning

# the panes, in the order the window lists them; a group not here is a mistake the test catches
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("account", "Account", "The reader you sign in to, and the stream that protects articles."),
    (
        "folders",
        "Folders",
        "Your BazQux folders: which are judged, which are never cut, and each one's cut-off.",
    ),
    ("models", "Models", "Which model fills each role, and how much each run gives it."),
    ("providers", "Providers and keys", "The daily budget and the price of every model."),
    ("judging", "Judging", "How a judgment becomes a score."),
    ("cutting", "Cutting", "Scoring-only or cutting, the cut-off, the prune, and the safety limits."),
    ("learning", "Learning", "How far back your reading counts, and the tags bodh writes and reads."),
    ("schedule", "Schedule", "The clock the day runs by."),
    ("connections", "Connections", "Where else the weekly note goes."),
    ("data", "Privacy and data", "Where the data lives."),
)
GROUP_KEYS = tuple(g[0] for g in GROUPS)

SECTIONS = (
    "models",
    "judge",
    "review",
    "catch_up",
    "limits",
    "autocut",
    "prune",
    "protect",
    "tags",
    "safety",
    "profile",
)
SCALAR_TOP = ("timezone", "data_dir", "profile_dir", "dashboard_dir", "obsidian_note")
WHOLE_VALUE_KEYS = ("judge.weights", "tags.bands", "models.prices_per_mtok", "folders")  # set as one value


class SettingsError(ConfigError):
    """A change the models refuse; `errors` names each key with the reason, for a form."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e['key']}: {e['message']}" for e in errors) or "invalid settings")


@dataclass
class Setting:
    """One key of config.yaml as the window shows it."""

    key: str
    title: str
    help: str
    type: str  # bool, int, float, str, path, dict
    control: str  # switch, number, text, choice, model, tag, path, timezone, weights, bands, prices, folders
    group: str
    default: Any
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    choices: list[str] | None = None
    nullable: bool = False
    advanced: bool = False
    columns: list[str] | None = None
    fields: list[Setting] = field(default_factory=list)  # a table's columns, each a setting of its own

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["default"] = _jsonable(self.default)
        if not self.fields:
            del out["fields"]
        return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump())
    return value


TITLES = {  # where the key's words are not the title's
    "daily_budget_usd": "Daily budget",
    "est_usd_per_article": "Estimated cost per article",
    "prices_per_mtok": "Prices per million tokens",
    "cache_ttl": "Prompt cache lifetime",
    "batch": "Batch mode",
    "batch_discount": "Batch discount",
    "rejudge_at": "Re-judge at",
    "per_run_sync": "Articles per synchronous run",
    "per_run_batch": "Articles per batch run",
    "max_age_days": "Oldest article judged",
    "max_per_run": "Most per run",
    "new_ground_share": "New ground share",
    "max_share_per_concept": "Largest share per concept",
    "must_read_stream": "Must-read stream",
    "max_cut_share": "Most cut in one run",
    "min_judged": "Fewest judged for the limit",
    "rescue_rate_week": "Rescue rate that lowers the cut-off",
    "lowered_cutoff": "Lowered cut-off",
    "mark_all_read_threshold": "Mark-all-read threshold",
    "concept_window_days": "Concept window",
    "obsidian_note": "Weekly note path",
    "data_dir": "Data folder",
    "profile_dir": "Profile folder",
    "dashboard_dir": "Dashboard folder",
    "home_angle": "Home angle",
    "home_bonus": "Home bonus",
    "text_chars": "Text the judge sees",
    "chars": "Text the reviewer sees",
    "window": "Review window",
    "cutoff": "Cut-off",
    "enabled": "Cut below the cut-off",
    "keep": "Keep",
    "judge": "Judge",
    "protected": "Never cut",
}


def _title(name: str) -> str:
    if name in TITLES:
        return TITLES[name]
    words = name.replace("_", " ").split()
    if not words:
        return name
    return words[0][:1].upper() + words[0][1:] + (" " + " ".join(words[1:]) if len(words) > 1 else "")


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """(the type, nullable): `int | None` -> (int, True)."""
    origin = get_origin(annotation)
    if origin is not None and type(None) in get_args(annotation):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        return (inner[0] if len(inner) == 1 else annotation), True
    return annotation, False


def _type_name(annotation: Any) -> str:
    base, _ = _unwrap(annotation)
    if base is bool:
        return "bool"
    if base is int:
        return "int"
    if base is float:
        return "float"
    if base is Path:
        return "path"
    if get_origin(base) is dict or base is dict:
        return "dict"
    return "str"


def _bounds(info: FieldInfo) -> tuple[float | None, float | None]:
    lo = hi = None
    for m in info.metadata:
        lo = getattr(m, "ge", lo) if hasattr(m, "ge") else lo
        hi = getattr(m, "le", hi) if hasattr(m, "le") else hi
    return lo, hi


def _default(key: str, info: FieldInfo) -> Any:
    if key == "timezone":
        return ""  # the factory answers with this computer's zone; the file's default is "no line"
    if info.default_factory is not None:
        return info.default_factory()  # type: ignore[call-arg]
    return info.default


def _setting(key: str, info: FieldInfo) -> Setting:
    extra: dict[str, Any] = dict(info.json_schema_extra or {})  # type: ignore[arg-type]
    type_name = _type_name(info.annotation)
    _, nullable = _unwrap(info.annotation)
    control = extra.get("control") or {
        "bool": "switch",
        "int": "number",
        "float": "number",
        "path": "path",
        "dict": "table",
    }.get(type_name, "text")
    lo, hi = _bounds(info)
    s = Setting(
        key=key,
        title=_title(key.rsplit(".", 1)[-1]),
        help=info.description or "",
        type=type_name,
        control=str(control),
        group=str(extra.get("group", "")),
        default=_default(key, info),
        unit=extra.get("unit"),
        min=lo,
        max=hi,
        choices=extra.get("choices"),
        nullable=nullable,
        advanced=bool(extra.get("advanced", False)),
        columns=extra.get("columns"),
    )
    if key == "folders":
        s.fields = [_setting(name, f) for name, f in FolderPolicy.model_fields.items()]
    return s


def schema() -> list[Setting]:
    """Every key of config.yaml, in the file's order: the top-level scalars, then each section's
    fields, then the folders table."""
    out: list[Setting] = []
    for name in SCALAR_TOP:
        out.append(_setting(name, Config.model_fields[name]))
    for section in SECTIONS:
        model = Config.model_fields[section].annotation
        assert isinstance(model, type) and issubclass(model, BaseModel)
        for name, info in model.model_fields.items():
            out.append(_setting(f"{section}.{name}", info))
    out.append(_setting("folders", Config.model_fields["folders"]))
    for s in out:  # a field placed in no pane would be invisible in the window: refuse at import time
        if s.group not in GROUP_KEYS:
            raise RuntimeError(f"settings: {s.key} has no group (one of {GROUP_KEYS})")
    return out


def values(cfg: Config) -> dict[str, Any]:
    """Every setting's value in effect, by dotted key, the whole-value keys as one object."""
    out: dict[str, Any] = {}
    for s in schema():
        out[s.key] = _jsonable(get_value(cfg, s.key))
    return out


def get_value(cfg: Config, key: str) -> Any:
    target: Any = cfg
    for part in key.split("."):
        target = getattr(target, part)
    return target


def sources(cfg: Config) -> dict[str, str]:
    """Where each value came from: config.yaml or the default."""
    out: dict[str, str] = {}
    from_file = cfg._from_file  # the loader's record of the file's own keys
    for s in schema():
        if s.key == "folders":
            out[s.key] = "config.yaml" if any(k.startswith("folders.") for k in from_file) else "default"
        else:
            out[s.key] = "config.yaml" if s.key in from_file else "default"
    return out


def schema_json(cfg: Config | None = None) -> dict[str, Any]:
    """The object `GET /api/settings/schema` serves: the groups, every setting with its control,
    and when a configuration is given, its value and source."""
    vals = values(cfg) if cfg else {}
    srcs = sources(cfg) if cfg else {}
    settings = []
    for s in schema():
        d = s.to_dict()
        if cfg:
            d["value"] = vals[s.key]
            d["source"] = srcs[s.key]
        settings.append(d)
    return {
        "schema": 1,
        "version": SCHEMA_VERSION,
        "groups": [{"key": k, "title": t, "help": h} for k, t, h in GROUPS],
        "settings": settings,
    }


# -- writing ----------------------------------------------------------------------------


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _read_document(path: Path) -> CommentedMap:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    doc = _yaml().load(text) if text.strip() else None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ConfigError(f"{path} must hold a mapping of sections, not {type(doc).__name__}")
    return doc


def _modernise_document(doc: CommentedMap) -> list[str]:
    """Move the old key names to their new places inside the round-trip document, carrying
    each key's comment along; returns the moves made."""
    moves: list[str] = []
    for (old_section, old_key), (section, key) in OLD_KEYS.items():
        holder = doc.get(old_section)
        if not isinstance(holder, CommentedMap) or old_key not in holder:
            continue
        value = holder.pop(old_key)
        comment = holder.ca.items.pop(old_key, None)
        target = doc if section is None else doc.setdefault(section, CommentedMap())
        if not isinstance(target, CommentedMap):
            raise ConfigError(f"config.yaml: `{section}` must be a mapping to hold `{key}`")
        if key in target:
            raise ConfigError(
                f"config.yaml sets both `{old_section}.{old_key}` and `{section}.{key}`; keep one"
            )
        target[key] = value
        if comment is not None:
            target.ca.items[key] = comment
        moves.append(f"{old_section}.{old_key} -> {section + '.' if section else ''}{key}")
    for old_section in ("scoring", "cut", "breakers", "feedback"):
        holder = doc.get(old_section)
        if isinstance(holder, CommentedMap) and not holder:
            del doc[old_section]
    folders = doc.get("folders")
    if isinstance(folders, CommentedMap):
        for name, policy in folders.items():
            if not isinstance(policy, CommentedMap):
                continue
            for old_key, key in OLD_FOLDER_KEYS.items():
                if old_key in policy and key not in policy:
                    policy[key] = policy.pop(old_key)
                    moves.append(f"folders.{name}.{old_key} -> folders.{name}.{key}")
    return moves


def _to_document_value(key: str, value: Any) -> Any:
    """A JSON value as the document stores it: band thresholds back to ints, flow style for the
    small mappings the example file writes on one line."""
    if key == "tags.bands" and isinstance(value, dict):
        out = CommentedMap((int(k), str(v)) for k, v in value.items())
        out.fa.set_flow_style()
        return out
    if key == "judge.weights" and isinstance(value, dict):
        out = CommentedMap((str(k), float(v)) for k, v in value.items())
        out.fa.set_flow_style()
        return out
    if key == "folders" and isinstance(value, dict):
        out = CommentedMap()
        for name, policy in value.items():
            entry = CommentedMap((k, v) for k, v in dict(policy).items() if v is not None)
            entry.fa.set_flow_style()
            out[str(name)] = entry
        return out
    if key == "models.prices_per_mtok" and isinstance(value, dict):
        out = CommentedMap()
        for model, prices in value.items():
            out[str(model)] = [float(p) for p in prices]
        return out
    if isinstance(value, Path):
        return str(value)
    return value


def _set_in_document(doc: CommentedMap, key: str, value: Any) -> None:
    parts = key.split(".", 1)
    if len(parts) == 1:
        doc[key] = _to_document_value(key, value)
        return
    section, name = parts
    holder = doc.get(section)
    if holder is None:
        holder = CommentedMap()
        doc[section] = holder
    if not isinstance(holder, CommentedMap):
        raise SettingsError([{"key": key, "message": f"`{section}` is not a mapping in config.yaml"}])
    holder[name] = _to_document_value(key, value)


def known_keys() -> set[str]:
    return {s.key for s in schema()}


def validate_text(text: str) -> Config:
    """The configuration a config.yaml text would load as, or a SettingsError naming each key."""
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise SettingsError([{"key": "", "message": "config.yaml must hold a mapping of sections"}])
    raw, _moves = modernise_keys(raw)
    try:
        return Config(**raw)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            msg = str(err["msg"])
            if msg.startswith("Value error, "):
                msg = msg[len("Value error, ") :]
            errors.append({"key": loc, "message": msg})
        raise SettingsError(errors) from None


def apply(root: Path, changes: dict[str, Any]) -> tuple[Config, list[str]]:
    """Set the given keys in `<root>/config.yaml` (every other line and comment kept), after the
    models have accepted the result. Returns the configuration now in effect and the notes
    (old key names moved). Nothing is written when any key is refused."""
    from .config import load_config

    unknown = [k for k in changes if k not in known_keys()]
    if unknown:
        raise SettingsError(
            [{"key": k, "message": "not a setting (see `bodh settings schema`)"} for k in unknown]
        )
    path = root / "config.yaml"
    doc = _read_document(path)
    notes = _modernise_document(doc)
    for key, value in changes.items():
        _set_in_document(doc, key, value)
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    text = buf.getvalue()
    validate_text(text)  # the same models, the same refusals, before the file changes
    _write_atomic(path, text)
    cfg = load_config(root)
    return cfg, notes


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            with contextlib.suppress(OSError):  # a file system without modes: the file is still written
                os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_cli_value(text: str) -> Any:
    """`bodh settings set KEY VALUE`: the value as YAML reads it (`true`, `35`, `0.2`, `null`,
    `"Must read"`, `{quality: 0.3, ...}`), so the terminal and the file agree."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


# -- CONFIG.md --------------------------------------------------------------------------


def _range_text(s: Setting) -> str:
    parts = []
    if s.min is not None and s.max is not None:
        parts.append(f"{s.min:g} to {s.max:g}")
    elif s.min is not None:
        parts.append(f"{s.min:g} or more")
    elif s.max is not None:
        parts.append(f"up to {s.max:g}")
    if s.choices:
        parts.append(" or ".join(f"`{c}`" for c in s.choices))
    if s.nullable:
        parts.append("or empty")
    return ", ".join(parts)


def _default_text(s: Setting) -> str:
    d = _jsonable(s.default)
    if isinstance(d, dict):
        if not d:
            return "none"
        return "`" + yaml.safe_dump(d, default_flow_style=True, width=1000).strip() + "`"
    if d is None:
        return "empty"
    if isinstance(d, bool):
        return "`true`" if d else "`false`"
    if isinstance(d, str):
        return f'`"{d}"`' if d else "empty"
    return f"`{d}`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_config_md() -> str:
    """The reference generated from the models, one table per pane; `scripts/gen_config_md.py`
    writes it and the suite fails when it is stale."""
    lines = [
        "# Every setting",
        "",
        "Generated from the configuration models by `scripts/gen_config_md.py`; do not edit by hand.",
        "`config.yaml` holds these keys (the example file shows every default with a comment); the Mac",
        "app's Settings window shows the same ones, pane by pane, and `bodh settings` reads and writes",
        "them from the terminal: `bodh settings` lists every value with its source, `bodh settings set",
        "autocut.cutoff 40` changes one with the file's comments kept, `bodh settings schema --json`",
        "prints what the app draws. A key that is not here is a typo and is refused.",
        "",
    ]
    by_group: dict[str, list[Setting]] = {k: [] for k in GROUP_KEYS}
    for s in schema():
        by_group[s.group].append(s)
    for key, title, help_text in GROUPS:
        lines += [f"## {title}", "", help_text, ""]
        lines += ["| Key | Default | Range | What it does |", "| --- | --- | --- | --- |"]
        for s in by_group[key]:
            help_md = _cell(s.help)
            if s.advanced:
                help_md += " (Advanced.)"
            if s.unit and s.unit != "share":
                help_md += f" Unit: {s.unit}."
            lines.append(f"| `{s.key}` | {_default_text(s)} | {_range_text(s)} | {help_md} |")
            for col in s.fields:
                col_help = _cell(col.help)
                lines.append(
                    f"| `{s.key}.<name>.{col.key}` | {_default_text(col)} | {_range_text(col)} | {col_help} |"
                )
        lines.append("")
    return "\n".join(lines)
