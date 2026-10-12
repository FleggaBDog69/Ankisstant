# Persistent unified Knowledge Gaps queue.
# Stored in ankisstant/user_files/kg_queue.json.
#
# A KG can come from seven sources:
#   - "manual"   — typed in via the ＋ KG button on the home screen or sidebar
#   - "analyse"  — sent over from the Analyse LO sub-feature
#   - "qbank"    — captured from the Capture missed Q dialog (was the legacy
#                  missed_queue.json store before unification)
#   - "browse"   — saved from Browse with Claude when nothing matched
#   - "chat"     — condensed out of a SynapsePro AI chat answer via the ＋KG
#                  button in its chat header (core/synapse_chat.py)
#   - "quick"    — captured outside Anki (dashboard command box, `life` CLI)
#   - "things"   — swept out of a "KGs" heading in Things 3
#
# The last two arrive through user_files/kg_inbox.json rather than being
# written straight into the queue — see drain_inbox() at the bottom.
#
# Statuses: "open" | "done" | "dismissed".

from __future__ import annotations

import json
from ...core import jsonfile
import os
import uuid
from datetime import datetime
from typing import Callable


# ── paths ─────────────────────────────────────────────────────────────────────

def _user_files() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return uf


def _queue_path() -> str:
    return os.path.join(_user_files(), "kg_queue.json")


def _legacy_missed_path() -> str:
    return os.path.join(_user_files(), "missed_queue.json")


def _legacy_migrated_path() -> str:
    return os.path.join(_user_files(), "missed_queue.migrated.json")


# ── load / save ──────────────────────────────────────────────────────────────

def load_all() -> list[dict]:
    # jsonfile.read sets an unreadable file aside rather than letting it read
    # as "no gaps" — the next save would otherwise write that over the lot.
    data = jsonfile.read(_queue_path(), [])
    try:
        if not isinstance(data, list):
            return []
        # One-shot in-memory migration: legacy status="dismissed" gets
        # folded into status="done" + dismissed=True. We don't write back
        # on every read — the next update() on each record persists it.
        out: list[dict] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            if it.get("status") == "dismissed":
                it = dict(it)
                it["status"] = "done"
                it["dismissed"] = True
            out.append(it)
        return out
    except (TypeError, AttributeError) as e:
        print(f"[ankisstant] kg_queue load failed: {e}")
        return []


def save_all(items: list[dict]) -> None:
    jsonfile.write(_queue_path(), items, indent=2)


# ── normalisation ────────────────────────────────────────────────────────────

VALID_SOURCES = {"manual", "analyse", "qbank", "browse", "chat",
                 "quick", "things"}
# "in_progress" is set when a gap is handed to Browse or Create: it's been
# picked up but nothing has come of it yet. It exists so a queue of 40 gaps
# doesn't look untouched after you've sent half of them off — the Open filter
# is then genuinely "not started".
VALID_STATUSES = {"open", "in_progress", "done"}
# Statuses that no longer exist but may still be in old saved data.
# "dismissed" used to be a separate top-level status; it now folds into
# status="done" plus the boolean `dismissed` flag (see _normalise).
LEGACY_STATUS_ALIASES: dict[str, str] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_type_for_source(source: str) -> str:
    return {"qbank": "mq", "analyse": "lo"}.get(source, "kg")


LEGACY_FIELD_KEYS = (
    "notes", "stem_html", "system", "subsystem", "topic",
    "platform", "lo", "lo_tag",
)


def _normalise(item: dict) -> dict:
    """Fill in defaults / coerce types so callers can pass partial dicts.

    Type-specific content lives in `fields: {key: value}`. Legacy top-level
    keys (notes, stem_html, system, ...) get promoted into `fields` on read
    so older entries continue to display."""
    out = dict(item)
    out.setdefault("id", uuid.uuid4().hex[:12])
    out["title"] = str(out.get("title", "")).strip()
    src = out.get("source", "manual")
    out["source"] = src if src in VALID_SOURCES else "manual"
    st = out.get("status", "open")
    st = LEGACY_STATUS_ALIASES.get(st, st)
    # Migration: legacy "dismissed" status collapses into status=done with
    # the boolean dismissed flag set. Anything coming in already-flagged
    # keeps its flag.
    dismissed_flag = bool(out.get("dismissed", False))
    if st == "dismissed":
        st = "done"
        dismissed_flag = True
    out["status"] = st if st in VALID_STATUSES else "open"
    out["dismissed"] = dismissed_flag if out["status"] == "done" else False
    # Type is a free-form slug (configurable in settings). Default keyed off
    # source so legacy entries get a sensible label.
    tp = str(out.get("type") or "").strip().lower()
    if not tp:
        tp = _default_type_for_source(out["source"])
    out["type"] = tp

    # Group — free-text label naming the batch this gap belongs to ("SRS 4",
    # "Week 3", "Cardio revision"). Blank means ungrouped.
    #
    # A TOP-LEVEL key rather than an entry in `fields`, deliberately: fields are
    # per-type content, and a group has to be filterable, sortable and
    # bulk-assignable across every type at once. It's also a third axis, not a
    # rename of an existing one — status is how far along a gap is, type is what
    # kind of thing it is, group is which batch of work it came in with.
    #
    # There is no managed list of group objects. The set of live groups is
    # derived (see groups() below), because a managed list means a second CRUD
    # screen to maintain and the entire point here is less management.
    out["group"] = str(out.get("group") or "").strip()

    tags = out.get("tags") or []
    out["tags"] = [str(t).strip() for t in tags if str(t).strip()]

    # Fields blob — promote legacy top-level keys.
    fields = dict(out.get("fields") or {})
    for legacy in LEGACY_FIELD_KEYS:
        if legacy in out:
            val = out.pop(legacy)
            if legacy not in fields and val not in (None, "", []):
                fields[legacy] = val
    # Coerce every value to a string for storage. Lists/dicts are JSON-
    # serialised so they round-trip; we keep resources as a separate list.
    cleaned_fields = {}
    for k, v in fields.items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, (list, dict)):
            cleaned_fields[key] = v
        elif v is None:
            cleaned_fields[key] = ""
        else:
            cleaned_fields[key] = str(v)
    out["fields"] = cleaned_fields

    # Resources stays top-level (it's a list of dicts, used regardless of type).
    resources = out.get("resources") or []
    cleaned: list[dict] = []
    for r in resources:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label", "") or "").strip()
        url   = str(r.get("url", "") or "").strip()
        if label or url:
            cleaned.append({"label": label, "url": url})
    out["resources"] = cleaned

    out.setdefault("created_at", _now())
    out["updated_at"] = _now()
    return out


