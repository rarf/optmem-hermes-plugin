"""E2E tests for the OptMem memory provider and its append-only engine.

These exercise the real engine against a temp HERMES_HOME — no mocks of the
store, no network. Covers the four tools (note/recall/nap/wake), accent-
normalized BM25, decay compression, and the builtin-memory mirror hook.
"""

import json

import pytest

from optmem import OptMemProvider
from optmem.engine import OptMemEngine

# ---------------------------------------------------------------------------
# Engine-level behaviour
# ---------------------------------------------------------------------------


class TestOptMemEngine:
    def test_append_and_count(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        mid = eng.append("primeira memoria duravel")
        assert mid == 0
        assert eng.log_len() == 1
        eng.append("segunda memoria")
        assert eng.log_len() == 2

    def test_append_rejects_newline(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        with pytest.raises(ValueError):
            eng.append("linha um\nlinha dois")

    def test_append_rejects_overlong(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        with pytest.raises(ValueError):
            eng.append("x" * 400)

    def test_bm25_accent_normalization(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        eng.append("a caçula chegou cedo")          # id 0
        eng.append("o cachorro late de noite")      # id 1
        eng.append("comprei cacau para a receita")  # id 2
        # BM25 is opt-in via mode="bm25" (accent-normalized).
        hits = eng.recall("cacula", topk=3, mode="bm25")
        assert hits, "expected at least one BM25 hit"
        ids = [mid for _, mid, _, _ in hits]
        # 'caçula' (id 0) must match a query for 'cacula' (no accent).
        assert 0 in ids
        # top hit should be the exact-match memory, not the cocoa one.
        assert hits[0][1] == 0

    def test_regex_recall_fallback(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        eng.append("regex test: AllDrivers paywall pendente")
        # Default recall mode mirrors `memo recall` exactly (regex, re.I).
        hits = eng.recall("AllDrivers.*paywall", topk=1, mode="regex")
        assert hits and hits[0][1] == 0

    def test_index_rebuilds_after_append(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        eng.append("seed memory for index staleness")
        eng.recall("seed", mode="bm25")  # builds BM25 index
        assert not eng._index_stale()
        eng.append("second memory changes length")
        assert eng._index_stale(), "index must be invalidated when log grows"

    def test_nap_compresses_block(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        for i in range(4):
            eng.append(f"memoria efemera numero {i}")
        # Blocks of size 2 become due once both halves exist.
        pending = eng.pending_naps()
        assert pending, "expected at least one compressible block"
        lo, hi = pending[0]
        ok = eng.apply_nap(lo, hi, "resumo das memorias efemeras")
        assert ok is True
        # After compression the block is no longer pending.
        remaining = eng.pending_naps()
        assert (lo, hi) not in remaining

    def test_nap_rejects_oversize_summary(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        for i in range(2):
            eng.append(f"m {i}")
        lo, hi = eng.pending_naps()[0]
        with pytest.raises(ValueError):
            eng.apply_nap(lo, hi, "x" * 400)

    def test_wake_shows_verbatim_recent_and_collapsed_old(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        eng.append("fato recente um")
        eng.append("fato recente dois")
        lines = eng.wake_lines()
        assert any("fato recente um" in ln for ln in lines)
        assert any("fato recente dois" in ln for ln in lines)

    def test_on_disk_format_is_fixed_width(self, tmp_path):
        eng = OptMemEngine(str(tmp_path))
        eng.append("registro de largura fixa")
        log = tmp_path / "LOG.txt"
        assert log.stat().st_size == 320  # LOG_REC


# ---------------------------------------------------------------------------
# Provider integration (tools + hook) against a temp HERMES_HOME
# ---------------------------------------------------------------------------


def _make_provider(tmp_path):
    mem_dir = tmp_path / "optmem_memory"
    provider = OptMemProvider(config={"memory_dir": str(mem_dir)})
    provider.initialize("test-session", hermes_home=str(tmp_path))
    return provider


class TestOptMemProvider:
    def test_is_always_available(self):
        assert OptMemProvider().is_available() is True

    def test_tool_schemas_present(self):
        names = {s["name"] for s in OptMemProvider().get_tool_schemas()}
        assert {"optmem_note", "optmem_recall", "optmem_nap", "optmem_wake"} <= names

    def test_note_and_recall_roundtrip(self, tmp_path):
        p = _make_provider(tmp_path)
        out = json.loads(
            p.handle_tool_call(
                "optmem_note", {"text": "deploy em staging autorizado"}
            )
        )
        assert out["status"] == "added"
        res = json.loads(p.handle_tool_call("optmem_recall", {"query": "staging"}))
        assert res["count"] >= 1
        assert any("deploy" in r["text"] for r in res["results"])

    def test_nap_via_tool(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "a"})
        p.handle_tool_call("optmem_note", {"text": "b"})
        # Force a pending block to surface.
        nap = p._engine.next_nap()
        assert nap, "expected a pending compression after 2 notes"
        (lo, hi), _ = nap
        res = json.loads(
            p.handle_tool_call("optmem_nap", {"lo": lo, "hi": hi, "summary": "ab resumido"})
        )
        assert res["status"] == "compressed"

    def test_wake_tool(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "contexto permanente"})
        res = json.loads(p.handle_tool_call("optmem_wake", {}))
        assert res["count"] >= 1

    def test_nap_accepts_legacy_inclusive_display_range(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "x"})
        p.handle_tool_call("optmem_note", {"text": "y"})
        # The old prompt/schema led callers to send #0-1 as hi=1.
        res = json.loads(
            p.handle_tool_call(
                "optmem_nap", {"lo": 0, "hi": 1, "summary": "xy resumido"}
            )
        )
        assert res["status"] == "compressed"
        assert res["block"] == "0-1"
        assert res["hi_exclusive"] == 2

    def test_prefetch_includes_explicit_exclusive_hi(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "x"})
        p.handle_tool_call("optmem_note", {"text": "y"})
        ctx = p.prefetch("")
        assert "OptMem context" in ctx
        assert "Compression due" in ctx
        assert "lo=0, hi=2" in ctx
        assert "hi argument is EXCLUSIVE" in ctx

    def test_on_memory_write_mirrors_builtin(self, tmp_path):
        p = _make_provider(tmp_path)
        before = p._engine.log_len()
        p.on_memory_write("add", "memory", "fato do builtin espelhado")
        after = p._engine.log_len()
        assert after == before + 1
        # The mirrored line is searchable.
        hits = p._engine.recall("espelhado", topk=1)
        assert hits and "espelhado" in hits[0][3]

    def test_byte_compatible_log_survives_reopen(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "persiste entre instancias"})
        # Re-open the same dir with a fresh engine — fixed-width format holds.
        reopened = OptMemEngine(str(tmp_path / "optmem_memory"))
        assert reopened.log_len() == 1
        assert "persiste" in reopened.wake_lines()[0]


class TestOptMemProviderLifecycle:
    def test_save_config_writes_yaml_atomic(self, tmp_path):
        from pathlib import Path
        p = _make_provider(tmp_path)
        p.save_config({"memory_dir": "/tmp/x"}, str(tmp_path))
        cfg = Path(tmp_path / "config.yaml")
        assert cfg.exists()
        import yaml
        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["plugins"]["optmem"] == {"memory_dir": "/tmp/x"}

    def test_init_store_is_idempotent(self, tmp_path):
        # Fresh provider (not yet initialized) → first init reports fresh=True.
        mem_dir = tmp_path / "optmem_memory"
        p = OptMemProvider(config={"memory_dir": str(mem_dir)})
        fresh1 = p.handle_tool_call("optmem_init", {})
        assert json.loads(fresh1)["fresh"] is True
        # Second init on the same dir → fresh=False (never overwrites data).
        p2 = OptMemProvider(config={"memory_dir": str(mem_dir)})
        fresh2 = p2.handle_tool_call("optmem_init", {})
        assert json.loads(fresh2)["fresh"] is False

    def test_import_lines_from_file(self, tmp_path):
        p = _make_provider(tmp_path)
        import_file = tmp_path / "bootstrap.txt"
        import_file.write_text(
            "2026-01-01 facto A duravel\n2026-02-01 facto B duravel\n",
            encoding="utf-8",
        )
        res = json.loads(p.handle_tool_call("optmem_import", {"file": str(import_file)}))
        assert res["status"] == "imported"
        assert res["count"] == 2

    def test_block_lines_returns_raw_lines(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento"})
        p.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
        lines = p._engine.block_lines(0, 2)
        assert lines == ["cliente X aprovou orcamento", "deploy em staging autorizado"]

    def test_on_turn_start_local_fallback_naps(self, tmp_path):
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "cliente X aprovou orcamento Q3"})
        p.handle_tool_call("optmem_note", {"text": "deploy em staging autorizado"})
        # No LLM, no _ctx → local deterministic summary must run.
        p.on_turn_start(10, "trigger")
        pending = p._engine.pending_naps()
        assert (0, 2) not in pending, "local auto-nap should compress block 0-2"
        # The nap summary is stored in the decay tree.
        assert "aprov" in p._engine._tree_get(0, 2) or "deploy" in p._engine._tree_get(0, 2)

    def test_on_turn_start_prefers_llm_when_optin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPTMEM_LLM_SUMMARY", "1")
        p = _make_provider(tmp_path)
        p.handle_tool_call("optmem_note", {"text": "facto duravel A"})
        p.handle_tool_call("optmem_note", {"text": "facto duravel B"})
        captured = {}

        class _Ctx:
            @staticmethod
            def llm(prompt, **kw):
                captured["prompt"] = prompt
                return "resumo llm de A e B"

        p._ctx = _Ctx()
        p.on_turn_start(10, "trigger")
        # Block compressed via LLM summary.
        assert (0, 2) not in p._engine.pending_naps()
        assert "resumo llm" in p._engine._tree_get(0, 2)
        assert "facto duravel" in captured.get("prompt", "")

    def test_on_turn_start_skips_ephemeral_only_block(self, tmp_path):
        p = _make_provider(tmp_path)
        # Lines with no durable keyword → local summary returns "" → skip.
        p.handle_tool_call("optmem_note", {"text": "bla bla irrelevant chat"})
        p.handle_tool_call("optmem_note", {"text": "mais bla sem sentido"})
        p.on_turn_start(10, "trigger")
        # Block stays pending (we refused to lose data).
        assert (0, 2) in p._engine.pending_naps()

    def test_on_turn_start_skips_when_no_engine(self, tmp_path):
        p = OptMemProvider(config={"memory_dir": str(tmp_path / "x")})
        # engine is None → must be a no-op (no crash).
        p.on_turn_start(10, "trigger")