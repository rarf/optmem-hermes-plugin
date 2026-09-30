"""Multi-level temporal-chain regression for the real OptMem store.

Scenario (synthetic, generic personas): an original proposal, owner refinements,
and an explicit dated supersession. The store is append-only and its decay tree
compresses memories pairwise into summaries, and summaries of summaries —
**structural** chaining. It deliberately does NOT resolve conflicting facts:
``semantic_conflict_resolution`` is False, a superseded record stays retrievable,
and only the summary text the caller (agent) writes can present a "latest
decision". These tests pin that boundary and prove the retrieval inputs the
model would see, without exercising any LLM question-answering.

Everything runs against a temp directory; no network, no live profile writes.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optmem import OptMemProvider  # noqa: E402
from optmem.config import write_declared_config  # noqa: E402
from optmem.engine import LOG_REC, RAW_MAX, WAKE_LINES, OptMemEngine  # noqa: E402

# --- synthetic timeline ----------------------------------------------------

TOTAL = 32  # enough for a size-32 block: five decay-tree levels above raw

PROPOSAL = "Proposal: adopt plan alpha for the release (owner proposed)."
FROZEN = "Refinement 30: alpha scope frozen to the billing module by the owner."
SUPERSESSION = "Supersession 2026-03-01: owner replaced plan alpha with plan beta."

# Summary texts the caller (the agent performing the nap) writes explicitly.
SUMMARY_PROPOSAL = "Proposal plan alpha with early refinements reviewed."
SUMMARY_REFINE = "Refinements continued; owner replaced plan alpha with plan beta (2026-03-01)."
SUMMARY_TOP = "Latest decision: plan beta supersedes plan alpha (2026-03-01)."
SUMMARY_SUPERSEDE = "Owner superseded plan alpha with plan beta (2026-03-01); billing scope."


def _record_texts() -> list[str]:
    texts = [PROPOSAL]
    texts += [f"Refinement {i}: alpha scope item {i} reviewed by the team." for i in range(1, 30)]
    texts.append(FROZEN)
    texts.append(SUPERSESSION)
    assert len(texts) == TOTAL
    return texts


def _date_for(index: int) -> str:
    return (datetime.date(2026, 1, 1) + datetime.timedelta(days=index)).isoformat()


def _summary_for(lo: int, hi: int) -> str:
    """The one-line compression the agent would write for block [lo, hi)."""
    return {
        (0, 16): SUMMARY_PROPOSAL,
        (16, 32): SUMMARY_REFINE,
        (0, 32): SUMMARY_TOP,
        (28, 32): SUMMARY_SUPERSEDE,
    }.get((lo, hi), f"compressed block {lo}-{hi - 1}")


def _build_engine(directory: Path) -> OptMemEngine:
    engine = OptMemEngine(str(directory))
    for index, text in enumerate(_record_texts()):
        engine.append(text, date=_date_for(index))
    return engine


def _drain(engine: OptMemEngine) -> list[tuple[int, int]]:
    """Apply every pending nap bottom-up, as the agent would, and return blocks."""
    applied: list[tuple[int, int]] = []
    while (nxt := engine.next_nap()) is not None:
        (lo, hi), _prompt = nxt
        assert engine.apply_nap(lo, hi, _summary_for(lo, hi)) is True
        applied.append((lo, hi))
    return applied


def _wake_lines_from_context(context: str) -> list[str]:
    """Wake node lines are ``#<id>`` / ``#<lo>-<hi>``; the section header is ``##``."""
    return [line for line in context.splitlines() if re.match(r"^#\d", line)]


def _provider(home: Path, **declared) -> OptMemProvider:
    write_declared_config(home, {"memory_dir": str(home / "optmem_memory"), **declared})
    provider = OptMemProvider()
    provider.initialize("chain-session", hermes_home=str(home))
    return provider


# ---------------------------------------------------------------------------
# Append-only integrity
# ---------------------------------------------------------------------------


class TestLogIntegrity:
    def test_log_is_fixed_width_and_never_rewritten_by_naps(self, tmp_path):
        engine = _build_engine(tmp_path)
        log = tmp_path / "LOG.txt"
        before = log.read_bytes()
        assert len(before) == TOTAL * LOG_REC
        assert engine.log_len() == TOTAL

        applied = _drain(engine)

        assert len(applied) == 31, "2/4/8/16/32 levels: 16+8+4+2+1 blocks"
        assert log.read_bytes() == before, "compression must never touch LOG.txt"
        assert log.stat().st_size == TOTAL * LOG_REC
        assert engine.log_len() == TOTAL


