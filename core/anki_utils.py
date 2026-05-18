# Shared mw.col helpers used by more than one tool.
# Tool-specific helpers (e.g. AJT grading) stay in the tool that uses them.

from __future__ import annotations

import html as _html
import re

from aqt import mw
from aqt.utils import tooltip


# ── lifecycle helpers ─────────────────────────────────────────────────────────

def require_col() -> bool:
    """True if the collection is loaded; otherwise shows a tooltip and returns False.
    Use at the top of any user-triggered handler that needs mw.col."""
    if mw is None or mw.col is None:
        try:
            tooltip("Anki collection not ready yet.")
        except Exception:
            pass
        return False
    return True


# ── text helpers ──────────────────────────────────────────────────────────────

def strip_html(s: str) -> str:
    """Plain-text from an HTML fragment. Squeezes whitespace and unescapes entities."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def strip_cloze(s: str) -> str:
    """{{c1::answer::hint}} → answer."""
    return re.sub(r"\{\{c\d+::([^:}]+)(?:::[^}]*)?\}\}", r"\1", s or "")


def slugify_tag_leaf(text: str, max_len: int = 36) -> str:
    """One piece of a tag → tag-safe leaf (no spaces, no separators)."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    if len(s) > max_len:
        s = s[:max_len].rstrip("_")
    return s


def build_tag(tag_root: str, *levels: str) -> str:
    """Compose <tag_root>::<level1>::<level2>::… Empty levels are skipped."""
    parts: list[str] = []
    root = (tag_root or "").strip().strip(":")
    if root:
        parts.append(root)
    for level in levels:
        leaf = slugify_tag_leaf(level)
        if leaf:
            parts.append(leaf)
    return "::".join(parts)


# ── note-search helpers ──────────────────────────────────────────────────────

def find_notes_by_query(query: str, limit: int | None = None) -> list[int]:
    if not query or mw.col is None:
        return []
    try:
        nids = list(mw.col.find_notes(query))
    except Exception as e:
        print(f"[ankisstant] find_notes failed for {query!r}: {e}")
        return []
    return nids[:limit] if limit else nids


def find_notes_for_terms(terms: list[str], limit: int = 30) -> list[int]:
    """OR-join the terms and return matching note ids (capped)."""
    if not terms:
        return []
    cleaned = [t.strip() for t in terms if t and t.strip()]
    if not cleaned:
        return []
    query = " OR ".join(f'"{t}"' for t in cleaned)
    return find_notes_by_query(query, limit=limit)


def note_preview(note_id: int, max_len: int = 140) -> tuple[str, str]:
    """Return (deck_short_name, plain-text preview of first non-empty field)."""
    if mw.col is None:
        return ("", "")
    try:
        note = mw.col.get_note(note_id)
    except Exception:
        return ("", "")

    raw = next((f for f in note.fields if f.strip()), "")
    plain = strip_html(raw)
    if len(plain) > max_len:
        plain = plain[: max_len - 1] + "…"

    deck = ""
    try:
        cids = note.card_ids()
        if cids:
            card = mw.col.get_card(cids[0])
            full = mw.col.decks.name(card.did)
            deck = full.split("::")[-1]
    except Exception:
        pass
    return (deck, plain)


def note_suspended_state(note) -> tuple[int, int]:
    """Returns (suspended_count, total_count) across the note's cards."""
    if mw.col is None:
        return (0, 0)
    cids = note.card_ids() if hasattr(note, "card_ids") else [c.id for c in note.cards()]
    total = len(cids)
    if total == 0:
        return (0, 0)
    susp = 0
    for cid in cids:
        try:
            if mw.col.get_card(cid).queue == -1:
                susp += 1
        except Exception:
            pass
    return (susp, total)


# ── write helpers ─────────────────────────────────────────────────────────────

def tag_notes(note_ids: list[int], tag: str) -> int:
    """Bulk-add `tag` to every note. Returns count tagged. No-op on empty input."""
    if not tag or not note_ids or mw.col is None:
        return 0
    try:
        mw.col.tags.bulk_add(list(note_ids), tag)
        return len(note_ids)
    except Exception as e:
        print(f"[ankisstant] tag_notes failed: {e}")
        return 0


def unsuspend_cards(cids: list[int]) -> bool:
    """Try several API spellings — Anki has moved this around between versions."""
    if not cids or mw.col is None:
        return True
    cids = list(cids)
    try:
        if hasattr(mw.col, "unsuspend_cards"):
            mw.col.unsuspend_cards(cids)
            return True
        sched = mw.col.sched
        if hasattr(sched, "unsuspend_cards"):
            sched.unsuspend_cards(cids)
            return True
        if hasattr(sched, "unsuspendCards"):
            sched.unsuspendCards(cids)
            return True
    except Exception as e:
        print(f"[ankisstant] unsuspend_cards failed: {e}")
        return False
    return False


def append_to_field(note_id: int, field_name: str, content: str) -> bool:
    """Append HTML content to a field with <br>---<br> separator.
    Returns False if the field is missing on the note."""
    if mw.col is None:
        return False
    try:
        note = mw.col.get_note(note_id)
        if field_name not in note:
            return False
        existing = note[field_name].strip()
        note[field_name] = (existing + "<br>---<br>" + content) if existing else content
        mw.col.update_note(note)
        return True
    except Exception as e:
        print(f"[ankisstant] append_to_field failed for note {note_id}: {e}")
        return False


# ── Add Cards dialog plumbing ─────────────────────────────────────────────────

def find_notetype(name: str):
    """By exact name; falls back to case-insensitive match. Returns the dict or None."""
    if not name or mw.col is None:
        return None
    nt = mw.col.models.by_name(name)
    if nt:
        return nt
    lname = name.lower()
    for m in mw.col.models.all_names_and_ids():
        if m.name.lower() == lname:
            return mw.col.models.get(m.id)
    return None


def find_deck_id(name: str) -> int | None:
    if not name or mw.col is None:
        return None
    try:
        deck = mw.col.decks.by_name(name)
        if deck:
            return deck["id"]
    except Exception:
        pass
    return None


def open_add_card_prefilled(
    fields: dict,
    notetype_name: str = "",
    deck_name: str = "",
    tag: str = "",
) -> None:
    """Open Anki's Add Cards dialog with explicit field/notetype/deck/tag setup.
    Missing fields are skipped silently."""
    try:
        import aqt
        addcards = aqt.dialogs.open("AddCards", mw)

        if notetype_name:
            nt = find_notetype(notetype_name)
            if nt:
                try:
                    addcards.notetype_chooser.selected_notetype_id = nt["id"]
                except Exception as e:
                    print(f"[ankisstant] couldn't set note type {notetype_name!r}: {e}")
            else:
                print(f"[ankisstant] note type {notetype_name!r} not found")

        if deck_name:
            did = find_deck_id(deck_name)
            if did is not None:
                try:
                    addcards.deck_chooser.selected_deck_id = did
                except Exception as e:
                    print(f"[ankisstant] couldn't set deck {deck_name!r}: {e}")
            else:
                print(f"[ankisstant] deck {deck_name!r} not found")

        note = addcards.editor.note
        if note:
            for fname, content in (fields or {}).items():
                if content and fname in note:
                    note[fname] = content
            if tag and tag not in note.tags:
                note.tags.append(tag)
            try:
                addcards.editor.loadNote()
            except Exception:
                pass

        addcards.show()
        addcards.raise_()
        addcards.activateWindow()
    except Exception as e:
        print(f"[ankisstant] open_add_card_prefilled failed: {e}")
