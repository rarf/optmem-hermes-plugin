"""``hermes optmem`` — the config interface for the OptMem memory provider.

Wiring (``plugins/memory/__init__.py`` + ``hermes_cli/main.py``): the host
imports this file BY PATH during argparse setup and calls
``register_cli(subparser)``; ``optmem_command`` is the handler. Because it is
loaded before any provider is constructed, this module uses only RELATIVE
imports of pure-stdlib siblings — never ``optmem/__init__.py``, which pulls the
agent runtime into the CLI process.

Read-only commands (``status``, ``show``, ``check``) never create or modify
anything. ``mode`` refuses without ``--yes`` and, when it refuses, writes
nothing at all — no declared config, no config.yaml edit. ``migrate`` takes the
backup and imports only the facts it can prove are whole; a blocked plan exits
non-zero and leaves the built-in store running.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import (
    MODES,
    declared_config_path,
    default_hermes_home,
    native_memory_paths,
    read_native_flags,
    resolve_config,
)
from .engine import OptMemEngine
from .migrate import (
    MigrationPlan,
    apply_migration,
    apply_mode_switch,
    backup_native_files,
    plan_migration,
    plan_mode_switch,
    readiness,
    rollback_mode,
)

PROG = "hermes optmem"

# What the provider actually does; mirrors OptMemProvider.capabilities() without
# importing the provider (see the module docstring).
CAPABILITIES: dict[str, Any] = {
    "structural_chaining": True,
    "semantic_conflict_resolution": False,
    "append_only": True,
    "local_only": True,
}

_LIMITS = (
    "Limits: no semantic conflict resolution — a later note does not invalidate an "
    "earlier one, so a superseded decision stays retrievable and the model must judge. "
    "Compaction is lossy for detail (raw records survive). `forget` drops summaries, "
    "not raw records."
)


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #

def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add the two global options. Subparsers use ``suppress`` so their defaults
    do not clobber a value already parsed at the top level (argparse gotcha)."""
    parser.add_argument(
        "--hermes-home", metavar="PATH",
        default=argparse.SUPPRESS if suppress else None,
        help="Profile home to operate on (default: the active HERMES_HOME).",
    )
    parser.add_argument(
        "--json", action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="Machine-readable output on stdout.",
    )


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes optmem <action>`` tree (called by the host)."""
    subparser.description = (
        "Inspect and configure the OptMem memory provider: store location, wake "
        "budget, retrieval mode, native migration and the Hybrid/OptMem-only switch."
    )
    _add_common(subparser, suppress=False)
    suppress = argparse.ArgumentParser(add_help=False)
    _add_common(suppress, suppress=True)
    sub = subparser.add_subparsers(
        dest="optmem_action",
        metavar="{status,show,check,migrate,mode,import,rollback}",
    )

    sub.add_parser("status", parents=[suppress], help="Mode, store and native-store state")
    sub.add_parser(
        "show", parents=[suppress], help="Effective configuration and where it came from"
    )
    check = sub.add_parser(
        "check", parents=[suppress], help="Is the store ready to replace the native one?"
    )
    check.add_argument(
        "--split", action="store_true", help="Consider splitting over-long native entries"
    )

    migrate = sub.add_parser(
        "migrate", parents=[suppress], help="Back up and import MEMORY.md/USER.md"
    )
    migrate.add_argument("--split", action="store_true",
                         help="Split native entries over 280 bytes on safe boundaries")
    migrate.add_argument("--dry-run", action="store_true", help="Report the plan; write nothing")

    mode = sub.add_parser("mode", parents=[suppress], help="Switch between hybrid and optmem-only")
    mode.add_argument("target_mode", choices=list(MODES))
    mode.add_argument("--yes", action="store_true", help="Apply the switch (required to write)")
    mode.add_argument(
        "--split", action="store_true", help="Allow splitting over-long native entries"
    )

    imp = sub.add_parser(
        "import", parents=[suppress], help="Import a curated 'YYYY-MM-DD <text>' file"
    )
    imp.add_argument("file")
    imp.add_argument(
        "--no-dedupe", action="store_true", help="Append duplicates already in the store"
    )

    sub.add_parser("rollback", parents=[suppress], help="Undo the last mode switch")
    sub.add_parser("version", parents=[suppress], help="Show the installed plugin version")
    subparser.set_defaults(optmem_action="status")


def optmem_command(args: argparse.Namespace) -> int:
    """Entry point the host registers as ``func``. Returns a process exit code."""
    action = getattr(args, "optmem_action", None) or "status"
    home = Path(getattr(args, "hermes_home", None) or default_hermes_home()).expanduser()
    as_json = bool(getattr(args, "json", False))
    try:
        if action == "version":
            return _cmd_version(as_json)
        if action == "status":
            return _cmd_status(home, as_json)
        if action == "show":
            return _cmd_show(home, as_json)
        if action == "check":
            return _cmd_check(home, as_json, split=bool(getattr(args, "split", False)))
        if action == "migrate":
            return _cmd_migrate(home, as_json, split=bool(getattr(args, "split", False)),
                                dry_run=bool(getattr(args, "dry_run", False)))
        if action == "mode":
            return _cmd_mode(home, getattr(args, "target_mode", ""), as_json,
                             yes=bool(getattr(args, "yes", False)),
                             split=bool(getattr(args, "split", False)))
        if action == "import":
            return _cmd_import(home, getattr(args, "file", ""), as_json,
                               dedupe=not bool(getattr(args, "no_dedupe", False)))
        if action == "rollback":
            return _cmd_rollback(home, as_json)
    except (OSError, ValueError) as exc:
        _emit({"ok": False, "error": str(exc)}, as_json, [f"error: {exc}"])
        return 1
    _emit({"ok": False, "error": f"unknown action {action!r}"}, as_json,
          [f"error: unknown action {action!r}"])
    return 2


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def _emit(payload: dict[str, Any], as_json: bool, lines: Sequence[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))  # noqa: T201
    else:
        for line in lines:
            print(line)  # noqa: T201


def _store_engine(config, *, create: bool) -> OptMemEngine | None:
    """Open the store; ``create=False`` returns None when it does not exist yet.

    ``OptMemEngine.__init__`` creates LOG.txt, so a read-only command must not
    touch a store that has never been initialised.
    """
    path = Path(config.memory_dir)
    if not create and not (path / "LOG.txt").exists():
        return None
    return OptMemEngine(str(path))


def _pending_naps(engine: OptMemEngine | None) -> int:
    if engine is None:
        return 0
    try:
        return len(engine.pending_naps())
    except Exception:
        return 0


class _AbsentStore:
    """Read-only stand-in for a store that does not exist yet (dry runs).

    ``plan_migration`` only *reads* a store (its texts and its ``migration.json``),
    so a dry run can plan against an empty store without creating anything. A
    bare ``None`` would make the planner log a spurious traceback for an engine it
    cannot call, so the CLI hands it this empty, side-effect-free object instead.
    """

    def _all_records(self) -> list:
        return []


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def _cmd_version(as_json: bool) -> int:
    from . import __version__  # noqa: PLC0415  (importing the package is fine here)

    _emit({"version": __version__}, as_json, [__version__])
    return 0


def _status_payload(home: Path) -> dict[str, Any]:
    config = resolve_config(home)
    engine = _store_engine(config, create=False)
    mem_path, user_path = native_memory_paths(home)
    verification = readiness(home, engine=engine)
    return {
        "mode": config.mode,
        "mode_source": config.source,
        "recall_mode": config.recall_mode,
        "wake_budget": config.wake_budget,
        "memory_dir": config.memory_dir,
        "store_exists": engine is not None,
        "store_entries": int(engine.log_len()) if engine is not None else 0,
        "pending_naps": _pending_naps(engine),
        "native": {
            **read_native_flags(home),
            "files": {"MEMORY.md": mem_path.exists(), "USER.md": user_path.exists()},
        },
        "configured": declared_config_path(home).exists(),
        "ready": verification["ready"],
        "reasons": verification["reasons"],
        "capabilities": CAPABILITIES,
        "limits": _LIMITS,
    }


def _cmd_status(home: Path, as_json: bool) -> int:
    payload = _status_payload(home)
    native = payload["native"]
    lines = [
        f"mode            : {payload['mode']} ({payload['mode_source']})",
        f"store           : {payload['memory_dir']}",
        f"entries         : {payload['store_entries']} "
        f"({payload['pending_naps']} compression(s) pending)",
        f"retrieval       : {payload['recall_mode']} | wake budget {payload['wake_budget']} lines",
        f"native store    : memory_enabled={native['memory_enabled']} "
        f"user_profile_enabled={native['user_profile_enabled']} "
        f"files={native['files']}",
        f"ready to be exclusive: {payload['ready']}",
    ]
    if payload["reasons"]:
        lines += ["", "not ready because:"] + [f"  - {reason}" for reason in payload["reasons"]]
    lines += ["", _LIMITS]
    _emit(payload, as_json, lines)
    return 0


def _cmd_show(home: Path, as_json: bool) -> int:
    config = resolve_config(home)
    payload = config.as_dict()
    payload["configured_file"] = str(declared_config_path(home))
    payload["available_modes"] = list(MODES)
    lines = [f"{key}: {value}" for key, value in payload.items() if key != "diagnostics"]
    for note in config.diagnostics:
        lines.append(f"warning: {note}")
    _emit(payload, as_json, lines)
    return 0


def _plan(home: Path, *, engine, split: bool) -> MigrationPlan:
    return plan_migration(home, engine=engine, split_long=split)


def _cmd_check(home: Path, as_json: bool, *, split: bool) -> int:
    config = resolve_config(home)
    engine = _store_engine(config, create=False)
    plan = _plan(home, engine=engine, split=split) if engine is not None else None
    backup = _latest_backup(home)
    report = readiness(home, engine=engine, plan=plan, backup=backup)
    report["plan"] = plan.as_dict() if plan is not None else None
    lines = [f"ready: {report['ready']}", f"store entries: {report['store_entries']}"]
    lines += [f"  - {reason}" for reason in report["reasons"]]
    lines.append(_LIMITS)
    _emit(report, as_json, lines)
    return 0 if report["ready"] else 1


def _cmd_migrate(home: Path, as_json: bool, *, split: bool, dry_run: bool) -> int:
    config = resolve_config(home)
    engine = _store_engine(config, create=not dry_run)
    # A dry run must answer "what would be imported?" without creating anything.
    # ``plan_migration`` only reads (the store's texts, its migration.json), so it
    # is safe to plan against an absent store via the empty stand-in.
    plan = _plan(home, engine=engine if engine is not None else _AbsentStore(), split=split)
    if dry_run:
        payload = {"ok": not plan.blocked, "plan": plan.as_dict(), "added": 0,
                   "skipped": len(plan.skipped), "backup": None,
                   "store_exists": engine is not None}
        lines = [f"plan: {plan.status} — {len(plan.adds)} to add, "
                 f"{len(plan.skipped)} duplicate(s), {len(plan.unresolved)} unresolved"]
        if engine is None:
            lines.append("no OptMem store yet: a real run would create one")
        lines += [f"  - {reason}" for reason in plan.reasons]
        _emit(payload, as_json, lines)
        return 0 if not plan.blocked else 1
    assert engine is not None  # create=not dry_run, and dry_run returned above

    mem_path, user_path = native_memory_paths(home)
    backup = backup_native_files(home, paths=(mem_path, user_path))
    try:
        result = apply_migration(engine, plan)
    except ValueError as exc:
        payload = {"ok": False, "plan": plan.as_dict(), "added": 0,
                   "skipped": len(plan.skipped), "backup": backup, "reasons": list(plan.reasons)}
        lines = [f"migration blocked: {exc}", f"backup kept at {backup['dir']}"]
        lines += [f"  - {reason}" for reason in plan.reasons]
        _emit(payload, as_json, lines)
        return 1

    payload = {"ok": True, "plan": plan.as_dict(), **result, "backup": backup}
    lines = [
        f"imported {result['added']} entr{'y' if result['added'] == 1 else 'ies'} "
        f"({result['skipped']} duplicate(s) skipped), dated {result['date']}",
        f"raw backup: {backup['dir']}",
    ]
    if plan.split_count:
        lines.append(f"{plan.split_count} over-long entr(y/ies) split on safe boundaries")
    lines.append("run `hermes optmem mode optmem-only --yes` to make OptMem the only store")
    _emit(payload, as_json, lines)
    return 0


def _cmd_mode(home: Path, target_mode: str, as_json: bool, *, yes: bool, split: bool) -> int:
    config = resolve_config(home)
    engine = _store_engine(config, create=False)
    backup = _latest_backup(home)
    plan = _plan(home, engine=engine, split=split) if engine is not None else None
    report = plan_mode_switch(home, target_mode, engine=engine, backup=backup, plan=plan)

    if not yes:
        report = {**report, "ok": False,
                  "reasons": [*report.get("reasons", []),
                              "refusing to write without --yes (this command changes config.yaml)"]}
    if not report["ok"]:
        lines = [f"mode switch to {target_mode}: NOT applied"]
        lines += [f"  - {reason}" for reason in report.get("reasons", [])]
        _emit(report, as_json, lines)
        return 1

    result = apply_mode_switch(home, target_mode, engine=engine, backup=backup, plan=plan)
    if not result["ok"]:
        lines = [f"mode switch to {target_mode}: NOT applied"]
        lines += [f"  - {reason}" for reason in result.get("reasons", [])]
        _emit(result, as_json, lines)
        return 1
    lines = [
        f"mode: {result['mode']}",
        f"config.yaml: {result['changes']} (previous file kept at {result.get('config_backup')})",
        "restart the gateway for the change to take effect; `hermes optmem rollback` undoes it",
    ]
    _emit(result, as_json, lines)
    return 0


def _cmd_import(home: Path, file: str, as_json: bool, *, dedupe: bool) -> int:
    config = resolve_config(home)
    engine = _store_engine(config, create=True)
    assert engine is not None
    raw = Path(file).read_text(encoding="utf-8")
    parsed = engine.parse_import_lines(raw.splitlines())
    if dedupe:
        from .engine import _normalize

        existing = {_normalize(record[2]) for record in engine._all_records()}
        kept, skipped = [], 0
        seen: set[str] = set()
        for entry in parsed:
            key = _normalize(entry[1])
            if key in existing or key in seen:
                skipped += 1
                continue
            seen.add(key)
            kept.append(entry)
        parsed = kept
    else:
        skipped = 0
    ids = list(range(engine.log_len(), engine.log_len() + len(parsed)))
    if parsed:
        engine.import_lines_pairs(parsed)
    payload = {"ok": True, "added": len(parsed), "skipped": skipped, "ids": ids, "file": file}
    lines = [f"imported {len(parsed)} entr{'y' if len(parsed) == 1 else 'ies'} "
             f"({skipped} duplicate(s) skipped) from {file}"]
    _emit(payload, as_json, lines)
    return 0


def _cmd_rollback(home: Path, as_json: bool) -> int:
    result = rollback_mode(home)
    lines = ([f"restored {result.get('restored_config')}", f"mode: {result.get('mode')}"]
             if result["ok"] else ["rollback: nothing to undo"]
             + [f"  - {reason}" for reason in result.get("reasons", [])])
    _emit(result, as_json, lines)
    return 0 if result["ok"] else 1


def _latest_backup(home: Path) -> dict[str, Any] | None:
    """Most recent native backup directory under ``<home>/optmem_backups``, if any."""
    root = home / "optmem_backups"
    if not root.is_dir():
        return None
    candidates = sorted((p for p in root.glob("native-*") if p.is_dir()), reverse=True)
    for candidate in candidates:
        manifest = candidate / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(data, dict):
            return {
                "dir": str(candidate),
                "files": data.get("files") or [],
                "created": data.get("created"),
            }
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point (``python -m optmem.cli``); the host calls register_cli."""
    parser = argparse.ArgumentParser(prog=PROG)
    register_cli(parser)
    return optmem_command(parser.parse_args(list(argv) if argv is not None else sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
