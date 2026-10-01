# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `optmem_import` (model tool) now only reads regular files inside
  `<HERMES_HOME>/optmem/imports/` (symlinks resolved, 4 MiB cap). Import
  errors no longer echo the offending line's content. The `import` CLI
  command still accepts any path.
- `mode` switches now write `memory.provider: optmem-hermes` instead of the
  unregistered name `optmem`.

## [0.3.2] - 2026-10-01

### Changed
- Remove the standalone demo, legacy-heavy configuration example and unused
  catalog image variants; retain the final 2:1 catalog card.
- Shorten the READMEs while preserving migration guards, upstream attribution
  and opt-in LLM egress/cost warnings. Add a catalog README parity test.
- Simplify development targets and remove redundant CI compile steps; keep
  Linux, Windows and pinned-host integration coverage.
- Clarify that the upstream sync script checks three constants and local tests,
  not full format or decay-algorithm equivalence.

### Fixed
- Declare the build frontend in the development extra so `make build` works.

Runtime behavior, data format, defaults and legacy configuration readers are
unchanged from 0.3.1.

## [0.3.1] - 2026-10-01

### Changed
- **Manual merges by default.** `auto_nap` now defaults to off, matching upstream
  OptMem (`memo` compacts only when asked). Background drain every ~10 turns is
  an explicit opt-in. Installations that already set `auto_nap: true` are unchanged.
- **`llm_summary` is not a standalone switch.** It only chooses the summarizer
  used by the `auto_nap` path. Network egress and token cost happen only when
  both are true.
- **Wake refuses a hole.** A required summary that is missing makes `wake_lines`
  raise `WakeNeedsCompression` instead of printing a digest with a gap. `wake`
  reports `complete`, `missing` and the next nap prompt. Mirrors upstream
  `memo wake`.
- **Block ids are checked.** `nap`, `zoom` and `forget` reject a range that is
  not an aligned power-of-two block, so `4-5` and `5-6` cannot read the same record.

### Fixed
- Tool descriptions state the 280-byte UTF-8 limit, not a character limit.

## [0.3.0] - 2026-09-30

### Added
- **`hermes optmem-hermes` CLI** (`optmem/cli.py`) — `status`, `show`, `check`,
  `migrate`, `mode`, `import`, `rollback`, `version`, with `--json` and
  `--hermes-home PATH`. Read-only commands create and modify nothing.
- **Hybrid / OptMem-only modes** — `hermes optmem-hermes mode optmem-only --yes` turns
  off the built-in `MEMORY.md`/`USER.md` store only after a verified migration
  (every native entry present in the store) and a native backup. Without `--yes`
  the command refuses and writes nothing. `rollback` restores the previous
  `config.yaml` byte-for-byte and keeps OptMem data.
- **Declared config + GUI panel** (`optmem/config_schema.py`, stored at
  `<HERMES_HOME>/optmem/config.json`) — mode, memory_dir, wake_budget,
  recall_mode, auto_nap, llm_summary, migration_split_long, rendered by Hermes'
  generic memory-provider panel. Precedence: declared → legacy `config.yaml`
  keys → defaults. LLM summaries are opt-in and disabled by default.
- **Migration tooling** (`optmem/migrate.py`) — byte-for-byte native backups
  with a sha256 manifest, an idempotent import that never drops a fact, safe
  splitting of over-long entries (`--split`), and a blocked plan that exits
  non-zero and leaves the native store running.

### Changed
- **Retrieval**: `recall_mode` adds `auto` (regex for pattern-like queries,
  token/BM25 for prose) on top of `regex` (`memo` parity) and `bm25`.
- **Docs**: README rewritten to state only verifiable behaviour — the log grows
  one fixed-width record per memory (the *injected context* is what stays
  bounded), retrieval/default compression are LLM-free while the wake digest does spend
  context tokens, `forget` drops summaries only, and there is no semantic
  conflict resolution. The built-in/Honcho comparison no longer asserts
  unverified internals of other products. SECURITY.md and `config.example.yaml`
  updated with supported versions, optional dependencies, real keys and defaults.
  Installation uses the versioned GitHub release; PyPI availability is not assumed.

### Fixed
- **`hermes optmem version` under the host's by-path load.** The host imports
  `optmem/cli.py` by path under a synthetic package shell that never executes
  `optmem/__init__.py`, so `from . import __version__` raised ImportError and the
  command crashed. It now reads the installed distribution metadata
  (`importlib.metadata`) and falls back to the shipped `plugin.yaml` (stdlib
  parse, no provider import); a stale editable install no longer misreports the
  copy actually loaded.
