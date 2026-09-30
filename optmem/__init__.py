"""OptMem memory provider for Hermes.

A portable, append-only, decay-compressed memory backend that plugs into
Hermes via the MemoryProvider interface. On-disk format is byte-compatible
with https://github.com/VictorTaelin/OptMem, so logs are interchangable
with the original ``memo`` tool.

Activation (profile-scoped, set in config.yaml):
    memory:
      provider: optmem

Modes
-----
- **hybrid** (default): OptMem runs alongside the built-in MEMORY.md/USER.md
  store. Nothing about the native store changes.
- **optmem-only**: the native store is switched off
  (``memory.memory_enabled: false`` + ``memory.user_profile_enabled: false``)
  *after* the native facts are migrated and verified. Switching is done through
  ``hermes optmem mode optmem-only``, which is gated on a successful import —
  never by silently changing config from the provider.

Configuration precedence (highest first):
1. ``<HERMES_HOME>/optmem/config.json`` — the declared schema
   (``optmem/config_schema.py``) written by the Desktop/dashboard config panel
   and by ``hermes optmem``.
2. ``config.yaml`` ``memory.optmem`` / ``plugins.optmem`` — legacy keys from
   0.2.0, still honoured.
3. Built-in defaults.

Design notes
------------
- The agent records durable facts with ``optmem_note`` (one line, <=280 bytes).
- When a pair of memories forms, the provider surfaces a pending compression
  via ``prefetch`` (and ``optmem_nap``), and the agent performs the "nap":
  it merges the block into one line. This is Taelin's "nap, don't sleep".
- ``optmem_recall`` defaults to the ``memo`` CLI's regex behavior; a
  natural-language sentence is detected and routed to token search instead of
  being compiled as a (broken or useless) regex.
"""

from __future__ import annotations

import logging
from typing import Any

__version__ = "0.3.0"


# Hermes-only dependencies. The plugin must import cleanly in a bare CI
# environment (no gateway on sys.path) so `import optmem` works for tests and
# the provider's own unit suite. We fall back to a builtin base + local
# tool_error when Hermes is absent.
try:
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - exercised only outside the gateway

    class MemoryProvider:  # type: ignore[no-redef]
        """Minimal stand-in so the module imports without the Hermes core."""

        def name(self) -> str:
            raise NotImplementedError

        def is_available(self) -> bool:
            raise NotImplementedError

        def initialize(self, session_id: str, **kwargs) -> None:
            raise NotImplementedError

        def get_tool_schemas(self):
            raise NotImplementedError


try:
    from tools.registry import tool_error
except Exception:  # pragma: no cover

    def tool_error(message: str, **extra: Any) -> str:
        import json

        return json.dumps({"error": message, **extra}, ensure_ascii=False)


from .config import (
    OptMemConfig,
    default_hermes_home,
    legacy_plugin_config,
    resolve_config,
)
from .engine import ENTRY_CHARS, RAW_MAX, WAKE_LINES, OptMemEngine

logger = logging.getLogger(__name__)


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import cfg_get, load_config

        config = load_config()
        return cfg_get(config, "plugins", "optmem", default={}) or {}
    except Exception:
        return {}


def _get_hermes_home() -> str:
    """Return HERMES_HOME, using the Hermes helper when available else env/default."""
    return default_hermes_home()


def _display_hermes_home() -> str:
    try:
        from hermes_constants import display_hermes_home

        return str(display_hermes_home())
    except Exception:
        return _get_hermes_home()


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

NOTE_SCHEMA = {
    "name": "optmem_note",
    "description": (
        "Record one durable memory line to the OptMem append-only log "
        "(family facts, decisions, events of lasting effect). One line, "
        "max 280 chars. If a compression is due, do it (optmem_nap) before "
        "your next action. Use for things worth remembering forever — not "
        "ephemeral chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The memory, one line (<=280 chars).",
            },
        },
        "required": ["text"],
    },
}

