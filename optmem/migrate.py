"""Migrate the built-in MEMORY.md/USER.md store into OptMem, safely.

Everything here is written around one rule: **a fact is never lost and the
native store is never switched off on an unverified plan.**

- Parsing mirrors the host exactly (``tools/memory_tool_store.py``):
  ``MEMORY.md`` / ``USER.md`` under ``<HERMES_HOME>/memories/``, entries joined
  by ``"\\n§\\n"``. Other ``.md`` files in that folder are drafts, not memories.
- An entry over 280 UTF-8 bytes is split on the strongest safe boundary; if it
  cannot be split it is reported as *unresolved* and the plan is **blocked**.
  Entries are never truncated, reworded or dropped.
- The raw native files are copied byte-for-byte (with a sha256 manifest) before
  any migration or mode change.
- Re-running the migration is a no-op: entries already in the store are skipped
  by accent-insensitive text match, and imported sources are recorded in
  ``<store>/migration.json``.
- ``config.yaml`` is edited surgically (key by key, comments and unrelated keys
  preserved) and never rewritten wholesale; the pre-switch file is backed up so
  ``rollback`` can restore it byte-for-byte.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any

from .config import (
    MODE_HYBRID,
    MODE_OPTMEM_ONLY,
    MODES,
    _normalize_mode,
    native_memory_paths,
    resolve_config,
    write_declared_config,
)
from .engine import ENTRY_CHARS, _normalize

logger = logging.getLogger(__name__)

# Exactly the host's delimiter (tools/memory_tool_store.py: ENTRY_DELIMITER).
DELIMITER = "\n§\n"

BACKUP_DIR_NAME = "optmem_backups"
NATIVE_SUBDIR = "memories"
MIGRATION_STATE_NAME = "migration.json"
MODE_STATE_NAME = "mode_state.json"

# Strongest boundary first. Splitting is recursive: a chunk that is still too
# long is re-split on the next weaker boundary.
_BOUNDARY_LEVELS: tuple[tuple[str, ...], ...] = (
    ("\n",),
    (". ", "! ", "? ", ".\t"),
    ("; ",),
    (" | ",),
    (", ",),
)

_MEMORY_SECTION_RE = re.compile(r"^memory:\s*(#.*)?$")
_MEMORY_INLINE_RE = re.compile(r"^memory:\s*\S")


class NativeReadError(RuntimeError):
    """A native memory file exists but could not be read as UTF-8 text."""


# --------------------------------------------------------------------------- #
# Native parsing
# --------------------------------------------------------------------------- #


def _read_raw_text(path: Path) -> str:
    """Strict UTF-8 (BOM tolerated) read; raises :class:`NativeReadError` on failure."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise NativeReadError(f"{path}: could not be read as UTF-8 text ({exc})") from exc


def read_native_entries(path: str | os.PathLike[str]) -> list[str]:
    """Entries of one native memory file (``[]`` when the file does not exist)."""
    target = Path(path)
    if not target.exists():
        return []
    raw = _read_raw_text(target)
    return [entry for entry in (piece.strip() for piece in raw.split(DELIMITER)) if entry]


# --------------------------------------------------------------------------- #
# Splitting long entries
# --------------------------------------------------------------------------- #


def _split_on(text: str, separators: Sequence[str]) -> list[str]:
    """Split keeping the separator attached to the left piece; never drops text."""
    pattern = "(" + "|".join(re.escape(sep) for sep in separators) + ")"
    pieces: list[str] = []
    buffer = ""
    for token in re.split(pattern, text):
        if token in separators:
            buffer += token
            if buffer.strip():
                pieces.append(buffer.strip())
            buffer = ""
        else:
            buffer += token
    if buffer.strip():
        pieces.append(buffer.strip())
    return pieces or [text.strip()]


def _split_recursive(text: str, limit: int, level: int) -> list[str] | None:
    if len(text.encode("utf-8")) <= limit:
        return [text.strip()] if text.strip() else None
    if level >= len(_BOUNDARY_LEVELS):
        return None
    pieces = _split_on(text, _BOUNDARY_LEVELS[level])
    if len(pieces) <= 1:
        return _split_recursive(text, limit, level + 1)
    out: list[str] = []
    for piece in pieces:
        if len(piece.encode("utf-8")) <= limit:
            out.append(piece)
            continue
        sub = _split_recursive(piece, limit, level + 1)
        if sub is None:
            return None
        out.extend(sub)
    return out


