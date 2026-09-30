"""Real Hermes integration for the OptMem provider.

Everything here drives the host's own seams — plugin discovery and loading, the
declared ``config_schema.py`` loaded by path, the CLI command the host registers,
the built-in memory surface the host gates on ``memory.memory_enabled`` /
``memory.user_profile_enabled``, and the provider's local-only guarantees — with
only the *installed metadata* faked (an entry point, exactly as the host's own
``tests/plugins/memory`` do). The provider under test is the real one.

The host (``hermes-agent``) is located from the installed ``agent`` package; when
it is not importable (the plugin's own CI, no Hermes on the path) the module
skips. Every test runs against an isolated ``HERMES_HOME`` and never writes to a
live profile.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import socket
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = PROJECT_ROOT / "optmem"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _find_host_root() -> Path | None:
    """The hermes-agent tree, derived from the installed ``agent`` package."""
    try:
        import agent  # noqa: PLC0415
    except Exception:
        return None
    return Path(agent.__file__).resolve().parent.parent


HOST_ROOT = _find_host_root()
if HOST_ROOT is None:
    pytest.skip(
        "Hermes host not importable; host-integration tests need hermes-agent on sys.path",
        allow_module_level=True,
    )
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

import agent.prompt_builder as host_prompt_builder  # noqa: E402
import plugins.memory as host_memory  # noqa: E402
import tools.memory_tool as host_memory_tool  # noqa: E402
from agent.turn_context import _tick_memory_nudge  # noqa: E402
from hermes_cli.web_routers.memory_providers import _flat_json_path, _read_flat_json  # noqa: E402
from plugins.memory import config_schema as host_config_schema  # noqa: E402
from plugins.memory.config_schema import STORAGE_FLAT_JSON  # noqa: E402

from optmem import (  # noqa: E402
    OptMemProvider,
    migrate,
)
from optmem.config import (  # noqa: E402
    EDITABLE_KEYS,
    declared_config_path,
    default_hermes_home,
    resolve_config,
    write_declared_config,
)
from optmem.engine import OptMemEngine  # noqa: E402

EXPECTED_TOOLS = {
    "optmem_note",
    "optmem_recall",
    "optmem_nap",
    "optmem_wake",
    "optmem_zoom",
    "optmem_forget",
    "optmem_config",
    "optmem_import",
    "optmem_init",
}

HYBRID_CONFIG = (
    "# keep me\nagent:\n  max_turns: 100\n"
    "memory:\n  provider: optmem\n  memory_enabled: true\n  user_profile_enabled: true\n"
)


# ---------------------------------------------------------------------------
# Discovery fixtures (metadata only; the provider itself is real)
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """Mirrors the importlib.metadata surface ``plugins.memory`` reads."""

    group = host_memory.ENTRY_POINTS_GROUP

    def __init__(self, name: str, value: str) -> None:
        self.name, self.value = name, value

    def load(self):
        import importlib

        module_name, _, attr = self.value.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attr) if attr else module


class _FakeEntryPoints(list):
    def select(self, *, group):
        return [ep for ep in self if ep.group == group]


@pytest.fixture
def optmem_entry_point(monkeypatch):
    """Replace the entry-point scan with an empty, fillable registry."""
    registry = _FakeEntryPoints()
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: registry)
    return registry


@pytest.fixture
def installed(optmem_entry_point):
    """An environment where the optmem distribution's entry point is installed."""
    optmem_entry_point.append(_FakeEntryPoint("optmem", "optmem"))
    return optmem_entry_point


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """A throwaway profile home; no live profile is ever touched."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_hybrid_config(home: Path) -> None:
    (home / "config.yaml").write_text(HYBRID_CONFIG, encoding="utf-8")


def _write_native_entries(home: Path) -> None:
    memories = home / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (memories / "MEMORY.md").write_text(
        "Durable fact: the owner prefers concise summaries.", encoding="utf-8"
    )
    (memories / "USER.md").write_text(
        "Owner profile: works on a small product team.", encoding="utf-8"
    )


def _switch_to_optmem_only(home: Path):
    """Run the plugin's real verified switch: migrate, back up, then flip the mode."""
    engine = OptMemEngine(str(home / "optmem_memory"))
    plan = migrate.plan_migration(home, engine=engine)
    assert not plan.blocked, plan.reasons
    migrate.apply_migration(engine, plan)
    backup = migrate.backup_native_files(home)
    result = migrate.apply_mode_switch(home, "optmem-only", engine=engine, backup=backup, plan=plan)
    assert result["ok"], result.get("reasons")
    return engine, backup


class _NudgeStub:
    """The minimal agent surface the real ``_tick_memory_nudge`` reads."""

    def __init__(self, store) -> None:
        self._memory_nudge_interval = 3
        self._turns_since_memory = 0
        self.valid_tool_names = {"memory"}
        self._memory_store = store


def _tool_names(provider) -> set[str]:
    return {schema["name"] for schema in provider.get_tool_schemas()}


def _assert_real_optmem_provider(provider) -> None:
    """The host imports an out-of-tree provider under its synthetic namespace, so
    the class object is intentionally NOT the top-level ``optmem.OptMemProvider``;
    assert on identity of behaviour and origin instead."""
    assert provider is not None
    assert type(provider).__name__ == "OptMemProvider"
    module = sys.modules[type(provider).__module__]
    assert Path(module.__file__).resolve().is_relative_to(PLUGIN_PACKAGE.resolve())
    assert provider.name == "optmem"
    assert _tool_names(provider) == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# Discovery and loading
# ---------------------------------------------------------------------------


class TestHostDiscovery:
    def test_entry_point_metadata_matches_the_host_scanner(self):
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["entry-points"][host_memory.ENTRY_POINTS_GROUP]
        assert declared == {"optmem": "optmem"}

    def test_provider_is_discovered_from_its_entry_point(self, installed, isolated_home):
        assert "optmem" in host_memory.list_memory_provider_names()
        found = host_memory.find_provider_dir("optmem")
        assert found is not None and found.resolve() == PLUGIN_PACKAGE.resolve()
        available = {name: ok for name, _desc, ok in host_memory.discover_memory_providers()}
        assert available.get("optmem") is True

    def test_provider_loads_with_its_full_tool_surface(self, installed, isolated_home):
        provider = host_memory.load_memory_provider("optmem")
        _assert_real_optmem_provider(provider)

    def test_project_local_copy_is_discovered_without_an_entry_point(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda: _FakeEntryPoints())
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        work = tmp_path / "work"
        plugins_dir = work / ".hermes" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "optmem").symlink_to(PLUGIN_PACKAGE, target_is_directory=True)
        monkeypatch.chdir(work)
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")

        found = host_memory.find_provider_dir("optmem")
        assert found is not None and found.resolve() == PLUGIN_PACKAGE.resolve()
        provider = host_memory.load_memory_provider("optmem")
        _assert_real_optmem_provider(provider)


# ---------------------------------------------------------------------------
# Declared config schema, loaded by path
# ---------------------------------------------------------------------------


class TestDeclaredConfigSchema:
    def test_host_loads_the_schema_by_path_as_real_dataclasses(self, installed, isolated_home):
        module = host_memory.import_provider_module("optmem", "config_schema")
        assert module.__file__ == str(PLUGIN_PACKAGE / "config_schema.py")
        schema = module.CONFIG_SCHEMA
        assert isinstance(schema, host_config_schema.ProviderConfigSchema)
        assert isinstance(schema.fields[0], host_config_schema.ProviderField)
        assert schema.name == "optmem"
        assert schema.storage == STORAGE_FLAT_JSON
        assert {field.key for field in schema.fields} == set(EDITABLE_KEYS)
        assert not [f for f in schema.fields if f.kind == host_config_schema.KIND_SECRET]

    def test_host_schema_cache_returns_the_same_schema(self, installed, isolated_home):
        schema = host_config_schema.get_provider_config_schema("optmem")
        assert schema is not None and schema.name == "optmem"
        assert schema is host_config_schema.get_provider_config_schema("optmem")

    def test_flat_json_storage_path_matches_the_plugins_resolver(self, installed, isolated_home):
        schema = host_config_schema.get_provider_config_schema("optmem")
        assert schema is not None
        # The host resolves flat_json to <HERMES_HOME>/<name>/config.json — exactly
        # the path optmem/config.py reads and writes.
        assert _flat_json_path(schema) == declared_config_path(isolated_home)

    def test_host_reads_back_a_plugin_written_declared_config(self, installed, isolated_home):
        write_declared_config(isolated_home, {"mode": "optmem-only", "wake_budget": 8})
        schema = host_config_schema.get_provider_config_schema("optmem")
        data = _read_flat_json(schema)
        assert data["mode"] == "optmem-only"
        assert data["wake_budget"] == 8


# ---------------------------------------------------------------------------
# CLI command the host registers
# ---------------------------------------------------------------------------


class TestHostCliRegistration:
    def _command(self, home: Path):
        _write_hybrid_config(home)
        commands = host_memory.discover_plugin_cli_commands()
        assert [command["name"] for command in commands] == ["optmem"]
        command = commands[0]
        assert callable(command["setup_fn"]) and callable(command["handler_fn"])
        assert command["handler_fn"].__name__ == "optmem_command"
        return command

    def _parse(self, command, argv):
        parser = argparse.ArgumentParser(prog="hermes")
        subparsers = parser.add_subparsers(dest="cmd")
        command["setup_fn"](subparsers.add_parser("optmem"))
        return parser.parse_args(["optmem", *argv])

    def test_read_only_status_via_host_registration_writes_nothing(
        self, installed, isolated_home, capsys
    ):
        command = self._command(isolated_home)
        before = (isolated_home / "config.yaml").read_bytes()
        args = self._parse(command, ["status", "--json"])
        assert command["handler_fn"](args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "hybrid"
        assert payload["capabilities"]["semantic_conflict_resolution"] is False
        assert not declared_config_path(isolated_home).exists()
        assert (isolated_home / "config.yaml").read_bytes() == before

    def test_mode_switch_refuses_without_yes_and_writes_nothing(
        self, installed, isolated_home, capsys
    ):
        command = self._command(isolated_home)
        before = (isolated_home / "config.yaml").read_bytes()
        args = self._parse(command, ["mode", "optmem-only", "--json"])
        assert command["handler_fn"](args) != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert (isolated_home / "config.yaml").read_bytes() == before
        assert not declared_config_path(isolated_home).exists()


# ---------------------------------------------------------------------------
# Native memory surface: tools / guidance / nudges off under OptMem-only
# ---------------------------------------------------------------------------


class TestNativeSurfaceExclusivity:
    def test_hybrid_keeps_the_native_memory_surface(self, installed, isolated_home):
        _write_hybrid_config(isolated_home)
        _write_native_entries(isolated_home)

        assert host_memory_tool.get_builtin_memory_store_flags() == (True, True)
        assert host_memory_tool.check_memory_requirements() is True
        assert host_prompt_builder.build_memory_guidance(True, True)

        agent = _NudgeStub(object())
        assert [_tick_memory_nudge(agent) for _ in range(3)] == [False, False, True]

        provider = host_memory.load_memory_provider("optmem")
        assert _tool_names(provider) == EXPECTED_TOOLS

    def test_optmem_only_disables_the_native_memory_surface(self, installed, isolated_home):
        _write_hybrid_config(isolated_home)
        _write_native_entries(isolated_home)
        _switch_to_optmem_only(isolated_home)

        text = (isolated_home / "config.yaml").read_text(encoding="utf-8")
        assert "memory_enabled: false" in text and "user_profile_enabled: false" in text
        assert "# keep me" in text and "max_turns: 100" in text
        assert resolve_config(isolated_home).mode == "optmem-only"

        # The built-in memory tool is no longer offered ...
        assert host_memory_tool.get_builtin_memory_store_flags() == (False, False)
        assert host_memory_tool.check_memory_requirements() is False
        # ... its guidance is not injected ...
        assert host_prompt_builder.build_memory_guidance(False, False) == ""
        # ... and the turn-based memory nudge never fires (no store is built).
        agent = _NudgeStub(None)
        assert [_tick_memory_nudge(agent) for _ in range(4)] == [False, False, False, False]
        # OptMem itself is still the active store.
        provider = host_memory.load_memory_provider("optmem")
        assert _tool_names(provider) == EXPECTED_TOOLS

    def test_switching_back_to_hybrid_restores_the_native_surface(self, installed, isolated_home):
        _write_hybrid_config(isolated_home)
        _write_native_entries(isolated_home)
        engine, backup = _switch_to_optmem_only(isolated_home)
        assert host_memory_tool.check_memory_requirements() is False

        result = migrate.apply_mode_switch(isolated_home, "hybrid", engine=engine, backup=backup)
        assert result["ok"], result.get("reasons")
        assert host_memory_tool.get_builtin_memory_store_flags() == (True, True)
        assert host_memory_tool.check_memory_requirements() is True


# ---------------------------------------------------------------------------
# Local-only: no credentials, no network, no live writes
# ---------------------------------------------------------------------------


class TestLocalOnly:
    def test_provider_needs_no_credentials_and_makes_no_network_calls(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        provider = OptMemProvider()
        assert provider.is_available() is True
        capabilities = provider.capabilities()
        assert capabilities["local_only"] is True
        assert capabilities["semantic_conflict_resolution"] is False

        def _no_network(*args, **kwargs):
            raise AssertionError("the provider attempted network access")

        monkeypatch.setattr(socket, "socket", _no_network)
        provider.initialize("session", hermes_home=str(home))
        note = json.loads(
            provider.handle_tool_call(
                "optmem_note", {"text": "Owner approved durable decision alpha."}
            )
        )
        assert note["status"] == "added"
        recall = json.loads(provider.handle_tool_call("optmem_recall", {"query": "alpha"}))
        assert recall["count"] == 1
        assert json.loads(provider.handle_tool_call("optmem_wake", {}))["count"] >= 1

    def test_every_write_lands_under_the_isolated_home(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # The plugin's own home resolver honors the override rather than ~/.hermes.
        assert default_hermes_home() == str(home)

        provider = OptMemProvider()
        provider.initialize("session", hermes_home=str(home))
        provider.handle_tool_call("optmem_note", {"text": "Owner approved durable decision alpha."})

        assert Path(provider._memory_dir).is_relative_to(home)
        assert (home / "optmem_memory" / "LOG.txt").exists()
        assert Path(resolve_config(home).memory_dir).is_relative_to(home)
