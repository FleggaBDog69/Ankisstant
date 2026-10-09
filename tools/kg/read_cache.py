# Per-GAP cache of the READ stage.
#
# Reading is the slow half of every KG run: 33 gaps measured 323s even after the
# sort/decompose split, because output tokens generate serially however you
# batch them. Almost none of that work is new — a gap read on Tuesday reads
# identically on Thursday unless it was edited.
#
# Keyed per GAP and never per batch, which is the entire point: re-running 32 of
# yesterday's 33 gaps pays for the one that changed and nothing else. A batch
# key would have missed on all 33, which is exactly the case that hurts — a run
# that died halfway (a session limit, a Stop) currently costs full price to
# repeat.
#
# What is cached is the RAW merged object the model returned, not the finished
# KGPlan: the clamps, the locks and the per-point cap are applied after the
# cache, so changing a setting re-shapes an old reading instead of stranding it.
#
# What is NOT cached is a gap whose decomposition failed. A point-less object is
# the failure marker downstream, and caching it would make a quota blip
# permanent.

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime

from ...core import log

_VERSION = 1
_LOCK = threading.Lock()


def _path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "kg_read_cache.json")


def _cfg(name: str, default):
    try:
        from ...core.config import load_config
        val = load_config().get(name)
        return default if val is None else val
    except Exception:
        return default


def enabled() -> bool:
    return bool(_cfg("read_cache", True))


def _load() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {"version": _VERSION, "entries": {}}
    try:
        with open(p, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            # A version bump throws the lot away rather than trying to read an
            # older shape: the cost of a miss is one AI call, the cost of
            # mis-reading a stale shape is a wrong plan.
            return {"version": _VERSION, "entries": {}}
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data
    except Exception as e:
        log.warn(f"kg read cache: unreadable, starting fresh ({e})")
        return {"version": _VERSION, "entries": {}}


def _save(data: dict) -> None:
    p = _path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception as e:
        log.warn(f"kg read cache: couldn't save ({e})")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def prompt_signature(*parts: str) -> str:
    """A short hash of the system prompts a read used.

    Every prompt edit — including the one the restate-concept setting makes —
    changes this, so a prompt change invalidates the cache instead of quietly
    serving readings written under the old brief."""
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


def key(*, gap_line: str, flow: str, model, prompt_sig: str) -> str:
    """The cache key for ONE gap.

    `gap_line` is the exact line the gap contributes to the prompt, which is
    what makes an edit to its text, its notes or its locks a miss — the key is
    derived from what the model would actually be shown, not from the gap's id.
    """
    h = hashlib.sha1()
    for part in (str(flow), str(model or ""), prompt_sig, gap_line):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def get_many(keys: list[str]) -> dict[str, dict]:
    """Whatever of `keys` is cached. Missing keys are simply absent."""
    if not keys or not enabled():
        return {}
    with _LOCK:
        data = _load()
        entries = data["entries"]
        out: dict[str, dict] = {}
        touched = False
        for k in keys:
            e = entries.get(k)
            obj = (e or {}).get("obj")
            if isinstance(obj, dict):
                out[k] = obj
                # Microseconds, not seconds: the prune orders by this, and a
                # whole batch read in the same second would order arbitrarily.
                e["used"] = datetime.now().isoformat(timespec="microseconds")
                e["hits"] = int(e.get("hits") or 0) + 1
                touched = True
        if touched:
            _save(data)
        return out


def put_many(items: dict[str, dict], gap_ids: dict[str, str] | None = None) -> None:
    """Store readings. A point-less object is dropped on the floor — see the
    header: that is a failed decompose, not a reading."""
    items = {k: v for k, v in (items or {}).items()
             if isinstance(v, dict) and v.get("points")}
    if not items or not enabled():
        return
    cap = 2000
    try:
        cap = max(50, int(_cfg("read_cache_max", 2000)))
    except Exception:
        pass
    now = datetime.now().isoformat(timespec="microseconds")
    with _LOCK:
        data = _load()
        entries = data["entries"]
        for k, obj in items.items():
            o = dict(obj)
            o.pop("n", None)       # the batch position is not part of a reading
            entries[k] = {"obj": o, "ts": now, "used": now, "hits": 0,
                          # Which gap this reading belongs to, so a "read it
                          # again" can find every entry for it — the key itself
                          # is a hash of the prompt line, so it can't be
                          # searched for, and one gap can have several (search
                          # and create, with locks and without).
                          "gap": str((gap_ids or {}).get(k) or "")}
        if len(entries) > cap:
            # Oldest USE first, so the gaps you keep coming back to survive a
            # prune and the one-offs are what goes.
            order = sorted(entries.items(),
                           key=lambda kv: str(kv[1].get("used") or kv[1].get("ts") or ""))
            for k, _ in order[:len(entries) - cap]:
                entries.pop(k, None)
        _save(data)


def drop(keys: list[str]) -> int:
    """Forget these readings, so the next run reads them fresh. The escape
    hatch for a gap the model read badly: without it a bad reading would be
    served for as long as the gap's wording stayed the same."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return 0
    with _LOCK:
        data = _load()
        entries = data["entries"]
        n = 0
        for k in keys:
            if entries.pop(k, None) is not None:
                n += 1
        if n:
            _save(data)
        return n


def drop_for_gaps(gap_ids: list[str]) -> int:
    """Forget every reading belonging to these gaps, whichever flow, brief or
    lock set produced it. The escape hatch for a gap the model read badly:
    without it a bad reading would be served for as long as the gap's wording
    stayed the same."""
    want = {str(g) for g in (gap_ids or []) if g}
    if not want:
        return 0
    with _LOCK:
        data = _load()
        entries = data["entries"]
        doomed = [k for k, e in entries.items()
                  if str((e or {}).get("gap") or "") in want]
        for k in doomed:
            entries.pop(k, None)
        if doomed:
            _save(data)
        return len(doomed)


def clear() -> int:
    """Empty it. Returns how many readings were dropped."""
    with _LOCK:
        data = _load()
        n = len(data["entries"])
        data["entries"] = {}
        _save(data)
        return n


def stats() -> dict:
    with _LOCK:
        data = _load()
        entries = data["entries"]
        return {
            "count": len(entries),
            "hits": sum(int((e or {}).get("hits") or 0) for e in entries.values()),
            "path": _path(),
        }
