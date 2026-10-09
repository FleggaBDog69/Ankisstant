# ── Write your own card, in Anki's real Add window ───────────────────────────
#
# The old "Write your own" was a self-contained dialog with a Front box and a
# Back box. It committed correctly, but two boxes is not an editor: no cloze
# button, no bold/italic, no image paste, no occlusion, no sticky fields, no
# HTML view, none of the shortcuts already in the hands.
#
# Anki's Add window has all of that and is not ours to rebuild. What it does
# NOT have is the gap — what you wrote down at the time, the resources you
# filed against it, the tag the card should carry, and the fact that closing
# the gap is the point of writing the card. So the split is: Anki's window does
# the typing, a slim Ankisstant window beside it holds the gap, and dock_beside
# glues them together so they move, resize and close as one.
#
# Nothing is reparented. Anki's Add window stays an ordinary Anki window with
# its own close/save behaviour, its own saved geometry and its own place in
# aqt.dialogs — press "A" in the main window and you get the same instance,
# side-car and all.
#
# The note handed to the editor is built by card_creator.set_fields with an
# EMPTY card, so the gap's stem, notes, images, resources and tags land in
# exactly the fields a GENERATED card would have used. A hand-written card and
# an AI one are then the same shape, which is the only way the audit tags mean
# anything.
#
# A run of gaps does NOT advance by itself. Writing two cards for one gap is
# normal, and a window that jumps to the next gap the moment you press Add
# takes that away — so an Add offers the next gap and waits to be asked.

from __future__ import annotations

from aqt import mw, dialogs, gui_hooks
from aqt.qt import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, Qt, QTimer, QVBoxLayout,
)
from aqt.utils import tooltip

from ...core import anki_utils, log
from ...core.config import month_tag, tool_config
from ...core.qt_utils import attach_tag_completer, dock_beside, theme_dialog
from .. import autotag
from . import store as kg_store

import html as _html


# One side-car at a time: it is bound to whichever note the single shared Add
# window is currently showing, and two of them would fight over that note.
_live: "_WriteOwnSidecar | None" = None
# WHICH side-car owns the add-note hook. A plain _hooked flag let a stale
# side-car's close pull the hook out from under the live one — which is exactly
# what stopped gaps ticking themselves done.
_hook_owner: "_WriteOwnSidecar | None" = None


def open_write_own(kgs: list, parent=None) -> None:
    """Open Anki's Add window primed for these gaps, with the side-car beside it.

    Takes a LIST because a run of gaps is the normal case — you sit down with
    six things you didn't know and write a card for each. The side-car walks
    them in order, one Add at a time, and never moves on unasked.
    """
    global _live
    if not anki_utils.require_col():
        return
    gaps = [g for g in (kgs or []) if isinstance(g, dict) and g.get("id")]
    if not gaps:
        tooltip("No gap to write a card for.")
        return
    if _live is not None:
        try:
            _live.close()          # deletes it: WA_DeleteOnClose is set
        except Exception:
            pass
        _live = None
    sidecar = _WriteOwnSidecar(gaps, parent=parent)
    _live = sidecar
    sidecar.start()


def _on_note_added(note) -> None:
    if _live is None:
        return

    def deliver() -> None:
        sc = _live                 # re-read: it may have closed in between
        if sc is None:
            return
        try:
            sc.note_added(note)
        except RuntimeError:
            # The Qt object went with its parent window without a closeEvent
            # (Anki's Add window deleting its children). Tidy up here instead,
            # or the hook keeps firing into a corpse.
            _forget(sc)
        except Exception as e:
            log.error(f"write-your-own: post-add handling failed: {e}")

    # Deferred so Anki's own post-Add reset (which blanks the editor) finishes
    # before we re-prime it.
    QTimer.singleShot(0, deliver)


def _forget(sidecar) -> None:
    """Drop every module-level reference to a side-car that is already gone."""
    global _live
    _detach_hook(sidecar)
    if _live is sidecar:
        _live = None


def _attach_hook(owner) -> None:
    global _hook_owner
    if _hook_owner is None:
        gui_hooks.add_cards_did_add_note.append(_on_note_added)
    _hook_owner = owner


