# OptMem for Hermes

![Victor Taelin's original OptMem animation](https://raw.githubusercontent.com/VictorTaelin/OptMem/1fb164cf39028047781f72ac3bb1e5a691c1dcb0/anim/optmem.gif)

*Original design animation by [Victor Taelin](https://github.com/VictorTaelin/OptMem). This is an independent Hermes integration, not the upstream project.*

**Local memory that keeps the history and limits what enters your context.**
Store durable facts, search them with regex/BM25, and zoom from summaries back to original records. No API key or LLM call is needed by the memory engine.

## Install

Version **0.3.1** is distributed through GitHub Releases. The plugin is **not yet in the Hermes catalog**. Install into the Python environment used by Hermes:

```bash
pip install "git+https://github.com/rarf/optmem-hermes-plugin@v0.3.1"
```

Select the provider in your profile's `config.yaml`, then restart that profile's Hermes process:

```yaml
memory:
  provider: optmem-hermes
```

```bash
hermes optmem-hermes status --json
```

## Choose your mode

- **Hybrid** (default): OptMem runs alongside native `MEMORY.md` / `USER.md`.
- **OptMem-only**: replaces native memory injection after verified migration and backup.

To switch safely, inspect the migration plan first:

```bash
hermes optmem-hermes migrate --dry-run --json
hermes optmem-hermes migrate --split --json
hermes optmem-hermes check --json
hermes optmem-hermes mode optmem-only --yes --json
```

Native files are preserved. `check` is read-only and exits 1 until replacement readiness is satisfied. Use `--hermes-home PATH` to target one profile explicitly.

To undo the last mode switch without deleting OptMem data:

```bash
hermes optmem-hermes rollback --json
```

## Good to know

- Raw records are append-only. Summaries are **lossy**, but original details remain recoverable.
- Later notes do not automatically supersede earlier ones: **no semantic conflict resolution**.
- The wake digest is bounded but **consumes model context**.
- `forget` removes summaries, not raw records.
- **Manual merges by default.** Compaction happens only when the agent runs the
  requested `optmem_nap` ("nothing runs in the background"), matching upstream.
  `auto_nap` (background drain every ~10 turns) and `llm_summary` (host-LLM
  summaries) are both **optional opt-ins**, off unless you enable them.
  `llm_summary` is **not a standalone activation**: it only selects which
  summarizer the `auto_nap` path uses, and network egress / token cost happen
  only when **both** `auto_nap` and `llm_summary` are true.
- Upgrading an older installation: change `memory.provider: optmem` to `optmem-hermes` only in the selected profile. Data paths stay unchanged; the CLI becomes `hermes optmem-hermes`.

## Optional LLM summaries

Compaction is local and LLM-free by default. `llm_summary` is **not a standalone
activation**: it only selects which summarizer the `auto_nap` path uses. Opt in
to `auto_nap` and `llm_summary` together and the host LLM summarizes pending
decay blocks through the native auxiliary task `optmem_summary`: the host owns
auth and routing, the plugin supplies no keys, and `provider: auto` / `model: ""`
uses the host's configured model. Only when **both** are true does it send the
pending block lines to the provider you select (network egress, tokens/cost);
with either one off there is no egress. Any failure falls back to the local
extractor with the raw records retained. Set both in
`<HERMES_HOME>/optmem/config.json` and route the task in `hermes model` or under
`auxiliary.optmem_summary`. See
[LLM summaries (opt-in)](docs/reference.md#llm-summaries-opt-in).

## More

[Configuration, tools and technical reference](docs/reference.md) · [Catalog submission checklist](docs/catalog/README.md) · [MIT license](LICENSE)

The upstream animation is linked, not copied or rehosted. Upstream ships no LICENSE file; this repository's MIT license does not cover its media. No affiliation or endorsement is implied.
