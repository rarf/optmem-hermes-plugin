# OptMem — Permanent Local Memory for Hermes Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-blue.svg)](pyproject.toml)
[![Hermes](https://img.shields.io/badge/Hermes%20Agent-memory%20provider-8A2BE2.svg)](https://github.com/NousResearch/hermes-agent)
[![Parity](https://img.shields.io/badge/byte--compatible%20with%20memo%20CLI-✅-green.svg)](https://github.com/VictorTaelin/OptMem)
[![CI](https://github.com/rarf/optmem-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/rarf/optmem-hermes-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/github/v/tag/rarf/optmem-hermes-plugin?label=version)](https://github.com/rarf/optmem-hermes-plugin/releases)

**Permanent, searchable agent memory that never leaves your machine, costs zero
tokens to recall, and is byte-for-byte compatible with Victor Taelin's
`OptMem` `memo` CLI.**

OptMem is a drop-in `MemoryProvider` for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that gives your agent a durable, decaying memory store — the same design
Taelin ships as a CLI, but wired directly into Hermes. No cloud, no API key, no
per-turn LLM cost. Just memory that survives restarts and stays small.

> **License note.** The upstream `VictorTaelin/OptMem` repo currently ships
> **without an explicit license** (all rights reserved by default). This
> repository is an *independent* reimplementation of the published design and
> memory format — it does not copy the upstream source. It is released here
> under MIT (see [LICENSE](LICENSE)). If/once upstream adopts a license, this
> plugin will align with it.

---

## OptMem vs Hermes built-in vs Honcho (cloud)

Three ways to give Hermes memory. Here is how they compare on the things that
actually matter in production.

| Dimension | Hermes built-in (`memory`) | Honcho (cloud) | **OptMem (local)** |
|---|---|---|---|
| Setup | Zero — `memory_enabled: true` | API key + base URL + network | **Zero** — `memory.provider: optmem` |
| Storage | JSONL, one file per session | Cloud DB (vendor-hosted) | Append-only `LOG.txt` + binary decay `TREE/` |
| Entry size limit | None — paragraphs allowed | None | **≤280 bytes** per atomic fact |
| Deletion | Edited/deleted freely | Via API (vendor-controlled) | **Never deleted** — forgotten = rebuilt |
| Growth | Unbounded (log grows forever) | Scales in cloud (costs rise) | **Self-compressing** via decay tree ("nap") |
| Recall | Recent-first + optional semantic | Semantic + LLM-ranking | Regex (default, = `memo`) or accent BM25 |
| Recall latency | Local (recent) / embeddings | Network round-trip, often an LLM call | **Local, sub-ms** |
| Per-call cost | Free recent; embeddings if semantic | **Reasoning-tier LLM every call** | **0 tokens** — local search |
| Cross-session | Per-session files, needs aggregation | Global user model | **One identity**, whole history searchable |
| Data residency | On disk in `HERMES_HOME` | **Leaves the machine to a 3rd party** | On disk in `HERMES_HOME` |
| Offline / private | Yes | No (needs network) | **Yes** |
| Portability | Hermes-only format | Honcho-only | **Byte-compatible** with `memo` CLI |
| Code footprint | Core (no extra install) | 1500+ LOC + SDK + threads | ~850 LOC, stdlib only |

**How to choose:**

- **Built-in** — zero setup, free-form notes, semantic search over recent
  context. Good for short-lived assistants and quick experiments.
- **Honcho (cloud)** — cross-session *user modeling* and rich semantics, but
  needs a key, network, and pays an LLM call on recall.
- **OptMem** — the *durable, always-on default*: a permanent single identity
  that survives restarts without growing unbounded, costs **zero tokens** to
  recall, stays on your disk, and is portable to the `memo` CLI. Great for
  long-running personal agents that need to "remember forever" cheaply.

> OptMem trades *free-form editing* and *cloud user-modeling* for *permanent,
> compressed, portable, token-free* memory. If you need both, run them side by
> side: built-in for scratch notes, Honcho for modeling, OptMem for the durable
> identity.

---

## What you get

- **🔒 Permanent & private** — append-only `LOG.txt` on your disk. Nothing is
  ever deleted; forgotten summaries are rebuilt, never lost.
- **🌳 Self-compressing** — the decay tree ("nap, don't sleep") keeps context
  dense instead of growing unbounded. One line per atomic fact, ≤280 bytes.
- **🔎 Two search modes** — `recall` defaults to the **same regex behavior as
  the `memo` CLI** (case-insensitive, newest-first), with optional
  accent-normalized **BM25 ranking** (`cacula` finds `caçula`).
- **💾 Byte-compatible with `memo`** — same fixed-width 320/288-byte records and
  `.lock` file. Run the CLI and the plugin on the **same store**; they read and
  write each other's memories safely.
- **🪟 Native Windows** — `msvcrt` advisory locks with spin/backoff (no WSL, no
  `Resource deadlock avoided`). `fcntl` on Unix.
- **🧩 Zero-dependency** — pure Python standard library.

## Quick start

```bash
# 1. Install
git clone https://github.com/rarf/optmem-hermes-plugin.git
cp -r optmem-hermes-plugin/optmem ~/.hermes/plugins/optmem
# (or: pip install optmem-hermes-plugin)

# 2. Activate in ~/.hermes/config.yaml
memory:
  provider: optmem

# 3. Restart the gateway
hermes gateway restart
```

That's it. The agent now has permanent memory — no migration, no prompt paste.

### Windows profiles

Hermes profiles keep independent plugin copies. Updating the repository does
not automatically update already-installed profile copies. After pulling a new
plugin version on Windows, sync both Python modules into every profile that
uses OptMem:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/AppData/Local/hermes}"
for profile in default alldrivers-dev alldrivers-devops alldrivers-planner \
               alldrivers-qa alldrivers-seo casa coach; do
  target="$HERMES_HOME/profiles/$profile/plugins/optmem"
  mkdir -p "$target"
  cp optmem/__init__.py optmem/engine.py "$target/"
done
```

Also update the global copy at `$HERMES_HOME/plugins/optmem/` when the default
installation uses it. Restart long-running Hermes/Desktop/gateway processes
after syncing; Python keeps already-imported plugin modules in memory.

---

## Tools exposed to the agent

| Tool | Purpose |
|---|---|
| `optmem_note` | Record one durable memory line (≤280 bytes). |
| `optmem_recall` | Search all history — regex by default (matches `memo recall`), or `mode="bm25"` for ranked/accent-tolerant search. |
| `optmem_wake` | Print the current decayed context (permanent memory). |
| `optmem_nap` | Apply a compression the engine requested. |
| `optmem_zoom` | Navigate the decay tree (halve a block to see its parts). |
| `optmem_forget` | Drop a bad summary so the next nap rebuilds it. |
| `optmem_config` | Show or change size knobs (mirrors `memo config`). |
| `optmem_import` | Bulk-load historical `YYYY-MM-DD <text>` memories (bootstrap). |
| `optmem_init` | Create the store deliberately (mirrors `memo init`). |

All nine mirror the `memo` CLI surface — so scripts and habits transfer 1:1.

---

## Parity with upstream `memo`

The memory model is identical: append-only log, binary decay tree, "nap, don't
sleep" compression, fixed-width record format. Logs are interchangeable on disk.

| Aspect | `VictorTaelin/OptMem` (`memo`) | `optmem-hermes-plugin` |
|---|---|---|
| On-disk format | `LOG_REC=320`, `TREE_REC=288`, `RAW_MAX=16` | **Identical** |
| `recall` | regex only | regex by default (**same behavior**); BM25 opt-in |
| `wake` | printed once per session | surfaced once per session via `prefetch` (Option B) |
| Platform locks | `fcntl` only (Unix) | `msvcrt` on Windows, `fcntl` on Unix |
| Coexist on one machine | — | **Yes** — shared store + shared `.lock` (proven by retro-test) |
| Form factor | CLI you paste into `AGENTS.md` | Hermes `MemoryProvider` — auto-wired, no paste |

In short: **same durable store, full CLI parity, plus native Windows and
first-class Hermes integration.**

---

## How it works

- **Append-only log** (`LOG.txt`, fixed-width 320-byte records).
- **Decay tree** (`TREE/<size>`). When a pair of memories forms, a *nap* merges
  the block into one line — old context compresses instead of growing.
- **Search** — regex (default, matches `memo`) or accent-normalized BM25.
- **Auto-compaction** — `on_turn_start` drains pending naps every ~10 turns
  with a **deterministic, LLM-free** extractive summary (no gateway, no API,
  works in CI/standalone). Opt into fluent LLM summaries with
  `llm_summary: true` or `OPTMEM_LLM_SUMMARY=1`.
- **Portable lock** — `msvcrt` on Windows (`LK_NBLCK` + spin/backoff), `fcntl`
  on Unix. Descriptors are closed on release (no fd leak).

Related upstream fix (Windows `fcntl` → `msvcrt`):
[VictorTaelin/OptMem#2](https://github.com/VictorTaelin/OptMem/pull/2).

---

## Examples

A self-contained demo runs the full memory lifecycle **without Hermes**
(temp store, no network, zero tokens):

```bash
python examples/standalone_demo.py
```

It shows: `note` → `recall` (regex + accent-tolerant BM25) → auto-compaction
(deterministic, LLM-free) → `wake` (decayed context). Equivalent to what the
agent gets automatically via the `on_turn_start` hook.

---

## Tests

```bash
pip install pytest
pytest tests/
```

26 end-to-end tests run against the **real** engine and provider (temp
`HERMES_HOME`, no mocks): append, regex + BM25 recall, accent normalization,
nap/decay compression, byte-compat reopen, tool roundtrip, prefetch (wake-once
per session), `on_memory_write` mirror, `on_turn_start` auto-compaction
(local + LLM opt-in + ephemeral-skip), and config/import/init.

A bidirectional retro-compatibility harness also proves the plugin and the
official `memo` CLI read/write the **same store** safely.

### Auto-compaction (no tokens required)

OptMem never grows unbounded: the decay tree compresses old blocks into one
line each ("nap, don't sleep"). `on_turn_start` runs this automatically every
~10 turns so context stays dense without you invoking `optmem_nap`.

The summary is produced by a **deterministic, LLM-free** extractor
(`_local_summary`):

- Scores each line by durability keywords (dates, names, decisions, approvals,
  budgets, churn, KRs, etc.) and a leading `YYYY-MM-DD` date.
- Greedily packs the highest-scoring lines into **≤280 bytes** joined by ` | `.
- Returns `""` when a block has **no** durable signal — the block is then left
  raw rather than losing potentially-relevant ephemeral context.

This means compaction works **everywhere** — CI, standalone scripts, offline —
with **zero token cost**. If you want a more fluent summary, opt in:

```yaml
memory:
  provider: optmem
  llm_summary: true   # or export OPTMEM_LLM_SUMMARY=1
```

When enabled and a host LLM is available, it writes the single summary line;
otherwise the local extractor is used as a fallback.

### Staying aligned with upstream

`./scripts/sync_upstream.sh` fetches Taelin's `memo`, checks that its on-disk
constants still match this engine's, and re-runs the suite. It does **not**
auto-merge — it alerts you when upstream drifts so you can adapt deliberately.

```bash
./scripts/sync_upstream.sh
```

---

## Credits

- Memory model and on-disk format by **Victor Taelin** —
  [VictorTaelin/OptMem](https://github.com/VictorTaelin/OptMem).
- Standalone Hermes integration, Windows locking, BM25 search, and CLI parity
  by the project contributors.

## License

MIT — see [LICENSE](LICENSE).