def split_long_entry(text: str, limit: int = ENTRY_CHARS) -> list[str] | None:
    """Split ``text`` into parts of at most ``limit`` UTF-8 bytes.

    Returns ``None`` when no safe boundary exists (a single over-long token).
    Content is preserved — parts are never truncated.
    """
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped.encode("utf-8")) <= limit:
        return [stripped]
    return _split_recursive(stripped, limit, 0)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlannedEntry:
    """One atomic memory to append, with its provenance."""

    text: str
    source: str
    date: str


@dataclass(frozen=True)
class MigrationPlan:
    adds: tuple[PlannedEntry, ...] = ()
    skipped: tuple[str, ...] = ()
    unresolved: tuple[tuple[str, int], ...] = ()
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    split_count: int = 0
    date: str = ""
    status: str = "empty"

    @property
    def blocked(self) -> bool:
        return bool(self.unresolved)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "adds": len(self.adds),
            "skipped": len(self.skipped),
            "split": self.split_count,
            "unresolved": [{"bytes": n, "text": t} for t, n in self.unresolved],
            "sources": list(self.sources),
            "reasons": list(self.reasons),
            "date": self.date,
        }


def _store_texts(engine) -> set[str]:
    """Normalized text of every memory already in the store."""
    try:
        return {_normalize(record[2]) for record in engine._all_records()}
    except Exception:  # pragma: no cover - unreadable store must not block planning
        logger.warning("could not enumerate store entries for dedupe", exc_info=True)
        return set()


def _state_path(store_dir: str | os.PathLike[str]) -> Path:
    return Path(store_dir) / MIGRATION_STATE_NAME