def field(kg: dict, key: str, default: str = "") -> str:
    """Return a field value, supporting both new `fields[key]` and legacy
    top-level keys. Read-only — callers should write into `fields/`."""
    fields = kg.get("fields") or {}
    val = fields.get(key)
    if val in (None, ""):
        # Backward-compat: legacy entries that haven't been normalised yet.
        val = kg.get(key, default)
    return val if val is not None else default


# ── groups ───────────────────────────────────────────────────────────────────

def group_sort_key(name: str):
    """Sort groups the way a human reads them: SRS 2 before SRS 12.

    A plain alphabetical sort puts "SRS 12" above "SRS 2", which is wrong
    everywhere a group is shown. Splitting digit runs out and comparing them
    numerically fixes it without needing the labels to follow any scheme."""
    import re as _re
    parts = _re.split(r"(\d+)", (name or "").strip().lower())
    return [(1, int(p)) if p.isdigit() else (0, p) for p in parts if p != ""]


def groups(items: list[dict] | None = None) -> list[str]:
    """Every group in use, naturally sorted. Derived, never stored — deleting
    the last gap in a group is how a group stops existing."""
    src = items if items is not None else load_all()
    names = {str(it.get("group") or "").strip()
             for it in src if isinstance(it, dict)}
    names.discard("")
    return sorted(names, key=group_sort_key)


def in_group(name: str, items: list[dict] | None = None) -> list[dict]:
    """Every KG in `name`. Pass name="" for the ungrouped ones."""
    src = items if items is not None else load_all()
    want = (name or "").strip()
    return [it for it in src
            if isinstance(it, dict) and str(it.get("group") or "").strip() == want]


def set_group(item_ids: list[str], name: str) -> int:
    """Assign (or, with name="", clear) the group on many KGs in one pass.

    One load/save for the whole selection: update() re-reads and re-writes the
    file per call, which is 40 full rewrites when you group a term's worth of
    LOs. Returns how many actually changed."""
    want = (name or "").strip()
    wanted = set(item_ids or [])
    if not wanted:
        return 0
    items = load_all()
    changed = 0
    for i, it in enumerate(items):
        if it.get("id") not in wanted:
            continue
        if str(it.get("group") or "").strip() == want:
            continue
        merged = dict(it)
        merged["group"] = want
        items[i] = _normalise(merged)
        changed += 1
    if changed:
        save_all(items)
    return changed


# ── CRUD ─────────────────────────────────────────────────────────────────────

def get(item_id: str) -> dict | None:
    for it in load_all():
        if it.get("id") == item_id:
            return it
    return None


def add(**fields) -> dict:
    item = _normalise(fields)
    items = load_all()
    items.append(item)
    save_all(items)
    return item


def add_many(items: list[dict]) -> list[dict]:
    if not items:
        return []
    existing = load_all()
    new = [_normalise(it) for it in items]
    existing.extend(new)
    save_all(existing)
    return new


def update(item_id: str, **fields) -> dict | None:
    items = load_all()
    for i, it in enumerate(items):
        if it.get("id") != item_id:
            continue
        merged = dict(it)
        merged.update(fields)
        items[i] = _normalise(merged)
        save_all(items)
        return items[i]
    return None


def remove(item_id: str) -> None:
    save_all([it for it in load_all() if it.get("id") != item_id])


