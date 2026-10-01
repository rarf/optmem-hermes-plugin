"""Upstream OptMem contract that 0.3.0 diverged from.

Official ``memo`` does not compact in the background, refuses a wake whose
required summary is missing, and rejects a block id that is not an aligned
power-of-two range. These tests lock that contract.
"""

from __future__ import annotations

import pytest

from optmem import OptMemProvider
from optmem.engine import OptMemEngine, WakeNeedsCompression, validate_block


def _provider(tmp_path, **config):
    mem_dir = tmp_path / "optmem_memory"
    provider = OptMemProvider(config={"memory_dir": str(mem_dir), **config})
    provider.initialize("test-session", hermes_home=str(tmp_path))
    return provider


def test_auto_nap_defaults_off(tmp_path):
    provider = _provider(tmp_path)
    assert provider._config.auto_nap is False


def test_turn_start_does_not_compact_unless_auto_nap_is_on(tmp_path):
    provider = _provider(tmp_path)
    provider.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento Q3"})
    provider.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
    provider.on_turn_start(10, "trigger")
    assert (0, 2) in provider._engine.pending_naps()


def test_llm_summary_without_auto_nap_does_not_compact(tmp_path):
    provider = _provider(tmp_path, llm_summary=True)
    provider.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento Q3"})
    provider.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
    provider.on_turn_start(10, "trigger")
    assert (0, 2) in provider._engine.pending_naps()
    assert provider._engine._tree_get(0, 2) is None


def test_validate_block_rejects_a_misaligned_display_range():
    # Display ids are inclusive; the engine takes an exclusive hi.
    # 4-5 is the aligned block [4, 6). 5-6 is not a block, and without this
    # check it would read the same tree record as 4-5.
    assert validate_block(0, 2) is None
    assert validate_block(4, 6) is None
    assert validate_block(5, 7) is not None


def test_wake_refuses_a_missing_required_summary(tmp_path):
    engine = OptMemEngine(str(tmp_path))
    for text in ("alpha duravel", "beta duravel", "gamma duravel", "delta duravel"):
        engine.append(text)
    result = engine.wake(budget=1)
    assert result["complete"] is False
    assert result["missing"]
    with pytest.raises(WakeNeedsCompression):
        engine.wake_lines(budget=1)
