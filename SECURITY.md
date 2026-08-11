# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

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

This plugin is a **local-only memory store** — it makes no network calls and has no external dependencies beyond Python stdlib + PyYAML (which is already a Hermes dependency).

### What this means for security:
- **No data leaves your machine** — the store (`LOG.txt` + `TREE/`) lives entirely in your `HERMES_HOME` (default: `~/.hermes/optmem_memory/`)
- **No API keys, no credentials** — the provider requires zero configuration beyond `memory.provider: optmem`
- **File permissions** — the store inherits standard filesystem permissions. Restrict `HERMES_HOME` if you share the machine.
- **Lock file** — a `.lock` file coordinates concurrent access (advisory locking via `msvcrt` on Windows, `fcntl` on Unix). It does not provide cryptographic security.

### Threat model
This plugin is designed for **single-user, local agent memory**. It is NOT designed for:
- Multi-user shared stores without filesystem-level isolation
- Untrusted input processing (the 280-byte limit prevents DoS via oversized entries)
- Cryptographic integrity (the format is plain text; anyone with read access can modify `LOG.txt`)

If you need stronger guarantees (encryption, tamper-evidence, multi-tenant isolation), consider layering filesystem encryption or a dedicated vault on top of the store directory.

## Dependency Security

Only dependency: `pyyaml` (transitive via Hermes). We pin `pyyaml>=6.0` in optional dev deps. Hermes itself manages its dependencies.

Run `pip-audit` or `pip install pip-audit && pip-audit` periodically if you install in a standalone environment.