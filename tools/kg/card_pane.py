# The card panel — the right-hand side of the work-through's results.
#
# A tree row holds a line of text, and a card is often more than a line: the
# rows cut off mid-sentence, and the only way to read the rest was a hover or
# opening the browser. So whichever row is selected is shown here IN FULL:
#
#   a card you own      every field, as text, with a button to open the note
#   a written card      front, back, the SOURCE it cites (grounding), the
#                       images on it, and — when Auto-image flagged it — the
#                       suggested pictures, one click to attach
#   a point / a gap     what it is and what it was searched as
#
# AI Lecture adds its own sections underneath (the slides a card can carry and
# the Lecture Notes lines proposed for it) — see lecture/door.py.
#
# A mixin on BulkSearchDialog, like the lecture door, because it is part of the
# one screen rather than a window of its own.

from __future__ import annotations

import html
import re

from ...core import log


def _plain(text: str) -> str:
    """A field's HTML as readable text: line breaks kept, tags dropped."""
    t = re.sub(r"(?i)<br\s*/?>|</(div|p|li|tr)>", "\n", str(text or ""))
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


class CardPaneMixin:

    # How many suggested pictures to offer per card. The images module caps
    # each source; this caps the strip.
    SUGGEST_MAX = 6

    def _build_card_pane(self):
        from aqt.qt import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(6, 0, 0, 0)
        self._pane_head = QLabel("The selected card, in full")
        self._pane_head.setStyleSheet("font-weight: 600;")
        col.addWidget(self._pane_head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self._pane_body = QVBoxLayout(holder)
        self._pane_body.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(holder)
        col.addWidget(scroll, 1)
        pane.setMinimumWidth(260)
        self._card_pane = pane
        # query -> [candidate dicts] once fetched; query -> "loading" meanwhile.
        self._img_cands: dict = {}
        # url -> thumbnail bytes (or b"" when it wouldn't load).
        self._img_thumbs: dict = {}
        return pane

    # -- small builders ------------------------------------------------------

    def _pane_label(self, text: str, *, grey: bool = False, bold: bool = False,
                    rich: bool = False):
        from aqt.qt import QLabel, Qt
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        if rich:
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setOpenExternalLinks(True)
        else:
            lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | (Qt.TextInteractionFlag.LinksAccessibleByMouse if rich
               else Qt.TextInteractionFlag.NoTextInteraction))
        style = []
        if grey:
            style.append("color: gray; font-size: 11px;")
        if bold:
            style.append("font-weight: 600;")
        if style:
            lbl.setStyleSheet(" ".join(style))
        return lbl

    def _pane_heading(self, text: str):
        lbl = self._pane_label(text, bold=True)
        lbl.setContentsMargins(0, 8, 0, 0)
        return lbl

    def _pane_clear(self) -> bool:
        try:
            while self._pane_body.count():
                it = self._pane_body.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.deleteLater()
            return True
        except RuntimeError:
            return False

    # -- dispatch ------------------------------------------------------------

    def _pane_show(self, item=None, _prev=None) -> None:
        """Redraw the panel for the selected row."""
        if getattr(self, "_closed", False) or getattr(self, "_card_pane", None) is None:
            return
        if not self._pane_clear():
            return
        kind = item.data(0, self.ROLE_KIND) if item is not None else None
        try:
            if kind == "note":
                self._pane_found(item)
            elif kind == "newcard":
                self._pane_written(item)
            elif kind == "point":
                self._pane_point(item)
            elif kind == "gap":
                self._pane_gap(item)
            else:
                self._pane_body.addWidget(self._pane_label(
                    "Click a card to read the whole of it here — the rows "
                    "only have room for the first line.", grey=True))
            if self._lecture is not None:
                self._lec_pane_extras(item, kind)
        except Exception as e:
            log.error(f"card pane: couldn't show that row: {e}")
        self._pane_body.addStretch(1)

    # -- a card you already own ----------------------------------------------

    def _pane_found(self, leaf) -> None:
        from aqt import mw
        from aqt.qt import QPushButton
        body = self._pane_body
        nid = int(leaf.data(0, self.ROLE_KEY))
        try:
            note = mw.col.get_note(nid)
        except Exception:
            body.addWidget(self._pane_label("That note is no longer in your "
                                            "collection.", grey=True))
            return
        try:
            nt_name = note.note_type()["name"]
        except Exception:
            nt_name = ""
        body.addWidget(self._pane_label(
            "A card you already own" + (f" · {nt_name}" if nt_name else ""),
            grey=True))
        shown = 0
        for name, value in note.items():
            text = _plain(value)
            if not text:
                continue
            body.addWidget(self._pane_heading(name))
            body.addWidget(self._pane_label(text))
            shown += 1
            if shown >= 6:
                break
        flaw = self._found_flaw(nid)
        if flaw:
            body.addWidget(self._pane_label(f"Quality check: {flaw}", grey=True))
        b = QPushButton("Open in the Anki browser")
        b.setAutoDefault(False)
        b.clicked.connect(lambda: self._on_tree_double_clicked(leaf, 0))
        body.addWidget(b)

    # -- a card written for this run ------------------------------------------

    def _pane_written(self, leaf) -> None:
        from aqt.qt import QPushButton
        body = self._pane_body
        card = leaf.data(0, self.ROLE_CARD) or {}
        body.addWidget(self._pane_label(
            "Written for this run — not in your collection until you Apply",
            grey=True))
        body.addWidget(self._pane_heading("Front"))
        body.addWidget(self._pane_label(_plain(card.get("front"))))
        back = _plain(card.get("extra") or card.get("back"))
        if back:
            body.addWidget(self._pane_heading("Back"))
            body.addWidget(self._pane_label(back))

        # Grounding: the citation the model was held to. Linked, so a source
        # can be checked from right here.
        src = str(card.get("source") or "").strip()
        body.addWidget(self._pane_heading("Source"))
        if src:
            linked = re.sub(r"(https?://[^\s<>\"']+)",
                            r'<a href="\1">\1</a>', html.escape(_plain(src)))
            body.addWidget(self._pane_label(linked.replace("\n", "<br>"),
                                            rich=True))
        else:
            body.addWidget(self._pane_label(
                "None cited." + ("" if self._ground_on() else
                                 " Grounding was off for this run."),
                grey=True))

        # Images on the card, then the suggestions.
        paths = list(card.get("_image_paths") or [])
        body.addWidget(self._pane_heading(f"Images ({len(paths)})"))
        for p in paths:
            img = self._pane_image_from_file(p)
            if img is not None:
                body.addWidget(img)
        query = str(card.get("image_query") or "").strip()
        if query:
            self._pane_suggestions(leaf, query)
        elif not paths:
            body.addWidget(self._pane_label(
                "The AI didn't flag this card as needing a picture."
                if self._autoimg_on() else
                "Auto-image was off for this run.", grey=True))
        more = QPushButton("Find an image…")
        more.setAutoDefault(False)
        more.clicked.connect(lambda: self._add_card_images(leaf, "find"))
        body.addWidget(more)
        edit = QPushButton("Edit this card…")
        edit.setAutoDefault(False)
        edit.clicked.connect(lambda: self._edit_written(leaf))
        body.addWidget(edit)

    def _pane_image_from_file(self, path: str, width: int = 240):
        from aqt.qt import QLabel, QPixmap, Qt
        pm = QPixmap(str(path or ""))
        if pm.isNull():
            return None
        lbl = QLabel()
        lbl.setPixmap(pm.scaledToWidth(min(width, pm.width()),
                                       Qt.TransformationMode.SmoothTransformation))
        return lbl

    def _pane_suggestions(self, leaf, query: str) -> None:
        """Pictures for a card the AI flagged with an image_query. Fetched the
        first time the card is looked at — never for cards nobody opens — and
        kept for the rest of the run."""
        body = self._pane_body
        body.addWidget(self._pane_label(f"Suggested — searched as “{query}”. "
                                        "Click one to put it on the card.",
                                        grey=True))
        got = self._img_cands.get(query)
        if got is None:
            self._img_cands[query] = "loading"
            self._fetch_suggestions(query)
            got = "loading"
        if got == "loading":
            body.addWidget(self._pane_label("Finding images…", grey=True))
            return
        if not got:
            body.addWidget(self._pane_label("Nothing usable came back.",
                                            grey=True))
            return
        from aqt.qt import QGridLayout, QWidget
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        for i, r in enumerate(got[:self.SUGGEST_MAX]):
            grid.addWidget(self._suggestion_button(leaf, r), i // 2, i % 2)
        body.addWidget(grid_w)

    def _suggestion_button(self, leaf, r: dict):
        from aqt.qt import QIcon, QPixmap, QPushButton, QSize
        btn = QPushButton()
        btn.setMinimumSize(QSize(116, 116))
        btn.setIconSize(QSize(108, 108))
        btn.setToolTip((r.get("title") or "") + f"\n{r.get('source', '')}"
                       "\n\nClick to put this image on the card.")
        data = self._img_thumbs.get(r.get("thumb_url") or r.get("url") or "")
        pm = QPixmap()
        if data and pm.loadFromData(data):
            btn.setIcon(QIcon(pm))
        else:
            btn.setText(f"📎 {r.get('source', 'image')}")
        btn.clicked.connect(lambda _c=False, rr=r, b=btn:
                            self._attach_suggestion(leaf, rr, b))
        return btn

    def _fetch_suggestions(self, query: str) -> None:
        from aqt import mw
        from ...core.config import tool_config
        from .. import images
        try:
            cfg = tool_config("card_creator")
        except Exception:
            cfg = None

        def _run():
            try:
                res = images.search_images(query, cfg=cfg, limit_per_source=2)
            except Exception as e:
                log.warn(f"card pane: image search failed for {query!r}: {e}")
                res = []
            res = res[:self.SUGGEST_MAX]
            for r in res:
                key = r.get("thumb_url") or r.get("url") or ""
                data = b""
                for u in (r.get("thumb_url"), r.get("url")):
                    if not u:
                        continue
                    try:
                        data = images._get(u, timeout=8) or b""
                    except Exception:
                        data = b""
                    if data:
                        break
                self._img_thumbs[key] = data
            mw.taskman.run_on_main(lambda: self._suggestions_landed(query, res))

        images.submit(_run)

    def _suggestions_landed(self, query: str, res: list) -> None:
        if getattr(self, "_closed", False):
            return
        self._img_cands[query] = list(res or [])
        cur = self._tree.currentItem()
        if cur is not None and cur.data(0, self.ROLE_KIND) == "newcard" \
                and str((cur.data(0, self.ROLE_CARD) or {}).get("image_query")
                        or "").strip() == query:
            self._pane_show(cur)

    def _attach_suggestion(self, leaf, r: dict, btn) -> None:
        from aqt import mw
        from aqt.utils import tooltip
        from .. import images
        btn.setEnabled(False)
        url = r.get("url") or ""

        def _run():
            path = images.download_to_temp(url)

            def _ui():
                if getattr(self, "_closed", False):
                    return
                if not path:
                    btn.setEnabled(True)
                    tooltip("Couldn't download that image.")
                    return
                card = leaf.data(0, self.ROLE_CARD) or {}
                from .bulk_search import _merged_images
                self._set_card_images(leaf, _merged_images(
                    card.get("_image_paths"), [path]))
                if self._tree.currentItem() is leaf:
                    self._pane_show(leaf)
            mw.taskman.run_on_main(_ui)

        images.submit(_run)

    # -- points and gaps ------------------------------------------------------

    def _pane_point(self, node) -> None:
        body = self._pane_body
        body.addWidget(self._pane_label("Learning point", grey=True))
        body.addWidget(self._pane_label(str(node.data(0, self.ROLE_CONCEPT) or
                                            node.text(0))))
        terms = ""
        try:
            m = self._lec_match_for(node) if self._coverage is not None else None
            if m is not None and m.point.terms:
                terms = ", ".join(m.point.terms)
        except Exception:
            terms = ""
        if terms:
            body.addWidget(self._pane_label(f"Searched as: {terms}", grey=True))

    def _pane_gap(self, top) -> None:
        body = self._pane_body
        kg_id = str(top.data(0, self.ROLE_KEY) or "")
        plan = next((p for p in self._run_plans if p.kg_id == kg_id), None)
        rec = self._record_for(kg_id) or {}
        body.addWidget(self._pane_label(
            "Section" if self._lecture is not None else "Knowledge gap",
            grey=True))
        body.addWidget(self._pane_label(str(top.data(0, self.ROLE_TITLE) or "")))
        if plan is not None and plan.concept and plan.concept != plan.title:
            body.addWidget(self._pane_label(plan.concept))
        try:
            from . import store as kg_store
            notes = kg_store.field(rec, "notes")
        except Exception:
            notes = ""
        if notes:
            body.addWidget(self._pane_heading(
                "From the lecture" if self._lecture is not None else "Your notes"))
            body.addWidget(self._pane_label(notes))
        if plan is not None:
            body.addWidget(self._pane_label(
                f"{len(plan.points)} point(s)"
                + ("" if self._lecture is not None
                   else f" · budget {plan.cards} card(s), found + written"),
                grey=True))

    # -- the run's toggles ---------------------------------------------------

    def _ground_on(self) -> bool:
        cb = getattr(self, "_ground_cb", None)
        return bool(cb is not None and cb.isChecked())

    def _autoimg_on(self) -> bool:
        cb = getattr(self, "_autoimg_cb", None)
        return bool(cb is not None and cb.isChecked())
