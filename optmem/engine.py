"""OptMem engine — portable reimplementation of Taelin's OptMem store.

This is a dependency-free, cross-platform reimplementation of the OptMem
append-only memory. It keeps the EXACT on-disk format of the original
``memo`` tool (fixed-width records, ``LOG.txt`` + ``TREE/<size>``), so logs
are interchangable with https://github.com/VictorTaelin/OptMem.

Differences from the original:
- No ``fcntl`` (POSIX-only). Uses a portable advisory lock: ``msvcrt`` on
  Windows, ``fcntl`` on Unix. Falls back to no-op if neither is available.
- Adds a BM25 ranked search (``recall``) with accent normalization, on top
  of the original regex recall.
- Exposes a small API used by the Hermes ``MemoryProvider`` wrapper:
  ``append``, ``wake_lines``, ``pending_naps``, ``apply_nap``, ``recall``.

Records are fixed width so a memory or block is found by seeking to its
offset — no index file to keep in sync.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import re
import sys
import unicodedata
from collections import defaultdict

# Fixed-width records, identical to the original memo tool so the two are
# byte-compatible on disk.
LOG_REC = 320
TREE_REC = 288

# Blocks up to this many raw memories compress straight from the log.
RAW_MAX = 16

# Sizes (mirror memo defaults).
WAKE_LINES = 96           # ~8k tokens of context printed by wake (memo default)
ENTRY_CHARS = 280         # longest one memory line, in bytes


class WakeNeedsCompression(Exception):
    """A required block summary is missing; the wake digest is incomplete.

    Carries the partially rendered context and the next nap prompt (in
    ``result``) so a caller can surface the compression-needed response instead
    of papering over the hole with a placeholder line. Mirrors upstream
    ``memo wake`` refusing with "Cannot wake" while a needed summary is
    uncompressed.
    """

    def __init__(self, result: dict):
        missing = result.get("missing", [])
        super().__init__(
            "wake needs "
            + ", ".join(f"#{lo}-{hi - 1}" for lo, hi in missing)
            + " compressed before it can be complete"
        )
        self.result = result


# ---------------------------------------------------------------------------
# Portable advisory lock
# ---------------------------------------------------------------------------

def _make_lock(path: str):
    """Return a context manager granting an exclusive lock on ``path``.

    Uses msvcrt on Windows, fcntl on Unix. No-op if neither is importable.
    """
    lockf = open(os.path.join(os.path.dirname(path) or ".", ".lock"), "a")  # noqa: SIM115
    if sys.platform == "win32":
        try:
            import msvcrt
        except Exception:
            return _NullLock(lockf)

        def _acquire():
            # LK_NBLCK never blocks; spin with backoff so parallel processes
            # (Taelin's target: many concurrent sessions) queue instead of
            # raising 'Resource deadlock avoided' like LK_LOCK does under load.
            import time as _t
            waited = 0.0
            while True:
                try:
                    msvcrt.locking(lockf.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if waited > 30.0:
                        raise
                    _t.sleep(min(0.01 + waited * 0.2, 0.25))
                    waited += 0.01

        def _release():
            with contextlib.suppress(Exception):
                msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)

        return _Flock(lockf, _acquire, _release)

    try:
        import fcntl
    except Exception:
        return _NullLock(lockf)

    def _acquire():
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)

    def _release():
        with contextlib.suppress(Exception):
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    return _Flock(lockf, _acquire, _release)


class _Flock:
    def __init__(self, f, acquire, release):
        self._f = f
        self._acquire = acquire
        self._release = release

    def __enter__(self):
        self._acquire()
        return self

    def __exit__(self, *exc):
        try:
            self._release()
        finally:
            with contextlib.suppress(Exception):
                self._f.close()
        return False


class _NullLock:
    def __init__(self, f):
        self._f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(Exception):
            self._f.close()
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """lowercase + strip diacritics so 'caçula' == 'cacula'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _normalize(s)) if t]


# Regex metacharacters: their presence means the caller is speaking `memo`'s
# language (a pattern), not prose.
_REGEX_HINT_CHARS = frozenset(".*+?[](){}|^$\\")