def load_migration_state(store_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Recorded migration history for a store (``{}`` when absent/unreadable)."""
    path = _state_path(store_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.warning("could not read %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _source_fingerprint(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}\x00{text}".encode()).hexdigest()


def record_migration(
    store_dir: str | os.PathLike[str], plan: MigrationPlan, result: Mapping[str, Any]
) -> Path:
    """Append this migration's provenance to ``<store>/migration.json`` (idempotent)."""
    state = load_migration_state(store_dir)
    entries = list(state.get("entries") or [])
    known = {entry.get("fingerprint") for entry in entries if isinstance(entry, dict)}
    for entry in plan.adds:
        fingerprint = _source_fingerprint(entry.source, entry.text)
        if fingerprint in known:
            continue
        known.add(fingerprint)
        entries.append(
            {
                "fingerprint": fingerprint,
                "source": entry.source,
                "bytes": len(entry.text.encode("utf-8")),
                "date": entry.date,
                "imported_at": _date.today().isoformat(),
            }
        )
    state["entries"] = entries
    state["last_import"] = {
        "date": result.get("date"),
        "added": result.get("added"),
        "skipped": result.get("skipped"),
        "ids": list(result.get("ids") or []),
    }
    path = _state_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def plan_migration(
    hermes_home: str | os.PathLike[str],
    *,
    engine,
    memory_path: str | os.PathLike[str] | None = None,
    user_path: str | os.PathLike[str] | None = None,
    split_long: bool | None = None,
    date: str | None = None,
    store_dir: str | os.PathLike[str] | None = None,
) -> MigrationPlan:
    """Plan the native -> OptMem import without writing anything."""
    home = Path(hermes_home).expanduser()
    config = resolve_config(home)
    if split_long is None:
        split_long = config.migration_split_long
    import_date = date or _date.today().isoformat()

    default_mem, default_user = native_memory_paths(home)
    sources: list[tuple[str, Path]] = [
        ("MEMORY.md", Path(memory_path) if memory_path else default_mem),
        ("USER.md", Path(user_path) if user_path else default_user),
    ]

    store_dir = (
        Path(store_dir) if store_dir else Path(getattr(engine, "dir", home / "optmem_memory"))
    )
    recorded = {
        entry.get("fingerprint")
        for entry in (load_migration_state(store_dir).get("entries") or [])
        if isinstance(entry, dict)
    }
    existing = _store_texts(engine)

    adds: list[PlannedEntry] = []
    skipped: list[str] = []
    unresolved: list[tuple[str, int]] = []
    reasons: list[str] = []
    split_count = 0
    seen: set[str] = set()
    present_sources: list[str] = []

    for name, path in sources:
        entries = read_native_entries(path)
        if entries:
            present_sources.append(name)
        for text in entries:
            nbytes = len(text.encode("utf-8"))
            key = _normalize(text)
            if key in seen:
                skipped.append(text)
                continue
            seen.add(key)
            if key in existing:
                skipped.append(text)
                continue
            if _source_fingerprint(name, text) in recorded:
                skipped.append(text)
                continue
            if nbytes <= ENTRY_CHARS:
                adds.append(PlannedEntry(text=text, source=name, date=import_date))
                continue
            # Over-long: split on safe boundaries or block the whole plan.
            if split_long:
                parts = split_long_entry(text)
                if parts is not None and all(len(p.encode("utf-8")) <= ENTRY_CHARS for p in parts):
                    if len(parts) > 1:
                        split_count += 1
                    adds.extend(
                        PlannedEntry(text=part, source=name, date=import_date) for part in parts
                    )
                    continue
            unresolved.append((text, nbytes))

    if unresolved:
        reasons.append(
            f"{len(unresolved)} native entr{'y' if len(unresolved) == 1 else 'ies'} "
            f"exceed {ENTRY_CHARS} bytes and cannot be split safely; shorten or split them in "
            f"{'/'.join(present_sources) or 'the native memory file'}, pass --split to split on "
            f"safe boundaries, or import a curated file instead"
        )
    for text, nbytes in unresolved:
        reasons.append(f"{nbytes} bytes: {text[:80]}{'…' if len(text) > 80 else ''}")

    if unresolved:
        status = "blocked"
    elif adds:
        status = "ok"
    else:
        status = "empty"

    return MigrationPlan(
        adds=tuple(adds),
        skipped=tuple(skipped),
        unresolved=tuple(unresolved),
        sources=tuple(present_sources),
        reasons=tuple(reasons),
        split_count=split_count,
        date=import_date,
        status=status,
    )


def apply_migration(engine, plan: MigrationPlan) -> dict[str, Any]:
    """Append every planned entry. Refuses a blocked plan (never partial data loss)."""
    if plan.blocked:
        raise ValueError(
            "; ".join(plan.reasons)
            or "migration plan is blocked: some entries cannot be split safely"
        )
    if not plan.adds:
        return {"added": 0, "ids": [], "skipped": len(plan.skipped), "date": plan.date}

    base = engine.import_lines_pairs([(entry.date, entry.text) for entry in plan.adds])
    ids = list(range(base, base + len(plan.adds)))
    result = {
        "added": len(plan.adds),
        "ids": ids,
        "skipped": len(plan.skipped),
        "date": plan.date,
    }
    with contextlib.suppress(Exception):
        record_migration(getattr(engine, "dir", "."), plan, result)
    return result


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


def _copy_raw(src: Path, dest: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(src, "rb") as reader, open(dest, "wb") as writer:
        for chunk in iter(lambda: reader.read(65536), b""):
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest(), size


def backup_native_files(
    hermes_home: str | os.PathLike[str],
    *,
    paths: Iterable[str | os.PathLike[str]] | None = None,
    backup_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Copy the raw native memory files byte-for-byte into a restrictive backup dir.

    A missing source is recorded as ``present: false`` (a fresh install has no
    native store) — that is not an error.
    """
    home = Path(hermes_home).expanduser()
    if paths is None:
        mem, user = native_memory_paths(home)
        targets = [mem, user]
    else:
        targets = [Path(p) for p in paths]

    root = Path(backup_root) if backup_root else home / BACKUP_DIR_NAME
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{time.time_ns() % 1_000_000:06d}"
    backup_dir = root / f"native-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    with contextlib.suppress(OSError):
        os.chmod(backup_dir, 0o700)

    files: list[dict[str, Any]] = []
    for src in targets:
        record: dict[str, Any] = {
            "name": src.name,
            "path": str(src),
            "present": False,
            "bytes": 0,
            "sha256": None,
        }
        if src.exists() and src.is_file():
            dest = backup_dir / src.name
            try:
                digest, size = _copy_raw(src, dest)
            except OSError as exc:
                raise ValueError(f"could not back up {src}: {exc}") from exc
            record.update({"present": True, "bytes": size, "sha256": digest, "backup": str(dest)})
        files.append(record)

    manifest = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hermes_home": str(home),
        "files": files,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"dir": str(backup_dir), "files": files, "created": manifest["created"]}


def verify_backup(backup: Mapping[str, Any] | None, hermes_home: Path) -> tuple[bool, list[str]]:
    """Require an intact backup of the current canonical native sources."""
    if not backup or not backup.get("dir"):
        return False, ["no native backup has been taken; take a fresh backup first"]
    directory = Path(str(backup["dir"]))
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise ValueError("invalid backup manifest")
        if Path(str(manifest.get("hermes_home", ""))).resolve() != hermes_home.resolve():
            raise ValueError("backup belongs to a different profile")
        records = manifest["files"]
        expected = native_memory_paths(hermes_home)
        if len(records) != len(expected) or not all(isinstance(r, dict) for r in records):
            raise ValueError("backup manifest must cover both canonical native sources")
        by_source = {Path(str(r.get("path", ""))).resolve(): r for r in records}
        if len(by_source) != len(expected):
            raise ValueError("duplicate backup source records")
        for source in expected:
            record = by_source.get(source.resolve())
            if record is None or not isinstance(record.get("present"), bool):
                raise ValueError("backup does not cover the current native sources")
            if not record["present"]:
                if source.exists():
                    raise ValueError("backup predates native source changes; take a fresh backup")
                continue
            copy = directory / source.name
            if Path(str(record.get("backup", ""))).resolve() != copy.resolve():
                raise ValueError("backup copy is outside the declared backup directory")
            data = copy.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != record.get("sha256") or len(data) != record.get("bytes"):
                raise ValueError("backup copy hash or size does not match its manifest")
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError("backup predates native source changes; take a fresh backup")
    except (OSError, ValueError, TypeError) as exc:
        return False, [f"native backup verification failed: {exc}"]
    return True, []


def restore_native_files(backup: Mapping[str, Any], *, overwrite: bool = False) -> list[str]:
    """Put a backup's files back where they came from (rollback path)."""
    restored: list[str] = []
    for record in backup.get("files") or []:
        if not record.get("present") or not record.get("backup"):
            continue
        src, dest = Path(record["backup"]), Path(record["path"])
        if dest.exists() and not overwrite:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        restored.append(str(dest))
    return restored


# --------------------------------------------------------------------------- #
# config.yaml surgical editing
# --------------------------------------------------------------------------- #


def _memory_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``(start, end)`` line indices of the top-level ``memory:`` block."""
    start = next((i for i, line in enumerate(lines) if _MEMORY_SECTION_RE.match(line)), None)
    if start is None:
        if any(_MEMORY_INLINE_RE.match(line) for line in lines):
            raise ValueError(
                "config.yaml uses inline flow style for the 'memory:' section "
                "(e.g. 'memory: {provider: optmem-hermes}'); edit it manually and re-run"
            )
        return None
    depth = 1
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in " \t":
            depth = i
            break
    else:
        depth = len(lines)
    return start, depth


def update_memory_config(
    hermes_home: str | os.PathLike[str], updates: Mapping[str, str | int | bool]
) -> Path:
    """Set keys under the top-level ``memory:`` block, preserving everything else.

    Comments, key order, unrelated top-level sections and unrelated keys inside
    ``memory:`` all survive. The file is replaced atomically. Refuses (without
    writing) when the section is expressed in flow style.
    """
    home = Path(hermes_home).expanduser()
    path = home / "config.yaml"
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    trailing_newline = text.endswith("\n") or not text
    lines = text.splitlines()

    bounds = _memory_block_bounds(lines)
    if bounds is None:
        block = ["memory:"]
        for key, value in updates.items():
            block.append(f"  {key}: {_render_scalar(value)}")
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    else:
        start, end = bounds
        indent = "  "
        for i in range(start + 1, end):
            if lines[i].strip():
                indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                break
        for key, value in updates.items():
            pattern = re.compile(rf"^(\s*){re.escape(key)}:\s*.*$")
            replaced = False
            for i in range(start + 1, end):
                if pattern.match(lines[i]):
                    lines[i] = f"{indent}{key}: {_render_scalar(value)}"
                    replaced = True
                    break
            if not replaced:
                lines.insert(start + 1, f"{indent}{key}: {_render_scalar(value)}")
                end += 1

    new_text = "\n".join(lines) + ("\n" if trailing_newline or lines else "")
    if new_text == text:
        return path
    tmp = path.with_name(path.name + ".optmem.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if path.exists():
        with contextlib.suppress(OSError):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    return path


def _render_scalar(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def readiness(
    hermes_home: str | os.PathLike[str],
    *,
    engine,
    plan: MigrationPlan | None = None,
    backup: Mapping[str, Any] | None = None,
    target_mode: str | None = None,
) -> dict[str, Any]:
    """Can the native store be switched off without losing a fact?"""
    home = Path(hermes_home).expanduser()
    config = resolve_config(home)
    mem_path, user_path = native_memory_paths(home)
    present = tuple(
        name
        for name, path in (("MEMORY.md", mem_path), ("USER.md", user_path))
        if path.exists() and path.stat().st_size
    )
    reasons: list[str] = []

    store_texts = _store_texts(engine) if engine is not None else set()
    store_entries = int(engine.log_len()) if engine is not None else 0

    native_absent: list[str] = []
    for _name, path in (("MEMORY.md", mem_path), ("USER.md", user_path)):
        try:
            entries = read_native_entries(path)
        except NativeReadError as exc:
            native_absent.append(str(exc))
            reasons.append(str(exc))
            continue
        for entry in entries:
            parts = split_long_entry(entry) if len(entry.encode("utf-8")) > ENTRY_CHARS else [entry]
            if _normalize(entry) not in store_texts and not (
                parts and all(_normalize(part) in store_texts for part in parts)
            ):
                native_absent.append(entry)
    if native_absent:
        reasons.append(
            f"{len(native_absent)} native memory entr"
            f"{'y is' if len(native_absent) == 1 else 'ies are'} not in the OptMem store yet; "
            "run the migration before disabling the native store"
        )

    unresolved = tuple(plan.unresolved) if plan is not None else ()
    if unresolved:
        reasons.extend(plan.reasons if plan is not None else ())

    backup_ok, backup_reasons = verify_backup(backup, home)
    reasons.extend(backup_reasons)

    imported = not native_absent and not unresolved and store_entries > 0
    if store_entries == 0 and not native_absent:
        reasons.append("the OptMem store is empty; nothing was imported")

    if target_mode is not None and _normalize_mode(target_mode) is None:
        reasons.append(f"mode {target_mode!r} is not one of {list(MODES)}")

    checks = {
        "provider_ready": engine is not None,
        "store_entries": store_entries,
        "native_files": present,
        "native_absent": tuple(native_absent),
        "unresolved_long": unresolved,
        "imported": imported,
        "backup": backup_ok,
        "mode": config.mode,
    }
    return {
        "ready": bool(imported and backup_ok and not unresolved),
        "checks": checks,
        "reasons": reasons,
        "store_entries": store_entries,
        "mode": config.mode,
    }


# --------------------------------------------------------------------------- #
# Mode switching
# --------------------------------------------------------------------------- #


def _mode_state_path(hermes_home: Path) -> Path:
    return hermes_home / "optmem" / MODE_STATE_NAME


def plan_mode_switch(
    hermes_home: str | os.PathLike[str],
    target_mode: str,
    *,
    engine,
    backup: Mapping[str, Any] | None = None,
    plan: MigrationPlan | None = None,
) -> dict[str, Any]:
    """Decide whether a mode switch may proceed. Writes nothing."""
    home = Path(hermes_home).expanduser()
    reasons: list[str] = []
    mode = _normalize_mode(target_mode)
    if mode is None:
        return {
            "ok": False,
            "mode": None,
            "reasons": [f"mode {target_mode!r} is not one of {list(MODES)}"],
        }

    # Config editability is checked BEFORE anything else is written.
    try:
        lines = (
            (home / "config.yaml").read_text(encoding="utf-8-sig").splitlines()
            if (home / "config.yaml").exists()
            else []
        )
        _memory_block_bounds(lines)
    except ValueError as exc:
        reasons.append(str(exc))

    report: dict[str, Any] = {"ok": True, "mode": mode, "reasons": reasons, "changes": {}}
    if mode == MODE_OPTMEM_ONLY:
        report["changes"] = {"memory_enabled": False, "user_profile_enabled": False}
        verification = readiness(home, engine=engine, plan=plan, backup=backup, target_mode=mode)
        report["readiness"] = verification
        if not verification["ready"]:
            reasons.extend(verification["reasons"])
        if reasons:
            report["ok"] = False
    else:  # hybrid: re-enabling the native store is always allowed and reversible
        report["changes"] = {"memory_enabled": True, "user_profile_enabled": True}
    if reasons:
        report["ok"] = False
    return report


def apply_mode_switch(
    hermes_home: str | os.PathLike[str],
    target_mode: str,
    *,
    engine,
    backup: Mapping[str, Any] | None = None,
    plan: MigrationPlan | None = None,
) -> dict[str, Any]:
    """Perform a verified mode switch; writes config.yaml + the declared config.

    A refused switch writes NOTHING (no declared config, no config.yaml edit).
    """
    home = Path(hermes_home).expanduser()
    report = plan_mode_switch(home, target_mode, engine=engine, backup=backup, plan=plan)
    if not report["ok"]:
        return report

    config_backup = _backup_config_yaml(home, backup)
    if config_backup is None:
        return {
            "ok": False,
            "mode": report["mode"],
            "reasons": ["config.yaml exists but could not be backed up; refusing to edit it"],
        }

    previous = _current_flags(home)
    updates = dict(report["changes"])
    declared = home / "optmem" / "config.json"
    declared_before = declared.read_bytes() if declared.exists() else None
    state_path = _mode_state_path(home)
    state_before = state_path.read_bytes() if state_path.exists() else None
    try:
        update_memory_config(home, {**updates, "provider": "optmem-hermes"})
        write_declared_config(home, {"mode": report["mode"]})
        state_payload = (
            json.dumps(
                {
                    "previous": previous,
                    "previous_mode": previous.get("mode", MODE_HYBRID),
                    "target_mode": report["mode"],
                    "config_backup": config_backup,
                    "switched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        _atomic_private_bytes(state_path, state_payload.encode("utf-8"))
    except Exception as exc:
        _atomic_private_bytes(home / "config.yaml", Path(config_backup).read_bytes())
        for path, before in ((declared, declared_before), (state_path, state_before)):
            if before is not None:
                _atomic_private_bytes(path, before)
            else:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
        return {
            "ok": False,
            "mode": report["mode"],
            "reasons": [str(exc)],
            "config_backup": config_backup,
        }

    return {
        "ok": True,
        "mode": report["mode"],
        "changes": updates,
        "config_backup": config_backup,
        "state": str(state_path),
        "reasons": [],
    }


def _atomic_private_bytes(path: Path, data: bytes) -> None:
    """Write private state without following predictable temporary symlinks."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".optmem-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _current_flags(home: Path) -> dict[str, Any]:
    config = resolve_config(home)
    return {"mode": config.mode, "memory_dir": config.memory_dir}


def _backup_config_yaml(home: Path, backup: Mapping[str, Any] | None) -> str | None:
    """Copy the current config.yaml aside (byte-for-byte) before editing it."""
    path = home / "config.yaml"
    if not path.exists():
        return None
    if backup and backup.get("dir"):
        target_dir = Path(str(backup["dir"]))
    else:
        stamp = (
            time.strftime("%Y%m%d-%H%M%S", time.localtime()) + f"-{time.time_ns() % 1_000_000:06d}"
        )
        target_dir = home / BACKUP_DIR_NAME / f"config-{stamp}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target_dir, 0o700)
        dest = target_dir / "config.yaml"
        _copy_raw(path, dest)
    except OSError as exc:
        logger.warning("could not back up config.yaml: %s", exc)
        return None
    return str(dest)


def rollback_mode(hermes_home: str | os.PathLike[str], *, engine=None) -> dict[str, Any]:
    """Undo the last mode switch: restore config.yaml and the declared mode.

    OptMem data is never touched.
    """
    home = Path(hermes_home).expanduser()
    state_path = _mode_state_path(home)
    if not state_path.exists():
        return {"ok": False, "reasons": ["no mode switch has been recorded for this profile"]}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "reasons": [f"mode state is unreadable ({exc}); nothing restored"]}

    restored = None
    backup = state.get("config_backup")
    if backup and Path(backup).exists():
        try:
            shutil.copy2(backup, home / "config.yaml")
            restored = backup
        except OSError as exc:
            return {"ok": False, "reasons": [f"could not restore {backup}: {exc}"]}

    previous_mode = _normalize_mode(state.get("previous_mode")) or MODE_HYBRID
    with contextlib.suppress(Exception):
        write_declared_config(home, {"mode": previous_mode})
    with contextlib.suppress(OSError):
        state_path.unlink()
    return {"ok": True, "mode": previous_mode, "restored_config": restored, "reasons": []}
