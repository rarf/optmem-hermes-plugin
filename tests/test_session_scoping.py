"""Wake/reset behavior across session boundaries.

Baseline defect: the provider kept ONE ``_woke_session`` boolean and ignored the
``session_id`` argument, so the very first session that ran in a process
consumed the wake context for EVERY later session (``/new``, ``/resume``, a
compression child, the gateway reusing one manager across turns). The second
conversation therefore started with no OptMem context at all.

Two hooks exist to fix this without a core edit: ``prefetch(query,
session_id=...)`` (the manager always passes the live session id) and
``on_session_switch(new_session_id, reset=..., rewound=...)``.
"""

from __future__ import annotations

from optmem import OptMemProvider


def _provider(tmp_path):
    provider = OptMemProvider(config={"memory_dir": str(tmp_path / "store")})
    provider.initialize("session-a", hermes_home=str(tmp_path))
    provider.handle_tool_call("optmem_note", {"text": "a memoria permanente de referencia"})
    return provider


def _wake_calls(provider, *session_ids):
    """Call prefetch once per session id; return how many included the wake block."""
    return [
        "## OptMem context" in provider.prefetch("pergunta", session_id=sid) for sid in session_ids
    ]


class TestWakeSessionScoping:
    def test_wake_is_injected_once_per_session(self, tmp_path):
        p = _provider(tmp_path)
        woke = _wake_calls(p, "s1", "s1", "s1")
        assert woke == [True, False, False]

    def test_a_different_session_id_gets_wake_again(self, tmp_path):
        """The whole point: session 2 must not inherit session 1's wake."""
        p = _provider(tmp_path)
        woke = _wake_calls(p, "s1", "s2")
        assert woke == [True, True], "a new session must receive OptMem context"

    def test_third_session_still_gets_wake(self, tmp_path):
        p = _provider(tmp_path)
        woke = _wake_calls(p, "s1", "s2", "s3")
        assert woke == [True, True, True]

    def test_explicit_switch_with_reset_rewakes_same_id(self, tmp_path):
        p = _provider(tmp_path)
        assert p.prefetch("q", session_id="s1").count("## OptMem context") == 1
        p.on_session_switch("s1", reset=True)
        assert "## OptMem context" in p.prefetch("q", session_id="s1")

    def test_rewound_session_rewakes(self, tmp_path):
        p = _provider(tmp_path)
        assert "## OptMem context" in p.prefetch("q", session_id="s1")
        p.on_session_switch("s2", rewound=True)
        assert "## OptMem context" in p.prefetch("q", session_id="s2")

    def test_compression_switch_does_not_duplicate_wake_for_the_same_id(self, tmp_path):
        """reset=False + same id (in-place compression) keeps the transcript's context."""
        p = _provider(tmp_path)
        assert "## OptMem context" in p.prefetch("q", session_id="s1")
        p.on_session_switch("s1", reset=False, reason="compression")
        assert "## OptMem context" not in p.prefetch("q", session_id="s1")

    def test_compression_child_session_gets_wake(self, tmp_path):
        """reset=False but a NEW id: its transcript no longer holds the old block."""
        p = _provider(tmp_path)
        assert "## OptMem context" in p.prefetch("q", session_id="parent")
        p.on_session_switch("child", reset=False, reason="compression")
        assert "## OptMem context" in p.prefetch("q", session_id="child")

    def test_empty_session_id_uses_the_bound_session(self, tmp_path):
        """The manager may call with session_id="" — don't treat every turn as new."""
        p = _provider(tmp_path)
        first = p.prefetch("q").count("## OptMem context")
        second = p.prefetch("q", session_id="").count("## OptMem context")
        assert (first, second) == (1, 0)

    def test_bound_session_tracks_initialize_session(self, tmp_path):
        p = _provider(tmp_path)
        assert "## OptMem context" in p.prefetch("q")  # bound to "session-a"
        assert "## OptMem context" not in p.prefetch("q", session_id="session-a")

    def test_state_stays_bounded_across_many_switches(self, tmp_path):
        """Wake bookkeeping must not accumulate per-session state forever."""
        p = _provider(tmp_path)
        for i in range(200):
            p.prefetch("q", session_id=f"s{i}")
            p.on_session_switch(f"s{i}", reset=True)
        # Coming back to the first session re-wakes (only the live key is remembered).
        assert "## OptMem context" in p.prefetch("q", session_id="s0")

    def test_switch_ignores_empty_new_session_id(self, tmp_path):
        p = _provider(tmp_path)
        assert "## OptMem context" in p.prefetch("q", session_id="s1")
        p.on_session_switch("", reset=True)  # must be a no-op, not a crash
        assert "## OptMem context" not in p.prefetch("q", session_id="s1")
