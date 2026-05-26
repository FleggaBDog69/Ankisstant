# Persistent unified Knowledge Gaps queue.
# Stored in ankisstant/user_files/kg_queue.json.
#
# A KG can come from four sources:
#   - "manual"   — typed in via the ＋ KG button on the home screen or sidebar
#   - "analyse"  — sent over from the Analyse LO sub-feature
#   - "qbank"    — captured from the Capture missed Q dialog (was the legacy
#                  missed_queue.json store before unification)
#   - "browse"   — saved from Browse with Claude when nothing matched
#
# Statuses: "open" | "done" | "dismissed".

from __future__ import annotations

import json
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
    path = _queue_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
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
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ankisstant] kg_queue load failed: {e}")
        return []


def save_all(items: list[dict]) -> None:
    try:
        with open(_queue_path(), "w") as f:
            json.dump(items, f, indent=2)
    except OSError as e:
        print(f"[ankisstant] kg_queue save failed: {e}")


# ── normalisation ────────────────────────────────────────────────────────────

VALID_SOURCES = {"manual", "analyse", "qbank", "browse"}
VALID_STATUSES = {"open", "done"}
# Statuses that no longer exist but may still be in old saved data.
# "dismissed" used to be a separate top-level status; it now folds into
# status="done" plus the boolean `dismissed` flag (see _normalise).
LEGACY_STATUS_ALIASES = {"in_progress": "open"}


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
