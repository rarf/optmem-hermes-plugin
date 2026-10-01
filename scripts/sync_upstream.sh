#!/usr/bin/env bash
# sync_upstream.sh — keep OptMem-Hermes aligned with Victor Taelin's OptMem.
#
# This does NOT auto-merge code. It fetches the upstream `memo` tool, compares
# only LOG_REC, TREE_REC, and RAW_MAX with this repo's engine constants, and
# runs this repo's local test suite. It detects drift in those three constants
# or test failures; it does not verify the full TREE layout, cover/decay math,
# or overall byte compatibility.
#
# Uses Python (urllib) for the download so it works on both Windows (MSYS) and
# Unix without depending on a working `curl` binary.
#
# Usage:  ./scripts/sync_upstream.sh
# Exit 0 = the three constants match and local tests pass.
# Exit 1 = drift or a test failure.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# MSYS path (/c/...) is not understood by the native Windows Python we shell
# out to for the download — convert to a Windows path.
if command -v cygpath >/dev/null 2>&1; then
  WIN_HERE="$(cygpath -w "$HERE")"
else
  WIN_HERE="$HERE"
fi
# Always use a temp dir inside the repo — MSYS /tmp is unreliable for writes.
TMP="$HERE/.sync_tmp"
mkdir -p "$TMP"
cleanup() { rm -rf "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

echo "==> Fetching upstream OptMem (VictorTaelin/OptMem@main) ..."
python - "$WIN_HERE/.sync_tmp/memo" <<'PY'
import sys, urllib.request
url = "https://raw.githubusercontent.com/VictorTaelin/OptMem/main/memo"
data = urllib.request.urlopen(url, timeout=30).read()
open(sys.argv[1], "wb").write(data)
print("    fetched", len(data), "bytes")
PY

echo "==> Extracting upstream on-disk constants (LOG_REC / TREE_REC / RAW_MAX) ..."
UP_MEMO="$WIN_HERE/.sync_tmp/memo"
up_log()  { grep -E '^LOG_REC'  "$UP_MEMO" | head -1 | grep -oE '[0-9]+'; }
up_tree() { grep -E '^TREE_REC' "$UP_MEMO" | head -1 | grep -oE '[0-9]+'; }
up_raw()  { grep -E '^RAW_MAX'  "$UP_MEMO" | head -1 | grep -oE '[0-9]+'; }

UP_LOG="$(up_log)";  UP_TREE="$(up_tree)";  UP_RAW="$(up_raw)"
echo "    upstream: LOG_REC=$UP_LOG TREE_REC=$UP_TREE RAW_MAX=$UP_RAW"

# This repo's engine constants (single source of truth: optmem/engine.py).
eng_log()  { grep -E '^LOG_REC'  "$HERE/optmem/engine.py" | head -1 | grep -oE '[0-9]+'; }
eng_tree() { grep -E '^TREE_REC' "$HERE/optmem/engine.py" | head -1 | grep -oE '[0-9]+'; }
eng_raw()  { grep -E '^RAW_MAX'  "$HERE/optmem/engine.py" | head -1 | grep -oE '[0-9]+'; }

ENG_LOG="$(eng_log)";  ENG_TREE="$(eng_tree)";  ENG_RAW="$(eng_raw)"
echo "    this repo: LOG_REC=$ENG_LOG TREE_REC=$ENG_TREE RAW_MAX=$ENG_RAW"

DRIFT=0
if [ "$UP_LOG" != "$ENG_LOG" ]; then echo "!! LOG_REC drift: upstream=$UP_LOG this=$ENG_LOG"; DRIFT=1; fi
if [ "$UP_TREE" != "$ENG_TREE" ]; then echo "!! TREE_REC drift: upstream=$UP_TREE this=$ENG_TREE"; DRIFT=1; fi
if [ "$UP_RAW" != "$ENG_RAW" ]; then echo "!! RAW_MAX drift: upstream=$UP_RAW this=$ENG_RAW"; DRIFT=1; fi

echo "==> Running this repo's test suite (real engine, no mocks) ..."
( cd "$HERE" && python -m pytest tests/ -q ) || DRIFT=1

if [ "$DRIFT" -ne 0 ]; then
  echo
  echo "!! DRIFT DETECTED — upstream changed something OptMem-Hermes depends on."
  echo "   Review the upstream diff and update optmem/engine.py deliberately."
  echo "   Upstream: https://github.com/VictorTaelin/OptMem"
  exit 1
fi

echo
echo "==> OK: upstream format compatible and local tests pass. Nothing to do."
echo "   (Tip: also run 'git -C \"$HERE\" log --oneline -1' to see your last change.)"
