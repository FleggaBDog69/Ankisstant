# Which gaps are ticked — kept between sessions.
# Stored in ankisstant/user_files/kg_ticks.json.
#
# WHY A TICK IS ON DISK AND NOT IN THE PANEL.
# A tick is a mark you put on a gap, not a highlight. You tick four gaps over
# an afternoon's reading, quit Anki, come back and expect to find four gaps
# ticked — the same way the Closed box on a gap is still ticked when you come
# back. A set held by the panel dies with the panel, which is every time the
# Ankisstant window closes, so the panel renders this instead of owning it.
#
# IT HOLDS IDS, and it is pruned only against the store. A filter or a group
# switch hides rows; it does not untick them. The pages decide what a hidden
# tick may ACT on — see _selected_ids / gap_ids, which read the rows on the
# page — but nothing here throws a tick away for being out of sight.

from __future__ import annotations

import json
import os

from . import store as kg_store


def _path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "kg_ticks.json")


def ids() -> set[str]:
    """The ticked ids, minus any whose gap has been deleted.

    The prune is silent: a gap can only leave kg_queue.json by being deleted,
    and a tick on nothing is noise rather than news."""
    path = _path()
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ankisstant] kg_ticks load failed: {e}")
        return set()
    if not isinstance(data, list):
        return set()
    out: set[str] = set()
    for row in data:
        kid = str(row or "").strip()
        if kid and kid not in out and kg_store.get(kid):
            out.add(kid)
    return out


def save(kg_ids) -> None:
    """Replace the saved ticks. The panel always hands over its whole set, so
    this is a write of the truth rather than a patch of it."""
    rows = sorted({str(k or "").strip() for k in kg_ids if str(k or "").strip()})
    try:
        with open(_path(), "w") as f:
            json.dump(rows, f, indent=2)
    except OSError as e:
        print(f"[ankisstant] kg_ticks save failed: {e}")


def count() -> int:
    return len(ids())


def clear() -> int:
    n = count()
    save([])
    return n
