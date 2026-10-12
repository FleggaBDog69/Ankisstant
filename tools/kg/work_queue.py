# The work-through queue — gaps parked for a run, kept between sessions.
# Stored in ankisstant/user_files/kg_work_queue.json.
#
# WHY IT IS A STORE AND NOT A LIST ON THE PANEL.
# The point of the queue is that you can send a gap to it, carry on reading,
# send three more, quit Anki, and still find the four waiting. A list held by
# KnowledgeGapsPanel dies with the panel — which is every time the Ankisstant
# window closes. So the queue lives on disk next to kg_queue.json and the panel
# only renders it.
#
# It holds IDS, never copies of the records. A queued gap that is renamed,
# regrouped or marked Done elsewhere must show its CURRENT state on the
# dashboard; a snapshot taken at queue time would quietly go stale, and the
# dashboard's whole job is telling you what is actually waiting.

from __future__ import annotations

from ...core import jsonfile
import os
from datetime import datetime

from . import store as kg_store


def _path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "kg_work_queue.json")


# ── load / save ──────────────────────────────────────────────────────────────

def _load_raw() -> list[dict]:
    data = jsonfile.read(_path(), [])
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for row in data:
        # Tolerate a bare list of ids — that's the shape a hand-edit or an
        # older build would most plausibly leave behind.
        if isinstance(row, str):
            row = {"id": row, "added_at": ""}
        if not isinstance(row, dict):
            continue
        kid = str(row.get("id") or "").strip()
        if not kid or kid in seen:
            continue
        seen.add(kid)
        out.append({"id": kid, "added_at": str(row.get("added_at") or "")})
    return out


def _save_raw(rows: list[dict]) -> None:
    jsonfile.write(_path(), rows, indent=2)


# ── reading ──────────────────────────────────────────────────────────────────

def ids() -> list[str]:
    """Queued ids, oldest first, minus any whose gap has been deleted.

    The prune is silent and not written back: a gap can only vanish from
    kg_queue.json by being deleted, and a queue entry pointing at nothing is
    noise rather than news."""
    return [r["id"] for r in _load_raw() if kg_store.get(r["id"])]


def entries() -> list[dict]:
    """(gap record, added_at) for everything queued, oldest first.

    Returns the LIVE record from the store, so a gap marked Done or regrouped
    since it was queued shows up here as it is now."""
    out: list[dict] = []
    for row in _load_raw():
        kg = kg_store.get(row["id"])
        if kg:
            out.append({"kg": kg, "added_at": row.get("added_at", "")})
    return out


def count() -> int:
    return len(ids())


def contains(kg_id: str) -> bool:
    return kg_id in set(ids())


# ── writing ──────────────────────────────────────────────────────────────────

def add(kg_ids: list[str]) -> tuple[int, int]:
    """Queue these gaps. Returns (added, already there).

    Adding something twice is a no-op rather than an error — sending the same
    group over again after adding one gap to it is a normal thing to do, and
    the answer should be "it's already waiting", not a duplicate row."""
    rows = _load_raw()
    have = {r["id"] for r in rows}
    now = datetime.now().isoformat(timespec="seconds")
    added = dupes = 0
    for kid in kg_ids:
        kid = str(kid or "").strip()
        if not kid:
            continue
        if kid in have:
            dupes += 1
            continue
        if not kg_store.get(kid):
            continue
        rows.append({"id": kid, "added_at": now})
        have.add(kid)
        added += 1
    if added:
        _save_raw(rows)
    return added, dupes


def remove(kg_ids: list[str]) -> int:
    drop = {str(k or "").strip() for k in kg_ids}
    rows = _load_raw()
    kept = [r for r in rows if r["id"] not in drop]
    if len(kept) != len(rows):
        _save_raw(kept)
    return len(rows) - len(kept)


def clear() -> int:
    n = len(_load_raw())
    _save_raw([])
    return n


def clear_done() -> int:
    """Drop the gaps that have since been closed. The queue is a to-do list;
    a gap you finished elsewhere has no business still sitting in it."""
    drop = [r["id"] for r in _load_raw()
            if (kg_store.get(r["id"]) or {}).get("status") == "done"]
    return remove(drop)
