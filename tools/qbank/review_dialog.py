# Review queue panel for captured "missed questions".
# For each item: AI search → checklist of matches → append + re-rate Again,
# or create new card via Claude.

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSplitter, Qt, QTextEdit,
    QVBoxLayout, QWidget,
)
from aqt.utils import showWarning, tooltip

from ...core import anki_utils, api as core_api
from ...core.config import tool_config
from .anki_helpers import mark_again
from .missed_queue import load_queue, remove_item


# ── prompts ───────────────────────────────────────────────────────────────────

SEARCH_SYSTEM = (
    "You generate Anki search terms for a medical student's deck."
)

SEARCH_PROMPT = (
    "A medical student got a question wrong in a question bank. "
    "They wrote this short note describing the specific concept they missed:\n\n"
    "\"\"\"{concept}\"\"\"\n\n"
    "Generate 3-5 short Anki search terms (1-3 words each) that would find relevant "
    "flashcards in their deck. Use specific medical terminology — drug names, condition "
    "names, mechanisms, anatomy — rather than generic words like 'treatment' or 'pathology'. "
    "Return ONLY a JSON array of strings, no surrounding text. "
    "Example output: [\"digoxin toxicity\", \"hypokalaemia\", \"Na/K ATPase\"]"
)

CARD_SYSTEM = (
    "You generate AnKing-style cloze flashcards for a medical student."
)

CARD_PROMPT_DEFAULT = (
    "You are helping a medical student create an Anki cloze flashcard for a concept "
    "they got wrong in a question bank.\n\n"
    "CONCEPT MISSED:\n{concept}\n\n"
    "SOURCE QUESTION (may include HTML/image references — ignore image src, focus on text):\n{stem}\n\n"
    "Generate a focused AnKing-style cloze card.\n"
    "- Text field: 1-3 short cloze deletions using {{c1::...}}, {{c2::...}}, etc. — "
    "tightly scoped to what was missed. Do NOT restate the whole question.\n"
    "- Extra field: short context — mechanism, mnemonic, common pitfall, or 1-2 high-yield "
    "clarifications. Keep it under 4 lines.\n"
    "- Use <br> for line breaks and <b> for emphasis. No bullet points unless essential.\n\n"
    "Return ONLY a JSON object with this exact shape (no markdown fences, no surrounding text):\n"
    "{\"text\": \"...\", \"extra\": \"...\"}"
)

CARD_PROMPT_SKILL = (
    "Create an Anki cloze flashcard for a medical student.\n\n"
    "CONCEPT MISSED (in a question bank):\n{concept}\n\n"
    "SOURCE QUESTION (may contain HTML/image refs — ignore image src, focus on text):\n{stem}\n\n"
    "Use the card-creation skill to format the card per its guidance. "
    "Return ONLY a JSON object with this exact shape (no markdown fences, no surrounding prose):\n"
    "{\"text\": \"<cloze content>\", \"extra\": \"<additional context>\"}"
)


# ── search helpers ────────────────────────────────────────────────────────────

def _generate_search_terms(concept: str) -> list[str]:
    cfg = tool_config("qbank")
    model = cfg.get("search_model") or "claude-haiku-4-5-20251001"
    raw = core_api.ask_claude_json(
        prompt=SEARCH_PROMPT.format(concept=concept.strip()),
        system=SEARCH_SYSTEM,
        max_tokens=256,
        model=model,
        show_errors=False,
    )
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def _generate_card_draft(concept: str, stem: str) -> dict:
    cfg = tool_config("qbank")
    model     = cfg.get("card_gen_model") or "claude-sonnet-4-6"
    skill_id  = (cfg.get("card_skill_id") or "").strip()
    template  = cfg.get("card_prompt") or (CARD_PROMPT_SKILL if skill_id else CARD_PROMPT_DEFAULT)
    prompt    = template.replace("{concept}", concept).replace("{stem}", stem)
    raw = core_api.ask_claude_json(
        prompt=prompt,
        system=CARD_SYSTEM,
        max_tokens=1024,
        model=model,
        skill_id=skill_id,
        show_errors=False,
    )
    if isinstance(raw, dict):
        return {"text": str(raw.get("text", "")), "extra": str(raw.get("extra", ""))}
    return {}


