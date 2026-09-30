"""Catalog-admission contract for the OptMem memory provider.

Hermes' plugin catalog refuses a ``memory`` entry that reuses the bare name of
an upstream project it is not affiliated with (``plugin-catalog/README.md``:
"the affiliated project gets the bare key"). This plugin is an independent
integration of Victor Taelin's upstream OptMem design, so its **registered
provider name**, its **manifest name** and its **entry-point key** must all be
``optmem-hermes`` — while the Python package stays ``optmem`` and the existing
data paths keep their names.

``hermes plugins validate`` additionally imports the plugin and calls its
``register(ctx)`` against a recording stub, then diffs the *actual*
registrations against the manifest's declared capabilities. The supported
memory-provider contract (see the host's ``plugins/memory/__init__.py`` and the
merged ``entropicmem`` entry) is exactly::

    def register_memory_provider(ctx):
        ctx.register_memory_provider(Provider())

    def register(ctx):
        register_memory_provider(ctx)

The provider's nine tools and two lifecycle hooks are exposed through the
``MemoryProvider`` interface (``get_tool_schemas`` / ``on_memory_write`` /
``on_turn_start``), which the host's ``MemoryManager`` wires directly. Registering
them a second time through ``ctx.register_tool`` / ``ctx.register_hook`` would
duplicate the tool surface (and register a hook name the host never dispatches),
so ``register()`` must register the provider and nothing else.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = PROJECT_ROOT / "optmem"
CANONICAL_NAME = "optmem-hermes"
UPSTREAM_BARE_NAME = "optmem"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _find_host_root() -> Path | None:
    """The hermes-agent tree, derived from the installed ``agent`` package.

    The host is an editable install that maps *packages* (``agent``,
    ``hermes_cli``, …); its top-level modules (``hermes_yaml``) only resolve
    once the tree itself is on ``sys.path`` — exactly what the host's own CLI
    does at startup. Absent a host (the plugin's standalone CI), the host-only
    assertions below skip instead of erroring.
    """
    try:
        import agent  # noqa: PLC0415
    except Exception:
        return None
    return Path(agent.__file__).resolve().parent.parent


HOST_ROOT = _find_host_root()
if HOST_ROOT is not None and str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

import optmem  # noqa: E402
from optmem import OptMemProvider  # noqa: E402

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
EXPECTED_HOOKS = {"on_memory_write", "on_turn_start"}


class RecordingContext:
    """The narrow plugin-context surface a memory provider's ``register`` uses.

    Mirrors the host's ``hermes_cli.plugin_validate`` probe stub: only the
    categories the admission diff audits are recorded, so a stray
    ``register_tool``/``register_hook`` call is visible as a failure here.
    """

    plugin_config: dict = {}
    profile_name = "default"
    plugin_id = "optmem_catalog_admission_probe"

    def __init__(self) -> None:
        self.providers: list = []
        self.tools: list = []
        self.hooks: list = []
        self.middleware: list = []
        self.commands: list = []
        self.skills: list = []

    def register_memory_provider(self, provider) -> None:
        self.providers.append(provider)

    def register_tool(self, name, *args, **kwargs) -> None:
        self.tools.append(str(name))

    def register_hook(self, hook_name, callback) -> None:
        self.hooks.append(str(hook_name))

    def register_middleware(self, kind, callback) -> None:
        self.middleware.append(str(kind))

    def register_cli_command(self, name, *args, **kwargs) -> None:
        self.commands.append(str(name))

    def register_skill(self, *args, **kwargs) -> None:
        self.skills.append(str(args[0] if args else kwargs.get("name")))

    def get_config(self, key, default=None):
        return default


def _load_manifest() -> dict:
    """Parse the shipped ``plugin.yaml`` (host reader when present, else PyYAML)."""
    text = (PLUGIN_PACKAGE / "plugin.yaml").read_text(encoding="utf-8-sig")
    try:
        import hermes_yaml as yaml  # noqa: PLC0415
    except Exception:
        try:
            import yaml  # noqa: PLC0415
        except Exception:
            pytest.skip("no YAML reader available for the manifest")
    return yaml.safe_load(text)


# ---------------------------------------------------------------------------
# Registration: the missing piece `hermes plugins validate` fails on
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_registers_exactly_one_memory_provider(self):
        ctx = RecordingContext()
        optmem.register(ctx)
        assert len(ctx.providers) == 1
        provider = ctx.providers[0]
        assert isinstance(provider, OptMemProvider)

    def test_register_memory_provider_helper_matches_the_supported_host_api(self):
        ctx = RecordingContext()
        optmem.register_memory_provider(ctx)
        assert [type(p).__name__ for p in ctx.providers] == ["OptMemProvider"]

    def test_register_does_not_duplicate_the_provider_tool_surface(self):
        """Tools come from ``get_tool_schemas``; ``ctx.register_tool`` would double them."""
        ctx = RecordingContext()
        optmem.register(ctx)
        assert ctx.tools == []
        assert ctx.middleware == []

    def test_register_does_not_register_hooks_the_host_never_dispatches(self):
        """``on_memory_write``/``on_turn_start`` are provider methods, not plugin hooks."""
        ctx = RecordingContext()
        optmem.register(ctx)
        assert ctx.hooks == []

    def test_register_performs_no_writes_under_hermes_home(self, tmp_path, monkeypatch):
        """Discovery must not create a store, a config, or anything else on disk."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        ctx = RecordingContext()
        optmem.register(ctx)
        after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        assert after == before, "register()/discovery must not write to disk"


# ---------------------------------------------------------------------------
# Naming: registered provider / manifest / entry point
# ---------------------------------------------------------------------------


class TestCanonicalNaming:
    def test_registered_provider_name_is_the_canonical_name(self):
        assert OptMemProvider().name == CANONICAL_NAME

    def test_manifest_declares_the_canonical_name(self):
        assert _load_manifest()["name"] == CANONICAL_NAME

    def test_pyproject_entry_point_uses_the_canonical_key(self):
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["entry-points"]["hermes_agent.memory_providers"]
        assert declared == {CANONICAL_NAME: "optmem"}

    def test_no_declared_name_reuses_the_upstream_bare_key(self):
        manifest = _load_manifest()
        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        entry_points = data["project"]["entry-points"]["hermes_agent.memory_providers"]
        assert manifest["name"] != UPSTREAM_BARE_NAME
        assert UPSTREAM_BARE_NAME not in entry_points
        assert OptMemProvider().name != UPSTREAM_BARE_NAME


# ---------------------------------------------------------------------------
# Declared capabilities must describe the provider's real surface
# ---------------------------------------------------------------------------


class TestDeclaredCapabilities:
    def test_declared_tools_match_the_provider_tool_schemas(self):
        manifest = _load_manifest()
        declared = set(manifest.get("provides_tools") or manifest.get("tools") or [])
        actual = {schema["name"] for schema in OptMemProvider().get_tool_schemas()}
        assert declared == EXPECTED_TOOLS
        assert declared == actual

    def test_declared_hooks_are_real_provider_methods(self):
        manifest = _load_manifest()
        declared = set(manifest.get("provides_hooks") or manifest.get("hooks") or [])
        assert declared == EXPECTED_HOOKS
        provider = OptMemProvider()
        for hook in declared:
            assert callable(getattr(provider, hook, None)), f"{hook} is not a provider method"

    def test_manifest_does_not_carry_a_duplicate_legacy_hooks_key(self):
        """One declaration only (the catalog review diffs it against register())."""
        manifest = _load_manifest()
        assert "hooks" not in manifest or "provides_hooks" not in manifest


# ---------------------------------------------------------------------------
# The real admission gate: `hermes plugins validate`
# ---------------------------------------------------------------------------


class TestHostValidate:
    def test_validate_plugin_dir_passes_the_capability_probe(self):
        host_validate = pytest.importorskip("hermes_cli.plugin_validate")
        report = host_validate.validate_plugin_dir(PLUGIN_PACKAGE)
        assert report.ok, report.failures
        probe = next(check for check in report.checks if check[0] == "capability probe")
        assert probe[1] is True
        assert "register()" in probe[2]

    def test_validate_records_no_undeclared_capabilities(self):
        """A memory provider declares its surface; nothing extra may be registered."""
        host_validate = pytest.importorskip("hermes_cli.plugin_validate")
        report = host_validate.validate_plugin_dir(PLUGIN_PACKAGE)
        undeclared = [name for name, ok, _detail in report.checks
                      if name.startswith("declared ") and not ok]
        assert undeclared == []