def _detach_hook(owner) -> None:
    """Only the side-car that owns the hook may remove it.

    Without the ownership test, closing a side-car left over from an earlier
    run — one that should not have been on screen at all — silently unhooked
    the one you were actually using, and the next card you added left its gap
    open."""
    global _hook_owner
    if _hook_owner is not owner:
        return
    try:
        gui_hooks.add_cards_did_add_note.remove(_on_note_added)
    except ValueError:
        pass
    _hook_owner = None


class _WriteOwnSidecar(QDialog):
    """The gap, beside Anki's Add window. Never edits the card itself."""

    def __init__(self, gaps: list, parent=None):
        # Qt.Tool + the Add window as parent keeps it above its host and off the
        # taskbar; dock_beside does the rest.
        super().__init__(parent, Qt.WindowType.Tool)
        # Destroyed on close, not merely hidden. A hidden side-car still holds
        # an event filter on the SHARED Add window, and the next time that
        # window moved it put itself back on screen — a fresh ghost beside the
        # editor for every gap already written.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._gaps = list(gaps)
        self._i = 0
        self._added: list = []
        self._closed_ids: list = []
        # Why the last tick didn't take, if it didn't. Reported next to "card
        # added" rather than in a status line that gets overwritten a line later.
        self._tick_error: str = ""
        self._ac = None
        self._dock = None
        self.setWindowTitle("Ankisstant — the gap")
        self.setMinimumWidth(300)
        self.resize(340, 620)
        self._build()

    # ── layout ──────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        self._counter = QLabel("")
        self._counter.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(self._counter)

        self._title = QLabel("")
        self._title.setWordWrap(True)
        self._title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._title)

        self._ref = QTextEdit()
        self._ref.setReadOnly(True)
        root.addWidget(self._ref, 1)

        # What the gap is sending to the card, said out loud. The resources are
        # already on the note in the editor by the time this is read; showing
        # them here is how you know that without hunting through Extra.
        self._res = QLabel("")
        self._res.setWordWrap(True)
        self._res.setOpenExternalLinks(True)
        self._res.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(self._res)

        root.addWidget(QLabel("Tag"))
        self._tag = QLineEdit()
        self._tag.setPlaceholderText("no tag")
        attach_tag_completer(self._tag, multi=False)
        # Typing here retags the note already open in the editor, so the box on
        # screen and the note about to be added never disagree.
        self._tag.editingFinished.connect(self._push_tag)
        root.addWidget(self._tag)

        self._done_cb = QCheckBox("Mark the gap done when a card is added")
        self._done_cb.setChecked(True)
        root.addWidget(self._done_cb)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        root.addWidget(self._build_prompt())

        row = QHBoxLayout()
        self._skip_btn = QPushButton("Skip →")
        self._skip_btn.setAutoDefault(False)
        self._skip_btn.setToolTip("Leave this gap open and load the next one "
                                  "into the Add window.")
        self._skip_btn.clicked.connect(lambda: self._go_next(mark_done=False))
        row.addWidget(self._skip_btn)
        row.addStretch(1)
        close = QPushButton("Done")
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        row.addWidget(close)
        root.addLayout(row)

        theme_dialog(self)

    def _build_prompt(self) -> QFrame:
        """The 'that one's in — next gap?' strip, hidden until a card is added.

        Deliberately NOT a modal: a dialog you must dismiss after every single
        card is worse than the auto-advance it replaced, and it would steal the
        focus that belongs in the editor. This sits in the side-car, where the
        answer to 'what happens next' already lives."""
        self._prompt = QFrame()
        self._prompt.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self._prompt)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self._prompt_lbl = QLabel("")
        self._prompt_lbl.setWordWrap(True)
        lay.addWidget(self._prompt_lbl)
        btns = QHBoxLayout()
        self._next_btn = QPushButton("Next gap →")
        self._next_btn.setAutoDefault(False)
        self._next_btn.setToolTip("Load the next gap into the Add window.")
        self._next_btn.clicked.connect(lambda: self._go_next(mark_done=False))
        btns.addWidget(self._next_btn)
        stay = QPushButton("Another for this one")
        stay.setAutoDefault(False)
        stay.setToolTip("Stay on this gap — the editor is already primed for "
                        "a second card.")
        stay.clicked.connect(self._stay_here)
        btns.addWidget(stay)
        lay.addLayout(btns)
        self._prompt.setVisible(False)
        return self._prompt

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        ac = dialogs.open("AddCards", mw)
        self._ac = ac
        _attach_hook(self)
        self._load_current()
        try:
            ac.show()
            ac.raise_()
            ac.activateWindow()
        except Exception as e:
            log.error(f"write-your-own: couldn't raise the Add window: {e}")
        # Parent to the Add window so the side-car floats above it — and, on
        # macOS, follows it into a full-screen space instead of being stranded
        # on the desktop behind it.
        try:
            self.setParent(ac, Qt.WindowType.Tool)
        except Exception as e:
            log.error(f"write-your-own: couldn't parent the side-car: {e}")
        self.show()
        # make_room: an Add window that fills the screen (maximised, or opened
        # into Anki's full-screen space) leaves no "beside" to dock into, and
        # the side-car used to be placed off the edge — present, focusable,
        # invisible. dock_beside now takes the width out of the Add window and
        # gives it back when this closes.
        self._dock = dock_beside(self, ac, side="left", make_room=True)
        QTimer.singleShot(60, lambda: (ac.raise_(), ac.activateWindow()))

    def closeEvent(self, ev) -> None:
        global _live
        _detach_hook(self)
        # Let go of the Add window: no filter left behind, and the width we
        # borrowed handed back.
        try:
            if self._dock is not None:
                self._dock.detach()
        except Exception as e:
            log.error(f"write-your-own: couldn't undock: {e}")
        if _live is self:
            _live = None
        n = len(self._added)
        if n:
            tooltip(f"Added {n} card(s) — Ctrl+Z in the main window undoes "
                    "the last one.")
        self._refresh_panel()
        super().closeEvent(ev)

    # ── the current gap ─────────────────────────────────────────────────────
    def _kg(self) -> dict:
        g = self._gaps[self._i]
        return kg_store.get(g.get("id")) or g

    def _load_current(self) -> None:
        """Show this gap, and prime the Add editor with an empty note that
        already carries its tags and its attached content."""
        kg = self._kg()
        total = len(self._gaps)
        self._counter.setText(
            f"Gap {self._i + 1} of {total}" if total > 1 else "")
        self._skip_btn.setVisible(self._i + 1 < total)
        self._prompt.setVisible(False)
        title = str(kg.get("title") or "(untitled)")
        self._title.setText(f"<b>{_html.escape(title)}</b>")
        notes = kg_store.field(kg, "notes") or ""
        stem = kg_store.field(kg, "stem_html") or ""
        if stem:
            self._ref.setHtml(stem)
        elif notes:
            self._ref.setHtml(_html.escape(notes).replace("\n", "<br>"))
        else:
            self._ref.setHtml("<i style='color:gray'>You wrote nothing down "
                              "for this one.</i>")
        self._show_resources(kg)
        self._tag.setText(self._suggest_tag(kg))
        self._status.setText("Type the card in the Add window, then press "
                             "Anki's Add button.")
        self._prime_editor()

    def _show_resources(self, kg: dict) -> None:
        """Name the resources that are going onto Extra with this card."""
        bits = []
        for r in (kg.get("resources") or []):
            if not isinstance(r, dict):
                continue
            label = str(r.get("label") or "").strip()
            url = str(r.get("url") or "").strip()
            if url:
                bits.append(f'<a href="{_html.escape(url, quote=True)}">'
                            f'{_html.escape(label or url)}</a>')
            elif label:
                bits.append(_html.escape(label))
        self._res.setText("→ Extra: " + " · ".join(bits) if bits else "")
        self._res.setVisible(bool(bits))
        self._res.setToolTip("These go onto the card's Extra field, the same "
                             "as they would on a generated card.")

    def _suggest_tag(self, kg: dict) -> str:
        """The tag AI Create would give a card written for this gap."""
        try:
            from ..knowledge_gaps import _type_meta
            levels = {k: kg_store.field(kg, k) or ""
                      for k in ("system", "subsystem", "topic")}
            if not any(levels.values()):
                levels["topic"] = str(kg.get("title") or "")
            key = str(kg.get("type") or "kg")
            return autotag.content_tag_from_levels(
                levels, kg_type_key=key, type_meta=_type_meta(key),
                group=str(kg.get("group") or ""))
        except Exception as e:
            log.warn(f"write-your-own: couldn't suggest a tag: {e}")
            return ""

    def _tags_for(self, kg: dict) -> list:
        """Exactly the tag set a GENERATED card for this gap would carry."""
        cfg = tool_config("card_creator")
        tags = [str(t).strip() for t in (cfg.get("default_tags") or [])
                if str(t).strip()]

        def add(t: str) -> None:
            t = str(t or "").strip()
            if t and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)

        add(self._tag.text().strip())
        # The rest of the scheme — the group's shared branch, the batch's
        # source. A card you write by hand for a gap files exactly where a
        # generated one for the same gap does; that is the whole promise of
        # this door, and it was keeping only half of it.
        try:
            from .. import card_creator as cc
            levels = {k: kg_store.field(kg, k) or ""
                      for k in ("system", "subsystem", "topic")}
            for t in cc.scheme_tags_for_gap(dict(kg, kg_type=kg.get("type")),
                                            levels if any(levels.values()) else None,
                                            content_tag=""):
                add(t)
        except Exception as e:
            log.warn(f"write-your-own: scheme tags skipped: {e}")
        add(cfg.get("audit_tag") or "")
        add(month_tag())
        for t in (kg.get("tags") or []):
            add(t)
        return tags

    def _prime_editor(self) -> None:
        """Put an EMPTY note for this gap into the Add window's editor.

        Empty on purpose — the front is his to write. What it is NOT empty of
        is everything else a generated card would have had: the deck, the
        notetype, the tags, the gap's own stem/notes/images routed to whichever
        field the notetype profile sends them to, and the gap's resources on
        Extra."""
        ac = self._ac
        if ac is None:
            return
        from .. import card_creator as cc
        from ..knowledge_gaps import _type_meta
        kg = self._kg()
        cfg = tool_config("card_creator")
        profile = cc._resolved_profile(
            cfg, (cfg.get("selected_notetype") or cfg.get("default_notetype") or ""))
        nt = cc.resolve_notetype(profile, warn=False)
        if nt is None:
            self._status.setText(
                "That notetype profile doesn't resolve to a notetype here — "
                "check Settings → AI Create. The Add window still works; the "
                "tags just won't be prefilled.")
            return
        ctx = cc.CommitCtx(
            profile=profile, tags=self._tags_for(kg), mode="topic",
            kg_stem_html=kg_store.field(kg, "stem_html") or "",
            kg_notes=kg_store.field(kg, "notes") or "",
            kg_concept=str(kg.get("title") or ""),
            kg_type_for_image=str(kg.get("type") or ""),
            # The gap's reading list, onto Extra — the same block, from the same
            # renderer, that a generated card from this gap gets.
            kg_resources=list(kg.get("resources") or []),
            type_meta=_type_meta(str(kg.get("type") or "kg")),
            kg_field_values=dict(kg.get("fields") or {}),
        )
        try:
            deck_name = (cfg.get("default_deck") or "Default").strip() or "Default"
            deck_id = mw.col.decks.id(deck_name)
            mw.col.models.set_current(nt)
            mw.col.decks.select(deck_id)
            note = mw.col.new_note(nt)
            cc.set_fields(note, {"front": "", "extra": ""}, ctx)
            try:
                ac.set_note(note, deck_id=deck_id)
            except Exception as e:
                log.error(f"write-your-own: set_note failed ({e}); "
                          "falling back to the editor")
                ac.editor.set_note(note)
            self._sync_tag_widget(note.tags)
        except Exception as e:
            log.error(f"write-your-own: couldn't prime the editor: {e}")
            self._status.setText("Couldn't prefill the Add window — type the "
                                 "card and add the tag by hand.")

    def _sync_tag_widget(self, tags: list) -> None:
        """Anki's sticky-tag behaviour can clobber tags set on the note, so the
        editor's own tag box gets the final word."""
        try:
            ed = self._ac.editor
            if hasattr(ed, "tags") and hasattr(ed.tags, "setText"):
                ed.tags.setText(" ".join(tags))
        except Exception as e:
            log.error(f"write-your-own: couldn't refresh the tag box: {e}")

    def _push_tag(self) -> None:
        """An edit in our tag box re-tags the note currently in the editor."""
        try:
            note = self._ac.editor.note
            if note is None:
                return
            kg = self._kg()
            wanted = self._tags_for(kg)
            note.tags = list(wanted)
            self._sync_tag_widget(wanted)
        except Exception as e:
            log.error(f"write-your-own: couldn't push the tag: {e}")

    # ── the Add button fired ────────────────────────────────────────────────
    def note_added(self, note) -> None:
        try:
            self._added.append(getattr(note, "id", None))
        except Exception:
            pass
        kg = self._kg()
        self._tick_error = ""
        closed = self._mark_done(kg) if self._done_cb.isChecked() else False
        # Re-prime first, so "another for this one" is already true by the time
        # it is offered: the editor is blank, tagged, and carrying this gap's
        # content again.
        self._prime_editor()
        more = self._i + 1 < len(self._gaps)
        head = "Card added — gap closed." if closed else "Card added."
        if self._tick_error:
            head = "Card added. " + self._tick_error
        if more:
            nxt = self._gaps[self._i + 1]
            nxt = kg_store.get(nxt.get("id")) or nxt
            self._prompt_lbl.setText(
                f"{head} Move on to the next gap?<br>"
                f"<b>{_html.escape(str(nxt.get('title') or '(untitled)'))}</b>")
            self._prompt.setVisible(True)
            self._status.setText(
                f"{len(self._added)} card(s) added this session.")
        else:
            self._prompt.setVisible(False)
            self._status.setText(
                f"{head} {len(self._added)} card(s) added. Write another for "
                "this gap, or press Done.")

    def _mark_done(self, kg: dict) -> bool:
        """Tick the gap off — and prove it stuck.

        Read back rather than trusting the write: a gap that silently stays
        open is the failure this feature is judged on, and a status line that
        says so beats finding out in the list an hour later."""
        kid = kg.get("id")
        if not kid or kid in self._closed_ids:
            return bool(kid in self._closed_ids)
        try:
            kg_store.update(kid, status="done")
            after = kg_store.get(kid) or {}
            if after.get("status") != "done":
                log.error(f"write-your-own: {kid} did not stay done")
                self._tick_error = ("Couldn't tick that gap off — it's still "
                                    "open in the list.")
                return False
            self._closed_ids.append(kid)
            self._refresh_panel()
            try:
                from ..knowledge_gaps import maybe_sync_to_things
                maybe_sync_to_things()
            except Exception as e:
                log.warn(f"write-your-own: Things sync skipped: {e}")
            return True
        except Exception as e:
            log.warn(f"write-your-own: couldn't mark {kid} done: {e}")
            self._tick_error = ("Couldn't tick that gap off — it's still open "
                                "in the list.")
            return False

    def _refresh_panel(self) -> None:
        """Repaint the Gaps list now, not at close: a tick you cannot see is
        indistinguishable from one that never happened."""
        try:
            from ..knowledge_gaps import _refresh_open_panel
            _refresh_open_panel()
        except Exception as e:
            log.warn(f"write-your-own: panel refresh skipped: {e}")

    # ── moving between gaps ─────────────────────────────────────────────────
    def _stay_here(self) -> None:
        self._prompt.setVisible(False)
        self._status.setText("Still on this gap — the editor is primed for "
                             "another card.")
        self._focus_editor()

    def _go_next(self, *, mark_done: bool) -> None:
        """Load the next gap. Only ever from a button — never off an Add."""
        if self._i + 1 >= len(self._gaps):
            self._prompt.setVisible(False)
            return
        if mark_done:
            self._mark_done(self._kg())
        self._i += 1
        self._load_current()
        self._focus_editor()

    def _focus_editor(self) -> None:
        """Hand typing back to the Add window after a click over here."""
        ac = self._ac
        if ac is None:
            return
        try:
            ac.raise_()
            ac.activateWindow()
            ac.editor.web.setFocus()
        except Exception as e:
            log.warn(f"write-your-own: couldn't refocus the editor: {e}")
