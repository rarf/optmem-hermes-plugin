# Hermes catalog submission

Status: submitted in [Hermes PR #130200](https://github.com/NousResearch/hermes-agent/pull/130200), pinned to the published `v0.3.1` release.

The local `optmem-hermes.yaml` records the published release used by that PR. For future releases, update the existing upstream PR rather than opening a duplicate.

## Entry and discovery

- Category `memory`, tier `community`, maintainer `rarf`.
- Canonical manifest/provider/catalog name: `optmem-hermes`. Python package and existing data paths retain their names. The CLI becomes `hermes optmem-hermes`.
- `subdir: optmem`: the catalog renders `optmem/README.md` at the reviewed pin.
- Linux and Windows are covered by CI; the full test suite passed on a native macOS host.
- `optmem/plugin.yaml` sets `requires_hermes: ">=0.21.2"`. The v0.21.2 release contains both the manifest version gate and `PluginContext.register_auxiliary_task`; v0.21.1 has the task API but lacks the gate.
- The entry tracks the latest published release, not an unmerged cleanup branch. Update the SHA, version and image pin together after publishing a new release.

Proposed GitHub topics: `hermes-agent`, `hermes-plugin`, `memory-provider`, `agent-memory`, `optmem`, `local-first`, `append-only`, `bm25`, `python`.

The catalog has no free-form `tags` field. Category, tier and capability chips are supported discovery fields; GitHub topics are separate.

## Visual and attribution

The catalog uses `docs/assets/catalog-banner-card.png` (2116×1058), the owner-authorized diagram with white horizontal padding for an exact 2:1 ratio and no cropping. Keep only this final asset in the repository. Pin its URL to the same reviewed release commit as the catalog entry.

The README starts with Victor Taelin's original animation, linked rather than copied or rehosted:

https://raw.githubusercontent.com/VictorTaelin/OptMem/1fb164cf39028047781f72ac3bb1e5a691c1dcb0/anim/optmem.gif

It explains the upstream design, not this plugin's UI. The image is pinned to a separate upstream commit; disclose that in the catalog PR. GitHub image hosting passes the structural schema. The 960×540 GIF is approximately 7.4 MB and may be cropped in the 2:1 card. No duplicate gallery entry is needed. Upstream ships no LICENSE file; attribution is not permission to redistribute its media.

## Gates before upstream submission

1. Full tests, lint, wheel build, real-host integration and independent final-commit review.
2. `hermes plugins validate --install-deps optmem --json`: actual registration must match declared capabilities.
3. `python <hermes-agent>/scripts/validate_plugin_catalog.py docs/catalog/optmem-hermes.yaml`.
4. Verify the remote main SHA after merge and publish the matching release tag.
5. Prepare the upstream entry with that exact 40-character SHA and matching version.
6. Verify pinned README/image URLs, open the owner-submitted upstream PR, and inspect CI/readback.

## Policy

Source: https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md

Current upstream main has no two-week plugin-age gate. Its dependency quarantine recommendation is separate; this project declares no dependencies. Memory providers must not use the bare name of an unaffiliated upstream project, hence `optmem-hermes`.

## Upgrade boundary

Existing installations using `memory.provider: optmem` must not be silently changed during development. On upgrade, change the provider key only in the selected profile, retain store paths/mode flags, restart its long-lived process, and verify native gating and existing memory access. Live profile changes are outside these repository corrections.