def is_natural_language(query: str) -> bool:
    """True when *query* is prose rather than a `memo`-style regex/pattern.

    A user sentence compiled as a regex stops matching anything useful (the
    literal words plus punctuation must appear verbatim) and an unbalanced
    bracket raises ``re.error``. Sentences are routed to token search instead,
    while short literal queries ("paywall") and explicit patterns
    ("AllDrivers.*paywall") keep the regex path.
    """
    text = (query or "").strip()
    if not text:
        return False
    if text.endswith(("?", "!")):
        return True
    if any(ch in _REGEX_HINT_CHARS for ch in text):
        return False
    return len(text.split()) >= 3


def _pad(text: str, rec: int) -> bytes:
    b = text.encode("utf-8")
    if len(b) > rec - 1:
        raise ValueError(f"entry too long: {len(b)} bytes, limit {rec - 1}")
    return b + b" " * (rec - 1 - len(b)) + b"\n"


def _parse(line: str) -> tuple[int, str, str]:
    head, _, rest = line.partition(" ")
    date, _, text = rest.partition(" ")
    return int(head[1:]), date, text


def validate_block(lo: int, hi: int) -> str | None:
    """Return an error string unless ``(lo, hi)`` is a real block id.

    A block is an aligned power-of-two range, ``hi`` EXCLUSIVE — exactly what
    ``wake`` prints (``#16-31`` -> ``lo=16, hi=32``). Mirrors upstream
    ``memo``'s ``block_id``: without the shape check, ``4-5`` and ``5-6`` would
    both read record 5. ``None`` means valid.
    """
    n = hi - lo
    if n < 2 or (n & (n - 1)) or lo % n or lo < 0:
        return f"{lo}-{hi - 1} is not a block. Copy the id printed by optmem_wake, like 16-31."
    return None


# ---------------------------------------------------------------------------
# Cover / decay tree (identical math to memo)
# ---------------------------------------------------------------------------

def _cover(T: int, alpha: float) -> list[tuple[int, int]]:
    """Tile [0,T) with aligned power-of-two blocks; keep a block whole iff its
    size is at most ``alpha`` times its age. Bigger alpha = coarser."""
    root = 1
    while root < T:
        root *= 2
    out: list[tuple[int, int]] = []
    stack = [(0, root)]
    while stack:
        lo, hi = stack.pop()
        if lo >= T:
            continue
        size = hi - lo
        if size > 1 and (hi > T or size > alpha * (T - lo)):
            mid = (lo + hi) // 2
            stack.append((mid, hi))
            stack.append((lo, mid))
        else:
            out.append((lo, hi))
    out.sort()
    return out