def remove_many(item_ids) -> int:
    """Drop several at once. ONE read-modify-write, not one per id: save_all
    rewrites the whole file, so a loop of remove() over forty gaps is forty
    full rewrites and forty chances for a concurrent write to lose the rest."""
    wanted = {str(i) for i in (item_ids or []) if i}
    if not wanted:
        return 0
    items = load_all()
    keep = [it for it in items if str(it.get("id")) not in wanted]
    removed = len(items) - len(keep)
    if removed:
        save_all(keep)
    return removed


def count(filter_fn: Callable[[dict], bool] | None = None) -> int:
    items = load_all()
    if filter_fn is None:
        return len(items)
    return sum(1 for it in items if filter_fn(it))


# ── one-shot migration from legacy missed_queue.json ─────────────────────────

def migrate_from_missed_queue() -> int:
    """Move every item from user_files/missed_queue.json into kg_queue.json
    as a source=qbank KG, then rename the legacy file to
    missed_queue.migrated.json so this is idempotent.

    Returns the number of items migrated (0 if nothing to do)."""
    legacy = _legacy_missed_path()
    if not os.path.isfile(legacy):
        return 0
    try:
        with open(legacy, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ankisstant] kg migration: couldn't read legacy queue: {e}")
        return 0
    if not isinstance(raw, list) or not raw:
        # Nothing to do, but still archive the empty file so we don't retry.
        try:
            os.rename(legacy, _legacy_migrated_path())
        except OSError:
            pass
        return 0

    converted: list[dict] = []
    for old in raw:
        if not isinstance(old, dict):
            continue
        stem    = str(old.get("stem", "") or "")
        concept = str(old.get("concept", "") or "").strip()
        title   = concept
        if not title:
            import re as _re
            plain = _re.sub(r"<[^>]+>", " ", stem)
            plain = _re.sub(r"\s+", " ", plain).strip()
            title = plain[:60] or "(captured)"
        converted.append({
            "title":      title,
            "source":     "qbank",
            "type":       "mq",
            "status":     "open",
            "tags":       [],
            "resources":  [],
            "fields": {
                "concept":   concept,
                "stem_html": stem,
                "system":    str(old.get("system", "") or ""),
                "subsystem": str(old.get("subsystem", "") or ""),
                "topic":     str(old.get("topic", "") or ""),
                "platform":  str(old.get("platform", "") or ""),
                "notes":     "",
            },
            "created_at": str(old.get("captured_at") or _now()),
        })

    if not converted:
        try:
            os.rename(legacy, _legacy_migrated_path())
        except OSError:
            pass
        return 0

    add_many(converted)
    try:
        os.rename(legacy, _legacy_migrated_path())
    except OSError as e:
        print(f"[ankisstant] kg migration: rename legacy file failed: {e}")
    return len(converted)


# ── inbox drain (captures made while Anki was closed) ────────────────────────

def _inbox_path() -> str:
    return os.path.join(_user_files(), "kg_inbox.json")


def drain_inbox(*, groups_out: list | None = None) -> int:
    """Move anything captured outside Anki into the queue, then clear it.

    The dashboard daemon (lifed) and the `life` CLI can't write kg_queue.json
    safely: save_all() rewrites the whole file, so an external append made
    while Anki holds the queue in memory would be silently lost the next time
    the add-on saves. They append to kg_inbox.json instead and we drain it
    here, on profile load, when nothing else is touching the queue.

    Deduped on fields.things_uuid so a Things gap that was swept more than
    once doesn't land twice. Returns the number added.

    `groups_out`, if given, is filled with the group names the drained gaps
    landed in. This module deliberately knows nothing about the add-on config,
    so it can't un-archive a put-away group itself — it hands the caller the
    names instead (see knowledge_gaps.wake_groups)."""
    path = _inbox_path()
    claim = path + ".draining"
    added = 0
    # A claim left by a drain that died part-way is finished first.
    if os.path.isfile(claim):
        added += _drain_claim(claim, groups_out)
    # CLAIM, then read. Reading the inbox and truncating it afterwards lost
    # anything appended in between; renaming it first means a writer that
    # comes along mid-drain simply starts a fresh inbox (they all create []
    # when it's missing) and is picked up next time.
    if os.path.isfile(path):
        try:
            os.replace(path, claim)
        except OSError as e:
            print(f"[ankisstant] kg inbox claim failed: {e}")
            return added
        added += _drain_claim(claim, groups_out)
    return added


def _drain_claim(claim: str, groups_out: list | None) -> int:
    raw = jsonfile.read(claim, [])
    if not isinstance(raw, list):
        raw = []

    seen = {
        (it.get("fields") or {}).get("things_uuid")
        for it in load_all()
        if (it.get("fields") or {}).get("things_uuid")
    }
    fresh = []
    for it in raw:
        if not isinstance(it, dict) or not str(it.get("title", "")).strip():
            continue
        tu = (it.get("fields") or {}).get("things_uuid")
        if tu and tu in seen:
            continue
        if tu:
            seen.add(tu)
        fresh.append(it)

    if fresh:
        add_many(fresh)
        if groups_out is not None:
            groups_out.extend(
                str(it.get("group") or "").strip() for it in fresh)
    try:
        os.remove(claim)
    except OSError as e:
        print(f"[ankisstant] kg inbox cleanup failed: {e}")
    return len(fresh)
