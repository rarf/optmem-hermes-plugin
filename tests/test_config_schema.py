"""The declared config schema Hermes' dashboard renders.

``optmem/config_schema.py`` is loaded BY PATH by the host
(``plugins/memory/config_schema.py:get_provider_config_schema``) and may import
only that pure-data module. It must therefore:

- expose ``CONFIG_SCHEMA`` with the same keys the resolver reads (no drift), and
- be importable without pulling the agent runtime into the web server.

The host-shaped assertions live in ``test_host_integration.py``; this file pins
the declarations and the import purity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from optmem.config import EDITABLE_KEYS, MODES, RECALL_MODES
from optmem.config_schema import CONFIG_SCHEMA

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _field(key: str):
    return next(f for f in CONFIG_SCHEMA.fields if f.key == key)


class TestDeclaredSchema:
    def test_identity(self):
        assert CONFIG_SCHEMA.name == "optmem"
        assert CONFIG_SCHEMA.label == "OptMem"
        assert CONFIG_SCHEMA.storage == "flat_json"
        assert CONFIG_SCHEMA.docs_url

    def test_keys_match_the_resolver_exactly(self):
        declared = {f.key for f in CONFIG_SCHEMA.fields}
        assert declared == set(EDITABLE_KEYS), (
            "the GUI schema and the config resolver must not drift apart"
        )

    def test_field_labels_and_descriptions_present(self):
        for field in CONFIG_SCHEMA.fields:
            assert field.label, field.key
            assert field.description, field.key

    def test_mode_select_offers_both_modes(self):
        field = _field("mode")
        assert field.kind == "select"
        assert {opt.value for opt in field.options} == set(MODES)
        assert field.default == "hybrid", (
            "defaulting to optmem-only would silently disable native memory"
        )

    def test_recall_mode_options_match_the_resolver(self):
        field = _field("recall_mode")
        assert field.kind == "select"
        assert {opt.value for opt in field.options} == set(RECALL_MODES)
        assert field.default == "auto"

    def test_wake_budget_is_a_number(self):
        field = _field("wake_budget")
        assert field.kind == "number"
        assert field.default == "96"

    def test_boolean_fields_have_string_defaults(self):
        for key in ("llm_summary", "migration_split_long", "auto_nap"):
            field = _field(key)
            assert field.kind == "bool", key
            assert field.default in {"true", "false"}, key

    def test_no_secret_fields(self):
        """OptMem is local-only: there is nothing to authenticate."""
        assert not [f for f in CONFIG_SCHEMA.fields if f.kind == "secret"]

    def test_common_choices_are_inline(self):
        inline = {f.key for f in CONFIG_SCHEMA.inline_fields()}
        assert inline == set(EDITABLE_KEYS), "every supported knob fits the compact panel"

    def test_migration_split_defaults_to_safe(self):
        """Splitting must be opt-in: the default blocks rather than reshapes facts."""
        assert _field("migration_split_long").default == "false"

    def test_fields_are_grouped(self):
        for field in CONFIG_SCHEMA.fields:
            assert field.group, field.key


class TestImportPurity:
    """Loading the schema must not drag the agent runtime into the web server.

    The host loads the file by path before any provider is selected, so an
    import chain of ours that reached ``agent.memory_provider`` would run the
    agent runtime inside the dashboard process.
    """

    _PROBE = (
        "import importlib.util, sys, json\n"
        "path = sys.argv[1]\n"
        "spec = importlib.util.spec_from_file_location('_probe_schema', path)\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "print(json.dumps({'keys': sorted(f.key for f in mod.CONFIG_SCHEMA.fields),\n"
        "                  'name': mod.CONFIG_SCHEMA.name,\n"
        "                  'storage': mod.CONFIG_SCHEMA.storage,\n"
        "                  'agent': 'agent.memory_provider' in sys.modules,\n"
        "                  'hermes_cli': 'hermes_cli' in sys.modules}))\n"
    )

    def _run_probe(self, python: str, env: dict) -> dict:
        import json as _json
        import os as _os

        proc = subprocess.run(
            [python, "-c", self._PROBE, str(PROJECT_ROOT / "optmem" / "config_schema.py")],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        assert proc.returncode == 0, f"{python} failed:\n{proc.stderr}"
        return _json.loads(proc.stdout.strip().splitlines()[-1]), _os

    def test_under_the_host_it_never_imports_the_agent_runtime(self):
        import os

        payload, _ = self._run_probe(sys.executable, dict(os.environ))
        assert payload["name"] == "optmem"
        assert payload["storage"] == "flat_json"
        assert set(payload["keys"]) == set(EDITABLE_KEYS)
        assert payload["agent"] is False, (
            "loading config_schema.py must not import agent.memory_provider"
        )

    def test_a_bare_interpreter_without_hermes_still_loads_it(self):
        """The plugin's own CI has no Hermes: the module must degrade, not break."""
        import os
        import shutil

        bare = shutil.which("python3")
        if not bare:
            pytest.skip("no bare python3 available")
        env = {**os.environ, "PYTHONPATH": ""}
        payload, _ = self._run_probe(bare, env)
        assert set(payload["keys"]) == set(EDITABLE_KEYS)
        assert payload["agent"] is False
        assert payload["hermes_cli"] is False


def test_schema_loads_by_path_as_the_host_does(tmp_path):
    """Exactly the host's load sequence: spec_from_file_location on the file."""
    import importlib.util

    path = PROJECT_ROOT / "optmem" / "config_schema.py"
    spec = importlib.util.spec_from_file_location("_hermes_memory_config_schema.optmem", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert getattr(module, "CONFIG_SCHEMA", None) is not None
    assert module.CONFIG_SCHEMA.name == "optmem"
