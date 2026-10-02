"""Migration of the built-in MEMORY.md/USER.md store, and the guarded mode switch.

Hard requirements this file pins down:

- The native format is exactly what ``tools/memory_tool_store.py`` reads:
  ``MEMORY.md``/``USER.md`` under ``<HERMES_HOME>/memories/``, entries joined by
  ``"\\n§\\n"``. Other ``.md`` files in that folder are NOT native memories.
- An entry longer than 280 UTF-8 bytes must be split on safe boundaries or the
  migration must STOP with an actionable error. Never truncate, never silently
  drop, never disable the native store on a plan that lost data.
- The raw native files are copied byte-for-byte before anything changes.
- Re-running the migration must not duplicate already-imported facts.
- Disabling native memory is a separate, explicit, reversible step that only
  runs when the store is verified; unrelated config.yaml content is preserved.
"""

from __future__ import annotations

import json
import os
from datetime import date as _date
from pathlib import Path

import pytest

from optmem.config import resolve_config
from optmem.engine import ENTRY_CHARS, OptMemEngine
from optmem.migrate import (
    DELIMITER,
    MigrationPlan,
    NativeReadError,
    apply_migration,
    apply_mode_switch,
    backup_native_files,
    plan_migration,
    plan_mode_switch,
    read_native_entries,
    readiness,
    rollback_mode,
    split_long_entry,
)

TODAY = _date.today().isoformat()


def _write_native(home: Path, memory: str = "", user: str = "") -> tuple[Path, Path]:
    memories = home / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    mem_path, user_path = memories / "MEMORY.md", memories / "USER.md"
    if memory:
        mem_path.write_text(memory, encoding="utf-8")
    if user:
        user_path.write_text(user, encoding="utf-8")
    return mem_path, user_path


def _engine(home: Path) -> OptMemEngine:
    return OptMemEngine(str(home / "optmem_memory"))


def _cards(*entries: str) -> str:
    return DELIMITER.join(entries)


# --------------------------------------------------------------------------- #
# native parsing
# --------------------------------------------------------------------------- #


class TestNativeParsing:
    def test_parses_host_delimiter_and_strips(self, tmp_path):
        path, _ = _write_native(tmp_path, memory=_cards("  facto um  ", "facto dois") + "\n")
        assert read_native_entries(path) == ["facto um", "facto dois"]

    def test_delimiter_is_the_host_constant(self):
        assert DELIMITER == "\n§\n"

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_native_entries(tmp_path / "nope.md") == []

    def test_empty_file_is_empty(self, tmp_path):
        path, _ = _write_native(tmp_path, memory="")
        (path).write_text("", encoding="utf-8")
        assert read_native_entries(path) == []

    def test_bare_section_sign_survives(self, tmp_path):
        """The host splits on the FULL delimiter, so a lone § is content."""
        path, _ = _write_native(tmp_path, memory="preco § desconto aplicado")
        assert read_native_entries(path) == ["preco § desconto aplicado"]

    def test_unreadable_file_raises_with_the_path(self, tmp_path):
        path, _ = _write_native(tmp_path, memory="x")
        path.chmod(0o000)
        try:
            if os.access(path, os.R_OK):  # running as root: cannot test this
                pytest.skip("file still readable (root)")
            with pytest.raises(NativeReadError) as err:
                read_native_entries(path)
            assert str(path) in str(err.value)
        finally:
            path.chmod(0o600)

    def test_invalid_utf8_raises_rather_than_guessing(self, tmp_path):
        path, _ = _write_native(tmp_path, memory="ok")
        path.write_bytes("facto válido".encode("utf-16"))
        with pytest.raises(NativeReadError):
            read_native_entries(path)

    def test_migration_only_considers_the_two_canonical_files(self, tmp_path):
        _write_native(tmp_path, memory=_cards("canonica"))
        (tmp_path / "memories" / "about-user.md").write_text(
            "RASCUNHO nao migrar", encoding="utf-8"
        )
        (tmp_path / "memories" / "alldrivers-overview.md").write_text(
            "RASCUNHO nao migrar", encoding="utf-8"
        )
        plan = plan_migration(tmp_path, engine=_engine(tmp_path))
        assert [e.text for e in plan.adds] == ["canonica"]
        assert all("RASCUNHO" not in e.text for e in plan.adds)


