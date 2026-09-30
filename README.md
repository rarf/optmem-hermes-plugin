# OptMem — permanent local memory for Hermes Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](pyproject.toml)
[![Hermes](https://img.shields.io/badge/Hermes%20Agent-memory%20provider-8A2BE2.svg)](https://github.com/NousResearch/hermes-agent)
[![CI](https://github.com/rarf/optmem-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/rarf/optmem-hermes-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/rarf/optmem-hermes-plugin?label=version)](https://github.com/rarf/optmem-hermes-plugin/releases)

**Append-only, local, LLM-free memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**
OptMem is a `MemoryProvider` that keeps durable facts in a fixed-width log on
your disk, compresses old context with a binary decay tree, and searches it
locally with regex or BM25. No network, no API key, no LLM call to store,
retrieve or compress.

> **An independent Hermes integration — not Victor Taelin's project.** The
> memory model and on-disk format come from Victor Taelin's upstream
> [`OptMem`](https://github.com/VictorTaelin/OptMem) (`memo`). *This* repository
> is an independent reimplementation of that published design as a Hermes
> memory provider — it does not copy upstream source and is not affiliated with
> or endorsed by upstream. Upstream ships no `LICENSE` file, so check upstream
> for its current terms before reusing its code or media; this repo is MIT (see
> [LICENSE](LICENSE)).

---

## What OptMem does — and what it does not claim

- **Append-only log.** `LOG.txt` holds one fixed 320-byte record per memory.
  Records are never rewritten or deleted.
- **The log grows; the injected context does not.** Every `optmem_note` adds one
  record. The decay tree ("nap, don't sleep") merges blocks of records into
  summaries of at most 280 bytes, so the context injected at session start stays
  bounded even as the log grows. The log file itself grows linearly with the
  number of memories — that is the price of never losing one.
- **Compaction is lossy for detail.** A summary can omit facts that were in its
  block — the raw `LOG.txt` records survive so the detail stays recoverable via
  `optmem_zoom`, but the summary itself is not a lossless condensation, and the
  raw records surviving does not mean the summary kept every fact.
- **No semantic conflict resolution.** A later note does not invalidate an
  earlier one: "policy X was replaced by policy Y" is two records and the model
  must read both and judge which is current. There is no automatic supersession.
- **`forget` drops summaries, not raw memories.** It truncates the decay-tree
  entries for a block (and larger blocks built on it); `LOG.txt` is untouched
  and the next nap rebuilds a summary.
- **No LLM or API tokens to store, retrieve or compress — but the wake digest
  does spend model context.** Storing, retrieving and compressing are local and
  LLM-free. The one place context is spent is the *wake* digest: on the first
  turn of each session the provider injects the decayed context into the prompt
  (`wake_budget`, default 96 lines). That is a reading budget, not a storage
  cap, and it consumes model context tokens — "bounded" means the injected
  context stays roughly constant as the log grows, not that it is free.
- **Local-only.** No network calls, no credentials. The store lives in your
  `HERMES_HOME`.

---

## Quickstart

### 1. Install (from a pinned source commit)

> **0.3.0 is unreleased.** There is no `v0.3.0` tag and no PyPI artifact yet, so
> `pip install "optmem-hermes-plugin==0.3.0"` and `git clone --branch v0.3.0`
> fail. Install the reviewed source at an exact commit instead.
>
> **Not in the Hermes plugin catalog.** OptMem has no entry in the Hermes plugin
> catalog yet, so `hermes plugins install optmem` is not a working install path
> today. The catalog is a reviewed, SHA-pinned listing; until this plugin has an
> entry, use the pinned-source install below.

```bash
# from an exact reviewed commit (replace with the full 40-character SHA)
pip install "git+https://github.com/rarf/optmem-hermes-plugin@<FULL-40-CHAR-COMMIT-SHA>"

# ...or from a pinned source checkout
git clone https://github.com/rarf/optmem-hermes-plugin.git
cd optmem-hermes-plugin
git checkout <FULL-40-CHAR-COMMIT-SHA>
pip install .

# latest published release (0.2.0), for the previous provider
pip install "optmem-hermes-plugin==0.2.0"
```

### 2. Activate it

Set the provider in the profile's `config.yaml` and restart:

```yaml
memory:
  provider: optmem-hermes
```

```bash
hermes gateway restart
```

### Upgrading an older profile

The canonical provider name is now `optmem-hermes`, distinct from the upstream
project. For a profile previously configured with `memory.provider: optmem`,
change only that key to `optmem-hermes` after installing this version, preserve
its existing data paths and native-memory flags, and restart that profile.
The Python package and OptMem data paths remain unchanged. The top-level CLI
command changes from `hermes optmem` to `hermes optmem-hermes`.
Do not apply this change to other profiles automatically.

### 3. Verify before you switch (read-only)

These commands create and modify nothing — run them first:

```bash
hermes optmem-hermes status          # mode, store path, entry count, pending naps, readiness
hermes optmem-hermes show            # effective config and where each value came from
hermes optmem-hermes check           # is the store ready to replace the native one? (exit 1 if not)
```

`status` starts in **hybrid** mode, where the built-in `MEMORY.md`/`USER.md`
store keeps working unchanged. Switching to **optmem-only** is gated on a
verified migration — see [Modes](#modes).

### Profile plugin copies

Hermes profiles keep independent plugin copies, so a copied (non-`pip`) install
must be updated in **only the profile(s) you explicitly select** — never as a
blanket update of every profile on the machine. Target one profile explicitly:

```bash
hermes optmem-hermes status --hermes-home "$HERMES_HOME/profiles/<profile>"
```

then update the copy under that profile's `plugins/optmem-hermes/` and restart that
profile's long-running Hermes/Desktop/gateway process — Python keeps
already-imported modules in memory. Other profiles are left untouched.

### Rollback

- `hermes optmem-hermes rollback` restores the `config.yaml` saved before the last mode
  switch, byte-for-byte, and the previous declared mode. OptMem data is untouched.
- The raw native files are copied before any migration or mode change into
  `<HERMES_HOME>/optmem_backups/native-<stamp>/` with a sha256 manifest.
- To roll back the *code*, reinstall the previous pinned version
  (`pip install "optmem-hermes-plugin==0.2.0"`) and re-sync only the copied
  plugin directory for the profile you are targeting.

---

## See the original design, in motion

The animation below is **Victor Taelin's original** `OptMem` explainer. It walks
through the upstream `memo` design: an append-only log that never deletes a
memory, on-the-spot "nap, don't sleep" compression into a binary merge tree, and
a memory context that stays constant-sized while details fade with age into
higher-level summaries that can be zoomed back down. It explains the **upstream
design that this plugin reimplements — not this Hermes plugin itself.**

It is linked from upstream by its raw URL, **pinned to a verified upstream
commit**, with attribution — it is **not copied or rehosted** in this repository:

![Victor Taelin's original OptMem design animation](https://raw.githubusercontent.com/VictorTaelin/OptMem/1fb164cf39028047781f72ac3bb1e5a691c1dcb0/anim/optmem.gif)

Source: [VictorTaelin/OptMem](https://github.com/VictorTaelin/OptMem) —
[`anim/optmem.gif`](https://github.com/VictorTaelin/OptMem/blob/main/anim/optmem.gif)
at upstream commit
[`1fb164cf`](https://github.com/VictorTaelin/OptMem/commit/1fb164cf39028047781f72ac3bb1e5a691c1dcb0).
Upstream ships no `LICENSE` file — check upstream for its current terms before
reusing its media.

For how *this* Hermes integration behaves, see the sections below.

---

## Modes

- **hybrid** (default) — OptMem runs alongside the built-in `MEMORY.md`/`USER.md`
  store. Nothing about the built-in store changes.
- **optmem-only** — the built-in store is switched off
  (`memory.memory_enabled: false` and `memory.user_profile_enabled: false`) so
  only OptMem is active. The switch is gated: `hermes optmem-hermes mode optmem-only
  --yes` applies it only when the migration is verified (every native entry
  present in the store) and a native backup exists. Without `--yes` the command
  refuses and writes nothing.

`hermes optmem-hermes mode hybrid --yes` re-enables the built-in store; OptMem data is
kept.

---

## Configuring

Two supported surfaces expose the same keys.

### `hermes optmem-hermes <action>`

Scriptable, with `--json` for machine output and `--hermes-home PATH` to target
one profile explicitly.

| Action | What it does |
|---|---|
| `status` | Mode, store path, entry count, pending compressions, native-store flags, readiness |
| `show` | Effective configuration and where each value came from (declared / legacy / defaults) |
| `check` | Is the store ready to replace the native one? Read-only; exit code 1 when not ready |
| `migrate` | Back up `MEMORY.md`/`USER.md` and import them. Idempotent. `--dry-run` plans only, `--split` splits over-long entries on safe boundaries |
| `mode` | Switch `hybrid` / `optmem-only` (`--yes` required to write) |
| `import` | Import a curated `YYYY-MM-DD <text>` file (dedupes by default; `--no-dedupe` to append) |
| `rollback` | Undo the last mode switch |
| `version` | Installed plugin version |

`status`, `show` and `check` never create or modify anything.

### Desktop / dashboard panel

The declared schema (`optmem/config_schema.py`, stored as
`<HERMES_HOME>/optmem/config.json`) is rendered by Hermes' generic
memory-provider config panel. It exposes the same keys: `mode`, `memory_dir`,
`wake_budget`, `recall_mode`, `auto_nap`, `llm_summary`,
`migration_split_long`. Changing `mode` in the panel only records the intent —
the switch itself is gated on migration, so it is applied by the CLI.
`llm_summary` is reserved and has no effect yet (see Auto-compaction).

**Precedence:** declared `config.json` → legacy `memory.optmem` /
`plugins.optmem` keys in `config.yaml` → built-in defaults. An unknown mode
resolves to `hybrid` (the built-in store keeps working) and is reported as a
diagnostic.

---

## Tools exposed to the agent

| Tool | Purpose |
|---|---|
| `optmem_note` | Record one durable memory line (≤280 bytes). |
| `optmem_recall` | Search all history — `auto` by default (regex for pattern-like queries, token/BM25 for prose); `regex` forces `memo` parity, `bm25` is ranked, accent-tolerant search. |
| `optmem_wake` | Print the current decayed context. |
| `optmem_nap` | Apply a compression the engine requested. |
| `optmem_zoom` | Walk the decay tree back down to raw records. |
| `optmem_forget` | Drop a summary so the next nap rebuilds it (raw records stay). |
| `optmem_config` | Show or change size knobs. |
| `optmem_import` | Bulk-load historical `YYYY-MM-DD <text>` memories. |
| `optmem_init` | Create the store deliberately. |

### Recall modes

- **auto** (the default `recall_mode`, and the default for `optmem_recall`) —
  regex for pattern-like queries, token/BM25 for natural-language sentences; an
  invalid pattern is never compiled blindly.
- **regex** (the engine API default, and `memo` parity) — case-insensitive regex
  over `#id date text`, newest matches first.
- **bm25** — accent-normalized ranked search (`cacula` finds `caçula`).
- **token** — BM25 plus a literal substring fallback so a rare identifier BM25
  cannot rank is still found.

### Auto-compaction

`on_turn_start` drains pending naps every ~10 turns with a deterministic,
LLM-free extractive summarizer; a block with no durable signal is left raw
rather than losing it. That extractor is lossy for detail — a summary can omit
facts from its block, and the raw `LOG.txt` records surviving does **not** mean
the summary kept them. `auto_nap: false` disables automatic compaction.

`llm_summary` is **reserved and currently has no effect**: no automatic host-LLM
summarization path is wired up, so compaction is always the local extractor. The
setting is accepted (declared config, the legacy `config.yaml` key and
`OPTMEM_LLM_SUMMARY=1`) so a future release can use it without a config change,
but enabling it today changes nothing.

---

## Format compatibility with upstream `memo`

The on-disk constants mirror the published upstream format: `LOG_REC = 320`,
`TREE_REC = 288`, `RAW_MAX = 16`, one entry of at most 280 UTF-8 bytes, and
native entries joined by `"\n§\n"`. `scripts/sync_upstream.sh` fetches upstream's
`memo`, compares those constants, and exits non-zero when they drift — that
script is the compatibility check. This repository's test suite does not run the
upstream CLI.

---

## Examples

```bash
python examples/standalone_demo.py
```

Runs the full lifecycle without Hermes (temp store, no network): `note` →
`recall` (regex + accent-tolerant BM25) → auto-compaction (deterministic,
LLM-free) → `wake`.

---

## Tests and CI

```bash
pip install -e .[dev]
pytest tests/
```

The suite runs against the real engine and provider (temp `HERMES_HOME`, no
mocks): append, regex/BM25/token recall, accent normalization, nap/decay
compression, byte-compat reopen, tool roundtrip, wake-once-per-session,
`on_memory_write`, `on_turn_start` auto-compaction, the config resolver and
declared schema, migration/backup/mode switching, and the `hermes optmem-hermes` CLI.
CI runs it on Linux (Python 3.11/3.12) and Windows (3.11), and lints with ruff.

---

## How this differs from other memory backends

Rather than assert internals of other products, here are OptMem's own
trade-offs:

- The **built-in Hermes store** (`MEMORY.md`/`USER.md`) is free-form and part of
  Hermes core; OptMem is opt-in, append-only, limited to one atomic fact per
  line, and separately searchable. Hybrid mode runs both at once.
- **Cloud / LLM-backed memory providers** (for example Honcho) require their own
  configuration and credentials, and may send data off the machine; OptMem needs
  neither and never leaves the disk. See each provider's own documentation for
  its behaviour and costs.
- OptMem trades free-form editing and automatic conflict resolution for a
  permanent, append-only, locally searchable store.

---

## Credits

- Memory model and on-disk format by **Victor Taelin** —
  [VictorTaelin/OptMem](https://github.com/VictorTaelin/OptMem). The original
  design animation is his work, linked (not rehosted) above.
- Standalone Hermes integration, Windows locking, BM25/token search, the
  migration/mode tooling and CLI parity by the project contributors.

## License

MIT — see [LICENSE](LICENSE).