RECALL_SCHEMA = {
    "name": "optmem_recall",
    "description": (
        "Search the entire OptMem history. mode='auto' (default) keeps the "
        "original OptMem `memo recall` regex for pattern-like queries and routes "
        "a natural-language sentence to accent-normalized token search; "
        "mode='regex' forces exact `memo` parity (an invalid pattern is reported "
        "as an error, not a traceback); mode='bm25' forces ranked fuzzy search "
        "(e.g. 'cacula' matches 'caçula'). The response reports mode_used. Use "
        "when you need an old fact, decision, or event."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (a sentence or a regex pattern).",
            },
            "topk": {"type": "integer", "description": "Max results (default 5)."},
            "mode": {
                "type": "string",
                "enum": ["auto", "regex", "bm25"],
                "description": "Retrieval mode. Defaults to the configured recall_mode ('auto').",
            },
            "bm25": {
                "type": "boolean",
                "description": "Legacy alias for mode='bm25' (default false).",
            },
        },
        "required": ["query"],
    },
}

NAP_SCHEMA = {
    "name": "optmem_nap",
    "description": (
        "Apply a compression the provider asked for. Call optmem_nap with "
        "the block id and a one-line summary (<=280 chars) that keeps what "
        "has lasting effect and drops the rest. Invent nothing. Mirrors "
        "Taelin's 'nap, don't sleep'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lo": {"type": "integer", "description": "Block start id (inclusive)."},
            "hi": {
                "type": "integer",
                "description": "Block end id (EXCLUSIVE). A displayed #8-9 block uses hi=10.",
            },
            "summary": {"type": "string", "description": "One-line compression."},
        },
        "required": ["lo", "hi", "summary"],
    },
}

WAKE_SCHEMA = {
    "name": "optmem_wake",
    "description": (
        "Print the current OptMem context (recent memories verbatim, old ones "
        "decayed into summaries). Run at session start or when you need the "
        "full picture."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ZOOM_SCHEMA = {
    "name": "optmem_zoom",
    "description": (
        "Open a decay-tree node (block lo-hi, e.g. 0-15) into its two halves, "
        "down to the raw memories. Use to recover detail compressed away by a "
        "nap. hi is exclusive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lo": {"type": "integer", "description": "Block start id (inclusive)."},
            "hi": {"type": "integer", "description": "Block end id (EXCLUSIVE)."},
        },
        "required": ["lo", "hi"],
    },
}

FORGET_SCHEMA = {
    "name": "optmem_forget",
    "description": (
        "Drop a bad summary at block lo-hi so the next nap rebuilds it. hi is exclusive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lo": {"type": "integer", "description": "Block start id (inclusive)."},
            "hi": {"type": "integer", "description": "Block end id (EXCLUSIVE)."},
        },
        "required": ["lo", "hi"],
    },
}

CONFIG_SCHEMA = {
    "name": "optmem_config",
    "description": (
        "Show or change OptMem size knobs for this store (mirrors `memo config`). "
        "Pass NAME=VALUE pairs to change (e.g. ENTRY_CHARS=280), or no args to "
        "show current values. Allowed: WAKE_LINES, ENTRY_CHARS, RAW_MAX, "
        "PART_CHARS, PART_LINES."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional NAME=VALUE pairs to apply.",
            },
        },
    },
}

IMPORT_SCHEMA = {
    "name": "optmem_import",
    "description": (
        "Bulk-load historical memories from a file of 'YYYY-MM-DD <text>' lines "
        "(one identity bootstrap, used once). Mirrors `memo import`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "Path to the import file."},
        },
        "required": ["file"],
    },
}