# --------------------------------------------------------------------------- #
# splitting long entries
# --------------------------------------------------------------------------- #


class TestSplitLongEntry:
    def test_short_entry_is_not_split(self):
        assert split_long_entry("curto", limit=280) == ["curto"]

    def test_splits_on_sentence_boundaries(self):
        long = (
            "Primeira decisao sobre o telhado da casa de Marcia foi aprovada hoje. "
            "Segunda decisao sobre o orcamento da obra do telhado tambem foi aprovada. "
            "Terceira observacao longa sobre prazos de entrega e fornecedores."
        )
        parts = split_long_entry(long, limit=120)
        assert parts is not None
        assert len(parts) >= 3
        assert all(len(p.encode("utf-8")) <= 120 for p in parts)

    def test_splits_on_semicolons_and_pipes(self):
        long = "fato A aprovado; facto B decidido; facto C entregue; facto D iniciado"
        parts = split_long_entry(long, limit=30)
        assert parts is not None and len(parts) > 1
        assert all(len(p.encode("utf-8")) <= 30 for p in parts)

    def test_unsplittable_single_token_returns_none(self):
        assert split_long_entry("x" * 400, limit=280) is None

    def test_split_never_truncates_content(self):
        long = "frase longa numero um; frase longa numero dois; frase longa numero tres"
        parts = split_long_entry(long, limit=40)
        assert parts is not None
        joined = " ".join(parts)
        for token in ("um", "dois", "tres"):
            assert token in joined

    def test_split_of_exact_boundary_entry_is_unchanged(self):
        text = "y" * ENTRY_CHARS
        assert split_long_entry(text) == [text]

    def test_unicode_split_respects_byte_limit(self):
        long = ("ç" * 100) + "; " + ("ã" * 100) + "; " + ("õ" * 100)
        parts = split_long_entry(long, limit=210)
        assert parts is not None
        assert all(len(p.encode("utf-8")) <= 210 for p in parts)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


