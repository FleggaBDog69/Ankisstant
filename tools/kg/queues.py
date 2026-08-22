# Persistent Create / Browse queues.
#
# Both queues used to be plain lists on AnkisstantBody (ui/main_window.py),
# built empty on every profile open and dropped by _forget() on profile close.
# Queue forty gaps, crash, and they were gone — with no record anywhere that
# you had ever picked them, because a queued gap is only marked in_progress
# when a tool actually picks it up.
#
# So the lists become `Queue`: a list-like object that writes itself to
# user_files/queues.json whenever it changes. Nothing else had to move —
# the ~15 call sites in card_creator.py, browse.py, knowledge_gaps.py and
# kg/bulk_search.py keep using .append / .pop / .clear / slice assignment
# exactly as they did against a list.
#
# ── The one trap, written down so it isn't rediscovered the hard way ────────
# Most call sites read the queue as:
#
#     queue = getattr(main_window, "gap_queue", None) or []
#
# When the Queue is EMPTY that `or []` yields a throwaway plain list, so a
# mutation through `queue` would go nowhere. Every existing mutation site is
# safe from this — each one either uses the attribute directly, or returns
# early on an empty queue before mutating — but a new mutating call site MUST
# NOT be written with `or []`. Use the attribute, or guard on empty first.
#
# Storage is keyed by profile name: queues are per-profile (they hold KGs from
# that profile's collection), and Anki reuses one process across profile
# switches, so a single flat file would leak one profile's queue into another.

from __future__ import annotations

import json
import os

from ...core import log


# ── paths ────────────────────────────────────────────────────────────────────

def _user_files() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return uf


def _path() -> str:
    return os.path.join(_user_files(), "queues.json")


def _profile_key() -> str:
    """The current profile's name, or "" when Anki isn't up (tests, imports).

    Not a hash: the file is the user's own and a readable key makes it
    debuggable by hand."""
    try:
        from aqt import mw
        return str(mw.pm.name() if callable(mw.pm.name) else mw.pm.name)
    except Exception:
        return ""


# ── disk ─────────────────────────────────────────────────────────────────────

def _read_all() -> dict:
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warn(f"queues: load failed, starting empty: {e}")
        return {}


