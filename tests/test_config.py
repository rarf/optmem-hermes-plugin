"""OptMem configuration resolution contract.

The declared schema (``<HERMES_HOME>/optmem/config.json``) is the supported
config surface Hermes' dashboard writes. Legacy 0.2.0 keys
(``plugins.optmem``) stay honoured. A malformed or unknown value must never
crash and must never silently select a *different* mode than the one stored —
it falls back to the safe default and records a diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from optmem.config import (
    MODE_HYBRID,
    MODE_OPTMEM_ONLY,
    OptMemConfig,
    declared_config_path,
    native_memory_paths,
    resolve_config,
    write_declared_config,
)


def _write_declared(home: Path, payload) -> Path:
    path = declared_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_defaults_are_hybrid_and_do_not_touch_native(self, tmp_path):
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_HYBRID
        assert cfg.optmem_only is False
        assert cfg.memory_dir == str(tmp_path / "optmem_memory")
        assert cfg.wake_budget == 96
        assert cfg.recall_mode == "auto"
        assert cfg.llm_summary is False
        assert cfg.source == "defaults"
        assert cfg.diagnostics == ()

    def test_native_paths_are_the_canonical_two_files(self, tmp_path):
        mem, user = native_memory_paths(tmp_path)
        assert mem == tmp_path / "memories" / "MEMORY.md"
        assert user == tmp_path / "memories" / "USER.md"
        cfg = resolve_config(tmp_path)
        assert cfg.native_memory_file == str(mem)
        assert cfg.native_user_file == str(user)

    def test_other_markdown_in_memories_is_not_a_native_memory(self, tmp_path):
        """Only MEMORY.md/USER.md are the native store (host _path_for)."""
        memories = tmp_path / "memories"
        memories.mkdir()
        (memories / "about-user.md").write_text("stale", encoding="utf-8")
        cfg = resolve_config(tmp_path)
        assert cfg.native_memory_file == str(memories / "MEMORY.md")
        assert "about-user.md" not in cfg.native_memory_file

    def test_hermes_home_expansion_in_memory_dir(self, tmp_path):
        _write_declared(tmp_path, {"memory_dir": "$HERMES_HOME/custom"})
        assert resolve_config(tmp_path).memory_dir == str(tmp_path / "custom")
        _write_declared(tmp_path, {"memory_dir": "${HERMES_HOME}/braced"})
        assert resolve_config(tmp_path).memory_dir == str(tmp_path / "braced")


class TestDeclaredConfig:
    def test_declared_config_overrides_defaults(self, tmp_path):
        _write_declared(
            tmp_path,
            {
                "mode": "optmem-only",
                "wake_budget": 48,
                "recall_mode": "bm25",
                "llm_summary": True,
                "migration_split_long": True,
            },
        )
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_OPTMEM_ONLY
        assert cfg.optmem_only is True
        assert cfg.wake_budget == 48
        assert cfg.recall_mode == "bm25"
        assert cfg.llm_summary is True
        assert cfg.migration_split_long is True
        assert cfg.source == "declared"

    def test_mode_spelling_is_normalized(self, tmp_path):
        for raw in ("OptMem-Only", "optmem_only", "OPTMEM-ONLY", " optmem-only "):
            _write_declared(tmp_path, {"mode": raw})
            assert resolve_config(tmp_path).mode == MODE_OPTMEM_ONLY, raw

    def test_unknown_mode_falls_back_to_hybrid_with_diagnostic(self, tmp_path):
        """Never silently select a different identity: unknown -> safe default + reason."""
        _write_declared(tmp_path, {"mode": "exclusive"})
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_HYBRID
        assert any("mode" in d for d in cfg.diagnostics)

    def test_unknown_recall_mode_falls_back_with_diagnostic(self, tmp_path):
        _write_declared(tmp_path, {"recall_mode": "semantic"})
        cfg = resolve_config(tmp_path)
        assert cfg.recall_mode == "auto"
        assert any("recall_mode" in d for d in cfg.diagnostics)

    def test_bad_wake_budget_falls_back_with_diagnostic(self, tmp_path):
        for bad in ("many", -3, 0, 99999):
            _write_declared(tmp_path, {"wake_budget": bad})
            cfg = resolve_config(tmp_path)
            assert cfg.wake_budget == 96, bad
            assert any("wake_budget" in d for d in cfg.diagnostics), bad

    def test_json_null_means_unset_not_a_trust_problem(self, tmp_path):
        """The writers pop cleared keys; an explicit JSON null is simply "unset"."""
        _write_declared(tmp_path, {"mode": "optmem-only", "wake_budget": None})
        cfg = resolve_config(tmp_path)
        assert cfg.wake_budget == 96
        assert cfg.mode == MODE_OPTMEM_ONLY
        assert cfg.diagnostics == ()

    def test_malformed_json_does_not_crash_and_is_reported(self, tmp_path):
        _write_declared(tmp_path, "{not json")
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_HYBRID
        assert cfg.source == "defaults"
        assert any("config.json" in d for d in cfg.diagnostics)

    def test_non_object_json_is_reported(self, tmp_path):
        _write_declared(tmp_path, [1, 2, 3])
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_HYBRID
        assert cfg.diagnostics

    def test_bool_from_json_and_string_both_accepted(self, tmp_path):
        _write_declared(tmp_path, {"llm_summary": "true"})
        assert resolve_config(tmp_path).llm_summary is True
        _write_declared(tmp_path, {"llm_summary": False})
        assert resolve_config(tmp_path).llm_summary is False


class TestLegacyConfig:
    def test_legacy_plugins_section_still_honoured(self, tmp_path):
        cfg = resolve_config(tmp_path, plugin_config={"memory_dir": "/legacy/dir"})
        assert cfg.memory_dir == str(Path("/legacy/dir"))
        assert cfg.source == "legacy"

    def test_declared_wins_over_legacy(self, tmp_path):
        _write_declared(tmp_path, {"memory_dir": "/declared"})
        cfg = resolve_config(tmp_path, plugin_config={"memory_dir": "/legacy"})
        assert cfg.memory_dir == str(Path("/declared"))

    def test_legacy_llm_summary_and_mode(self, tmp_path):
        cfg = resolve_config(tmp_path, plugin_config={"llm_summary": True, "mode": "optmem-only"})
        assert cfg.llm_summary is True
        assert cfg.mode == MODE_OPTMEM_ONLY


class TestWriteDeclaredConfig:
    def test_write_creates_config_and_preserves_unrelated_keys(self, tmp_path):
        _write_declared(tmp_path, {"future_key": {"nested": 1}, "mode": "hybrid"})
        write_declared_config(tmp_path, {"mode": "optmem-only"})
        data = json.loads(declared_config_path(tmp_path).read_text(encoding="utf-8"))
        assert data["mode"] == "optmem-only"
        assert data["future_key"] == {"nested": 1}, "unrelated keys must survive a mode switch"

    def test_write_coerces_and_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(ValueError):
            write_declared_config(tmp_path, {"mode": "exclusive"})
        # Nothing was written by the refused call.
        assert not declared_config_path(tmp_path).exists()

    def test_write_is_atomic_and_never_touches_config_yaml(self, tmp_path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("memory:\n  provider: optmem\n", encoding="utf-8")
        before = yaml_path.read_bytes()
        write_declared_config(tmp_path, {"wake_budget": 32})
        assert yaml_path.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp")), "temp file left behind"
        assert json.loads(declared_config_path(tmp_path).read_text())["wake_budget"] == 32

    def test_write_preserves_existing_file_when_value_invalid(self, tmp_path):
        _write_declared(tmp_path, {"mode": "optmem-only", "wake_budget": 48})
        with pytest.raises(ValueError):
            write_declared_config(tmp_path, {"wake_budget": "lots"})
        data = json.loads(declared_config_path(tmp_path).read_text())
        assert data == {"mode": "optmem-only", "wake_budget": 48}

    def test_roundtrip_resolve_after_write(self, tmp_path):
        write_declared_config(tmp_path, {"mode": "optmem-only", "wake_budget": 12})
        cfg = resolve_config(tmp_path)
        assert cfg.mode == MODE_OPTMEM_ONLY
        assert cfg.wake_budget == 12
        assert cfg.diagnostics == ()


class TestConfigSurface:
    def test_config_is_frozen_and_hashable_snapshot(self):
        cfg = OptMemConfig()
        assert cfg.optmem_only is False
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            cfg.mode = "optmem-only"  # type: ignore[misc]
