"""OptMem's declared config surface — rendered by the generic Hermes panel.

Loaded BY PATH by the host (``plugins/memory/config_schema.py``) and therefore
allowed to import only that pure-data module. It must not import
``optmem/__init__.py`` (which pulls the agent runtime) — so the field list is
declared here literally and pinned against ``optmem.config.EDITABLE_KEYS`` by a
test, because this file cannot import the resolver.

Storage is ``flat_json``: the host reads/writes
``<HERMES_HOME>/<name>/config.json``, which is exactly what
``optmem.config.declared_config_path`` resolves.

Standalone (no Hermes on the path) a minimal same-shape shim keeps the module
importable for the plugin's own tests; the host always gets the real dataclasses.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_KIND_TEXT = "text"
_KIND_SELECT = "select"
_KIND_BOOL = "bool"
_KIND_NUMBER = "number"
_KIND_SECRET = "secret"
_STORAGE_FLAT_JSON = "flat_json"

try:  # pragma: no cover - the host branch is what ships
    from plugins.memory.config_schema import (
        KIND_BOOL,
        KIND_NUMBER,
        KIND_SELECT,
        KIND_TEXT,
        STORAGE_FLAT_JSON,
        ProviderConfigSchema,
        ProviderField,
        ProviderFieldOption,
    )
except Exception:  # pragma: no cover - bare environment (plugin unit tests/CI)
    # Deliberately plain classes, not dataclasses: the host loads this file with
    # ``module_from_spec`` + ``exec_module`` WITHOUT inserting it into
    # ``sys.modules``, and a by-path dataclass whose annotations are strings then
    # dies in ``dataclasses._is_type`` (it looks the module up in sys.modules).
    # The shim mirrors the host dataclass' public attributes so a degraded load
    # still renders and reads/writes the same panel.
    KIND_TEXT, KIND_SELECT, KIND_BOOL, KIND_NUMBER = (
        _KIND_TEXT,
        _KIND_SELECT,
        _KIND_BOOL,
        _KIND_NUMBER,
    )
    KIND_SECRET = _KIND_SECRET
    STORAGE_FLAT_JSON = _STORAGE_FLAT_JSON

    class ProviderFieldOption:  # type: ignore[no-redef]
        __slots__ = ("value", "label", "description")

        def __init__(self, value: str, label: str, description: str = "") -> None:
            self.value, self.label, self.description = value, label, description

    class ProviderField:  # type: ignore[no-redef]
        __slots__ = (
            "key",
            "label",
            "kind",
            "default",
            "description",
            "placeholder",
            "options",
            "env_key",
            "aliases",
            "env_fallbacks",
            "inline",
            "group",
            "info",
            "scope",
        )

        def __init__(
            self,
            key,
            label,
            kind=KIND_TEXT,
            default="",
            description="",
            placeholder="",
            options=(),
            env_key=None,
            aliases=(),
            env_fallbacks=(),
            inline=False,
            group="",
            info="",
            scope="host",
        ) -> None:
            self.key, self.label, self.kind = key, label, kind
            self.default, self.description, self.placeholder = default, description, placeholder
            self.options, self.env_key, self.aliases = tuple(options), env_key, tuple(aliases)
            self.env_fallbacks, self.inline, self.group = tuple(env_fallbacks), inline, group
            self.info, self.scope = info, scope

        @property
        def is_secret(self) -> bool:
            return self.kind == KIND_SECRET

        def allowed_values(self) -> set:
            return {opt.value for opt in self.options}

    class ProviderConfigSchema:  # type: ignore[no-redef]
        __slots__ = ("name", "label", "storage", "docs_url", "fields")

        def __init__(self, name, label, storage=STORAGE_FLAT_JSON, docs_url="", fields=()) -> None:
            self.name, self.label, self.storage = name, label, storage
            self.docs_url, self.fields = docs_url, tuple(fields)

        def inline_fields(self):
            return tuple(f for f in self.fields if f.inline)


def _opts(*pairs: tuple[str, str]) -> tuple:
    return tuple(ProviderFieldOption(value, label) for value, label in pairs)


_WAKE_INFO = (
    "Lines of decayed context printed on the first turn of EACH session (default 96, "
    "the memo default). This is a reading budget, not a storage cap: it costs model "
    "context tokens on the first turn after a session starts, /new or a compression."
)
_MODE_INFO = (
    "Hybrid: OptMem runs alongside the built-in MEMORY.md/USER.md store; nothing about "
    "it changes. OptMem-only: the built-in store is switched off (memory_enabled and "
    "user_profile_enabled both false) so only OptMem is active. OptMem-only requires a "
    "verified migration + native backup and is applied by `hermes optmem mode optmem-only`; "
    "changing the value here only records the intent."
)
_RECALL_INFO = (
    "auto: keep the original `memo recall` regex for pattern-like queries and route a "
    "natural-language sentence to accent-normalized token search. regex: exact `memo` "
    "parity (an invalid pattern is reported, never compiled blindly). bm25: ranked fuzzy "
    "search."
)
_NAP_INFO = (
    "Deterministic, LLM-free auto-compaction every ~10 turns: pending decay blocks are "
    "merged with an extractive summary (zero token cost, works offline). A block with no "
    "durable signal is left raw rather than losing data."
)
_LLM_INFO = (
    "Reserved — not yet wired. Compaction always uses the deterministic, LLM-free "
    "extractive summarizer; there is no automatic host-LLM path today, so this setting "
    "currently has no effect. That extractor is lossy for detail: a summary can omit "
    "facts from its block even though the raw LOG.txt records survive and optmem_zoom "
    "walks back down to them."
)
_SPLIT_INFO = (
    "Off by default. When a native MEMORY.md/USER.md entry exceeds 280 UTF-8 bytes the "
    "migration STOPS with an actionable error instead of guessing. Enabling this splits "
    "such entries on sentence/semicolon boundaries; anything still over the limit keeps "
    "the migration blocked. Entries are never truncated or reworded."
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="optmem",
    label="OptMem",
    storage=STORAGE_FLAT_JSON,
    docs_url="https://github.com/rarf/optmem-hermes-plugin#configuration",
    fields=(
        ProviderField(
            key="mode",
            label="Mode",
            kind=KIND_SELECT,
            description="Hybrid keeps the built-in store; OptMem-only disables it after migration.",
            info=_MODE_INFO,
            default="hybrid",
            inline=True,
            group="Mode",
            options=_opts(("hybrid", "Hybrid"), ("optmem-only", "OptMem only")),
        ),
        ProviderField(
            key="memory_dir",
            label="Store directory",
            kind=KIND_TEXT,
            description="Directory holding LOG.txt + TREE/ (default: $HERMES_HOME/optmem_memory).",
            placeholder="$HERMES_HOME/optmem_memory",
            inline=True,
            group="Store",
        ),
        ProviderField(
            key="wake_budget",
            label="Wake context lines",
            kind=KIND_NUMBER,
            description="Lines of decayed context injected on the first turn of each session.",
            info=_WAKE_INFO,
            default="96",
            placeholder="96",
            inline=True,
            group="Context",
        ),
        ProviderField(
            key="recall_mode",
            label="Recall mode",
            kind=KIND_SELECT,
            description="How queries are retrieved: auto, regex (memo parity) or BM25.",
            info=_RECALL_INFO,
            default="auto",
            inline=True,
            group="Retrieval",
            options=_opts(
                ("auto", "Auto"), ("regex", "Regex (memo parity)"), ("bm25", "BM25 ranked")
            ),
        ),
        ProviderField(
            key="auto_nap",
            label="Auto-compaction",
            kind=KIND_BOOL,
            description="Drain pending decay blocks automatically every ~10 turns.",
            info=_NAP_INFO,
            default="true",
            inline=True,
            group="Compaction",
        ),
        ProviderField(
            key="llm_summary",
            label="LLM summaries",
            kind=KIND_BOOL,
            description=(
                "Reserved; no effect yet — compaction is always the local LLM-free extractor."
            ),
            info=_LLM_INFO,
            default="false",
            inline=True,
            group="Compaction",
        ),
        ProviderField(
            key="migration_split_long",
            label="Split long entries on migration",
            kind=KIND_BOOL,
            description="Split native entries over 280 bytes on safe boundaries.",
            info=_SPLIT_INFO,
            default="false",
            inline=True,
            group="Migration",
        ),
    ),
)
