"""The ``hermes optmem`` command tree — the real, scriptable config interface.

The host wires it as: ``discover_plugin_cli_commands()`` imports ``optmem/cli.py``
by path, calls ``register_cli(subparser)`` during argparse setup, and uses
``optmem_command`` as the handler (``hermes_cli/main.py``).

Contract pinned here:
- read-only commands never mutate anything;
- ``mode`` refuses without an explicit ``--yes`` and writes NOTHING when refused;
- a blocked migration exits non-zero and leaves the native store active;
- ``rollback`` restores the previous config.yaml and keeps OptMem data;
- ``--json`` prints machine-readable output on stdout;
- ``--hermes-home`` targets one profile explicitly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from optmem.cli import optmem_command, register_cli
from optmem.config import declared_config_path, resolve_config
from optmem.engine import ENTRY_CHARS, OptMemEngine
from optmem.migrate import DELIMITER

TODAY_STR = __import__("datetime").date.today().isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes optmem")
    register_cli(parser)
    return parser


def _run(argv, capsys):
    args = _parser().parse_args(argv)
    code = optmem_command(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _native(tmp_path, memory="", user=""):
    memories = tmp_path / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    if memory:
        (memories / "MEMORY.md").write_text(memory, encoding="utf-8")
    if user:
        (memories / "USER.md").write_text(user, encoding="utf-8")


def _config_yaml(tmp_path, memory_enabled=True, user_profile_enabled=True):
    (tmp_path / "config.yaml").write_text(
        "# keep me\nagent:\n  max_turns: 100\nmemory:\n  provider: optmem\n"
        f"  memory_enabled: {str(memory_enabled).lower()}\n"
        f"  user_profile_enabled: {str(user_profile_enabled).lower()}\n",
        encoding="utf-8",
    )


class TestParserRegistration:
    def test_register_cli_builds_the_command_tree(self):
        parser = _parser()
        # Actions with a required positional need one supplied; the point here is
        # that every action is wired and ``optmem_action`` is set accordingly.
        cases = {
            "status": ["status"],
            "show": ["show"],
            "mode": ["mode", "hybrid"],
            "migrate": ["migrate"],
            "check": ["check"],
            "rollback": ["rollback"],
            "import": ["import", "facts.txt"],
        }
        for action, argv in cases.items():
            args = parser.parse_args(argv)
            assert args.optmem_action == action

    def test_module_exposes_the_handler_the_host_looks_for(self):
        import optmem.cli as cli

        assert callable(cli.optmem_command)
        assert getattr(cli.optmem_command, "__name__", "") == "optmem_command"

    def test_hermes_home_and_json_flags_parse_before_and_after_the_action(self):
        parser = _parser()
        before = parser.parse_args(["--hermes-home", "/x", "status"])
        after = parser.parse_args(["status", "--hermes-home", "/x", "--json"])
        assert before.hermes_home == after.hermes_home == "/x"
        assert before.json is False and after.json is True

    def test_migrate_flags_parse(self):
        args = _parser().parse_args(["migrate", "--split", "--dry-run"])
        assert args.split is True and args.dry_run is True


class TestStatusAndShow:
    def test_status_is_read_only(self, tmp_path, capsys):
        _config_yaml(tmp_path)
        code, out, _ = _run(["status", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["mode"] == "hybrid"
        assert payload["store_entries"] == 0
        assert payload["native"]["memory_enabled"] is True
        assert not declared_config_path(tmp_path).exists()

    def test_status_reports_capabilities_and_limits(self, tmp_path, capsys):
        code, out, _ = _run(["status", "--hermes-home", str(tmp_path), "--json"], capsys)
        payload = json.loads(out)
        caps = payload["capabilities"]
        assert caps["structural_chaining"] is True
        assert caps["semantic_conflict_resolution"] is False

    def test_status_human_output_is_not_json(self, tmp_path, capsys):
        code, out, _ = _run(["status", "--hermes-home", str(tmp_path)], capsys)
        assert code == 0
        assert "mode" in out.lower()
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)

    def test_no_action_defaults_to_status(self, tmp_path, capsys):
        code, out, _ = _run(["--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0 and json.loads(out)["mode"] == "hybrid"

    def test_show_reports_effective_values_and_source(self, tmp_path, capsys):
        from optmem.config import write_declared_config

        write_declared_config(tmp_path, {"mode": "hybrid", "wake_budget": 32})
        code, out, _ = _run(["show", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["source"] == "declared"
        assert payload["wake_budget"] == 32
        assert payload["mode"] == "hybrid"

    def test_show_is_read_only(self, tmp_path, capsys):
        before = (tmp_path / "config.yaml")
        _run(["show", "--hermes-home", str(tmp_path)], capsys)
        assert not before.exists()


class TestMigrateCommand:
    def test_migrate_dry_run_writes_nothing(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["facto A duravel", "facto B duravel"]))
        code, out, _ = _run(
            ["migrate", "--dry-run", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["plan"]["adds"] == 2 and payload["added"] == 0
        assert payload["store_exists"] is False
        assert not (tmp_path / "optmem_memory").exists()  # --dry-run created nothing
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() == 0

    def test_migrate_imports_and_backs_up(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["facto A duravel"]))
        code, out, _ = _run(["migrate", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["added"] == 1
        assert payload["backup"]["dir"] and Path(payload["backup"]["dir"]).is_dir()
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() == 1

    def test_migrate_blocked_exits_nonzero_and_changes_nothing(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["z" * (ENTRY_CHARS + 120)]))
        code, out, err = _run(["migrate", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code != 0
        payload = json.loads(out)
        assert payload["plan"]["status"] == "blocked"
        assert payload["added"] == 0
        assert "--split" in err or "--split" in json.dumps(payload)
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() == 0

    def test_migrate_split_unblocks(self, tmp_path, capsys):
        long = "; ".join(f"decisao numero {i} sobre o telhado aprovada" for i in range(12))
        _native(tmp_path, memory=DELIMITER.join([long]))
        code, out, _ = _run(
            ["migrate", "--split", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code == 0
        assert json.loads(out)["added"] >= 3

    def test_migrate_is_idempotent(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["facto unico duravel"]))
        _run(["migrate", "--hermes-home", str(tmp_path)], capsys)
        code, out, _ = _run(["migrate", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0
        assert json.loads(out)["added"] == 0
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() == 1


class TestModeCommand:
    def _ready(self, tmp_path, capsys):
        _config_yaml(tmp_path)
        _native(tmp_path, memory=DELIMITER.join(["facto migrado"]), user=DELIMITER.join(["perfil"]))
        # migrate + backup through the CLI (exercises the real path). Read the
        # captured output so it cannot leak into the caller's capsys buffer and
        # corrupt the JSON assertions of the test that follows.
        code, out, _ = _run(["migrate", "--hermes-home", str(tmp_path)], capsys)
        assert code == 0, out

    def test_mode_requires_yes_and_writes_nothing_without_it(self, tmp_path, capsys):
        self._ready(tmp_path, capsys)
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        code, out, _ = _run(
            ["mode", "optmem-only", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code != 0
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "yes" in json.dumps(payload).lower()
        assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
        assert not declared_config_path(tmp_path).exists()

    def test_mode_optmem_only_after_verified_migration(self, tmp_path, capsys):
        self._ready(tmp_path, capsys)
        code, out, _ = _run(
            ["mode", "optmem-only", "--yes", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code == 0, out
        assert json.loads(out)["ok"] is True
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "memory_enabled: false" in text
        assert "user_profile_enabled: false" in text
        assert "# keep me" in text and "max_turns: 100" in text
        assert resolve_config(tmp_path).mode == "optmem-only"

    def test_mode_optmem_only_refused_when_not_migrated(self, tmp_path, capsys):
        _config_yaml(tmp_path)
        _native(tmp_path, memory=DELIMITER.join(["facto ainda nao migrado"]))
        code, out, _ = _run(
            ["mode", "optmem-only", "--yes", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code != 0
        payload = json.loads(out)
        assert payload["ok"] is False and payload["reasons"]
        assert "memory_enabled: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    def test_mode_back_to_hybrid_restores_native(self, tmp_path, capsys):
        self._ready(tmp_path, capsys)
        _run(["mode", "optmem-only", "--yes", "--hermes-home", str(tmp_path)], capsys)
        code, out, _ = _run(
            ["mode", "hybrid", "--yes", "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code == 0
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "memory_enabled: true" in text and "user_profile_enabled: true" in text
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() >= 1

    def test_mode_rejects_unknown_mode(self, tmp_path, capsys):
        # An unknown mode is rejected by argparse at parse time (choices=), before
        # any provider is constructed — nothing is written and no JSON is emitted.
        with pytest.raises(SystemExit) as exc:
            _run(["mode", "exclusive", "--yes", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert exc.value.code != 0
        assert not declared_config_path(tmp_path).exists()

    def test_rollback_restores_config_and_keeps_store(self, tmp_path, capsys):
        self._ready(tmp_path, capsys)
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        _run(["mode", "optmem-only", "--yes", "--hermes-home", str(tmp_path)], capsys)
        code, out, _ = _run(["rollback", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0 and json.loads(out)["ok"] is True
        assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
        assert resolve_config(tmp_path).mode == "hybrid"
        assert OptMemEngine(str(tmp_path / "optmem_memory")).log_len() >= 1

    def test_rollback_without_a_switch_is_a_clean_failure(self, tmp_path, capsys):
        code, out, _ = _run(["rollback", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code != 0
        assert json.loads(out)["ok"] is False


class TestCheckAndImport:
    def test_check_reports_not_ready_before_migration(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["facto por migrar"]))
        code, out, _ = _run(["check", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code != 0
        payload = json.loads(out)
        assert payload["ready"] is False and payload["reasons"]

    def test_check_reports_ready_after_migration(self, tmp_path, capsys):
        _native(tmp_path, memory=DELIMITER.join(["facto migrado"]))
        _run(["migrate", "--hermes-home", str(tmp_path)], capsys)
        code, out, _ = _run(["check", "--hermes-home", str(tmp_path), "--json"], capsys)
        assert code == 0
        assert json.loads(out)["ready"] is True

    def test_import_curated_file_dedupes_against_the_store(self, tmp_path, capsys):
        curated = tmp_path / "curated.txt"
        curated.write_text(
            f"{TODAY_STR} facto curado um\n{TODAY_STR} facto curado dois\n", encoding="utf-8"
        )
        code, out, _ = _run(
            ["import", str(curated), "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code == 0 and json.loads(out)["added"] == 2
        code, out, _ = _run(
            ["import", str(curated), "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert json.loads(out)["added"] == 0
        assert json.loads(out)["skipped"] == 2

    def test_import_rejects_a_bad_line(self, tmp_path, capsys):
        bad = tmp_path / "bad.txt"
        bad.write_text("sem data\n", encoding="utf-8")
        code, out, err = _run(
            ["import", str(bad), "--hermes-home", str(tmp_path), "--json"], capsys
        )
        assert code != 0
        assert "YYYY-MM-DD" in json.dumps(json.loads(out)) or "YYYY-MM-DD" in err
