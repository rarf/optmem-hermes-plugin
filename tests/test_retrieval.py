"""Natural-language retrieval vs. `memo` regex parity.

Baseline defect: ``prefetch`` compiled the user's raw message as a regex. A
user sentence ("o que voce sabe sobre a caçula?") became a literal-pattern
search that matched nothing, and a sentence containing an unbalanced paren
raised ``re.error`` (swallowed by prefetch, surfaced as a bare traceback by
``optmem_recall``). Neither is a useful answer, and neither is honest.

Contract:
- ``mode="regex"`` stays the documented `memo`-parity behavior; an invalid
  pattern is reported as a clear error instead of a raw regex exception.
- ``mode="auto"`` (the default for prefetch and the tool) keeps regex for
  regex-looking queries and routes sentences to token search.
- The response says which mode actually ran — no silent substitution.
"""

from __future__ import annotations

import json

import pytest

from optmem import OptMemProvider
from optmem.engine import OptMemEngine


def _engine(tmp_path):
    eng = OptMemEngine(str(tmp_path / "store"))
    eng.append("a caçula nasceu em março de dois mil e vinte")
    eng.append("o AllDrivers usa paywall no plano gratuito")
    eng.append("orçamento da obra do telhado aprovado")
    return eng


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #


class TestEngineAutoRecall:
    def test_natural_sentence_finds_the_memory(self, tmp_path):
        eng = _engine(tmp_path)
        hits = eng.recall("quando a cacula nasceu?", mode="auto")
        assert hits, "a sentence must retrieve, not compile to a useless literal"
        assert any("cacula" in h[3].lower() or "caçula" in h[3] for h in hits)

    def test_accents_ignored_in_both_directions(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng.recall("marco de dois mil", mode="auto")
        assert eng.recall("março de dois mil", mode="auto")

    def test_regex_metacharacters_in_a_sentence_do_not_raise(self, tmp_path):
        eng = _engine(tmp_path)
        # Unbalanced paren: would be an invalid regex.
        hits = eng.recall("o que houve com (a familia?", mode="auto")
        assert isinstance(hits, list)

    def test_auto_falls_back_to_substring_when_tokens_are_unranked(self, tmp_path):
        """A rare token BM25 cannot rank must still be found literally."""
        eng = OptMemEngine(str(tmp_path / "store"))
        eng.append("codigo interno ZXQ-4481 liberado para producao")
        hits = eng.recall("qual e o codigo zxq-4481?", mode="auto")
        assert hits and any("ZXQ-4481" in h[3] for h in hits)

    def test_auto_mode_is_quiet_on_an_empty_store(self, tmp_path):
        assert OptMemEngine(str(tmp_path / "empty")).recall("qualquer coisa?", mode="auto") == []

    def test_explicit_regex_mode_reports_invalid_pattern(self, tmp_path):
        eng = _engine(tmp_path)
        with pytest.raises(ValueError) as err:
            eng.recall("(unbalanced", mode="regex")
        assert "regex" in str(err.value).lower()

    def test_explicit_regex_mode_still_matches_like_memo(self, tmp_path):
        eng = _engine(tmp_path)
        hits = eng.recall("AllDrivers.*paywall", mode="regex")
        assert hits and hits[0][1] == 1

    def test_long_query_is_bounded(self, tmp_path):
        eng = _engine(tmp_path)
        hits = eng.recall("palavra " * 5000, mode="auto")
        assert isinstance(hits, list)

    def test_topk_is_respected(self, tmp_path):
        eng = _engine(tmp_path)
        hits = eng.recall("a memoria de teste", mode="auto", topk=2)
        assert len(hits) <= 2

    def test_unknown_mode_is_rejected(self, tmp_path):
        eng = _engine(tmp_path)
        with pytest.raises(ValueError):
            eng.recall("x", mode="semantic")


class TestNaturalLanguageClassifier:
    @pytest.mark.parametrize(
        "text",
        [
            "quando a caçula nasceu?",
            "what did we decide about the paywall",
            "lembra do orcamento do telhado?",
            "me conta sobre a casa de marcia",
        ],
    )
    def test_sentences_are_natural_language(self, text):
        from optmem.engine import is_natural_language

        assert is_natural_language(text) is True

    @pytest.mark.parametrize("text", ["paywall", "caçula", "AllDrivers.*paywall", "^#12", "marcia"])
    def test_short_or_regex_queries_are_not(self, text):
        from optmem.engine import is_natural_language

        assert is_natural_language(text) is False


# --------------------------------------------------------------------------- #
# provider tools
# --------------------------------------------------------------------------- #


def _provider(tmp_path):
    p = OptMemProvider(config={"memory_dir": str(tmp_path / "store")})
    p.initialize("s", hermes_home=str(tmp_path))
    p.handle_tool_call("optmem_note", {"text": "a caçula nasceu em março"})
    p.handle_tool_call("optmem_note", {"text": "o AllDrivers usa paywall no plano gratuito"})
    return p


class TestProviderRecall:
    def test_sentence_query_returns_hits_and_reports_mode(self, tmp_path):
        p = _provider(tmp_path)
        out = json.loads(p.handle_tool_call("optmem_recall", {"query": "quando a caçula nasceu?"}))
        assert out["count"] >= 1
        assert out["mode_used"] in {"token", "bm25"}

    def test_invalid_regex_mode_is_an_actionable_error(self, tmp_path):
        p = _provider(tmp_path)
        out = json.loads(
            p.handle_tool_call("optmem_recall", {"query": "(unbalanced", "mode": "regex"})
        )
        assert "error" in out
        assert "regex" in out["error"].lower()
        assert "Traceback" not in json.dumps(out)

    def test_bm25_flag_still_forces_bm25(self, tmp_path):
        p = _provider(tmp_path)
        out = json.loads(p.handle_tool_call("optmem_recall", {"query": "cacula", "bm25": True}))
        assert out["count"] >= 1
        assert out["mode_used"] == "bm25"

    def test_explicit_regex_mode_matches_memo_behavior(self, tmp_path):
        p = _provider(tmp_path)
        out = json.loads(
            p.handle_tool_call("optmem_recall", {"query": "AllDrivers.*paywall", "mode": "regex"})
        )
        assert out["count"] == 1
        assert out["mode_used"] == "regex"

    def test_prefetch_uses_natural_retrieval_for_a_sentence(self, tmp_path):
        p = _provider(tmp_path)
        context = p.prefetch("o que voce sabe sobre a caçula?", session_id="s1")
        assert "caçula" in context
        assert "OptMem recall" in context

    def test_prefetch_does_not_inject_on_a_broken_regex(self, tmp_path):
        p = _provider(tmp_path)
        # Must neither raise nor inject an error blob into the model context.
        context = p.prefetch("o que houve com (a familia?", session_id="s1")
        assert "Traceback" not in context
        assert "re.error" not in context

    def test_recall_mode_config_forces_regex(self, tmp_path):
        from optmem.config import write_declared_config

        write_declared_config(tmp_path, {"recall_mode": "regex"})
        p = _provider(tmp_path)
        out = json.loads(p.handle_tool_call("optmem_recall", {"query": "caçula"}))
        assert out["mode_used"] == "regex"

    def test_recall_mode_config_bm25(self, tmp_path):
        from optmem.config import write_declared_config

        write_declared_config(tmp_path, {"recall_mode": "bm25"})
        p = _provider(tmp_path)
        out = json.loads(p.handle_tool_call("optmem_recall", {"query": "cacula"}))
        assert out["mode_used"] == "bm25"

    def test_tool_schema_documents_the_mode_argument(self, tmp_path):
        schema = next(
            s for s in OptMemProvider().get_tool_schemas() if s["name"] == "optmem_recall"
        )
        assert "mode" in schema["parameters"]["properties"]
        assert set(schema["parameters"]["properties"]["mode"]["enum"]) == {"auto", "regex", "bm25"}