def cover(T: int, budget: int) -> list[tuple[int, int]]:
    """The blocks ``wake`` prints: at most ``budget`` of them, finest near T.

    Detail decays with age: recent memories stay verbatim, ancient ones
    collapse. If everything fits, nothing is compressed at all.
    """
    if T <= 0:
        return []
    if budget >= T:
        return [(i, i + 1) for i in range(T)]
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if len(_cover(T, mid)) > budget:
            lo = mid
        else:
            hi = mid
    out = _cover(T, hi)
    while len(out) < budget:
        i = max((i for i, b in enumerate(out) if b[1] - b[0] > 1), default=None)
        if i is None:
            break
        lo_, hi_ = out[i]
        mid = (lo_ + hi_) // 2
        out[i:i + 1] = [(lo_, mid), (mid, hi_)]
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class OptMemEngine:
    """Append-only memory store with a binary decay tree and BM25 search."""

    def __init__(self, memory_dir: str):
        self.dir = memory_dir
        os.makedirs(os.path.join(self.dir, "TREE"), exist_ok=True)
        self.log_path = os.path.join(self.dir, "LOG.txt")
        if not os.path.exists(self.log_path):
            open(self.log_path, "a").close()

    # -- low level ----------------------------------------------------------

    def _lock(self):
        return _make_lock(self.log_path)

    def _count(self, path: str, rec: int) -> int:
        try:
            return os.path.getsize(path) // rec
        except FileNotFoundError:
            return 0

    def log_len(self) -> int:
        return self._count(self.log_path, LOG_REC)

    def _repair(self, path: str, rec: int) -> None:
        try:
            n = os.path.getsize(path)
        except FileNotFoundError:
            return
        if n % rec:
            with open(path, "r+b") as f:
                f.truncate(n - n % rec)

    def _log_slice(self, lo: int, hi: int) -> list[tuple[int, str, str]]:
        with open(self.log_path, "rb") as f:
            f.seek(lo * LOG_REC)
            buf = f.read((hi - lo) * LOG_REC)
        out = []
        for i in range(len(buf) // LOG_REC):
            raw = buf[i * LOG_REC:(i + 1) * LOG_REC].decode("utf-8", "replace").rstrip()
            if not raw.strip():
                continue
            try:
                mid, date, text = _parse(raw)
            except Exception:
                continue
            out.append((mid, date, text))
        return out

    def _tree_path(self, size: int) -> str:
        return os.path.join(self.dir, "TREE", str(size))

    def _tree_get(self, lo: int, hi: int) -> str | None:
        size = hi - lo
        p = self._tree_path(size)
        try:
            with open(p, "rb") as f:
                f.seek((lo // size) * TREE_REC)
                rec = f.read(TREE_REC)
        except FileNotFoundError:
            return None
        try:
            return rec.decode("utf-8", "replace").rstrip() or None
        except Exception:
            return None

    def _tree_put(self, lo: int, hi: int, text: str) -> bool:
        size = hi - lo
        p = self._tree_path(size)
        with self._lock():
            self._repair(p, TREE_REC)
            if self._count(p, TREE_REC) != lo // size:
                return False
            with open(p, "ab") as f:
                f.write(_pad(text, TREE_REC))
                f.flush()
                os.fsync(f.fileno())
        return True

    # -- public writes ------------------------------------------------------

    def append(self, text: str, date: str | None = None) -> int:
        """Append one memory line. Returns its id."""
        text = text.strip()
        if not text:
            raise ValueError("empty memory")
        if "\n" in text or "\r" in text:
            raise ValueError("a memory is one line")
        b = text.encode("utf-8")
        if len(b) > ENTRY_CHARS:
            raise ValueError(f"too long: {len(b)} bytes, limit {ENTRY_CHARS}")
        if date is None:
            date = datetime.date.today().isoformat()
        with self._lock():
            self._repair(self.log_path, LOG_REC)
            base = self.log_len()
            with open(self.log_path, "ab") as f:
                f.write(_pad(f"#{base} {date} {text}", LOG_REC))
                f.flush()
                os.fsync(f.fileno())
        return base

    def import_lines_pairs(self, lines: list[tuple[str, str]]) -> int:
            """Bulk append (date, text) pairs. Returns first id."""
            with self._lock():
                self._repair(self.log_path, LOG_REC)
                base = self.log_len()
                with open(self.log_path, "ab") as f:
                    for k, (date, text) in enumerate(lines):
                        f.write(_pad(f"#{base + k} {date} {text}", LOG_REC))
                    f.flush()
                    os.fsync(f.fileno())
            return base

    # -- reads --------------------------------------------------------------

    def wake(self, budget: int | None = None) -> dict:
        """Render the wake digest with completeness metadata.

        Mirrors upstream ``memo wake``: a block in the cover whose summary is
        missing means the document cannot be written completely, so it is
        reported (``complete=False`` + ``missing`` + the next ``nap`` prompt)
        rather than papered over with a placeholder. A raw-only store — every
        cover block a single record — is always complete, so a legitimate
        verbatim wake is never dropped.

        ``budget`` is the reading budget (lines printed). ``None`` uses the
        per-store ``config`` ``WAKE_LINES`` (memo parity) or the module default.
        """
        if not isinstance(budget, int) or budget < 1:
            budget = self.read_config().get("WAKE_LINES", WAKE_LINES)
        if not isinstance(budget, int) or budget < 1:
            budget = WAKE_LINES
        T = self.log_len()
        if T == 0:
            return {
                "lines": [],
                "complete": True,
                "missing": [],
                "pending": [],
                "nap": None,
                "rebuild": [],
                "budget": budget,
            }
        lines: list[str] = []
        missing: list[tuple[int, int]] = []
        for lo, hi in cover(T, budget):
            if hi - lo == 1:
                rec = self._log_slice(lo, hi)
                if not rec:
                    continue
                mid, date, text = rec[0]
                lines.append(f"#{mid} {date} {text}")
            else:
                s = self._tree_get(lo, hi)
                if s is None:
                    missing.append((lo, hi))
                    continue
                lines.append(f"#{lo}-{hi - 1} {s}")
        pending = self.pending_naps()
        nap = self.nap_prompt(pending[0][0], pending[0][1]) if pending else None
        # A missing cover block that is NOT pending cannot be fixed by the next
        # nap: its tree file still holds a record (so ``_pending`` thinks it is
        # built) but the record is unreadable — a corrupted summary. The caller
        # must be told to forget/rebuild it, not to "do the compression below".
        pending_set = set(pending)
        rebuild = [(lo, hi) for lo, hi in missing if (lo, hi) not in pending_set]
        return {
            "lines": lines,
            "complete": not missing,
            "missing": missing,
            "pending": pending,
            "nap": nap,
            "rebuild": rebuild,
            "budget": budget,
        }

    def wake_lines(self, budget: int | None = None) -> list[str]:
        """The rendered digest lines, refusing an incomplete document.

        Raises ``WakeNeedsCompression`` when a required summary is missing
        (upstream ``memo wake`` exits 1 rather than print a digest with a hole).
        Callers that must stay non-fatal — the Hermes prefetch — use ``wake``.
        """
        result = self.wake(budget)
        if not result["complete"]:
            raise WakeNeedsCompression(result)
        return result["lines"]

    # -- naps (compression) -------------------------------------------------

    def _pending(self, T: int, limit: int | None = None) -> list[tuple[int, int]]:
        todo: list[tuple[int, int]] = []
        size = 2
        while size <= T:
            have = self._count(self._tree_path(size), TREE_REC)
            for k in range(have, T // size):
                todo.append((k * size, (k + 1) * size))
                if limit and len(todo) >= limit:
                    return todo
            size *= 2
        return todo

    def pending_naps(self, limit: int | None = None) -> list[tuple[int, int]]:
        """Blocks that can be built and have not been, smallest first."""
        return self._pending(self.log_len(), limit)

    def nap_prompt(self, lo: int, hi: int) -> str:
        """Build the compression instruction for block [lo,hi)."""
        if hi - lo <= RAW_MAX:
            body = "\n".join(f"  #{e[0]} {e[1]} {e[2]}" for e in self._log_slice(lo, hi))
        else:
            mid = (lo + hi) // 2
            halves = []
            for a, b in ((lo, mid), (mid, hi)):
                s = self._tree_get(a, b)
                if s is None:
                    s = "(missing — rebuild)"
                halves.append(f"  #{a}-{b - 1} {s}")
            body = "\n".join(halves)
        left = len(self.pending_naps()) - 1
        tail = "" if left <= 0 else f"\n{left} compressions remain"
        return (
            f"Compress memories #{lo}-{hi - 1} into one line of at most "
            f"{ENTRY_CHARS} UTF-8 bytes (not characters).\n"
            "Aim well below the limit. After a size error, rewrite substantially shorter; "
            "do not retry with only small edits.\n"
            "Keep what has lasting effect, drop what does not. Invent nothing.\n\n"
            f"{body}{tail}\n"
        )

    def block_lines(self, lo: int, hi: int) -> list[str]:
        """Return the raw memory lines (text only) for block [lo, hi).

        For raw blocks (<= RAW_MAX) this pulls the original LOG.txt lines; for
        larger blocks it pulls the already-compressed summaries from TREE. Used
        by the local (LLM-free) auto-nap summarizer.
        """
        if hi - lo <= RAW_MAX:
            return [e[2] for e in self._log_slice(lo, hi)]
        mid = (lo + hi) // 2
        out: list[str] = []
        for a, b in ((lo, mid), (mid, hi)):
            s = self._tree_get(a, b)
            if s:
                out.append(s)
        return out

    def next_nap(self) -> tuple[tuple[int, int], str] | None:
        todo = self.pending_naps(limit=1)
        if not todo:
            return None
        lo, hi = todo[0]
        return (lo, hi), self.nap_prompt(lo, hi)

    def apply_nap(self, lo: int, hi: int, summary: str) -> bool:
        """Store a compression. True only when a summary was written."""
        return self.apply_nap_status(lo, hi, summary) == "compressed"

    def apply_nap_status(self, lo: int, hi: int, summary: str) -> str:
        """Validate and store a compression; report what actually happened.

        Returns ``"compressed"`` when a summary was written, ``"already_settled"``
        when the block already has one (nothing written) and
        ``"race"``/``"nothing_pending"`` when the writable slot changed under us
        (nothing written). Raises ``ValueError`` for an empty/oversized summary,
        a malformed block id, a block whose range exceeds the log, or a block
        that is not the next one due.

        Every check runs INSIDE the store lock, so a parallel writer cannot slip
        a block in between validation and the append (mirrors upstream
        ``memo nap``: a block that is not the next one is a "Wrong block").
        """
        summary = summary.strip()
        if not summary:
            raise ValueError("empty summary")
        nbytes = len(summary.encode("utf-8"))
        if nbytes > ENTRY_CHARS:
            raise ValueError(
                f"summary too long: {nbytes} UTF-8 bytes, max {ENTRY_CHARS} bytes; "
                f"reduce by at least {nbytes - ENTRY_CHARS} bytes. "
                "Rewrite substantially shorter; do not truncate important facts."
            )
        err = validate_block(lo, hi)
        if err:
            raise ValueError(err)
        size = hi - lo
        p = self._tree_path(size)
        with self._lock():
            self._repair(p, TREE_REC)
            total = self.log_len()
            if hi > total:
                raise ValueError(
                    f"block {lo}-{hi - 1} is beyond the log: it holds {total} "
                    f"{'memory' if total == 1 else 'memories'}, so this range "
                    "exceeds what exists."
                )
            if self._tree_get(lo, hi) is not None:
                return "already_settled"
            todo = self.pending_naps(limit=1)
            if not todo:
                return "nothing_pending"
            if (lo, hi) != todo[0]:
                raise ValueError(
                    f"wrong block {lo}-{hi - 1}: blocks are built in order; "
                    f"the next is {todo[0][0]}-{todo[0][1] - 1}."
                )
            if self._count(p, TREE_REC) != lo // size:
                return "race"
            with open(p, "ab") as f:
                f.write(_pad(summary, TREE_REC))
                f.flush()
                os.fsync(f.fileno())
        return "compressed"

    def forget(self, lo: int, hi: int) -> list[tuple[int, int]]:
        """Drop block ``[lo, hi)`` and every block built on it; LOG untouched.

        Returns the blocks actually dropped (empty when there was no summary
        there) so the caller can report a truthful not-found instead of a false
        success. Mirrors upstream ``memo forget`` / ``tree_drop``.
        """
        err = validate_block(lo, hi)
        if err:
            raise ValueError(err)
        gone: list[tuple[int, int]] = []
        with self._lock():
            total = self.log_len()
            size = hi - lo
            while size <= total:
                p = self._tree_path(size)
                k = lo // size
                n = self._count(p, TREE_REC)
                if n > k:
                    gone += [(i * size, (i + 1) * size) for i in range(k, n)]
                    with open(p, "r+b") as f:
                        f.truncate(k * TREE_REC)
                size *= 2
        return gone

    # -- search (BM25 + regex) ----------------------------------------------

    def _all_records(self) -> list[tuple[int, str, str, list[str]]]:
        T = self.log_len()
        docs = []
        for i in range(T):
            try:
                mid, date, text = self._log_slice(i, i + 1)[0]
            except Exception:
                continue
            docs.append((mid, date, text, _tokenize(text)))
        return docs

    # -- index (built once, reused) ---------------------------------------

    def build_index(self) -> None:
        """Build the in-memory BM25 index from the whole log.

        Call once after writes settle (or before a batch of recall calls).
        Avoids re-tokenizing every record on every query. The index is
        invalidated automatically when the log grows (see ``_index_len``).
        """
        docs = self._all_records()
        self._index_docs = docs
        self._index_len = len(docs)
        N = len(docs)
        df = defaultdict(int)
        for _, _, _, toks in docs:
            for t in set(toks):
                df[t] += 1
        self._index_idf = {t: (N - df[t] + 0.5) / (df[t] + 0.5) for t in df}
        self._index_avgdl = sum(len(t) for _, _, _, t in docs) / N if N else 0
        self._index_n = N

    def _index_stale(self) -> bool:
        """True if the log changed since the index was built."""
        if getattr(self, "_index_docs", None) is None:
            return True
        return self.log_len() != getattr(self, "_index_len", -1)

    def plan_recall(self, query: str, mode: str = "auto") -> str:
        """Resolve the concrete mode for *query*: "regex" | "bm25" | "token".

        ``mode="auto"`` keeps regex for pattern-looking queries and routes prose
        (``is_natural_language``) to token search — including an *invalid* pattern,
        which falls back rather than failing a user sentence.

        An EXPLICIT ``mode="regex"`` with an invalid pattern raises ``ValueError``:
        the caller asked for `memo` parity and must be told the pattern is broken
        instead of getting a bare ``re.error`` or an empty result.
        """
        requested = (mode or "auto").strip().lower()
        if requested not in ("auto", "regex", "bm25", "token"):
            raise ValueError(
                f"unknown recall mode {mode!r}; allowed: auto, regex, bm25, token"
            )
        if requested == "auto":
            requested = "token" if is_natural_language(query) else "regex"
        if requested == "regex":
            try:
                re.compile(query, re.I)
            except re.error as exc:
                if mode and mode.strip().lower() == "regex":
                    raise ValueError(f"invalid regex {query!r}: {exc}") from exc
                return "token"
        return requested

    def recall(self, query: str, topk: int = 5, mode: str = "regex",
               use_index: bool = True) -> list[tuple[float, int, str, str]]:
        """Recall, dispatch on the planned mode.

        Default mode "regex" matches the official OptMem `memo recall` behavior
        exactly: case-insensitive regex over "#id date text", newest matches
        first, capped by output size. "bm25" is the optional accent-normalized
        ranked search; "token" is natural-language retrieval (BM25 plus a literal
        substring fallback so a rare token BM25 cannot rank is still found);
        "auto" chooses per query and never compiles prose into a broken regex.
        """
        if not self.log_len():
            return []
        resolved = self.plan_recall(query, mode)
        if resolved == "token":
            return self._recall_token(query, topk)
        if resolved == "bm25":
            return self._recall_bm25(query, topk, use_index)
        return self._recall_regex(query, topk)

    def recall_meta(self, query: str, topk: int = 0, mode: str = "regex",
                    use_index: bool = True) -> dict:
        """Recall plus truthful counts for the caller's response.

        Returns ``{"results", "total", "truncated", "mode_used"}``. For ``regex``
        the ``total`` is every matching record and the returned list is the
        newest matches that fit the reading budget (``PART_CHARS``) and, when
        given, ``topk`` — so a vague pattern is never silently cut to five.
        ``truncated`` is True when matches were dropped by either cap. Semantic
        modes keep their ranked ``topk`` (0 = all ranked hits).
        """
        if not self.log_len():
            return {"results": [], "total": 0, "truncated": False, "mode_used": mode}
        resolved = self.plan_recall(query, mode)
        if resolved == "regex":
            results, total = self._recall_regex_page(query)
            returned = results[:topk] if topk else results
            return {
                "results": returned,
                "total": total,
                "truncated": len(returned) < total,
                "mode_used": resolved,
            }
        if resolved == "token":
            results = self._recall_token(query, topk)
            full = len(results) if not topk else len(self._recall_token(query, 0))
        else:
            results = self._recall_bm25(query, topk, use_index)
            full = len(results) if not topk else len(self._recall_bm25(query, 0, use_index))
        return {
            "results": results,
            "total": full,
            "truncated": full > len(results),
            "mode_used": resolved,
        }

    def _recall_regex(self, query: str, topk: int = 5) -> list[tuple[float, int, str, str]]:
        """`memo recall` parity: case-insensitive regex, newest matches first."""
        out, _hits = self._recall_regex_page(query)
        return out[:topk] if topk else out

    def _recall_regex_page(
        self, query: str
    ) -> tuple[list[tuple[float, int, str, str]], int]:
        """The newest regex matches that fit ``PART_CHARS``, plus the total hits.

        ``total`` is every matching record; the list is capped by the reading
        budget exactly as upstream ``memo recall`` caps it (a vague regex
        matches the whole log, which does not fit a harness's output).
        """
        pat = re.compile(query, re.I)
        part_chars = self.read_config().get("PART_CHARS", 20000)
        hits, out, size = 0, [], 0
        for e in self._all_records():
            line = f"#{e[0]} {e[1]} {e[2]}"
            if not pat.search(line):
                continue
            hits += 1
            out.append((1.0, e[0], e[1], e[2]))
            size += len(line.encode()) + 1
            while size > part_chars:
                old = out.pop(0)
                size -= len(f"#{old[1]} {old[2]} {old[3]}".encode()) + 1
        out.reverse()  # newest-first, matching memo's "Newest N of M" output
        return out, hits

    def _recall_token(self, query: str, topk: int = 5) -> list[tuple[float, int, str, str]]:
        """Natural-language retrieval: BM25 ranking plus a literal fallback.

        A question rarely repeats a memory verbatim, so BM25 ranks whatever
        matches; a rare identifier BM25's tokenizer splits ("ZXQ-4481") is
        recovered by a normalized substring scan. No pattern is compiled from the
        user's text, so punctuation cannot break the search.
        """
        ranked = self._recall_bm25(query, topk=topk)
        seen = {hit[1] for hit in ranked}
        for hit in self._recall_substring(query, topk):
            if hit[1] not in seen:
                seen.add(hit[1])
                ranked.append(hit)
        return ranked[:topk] if topk else ranked

    def _recall_substring(self, query: str, topk: int = 5) -> list[tuple[float, int, str, str]]:
        """Normalized literal scan for the query's most distinctive tokens."""
        tokens = sorted({t for t in _tokenize(query) if len(t) >= 3}, key=len, reverse=True)[:3]
        if not tokens:
            return []
        pat = re.compile("|".join(re.escape(t) for t in tokens))
        out = []
        for mid, date, text, _tokens in self._all_records():
            if pat.search(_normalize(f"#{mid} {date} {text}")):
                out.append((1.0, mid, date, text))
        out.reverse()  # newest first
        return out[:topk] if topk else out

    def _recall_bm25(self, query: str, topk: int = 5,
                     use_index: bool = True) -> list[tuple[float, int, str, str]]:
        """Accent-normalized BM25 (optional, non-default).

        ``topk`` follows the same convention as every other recall helper:
        ``0`` (or any falsy value) means UNLIMITED — return the whole ranked
        list. ``recall_meta`` relies on this to count the full ranked set when
        the caller asked for a bounded ``topk``; a bare ``scored[:0]`` would
        silently report ``total_matches=0`` for a non-empty result.
        """
        if use_index and self._index_stale():
            self.build_index()
        docs = self._index_docs if use_index else self._all_records()
        if not docs:
            return []
        q = _tokenize(query)
        if not q:
            return []
        idf = self._index_idf if use_index else None
        avgdl = self._index_avgdl if use_index else None
        if not use_index:
            N = len(docs)
            df = defaultdict(int)
            for _, _, _, toks in docs:
                for t in set(toks):
                    df[t] += 1
            idf = {t: (N - df[t] + 0.5) / (df[t] + 0.5) for t in df}
            avgdl = sum(len(t) for _, _, _, t in docs) / N if N else 0
        k1, b = 1.5, 0.75
        scored = []
        for mid, date, text, toks in docs:
            dl = len(toks)
            tf = defaultdict(int)
            for t in toks:
                tf[t] += 1
            score = 0.0
            for term in set(q):
                if term not in idf:
                    continue
                f = tf.get(term, 0)
                score += idf[term] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
            if score > 0:
                scored.append((score, mid, date, text))
        scored.sort(reverse=True)
        return scored[:topk] if topk else scored

    # -- config / init / import (mirror memo config | init | import) --------

    KNOBS = {
        "WAKE_LINES": (96, "memories printed by wake"),
        "ENTRY_CHARS": (280, "longest one memory line, in bytes"),
        "RAW_MAX": (16, "blocks up to this many memories compress raw"),
        "PART_CHARS": (20000, "output paging: largest part, in bytes"),
        "PART_LINES": (500, "output paging: largest part, in lines"),
    }

    # Only these knobs are read by the engine at runtime (``wake`` honours
    # ``WAKE_LINES``; regex recall honours ``PART_CHARS``). The others are
    # memo-parity display values: they are shown and preserved for compatibility
    # but have NO runtime effect, so a change to one must be rejected before any
    # write instead of persisted as a silent no-op.
    SETTABLE_KNOBS = ("WAKE_LINES", "PART_CHARS")

    def read_config(self) -> dict[str, int]:
        """Read the per-store `config` file (mirrors memo's `config`)."""
        path = os.path.join(self.dir, "config")
        over = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    k, eq, v = line.partition("=")
                    if eq and k.strip() in self.KNOBS:
                        with contextlib.suppress(ValueError):
                            over[k.strip()] = int(v.strip())
        return over

    def write_config(self, over: dict[str, int]) -> None:
        """Write the per-store `config` file (mirrors memo's write_config)."""
        path = os.path.join(self.dir, "config")
        out = [
            "# OptMem sizes for this memory. A commented line means: follow the",
            "# tool's default. Edit with `optmem_config NAME=VALUE`.",
            "",
        ]
        for k, (default, what) in self.KNOBS.items():
            prefix = "" if k in over else "# "
            out.append(f"{prefix}{k:<12} = {over.get(k, default):<6} # {what}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)

    def init_store(self) -> bool:
        """Create the store deliberately (mirrors memo init). Returns True if fresh."""
        from pathlib import Path
        d = Path(self.dir)
        # Fresh means the store (LOG.txt) did not already exist. The TREE/
        # subdir is created eagerly by __init__, so don't key off is_dir().
        fresh = not (d / "LOG.txt").exists()
        (d / "TREE").mkdir(parents=True, exist_ok=True)
        open(os.path.join(d, "LOG.txt"), "a").close()
        if not (d / "config").exists():
            self.write_config({})
        return fresh

    def parse_import_lines(self, lines: list[str]) -> list[tuple[str, str]]:
        """Validate ``YYYY-MM-DD <text>`` lines without writing (atomic import).

        Raises ``ValueError`` with only the offending line number and reason.
        Kept separate so callers can validate, de-duplicate and report before
        appending anything.
        """
        recs = self._all_records()
        last = recs[-1][1] if recs else "0000-00-00"
        parsed: list[tuple[str, str]] = []
        for i, raw in enumerate(lines, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            date, _, text = line.partition(" ")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError(f"line {i}: expected 'YYYY-MM-DD <text>'")
            try:
                datetime.datetime.strptime(date, "%Y-%m-%d")
            except ValueError as err:
                raise ValueError(f"line {i}: date is not a real calendar date") from err
            if date < last:
                raise ValueError(f"line {i}: date precedes the previous memory")
            text = text.strip()
            if not text:
                raise ValueError(f"line {i}: empty text")
            byte_len = len(text.encode("utf-8"))
            if byte_len > ENTRY_CHARS:
                raise ValueError(f"line {i}: text exceeds the {ENTRY_CHARS}-byte limit")
            parsed.append((date, text))
            last = date
        return parsed

    def import_lines(self, lines: list[str]) -> int:
        """Bulk-append historical 'YYYY-MM-DD <text>' memories (mirrors memo import).
        Used once for bootstrapping an identity. Returns count appended.
        """
        parsed = self.parse_import_lines(lines)  # validate everything first, then write
        for date, text in parsed:
            self._append_raw(date, text)
        return len(parsed)

    def _append_raw(self, date: str, text: str) -> int:
        """Append a memory with an explicit date (used by import)."""
        with self._lock():
            mid = self.log_len()
            rec = f"#{mid} {date} {text}".encode()
            if len(rec) > LOG_REC - 1:
                raise ValueError(f"entry too long: {len(rec)} bytes")
            with open(os.path.join(self.dir, "LOG.txt"), "r+b") as f:
                f.seek(mid * LOG_REC)
                f.write(rec)
                f.write(b" " * (LOG_REC - len(rec) - 1))
                f.write(b"\n")
            self._index_len = -1
            return mid