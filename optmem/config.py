"""OptMem configuration resolution.

Single source of truth for the provider's effective settings, shared by the
provider, the migration/readiness logic and the ``hermes optmem-hermes`` CLI.

Precedence (highest first):

1. ``<HERMES_HOME>/optmem/config.json`` — the *declared* schema. Written by the
   Hermes dashboard config panel (``storage = flat_json`` in
   ``optmem/config_schema.py``; the host resolves that to
   ``<hermes_home>/<provider>/config.json``) and by ``write_declared_config``.
2. ``config.yaml`` ``memory.optmem`` / ``plugins.optmem`` — legacy 0.2.0 keys.
3. Built-in defaults.

Rules that matter for safety:

- A value we cannot trust never becomes a *different* mode. An unknown
  ``mode`` resolves to ``hybrid`` (the native store keeps working) and records a
  diagnostic, so a typo can never silently disable the user's native memory.
- Nothing here raises on a malformed config file; the caller gets defaults plus
  diagnostics. Writers validate first and refuse before touching the file.
- Reads and writes are scoped to the ``hermes_home`` passed in — the live
  profile is never mutated by library code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODE_HYBRID = "hybrid"
MODE_OPTMEM_ONLY = "optmem-only"
MODES = (MODE_HYBRID, MODE_OPTMEM_ONLY)

RECALL_MODES = ("auto", "regex", "bm25")

DEFAULT_WAKE_BUDGET = 96
WAKE_BUDGET_MIN = 1
WAKE_BUDGET_MAX = 4096

# Provider name; the declared config lives under <hermes_home>/<name>/config.json
# to match the host's own flat_json path (web_routers/memory_providers.py).
PROVIDER_NAME = "optmem"
DECLARED_CONFIG_NAME = "config.json"

# Keys the supported config surface owns. A test pins these against
# optmem/config_schema.py so the GUI and the resolver cannot drift apart.
EDITABLE_KEYS = (
    "mode",
    "memory_dir",
    "wake_budget",
    "recall_mode",
    "llm_summary",
    "migration_split_long",
    "auto_nap",
)

_TRUE = {"true", "1", "yes", "on", "y", "t"}
_FALSE = {"false", "0", "no", "off", "n", "f", ""}


def declared_config_path(hermes_home: str | os.PathLike[str]) -> Path:
    """``<hermes_home>/optmem/config.json`` (the host's flat_json location for us)."""
    return Path(hermes_home).expanduser() / PROVIDER_NAME / DECLARED_CONFIG_NAME


def default_hermes_home() -> str:
    """Active profile home: Hermes' resolver when importable, else ``HERMES_HOME``/``~/.hermes``.

    Lives here (not in ``optmem/__init__.py``) so ``optmem/cli.py`` — loaded by
    path during argparse setup — never imports the agent runtime.
    """
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


def read_native_flags(hermes_home: str | os.PathLike[str]) -> dict[str, bool | None]:
    """``memory.memory_enabled`` / ``user_profile_enabled`` as written in config.yaml.

    ``None`` means the key is absent — the host then defaults to enabled, which
    is why "absent" and "true" are not the same thing for status reporting.
    """
    home = Path(hermes_home).expanduser()
    config_path = home / "config.yaml"
    if not config_path.exists():
        return {"memory_enabled": None, "user_profile_enabled": None}
    try:
        import yaml
    except Exception:
        return {"memory_enabled": None, "user_profile_enabled": None}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        logger.debug("could not parse %s", config_path, exc_info=True)
        return {"memory_enabled": None, "user_profile_enabled": None}
    section = data.get("memory") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return {"memory_enabled": None, "user_profile_enabled": None}
    return {
        key: _as_bool(section[key]) if key in section else None
        for key in ("memory_enabled", "user_profile_enabled")
    }


def native_memory_paths(hermes_home: str | os.PathLike[str]) -> tuple[Path, Path]:
    """The two files the built-in store actually loads: MEMORY.md and USER.md.

    Other ``.md`` files in ``<hermes_home>/memories/`` (drafts, exports) are NOT
    native memories — ``tools/memory_tool_store.py`` only reads these two names.
    """
    memories = Path(hermes_home).expanduser() / "memories"
    return memories / "MEMORY.md", memories / "USER.md"


def _as_bool(value: Any) -> bool | None:
    """Tri-state bool coercion: ``None`` means "not a usable value"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return None


def _as_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        number = int(value.strip())
    else:
        return None
    return number if minimum <= number <= maximum else None


def _normalize_mode(value: Any) -> str | None:
    """``optmem-only`` / ``optmem_only`` / ``OptMem Only`` -> canonical, else None."""
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace("_", "-").replace(" ", "-")
    token = token.replace("-only", "-only").strip("-")
    if token in MODES:
        return token
    # Accept "optmem-only" written with extra dashes ("optmem--only").
    collapsed = "-".join(part for part in token.split("-") if part)
    return collapsed if collapsed in MODES else None


def _expand_home(value: str, hermes_home: Path) -> str:
    return value.replace("${HERMES_HOME}", str(hermes_home)).replace(
        "$HERMES_HOME", str(hermes_home)
    )


@dataclass(frozen=True)
class OptMemConfig:
    """Resolved, immutable view of the effective configuration."""

    mode: str = MODE_HYBRID
    memory_dir: str = ""
    wake_budget: int = DEFAULT_WAKE_BUDGET
    recall_mode: str = "auto"
    llm_summary: bool = False
    migration_split_long: bool = False
    auto_nap: bool = False
    native_memory_file: str = ""
    native_user_file: str = ""
    source: str = "defaults"
    diagnostics: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def optmem_only(self) -> bool:
        return self.mode == MODE_OPTMEM_ONLY

    @property
    def memory_path(self) -> Path:
        return Path(self.memory_dir)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "memory_dir": self.memory_dir,
            "wake_budget": self.wake_budget,
            "recall_mode": self.recall_mode,
            "llm_summary": self.llm_summary,
            "migration_split_long": self.migration_split_long,
            "auto_nap": self.auto_nap,
            "native_memory_file": self.native_memory_file,
            "native_user_file": self.native_user_file,
            "source": self.source,
            "diagnostics": list(self.diagnostics),
        }


def _read_declared_raw(hermes_home: Path) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """``(data, status, diagnostics)``; status is "declared" | "missing" | "unreadable"."""
    path = declared_config_path(hermes_home)
    if not path.exists():
        return {}, "missing", ()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return {}, "unreadable", (f"config.json could not be read ({exc}); using defaults",)
    if not text.strip():
        return {}, "missing", ()
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, "unreadable", (f"config.json is not valid JSON ({exc}); using defaults",)
    if not isinstance(data, dict):
        return {}, "unreadable", ("config.json must contain a JSON object; using defaults",)
    return data, "declared", ()


def resolve_config(
    hermes_home: str | os.PathLike[str],
    plugin_config: Mapping[str, Any] | None = None,
) -> OptMemConfig:
    """Resolve the effective config for ``hermes_home``.

    ``plugin_config`` is the legacy mapping (``memory.optmem`` / ``plugins.optmem``)
    that the caller already read from ``config.yaml``; it is only consulted for
    keys the declared config does not set.
    """
    home = Path(hermes_home).expanduser()
    declared, status, diagnostics = _read_declared_raw(home)
    legacy = dict(plugin_config or {})
    # The declared file wins key-by-key; legacy fills the gaps.
    merged: dict[str, Any] = {k: v for k, v in legacy.items() if v is not None}
    merged.update(declared)
    notes = list(diagnostics)

    mode_raw = merged.get("mode")
    mode = MODE_HYBRID if mode_raw is None else _normalize_mode(mode_raw)
    if mode is None:
        notes.append(f"mode {mode_raw!r} is not one of {list(MODES)}; keeping {MODE_HYBRID!r}")
        mode = MODE_HYBRID

    recall_raw = merged.get("recall_mode")
    recall_mode = "auto" if recall_raw is None else str(recall_raw).strip().lower()
    if recall_mode not in RECALL_MODES:
        notes.append(f"recall_mode {recall_raw!r} is not one of {list(RECALL_MODES)}; using 'auto'")
        recall_mode = "auto"

    wake_raw = merged.get("wake_budget")
    if wake_raw is None:
        wake_budget = DEFAULT_WAKE_BUDGET
    else:
        wake_budget = _as_int(wake_raw, minimum=WAKE_BUDGET_MIN, maximum=WAKE_BUDGET_MAX)
        if wake_budget is None:
            notes.append(
                f"wake_budget {wake_raw!r} is not an integer in "
                f"[{WAKE_BUDGET_MIN}, {WAKE_BUDGET_MAX}]; using {DEFAULT_WAKE_BUDGET}"
            )
            wake_budget = DEFAULT_WAKE_BUDGET

    def _bool(key: str, default: bool) -> bool:
        if key not in merged:
            return default
        parsed = _as_bool(merged[key])
        if parsed is None:
            notes.append(f"{key} {merged[key]!r} is not a boolean; using {default}")
            return default
        return parsed

    memory_dir_raw = merged.get("memory_dir")
    memory_dir = str(memory_dir_raw).strip() if isinstance(memory_dir_raw, str) else ""
    if not memory_dir:
        memory_dir = str(home / "optmem_memory")
    memory_dir = _expand_home(memory_dir, home)
    memory_dir = str(Path(memory_dir).expanduser())

    native_mem, native_user = native_memory_paths(home)
    native_memory_file = merged.get("native_memory_file")
    native_user_file = merged.get("native_user_file")

    if status == "declared":
        source = "declared"
    elif legacy:
        source = "legacy"
    else:
        source = "defaults"

    return OptMemConfig(
        mode=mode,
        memory_dir=memory_dir,
        wake_budget=wake_budget,
        recall_mode=recall_mode,
        llm_summary=_bool("llm_summary", False),
        migration_split_long=_bool("migration_split_long", False),
        auto_nap=_bool("auto_nap", False),
        native_memory_file=str(native_memory_file)
        if isinstance(native_memory_file, str) and native_memory_file
        else str(_expand_home(str(native_mem), home)),
        native_user_file=str(native_user_file)
        if isinstance(native_user_file, str) and native_user_file
        else str(_expand_home(str(native_user), home)),
        source=source,
        diagnostics=tuple(notes),
        raw=dict(merged),
    )


def _validate(key: str, value: Any) -> Any:
    """Coerce one submitted value; raise ``ValueError`` when unusable."""
    if key == "mode":
        normalized = _normalize_mode(value)
        if normalized is None:
            raise ValueError(f"invalid mode {value!r}; allowed: {', '.join(MODES)}")
        return normalized
    if key == "recall_mode":
        token = str(value).strip().lower()
        if token not in RECALL_MODES:
            raise ValueError(f"invalid recall_mode {value!r}; allowed: {', '.join(RECALL_MODES)}")
        return token
    if key == "wake_budget":
        number = _as_int(value, minimum=WAKE_BUDGET_MIN, maximum=WAKE_BUDGET_MAX)
        if number is None:
            raise ValueError(
                f"invalid wake_budget {value!r}; expected an integer in "
                f"[{WAKE_BUDGET_MIN}, {WAKE_BUDGET_MAX}]"
            )
        return number
    if key in ("llm_summary", "migration_split_long", "auto_nap"):
        parsed = _as_bool(value)
        if parsed is None:
            raise ValueError(f"invalid {key} {value!r}; expected a boolean")
        return parsed
    if key in ("memory_dir",):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty path string")
        return value.strip()
    if key in ("native_memory_file", "native_user_file"):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty path string")
        return value.strip()
    raise ValueError(f"unknown config key {key!r}")


def write_declared_config(
    hermes_home: str | os.PathLike[str],
    values: Mapping[str, Any],
    *,
    merge: bool = True,
) -> Path:
    """Merge ``values`` into the declared config file and write it atomically.

    Validates everything BEFORE opening the file, so a rejected value leaves the
    previous file byte-identical. Unrelated keys already in the file survive.
    """
    home = Path(hermes_home).expanduser()
    coerced: dict[str, Any] = {}
    for key, value in values.items():
        coerced[key] = _validate(key, value)  # raises before any write
    previous, status, diagnostics = _read_declared_raw(home)
    if merge and status == "unreadable":
        raise ValueError("refusing to overwrite unreadable config: " + "; ".join(diagnostics))
    payload = dict(previous) if merge else {}
    payload.update(coerced)

    path = declared_config_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"cannot create config directory {path.parent}: {exc}") from exc

    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return path


def legacy_plugin_config(hermes_home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read the legacy ``memory.optmem`` / ``plugins.optmem`` section from config.yaml.

    Best-effort: ``{}`` when PyYAML is missing, the file is absent or malformed.
    Never raises — the caller then falls back to declared config/defaults.
    """
    home = (
        Path(hermes_home).expanduser()
        if hermes_home
        else Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    )
    config_path = home / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml  # optional: only needed for the legacy surface
    except Exception:
        return {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        logger.debug("could not parse %s", config_path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    merged: dict[str, Any] = {}
    section = data.get("memory")
    if isinstance(section, dict):
        candidate = section.get(PROVIDER_NAME)
        if isinstance(candidate, dict):
            merged.update(candidate)
        # 0.2.0 documented llm_summary directly under memory:.
        for key in ("llm_summary", "mode"):
            if key in section and key not in merged:
                merged[key] = section[key]
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        candidate = plugins.get(PROVIDER_NAME)
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged
