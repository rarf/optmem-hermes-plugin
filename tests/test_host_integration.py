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
from hermes_cli.plugins import get_plugin_auxiliary_tasks  # noqa: E402
from hermes_cli.web_routers.memory_providers import _flat_json_path, _read_flat_json  # noqa: E402
from plugins.memory import config_schema as host_config_schema  # noqa: E402
from plugins.memory.config_schema import STORAGE_FLAT_JSON  # noqa: E402

from optmem import (  # noqa: E402
    SUMMARY_AUX_TASK,
    OptMemProvider,
    migrate,
)
from optmem import register as optmem_register  # noqa: E402
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
    "memory:\n  provider: optmem-hermes\n  memory_enabled: true\n  user_profile_enabled: true\n"
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
    """An environment where the optmem distribution's entry point is installed.

    The entry-point *key* is the canonical registered provider name
    (``optmem-hermes``); its value is the Python package, which keeps ``optmem``.
    """
    optmem_entry_point.append(_FakeEntryPoint("optmem-hermes", "optmem"))
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


def _plugin_manifest_version() -> str:
    """The version declared by the shipped plugin.yaml (stdlib parse)."""
    for line in (PLUGIN_PACKAGE / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "version":
            return value.strip().strip("'\"")
    raise AssertionError("plugin.yaml declares no version")


def _assert_real_optmem_provider(provider) -> None:
    """The host imports an out-of-tree provider under its synthetic namespace, so
    the class object is intentionally NOT the top-level ``optmem.OptMemProvider``;
    assert on identity of behaviour and origin instead."""
    assert provider is not None
    assert type(provider).__name__ == "OptMemProvider"
    module = sys.modules[type(provider).__module__]
    assert Path(module.__file__).resolve().is_relative_to(PLUGIN_PACKAGE.resolve())
    assert provider.name == "optmem-hermes"
    assert _tool_names(provider) == EXPECTED_TOOLS


# ---------------------------------------------------------------------------
# Discovery and loading
# ---------------------------------------------------------------------------


class TestHostDiscovery:
    def test_entry_point_metadata_matches_the_host_scanner(self):
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["entry-points"][host_memory.ENTRY_POINTS_GROUP]
        # The key is the canonical registered provider name; the value is the
        # Python package, which keeps its name.
        assert declared == {"optmem-hermes": "optmem"}

    def test_provider_is_discovered_from_its_entry_point(self, installed, isolated_home):
        assert "optmem-hermes" in host_memory.list_memory_provider_names()
        found = host_memory.find_provider_dir("optmem-hermes")
        assert found is not None and found.resolve() == PLUGIN_PACKAGE.resolve()
        available = {name: ok for name, _desc, ok in host_memory.discover_memory_providers()}
        assert available.get("optmem-hermes") is True

    def test_provider_loads_with_its_full_tool_surface(self, installed, isolated_home):
        provider = host_memory.load_memory_provider("optmem-hermes")
        _assert_real_optmem_provider(provider)

    def test_project_local_copy_is_discovered_without_an_entry_point(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda: _FakeEntryPoints())
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        work = tmp_path / "work"
        plugins_dir = work / ".hermes" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "optmem-hermes").symlink_to(PLUGIN_PACKAGE, target_is_directory=True)
        monkeypatch.chdir(work)
        monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")

        found = host_memory.find_provider_dir("optmem-hermes")
        assert found is not None and found.resolve() == PLUGIN_PACKAGE.resolve()
        provider = host_memory.load_memory_provider("optmem-hermes")
        _assert_real_optmem_provider(provider)


# ---------------------------------------------------------------------------
# Declared config schema, loaded by path
# ---------------------------------------------------------------------------


class TestDeclaredConfigSchema:
    def test_host_loads_the_schema_by_path_as_real_dataclasses(self, installed, isolated_home):
        module = host_memory.import_provider_module("optmem-hermes", "config_schema")
        # Host module caching may retain the project-local symlink path from
        # discovery; both paths must resolve to the same real schema source.
        assert Path(module.__file__).resolve() == (PLUGIN_PACKAGE / "config_schema.py").resolve()
        schema = module.CONFIG_SCHEMA
        assert isinstance(schema, host_config_schema.ProviderConfigSchema)
        assert isinstance(schema.fields[0], host_config_schema.ProviderField)
        # The schema `name` is the declared-config *directory* key, not the
        # registered provider name: keeping it `optmem` leaves the documented
        # data path `<HERMES_HOME>/optmem/config.json` unchanged across the
        # rename (see the upgrade notes).
        assert schema.name == "optmem"
        assert schema.storage == STORAGE_FLAT_JSON
        assert {field.key for field in schema.fields} == set(EDITABLE_KEYS)
        assert not [f for f in schema.fields if f.kind == host_config_schema.KIND_SECRET]

    def test_host_schema_cache_returns_the_same_schema(self, installed, isolated_home):
        schema = host_config_schema.get_provider_config_schema("optmem-hermes")
        assert schema is not None and schema.name == "optmem"
        assert schema is host_config_schema.get_provider_config_schema("optmem-hermes")

    def test_flat_json_storage_path_matches_the_plugins_resolver(self, installed, isolated_home):
        schema = host_config_schema.get_provider_config_schema("optmem-hermes")
        assert schema is not None
        # The host resolves flat_json to <HERMES_HOME>/<name>/config.json — exactly
        # the path optmem/config.py reads and writes.
        assert _flat_json_path(schema) == declared_config_path(isolated_home)

    def test_host_reads_back_a_plugin_written_declared_config(self, installed, isolated_home):
        write_declared_config(isolated_home, {"mode": "optmem-only", "wake_budget": 8})
        schema = host_config_schema.get_provider_config_schema("optmem-hermes")
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
        assert [command["name"] for command in commands] == ["optmem-hermes"]
        command = commands[0]
        assert callable(command["setup_fn"])
        # The host derives ``handler_fn`` as ``<memory.provider>_command``
        # (main.py:_attach_plugin_cli_command); a hyphenated provider name has no
        # such attribute, so the CLI binds ``func`` itself (like the bundled
        # honcho CLI does) — see optmem/cli.py:register_cli.
        assert command["handler_fn"] is None
        return command

    def _parse(self, command, argv):
        parser = argparse.ArgumentParser(prog="hermes")
        subparsers = parser.add_subparsers(dest="cmd")
        sub = subparsers.add_parser("optmem-hermes")
        command["setup_fn"](sub)
        args = parser.parse_args(["optmem-hermes", *argv])
        handler = getattr(args, "func", None)
        assert callable(handler) and handler.__name__ == "optmem_command"
        return args, handler

    def test_read_only_status_via_host_registration_writes_nothing(
        self, installed, isolated_home, capsys
    ):
        command = self._command(isolated_home)
        before = (isolated_home / "config.yaml").read_bytes()
        args, handler = self._parse(command, ["status", "--json"])
        assert handler(args) == 0
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
        args, handler = self._parse(command, ["mode", "optmem-only", "--json"])
        assert handler(args) != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert (isolated_home / "config.yaml").read_bytes() == before
        assert not declared_config_path(isolated_home).exists()

    def test_version_via_host_registration_reports_the_loaded_copy(
        self, installed, isolated_home, capsys
    ):
        command = self._command(isolated_home)
        # The host imports cli.py BY PATH under a synthetic package shell that
        # never executes optmem/__init__.py, so a package-relative
        # `from . import __version__` would raise ImportError here.
        args, handler = self._parse(command, ["version", "--json"])
        assert handler.__module__.startswith("_hermes_user_memory.")
        assert handler(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["version"] == _plugin_manifest_version()

    def test_version_via_host_registration_ignores_stale_metadata(
        self, installed, isolated_home, capsys, monkeypatch
    ):
        command = self._command(isolated_home)
        # A stale editable install must not misreport the by-path copy the host
        # actually loaded.
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.0-stale")
        args, handler = self._parse(command, ["version", "--json"])
        assert handler(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["version"] == _plugin_manifest_version()


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

        provider = host_memory.load_memory_provider("optmem-hermes")
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
        provider = host_memory.load_memory_provider("optmem-hermes")
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


# ---------------------------------------------------------------------------
# Plugin Doctor: the manifest must describe a memory provider's real surface
# ---------------------------------------------------------------------------


class TestHostPluginDoctor:
    def test_doctor_reports_no_errors_and_no_capability_mismatches(self):
        """``hermes plugins doctor`` (the real runtime contract) must be clean.

        A memory provider exposes its tools and lifecycle hooks through the
        ``MemoryProvider`` interface — ``get_tool_schemas`` / ``on_memory_write``
        / ``on_turn_start`` — which the host's ``MemoryManager`` wires directly.
        It does NOT register them through the plugin SDK, so the manifest must
        declare neither ``provides_tools`` nor ``provides_hooks``:

        * ``provides_hooks`` names outside ``VALID_HOOKS`` (``on_memory_write``,
          ``on_turn_start`` are not plugin hooks) are hard Doctor ERRORS, and
        * a declared-but-unregistered tool/hook is a Doctor WARNING.

        Every bundled provider (honcho, mem0, holographic) declares neither
        list for exactly this reason.
        """
        host_dev = pytest.importorskip("hermes_cli.plugin_dev")
        report = host_dev.doctor_plugin(PLUGIN_PACKAGE)
        errors = [f.message for f in report.findings if f.level == "error"]
        mismatches = [f.message for f in report.findings if "did not add it" in f.message]
        assert errors == [], errors
        assert mismatches == [], mismatches
        assert report.ok


# ---------------------------------------------------------------------------
# Opt-in LLM summaries: native auxiliary task + real PluginLlm facade
# ---------------------------------------------------------------------------


def _owner_ctx():
    """A REAL ``PluginContext`` for the plugin id, plus a provider capture shim.

    The plugin is an *exclusive* (memory) plugin, so the general PluginManager
    never calls its ``register(ctx)``; we build the same ``PluginContext`` the
    manager would and drive the plugin through it. The shim records the provider
    instance while forwarding ``register_auxiliary_task`` and ``llm`` to the real
    context, so the test observes both registrations and facade capture.
    """
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    manager = get_plugin_manager()
    real = PluginContext(PluginManifest(name="optmem-hermes", key="optmem-hermes"), manager)
    captured: dict = {}

    class _Shim:
        @property
        def plugin_id(self):
            return real.plugin_id

        @property
        def llm(self):
            return real.llm

        def register_auxiliary_task(self, *args, **kwargs):
            return real.register_auxiliary_task(*args, **kwargs)

        def register_memory_provider(self, provider):
            captured["provider"] = provider

    return real, _Shim(), captured


def _fake_response(text: str):
    import types

    return types.SimpleNamespace(
        model="fake-model",
        usage=types.SimpleNamespace(prompt_tokens=4, completion_tokens=6, total_tokens=10),
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
    )


class TestLlmSummaryHostIntegration:
    def test_register_adds_the_plugin_owned_auxiliary_task(self, installed, isolated_home):
        get_plugin_auxiliary_tasks()  # trigger idempotent discovery first
        real, shim, captured = _owner_ctx()
        optmem_register(shim)

        entry = next(
            (e for e in get_plugin_auxiliary_tasks() if e["key"] == SUMMARY_AUX_TASK), None
        )
        assert entry is not None, "optmem_summary was not registered on the host"
        assert entry["plugin"] == real.plugin_id
        assert entry["display_name"] == "OptMem summaries"
        assert entry["defaults"]["provider"] == "auto"
        assert entry["defaults"]["model"] == ""
        assert entry["defaults"]["timeout"] == 60
        # The provider captured the SUPPORTED facade handed to it by register().
        assert captured["provider"]._summary_llm() is real.llm

    def test_aux_task_routes_through_a_real_plugin_llm_without_credentials(
        self, installed, isolated_home
    ):
        from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test

        get_plugin_auxiliary_tasks()
        real, shim, _ = _owner_ctx()
        optmem_register(shim)

        captured: dict = {}

        def fake_transport(**kwargs):
            captured.update(kwargs)
            return "fake", "fake-model", _fake_response("resumo do bloco")

        llm = make_plugin_llm_for_test(
            plugin_id=real.plugin_id,
            policy=_TrustPolicy(plugin_id=real.plugin_id),
            sync_caller=fake_transport,
        )
        result = llm.complete(
            [{"role": "user", "content": "hi"}],
            task=SUMMARY_AUX_TASK,
            max_tokens=120,
            temperature=0.1,
            purpose="optmem.auto_nap",
        )
        # The task gate allowed the plugin's OWN task and routed it; no creds used.
        assert captured["task"] == SUMMARY_AUX_TASK
        assert captured["max_tokens"] == 120
        assert result.text == "resumo do bloco"

    def test_foreign_auxiliary_task_is_denied(self, installed, isolated_home):
        from agent.plugin_llm import PluginLlmTrustError, _TrustPolicy, make_plugin_llm_for_test

        get_plugin_auxiliary_tasks()
        real, shim, _ = _owner_ctx()
        optmem_register(shim)

        llm = make_plugin_llm_for_test(
            plugin_id=real.plugin_id,
            policy=_TrustPolicy(plugin_id=real.plugin_id),
            sync_caller=lambda **kw: ("fake", "fake", _fake_response("x")),
        )
        with pytest.raises(PluginLlmTrustError):
            llm.complete([{"role": "user", "content": "hi"}], task="compression")

    def test_aux_config_resolver_layers_plugin_defaults_under_user_config(
        self, installed, isolated_home
    ):
        from agent.auxiliary_client import _get_auxiliary_task_config, _get_task_timeout

        get_plugin_auxiliary_tasks()
        real, shim, _ = _owner_ctx()
        optmem_register(shim)

        # No user config: the plugin's declared defaults are the effective route.
        defaults = _get_auxiliary_task_config(SUMMARY_AUX_TASK)
        assert defaults["provider"] == "auto"
        assert defaults["model"] == ""
        assert _get_task_timeout(SUMMARY_AUX_TASK) == 60

        # The operator overrides provider/model in config.yaml; timeout default stays.
        (isolated_home / "config.yaml").write_text(
            "auxiliary:\n  optmem_summary:\n    provider: openrouter\n    model: vendor/model-x\n",
            encoding="utf-8",
        )
        from hermes_cli.config import load_config_readonly

        cache_clear = getattr(load_config_readonly, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
        overridden = _get_auxiliary_task_config(SUMMARY_AUX_TASK)
        assert overridden["provider"] == "openrouter"
        assert overridden["model"] == "vendor/model-x"
        assert overridden["timeout"] == 60  # plugin default preserved

    def test_memory_loader_context_borrows_a_compatible_facade(
        self, installed, isolated_home
    ):
        """The approved bridge, stated as a fact.

        ``_ProviderCollector`` still forwards only ``register_*`` calls, so the
        public ``ctx.llm`` raises AttributeError — but its private
        ``_plugin_context()`` returns a REAL ``PluginContext`` whose identity is
        the SAME provider name we register ``optmem_summary`` under. The adapter
        borrows that facade, so LLM summaries work on the real memory-provider
        path with no host change. Public ``ctx.llm`` always wins when present.
        """
        from optmem import _capture_summary_facade, _register_summary_aux_task

        get_plugin_auxiliary_tasks()  # idempotent discovery first (registry is stable)
        collector = host_memory._ProviderCollector("optmem-hermes")
        with pytest.raises(AttributeError):
            _ = collector.llm  # the public surface still has no llm
        facade = _capture_summary_facade(collector)
        assert facade is not None, "the compatibility bridge should hand back a facade"
        assert type(facade).__name__ == "PluginLlm"
        assert facade._plugin_id == "optmem-hermes"
        # register_* forwards, so the auxiliary task registers under the same owner.
        assert _register_summary_aux_task(collector) is True
        entry = next(e for e in get_plugin_auxiliary_tasks() if e["key"] == SUMMARY_AUX_TASK)
        assert entry["plugin"] == "optmem-hermes"

    def test_loader_provider_captures_the_borrowed_facade(self, installed, isolated_home):
        provider = host_memory.load_memory_provider("optmem-hermes")
        _assert_real_optmem_provider(provider)
        facade = provider._summary_llm()
        assert facade is not None, "the loader path now has a real facade"
        assert type(facade).__name__ == "PluginLlm"
        # The directory loader binds identity to the package basename; pip binds
        # it to the entry-point key. Preserve that host-owned trust identity.
        provider_dir = host_memory.find_provider_dir("optmem-hermes")
        expected_identity = provider_dir.name if provider_dir else "optmem-hermes"
        assert facade._plugin_id == expected_identity

    def test_collector_facade_capture_is_write_free(self, installed, tmp_path, monkeypatch):
        """Borrowing the facade must not scaffold HERMES_HOME or write anything."""
        from optmem import _capture_summary_facade

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        before = sorted(p.relative_to(home) for p in home.rglob("*"))
        collector = host_memory._ProviderCollector("optmem-hermes")
        assert _capture_summary_facade(collector) is not None
        after = sorted(p.relative_to(home) for p in home.rglob("*"))
        assert after == before, "facade capture must not write to disk"

    def test_provider_falls_back_to_local_when_the_bridge_is_incompatible(
        self, installed, isolated_home, monkeypatch
    ):
        """Fail-closed end-to-end: a borrowed context whose identity is NOT our
        provider name yields no facade, so the provider keeps its local extractor."""
        from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

        from optmem import register_memory_provider

        get_plugin_auxiliary_tasks()
        collector = host_memory._ProviderCollector("optmem-hermes")
        collector._context = PluginContext(
            PluginManifest(name="someone-else", key="someone-else"), get_plugin_manager()
        )
        register_memory_provider(collector)
        provider = collector.provider
        _assert_real_optmem_provider(provider)
        assert provider._summary_llm() is None  # incompatible bridge → no facade

        monkeypatch.setenv("OPTMEM_LLM_SUMMARY", "1")
        provider.initialize("s", hermes_home=str(isolated_home))
        provider.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento Q3"})
        provider.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
        provider.on_turn_start(10, "trigger")
        assert (0, 2) not in provider._engine.pending_naps()
        summary = provider._engine._tree_get(0, 2)
        assert "aprov" in summary or "deploy" in summary

    def test_collector_provider_completes_through_the_borrowed_facade_without_network(
        self, installed, isolated_home
    ):
        """End-to-end: the collector's provider completes through a REAL
        ``PluginLlm`` whose transport is the host's supported test injection
        (``make_plugin_llm_for_test``) — no network, no credentials."""
        from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test

        from optmem import register_memory_provider
        from optmem.config import write_declared_config

        get_plugin_auxiliary_tasks()
        collector = host_memory._ProviderCollector("optmem-hermes")
        real = collector._plugin_context()
        captured: dict = {}

        def transport(**kwargs):
            captured.update(kwargs)
            return "fake", "fake-model", _fake_response("resumo do bloco")

        # ``PluginContext.__init__`` documents that tests preseed the lazy facade.
        real._llm = make_plugin_llm_for_test(
            plugin_id=real.plugin_id,
            policy=_TrustPolicy(plugin_id=real.plugin_id),
            sync_caller=transport,
        )
        register_memory_provider(collector)
        provider = collector.provider
        _assert_real_optmem_provider(provider)
        assert provider._summary_llm() is real.llm  # the borrowed facade

        # Enable the opt-in through the declared config (the supported path).
        write_declared_config(isolated_home, {"llm_summary": True})
        provider.initialize("s", hermes_home=str(isolated_home))
        provider.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento Q3"})
        provider.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
        provider.on_turn_start(10, "trigger")

        assert captured["task"] == SUMMARY_AUX_TASK
        assert captured["max_tokens"] == 120
        assert captured["temperature"] == 0.1
        assert provider._engine._tree_get(0, 2) == "resumo do bloco"
        assert (0, 2) not in provider._engine.pending_naps()