class TestPlanMigration:
    def test_short_entries_plan_ok(self, tmp_path):
        _write_native(
            tmp_path,
            memory=_cards("facto um duravel", "facto dois duravel"),
            user=_cards("perfil do usuario"),
        )
        plan = plan_migration(tmp_path, engine=_engine(tmp_path))
        assert isinstance(plan, MigrationPlan)
        assert plan.status == "ok"
        assert plan.blocked is False
        assert len(plan.adds) == 3
        assert {e.source for e in plan.adds} == {"USER.md", "MEMORY.md"}
        assert all(e.date == TODAY for e in plan.adds)
        assert plan.as_dict()["adds"] == 3

    def test_empty_native_store_plans_empty(self, tmp_path):
        _write_native(tmp_path, memory="")
        plan = plan_migration(tmp_path, engine=_engine(tmp_path))
        assert plan.status == "empty"
        assert plan.adds == ()
        assert plan.blocked is False

    def test_long_entry_blocks_with_actionable_reason(self, tmp_path):
        long = "decisao longa " * 40  # > 280 bytes
        _write_native(tmp_path, memory=_cards("curto", long.strip()))
        plan = plan_migration(tmp_path, engine=_engine(tmp_path), split_long=False)
        assert plan.status == "blocked"
        assert plan.blocked is True
        assert len(plan.unresolved) == 1
        text, nbytes = plan.unresolved[0]
        assert nbytes > ENTRY_CHARS
        assert "split" in " ".join(plan.reasons).lower()
        assert len(plan.reasons) >= 1

    def test_long_entry_splits_when_enabled(self, tmp_path):
        long = "; ".join(f"decisao numero {i} sobre o telhado aprovada" for i in range(12))
        assert len(long.encode()) > ENTRY_CHARS
        _write_native(tmp_path, memory=_cards(long))
        plan = plan_migration(tmp_path, engine=_engine(tmp_path), split_long=True)
        assert plan.status == "ok"
        assert len(plan.adds) >= 3
        assert all(len(e.text.encode("utf-8")) <= ENTRY_CHARS for e in plan.adds)
        assert all(e.source == "MEMORY.md" for e in plan.adds)
        assert plan.split_count == 1

    def test_long_entry_splits_via_declared_config(self, tmp_path):
        from optmem.config import write_declared_config

        write_declared_config(tmp_path, {"migration_split_long": True})
        long = "; ".join(f"decisao numero {i} sobre o telhado aprovada" for i in range(12))
        _write_native(tmp_path, memory=_cards(long))
        plan = plan_migration(tmp_path, engine=_engine(tmp_path))
        assert plan.status == "ok" and plan.split_count == 1

    def test_unsplittable_long_entry_blocks_even_with_split_enabled(self, tmp_path):
        _write_native(tmp_path, memory=_cards("z" * 400))
        plan = plan_migration(tmp_path, engine=_engine(tmp_path), split_long=True)
        assert plan.status == "blocked"
        assert len(plan.unresolved) == 1

    def test_duplicate_of_existing_store_entry_is_skipped(self, tmp_path):
        engine = _engine(tmp_path)
        engine.append("facto que ja existe na store")
        _write_native(tmp_path, memory=_cards("facto que ja existe na store", "facto novo"))
        plan = plan_migration(tmp_path, engine=engine)
        assert [e.text for e in plan.adds] == ["facto novo"]
        assert plan.skipped == ("facto que ja existe na store",)

    def test_duplicate_dedupe_is_accent_insensitive(self, tmp_path):
        engine = _engine(tmp_path)
        engine.append("a caçula nasceu em marco")
        _write_native(tmp_path, memory=_cards("a cacula nasceu em marco"))
        plan = plan_migration(tmp_path, engine=engine)
        assert plan.adds == ()

    def test_duplicate_across_both_files_counted_once(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto repetido"), user=_cards("facto repetido"))
        plan = plan_migration(tmp_path, engine=_engine(tmp_path))
        assert len(plan.adds) == 1
        assert plan.skipped == ("facto repetido",)

    def test_rerun_after_apply_adds_nothing(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto A duravel", "facto B duravel"))
        engine = _engine(tmp_path)
        plan = plan_migration(tmp_path, engine=engine)
        apply_migration(engine, plan)
        again = plan_migration(tmp_path, engine=engine)
        assert again.adds == (), "re-running the migration must not duplicate facts"
        assert again.status == "empty"
        assert len(again.skipped) == 2


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #


class TestBackup:
    def test_backup_copies_raw_bytes_verbatim_with_hashes(self, tmp_path):
        mem = _cards("facto com § dentro", "outro facto")
        mem_path, _ = _write_native(tmp_path, memory=mem, user=_cards("perfil"))
        raw_before = mem_path.read_bytes()
        from optmem.config import native_memory_paths

        mem_p, user_p = native_memory_paths(tmp_path)
        report = backup_native_files(tmp_path, paths=(mem_p, user_p))
        backup_dir = Path(report["dir"])
        assert backup_dir.is_dir()
        copied = backup_dir / "MEMORY.md"
        assert copied.read_bytes() == raw_before
        entry = next(f for f in report["files"] if f["name"] == "MEMORY.md")
        import hashlib

        assert entry["sha256"] == hashlib.sha256(raw_before).hexdigest()
        assert entry["present"] is True and entry["bytes"] == len(raw_before)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["files"][0]["sha256"] == entry["sha256"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not describe Windows ACLs")
    def test_backup_dir_is_restrictive(self, tmp_path):
        mem_p, user_p = _write_native(tmp_path, memory=_cards("x"))
        report = backup_native_files(tmp_path, paths=(mem_p, user_p))
        mode = os.stat(report["dir"]).st_mode & 0o777
        assert mode & 0o077 == 0, f"backup dir too permissive: {oct(mode)}"

    def test_missing_native_file_recorded_as_absent(self, tmp_path):
        memories = tmp_path / "memories"
        memories.mkdir(parents=True, exist_ok=True)
        present = memories / "USER.md"
        present.write_text(_cards("perfil"), encoding="utf-8")
        absent = memories / "MEMORY.md"
        assert not absent.exists()
        report = backup_native_files(tmp_path, paths=(absent, present))
        missing = next(f for f in report["files"] if f["name"] == "MEMORY.md")
        assert missing["present"] is False and missing["sha256"] is None
        assert not (Path(report["dir"]) / "MEMORY.md").exists()

    def test_two_backups_do_not_overwrite_each_other(self, tmp_path):
        mem_p, user_p = _write_native(tmp_path, memory=_cards("x"))
        first = backup_native_files(tmp_path, paths=(mem_p, user_p))
        second = backup_native_files(tmp_path, paths=(mem_p, user_p))
        assert first["dir"] != second["dir"]
        assert Path(first["dir"]).is_dir() and Path(second["dir"]).is_dir()


# --------------------------------------------------------------------------- #
# apply + readiness
# --------------------------------------------------------------------------- #


class TestApplyAndReadiness:
    def test_apply_writes_every_planned_entry_and_reads_back(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto A duravel"), user=_cards("facto B duravel"))
        engine = _engine(tmp_path)
        plan = plan_migration(tmp_path, engine=engine)
        result = apply_migration(engine, plan)
        assert result["added"] == 2
        assert engine.log_len() == 2
        hits = engine.recall("facto A duravel", mode="regex")
        assert hits and "facto A" in hits[0][3]
        assert result["ids"] == [0, 1]
        assert result["date"] == TODAY

    def test_apply_is_idempotent(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto unico"))
        engine = _engine(tmp_path)
        plan = plan_migration(tmp_path, engine=engine)
        apply_migration(engine, plan)
        apply_migration(engine, plan_migration(tmp_path, engine=engine))
        assert engine.log_len() == 1

    def test_apply_refuses_a_blocked_plan(self, tmp_path):
        _write_native(tmp_path, memory=_cards("z" * 400))
        engine = _engine(tmp_path)
        plan = plan_migration(tmp_path, engine=engine)
        with pytest.raises(ValueError) as err:
            apply_migration(engine, plan)
        assert "split" in str(err.value).lower()
        assert engine.log_len() == 0

    def test_readiness_is_not_ready_before_import(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto por migrar"))
        engine = _engine(tmp_path)
        report = readiness(tmp_path, engine=engine)
        assert report["ready"] is False
        assert report["checks"]["imported"] is False
        assert report["reasons"]

    def test_readiness_ready_after_verified_import(self, tmp_path):
        mem_p, user_p = _write_native(
            tmp_path, memory=_cards("facto migrado"), user=_cards("perfil")
        )
        engine = _engine(tmp_path)
        backup = backup_native_files(tmp_path, paths=(mem_p, user_p))
        plan = plan_migration(tmp_path, engine=engine)
        apply_migration(engine, plan)
        report = readiness(tmp_path, engine=engine, backup=backup)
        assert report["ready"] is True
        assert report["checks"]["native_absent"] == () or report["checks"]["native_absent"] == []
        assert report["checks"]["backup"] is True
        assert report["store_entries"] == 2  # MEMORY.md entry + USER.md entry

    def test_readiness_blocks_on_unresolved_long_entries(self, tmp_path):
        _write_native(tmp_path, memory=_cards("z" * 400))
        engine = _engine(tmp_path)
        plan = plan_migration(tmp_path, engine=engine)
        report = readiness(tmp_path, engine=engine, plan=plan)
        assert report["ready"] is False
        assert any("long" in r.lower() or "split" in r.lower() for r in report["reasons"])

    def test_readiness_requires_a_backup(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto"))
        engine = _engine(tmp_path)
        apply_migration(engine, plan_migration(tmp_path, engine=engine))
        report = readiness(tmp_path, engine=engine, backup=None)
        assert report["ready"] is False
        assert any("backup" in r.lower() for r in report["reasons"])


# --------------------------------------------------------------------------- #
# mode switch
# --------------------------------------------------------------------------- #


def _config_yaml(home: Path, **values) -> Path:
    lines = [
        "# my precious config",
        "agent:",
        "  max_turns: 100",
        "memory:",
        "  provider: optmem",
    ]
    for key, value in values.items():
        lines.append(f"  {key}: {value}")
    lines += ["toolsets:", "  - hermes-cli", ""]
    path = home / "config.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestModeSwitch:
    def _ready(self, tmp_path):
        mem_p, user_p = _write_native(
            tmp_path, memory=_cards("facto migrado"), user=_cards("perfil")
        )
        engine = _engine(tmp_path)
        backup = backup_native_files(tmp_path, paths=(mem_p, user_p))
        apply_migration(engine, plan_migration(tmp_path, engine=engine))
        return engine, backup

    def test_plan_refuses_without_verified_import(self, tmp_path):
        _write_native(tmp_path, memory=_cards("facto nao migrado"))
        engine = _engine(tmp_path)
        report = plan_mode_switch(tmp_path, "optmem-only", engine=engine, backup=None)
        assert report["ok"] is False
        assert report["reasons"]

    def test_plan_refuses_a_blocked_migration(self, tmp_path):
        _write_native(tmp_path, memory=_cards("z" * 400))
        engine = _engine(tmp_path)
        report = plan_mode_switch(tmp_path, "optmem-only", engine=engine)
        assert report["ok"] is False

    def test_switch_to_optmem_only_disables_native_and_preserves_config(self, tmp_path):
        _config_yaml(tmp_path, memory_enabled=True, user_profile_enabled=True, nudge_interval=10)
        engine, backup = self._ready(tmp_path)
        report = apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        assert report["ok"] is True
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "memory_enabled: false" in text
        assert "user_profile_enabled: false" in text
        assert "nudge_interval: 10" in text, "unrelated memory keys must survive"
        assert "# my precious config" in text, "comments must survive"
        assert "max_turns: 100" in text
        assert "provider: optmem-hermes" in text
        assert resolve_config(tmp_path).mode == "optmem-only"
        # Native files are never deleted.
        assert (tmp_path / "memories" / "MEMORY.md").exists()

    def test_switch_adds_missing_keys_without_duplicating(self, tmp_path):
        _config_yaml(tmp_path)  # memory block has provider only
        engine, backup = self._ready(tmp_path)
        apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert text.count("memory_enabled:") == 1
        assert text.count("user_profile_enabled:") == 1
        assert text.index("memory:") < text.index("memory_enabled:")
        assert "toolsets:" in text

    def test_switch_without_a_memory_block_creates_one(self, tmp_path):
        (tmp_path / "config.yaml").write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
        engine, backup = self._ready(tmp_path)
        apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "memory:" in text
        assert "memory_enabled: false" in text
        assert "max_turns: 5" in text

    def test_switch_is_refused_when_config_yaml_uses_flow_style(self, tmp_path):
        (tmp_path / "config.yaml").write_text("memory: {provider: optmem}\n", encoding="utf-8")
        engine, backup = self._ready(tmp_path)
        report = apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        assert report["ok"] is False
        assert (tmp_path / "config.yaml").read_text(
            encoding="utf-8"
        ) == "memory: {provider: optmem}\n"

    def test_switch_back_to_hybrid_restores_native_and_keeps_store(self, tmp_path):
        _config_yaml(tmp_path, memory_enabled=True, user_profile_enabled=True)
        engine, backup = self._ready(tmp_path)
        apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        report = apply_mode_switch(tmp_path, "hybrid", engine=engine, backup=backup)
        assert report["ok"] is True
        text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        assert "memory_enabled: true" in text
        assert "user_profile_enabled: true" in text
        assert resolve_config(tmp_path).mode == "hybrid"
        assert engine.log_len() >= 1, "turning OptMem-only off must not delete OptMem data"

    def test_rollback_restores_prior_flags_and_keeps_store(self, tmp_path):
        _config_yaml(tmp_path, memory_enabled=True, user_profile_enabled=True)
        engine, backup = self._ready(tmp_path)
        before = (tmp_path / "config.yaml").read_text(encoding="utf-8")
        apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        report = rollback_mode(tmp_path, engine=engine)
        assert report["ok"] is True
        assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before
        assert resolve_config(tmp_path).mode == "hybrid"
        assert engine.log_len() >= 1

    def test_switch_never_writes_the_declared_config_on_a_refused_switch(self, tmp_path):
        _config_yaml(tmp_path)
        engine = _engine(tmp_path)
        apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=None)
        from optmem.config import declared_config_path

        assert not declared_config_path(tmp_path).exists()

    def test_switch_refuses_unknown_mode(self, tmp_path):
        _config_yaml(tmp_path)
        engine, backup = self._ready(tmp_path)
        report = apply_mode_switch(tmp_path, "exclusive", engine=engine, backup=backup)
        assert report["ok"] is False
        assert any("mode" in r.lower() for r in report["reasons"])

    def test_config_yaml_is_backed_up_before_a_switch(self, tmp_path):
        _config_yaml(tmp_path)
        engine, backup = self._ready(tmp_path)
        report = apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        assert report["config_backup"], "the pre-switch config.yaml must be preserved"
        assert Path(report["config_backup"]).read_text(encoding="utf-8") == (
            "# my precious config\nagent:\n  max_turns: 100\nmemory:\n  provider: optmem\n"
            "toolsets:\n  - hermes-cli\n"
        )
