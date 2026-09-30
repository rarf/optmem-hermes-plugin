# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :x:                |

Only the latest minor release receives security updates. Please upgrade to the latest version.

## Reporting a Vulnerability

**Please do NOT report security vulnerabilities via public GitHub issues.**

Instead, report them privately through the repository's security reporting
channel with:

1. Description of the vulnerability
2. Steps to reproduce (if applicable)
3. Impact assessment
4. Any suggested fix or mitigation

We will acknowledge receipt within 48 hours and provide a timeline for a fix.

## Security Considerations for Users

The engine, provider and CLI are **local by default** — the store, retrieval and
the default compaction make no network calls and need no runtime dependency
beyond the Python standard library. The one opt-in network path is LLM
summarization (`llm_summary`, off by default); see
[Opt-in LLM summaries](#opt-in-llm-summaries-network-egress). PyYAML is imported
lazily and only for the legacy `memory.optmem` / `plugins.optmem` config fallback
and for writing `config.yaml`; the supported declared-config path
(`<HERMES_HOME>/optmem/config.json`) is plain JSON.

### What this means for security:
- **Local by default** — the store (`LOG.txt` + `TREE/`) lives entirely in your `HERMES_HOME` (default: `~/.hermes/optmem_memory/`); nothing leaves the machine unless you enable LLM summaries
- **No API keys, no credentials in the plugin** — the provider requires zero configuration beyond `memory.provider: optmem-hermes`; LLM summaries borrow the host's own auth and the plugin never sees keys
- **File permissions** — the store inherits standard filesystem permissions. Restrict `HERMES_HOME` if you share the machine.
- **Validated, atomic config writes** — the declared `config.json` is validated before anything is opened, written to a temp file and `os.replace`d (mode `0600`); `config.yaml` edits are surgical (comments and unrelated keys preserved) and the pre-switch file is copied aside first. `hermes optmem-hermes mode` refuses to write without `--yes`.
- **Raw backups** — the native `MEMORY.md`/`USER.md` are copied byte-for-byte with a sha256 manifest into `<HERMES_HOME>/optmem_backups/` (mode `0700`) before any migration or mode change.
- **Lock file** — a `.lock` file coordinates concurrent access (advisory locking via `msvcrt` on Windows, `fcntl` on Unix). It does not provide cryptographic security.

### Opt-in LLM summaries (network egress)

`llm_summary` is **off by default**; compaction is the local, LLM-free extractor.
When you enable it, `on_turn_start` sends the pending decay-block **memory lines**
to the model provider you select through the host's `PluginLlm` facade, routed by
the native auxiliary task `optmem_summary` (configure `auxiliary.optmem_summary`
provider/model/timeout, or pick the `OptMem summaries` task in `hermes model`).
That is the only network egress: network traffic and token cost every ~10 turns
while there is pending work, and none when the setting is off. The lines are sent
as **untrusted data**; any error, empty, multi-line or oversized reply falls back
to the local extractor, and the raw `LOG.txt` records are retained regardless.
The plugin supplies no keys and cannot bypass the host's per-plugin trust gates
(`plugins.entries.optmem-hermes.llm.*`); the host owns auth and routing.

### Threat model
This plugin is designed for **single-user, local agent memory**. It is NOT designed for:
- Multi-user shared stores without filesystem-level isolation
- Untrusted input processing (the 280-byte limit prevents DoS via oversized entries)
- Cryptographic integrity (the format is plain text; anyone with read access can modify `LOG.txt`)

If you need stronger guarantees (encryption, tamper-evidence, multi-tenant isolation), consider layering filesystem encryption or a dedicated vault on top of the store directory.

## Dependency Security

No required runtime dependency: the engine, provider, config resolver and CLI
are standard library only. The optional `legacy-config` extra pins `pyyaml>=6.0,<7`
for the legacy `config.yaml` surface (Hermes itself ships PyYAML).

Run `pip-audit` or `pip install pip-audit && pip-audit` periodically if you install in a standalone environment.