INIT_SCHEMA = {
    "name": "optmem_init",
    "description": (
        "Create this OptMem store deliberately (LOG.txt + TREE/ + config). "
        "Mirrors `memo init`. Safe to re-run; never overwrites existing data."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


class OptMemProvider(MemoryProvider):
    """Hermes MemoryProvider backed by the OptMem append-only engine."""

    def __init__(self, config: dict | None = None):
        # `config` is an explicit legacy mapping injection (tests / callers that
        # already read config.yaml). The resolved, validated view lives in
        # self._config (optmem.config) and is rebuilt in initialize() against the
        # real profile home.
        #
        # The legacy fallback is read with our own PyYAML-based reader rather
        # than the host's ``hermes_cli.config.load_config()``: the host's reader
        # scaffolds HERMES_HOME (SOUL.md, logs/, sessions/, …) as a side effect,
        # which must not happen from a provider being constructed during
        # discovery/``register()``. ``initialize()`` re-resolves against the real
        # profile anyway, so nothing is lost at session time.
        self._plugin_config: dict | None = dict(config) if config else None
        self._config: OptMemConfig = resolve_config(
            _get_hermes_home(),
            self._plugin_config
            if self._plugin_config is not None
            else legacy_plugin_config(_get_hermes_home()),
        )
        self._engine: OptMemEngine | None = None
        self._memory_dir: str | None = None
        self._session_id: str = ""
        self._agent_context: str = "primary"
        self._woke_key: str | None = None

    @property
    def name(self) -> str:
        # Canonical registered name. It is NOT the bare upstream key: this
        # plugin is an independent Hermes integration of Victor Taelin's OptMem
        # design, not the upstream project, and the Hermes plugin catalog
        # reserves the bare key for the affiliated project
        # (plugin-catalog/README.md, "Names"). The Python package, the on-disk
        # store keep their names; the CLI becomes `hermes optmem-hermes`.
        return "optmem-hermes"

    def is_available(self) -> bool:
        # Pure-local, no credentials. Always available.
        return True

    def get_config_schema(self):
        default_dir = f"{_display_hermes_home()}/optmem_memory"
        return [
            {
                "key": "memory_dir",
                "description": (
                    "Directory for LOG.txt + TREE/ (default: $HERMES_HOME/optmem_memory)"
                ),
                "default": default_dir,
            },
        ]

    def save_config(self, values, hermes_home):
        # The setup wizard and native panel share one provider-owned file.
        # Saving intent does not disable native memory; mode switching is gated.
        from .config import write_declared_config

        write_declared_config(hermes_home, values)

    def initialize(self, session_id: str, **kwargs) -> None:
        # Honor hermes_home from kwargs when provided (profile-scoped storage),
        # else fall back to the global helper (or env/default in CI).
        home = str(kwargs.get("hermes_home") or _get_hermes_home())
        self._hermes_home = home
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        legacy = (
            dict(self._plugin_config)
            if self._plugin_config is not None
            else legacy_plugin_config(home)
        )
        self._config = resolve_config(home, legacy)
        mem_dir = self._config.memory_dir or f"{home}/optmem_memory"
        if isinstance(mem_dir, str):
            mem_dir = mem_dir.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
        self._memory_dir = mem_dir
        self._engine = OptMemEngine(mem_dir)
        self._session_id = session_id
        # Wake bookkeeping is ONE key: the session that already received the decay
        # context in THIS process. A single boolean leaked the first session's wake
        # to every later one (/new, /resume, compression child, gateway reuse), so
        # the second conversation started with no OptMem context at all.
        self._woke_key: str | None = None

    @property
    def config(self):
        """Resolved configuration (see optmem.config)."""
        return self._config

    def capabilities(self) -> dict[str, Any]:
        """What this provider actually does — stated so it can be verified, not assumed.

        ``structural_chaining`` is the binary decay tree: raw memories are merged
        pairwise into summaries, and summaries of summaries, so a query can be
        answered from a compressed block and zoomed back to the raw records.

        ``semantic_conflict_resolution`` is deliberately False and NOT claimed:
        a later memory does not automatically invalidate an earlier one. "Policy
        X was replaced by policy Y" is two records, and the model must read both
        and judge which is current. Building an event graph with automatic
        supersession is out of scope here.
        """
        return {
            "structural_chaining": True,
            "semantic_conflict_resolution": False,
            "append_only": True,
            "raw_retention": "LOG.txt is never rewritten; compressed blocks keep raw records",
            "summary_compression_lossy": True,
            "forget_scope": "summaries only — raw records remain unchanged, not erased",
            "local_only": True,
        }

    def _session_key(self, session_id: str = "") -> str:
        """The session the call belongs to: explicit id, else the bound one."""
        return session_id or self._session_id or ""

    def _wake_budget(self) -> int:
        """Context lines printed by a wake (a reading budget, not a storage cap)."""
        try:
            return int(self._config.wake_budget)
        except (TypeError, ValueError, AttributeError):
            return WAKE_LINES

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Rebind per-session state after ``/new``, ``/resume``, ``/reset`` or compression.

        ``reset``/``rewound`` mean the conversation genuinely starts over (or its
        transcript was truncated), so the next turn must re-inject the decay
        context. A plain session-id change re-wakes on its own: the id no longer
        matches the one that already consumed the wake.
        """
        if not new_session_id:
            return
        self._session_id = new_session_id
        if reset or rewound:
            self._woke_key = None

    # -- context ------------------------------------------------------------

    def system_prompt_block(self) -> str:
        # STATIC: must not contain counters (log_len / pending_naps) so the
        # cached system-prompt prefix is never invalidated. Instructions only.
        return (
            "# OptMem (permanent memory)\n"
            "Active. Use optmem_wake at session start to load context.\n"
            "Record durable facts with optmem_note: ONE line, max 280 bytes "
            "(a single atomic fact — do NOT write long paragraphs; split "
            "distinct facts into separate notes). If optmem_nap asks for a "
            "compression, do it before your next action.\n"
            "Search all history with optmem_recall; navigate the decay tree "
            "with optmem_zoom. Never edit or delete the memory files directly."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._engine is None:
            return ""
        try:
            lines = []
            # OPTION B (matches original `memo wake` semantics): the full decay
            # context is surfaced on the first turn of EACH session, not every
            # turn. Sleeps on the session key, not on process-wide state, so a
            # second conversation in the same process still gets its context.
            key = self._session_key(session_id)
            if key != self._woke_key:
                wake = self._engine.wake_lines(budget=self._wake_budget())
                if wake:
                    lines.append(
                        "## OptMem context (permanent, decay-compressed)\n" + "\n".join(wake)
                    )
                self._woke_key = key
            # Pending nap is always shown (mandatory pressure, like the CLI).
            nap = self._engine.next_nap()
            if nap:
                (lo, hi), prompt = nap
                lines.append(
                    f"[OptMem] Compression due for displayed range #{lo}-{hi - 1}. "
                    f"Call optmem_nap(lo={lo}, hi={hi}, summary=...). The hi argument "
                    "is EXCLUSIVE; max 280 bytes:\n"
                    f"{prompt}"
                )
            # Query-scoped recall only (no wake re-injection on later turns).
            # A user sentence goes through natural-language retrieval; only an
            # explicit recall_mode='regex' config compiles it as a pattern.
            if query:
                requested = self._config.recall_mode
                mode_used = self._engine.plan_recall(query, requested)
                results = self._engine.recall(query, topk=5, mode=requested)
                if results:
                    body = "\n".join(
                        f"- #{mid} {date} {text}" for score, mid, date, text in results
                    )
                    lines.append(f"## OptMem recall ({mode_used})\n" + body)
            return "\n\n".join(lines)
        except Exception as e:
            logger.debug("OptMem prefetch failed: %s", e)
            return ""

    # -- writes -------------------------------------------------------------

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None) -> None:
        # OptMem stores explicit facts via optmem_note, not auto-sync.
        pass

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        """Mirror builtin memory writes into the permanent OptMem log.

        Only mirrors PRIMARY-context writes (the agent's own working session),
        never cron or subagent writes — the official OptMem rule is that a
        subagent must never run memo, because it cannot judge what is already
        known and would duplicate/garble memories. Long content is NOT
        auto-split; if it exceeds 280 bytes engine.append raises and the
        agent is expected to store smaller, atomic facts (matches memo).
        """
        if (
            action != "add"
            or self._engine is None
            or not content
            or self._agent_context != "primary"
        ):
            return
        ctx = metadata or {}
        origin = str(ctx.get("execution_context") or ctx.get("write_origin") or "")
        if origin in ("cron", "subagent", "delegate", "background"):
            return
        try:
            self._engine.append(content.strip())
        except Exception as e:
            logger.warning("OptMem mirror rejected (>280B or invalid): %s", e)

    # -- tools --------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if self._agent_context != "primary":
            return []
        return [
            NOTE_SCHEMA,
            RECALL_SCHEMA,
            NAP_SCHEMA,
            WAKE_SCHEMA,
            ZOOM_SCHEMA,
            FORGET_SCHEMA,
            CONFIG_SCHEMA,
            IMPORT_SCHEMA,
            INIT_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if self._agent_context != "primary":
            return tool_error("OptMem tools are restricted to the primary agent")
        if tool_name == "optmem_note":
            return self._handle_note(args)
        if tool_name == "optmem_recall":
            return self._handle_recall(args)
        if tool_name == "optmem_nap":
            return self._handle_nap(args)
        if tool_name == "optmem_wake":
            return self._handle_wake(args)
        if tool_name == "optmem_zoom":
            return self._handle_zoom(args)
        if tool_name == "optmem_forget":
            return self._handle_forget(args)
        if tool_name == "optmem_config":
            return self._handle_config(args)
        if tool_name == "optmem_import":
            return self._handle_import(args)
        if tool_name == "optmem_init":
            return self._handle_init(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def _handle_note(self, args: dict) -> str:
        try:
            text = args["text"].strip()
            if not text:
                return tool_error("empty memory")
            mid = self._engine.append(text)  # raises ValueError if >280B
            out = {"saved_as": f"#{mid}", "status": "added"}
            nap = self._engine.next_nap()
            if nap:
                (lo, hi), _ = nap
                out["nap_due"] = {"lo": lo, "hi": hi}
                out["note"] = (
                    "Run optmem_nap for this block before your next action; hi is EXCLUSIVE."
                )
            return _json(out)
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_recall(self, args: dict) -> str:
        try:
            query = args["query"]
            topk = int(args.get("topk", 5))
            requested = args.get("mode") or (
                "bm25" if bool(args.get("bm25", False)) else self._config.recall_mode
            )
            # plan_recall validates the mode and raises an actionable error for an
            # explicitly-requested invalid regex (instead of a bare re.error).
            mode_used = self._engine.plan_recall(query, requested)
            hits = self._engine.recall(query, topk=topk, mode=requested)
            if not hits:
                return _json({"results": [], "count": 0, "mode_used": mode_used})
            results = [
                {"score": round(s, 2), "id": mid, "date": date, "text": text}
                for s, mid, date, text in hits
            ]
            return _json({"results": results, "count": len(results), "mode_used": mode_used})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_nap(self, args: dict) -> str:
        try:
            lo = int(args["lo"])
            hi = int(args["hi"])
            summary = args["summary"].strip()
            # The public contract is [lo, hi), but older prompts and schemas
            # described the displayed range (#8-9) as if hi were inclusive.
            # Accept that unambiguous legacy shape so stale callers do not
            # repeatedly fail with a misleading race message.
            if self._validate_block(lo, hi):
                legacy_hi = hi + 1
                if self._validate_block(lo, legacy_hi) is None:
                    hi = legacy_hi
                else:
                    return tool_error(self._validate_block(lo, hi))
            ok = self._engine.apply_nap(lo, hi, summary)
            if not ok:
                return tool_error(
                    f"no writable summary slot for #{lo}-{hi - 1} "
                    f"(hi={hi} exclusive); refresh optmem_wake because another "
                    "nap may have settled or forgotten it."
                )
            return _json({"status": "compressed", "block": f"{lo}-{hi - 1}", "hi_exclusive": hi})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_wake(self, args: dict) -> str:
        try:
            lines = self._engine.wake_lines()
            if not lines:
                return _json({"context": [], "note": "OptMem empty."})
            return _json({"context": lines, "count": len(lines)})
        except Exception as exc:
            return tool_error(str(exc))

    def _validate_block(self, lo: int, hi: int) -> str | None:
        """Return an error string if (lo,hi) is not a valid aligned power-of-two
        block id (mirrors memo's block_id check). hi is EXCLUSIVE."""
        n = hi - lo
        if n < 2 or (n & (n - 1)) or (lo % n):
            return f"{lo}-{hi - 1} is not a block. Copy the id printed by optmem_wake, like 16-31."
        return None

    def _handle_zoom(self, args: dict) -> str:
        try:
            lo = int(args["lo"])
            hi = int(args["hi"])
            err = self._validate_block(lo, hi)
            if err:
                return tool_error(err)
            size = hi - lo
            if size <= RAW_MAX:
                body = self._engine._log_slice(lo, hi)
                out = [f"#{e[0]} {e[1]} {e[2]}" for e in body]
            else:
                mid = (lo + hi) // 2
                out = []
                for a, b in ((lo, mid), (mid, hi)):
                    s = self._engine._tree_get(a, b)
                    if s is None:
                        s = "(missing - rebuild via optmem_nap)"
                    out.append(f"#{a}-{b - 1} {s}")
            return _json({"block": f"{lo}-{hi - 1}", "halves": out})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_forget(self, args: dict) -> str:
        try:
            lo = int(args["lo"])
            hi = int(args["hi"])
            err = self._validate_block(lo, hi)
            if err:
                return tool_error(err)
            self._engine.forget(lo, hi)
            return _json({"status": "forgotten", "block": f"{lo}-{hi - 1}"})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_config(self, args: dict) -> str:
        try:
            changes = args.get("changes") or []
            over = self._engine.read_config()
            for c in changes:
                k, eq, v = str(c).partition("=")
                k = k.strip().upper()
                if not eq or k not in self._engine.KNOBS:
                    allowed = ", ".join(self._engine.KNOBS)
                    return tool_error(f"invalid knob {c!r}; allowed: {allowed}")
                over[k] = int(v.strip())
            if changes:
                self._engine.write_config(over)
            rows = []
            for k, (default, what) in self._engine.KNOBS.items():
                cur = over.get(k, default)
                rows.append({"name": k, "value": cur, "default": default, "what": what})
            return _json({"config": rows, "changed": bool(changes)})
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_import(self, args: dict) -> str:
        try:
            path = args["file"]
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            added = self._engine.import_lines(lines)
            return _json({"status": "imported", "count": added})
        except (KeyError, ValueError, FileNotFoundError, UnicodeDecodeError) as exc:
            return tool_error(str(exc))
        except Exception as exc:
            return tool_error(str(exc))

    def _handle_init(self, args: dict) -> str:
        try:
            # Resolve the memory dir (mirror initialize) so init works even
            # before the provider has been wired into a session.
            home = _get_hermes_home()
            mem_dir = self._config.memory_dir or f"{home}/optmem_memory"
            if isinstance(mem_dir, str):
                mem_dir = mem_dir.replace("$HERMES_HOME", home).replace("${HERMES_HOME}", home)
            import os

            fresh = not os.path.exists(os.path.join(mem_dir, "LOG.txt"))
            eng = self._engine or OptMemEngine(mem_dir)
            eng.init_store()
            self._memory_dir = mem_dir
            self._engine = eng
            return _json({"status": "initialized", "fresh": fresh, "memory_dir": mem_dir})
        except Exception as exc:
            return tool_error(str(exc))

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Background decay-tree maintenance: drain pending naps incrementally.

        Default path is a deterministic, LLM-free extractive summary (zero
        token cost, works in any environment incl. CI/standalone). If LLM
        summarization is explicitly opted in (OPTMEM_LLM_SUMMARY=1 or config
        ``llm_summary: true``) and a host LLM is available, it is used for a
        more fluent summary. Runs every ~10 turns to bound cost. Never touches
        the prompt cache (writes to disk only) and never crashes the turn.
        """
        if self._engine is None or not self._config.auto_nap or self._agent_context != "primary":
            return
        if turn_number % 10 != 0:
            return
        try:
            next_nap = self._engine.next_nap()
            if not next_nap:
                return
            (lo, hi), _prompt = next_nap
            lines = self._engine.block_lines(lo, hi)
            if not lines:
                return

            # Decide summarizer: LLM (opt-in) else local deterministic.
            llm = None
            if self._config.llm_summary or _use_llm_summary():
                llm = (
                    kwargs.get("llm")
                    or getattr(self, "_ctx", None)
                    and getattr(self._ctx, "llm", None)
                )

            if llm:
                summary_prompt = (
                    "You are the OptMem auto-nap assistant. "
                    "Summarize the following memories into ONE line (<=280 bytes). "
                    "Keep what has lasting effect, drop what does not. Invent nothing.\n\n"
                    + "\n".join(f"- {ln}" for ln in lines)
                )
                resp = llm(summary_prompt, max_tokens=120, temperature=0.1)
                summary = resp.strip() if isinstance(resp, str) else str(resp).strip()
                if len(summary.encode("utf-8")) > ENTRY_CHARS:
                    return
            else:
                summary = _local_summary(lines)
                if not summary:
                    # Nothing durable in this block — skip rather than lose data.
                    return

            self._engine.apply_nap(lo, hi, summary)
        except Exception:
            # Never crash the turn on auto-nap failure.
            pass


def _local_summary(lines: list[str]) -> str:
    """Deterministic, LLM-free extractive summary of memory lines.

    Mirrors the original ``memo`` CLI spirit: no generation, just distillation.
    Keeps lines/fragments that look like durable facts (dates, names, decisions,
    approvals, budgets) and drops ephemeral chatter, fitting the result into
    ENTRY_CHARS bytes. Returns "" if nothing durable is found (caller then
    skips the nap rather than lose data).
    """
    if not lines:
        return ""
    # Keywords that signal a durable fact worth keeping.
    durable = (
        "aprov",
        "decid",
        "orçament",
        "orcament",
        "budget",
        "deploy",
        "launch",
        "inici",
        "start",
        "complet",
        "done",
        "shipped",
        "client",
        "contrat",
        "reun",
        "meet",
        "agend",
        "scheduled",
        "monitor",
        "churn",
        "kpi",
        "kr ",
        "objective",
        "goal",
        "paywall",
        "gtm",
        "growth",
        "onboard",
        "staging",
        "prod",
        "release",
        "fix",
        "bug",
        "feature",
        "approved",
        "decision",
        "replaced",
        "supersed",
        "preference",
        "policy",
    )
    scored: list[tuple[int, str]] = []
    for ln in lines:
        low = ln.lower()
        score = sum(1 for k in durable if k in low)
        # Prefer lines that open with a date (YYYY-MM-DD) — those are canonical.
        if len(ln) >= 10 and ln[0:4].isdigit() and ln[4] == "-":
            score += 2
        scored.append((score, ln.strip()))

    # Extraction remains lossy and cannot resolve semantic contradictions.
    # Reserve room for the newest line so an older high-scoring approval does
    # not crowd out a later correction. Prefer newer entries on score ties.
    if not any(score > 0 for score, _ in scored):
        return ""
    latest = scored[-1][1].encode("utf-8")[:ENTRY_CHARS].decode("utf-8", "ignore").rstrip()
    parts = [latest] if latest else []
    total = len(latest.encode("utf-8"))
    ranked = sorted(enumerate(scored[:-1]), key=lambda item: (item[1][0], item[0]), reverse=True)
    for _, (score, line) in ranked:
        if score <= 0 or not line or line in parts:
            continue
        cost = len(line.encode("utf-8")) + (3 if parts else 0)
        if total + cost <= ENTRY_CHARS:
            parts.append(line)
            total += cost
    return " | ".join(parts)


def _use_llm_summary() -> bool:
    """Opt-in LLM summarization: only when explicitly enabled via config/env.

    Default is the local deterministic summarizer (zero token cost, works
    everywhere). Set OPTMEM_LLM_SUMMARY=1 or provider config llm_summary: true
    to let the host LLM write a more fluent summary when available.
    """
    import os

    if os.environ.get("OPTMEM_LLM_SUMMARY") == "1":
        return True
    return bool((_load_plugin_config() or {}).get("llm_summary", False))


# ---------------------------------------------------------------------------
# Host entry points
# ---------------------------------------------------------------------------
#
# ``plugins/memory/__init__.py`` loads an out-of-tree memory provider by calling
# its ``register(ctx)`` with a context exposing ``register_memory_provider``
# (see also the merged ``entropicmem`` catalog entry). The provider is built
# with NO config: ``initialize()`` re-resolves the real profile's configuration,
# so anything read at registration time would belong to whichever profile the
# loader happened to run under.
#
# Register the provider ONLY. Its nine tools and its ``on_memory_write`` /
# ``on_turn_start`` hooks are exposed through the ``MemoryProvider`` interface,
# which the host's ``MemoryManager`` wires directly (``get_tool_schemas`` for
# tools, a per-provider fan-out for the hooks). Calling ``ctx.register_tool`` /
# ``ctx.register_hook`` here would register that surface a second time — the
# tools would appear twice to the model, and the hook names are not part of the
# host's ``VALID_HOOKS`` dispatch set. So there is deliberately no such call.


def register_memory_provider(ctx) -> None:
    """Memory-provider discovery entry point (host: ``plugins/memory``)."""
    ctx.register_memory_provider(OptMemProvider())


def register(ctx) -> None:
    """Plugin entry point the host's discovery calls with its plugin context."""
    register_memory_provider(ctx)