# ── UI ────────────────────────────────────────────────────────────────────────

class _MatchRow(QWidget):
    def __init__(self, note_id: int, deck: str, preview: str, parent=None):
        super().__init__(parent)
        self.note_id = note_id
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(6)

        self.check = QCheckBox()
        row.addWidget(self.check)

        deck_html = f"<span style='color:gray;font-size:11px;'>[{deck}]</span> " if deck else ""
        lbl = QLabel(f"{deck_html}{preview}")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        row.addWidget(lbl, stretch=1)

        open_btn = QPushButton("Open")
        open_btn.setFixedWidth(56)
        open_btn.clicked.connect(self._open_in_browser)
        row.addWidget(open_btn)

    def _open_in_browser(self) -> None:
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            browser.show()
            browser.raise_()
            browser.activateWindow()
            try:
                browser.search_for(f"nid:{self.note_id}")
            except RuntimeError:
                pass
        except Exception as e:
            from aqt.utils import showInfo
            showInfo(f"Couldn't open browser:\n{e}")
            print(f"[ankisstant] open in browser failed: {e}")

    def is_checked(self) -> bool:
        return self.check.isChecked()


class ReviewDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Review missed questions")
        self.resize(960, 620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._current_item: dict | None = None
        self._match_rows: list[_MatchRow] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.setSpacing(4)
        lcol.addWidget(QLabel("<b>Captured</b>"))
        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_select)
        lcol.addWidget(self._list)
        splitter.addWidget(left)

        right = QWidget()
        rcol = QVBoxLayout(right)
        rcol.setContentsMargins(0, 0, 0, 0)
        rcol.setSpacing(6)

        self._concept_lbl = QLabel("")
        self._concept_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        self._concept_lbl.setWordWrap(True)
        rcol.addWidget(self._concept_lbl)

        self._stem_view = QTextEdit()
        self._stem_view.setReadOnly(True)
        self._stem_view.setMaximumHeight(180)
        rcol.addWidget(self._stem_view)

        btn_row = QHBoxLayout()
        self._search_btn = QPushButton("🔍 AI search")
        self._search_btn.clicked.connect(self._do_search)
        btn_row.addWidget(self._search_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray; font-size: 11px;")
        self._status_lbl.setWordWrap(True)
        btn_row.addWidget(self._status_lbl, stretch=1)

        del_btn = QPushButton("🗑 Delete")
        del_btn.clicked.connect(self._delete_current)
        btn_row.addWidget(del_btn)
        rcol.addLayout(btn_row)

        rcol.addWidget(QLabel("<b>Matching cards</b>"))
        self._matches_inner  = QWidget()
        self._matches_layout = QVBoxLayout(self._matches_inner)
        self._matches_layout.setContentsMargins(0, 0, 0, 0)
        self._matches_layout.setSpacing(2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._matches_inner)
        rcol.addWidget(scroll, stretch=1)

        action_row = QHBoxLayout()
        self._apply_btn = QPushButton("✓ Apply to ticked  (append + Again)")
        self._apply_btn.clicked.connect(self._apply_ticked)
        self._apply_btn.setEnabled(False)
        action_row.addWidget(self._apply_btn)
        new_btn = QPushButton("+ Create new card")
        new_btn.clicked.connect(self._create_new)
        action_row.addWidget(new_btn)
        rcol.addLayout(action_row)

        hint = QLabel(
            "<span style='color:gray;font-size:10px'>"
            "⚠ Create new card uses the card-gen model + optional skill — "
            "takes ~30–60s. Click once and wait."
            "</span>"
        )
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        rcol.addWidget(hint)

        splitter.addWidget(right)
        splitter.setSizes([290, 670])

        self._reload_queue()

    # ── queue list ────────────────────────────────────────────────────────────

    def _reload_queue(self) -> None:
        self._list.clear()
        for item in load_queue():
            label = item.get("concept") or (item.get("stem", "")[:60] or "(empty)")
            li = QListWidgetItem(label[:80])
            li.setData(Qt.ItemDataRole.UserRole, item)
            self._list.addItem(li)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)
        else:
            self._clear_right()

    def _clear_right(self) -> None:
        self._current_item = None
        self._concept_lbl.setText("(queue empty)")
        self._stem_view.clear()
        self._status_lbl.setText("")
        self._clear_matches()
        self._apply_btn.setEnabled(False)

    def _on_select(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        item = items[0].data(Qt.ItemDataRole.UserRole)
        self._current_item = item
        levels = [item.get("system", ""), item.get("subsystem", ""), item.get("topic", "")]
        if not any(levels) and item.get("stage"):
            levels = [item["stage"]]
        levels_path = " :: ".join(l for l in levels if l)
        levels_html = (
            f"<span style='color:gray;font-size:11px;font-weight:normal;'>  ·  {levels_path}</span>"
            if levels_path else ""
        )
        self._concept_lbl.setText((item.get("concept") or "(no concept note)") + levels_html)
        self._concept_lbl.setTextFormat(Qt.TextFormat.RichText)
        stem = item.get("stem", "") or ""
        if "<" in stem:
            self._stem_view.setHtml(stem)
        else:
            self._stem_view.setPlainText(stem)
        self._status_lbl.setText("")
        self._clear_matches()
        self._apply_btn.setEnabled(False)

    # ── matches ───────────────────────────────────────────────────────────────

    def _clear_matches(self) -> None:
        while self._matches_layout.count():
            it = self._matches_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._match_rows = []

    def _do_search(self) -> None:
        if not self._current_item:
            return
        concept = self._current_item.get("concept", "")
        self._status_lbl.setText("Searching with Claude…")
        self._search_btn.setEnabled(False)

        def _bg():
            return _generate_search_terms(concept)

        def _done(fut):
            try:
                terms = fut.result()
            except Exception as e:
                terms = []
                print(f"[ankisstant] AI search task failed: {e}")
            self._search_btn.setEnabled(True)
            self._populate_matches(terms)

        mw.taskman.run_in_background(_bg, _done)

    def _populate_matches(self, terms: list[str]) -> None:
        self._clear_matches()
        if not terms:
            self._status_lbl.setText("AI returned no terms (check API key / console log).")
            return
        note_ids = anki_utils.find_notes_for_terms(terms)
        self._status_lbl.setText(
            f"Terms: {', '.join(terms)} &nbsp;→&nbsp; {len(note_ids)} match(es)"
        )
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)

        if not note_ids:
            empty = QLabel("No matching cards found in your collection.")
            empty.setStyleSheet("color: gray; font-size: 11px; padding: 8px;")
            self._matches_layout.addWidget(empty)
            self._matches_layout.addStretch()
            return

        for nid in note_ids:
            deck, preview = anki_utils.note_preview(nid)
            row = _MatchRow(nid, deck, preview)
            self._match_rows.append(row)
            self._matches_layout.addWidget(row)
        self._matches_layout.addStretch()
        self._apply_btn.setEnabled(True)

    # ── actions ───────────────────────────────────────────────────────────────

    def _apply_ticked(self) -> None:
        if not self._current_item:
            return
        ticked = [r for r in self._match_rows if r.is_checked()]
        if not ticked:
            tooltip("No cards ticked.")
            return
        cfg     = tool_config("qbank")
        field   = cfg.get("missed_q_field") or "Missed Questions"
        root    = cfg.get("tag_root") or "Missed_Questions"
        system    = self._current_item.get("system", "") or self._current_item.get("stage", "")
        subsystem = self._current_item.get("subsystem", "")
        topic     = self._current_item.get("topic", "")
        stem    = self._current_item.get("stem", "")
        concept = self._current_item.get("concept", "")

        parts = []
        if concept:
            parts.append(f"<b>{concept}</b>")
        if stem:
            parts.append(stem)
        content = "<br>".join(parts)

        tag = anki_utils.build_tag(root, system, subsystem, topic)
        note_ids = [r.note_id for r in ticked]

        appended = again = unsuspended = skipped = 0
        for nid in note_ids:
            if anki_utils.append_to_field(nid, field, content):
                appended += 1
            else:
                skipped += 1
            graded_n, unsus_n = mark_again(nid)
            again       += graded_n
            unsuspended += unsus_n
        tagged = anki_utils.tag_notes(note_ids, tag)

        remove_item(self._current_item["id"])
        bits = [f"Appended to {appended}"]
        if again:
            bits.append(f"re-rated {again}")
        if unsuspended:
            bits.append(f"unsuspended {unsuspended}")
        bits.append(f"tagged {tagged}")
        msg = ", ".join(bits) + "."
        if skipped:
            msg += f" Skipped {skipped} (no '{field}' field)."
        msg += f"\nTag: {tag}"
        tooltip(msg, period=5500)
        self._reload_queue()

    def _create_new(self) -> None:
        if not self._current_item:
            return
        if not anki_utils.require_col():
            return
        cfg          = tool_config("qbank")
        notetype     = (cfg.get("card_notetype") or "").strip()
        deck         = cfg.get("card_deck") or ""
        miss_field   = cfg.get("missed_q_field") or "Missed Questions"
        tag_root     = cfg.get("tag_root") or "Missed_Questions::System"

        if not notetype:
            showWarning(
                "QBank can't open Add Cards because no notetype is configured.\n\n"
                "Open Ankisstant Settings → QBank and set 'card_notetype'."
            )
            return
        nt = mw.col.models.by_name(notetype)
        if nt is None:
            showWarning(
                f"QBank notetype '{notetype}' not found.\n\n"
                "Update 'card_notetype' under Ankisstant Settings → QBank."
            )
            return
        field_names = {f["name"] for f in nt.get("flds", [])}
        if miss_field and miss_field not in field_names:
            showWarning(
                f"Notetype '{notetype}' has no '{miss_field}' field.\n\n"
                f"Available fields: {', '.join(sorted(field_names)) or '(none)'}\n\n"
                "Update 'missed_q_field' under Ankisstant Settings → QBank."
            )
            return

        stem      = self._current_item.get("stem", "")
        concept   = self._current_item.get("concept", "")
        system    = self._current_item.get("system", "") or self._current_item.get("stage", "")
        subsystem = self._current_item.get("subsystem", "")
        topic     = self._current_item.get("topic", "")
        tag       = anki_utils.build_tag(tag_root, system, subsystem, topic)
        item_id   = self._current_item["id"]

        self._status_lbl.setText("Drafting card with Claude…")

        def _bg():
            return _generate_card_draft(concept, stem)

        def _done(fut):
            try:
                draft = fut.result() or {}
            except Exception as e:
                draft = {}
                print(f"[ankisstant] card-gen task failed: {e}")

            text_field  = draft.get("text",  "").strip() or (f"{{{{c1::{concept}}}}}" if concept else "")
            extra_field = draft.get("extra", "").strip()

            fields = {
                "Text":     text_field,
                "Extra":    extra_field,
                miss_field: stem,
            }
            anki_utils.open_add_card_prefilled(
                fields=fields,
                notetype_name=notetype,
                deck_name=deck,
                tag=tag,
            )
            self._status_lbl.setText(
                "Card drafted by Claude — review and Add."
                if draft else "AI draft failed (see console). Opened Add Cards anyway."
            )
            remove_item(item_id)
            self._reload_queue()

        mw.taskman.run_in_background(_bg, _done)

    def _delete_current(self) -> None:
        if not self._current_item:
            return
        remove_item(self._current_item["id"])
        self._reload_queue()


_current_review: "ReviewDialog | None" = None


def _is_live(dlg) -> bool:
    if dlg is None:
        return False
    try:
        return dlg.isVisible()
    except RuntimeError:
        return False


def open_review() -> None:
    global _current_review
    if _is_live(_current_review):
        _current_review.raise_()
        _current_review.activateWindow()
        return
    _current_review = ReviewDialog(mw)
    _current_review.show()
    _current_review.raise_()
    _current_review.activateWindow()