def _write_all(data: dict) -> None:
    # Write-then-rename: a force-quit partway through a write would otherwise
    # leave truncated JSON, and the next open would start empty — the exact
    # failure this module exists to prevent.
    path = _path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.warn(f"queues: save failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── the list-like queue ──────────────────────────────────────────────────────

class Queue:
    """A list that persists itself. Only the operations the call sites actually
    use are implemented — deliberately, so an unsupported one fails loudly
    rather than silently not saving."""

    def __init__(self, key: str, items: list | None = None):
        self._key = key
        self._items: list = list(items or [])

    # -- reads ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __contains__(self, x) -> bool:
        return x in self._items

    def __repr__(self) -> str:
        return f"Queue({self._key!r}, {len(self._items)} items)"

    def index(self, x) -> int:
        return self._items.index(x)

    def as_list(self) -> list:
        """A plain copy, for code that genuinely wants a snapshot."""
        return list(self._items)

    # -- writes --------------------------------------------------------------
    def append(self, item) -> None:
        self._items.append(item)
        self._touch()

    def extend(self, items) -> None:
        self._items.extend(items)
        self._touch()

    def insert(self, i: int, item) -> None:
        self._items.insert(i, item)
        self._touch()

    def pop(self, i: int = -1):
        item = self._items.pop(i)
        self._touch()
        return item

    def remove(self, item) -> None:
        self._items.remove(item)
        self._touch()

    def clear(self) -> None:
        self._items.clear()
        self._touch()

    def __setitem__(self, i, value) -> None:
        # Slice assignment is how browse._advance_queue_after_done filters the
        # queue in place (`queue[:] = [...]`).
        self._items[i] = value
        self._touch()

    def __delitem__(self, i) -> None:
        del self._items[i]
        self._touch()

    # -- persistence ---------------------------------------------------------
    def _touch(self) -> None:
        _schedule_save()


# ── the live pair ────────────────────────────────────────────────────────────
# Module-level rather than per-body: a pop-out rebuilds AnkisstantBody and
# adopts the same Queue objects, and the save path must not care which body is
# currently holding them.

_gap: Queue | None = None
_browse: Queue | None = None
_timer = None


def _prune(items: list, id_key: str) -> list:
    """Drop entries whose KG has been deleted since it was queued.

    Content is deliberately NOT refreshed from the store here. A gap queued by
    Mass AI Search carries the specific uncovered points its search found, in
    `notes` and `points` — re-reading the KG would overwrite exactly the thing
    that makes that entry worth generating from. The two call sites that DO
    want fresh content (browse._on_bulk_search, card_creator._on_mass_create_queue)
    already re-read for themselves.

    Entries with no id are manual queue additions typed straight into Create.
    They have no KG behind them, so there is nothing to check — they stay."""
    from . import store as kg_store
    # One load_all() for the whole queue: kg_store.get() re-reads the file per
    # call, which is fine for a lookup and quadratic for a sweep.
    live = {str(k.get("id") or "") for k in kg_store.load_all()}
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kid = str(it.get(id_key) or "").strip()
        if not kid or kid in live:
            out.append(it)          # no id = manually-typed entry, nothing to go stale
    return out


def load() -> tuple[Queue, Queue]:
    """Build both queues from disk. Safe to call more than once — the second
    call returns the same objects, so a pop-out can't split the queues in two."""
    global _gap, _browse
    if _gap is not None and _browse is not None:
        return _gap, _browse

    saved = _read_all().get(_profile_key()) or {}
    raw_gap = saved.get("gap") if isinstance(saved.get("gap"), list) else []
    raw_browse = saved.get("browse") if isinstance(saved.get("browse"), list) else []

    try:
        gap_items = _prune(raw_gap, "kg_id")
        browse_items = _prune(raw_browse, "id")
    except Exception as e:
        # A store read can fail before the collection is open. Better to restore
        # everything unpruned than to restore nothing.
        log.warn(f"queues: prune skipped ({e})")
        gap_items, browse_items = list(raw_gap), list(raw_browse)

    dropped = (len(raw_gap) - len(gap_items)) + (len(raw_browse) - len(browse_items))
    if dropped:
        log.info(f"queues: dropped {dropped} queued gap(s) whose KG was deleted")

    _gap = Queue("gap", gap_items)
    _browse = Queue("browse", browse_items)
    if dropped:
        save_now()
    return _gap, _browse


def save_now() -> None:
    """Write both queues to disk immediately. Called by the debounce timer and
    on profile close."""
    if _gap is None or _browse is None:
        return
    key = _profile_key()
    if not key:
        return                      # no profile — nothing to key the save on
    data = _read_all()
    data[key] = {"gap": _gap.as_list(), "browse": _browse.as_list()}
    _write_all(data)


def _schedule_save() -> None:
    """Coalesce a burst of mutations into one write.

    Bulk-sending 40 gaps is 40 appends; without this that's 40 file writes.
    Falls back to writing straight away when Qt isn't available, so the module
    still behaves correctly outside Anki."""
    global _timer
    try:
        from aqt.qt import QTimer
    except Exception:
        save_now()
        return
    try:
        if _timer is None:
            _timer = QTimer()
            _timer.setSingleShot(True)
            _timer.timeout.connect(save_now)
        _timer.start(250)
    except Exception as e:
        log.warn(f"queues: debounce unavailable, saving inline: {e}")
        save_now()


def forget() -> None:
    """Flush and drop the in-memory queues. Called on profile close, where the
    next profile must not inherit this one's queues.

    The flush is the point: the debounce timer may not have fired yet, and
    profile close is the last moment the data exists."""
    global _gap, _browse, _timer
    try:
        save_now()
    except Exception as e:
        log.warn(f"queues: final save failed: {e}")
    if _timer is not None:
        try:
            _timer.stop()
        except Exception:
            pass
    _timer = None
    _gap = None
    _browse = None
