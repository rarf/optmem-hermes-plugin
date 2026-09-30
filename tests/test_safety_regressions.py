"""Safety regressions at the configuration and migration boundaries."""

import json

import pytest

from optmem import OptMemProvider, migrate
from optmem.config import write_declared_config
from optmem.engine import OptMemEngine


def ready_home(home):
    (home / "config.yaml").write_text(
        "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n"
    )
    folder = home / "memories"
    folder.mkdir()
    (folder / "MEMORY.md").write_text("A durable preference.")
    engine = OptMemEngine(str(home / "optmem_memory"))
    engine.init_store()
    engine.append("A durable preference.")
    return engine, migrate.backup_native_files(home)


@pytest.mark.parametrize("damage", ["empty", "manifest", "stale", "tampered"])
def test_readiness_requires_complete_fresh_hash_verified_backup(tmp_path, damage):
    engine, backup = ready_home(tmp_path)
    directory = tmp_path / "optmem_backups" / "not-a-backup"
    if damage == "empty":
        directory.mkdir()
        backup = {"dir": str(directory)}
    elif damage == "manifest":
        directory.mkdir()
        (directory / "manifest.json").write_text('{"files": []}')
        backup = {"dir": str(directory)}
    elif damage == "stale":
        source = tmp_path / "memories" / "MEMORY.md"
        source.write_text("Updated durable preference.")
        engine.append("Updated durable preference.")
    else:
        from pathlib import Path

        Path(backup["files"][0]["backup"]).write_text("Corrupted backup.")
    report = migrate.readiness(tmp_path, engine=engine, backup=backup)
    assert report["ready"] is False
    assert report["checks"]["backup"] is False
    before = (tmp_path / "config.yaml").read_bytes()
    assert not migrate.apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)[
        "ok"
    ]
    assert (tmp_path / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("context", ["subagent", "cron", "background_review"])
def test_non_primary_context_cannot_mirror_native_writes(tmp_path, context):
    provider = OptMemProvider()
    provider.initialize("child-session", hermes_home=str(tmp_path), agent_context=context)
    provider._engine.init_store()
    provider.on_memory_write("add", "memory", "A fact produced by a child.")
    assert provider._engine.log_len() == 0


def test_non_primary_context_has_no_memory_tools_or_compaction_writes(tmp_path):
    provider = OptMemProvider()
    provider.initialize("child", hermes_home=str(tmp_path), agent_context="subagent")
    provider._engine.init_store()
    provider._engine.append("2026-01-01 Approved durable decision alpha.")
    provider._engine.append("2026-01-02 Approved durable decision beta.")
    pending = provider._engine.next_nap()
    assert pending
    assert provider.get_tool_schemas() == []
    result = json.loads(provider.handle_tool_call("optmem_note", {"text": "Child wrote this."}))
    assert "primary agent" in result["error"]
    provider.on_turn_start(10, "child turn")
    assert provider._engine.log_len() == 2
    assert provider._engine.next_nap() == pending


def test_lossy_local_summary_keeps_latest_line_even_with_low_keyword_score():
    from optmem import _local_summary

    earlier = "2026-01-01 Budget approved: " + "alpha " * 38
    latest = "2026-02-01 X was replaced by Y; X no longer applies."
    summary = _local_summary([earlier, latest])
    assert latest in summary
    assert len(summary.encode("utf-8")) <= 280


def test_declared_write_refuses_corrupt_existing_config(tmp_path):
    path = tmp_path / "optmem" / "config.json"
    path.parent.mkdir()
    path.write_text('{"unrelated": BROKEN}')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="read|valid|corrupt"):
        write_declared_config(tmp_path, {"mode": "hybrid"})
    assert path.read_bytes() == before


def test_declared_write_does_not_follow_fixed_temp_symlink(tmp_path):
    folder = tmp_path / "optmem"
    folder.mkdir()
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me")
    (folder / "config.json.tmp").symlink_to(victim)
    write_declared_config(tmp_path, {"mode": "hybrid"})
    assert victim.read_text() == "keep me"


def test_failed_declared_mode_write_restores_native_config(tmp_path, monkeypatch):
    engine, backup = ready_home(tmp_path)
    before = (tmp_path / "config.yaml").read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected config write failure")

    monkeypatch.setattr(migrate, "write_declared_config", fail)
    try:
        report = migrate.apply_mode_switch(tmp_path, "optmem-only", engine=engine, backup=backup)
        assert not report["ok"]
    except OSError:
        pass
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_split_migration_readiness_recognizes_all_imported_parts(tmp_path):
    (tmp_path / "config.yaml").write_text("memory:\n  memory_enabled: true\n")
    native = tmp_path / "memories"
    native.mkdir()
    original = "Durable preference alpha. " * 20
    (native / "MEMORY.md").write_text(original)
    engine = OptMemEngine(str(tmp_path / "optmem_memory"))
    engine.init_store()
    plan = migrate.plan_migration(tmp_path, engine=engine, split_long=True)
    assert not plan.blocked
    migrate.apply_migration(engine, plan)
    backup = migrate.backup_native_files(tmp_path)
    report = migrate.readiness(tmp_path, engine=engine, plan=plan, backup=backup)
    assert report["ready"], report["reasons"]


def test_auto_nap_can_be_disabled_by_declared_configuration(tmp_path):
    write_declared_config(tmp_path, {"auto_nap": False})
    provider = OptMemProvider()
    provider.initialize("test-session", hermes_home=str(tmp_path))
    provider._engine.init_store()
    provider._engine.append("2026-01-01 Owner approved durable decision alpha.")
    provider._engine.append("2026-01-02 Owner approved durable decision beta.")
    before = provider._engine.next_nap()
    assert before
    provider.on_turn_start(10, "a user turn")
    assert provider._engine.next_nap() == before


def test_provider_setup_writes_declared_config_without_rewriting_host(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# preserve comments\nmemory:\n  provider: optmem\nother: unchanged\n")
    before = path.read_bytes()
    provider = OptMemProvider()
    provider.save_config({"memory_dir": "$HERMES_HOME/custom"}, str(tmp_path))
    assert path.read_bytes() == before
    assert (
        json.loads((tmp_path / "optmem" / "config.json").read_text())["memory_dir"]
        == "$HERMES_HOME/custom"
    )
