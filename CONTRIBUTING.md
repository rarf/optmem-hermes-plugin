# Contributing to OptMem Hermes Plugin

Thank you for considering contributing! This plugin is a reimplementation of Victor Taelin's OptMem for Hermes Agent.

## How to contribute

### Reporting bugs
- Use GitHub Issues with the `bug` label
- Include: Hermes version, OS, steps to reproduce, expected vs actual behavior
- For memory issues: include `LOG.txt` head/tail and `.lock` state if relevant

### Suggesting features
- Use GitHub Issues with the `enhancement` label
- Explain the use case and how it fits the OptMem philosophy (append-only, decay, portable)

### Pull requests
1. Fork the repo
2. Create a branch: `git checkout -b feat/your-feature`
3. Make changes with tests
4. Run: `make test` (or `python -m pytest tests/ -q`)
5. Run: `make lint` (if configured)
6. Submit PR with clear description

### Code style
- Python 3.11+
- Type hints on public functions
- No external dependencies beyond stdlib + PyYAML (already in Hermes)
- Keep engine stateless-ish; provider handles Hermes integration

### Testing
- Unit tests in `tests/` — test both `OptMemEngine` and `OptMemProvider`
- Test byte-compatibility with original `memo` CLI when possible
- Test both search modes: `regex` and `bm25`

### Philosophy
- **No network calls** — this is local-first by design
- **Byte-compatible with upstream** — never break `LOG.txt`/`TREE/` format
- **280 bytes/entry hard limit** — matches original; do not increase
- **Append-only** — never delete, only "forget" (summary rebuild)

## Development setup

```bash
# Clone
git clone https://github.com/rarf/optmem-hermes-plugin.git
cd optmem-hermes-plugin

# Install in editable mode (requires Hermes env for full test)
pip install -e .[dev]

# Run tests
python -m pytest tests/ -q

# Test with Hermes (requires Hermes installed and gateway running)
# 1. cp -r optmem ~/.hermes/plugins/optmem-hermes
# 2. Add to ~/.hermes/config.yaml:
#    memory:
#      provider: optmem-hermes
# 3. hermes gateway restart
```

## Release process

Maintainer only:
1. Update version in `pyproject.toml` and `optmem/plugin.yaml`
2. Update `CHANGELOG.md`
3. Tag: `git tag vX.Y.Z`
4. Push tag: `git push origin vX.Y.Z`
5. GitHub Actions builds and publishes release

## Code of conduct

Be respectful. This is a small project — constructive feedback only.