# ---------------------------------------------------------------------------
# Structural chaining: the decay tree
# ---------------------------------------------------------------------------


class TestDecayTree:
    def test_every_tree_level_is_built_by_naps(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        sizes = {int(p.name) for p in (tmp_path / "TREE").iterdir() if p.name.isdigit()}
        assert {2, 4, 8, 16, 32} <= sizes
        assert engine.pending_naps() == []
        assert engine._tree_get(0, 32) == SUMMARY_TOP

    def test_top_summary_is_built_from_child_summaries(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        # A size-32 block exceeds RAW_MAX, so its prompt lists the two size-16
        # summaries as inputs — this is the summaries-of-summaries level.
        assert RAW_MAX < 32
        prompt = engine.nap_prompt(0, 32)
        assert engine._tree_get(0, 16) == SUMMARY_PROPOSAL
        assert engine._tree_get(16, 32) == SUMMARY_REFINE
        assert SUMMARY_PROPOSAL in prompt
        assert SUMMARY_REFINE in prompt
        assert "#0-15" in prompt and "#16-31" in prompt


# ---------------------------------------------------------------------------
# Wake budgets
# ---------------------------------------------------------------------------


class TestWakeBudgets:
    def test_default_budget_prints_every_original_verbatim(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        lines = engine.wake_lines(budget=WAKE_LINES)
        assert len(lines) == TOTAL
        assert any(PROPOSAL in line for line in lines)
        assert any(SUPERSESSION in line for line in lines)

    @pytest.mark.parametrize("budget", [1, 4, 8])
    def test_forced_budget_prints_exactly_budget_blocks(self, tmp_path, budget):
        engine = _build_engine(tmp_path)
        _drain(engine)
        assert len(engine.wake_lines(budget=budget)) == budget

    def test_budget_one_surfaces_the_top_level_summary(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        assert engine.wake_lines(budget=1) == [f"#0-31 {SUMMARY_TOP}"]

    def test_budget_four_surfaces_the_supersession_summary(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        lines = engine.wake_lines(budget=4)
        # Coarse blocks: the size-16 proposal summary and the size-4 block that
        # carries the caller-written supersession summary.
        assert any(SUMMARY_PROPOSAL in line for line in lines)
        assert any(SUMMARY_SUPERSEDE in line for line in lines)
        assert any(line.startswith("#0-15") for line in lines)
        assert any(line.startswith("#28-31") for line in lines)

    def test_budget_eight_mixes_summaries_with_verbatim_recent(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        lines = engine.wake_lines(budget=8)
        assert any(SUMMARY_PROPOSAL in line for line in lines)
        # Recent singletons stay verbatim, including the supersession record.
        assert any(SUPERSESSION in line for line in lines)


# ---------------------------------------------------------------------------
# Reopen, zoom, historical recall
# ---------------------------------------------------------------------------


class TestReopenZoomRecall:
    def test_reopening_the_store_replays_the_same_context(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        expected = engine.wake_lines(budget=4)
        reopened = OptMemEngine(str(tmp_path))
        assert reopened.wake_lines(budget=4) == expected
        assert reopened._tree_get(0, 32) == SUMMARY_TOP

    def test_zoom_returns_raw_records_below_the_raw_max(self, tmp_path):
        provider = _provider(tmp_path / "home")
        for text in _record_texts():
            provider.handle_tool_call("optmem_note", {"text": text})
        payload = json.loads(provider.handle_tool_call("optmem_zoom", {"lo": 0, "hi": 2}))
        assert payload["block"] == "0-1"
        assert payload["halves"][0].endswith(PROPOSAL)
        assert "Refinement 1" in payload["halves"][1]

    def test_zoom_returns_child_summaries_above_the_raw_max(self, tmp_path):
        provider = _provider(tmp_path / "home")
        for index, text in enumerate(_record_texts()):
            provider._engine.append(text, date=_date_for(index))
        _drain(provider._engine)
        payload = json.loads(provider.handle_tool_call("optmem_zoom", {"lo": 0, "hi": 32}))
        assert payload["halves"] == [f"#0-15 {SUMMARY_PROPOSAL}", f"#16-31 {SUMMARY_REFINE}"]

    def test_regex_recall_returns_the_original_log_records(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        proposal_hits = engine.recall("Proposal", topk=0, mode="regex")
        assert [hit[3] for hit in proposal_hits] == [PROPOSAL]
        assert proposal_hits[0][1] == 0
        supersession_hits = engine.recall("Supersession", topk=0, mode="regex")
        assert supersession_hits[0][1] == TOTAL - 1
        assert supersession_hits[0][3] == SUPERSESSION


# ---------------------------------------------------------------------------
# Configured wake budget -> injected context (forced 4 / 8 / default 96)
# ---------------------------------------------------------------------------


class TestConfiguredWakeBudget:
    @pytest.mark.parametrize("budget,expected_lines", [(4, 4), (8, 8), (96, TOTAL)])
    def test_configured_budget_controls_the_injected_context(
        self, tmp_path, budget, expected_lines
    ):
        provider = _provider(tmp_path / "home", wake_budget=budget)
        assert provider._wake_budget() == budget
        for index, text in enumerate(_record_texts()):
            provider._engine.append(text, date=_date_for(index))
        _drain(provider._engine)

        wake = _wake_lines_from_context(provider.prefetch(""))
        assert len(wake) == expected_lines
        if budget >= TOTAL:
            # A budget that fits the whole store prints every memory verbatim.
            assert any(PROPOSAL in line for line in wake)
            assert any(SUPERSESSION in line for line in wake)
        elif budget == 4:
            assert any(SUMMARY_SUPERSEDE in line for line in wake)
        else:
            assert any(SUPERSESSION in line for line in wake)

    def test_default_budget_is_the_memo_default_96(self, tmp_path):
        provider = _provider(tmp_path / "home")
        assert provider._wake_budget() == 96 == WAKE_LINES

    def test_prefetch_surfaces_the_supersession_summary_as_a_retrieval_input(self, tmp_path):
        """The model's only route to 'latest decision' is the summary the caller wrote."""
        provider = _provider(tmp_path / "home", wake_budget=4)
        for index, text in enumerate(_record_texts()):
            provider._engine.append(text, date=_date_for(index))
        _drain(provider._engine)
        context = provider.prefetch("")
        assert SUMMARY_SUPERSEDE in context
        # And the superseded original is still there, at the default budget.
        verbatim = provider._engine.wake_lines(budget=WAKE_LINES)
        assert any(PROPOSAL in line for line in verbatim)


# ---------------------------------------------------------------------------
# Structural chaining is NOT semantic conflict resolution
# ---------------------------------------------------------------------------


class TestStructuralNotSemantic:
    def test_capabilities_disclaim_semantic_resolution(self):
        caps = OptMemProvider().capabilities()
        assert caps["structural_chaining"] is True
        assert caps["semantic_conflict_resolution"] is False
        assert caps["local_only"] is True
        assert caps["append_only"] is True

    def test_superseded_and_superseding_records_coexist(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        ids = {hit[1] for hit in engine.recall("alpha", topk=0, mode="regex")}
        assert 0 in ids, "the superseded proposal stays retrievable"
        assert TOTAL - 1 in ids, "the supersession is retrievable too"
        # Nothing invalidated #0: a wake at the default budget prints it verbatim.
        assert any(PROPOSAL in line for line in engine.wake_lines(budget=WAKE_LINES))

    def test_latest_decision_is_caller_written_not_algorithmic(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        # The engine stores exactly the caller's summary — it did not infer that
        # beta supersedes alpha, and it keeps the obsolete record intact.
        assert engine._tree_get(0, 32) == SUMMARY_TOP
        assert engine.recall("Proposal", topk=0, mode="regex")[0][3] == PROPOSAL
        assert engine.recall("plan beta", topk=0, mode="regex")[0][3] == SUPERSESSION

    def test_forget_drops_a_summary_but_not_the_raw_record(self, tmp_path):
        engine = _build_engine(tmp_path)
        _drain(engine)
        log_before = (tmp_path / "LOG.txt").read_bytes()
        engine.forget(0, 16)
        assert engine._tree_get(0, 16) is None
        assert (tmp_path / "LOG.txt").read_bytes() == log_before
        assert engine.recall("Proposal", topk=0, mode="regex")[0][3] == PROPOSAL
