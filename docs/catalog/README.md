# Hermes catalog submission

Status: preparation in the plugin repository, not an upstream submission.

Copy `optmem-hermes.yaml` to `plugin-catalog/optmem-hermes.yaml` on a branch based on current `NousResearch/hermes-agent:main` after the plugin corrections have merged.

## Entry and discovery

- Category `memory`, tier `community`, maintainer `rarf`.
- Canonical manifest/provider/catalog name: `optmem-hermes`. Python package and existing data paths retain their names. The CLI becomes `hermes optmem-hermes`.
- `subdir: optmem`: the catalog renders `optmem/README.md` at the reviewed pin.
- Linux and Windows match CI coverage. Minimum Hermes version is omitted until verified.
- The SHA in this template predates these changes: **replace it in the upstream submission with the final reviewed plugin SHA**. A file cannot contain its own commit hash; the final upstream entry is prepared after the plugin commit exists.

Proposed GitHub topics: `hermes-agent`, `hermes-plugin`, `memory-provider`, `agent-memory`, `optmem`, `local-first`, `append-only`, `bm25`, `python`.

The catalog has no free-form `tags` field. Category, tier and capability chips are supported discovery fields; GitHub topics are separate.

## Visual and attribution

The original owner-supplied diagram is preserved at `docs/assets/catalog-banner.png` (1912×1058). The card uses `docs/assets/catalog-banner-card.png` (2116×1058), with white horizontal padding for an exact 2:1 ratio and no cropping of the diagram. The owner confirmed authorship and explicitly authorized public redistribution. Pin the image URL to the same final source commit as the catalog entry; the current template URL is a placeholder and does not yet resolve.

The README starts with Victor Taelin's original animation, linked rather than copied or rehosted:

https://raw.githubusercontent.com/VictorTaelin/OptMem/1fb164cf39028047781f72ac3bb1e5a691c1dcb0/anim/optmem.gif

It explains the upstream design, not this plugin's UI. The image is pinned to a separate upstream commit; disclose that in the catalog PR. GitHub image hosting passes the structural schema. The 960×540 GIF is approximately 7.4 MB and may be cropped in the 2:1 card. No duplicate gallery entry is needed. Upstream ships no LICENSE file; attribution is not permission to redistribute its media.

## Gates before upstream submission

1. Full tests, lint, wheel build, real-host integration and independent final-commit review.
2. `hermes plugins validate --install-deps optmem --json`: actual registration must match declared capabilities.
3. `python <hermes-agent>/scripts/validate_plugin_catalog.py docs/catalog/optmem-hermes.yaml`.
4. Verify the remote main SHA after merge. Decide and authorize publication of the corresponding release/tag; 0.3.0 is currently unreleased.
5. Prepare the upstream entry with that exact 40-character SHA and matching version.
6. Verify pinned README/image URLs, open the owner-submitted upstream PR, and inspect CI/readback.

## Policy

Source: https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md

Current upstream main has no two-week plugin-age gate. Its dependency quarantine recommendation is separate; this project declares no dependencies. Memory providers must not use the bare name of an unaffiliated upstream project, hence `optmem-hermes`.

## Upgrade boundary

Existing installations using `memory.provider: optmem` must not be silently changed during development. On upgrade, change the provider key only in the selected profile, retain store paths/mode flags, restart its long-lived process, and verify native gating and existing memory access. Live profile changes are outside these repository corrections.
