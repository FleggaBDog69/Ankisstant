# Patch inbox — apply edits to EXISTING knowledge gaps from outside Anki.
#
# kg_inbox.json solves half the problem: something running outside Anki (the
# dashboard daemon, the `life` CLI, a Claude session) can ADD gaps safely by
# appending to the inbox, because the add is replayed inside Anki on the next
# profile load.
#
# It cannot TICK ONE OFF. Marking a gap done means editing a record that is
# already in kg_queue.json, and store.update() is a read-modify-write of the
# whole file: load_all() ... save_all(). An external writer that edits the file
# between those two calls has its edit silently overwritten the moment the
# add-on next saves. So "just write kg_queue.json" is unsafe whenever Anki is
# open — which it usually is.
#
# The fix is the same shape as the inbox: don't write the queue from outside,
# write an INTENT and let Anki apply it. kg_patches.json is a flat JSON list of
# patches; this module applies them on the main thread using the store's own
# load/save, so there is no race with anything else the add-on is doing. A
# QTimer polls the file's mtime so a patch dropped while Anki is running lands
# within a couple of seconds — no restart, unlike the inbox.
#
# Patch shape (selector + the fields to change):
#
#   [{"id": "20985a30298e", "status": "done",
#     "fields": {"auto_tag": "!!Curtanki::Year_3::SRS::12::ERCP"}},
#    {"match_title": "Mirizzi Syndorme", "group": "SRS 12 Jaundice",
#     "status": "done"}]
#
# `id` is exact and preferred. `match_title` is the fallback for callers that
# never saw an id (a paste-in list, a script working from titles); it matches
# case-insensitively on the stripped title, optionally narrowed by `group`, and
# is REFUSED when it hits more than one gap rather than guessing which you meant.
#
# `fields` is MERGED into the existing fields blob, never swapped for it —
# setting auto_tag must not drop the notes someone typed on the same gap.

from __future__ import annotations

import os

from ...core import jsonfile
from . import store

POLL_MS = 2000


def _patch_path() -> str:
    return os.path.join(os.path.dirname(store._queue_path()), "kg_patches.json")


def _result_path() -> str:
    return os.path.join(os.path.dirname(store._queue_path()),
                        "kg_patches.result.json")


# Keys a patch may set directly. Deliberately excludes id/created_at (identity
# and history aren't the caller's to rewrite) and fields (merged separately).
PATCHABLE = {"title", "status", "dismissed", "group", "tags", "type",
             "resources"}


def _select(items: list[dict], patch: dict) -> tuple[list[int], str]:
    """Return the indices a patch applies to, plus an error string if unusable."""
    pid = str(patch.get("id") or "").strip()
    if pid:
        hits = [i for i, it in enumerate(items) if it.get("id") == pid]
        return (hits, "" if hits else f"no gap with id {pid}")

    title = str(patch.get("match_title") or "").strip().lower()
    if not title:
        return ([], "patch has neither id nor match_title")

    group = patch.get("group_match")
    group = str(group).strip().lower() if group is not None else None
    hits = []
    for i, it in enumerate(items):
        if str(it.get("title") or "").strip().lower() != title:
            continue
        if group is not None and str(it.get("group") or "").strip().lower() != group:
            continue
        hits.append(i)
    if not hits:
        return ([], f"no gap titled {title!r}")
    if len(hits) > 1:
        # Ambiguous: applying to all of them is the kind of "helpful" guess that
        # quietly ticks off the wrong gap. Make the caller narrow it instead.
        return ([], f"{len(hits)} gaps titled {title!r} — add group_match or use id")
    return (hits, "")


def apply_pending() -> list[dict]:
    """Apply every queued patch in ONE load/save pass. Returns per-patch results.

    One pass rather than a store.update() per patch: update() rewrites the whole
    file each call, which is 40 full rewrites when a batch ticks off a term's
    worth of gaps (the same reason store.set_group exists)."""
    path = _patch_path()
    claim = path + ".applying"
    # CLAIM, then read — see store.drain_inbox. Reading and truncating
    # afterwards lost any patch appended in between, and this runs every two
    # seconds while Anki is open. A claim left by a pass that died part-way is
    # applied first; the live file waits for the next tick.
    if not os.path.isfile(claim):
        if not os.path.isfile(path):
            return []
        try:
            os.replace(path, claim)
        except OSError as e:
            print(f"[ankisstant] kg patch claim failed: {e}")
            return []
    raw = jsonfile.read(claim, [])
    if not isinstance(raw, list) or not raw:
        _done(claim)
        return []

    items = store.load_all()
    results: list[dict] = []
    changed = 0

    for patch in raw:
        if not isinstance(patch, dict):
            results.append({"ok": False, "error": "patch is not an object"})
            continue
        idxs, err = _select(items, patch)
        if err:
            results.append({"ok": False, "error": err,
                            "patch": patch.get("id") or patch.get("match_title")})
            continue

        i = idxs[0]
        merged = dict(items[i])
        for key in PATCHABLE:
            if key in patch:
                merged[key] = patch[key]
        incoming = patch.get("fields")
        if isinstance(incoming, dict):
            fields = dict(merged.get("fields") or {})
            fields.update(incoming)
            merged["fields"] = fields
        items[i] = store._normalise(merged)
        changed += 1
        results.append({"ok": True, "id": items[i].get("id"),
                        "title": items[i].get("title"),
                        "status": items[i].get("status")})

    if changed:
        store.save_all(items)
    jsonfile.write(_result_path(), results, indent=2)
    _done(claim)
    return results


def _done(claim: str) -> None:
    """Drop the claimed file. Writers create a fresh [] when it's missing."""
    try:
        os.remove(claim)
    except OSError as e:
        print(f"[ankisstant] kg patch cleanup failed: {e}")


# ── live watcher ─────────────────────────────────────────────────────────────

_timer = None
_seen: tuple[float, int] | None = None


def _stamp(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _tick() -> None:
    global _seen
    # Developer screenshots ride the same two-second tick (tools/devscreens.py).
    try:
        from .. import devscreens
        devscreens.check()
    except Exception as e:
        print(f"[ankisstant] dev screens check failed: {e}")
    path = _patch_path()
    stamp = _stamp(path)
    if stamp is None or stamp == _seen:
        return
    _seen = stamp
    try:
        applied = apply_pending()
    except Exception as e:
        print(f"[ankisstant] kg patch apply failed: {e}")
        return
    if not applied:
        _seen = _stamp(path)
        return
    ok = sum(1 for r in applied if r.get("ok"))
    print(f"[ankisstant] applied {ok}/{len(applied)} kg patch(es)")
    _seen = _stamp(path)
    # Repaint the queue if it's on screen, so a gap ticked off from outside
    # doesn't sit there looking open until the next manual refresh.
    try:
        from .. import knowledge_gaps as _kg
        if getattr(_kg, "_panel", None) is not None:
            _kg._panel.refresh_list()
    except Exception:
        pass


def start_watcher() -> None:
    """Poll for patches dropped while Anki is running. Idempotent."""
    global _timer, _seen
    if _timer is not None:
        return
    from aqt import mw
    _seen = _stamp(_patch_path())
    _timer = mw.progress.timer(POLL_MS, _tick, True, False)