- **Unreadable native memory files no longer escape the CLI as a traceback.** A
  `MEMORY.md`/`USER.md` that is not valid UTF-8 raises `NativeReadError` (a
  `RuntimeError`); `migrate` (including `--dry-run`), `check` and `mode` now
  emit a JSON error and exit non-zero, leaving the native file byte-for-byte
  unchanged and creating no store. Only `NativeReadError` was added to the
  handler's catch, so other exceptions still surface.
- **Functional opt-in LLM summaries.** Disabled by default; host-owned
  `PluginLlm` routes calls through `auxiliary.optmem_summary`, inheriting the
  configured Hermes model unless explicitly overridden. The version-sensitive
  memory-context bridge preserves host identity and trust gates. Invalid replies
  and unavailable hosts fall back to local extraction. Enabling this sends memory
  blocks to the selected provider and may incur token costs; raw records remain.
- **Clean admission and Doctor checks.** Memory lifecycle methods are not generic
  plugin hooks; the manifest no longer declares unsupported hook names.
- **Background-review isolation.** Non-primary writes do not mirror into memory.

## [0.2.0] - 2026-08-09

### Added
- **Auto-compaction, LLM-free** — `on_turn_start` drains pending naps every
  ~10 turns with a deterministic extractive summarizer (`_local_summary`):
  scores lines by durability keywords + leading date, packs into ≤280 bytes.
  Works in CI/standalone/offline with **zero token cost**.
- **Opt-in LLM summaries** — set `llm_summary: true` (config) or
  `OPTMEM_LLM_SUMMARY=1` (env) to let the host LLM write a more fluent
  summary when available; falls back to local extractor on any failure.
- `engine.block_lines(lo, hi)` — returns raw or compressed lines for a block
  (used by the local summarizer).
- `plugin.yaml` now registers the `on_turn_start` hook.
- Standalone demo (`examples/standalone_demo.py`) — full lifecycle without
  Hermes: note → recall (regex + BM25) → auto-nap → wake.
- Tests expanded 18 → 26: block_lines, local-fallback nap, LLM-opt-in nap,
  ephemeral-only skip, no-engine import, save_config, init idempotent.
- CI: GitHub Actions upgraded to Node 24 runners (`checkout@v7`,
  `setup-python@v7`, `action-gh-release@v3`); `release.yml` decoupled via
  `on: release` (no more empty false-runs).

### Changed
- Provider imports made defensive: loads without Hermes present (CI-clean).
- `wake_lines` no longer duplicates the date prefix in rendered output.
- README: documents auto-compaction, `llm_summary` option, 26 tests, demo.

[0.2.0]: ../../releases/tag/v0.2.0

## [0.1.0] - 2026-08-09

### Added
- Full byte-compatible reimplementation of Victor Taelin's OptMem (LOG.txt + TREE/ decay)
- 9 tools exposed to Hermes: `optmem_note`, `optmem_recall`, `optmem_nap`, `optmem_wake`, `optmem_zoom`, `optmem_forget`, `optmem_config`, `optmem_import`, `optmem_init`
- Native Windows locking (`msvcrt`) + Unix (`fcntl`) — no WSL required
- Two search modes: regex (default, = `memo` CLI behavior) and accent-normalized BM25
- Prefetch with "wake once per session" (Option B): wake injected only on first turn, subsequent turns do recall-only
- Parity with upstream `memo` CLI commands: note, recall, nap, wake, zoom, forget, config, import, init
- Lock file (`.lock`) compatible with original — safe coexistence on same store
- Entry size limit 280 bytes (matches original)
- Decay tree compression ("nap, don't sleep") with configurable `nap_prompt`
- 18 passing tests (engine + provider + integration)
- Comprehensive README with 3-way comparison table (Built-in / Honcho / OptMem)

### Fixed
- Lock file descriptor leak closed (fd closed in `__exit__`)
- `memory_dir` → `dir` alignment across engine
- Import atomicity: validate all lines before any write (matches original behavior)
- BM25 index rebuild only when `mode="bm25"` requested
- Prefetch changed from full wake every turn → wake-once + recall subsequent

### Security
- Rejects entries >280 bytes (matches upstream)
- No network calls, no external dependencies beyond stdlib + PyYAML

[0.1.0]: ../../releases/tag/v0.1.0