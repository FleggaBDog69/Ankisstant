# Update by Tag — bulk-reprocess existing notes to the current format/guidelines.
#
# Ported from Patrick's claude_cards (ui/update_tab.py + card_generator.update_card)
# but routed through ankisstant's engine instead of his hardcoded prompt/CLI:
#   • generation uses the selected notetype PROFILE (field map + skill/prompt +
#     card_format) and the shared CARD_GEN_SYSTEM / QA_GEN_SYSTEM contracts;
#   • source grounding (the AU/WA guideline allow-list) is applied, matched on the
#     note's own content + tags;
#   • the card quality pass runs (per the profile's override) and FAILs are tagged
#     for review rather than silently kept.
#
# Non-destructive by design: it creates UPDATED COPIES in a destination deck and
# never modifies the originals. Any <img> tags on the original survive into the
# copy. Fail-open throughout — one bad note never aborts the batch.

from __future__ import annotations

import re
import threading

from aqt import mw
from aqt.qt import (
    QComboBox, QCompleter, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QScrollArea, QStringListModel, Qt, QTimer,
    QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, api as core_api, log
from ..core.config import tool_config, tool_model_for
from ..grounding import guidelines as grounding
from . import card_creator as cc
from . import quality_pass as qp
from ..core import synapse


NAME = "Update by Tag"

_IMG_RE = re.compile(r"<img[^>]+>", re.IGNORECASE)

UPDATE_INSTRUCTION = (
    "You are UPDATING an EXISTING Anki card to current Australian/WA guidelines and "
    "the formatting standard described above. The original may be in any format "
    "(cloze, basic Front/Back, or a raw field dump). Extract the clinical content and "
    "re-express it as ONE card in the required output format.\n"
    "Rules:\n"
    "• Preserve the clinical meaning; correct anything that conflicts with current "
    "Australian guidelines.\n"
    "• Do NOT copy the original verbatim — rewrite it cleanly.\n"
    "• Return a JSON ARRAY with a SINGLE card object in the standard output format.\n"
)


# ── content extraction ───────────────────────────────────────────────────────

def _note_content(note) -> tuple[str, list[str]]:
    """Return (readable text block of all fields, list of <img> tags found)."""
    parts: list[str] = []
    imgs: list[str] = []
    try:
        items = note.items()  # [(field_name, value), ...]
    except Exception:
        items = []
    for fname, val in items:
        val = val or ""
        if val.strip():
            parts.append(f"[{fname}]: {val}")
        imgs.extend(_IMG_RE.findall(val))
    return "\n".join(parts), imgs


def _reinsert_images(card: dict, imgs: list[str]) -> dict:
    """Ensure any original <img> tags survive into the card's extra field."""
    if not imgs:
        return card
    extra = card.get("extra", "") or ""
    missing = [t for t in imgs if t not in extra]
    if missing:
        card = dict(card)
        card["extra"] = (extra + "<br>" if extra else "") + "<br>".join(missing)
    return card


# ── generation (worker-thread safe — no collection writes here) ──────────────

def _update_one(note, profile: dict, cc_cfg: dict, model) -> dict | None:
    """Re-generate a single card from an existing note. Pure AI + text work, safe
    to call off the main thread. Returns a {front, extra, source?} dict or None."""
    text, imgs = _note_content(note)
    if not text.strip():
        raise ValueError("note has no readable content")

    is_qa = str(profile.get("card_format", "cloze")).lower() == "qa"
    base_system = cc.QA_GEN_SYSTEM if is_qa else cc.CARD_GEN_SYSTEM
    system = cc._augment_system(base_system, profile)

    # Grounding: updating has no freshly-pasted source, so it's grounding's home
    # turf (treated like topic mode). Match on the card's own content + tags.
    try:
        if cc._grounding_active(cc_cfg, profile, "topic"):
            topic_hint = re.sub(r"<[^>]+>|\{\{[^}]+\}\}", " ", text).strip()[:200]
            block = grounding.build_guidelines_block(
                topic_hint, list(note.tags),
                fetch_content=cc._grounding_fetch_live(cc_cfg),
            )
            if block:
                system = system + "\n\n" + block
    except Exception as e:
        log.error(f"update grounding skipped: {e}")

    image_instruction = ""
    if imgs:
        image_instruction = (
            "\n\nIMAGES: the original card contains the image tag(s) below. Include "
            "them VERBATIM at the end of the extra field:\n" + "\n".join(imgs)
        )

    prompt = (
        UPDATE_INSTRUCTION
        + "\n---EXISTING CARD---\n" + text + "\n---END EXISTING CARD---"
        + image_instruction
    )
    skill_invocation, skill_id = cc._resolve_skill(profile)
    reply = core_api.ask_claude_json(
        prompt=prompt, system=system, max_tokens=2048, model=model,
        skill_id=skill_id, skill_invocation=skill_invocation,
        show_errors=False,
    )
    if isinstance(reply, list):
        card = reply[0] if reply else None
    elif isinstance(reply, dict):
        card = reply
    else:
        card = None
    if not isinstance(card, dict) or "front" not in card:
        return None
    card.setdefault("extra", "")
    return _reinsert_images(card, imgs)


# ── note writing (MAIN THREAD only) ──────────────────────────────────────────

def _write_copy(card: dict, profile: dict, nt, deck_id: int, extra_tags: list[str]) -> int:
    """Create a new note (the profile's notetype) from a generated card. Returns
    the new note id. Must run on the main thread (writes the collection)."""
    front_field   = profile.get("front_field", "Text")
    extra_field   = profile.get("extra_field", "Extra")
    image_field   = profile.get("image_field", extra_field)
    sources_field = (profile.get("sources_field") or "").strip()
    obo_field     = profile.get("one_by_one_field", "One by one")

    note = mw.col.new_note(nt)
    try:
        note[front_field] = card.get("front", "")
    except KeyError:
        note.fields[0] = card.get("front", "")
    if extra_field in note:
        note[extra_field] = card.get("extra", "") or ""
    source_html = (card.get("source") or "").strip()
    if source_html and sources_field and sources_field in note:
        if sources_field == extra_field:
            existing = note[sources_field] or ""
            note[sources_field] = (existing + "<br>" if existing else "") + source_html
        else:
            note[sources_field] = source_html
    if card.get("one_by_one") and obo_field in note:
        note[obo_field] = "y"
    for t in extra_tags:
        if t:
            note.add_tag(t)
    mw.col.add_note(note, deck_id)
    return note.id


# ── panel ────────────────────────────────────────────────────────────────────

class UpdateByTagPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("update_by_tag")
        self._note_ids: list[int] = []
        self._queue: list[int] = []
        self._cancelled = False
        self._running = False
        self._build()

    # -- profiles come from the AI Create tool (shared notetype profiles) --
    def _profiles(self) -> list[dict]:
        return tool_config("card_creator").get("notetypes") or []

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        intro = QLabel(
            "<b>Update by Tag</b> — re-process existing notes to the current "
            "format and Australian guidelines, as <i>new copies</i> in a "
            "destination deck. Originals are never modified; <code>&lt;img&gt;</code> "
            "tags are preserved."
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Filter: deck + tag
        self._sec("FIND NOTES", root)
        deck_row = QHBoxLayout()
        lbl = QLabel("Search deck:"); lbl.setFixedWidth(96)
        deck_row.addWidget(lbl)
        self._deck_combo = QComboBox(); self._deck_combo.setMinimumWidth(260)
        self._deck_combo.currentIndexChanged.connect(self._on_filter_changed)
        deck_row.addWidget(self._deck_combo); deck_row.addStretch(1)
        root.addLayout(deck_row)

        tag_row = QHBoxLayout()
        lbl2 = QLabel("Tag:"); lbl2.setFixedWidth(96)
        tag_row.addWidget(lbl2)
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText("optional — e.g. Malleus_CM::#Subjects::Psychiatry")
        self._tag_input.textChanged.connect(self._on_filter_changed)
        tag_row.addWidget(self._tag_input, 1)
        self._find_btn = QPushButton("Find Notes")
        self._find_btn.setFixedWidth(110)
        self._find_btn.clicked.connect(self._find_notes)
        tag_row.addWidget(self._find_btn)
        root.addLayout(tag_row)

        self._found_lbl = QLabel("")
        root.addWidget(self._found_lbl)

        root.addWidget(self._hsep())

        # Output: profile (format) + destination deck
        self._sec("OUTPUT", root)
        prof_row = QHBoxLayout()
        lbl3 = QLabel("Format as:"); lbl3.setFixedWidth(96)
        prof_row.addWidget(lbl3)
        self._profile_combo = QComboBox(); self._profile_combo.setMinimumWidth(260)
        prof_row.addWidget(self._profile_combo); prof_row.addStretch(1)
        root.addLayout(prof_row)

        dest_row = QHBoxLayout()
        lbl4 = QLabel("Copy to deck:"); lbl4.setFixedWidth(96)
        dest_row.addWidget(lbl4)
        self._dest_combo = QComboBox(); self._dest_combo.setMinimumWidth(260)
        dest_row.addWidget(self._dest_combo); dest_row.addStretch(1)
        root.addLayout(dest_row)

        root.addWidget(self._hsep())

        # Progress + results
        self._sec("PROGRESS", root)
        self._progress = QProgressBar(); self._progress.setVisible(False)
        root.addWidget(self._progress)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(self._status_lbl)

        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumHeight(150)
        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._results_layout.setSpacing(3)
        scroll.setWidget(self._results_container)
        root.addWidget(scroll, 1)

        btn_row = QHBoxLayout(); btn_row.addStretch(1)
        self._cancel_btn = QPushButton("Stop")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self._cancel_btn)
        self._run_btn = QPushButton("Create Updated Copies")
        self._run_btn.setMinimumHeight(36); self._run_btn.setMinimumWidth(190)
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._start)
        btn_row.addWidget(self._run_btn)
        root.addLayout(btn_row)

        self._refresh_decks()
        self._refresh_profiles()
        self._refresh_completer()

    # -- small ui helpers --
    def _sec(self, text: str, layout):
        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 10px; font-weight: 700; color: gray; letter-spacing: 0.6px;")
        layout.addWidget(lbl)

    def _hsep(self) -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("color: palette(mid);")
        return f

    def _deck_names(self) -> list[str]:
        try:
            return sorted(d.name for d in mw.col.decks.all_names_and_ids())
        except Exception:
            return []

    def _refresh_decks(self):
        self._deck_combo.blockSignals(True)
        self._deck_combo.clear()
        self._deck_combo.addItem("All decks")
        for n in self._deck_names():
            self._deck_combo.addItem(n)
        self._deck_combo.blockSignals(False)
        cur = self._dest_combo.currentText()
        self._dest_combo.clear()
        for n in self._deck_names():
            self._dest_combo.addItem(n)
        idx = self._dest_combo.findText(cur)
        if idx >= 0:
            self._dest_combo.setCurrentIndex(idx)

    def _refresh_profiles(self):
        self._profile_combo.clear()
        profs = self._profiles()
        if not profs:
            self._profile_combo.addItem("(no notetype profiles — add one in AI Create)")
            self._profile_combo.setEnabled(False)
            self._run_btn.setEnabled(False)
        else:
            self._profile_combo.setEnabled(True)
            for p in profs:
                self._profile_combo.addItem(p.get("name", "?"), p.get("name", ""))

    def _refresh_completer(self):
        try:
            tags = sorted(mw.col.tags.all())
        except Exception:
            tags = []
        comp = QCompleter(QStringListModel(tags), self._tag_input)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        self._tag_input.setCompleter(comp)

    def showEvent(self, ev):
        super().showEvent(ev)
        try:
            self.cfg = tool_config("update_by_tag")
            self._refresh_decks()
            self._refresh_profiles()
            self._refresh_completer()
        except Exception as e:
            print(f"[ankisstant] update-by-tag refresh failed: {e}")

    def _selected_profile(self) -> dict | None:
        name = self._profile_combo.currentData()
        if not name:
            return None
        for p in self._profiles():
            if p.get("name") == name:
                return p
        return None

    def _on_filter_changed(self, *_):
        self._note_ids = []
        self._found_lbl.setText("")
        self._run_btn.setEnabled(False)

    # -- find --
    def _find_notes(self):
        if not anki_utils.require_col():
            return
        deck = self._deck_combo.currentText()
        deck = "" if deck == "All decks" else deck
        tag = self._tag_input.text().strip()
        if not deck and not tag:
            showWarning("Pick a deck or enter a tag (or both) first.")
            return
        terms = []
        if deck:
            terms.append(f'deck:"{deck}"')
        if tag:
            terms.append(f'tag:"{tag}"')
        query = " ".join(terms)
        try:
            self._note_ids = list(mw.col.find_notes(query))
        except Exception as e:
            showWarning(f"Search failed:\n\n{e}")
            return
        n = len(self._note_ids)
        if n == 0:
            self._found_lbl.setText("No notes matched.")
            self._found_lbl.setStyleSheet(f"color: {synapse.status_error('#c05050')};")
            self._run_btn.setEnabled(False)
        else:
            self._found_lbl.setText(f"Found {n} note{'s' if n != 1 else ''} — ready to copy.")
            self._found_lbl.setStyleSheet(
                f"color: {synapse.status_ok()}; font-weight: 600;")
            self._run_btn.setEnabled(bool(self._profiles()))

    # -- run --
    def _start(self):
        if not anki_utils.require_col() or not self._note_ids:
            return
        profile = self._selected_profile()
        if profile is None:
            showWarning("Pick a notetype profile to format the updated copies.")
            return
        nt = mw.col.models.by_name(profile.get("name", ""))
        if nt is None:
            showWarning(f"Notetype not found: {profile.get('name', '?')}")
            return
        dest = self._dest_combo.currentText().strip()
        if not dest:
            showWarning("Pick a destination deck.")
            return
        n = len(self._note_ids)
        if not askUser(
            f"Create {n} updated cop{'ies' if n != 1 else 'y'} as "
            f"'{profile.get('name')}' in deck '{dest}'?\n\n"
            "Originals will NOT be changed. Continue?"
        ):
            return

        # Clear previous results
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._profile = profile
        self._nt = nt
        self._cc_cfg = tool_config("card_creator")
        self._model = tool_model_for("card_creation")
        self._deck_id = mw.col.decks.id(dest)
        self._audit_tag = (self.cfg.get("audit_tag") or "Ankisstant::AI::Updated").strip()
        self._review_tag = (self.cfg.get("review_tag") or "Ankisstant::AI::UpdateReview").strip()
        self._queue = list(self._note_ids)
        self._total = n
        self._done = 0
        self._ok = 0
        self._cancelled = False
        self._running = True

        self._progress.setMaximum(n); self._progress.setValue(0); self._progress.setVisible(True)
        self._status_lbl.setText(f"Processing 0 / {n}…")
        self._run_btn.setEnabled(False); self._find_btn.setEnabled(False)
        self._cancel_btn.setVisible(True); self._cancel_btn.setEnabled(True)
        self._process_next()

    def _process_next(self):
        if self._cancelled or not self._queue:
            self._finish()
            return
        nid = self._queue.pop(0)
        try:
            note = mw.col.get_note(nid)
        except Exception as e:
            self._on_done(nid, None, f"load failed: {e}")
            return
        profile, cc_cfg, model = self._profile, self._cc_cfg, self._model

        def _run():
            try:
                card = _update_one(note, profile, cc_cfg, model)
                verdict = None
                if card is not None:
                    try:
                        verdicts = cc.grade_cards_sync([card], profile, cc_cfg)
                        if verdicts:
                            verdict = verdicts[0]
                    except Exception as e:
                        log.error(f"update quality pass skipped: {e}")
                mw.taskman.run_on_main(lambda: self._on_done(nid, (card, verdict), None))
            except Exception as e:
                msg = str(e)
                mw.taskman.run_on_main(lambda: self._on_done(nid, None, msg))

        threading.Thread(target=_run, daemon=True).start()

    def _on_done(self, nid: int, payload, error):
        self._done += 1
        self._progress.setValue(self._done)
        self._status_lbl.setText(f"Processing {self._done} / {self._total}…")
        if error or payload is None or payload[0] is None:
            self._row(nid, False, error or "no card produced")
        else:
            card, verdict = payload
            try:
                tags = [self._audit_tag]
                detail = (card.get("front", "") or "")[:90]
                if verdict is not None and getattr(verdict, "verdict", qp.PASS) == qp.FAIL:
                    tags.append(self._review_tag)
                    detail = "⚠ flagged for review · " + detail
                _write_copy(card, self._profile, self._nt, self._deck_id, tags)
                self._ok += 1
                self._row(nid, True, detail)
            except Exception as e:
                self._row(nid, False, str(e)[:140])
        if not self._cancelled and self._queue:
            QTimer.singleShot(150, self._process_next)
        else:
            self._finish()

    def _row(self, nid: int, ok: bool, detail: str):
        lbl = QLabel(f"{'✓' if ok else '✗'}  note {nid} — {detail}")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            "font-size: 10px; padding: 1px 3px; color: "
            f"{synapse.status_ok() if ok else synapse.status_error('#c05050')};"
        )
        self._results_layout.addWidget(lbl)

    def _finish(self):
        self._running = False
        self._cancel_btn.setVisible(False)
        self._find_btn.setEnabled(True)
        self._run_btn.setEnabled(bool(self._note_ids and self._profiles()))
        self._progress.setVisible(False)
        self._status_lbl.setText(
            f"Done — {self._ok} cop{'ies' if self._ok != 1 else 'y'} created."
        )
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass

    def _cancel(self):
        self._cancelled = True
        self._cancel_btn.setEnabled(False)
        self._status_lbl.setText("Stopping after the current note…")


# ── tool entry points (mirrors the other tools) ──────────────────────────────

_panel: UpdateByTagPanel | None = None


def init(main_window) -> None:
    return


def get_panel() -> QWidget:
    global _panel
    if _panel is None:
        _panel = UpdateByTagPanel()
    return _panel
