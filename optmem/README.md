# OptMem for Hermes

Independent Hermes memory-provider integration of [Victor Taelin's OptMem design](https://github.com/VictorTaelin/OptMem). Not affiliated with or endorsed by upstream.

![Victor Taelin's original OptMem design animation](https://raw.githubusercontent.com/VictorTaelin/OptMem/1fb164cf39028047781f72ac3bb1e5a691c1dcb0/anim/optmem.gif)

Animation by Victor Taelin, linked at an immutable upstream commit, not copied or rehosted. It explains the original design, not a screenshot of this plugin. Upstream ships no LICENSE file; this integration's MIT license does not grant rights to upstream media.

## What you get

- Local append-only records, regex/BM25 retrieval and summary-tree navigation.
- Hybrid mode alongside native memory, or guarded OptMem-only mode after verified migration and backup.
- Nine memory tools and the `hermes optmem-hermes` configuration/migration CLI.
- No API keys or network requests by the memory engine. The wake digest consumes model context.

Summaries are lossy. Raw records remain recoverable, but a later note does not automatically supersede an earlier one. `forget` drops summaries, not raw log records.

## Install and activate

Version 0.3.0 is unreleased and this plugin is not yet in the Hermes catalog. Install a reviewed exact source commit into the environment used by your selected Hermes profile:

```bash
pip install "git+https://github.com/rarf/optmem-hermes-plugin@<FULL-40-CHAR-COMMIT-SHA>"
```

Set the selected profile's `config.yaml`:

```yaml
memory:
  provider: optmem-hermes
```

Restart its long-lived Hermes process, then verify:

```bash
hermes optmem-hermes version --json
hermes optmem-hermes status --json
hermes optmem-hermes check --json
```

`check` exits 1 until replacement readiness is satisfied. These checks are read-only. Hybrid is the default.

## Safely replace native memory

```bash
hermes optmem-hermes migrate --dry-run --json
hermes optmem-hermes migrate --split --json
hermes optmem-hermes check --json
hermes optmem-hermes mode optmem-only --yes --json
```

The CLI verifies migration and a fresh hash-checked backup before disabling native memory/profile injection. Native files are preserved. Inspect the migration plan before applying it, and use `--hermes-home PATH` to target one profile explicitly.

```bash
hermes optmem-hermes rollback --json
```

Rollback restores the previous mode configuration; OptMem data stays intact.

## Upgrade an existing installation

After installing this version, an older profile using `memory.provider: optmem` must select `optmem-hermes`. Preserve its existing mode flags and data paths; do not change other profiles. The Python package and data locations are unchanged. The top-level CLI command changes from `hermes optmem` to `hermes optmem-hermes`.

## Full documentation

[Root README: configuration, tools, storage format, tests, profile isolation, rollback and limitations](../README.md).
