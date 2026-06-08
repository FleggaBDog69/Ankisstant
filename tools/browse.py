# AI Browse — find Anki cards for a topic via AI-generated search terms,
# then tag and unsuspend in one step.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

import html as _html
import re
import threading

from aqt import mw
from aqt.qt import (
    QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QRadioButton, Qt, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, api as core_api, log
from ..core.config import (
    active_family, auto_tag_base, month_tag,
    mq_explain_enabled, tool_config, tool_model_for, save_tool_config,
)
from ..core.qt_utils import (
    attach_tag_completer, is_manual_provider, loading, make_help_button,
    make_setup_banner, provider_configured, run_claude_json, set_ai_buttons_enabled,
)
from . import autotag
from .kg import engine


NAME = "AI Browse"


# ── prompts ───────────────────────────────────────────────────────────────────

SEARCH_TERMS_SYSTEM = (
    "You generate Anki search terms for a medical student's deck. Given a topic, "
    "return a JSON array of 3 to 8 HIGHLY SPECIFIC search strings that surface "
    "cards genuinely about that topic — not cards that just mention it in passing.\n\n"
    "DECOMPOSITION (critical):\n"
    "If the input combines MORE THAN ONE distinct clinical concept — joined by "
    "'and', commas, semicolons, or expressed as a chain of reasoning failures "
    "('failed to recognise X, missed Y, didn't act on Z') — first SPLIT it into "
    "the underlying atomic concepts, then generate search terms for each. Don't "
    "try to find a single card matching the whole compound statement: the user's "
    "deck almost certainly has the pieces scattered across separate cards.\n\n"
    "Examples of decomposition:\n"
    "- Input: 'MRI is first line for HA with neuro deficit, failed to recognise "
    "red flags' → search for {headache red flags} AND {imaging for headache "
    "with focal neuro deficits} as separate concept groups.\n"
    "- Input: 'aortic stenosis murmur and indications for valve replacement' → "
    "{aortic stenosis murmur characteristics} AND {AVR indications}.\n"
    "- Input: single concept like 'McDonald criteria' → no decomposition, just "
    "generate variants on that one concept.\n\n"
    "RULES:\n"
    "- Prefer multi-word phrases and eponyms over single common words.\n"
    "- Avoid generic 1–3 letter abbreviations on their own (e.g. 'MS', 'DM', 'IV') — "
    "they collide with too many unrelated cards. Disambiguate them: 'multiple sclerosis', "
    "'McDonald criteria', not 'MS'.\n"
    "- Include classic exam-relevant entities: pathognomonic signs, key drugs, "
    "diagnostic criteria, eponyms — but ONLY if tightly bound to the topic.\n"
    "- For narrow single-concept topics, return 3–4 terms. For decomposed "
    "multi-concept inputs, return 2–3 terms per concept (up to 8 total). "
    "Quality over quantity.\n\n"
    "Return ONLY the JSON array of strings. No prose, no quotes around the array."
)

RESCOPE_SYSTEM = (
    "You regenerate Anki search terms for a medical student. You will be given a topic, "
    "the previous set of search terms, and a directive to go BROADER or NARROWER.\n\n"
    "BROADER: pull back a level of abstraction. Include adjacent / parent concepts, "
    "the wider disease category, related syndromes, broader exam topics. Replace "
    "narrow eponyms with their umbrella category.\n"
    "NARROWER: drill in. Use more specific sub-entities, drug names, exact "
    "diagnostic criteria, named signs, specific complications. Replace umbrella "
    "categories with their highest-yield sub-entries.\n\n"
    "Same JSON-array-of-strings format as before. 3–6 items. No prose."
)

# The search-terms request now folds a hierarchical tag suggestion and/or an MQ
# knowledge-gap explanation into the SAME round-trip via the declarative engine
# (tools/kg/engine.py) — see _search_with_system. When either is requested the
# reply becomes an object instead of a bare array.

TAG_SEARCH_SYSTEM = (
    "You generate Anki tag-name keywords for a medical student. Given a topic, "
    "return a JSON array of keyword strings that match tag names for THAT topic "
    "and its tightly-coupled disease entities only.\n\n"
    "STAY TIGHT — this is the most important rule:\n"
    "- Return the topic itself, plus AT MOST a couple of closely-related disease "
    "entities or direct differentials (e.g. for 'multiple sclerosis': "
    "multiple_sclerosis, optic_neuritis).\n"
    "- DO NOT branch out to the individual signs, symptoms, lab findings, "
    "investigations, or buzzwords associated with the topic. For 'multiple "
    "sclerosis' that means NO oligoclonal_bands, NO Uhthoff, NO MRI — those flood "
    "the results with tangents. Only widen to a separate condition, never to a "
    "feature of the topic.\n"
    "- 2 to 4 items, fewer is better. Quality over coverage.\n\n"
    "Format — JSON array of objects exactly like:\n"
    '[{"keyword": "multiple_sclerosis", "resource": "Boards & Beyond — Neuro: MS", "step": "Step 1"}, ...]\n\n'
    "RULES:\n"
    "- keyword is matched case-insensitively as a substring against existing tag names, "
    "so prefer the canonical form (snake_case or PascalCase, no spaces).\n"
    "- Avoid 1–3 letter abbreviations on their own. Disambiguate them (e.g. 'multiple_sclerosis', "
    "not 'MS').\n"
    "- resource is a concise study reference (Boards & Beyond chapter, Pathoma section, "
    "First Aid page-range, etc.).\n"
    "- step is one of 'Step 1', 'Step 2 CK', 'Step 3', or 'Step 1+2' if it spans both. "
    "Use 'AMC' for Australian-context entries when clearly post-graduation.\n\n"
    "Return ONLY the JSON array. No prose."
)

# ── helpers ──────────────────────────────────────────────────────────────────

_STEP_RE = re.compile(r"step[ _]?([123])", re.IGNORECASE)


def _step_label(segs: list[str]) -> str:
    """Pull a USMLE step from a tag's segments (e.g. '#AK_Step1_v12' → 'Step 1').
    Returns '' when the tag isn't step-tagged (e.g. '#Malleus_CM')."""
    for s in segs:
        m = _STEP_RE.search(s)
        if m:
            return f"Step {m.group(1)}"
    return ""


def _front_preview(note, front_field: str, limit: int = 140) -> str:
    fld = note[front_field] if front_field in note else note.fields[0]
    text = anki_utils.strip_html(fld)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _source_label(tags, source_tags) -> str:
    for entry in source_tags or []:
        try:
            label, needle = entry
        except (ValueError, TypeError):
            continue
        if not needle:
            continue
        for t in tags:
            if needle in t:
                return label
    return ""


# ── panel ────────────────────────────────────────────────────────────────────

class BrowsePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("browse")
        self._results: list = []
        self._last_topic = ""
        self._last_terms: list[str] = []
        # Auto-tag respects an existing tag: don't overwrite one carried from a
        # loaded KG, nor one the user typed by hand. Both flags reset when the
        # user edits the topic (a fresh topic deserves a fresh suggestion).
        self._tag_from_kg = False
        self._tag_user_set = False
        # "notes" — Claude → search terms → notes (the original behaviour).
        # "tags" — Claude → tag keywords → matching tags (study-planning mode).
        self._mode: str = "notes"
        # When preloaded from a KG, stamp the id here so a successful
        # tag+unsuspend can mark that KG done. Captured content + type ride
        # along: MQ-type KGs append the full captured missed-question (stem
        # HTML + images) to the Missed Questions field and re-rate any
        # already-unsuspended cards as Again so they resurface today.
        # Non-MQ types just append their images to Extra.
        self._linked_kg_id: str | None = None
        self._linked_kg_images: list[str] = []
        self._linked_kg_stem_html: str = ""
        self._linked_kg_type: str = ""
        # MQ KGs lead the appended content with the specific concept missed and
        # an AI-written explanation of it (above the stem/screenshot), matching
        # the Create flow. The explanation is folded into the search request.
        self._linked_kg_concept: str = ""
        self._linked_kg_explanation: str = ""
        self._build()

    def preload_for_kg(self, kg: dict) -> None:
        """Preload the panel from a KG and remember its id so a successful
        tag/unsuspend can mark the KG done."""
        title = (kg.get("title") or "").strip()
        if title:
            self.topic.setText(title)
        self._linked_kg_id = kg.get("id") or None
        fields_blob = kg.get("fields") or {}
        self._linked_kg_images = list(fields_blob.get("images") or [])
        self._linked_kg_stem_html = str(
            fields_blob.get("stem_html") or kg.get("stem_html") or ""
        )
        self._linked_kg_type = (kg.get("type") or "").lower()
        self._linked_kg_concept = str(fields_blob.get("concept") or "").strip()
        self._linked_kg_explanation = str(fields_blob.get("explanation") or "").strip()
        # Carry the KG's tag over so "Tag & Unsuspend" reuses it. We're
        # explicitly loading this KG, so override whatever's there (it was
        # only the last-used tag). Prefer a tag the user assigned in the KG
        # menu; fall back to the auto-generated hierarchical tag.
        manual_tags = kg.get("tags") or []
        auto_tag = (fields_blob.get("auto_tag") or "").strip()
        carry = (manual_tags[0] if manual_tags else "") or auto_tag
        if carry:
            self.tag_input.setText(carry)
            self._tag_from_kg = True
        self._update_autotag_hint(kg)
        self._last_topic = title
        self._last_terms = []

    def _store_linked_kg_explanation(self, text: str) -> None:
        """Cache an MQ explanation on the panel and persist it to the linked
        KG's store record so Create reuses it and it isn't regenerated."""
        text = (text or "").strip()
        if not text:
            return
        self._linked_kg_explanation = text
        if not self._linked_kg_id:
            return
        try:
            from .kg import store as kg_store
            existing = kg_store.get(self._linked_kg_id)
            if existing:
                fields = dict(existing.get("fields") or {})
                fields["explanation"] = text
                kg_store.update(self._linked_kg_id, fields=fields)
        except Exception as e:
            print(f"[ankisstant] browse mq explanation persist failed: {e}")

    def _update_autotag_hint(self, kg: dict | None) -> None:
        """No-op: the loaded-KG tag notice was removed (the tag is already
        visible in the tag box). Kept as a stub for existing callers."""
        return

    # ── queue (Knowledge Gaps → Browse) ────────────────────────────────────────

    @staticmethod
    def _kg_title(kg: dict) -> str:
        return (kg.get("title") or "").strip() or "(untitled KG)"

    def _form_is_dirty(self) -> bool:
        return bool(self.topic.text().strip())

    def refresh_queue_state(self, main_window) -> None:
        """Called by the main window when Browse is shown. Rebuilds the queue
        view and preloads the top KG if the user hasn't typed their own topic."""
        self._main_window = main_window
        self._rebuild_queue_view()
        queue = getattr(main_window, "browse_queue", []) or []
        if not queue:
            return
        top = queue[0]
        if isinstance(top, dict) and top.get("id") == self._linked_kg_id:
            return  # already loaded
        if self._form_is_dirty():
            return  # don't clobber what the user is working on
        self.preload_for_kg(top)

    def _rebuild_queue_view(self) -> None:
        queue = (
            getattr(self._main_window, "browse_queue", []) if self._main_window else []
        ) or []
        self.queue_list.clear()
        # Hide the whole box when empty so Browse stays uncluttered for ad-hoc
        # searches that don't come from the KG queue.
        self.queue_box.setVisible(bool(queue))
        if not queue:
            return
        self.queue_header.setText(
            f"<span>🔍 {len(queue)} KG(s) queued for Browse</span>"
        )
        for i, kg in enumerate(queue):
            marker = "▶ " if i == 0 else ""
            QListWidgetItem(f"{marker}{self._kg_title(kg)[:80]}", self.queue_list)

    def _refresh_queue_badge(self) -> None:
        if self._main_window is not None and hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()

    def _on_load_top_kg(self) -> None:
        queue = getattr(self._main_window, "browse_queue", None) or []
        if not queue:
            tooltip("Browse queue is empty.")
            return
        top = queue[0]
        if (self._form_is_dirty()
                and self.topic.text().strip() != self._kg_title(top)
                and not askUser(
                    "Overwrite the topic with the next queued KG?", defaultno=True)):
            return
        self.preload_for_kg(top)
        tooltip(f"Loaded: {self._kg_title(top)[:60]}")

    def _on_skip_kg(self) -> None:
        queue = getattr(self._main_window, "browse_queue", None) or []
        if not queue:
            return
        skipped = queue.pop(0)
        if self._linked_kg_id == (skipped.get("id") if isinstance(skipped, dict) else None):
            self._linked_kg_id = None
        self._refresh_queue_badge()
        self._rebuild_queue_view()
        if queue and not self._form_is_dirty():
            self.preload_for_kg(queue[0])
        tooltip(f"Skipped: {self._kg_title(skipped)[:60]}")

    def _on_remove_selected_kg(self) -> None:
        queue = getattr(self._main_window, "browse_queue", None) or []
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(queue):
            tooltip("Select a queued KG first.")
            return
        removed = queue.pop(row)
        self._refresh_queue_badge()
        self._rebuild_queue_view()
        tooltip(f"Removed: {self._kg_title(removed)[:60]}")

    def _on_clear_queue(self) -> None:
        queue = getattr(self._main_window, "browse_queue", None) or []
        if not queue:
            return
        if not askUser(f"Discard all {len(queue)} KG(s) queued for Browse?"):
            return
        queue.clear()
        self._refresh_queue_badge()
        self._rebuild_queue_view()

    def _advance_queue_after_done(self, done_kg_id: str) -> None:
        """After a KG is marked done by a successful Tag & Unsuspend, drop it
        from the Browse queue and load the next one."""
        queue = getattr(self._main_window, "browse_queue", None)
        if not queue:
            return
        queue[:] = [kg for kg in queue
                    if not (isinstance(kg, dict) and kg.get("id") == done_kg_id)]
        self._refresh_queue_badge()
        self._rebuild_queue_view()
        if queue and not self._form_is_dirty():
            self.preload_for_kg(queue[0])

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>AI Browse</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "AI Browse — help",
            "<h3>What it does</h3>"
            "<p>You type a topic. AI returns a short list of Anki search "
            "terms. The addon runs each search against your collection and shows "
            "the union, ranked by AI term order.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Type a topic (broad like 'MS' or specific like 'McDonald criteria').</li>"
            "<li>Click <b>Search</b>. Results appear with [deck] tags.</li>"
            "<li>Use <b>Broader</b> / <b>Narrower</b> to rescope if needed.</li>"
            "<li>Tick the cards you want, type a tag, click <b>Tag &amp; Unsuspend</b>.</li>"
            "</ol>"
            "<h3>Settings</h3>"
            "<p>Notetype filter, max results, source-tag filter and audit tag are "
            "in <b>Ankisstant Settings → AI Browse</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        # ── Queue panel — KGs sent here from the Knowledge Gaps page. Mirrors
        # the Create queue: work through them one at a time; a successful
        # Tag & Unsuspend marks the current KG done and advances. ────────────
        self._main_window = None
        self.queue_box = QFrame()
        self.queue_box.setObjectName("browseQueueBox")
        self.queue_box.setFrameShape(QFrame.Shape.StyledPanel)
        self.queue_box.setStyleSheet(
            "QFrame#browseQueueBox { background: rgba(80,160,255,0.16); "
            "border: 1px solid rgba(80,160,255,0.55); border-radius: 6px; }"
        )
        qbl = QVBoxLayout(self.queue_box)
        qbl.setContentsMargins(10, 8, 10, 8)
        qbl.setSpacing(5)
        self.queue_header = QLabel()
        self.queue_header.setStyleSheet("font-weight: 600; color: palette(text);")
        self.queue_header.setTextFormat(Qt.TextFormat.RichText)
        self.queue_header.setWordWrap(True)
        qbl.addWidget(self.queue_header)
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(120)
        self.queue_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; color: palette(text); }"
            "QListWidget::item:selected { background: rgba(80,160,255,0.30); color: palette(text); }"
        )
        qbl.addWidget(self.queue_list)
        queue_btn_row = QHBoxLayout()
        self.queue_remove_btn = QPushButton("Remove selected")
        self.queue_remove_btn.setAutoDefault(False)
        self.queue_remove_btn.clicked.connect(self._on_remove_selected_kg)
        queue_btn_row.addWidget(self.queue_remove_btn)
        queue_btn_row.addStretch(1)
        self.queue_load_btn = QPushButton("Load next →")
        self.queue_load_btn.setAutoDefault(False)
        self.queue_load_btn.setToolTip("Preload the top queued KG into the search above.")
        self.queue_load_btn.clicked.connect(self._on_load_top_kg)
        self.queue_skip_btn = QPushButton("Skip current")
        self.queue_skip_btn.setAutoDefault(False)
        self.queue_skip_btn.clicked.connect(self._on_skip_kg)
        self.queue_clear_btn = QPushButton("Clear queue")
        self.queue_clear_btn.setAutoDefault(False)
        self.queue_clear_btn.clicked.connect(self._on_clear_queue)
        queue_btn_row.addWidget(self.queue_load_btn)
        queue_btn_row.addWidget(self.queue_skip_btn)
        queue_btn_row.addWidget(self.queue_clear_btn)
        qbl.addLayout(queue_btn_row)
        root.addWidget(self.queue_box)

        # Mode toggle — Notes vs Tags.
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self._notes_radio = QRadioButton("Notes")
        self._notes_radio.setToolTip("Find individual cards/notes matching a topic.")
        self._tags_radio = QRadioButton("Tags")
        self._tags_radio.setToolTip(
            "Find tag groups for a topic — useful for planning study before creating cards."
        )
        self._notes_radio.setChecked(True)
        self._notes_radio.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self._notes_radio)
        mode_row.addWidget(self._tags_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        root.addWidget(QLabel("Topic (broad or narrow — AI figures out the search terms):"))
        topic_row = QHBoxLayout()
        self.topic = QLineEdit()
        self.topic.setMinimumWidth(500)
        self.topic.setPlaceholderText("e.g. MS, or 'progression of disease in MS'")
        self.topic.returnPressed.connect(self._on_search)
        # A fresh topic deserves a fresh auto-tag suggestion.
        self.topic.textEdited.connect(self._on_topic_edited)
        topic_row.addWidget(self.topic, 1)
        self.search_btn = QPushButton("Search")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self._on_search)
        topic_row.addWidget(self.search_btn)
        root.addLayout(topic_row)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray; font-size: 11px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # Tag-mode-only: favourites + filter/sort. A topic search can surface a
        # flood of matching tags; the user curates a set of favourite resource/
        # branch tags (any hierarchy level) and then filters/sorts the results
        # by which ones their favourites actually live on.
        self._fav_row = QWidget()
        fav_layout = QHBoxLayout(self._fav_row)
        fav_layout.setContentsMargins(0, 0, 0, 0)
        self.fav_btn = QPushButton("★ Favourite tags…")
        self.fav_btn.setAutoDefault(False)
        self.fav_btn.setToolTip(
            "Pick the resource/branch tags you study from. Results can then be "
            "filtered or sorted by which of your favourites they sit under."
        )
        self.fav_btn.clicked.connect(self._open_favourites_dialog)
        fav_layout.addWidget(self.fav_btn)
        self.fav_only_cb = QCheckBox("★ only")
        self.fav_only_cb.setToolTip("Show only tags that sit on cards from your favourite tags.")
        self.fav_only_cb.toggled.connect(lambda *_: self._render_tag_rows())
        fav_layout.addWidget(self.fav_only_cb)
        fav_hint = QLabel("Results group by your favourite order (Pathoma → B&B → …).")
        fav_hint.setStyleSheet("color: gray; font-size: 11px;")
        fav_layout.addWidget(fav_hint)
        fav_layout.addStretch(1)
        self._fav_row.setVisible(False)
        root.addWidget(self._fav_row)

        self.results_list = QListWidget()
        self.results_list.setAlternatingRowColors(True)
        self.results_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        root.addWidget(self.results_list, 1)

        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(self._select_all)
        sel_none = QPushButton("Select none")
        sel_none.clicked.connect(self._select_none)
        self.open_in_browser_btn = QPushButton("Open in browser")
        self.open_in_browser_btn.setToolTip(
            "Open every ticked note (or tag) in Anki's built-in browser. "
            "Same as double-clicking a single row, but works on the full "
            "selection at once."
        )
        self.open_in_browser_btn.setAutoDefault(False)
        self.open_in_browser_btn.clicked.connect(self._on_open_selected_in_browser)
        self.broader_btn = QPushButton("⬅ Broader")
        self.broader_btn.setToolTip("Re-run the search with wider, more general terms.")
        self.broader_btn.clicked.connect(lambda: self._rescope("broader"))
        self.broader_btn.setEnabled(False)
        self.broader_btn.setAutoDefault(False)
        self.narrower_btn = QPushButton("Narrower ➡")
        self.narrower_btn.setToolTip("Re-run the search with tighter, more specific terms.")
        self.narrower_btn.clicked.connect(lambda: self._rescope("narrower"))
        self.narrower_btn.setEnabled(False)
        self.narrower_btn.setAutoDefault(False)
        sel_row.addWidget(sel_all)
        sel_row.addWidget(sel_none)
        sel_row.addWidget(self.open_in_browser_btn)
        sel_row.addSpacing(12)
        sel_row.addWidget(self.broader_btn)
        sel_row.addWidget(self.narrower_btn)
        sel_row.addStretch(1)
        self.count_label = QLabel("0 selected")
        sel_row.addWidget(self.count_label)
        root.addLayout(sel_row)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tag to apply:"))
        self.tag_input = QLineEdit(self.cfg.get("last_used_tag", ""))
        self.tag_input.setMinimumWidth(400)
        attach_tag_completer(self.tag_input, multi=False)
        # A hand-typed tag is sacred — don't let auto-tag clobber it.
        self.tag_input.textEdited.connect(lambda *_: setattr(self, "_tag_user_set", True))
        tag_row.addWidget(self.tag_input, 1)
        root.addLayout(tag_row)
        audit_hint = QLabel(
            f"<small>Audit tag <code>{self.cfg.get('audit_tag', '')}</code> "
            "is also applied to every unsuspended note so you can find them later.</small>"
        )
        audit_hint.setTextFormat(Qt.TextFormat.RichText)
        audit_hint.setStyleSheet("color: gray;")
        root.addWidget(audit_hint)

        # Side-effect toggles — each side effect of Confirm is optional so
        # users can use Browse as a pure search/tag tool when they don't
        # want to disturb the scheduler. Defaults mirror prior behaviour.
        toggles_row = QHBoxLayout()
        self.cb_autotag = QCheckBox("Auto-tag")
        self.cb_autotag.setChecked(autotag.is_enabled(self.cfg))
        self.cb_autotag.setToolTip(
            "Suggest a hierarchical tag (base::Type::System::Subsystem::Topic) "
            "for a fresh topic search and fill it into the tag box. Same scheme "
            "as AI Create. A tag you type yourself is never overwritten."
        )
        self.cb_autotag.toggled.connect(self._on_toggle_persist)
        toggles_row.addWidget(self.cb_autotag)
        self.cb_unsuspend = QCheckBox("Unsuspend")
        self.cb_unsuspend.setChecked(bool(self.cfg.get("auto_unsuspend", True)))
        self.cb_unsuspend.setToolTip("Unsuspend every card on the selected notes.")
        self.cb_unsuspend.toggled.connect(self._on_toggle_persist)
        self.cb_audit = QCheckBox("Apply audit tag")
        self.cb_audit.setChecked(bool(self.cfg.get("auto_audit_tag", True)))
        self.cb_audit.setToolTip(
            "Also apply the audit tag from settings so you can find these notes later."
        )
        self.cb_audit.toggled.connect(self._on_toggle_persist)
        self.cb_grade = QCheckBox("Grade Again (MQ only)")
        self.cb_grade.setChecked(bool(self.cfg.get("auto_grade_again_mq", True)))
        self.cb_grade.setToolTip(
            "For MQ-linked KGs, re-rate every selected card as Again so it "
            "resurfaces today (uses FSRS-correct lapse, not a queue hack)."
        )
        self.cb_grade.toggled.connect(self._on_toggle_persist)
        toggles_row.addWidget(self.cb_unsuspend)
        toggles_row.addWidget(self.cb_audit)
        toggles_row.addWidget(self.cb_grade)
        toggles_row.addStretch(1)
        root.addLayout(toggles_row)

        confirm_row = QHBoxLayout()
        confirm_row.addStretch(1)
        self.confirm_btn = QPushButton("Tag + Unsuspend")
        self.confirm_btn.setAutoDefault(False)
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self._on_confirm)
        confirm_row.addWidget(self.confirm_btn)
        root.addLayout(confirm_row)

        self.results_list.itemChanged.connect(self._update_count)

    def _on_toggle_persist(self, _checked: bool) -> None:
        """Save toggle state to config so the user's preferences persist
        across panel rebuilds."""
        try:
            self.cfg["auto_tag"]            = bool(self.cb_autotag.isChecked())
            self.cfg["auto_unsuspend"]      = bool(self.cb_unsuspend.isChecked())
            self.cfg["auto_audit_tag"]      = bool(self.cb_audit.isChecked())
            self.cfg["auto_grade_again_mq"] = bool(self.cb_grade.isChecked())
            save_tool_config("browse", self.cfg)
        except Exception as e:
            print(f"[ankisstant] toggle persist failed: {e}")

    def _on_open_selected_in_browser(self) -> None:
        """Open every ticked row in the Anki browser. Works for both
        note-mode (nid: …) and tag-mode (tag:…) results."""
        try:
            from aqt import dialogs
        except Exception as e:
            print(f"[ankisstant] open-in-browser: dialogs unavailable: {e}")
            return
        if self._mode == "tags":
            rows = self._selected_tag_rows()
            if not rows:
                tooltip("Tick at least one tag first.")
                return
            query = " OR ".join(
                r["query"] if r.get("query") else f'tag:"{r["tag"]}"' for r in rows
            )
        else:
            nids = self._selected_nids()
            if not nids:
                tooltip("Tick at least one note first.")
                return
            query = " OR ".join(f"nid:{nid}" for nid in nids)
        try:
            browser = dialogs.open("Browser", mw)
            if hasattr(browser, "search_for_terms"):
                browser.search_for_terms(query)
            else:
                browser.form.searchEdit.lineEdit().setText(query)
                browser.onSearchActivated()
        except Exception as e:
            print(f"[ankisstant] couldn't open browser for selection: {e}")

    def refresh_setup_banner(self) -> None:
        try:
            ok = provider_configured()
            self._setup_banner.setVisible(not ok)
            set_ai_buttons_enabled([getattr(self, "search_btn", None)], ok)
        except Exception:
            pass

    def showEvent(self, ev):
        super().showEvent(ev)
        # Refresh the tag completer on every show — tags added since this
        # panel was first built won't appear otherwise.
        try:
            attach_tag_completer(self.tag_input, multi=False)
        except Exception as e:
            print(f"[ankisstant] tag completer refresh failed: {e}")

    # ── search ────────────────────────────────────────────────────────────────

    def _on_mode_changed(self, _checked: bool) -> None:
        self._mode = "tags" if self._tags_radio.isChecked() else "notes"
        # Different result kinds → clear what's there so the UI is consistent.
        self.results_list.clear()
        self._results = []
        self._last_terms = []
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        self._fav_row.setVisible(self._mode == "tags")
        if self._mode == "tags":
            self.status.setText("Tags mode — find tag groups for study planning.")
        else:
            self.status.setText("")

    def _on_topic_edited(self, *_):
        # New topic context: forget the loaded-KG / hand-typed tag guards so the
        # next search can auto-suggest a fresh hierarchical tag.
        self._tag_from_kg = False
        self._tag_user_set = False

    def _on_search(self):
        if not anki_utils.require_col():
            return
        topic = self.topic.text().strip()
        if not topic:
            return
        self._last_topic = topic
        if self._mode == "tags":
            self._search_tags(topic)
        else:
            self._search_with_system(
                SEARCH_TERMS_SYSTEM, f"Topic: {topic}",
                status_label="Asking AI for search terms…",
                want_tag=True,
            )

    def _rescope(self, direction: str):
        if not self._last_topic or not self._last_terms:
            return
        user_msg = (
            f"Topic: {self._last_topic}\n"
            f"Previous terms: {self._last_terms}\n"
            f"Directive: {direction.upper()}"
        )
        self._search_with_system(
            RESCOPE_SYSTEM, user_msg,
            status_label=f"Asking AI for {direction} terms…",
        )

    def _search_with_system(self, system_prompt, user_prompt, status_label,
                            want_tag: bool = False):
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.status.setText(status_label)

        # Fold a hierarchical auto-tag request into the same prompt when it's a
        # fresh topic search, Browse auto-tag is on, and the user hasn't already
        # got a tag in the field (carried from a loaded KG or hand-typed). The
        # scheme is the SHARED one (base + type), not a Browse-specific prefix.
        # Type segment: the loaded KG's type, else "KG" for a free search.
        base = auto_tag_base()
        kg_type_key = (getattr(self, "_linked_kg_type", "")
                       if getattr(self, "_linked_kg_id", None) else "") or autotag.default_type_key()
        # Gate: the Auto-tag checkbox (shared toggle with AI Create) + a base is
        # set + we're not about to clobber a tag carried from a KG or hand-typed.
        merge_tag = (want_tag and autotag.is_enabled(self.cfg)
                     and bool(base)
                     and not self._tag_from_kg and not self._tag_user_set)
        # MQ explanation: fold it into this same search request when a missed-
        # question KG is loaded, the feature's on, and we don't already have one.
        merge_explanation = (
            want_tag and mq_explain_enabled()
            and bool(getattr(self, "_linked_kg_id", None))
            and self._linked_kg_type == "mq"
            and not self._linked_kg_explanation.strip()
            and bool((self._linked_kg_concept
                      or self.topic.text()).strip())
        )
        # Build the one-call request from the linked KG type's declarative field
        # specs (tools/kg/engine.py): the AI returns the search terms plus, when
        # wanted, a tag classification and the MQ explanation in a single object.
        # The object form is requested only when there's something extra to fold
        # in (tag or explanation); otherwise the reply stays a bare terms array.
        types = tool_config("knowledge_gaps").get("types") or []
        type_meta = next((t for t in types if isinstance(t, dict)
                          and str(t.get("key", "")).lower() == kg_type_key), None)
        exclude = set() if merge_explanation else {"explanation"}
        plan, req_fields = engine.plan_for(
            type_meta, "browse", want_cards=False, want_terms=True,
            want_tag=merge_tag, exclude=exclude)
        if merge_tag or merge_explanation:
            obj = engine.build_object_instructions(plan, req_fields)
            if obj:
                system_prompt = system_prompt + obj
            if merge_explanation:
                concept = (self._linked_kg_concept or self.topic.text()).strip()
                user_prompt += "\n\nCONTEXT for the AI-written fields:\nconcept: " + concept

        model = tool_model_for("search")
        reply = run_claude_json(
            self.search_btn, "Asking AI…",
            prompt=user_prompt,
            system=system_prompt,
            max_tokens=512,
            model=model,
        )

        # Route the reply with the engine: an object {tags, explanation, terms}
        # when extras were folded in, else a bare terms array.
        routed = engine.route(reply, plan)
        terms = routed.terms if routed.terms else (reply if isinstance(reply, list) else None)
        if merge_tag and isinstance(routed.tag_levels, dict):
            tag = autotag.tag_from_levels(routed.tag_levels, kg_type_key=kg_type_key)
            if tag:
                self.tag_input.setText(tag)
        if merge_explanation:
            self._store_linked_kg_explanation(
                str(routed.field_values.get("explanation") or ""))

        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            self._refresh_rescope_enabled()
            self.status.setText("")
            return  # ask_claude_json already showed a tooltip

        terms = [t.strip() for t in terms if t.strip()]
        self._last_terms = terms
        self.status.setText("Searching Anki for: " + ", ".join(terms))
        QApplication.processEvents()

        notetype_filter = (self.cfg.get("notetype_filter") or "").strip()
        max_results = int(self.cfg.get("max_results", 50))

        with loading(self.search_btn, "Searching Anki…"):
            results, per_term = self._run_searches(terms, notetype_filter, max_results)
            if not results and notetype_filter:
                log.info("notetype filter returned 0 — retrying without it.")
                results, per_term = self._run_searches(terms, "", max_results)

        log.debug(f"per-term hits: {per_term}")

        capped = len(results) > max_results
        results = results[:max_results]
        self._populate_results(results, terms, capped, per_term)
        self._refresh_rescope_enabled()

    def _refresh_rescope_enabled(self):
        # Broader / Narrower are notes-mode-only — tag-mode prompts are different.
        ok = bool(self._last_topic and self._last_terms) and self._mode == "notes"
        self.broader_btn.setEnabled(ok)
        self.narrower_btn.setEnabled(ok)

    # ── tag-search mode ───────────────────────────────────────────────────────

    def _search_tags(self, topic: str) -> None:
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.status.setText("Asking AI for tag keywords…")

        model = tool_model_for("search")
        entries = run_claude_json(
            self.search_btn, "Asking AI…",
            prompt=f"Topic: {topic}",
            system=TAG_SEARCH_SYSTEM,
            max_tokens=768,
            model=model,
        )
        if not isinstance(entries, list):
            self.status.setText("")
            return

        cleaned: list[dict] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            kw = (e.get("keyword") or "").strip()
            if not kw:
                continue
            cleaned.append({
                "keyword":  kw,
                "resource": (e.get("resource") or "").strip(),
                "step":     (e.get("step") or "").strip(),
            })
        if not cleaned:
            self.status.setText("AI returned no usable tag keywords.")
            return

        self._last_terms = [e["keyword"] for e in cleaned]
        self.status.setText("Scanning your tags…")
        QApplication.processEvents()

        with loading(self.search_btn, "Scanning tags…"):
            tag_rows = self._resolve_tags(cleaned)
        self._populate_tag_results(tag_rows, cleaned)

    def _favourite_tags(self) -> list[str]:
        """Favourites in the user's priority order (top = preferred)."""
        return [t for t in (self.cfg.get("favourite_tags") or []) if t]

    @staticmethod
    def _segs_contains(hay: list[str], needle: list[str]) -> bool:
        """True if `needle` appears as a contiguous run of segments within
        `hay` (case-insensitive). Lets a favourite match whether it's a full
        path (A::B is a prefix of A::B::C) or a single segment (#Pathoma is one
        of the tag's levels)."""
        if not needle or len(needle) > len(hay):
            return False
        hay_l = [s.lower() for s in hay]
        needle_l = [s.lower() for s in needle]
        for i in range(len(hay_l) - len(needle_l) + 1):
            if hay_l[i:i + len(needle_l)] == needle_l:
                return True
        return False

    def _annotate_fav(self, rows: list[dict]) -> None:
        """Attach favourite-ranking by HIERARCHY, not by shared notes. A tag is
        "under" a favourite only if the favourite's path actually appears in the
        tag's own segments — so a Boards & Beyond tag is never mislabelled as
        Pathoma just because their cards overlap. fav_rank is the index of the
        highest-priority matching favourite (lower = preferred), None if none."""
        favs = [(f, f.split("::")) for f in self._favourite_tags()]
        for r in rows:
            segs = r.get("segs") or r["tag"].split("::")
            rank, label = None, ""
            for i, (fav, fav_segs) in enumerate(favs):
                if self._segs_contains(segs, fav_segs):
                    rank, label = i, fav_segs[-1]
                    break
            r["fav_rank"] = rank
            r["fav_label"] = label

    def _reannotate_favourites(self) -> None:
        self._annotate_fav(self._results)

    def _resolve_tags(self, entries: list[dict]) -> list[dict]:
        """For each keyword, return every collection tag that has the keyword in
        one of its hierarchy segments — including the tags *below* the matched
        level, each as its own row. Note count / nids span the tag's subtree."""
        all_tags = list(mw.col.tags.all())
        seen: dict[str, dict] = {}
        for entry in entries:
            needle = entry["keyword"].lower()
            for t in all_tags:
                segs = t.split("::")
                if t in seen:
                    continue
                # shallowest segment containing the keyword — display starts here
                idx = next((i for i, s in enumerate(segs) if needle in s.lower()), None)
                if idx is None:
                    continue
                query = f'tag:"{t}" OR tag:"{t}::*"'
                try:
                    nids = list(mw.col.find_notes(query))
                except Exception as e:
                    print(f"[ankisstant] tag note query failed for {t!r}: {e}")
                    nids = []
                seen[t] = {
                    "tag":       t,
                    "segs":      segs,
                    "match_idx": idx,
                    "keyword":   entry["keyword"],
                    "query":     query,
                    "nids":      nids,
                    "n_notes":   len(nids),
                    "step":      _step_label(segs),
                }
        rows = list(seen.values())
        self._annotate_fav(rows)
        return rows

    def _populate_tag_results(self, tag_rows: list[dict], entries: list[dict]) -> None:
        # Keep the full resolved set; the visible list is a filtered/sorted view.
        self._results = tag_rows
        self._tag_entries = entries
        self._render_tag_rows()

    @staticmethod
    def _format_tag_head(row: dict) -> str:
        """The tag path from the matched segment downward only — levels *above*
        the search term (version tag, resource, intermediate categories) are
        dropped, since the resource is shown via the favourite chip and the step
        via its own chip. Sub-tags below the match are kept and visible."""
        segs = row.get("segs") or row["tag"].split("::")
        idx = row.get("match_idx", 0)
        body = segs[idx:] or segs
        return "  ›  ".join(body)

    def _render_tag_rows(self) -> None:
        """Rebuild the visible tag list from self._results, applying the current
        favourites filter and sort order. Re-runs no queries."""
        if self._mode != "tags":
            return
        has_favs = bool(self._favourite_tags())
        rows = list(self._results)
        filtered_no_favs = False
        if self.fav_only_cb.isChecked():
            if has_favs:
                rows = [r for r in rows if r.get("fav_rank") is not None]
            else:
                filtered_no_favs = True
        # Group by favourite priority (rank 0 first), non-favourites last; within
        # a group, most-populated tags first.
        BIG = 10**9
        rows.sort(key=lambda r: (
            r["fav_rank"] if r.get("fav_rank") is not None else BIG,
            -r["n_notes"],
        ))

        self.results_list.blockSignals(True)
        self.results_list.clear()
        for row in rows:
            # Order: resource (favourite) chip · step · tag path · note count.
            bits = []
            if row.get("fav_label"):
                bits.append(f"★ {row['fav_label']}")
            if row.get("step"):
                bits.append(row["step"])
            bits.append(self._format_tag_head(row))
            bits.append(f"{row['n_notes']} note{'s' if row['n_notes'] != 1 else ''}")
            label = "   ·   ".join(bits)
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, row["tag"])
            self.results_list.addItem(item)
        self.results_list.blockSignals(False)

        entries = getattr(self, "_tag_entries", []) or []
        breakdown = ", ".join(e["keyword"] for e in entries)
        if filtered_no_favs:
            self.status.setText(
                "No favourite tags set yet — click ★ Favourite tags… to pick some. "
                f"Showing all {len(rows)} matching tag(s)."
            )
        else:
            shown = f"Showing {len(rows)} of {len(self._results)}" \
                if len(rows) != len(self._results) else f"Found {len(rows)}"
            self.status.setText(
                f"{shown} matching tag(s) for: {breakdown}. "
                "Double-click to open a tag in the browser."
            )
        self._update_count()
        self.confirm_btn.setEnabled(self.results_list.count() > 0)

    def _open_favourites_dialog(self) -> None:
        """Pick favourite tags AND order them by priority. Top pane: searchable,
        checkable list of the whole tag tree to add/remove favourites. Bottom
        pane: the chosen favourites in priority order, with Move up/down — that
        order is exactly how tag-search results are grouped (top = preferred)."""
        if not anki_utils.require_col():
            return
        all_tags = sorted(mw.col.tags.all(), key=str.lower)
        if not all_tags:
            tooltip("No tags in your collection yet.")
            return
        # Ordered list of favourites; membership tracked via this list's order.
        order: list[str] = list(self._favourite_tags())

        dlg = QDialog(self)
        dlg.setWindowTitle("Favourite tags")
        dlg.resize(560, 640)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "Tick the resource/branch tags you study from — any hierarchy level "
            "works (e.g. <code>Anking_S1_V12::Boards_&amp;_Beyond</code> or a "
            "parent/child). Results are grouped by the priority order below."
        ))
        search = QLineEdit()
        search.setPlaceholderText("Filter tags…")
        v.addWidget(search)
        lst = QListWidget()
        v.addWidget(lst, 1)

        v.addWidget(QLabel("Priority order — top is preferred:"))
        order_row = QHBoxLayout()
        order_list = QListWidget()
        order_row.addWidget(order_list, 1)
        order_btns = QVBoxLayout()
        up_btn = QPushButton("▲ Up")
        up_btn.setAutoDefault(False)
        down_btn = QPushButton("▼ Down")
        down_btn.setAutoDefault(False)
        remove_btn = QPushButton("✕ Remove")
        remove_btn.setAutoDefault(False)
        order_btns.addWidget(up_btn)
        order_btns.addWidget(down_btn)
        order_btns.addWidget(remove_btn)
        order_btns.addStretch(1)
        order_row.addLayout(order_btns)
        v.addLayout(order_row, 1)

        # Add a tag by name — for top-level tags that don't surface in the list,
        # or any tag you'd rather type than scroll to. Autocompletes existing tags
        # but accepts anything you type.
        add_row = QHBoxLayout()
        add_input = QLineEdit()
        add_input.setPlaceholderText("Type a tag name to add as a favourite…")
        attach_tag_completer(add_input, multi=False)
        add_btn = QPushButton("Add")
        add_btn.setAutoDefault(False)
        add_row.addWidget(add_input, 1)
        add_row.addWidget(add_btn)
        v.addLayout(add_row)

        def refresh_order_list():
            order_list.blockSignals(True)
            keep = order_list.currentRow()
            order_list.clear()
            for t in order:
                order_list.addItem(QListWidgetItem(t))
            if 0 <= keep < order_list.count():
                order_list.setCurrentRow(keep)
            order_list.blockSignals(False)

        def rebuild(needle: str = ""):
            needle = needle.lower().strip()
            lst.blockSignals(True)
            lst.clear()
            for t in all_tags:
                if needle and needle not in t.lower():
                    continue
                it = QListWidgetItem(t)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(
                    Qt.CheckState.Checked if t in order else Qt.CheckState.Unchecked
                )
                lst.addItem(it)
            lst.blockSignals(False)

        def on_item_changed(item):
            t = item.text()
            if item.checkState() == Qt.CheckState.Checked:
                if t not in order:
                    order.append(t)
            elif t in order:
                order.remove(t)
            refresh_order_list()

        def move(delta: int):
            i = order_list.currentRow()
            j = i + delta
            if i < 0 or not (0 <= j < len(order)):
                return
            order[i], order[j] = order[j], order[i]
            order_list.setCurrentRow(j)
            refresh_order_list()

        def remove_selected():
            i = order_list.currentRow()
            if 0 <= i < len(order):
                del order[i]
                refresh_order_list()
                rebuild(search.text())  # re-untick in the main list if shown

        def add_typed():
            t = add_input.text().strip().strip(":")
            if not t:
                return
            if t not in order:
                order.append(t)
            add_input.clear()
            refresh_order_list()
            rebuild(search.text())  # re-tick in the main list if shown

        lst.itemChanged.connect(on_item_changed)
        search.textChanged.connect(rebuild)
        up_btn.clicked.connect(lambda: move(-1))
        down_btn.clicked.connect(lambda: move(1))
        remove_btn.clicked.connect(remove_selected)
        add_btn.clicked.connect(add_typed)
        add_input.returnPressed.connect(add_typed)
        rebuild()
        refresh_order_list()

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)

        if dlg.exec():
            self.cfg["favourite_tags"] = list(order)  # preserve priority order
            try:
                save_tool_config("browse", self.cfg)
            except Exception as e:
                print(f"[ankisstant] saving favourite tags failed: {e}")
            self._reannotate_favourites()
            self._render_tag_rows()

    def _run_searches(self, terms, notetype_filter, max_results):
        seen: set[int] = set()
        results: list[int] = []
        per_term = []
        for term in terms:
            q = self._build_query(term, notetype_filter)
            try:
                nids = list(mw.col.find_notes(q))
            except Exception as e:
                log.warn(f"query failed: {q!r} → {e}")
                per_term.append((term, "ERR"))
                continue
            per_term.append((term, len(nids)))
            for nid in nids:
                if nid in seen:
                    continue
                seen.add(nid)
                results.append(nid)
                if len(results) >= max_results + 1:
                    return results, per_term
        return results, per_term

    def _build_query(self, term: str, notetype_filter: str) -> str:
        if notetype_filter:
            return f'"note:*{notetype_filter}*" {term}'
        return term

    def _populate_results(self, nids, terms, capped, per_term=None):
        self.results_list.blockSignals(True)
        self.results_list.clear()
        front_field = self.cfg.get("front_field", "Text")
        source_tags = self.cfg.get("source_tags", []) or []
        self._results = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            preview = _front_preview(note, front_field)
            tags = list(note.tags)
            source = _source_label(tags, source_tags)
            susp, total = anki_utils.note_suspended_state(note)
            prefix_bits = []
            if susp:
                prefix_bits.append(f"⏸{susp}/{total}" if total > 1 else "⏸")
            if source:
                prefix_bits.append(f"[{source}]")
            prefix = " ".join(prefix_bits)
            label = f"{prefix}  {preview}" if prefix else preview
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, nid)
            self.results_list.addItem(item)
            self._results.append((nid, preview, tags, source, susp, total))
        self.results_list.blockSignals(False)

        if per_term:
            breakdown = ", ".join(f"{t} ({n})" for t, n in per_term)
        else:
            breakdown = ", ".join(terms)
        msg = f"Found {len(nids)} note(s) — {breakdown}"
        if capped:
            msg += f" — capped at {self.cfg.get('max_results', 50)}."
        self.status.setText(msg)
        self._update_count()
        self.confirm_btn.setEnabled(len(nids) > 0)

    # ── selection ─────────────────────────────────────────────────────────────

    def _select_all(self):
        for i in range(self.results_list.count()):
            self.results_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self):
        for i in range(self.results_list.count()):
            self.results_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _update_count(self, *_):
        n = sum(
            1 for i in range(self.results_list.count())
            if self.results_list.item(i).checkState() == Qt.CheckState.Checked
        )
        self.count_label.setText(f"{n} selected")

    def _selected_nids(self):
        nids = []
        for i in range(self.results_list.count()):
            item = self.results_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                nids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return nids

    def _on_item_double_clicked(self, item):
        if self._mode == "tags":
            tag = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if not tag:
                return
            row = next((r for r in self._results if r["tag"] == tag), None)
            query = row["query"] if row and row.get("query") else f'tag:"{tag}"'
        else:
            nid = int(item.data(Qt.ItemDataRole.UserRole))
            query = f"nid:{nid}"
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            if hasattr(browser, "search_for_terms"):
                browser.search_for_terms(query)
            else:
                browser.form.searchEdit.lineEdit().setText(query)
                browser.onSearchActivated()
        except Exception as e:
            print(f"[ankisstant] couldn't open browser for {query!r}: {e}")

    # ── confirm ───────────────────────────────────────────────────────────────

    def _refresh_badges(self):
        if self._mode != "notes":
            return  # tag-mode rows don't carry per-note suspension state
        front_field = self.cfg.get("front_field", "Text")
        source_tags = self.cfg.get("source_tags", []) or []
        for i in range(self.results_list.count()):
            item = self.results_list.item(i)
            nid = int(item.data(Qt.ItemDataRole.UserRole))
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            preview = _front_preview(note, front_field)
            tags = list(note.tags)
            source = _source_label(tags, source_tags)
            susp, total = anki_utils.note_suspended_state(note)
            prefix_bits = []
            if susp:
                prefix_bits.append(f"⏸{susp}/{total}" if total > 1 else "⏸")
            if source:
                prefix_bits.append(f"[{source}]")
            prefix = " ".join(prefix_bits)
            item.setText(f"{prefix}  {preview}" if prefix else preview)

    def _selected_tag_rows(self) -> list[dict]:
        rows: list[dict] = []
        by_tag = {r["tag"]: r for r in self._results} if self._mode == "tags" else {}
        for i in range(self.results_list.count()):
            item = self.results_list.item(i)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            tag = str(item.data(Qt.ItemDataRole.UserRole) or "")
            row = by_tag.get(tag)
            if row is not None:
                rows.append(row)
        return rows

    def _on_confirm(self):
        if not anki_utils.require_col():
            return
        tag = self.tag_input.text().strip()
        if not tag:
            showWarning("Tag is empty. Type a tag before confirming.")
            return

        if self._mode == "tags":
            rows = self._selected_tag_rows()
            if not rows:
                tooltip("No tags selected.")
                return
            nid_set: set[int] = set()
            for row in rows:
                nid_set.update(row.get("nids") or [])
            nids = list(nid_set)
            if not nids:
                tooltip("Selected tags had no notes.")
                return
        else:
            nids = self._selected_nids()
            if not nids:
                tooltip("No notes selected.")
                return

        cids = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            cids.extend(note.card_ids() if hasattr(note, "card_ids") else [c.id for c in note.cards()])

        if hasattr(mw, "checkpoint"):
            mw.checkpoint("AI Browse: tag + unsuspend")
        # Always apply the user-typed tag (that's the point of clicking
        # Confirm). The audit tag and unsuspend are gated by toggles.
        anki_utils.tag_notes(nids, tag)
        apply_audit = bool(self.cb_audit.isChecked())
        audit_tag = (self.cfg.get("audit_tag") or "").strip()
        if apply_audit and audit_tag and audit_tag != tag:
            anki_utils.tag_notes(nids, audit_tag)
        # Month tag for temporality (global toggle in Settings → Global).
        mtag = month_tag()
        if mtag and mtag != tag:
            anki_utils.tag_notes(nids, mtag)

        do_unsuspend = bool(self.cb_unsuspend.isChecked())
        if do_unsuspend:
            pre_cards = [mw.col.get_card(cid) for cid in cids]
            before_suspended = sum(1 for c in pre_cards if c.queue == -1)
            unsuspend_ok = anki_utils.unsuspend_cards(cids) if cids else True
            after_suspended = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
            actually_unsuspended = before_suspended - after_suspended
        else:
            before_suspended = 0
            unsuspend_ok = True
            actually_unsuspended = 0

        # Persist last_used_tag.
        self.cfg["last_used_tag"] = tag
        save_tool_config("browse", self.cfg)

        self._refresh_badges()

        if not unsuspend_ok:
            showWarning(
                "Tagging succeeded but the unsuspend API call didn't return cleanly. "
                "Check the Anki console for details."
            )
        else:
            if do_unsuspend:
                tooltip(
                    f"Tagged {len(nids)} note(s) ({len(cids)} card(s)). "
                    f"Unsuspended {actually_unsuspended} of {before_suspended} previously-suspended."
                )
            else:
                tooltip(
                    f"Tagged {len(nids)} note(s) ({len(cids)} card(s)). "
                    "Unsuspend skipped (toggle off)."
                )

        # If preloaded from a KG, append the captured content to each
        # tagged note. MQ-type → full captured missed-question (stem HTML +
        # images) into the Missed Questions field. Non-MQ → just images into
        # Extra. The field name is whatever the user's notetype uses.
        img_html = "<br>".join(
            f'<img src="{_html.escape(f, quote=True)}">'
            for f in self._linked_kg_images if f
        )
        if self._linked_kg_id:
            try:
                if self._linked_kg_type == "mq":
                    qb_cfg = tool_config("qbank")
                    target_field = qb_cfg.get("missed_q_field") or "Missed Questions"
                    parts = []
                    # Lead with the specific knowledge gap + its explanation,
                    # then the captured stem/screenshot — matching Create.
                    if self._linked_kg_concept:
                        parts.append(
                            f"<b>Knowledge gap:</b> {_html.escape(self._linked_kg_concept)}"
                        )
                    if self._linked_kg_explanation:
                        parts.append(
                            "<i>"
                            + _html.escape(self._linked_kg_explanation).replace("\n", "<br>")
                            + "</i>"
                        )
                    if self._linked_kg_stem_html:
                        parts.append(self._linked_kg_stem_html)
                    if img_html:
                        parts.append(img_html)
                    content_html = "<br>".join(parts)
                else:
                    target_field = "Extra"
                    content_html = img_html
                if content_html:
                    appended = 0
                    for nid in nids:
                        if anki_utils.append_to_field(nid, target_field, content_html):
                            appended += 1
                    if appended:
                        tooltip(
                            f"Appended KG content to '{target_field}' on {appended} note(s).",
                            period=4000,
                        )
            except Exception as e:
                print(f"[ankisstant] append KG content from Browse failed: {e}")

        # For MQ-type KGs, re-rate every selected non-new card as Again so
        # they resurface today — this is the "I got this wrong on Qbank,
        # bring it back" workflow. Uses the vendored AJT Card Management
        # grade_cards() so FSRS sees a proper lapse (not a queue hack).
        # Skip cards that are still suspended/buried (queue < 0) and cards
        # that are brand-new (type == 0) — answerCard would either choke or
        # do nothing useful on those; freshly-unsuspended new cards are
        # already going to surface as new.
        if (
            self._linked_kg_type == "mq"
            and cids
            and bool(self.cb_grade.isChecked())
            and getattr(mw.col.sched, "version", 0) >= 3
        ):
            try:
                from .qbank.grade_cards import grade_cards as _grade_cards
                to_grade = []
                for cid in cids:
                    c = mw.col.get_card(cid)
                    if c.queue >= 0 and c.type >= 1:
                        to_grade.append(c)
                if to_grade:
                    _grade_cards(mw.col, to_grade, 1)
                    tooltip(
                        f"Marked {len(to_grade)} card(s) Again — they'll resurface today.",
                        period=4000,
                    )
            except Exception as e:
                log.warn(f"grade Again failed for MQ KG: {e}")

        # If preloaded from a KG, mark it done.
        if self._linked_kg_id:
            done_id = self._linked_kg_id
            try:
                from .kg import store as kg_store
                kg_store.update(done_id, status="done")
                from . import knowledge_gaps
                knowledge_gaps._refresh_open_panel()
            except Exception as e:
                print(f"[ankisstant] mark KG done from Browse failed: {e}")
            self._linked_kg_id = None
            self._linked_kg_images = []
            self._linked_kg_stem_html = ""
            self._linked_kg_type = ""
            # Advance the Browse queue (if this KG came from one).
            try:
                self._advance_queue_after_done(done_id)
            except Exception as e:
                print(f"[ankisstant] browse queue advance failed: {e}")


# ── Native-browser AI search ─────────────────────────────────────────────────
#
# A small "✨ AI Search" checkbox sits right next to Anki's own search bar in
# the native card browser. When it's on, pressing Enter doesn't run the typed
# text as a literal Anki query — instead it's sent to AI as a topic (the exact
# same SEARCH_TERMS_SYSTEM prompt and run_claude_json plumbing as AI Browse),
# and the returned terms are combined into one query and searched instead.
# Toggle it off to use the search bar normally. This keeps the workflow fully
# inside Anki's familiar browser — no separate panel or popup to learn.

_AI_CHECKBOX_TOOLTIP = (
    "When on, typing a topic and pressing Enter sends it to AI for Anki search "
    "terms (the same process as AI Browse) and searches for those instead of "
    "the literal text. Turn off to search normally."
)


def _on_native_ai_toggle(checked: bool) -> None:
    try:
        cfg = tool_config("browse")
        cfg["native_ai_search_enabled"] = bool(checked)
        save_tool_config("browse", cfg)
    except Exception as e:
        print(f"[ankisstant] persist native AI search toggle failed: {e}")


def _scoped_native_ai_query(cfg: dict, terms: list[str]) -> str:
    """AND the AI-generated OR-group with the user's deck/tag/suspended scope
    filters from Settings → AI Browse → "Native browser AI Search — scope"."""
    clauses = ["(" + " OR ".join(f"({t})" for t in terms) + ")"]
    scope = cfg.get("native_ai_search_scope") or {}
    decks = [d.strip() for d in (scope.get("decks") or []) if d.strip()]
    if decks:
        clauses.append("(" + " OR ".join(f'deck:"{d}"' for d in decks) + ")")
    tags = [t.strip() for t in (scope.get("tags") or []) if t.strip()]
    if tags:
        clauses.append("(" + " OR ".join(f'tag:"{t}"' for t in tags) + ")")
    suspended = (scope.get("suspended") or "any").strip().lower()
    if suspended == "suspended":
        clauses.append("is:suspended")
    elif suspended == "unsuspended":
        clauses.append("-is:suspended")
    return " ".join(clauses)


def _apply_native_ai_results(browser, cfg: dict, reply) -> None:
    if not isinstance(reply, list) or not all(isinstance(t, str) for t in reply):
        return  # ask_claude_json already reported the failure
    terms = [t.strip() for t in reply if t.strip()]
    if not terms:
        tooltip("AI returned no usable search terms.")
        return

    query = _scoped_native_ai_query(cfg, terms)
    try:
        # search_for() (lowercase) sets _lastSearchTxt, updates the line edit
        # text and runs the search directly — unlike search_for_terms(), it
        # does NOT call back into onSearchActivated, so it can't re-enter our
        # wrapper above and mistake the generated query for a fresh topic.
        browser.search_for(query)
    except Exception as e:
        print(f"[ankisstant] AI search couldn't run query: {e}")
        return
    tooltip("AI search for: " + ", ".join(terms), period=4000)


def _set_native_ai_busy(checkbox, line_edit, busy: bool) -> None:
    """Toggle the inline '⏳ Thinking…' state on the checkbox itself — a
    lightweight in-place indicator next to the search bar instead of the modal
    progress dialog (which yanks focus to the main Anki window)."""
    try:
        checkbox.setText("✨ AI Search ⏳ Thinking…" if busy else "✨ AI Search")
        checkbox.setEnabled(not busy)
        line_edit.setEnabled(not busy)
    except RuntimeError:
        pass  # browser closed mid-call


def _run_native_ai_search(browser, checkbox, line_edit, topic: str) -> None:
    if not provider_configured():
        tooltip("Set up an AI provider in Ankisstant Settings first.", period=4000)
        return

    cfg = tool_config("browse")
    model = tool_model_for("native_search")
    kwargs = dict(prompt=f"Topic: {topic}", system=SEARCH_TERMS_SYSTEM,
                  max_tokens=512, model=model)

    if is_manual_provider():
        # Interactive: the manual provider shows its own copy/paste dialog and
        # must run on the main thread — no inline indicator needed here, the
        # dialog itself is the "loading" state.
        reply = core_api.ask_claude_json(**kwargs)
        _apply_native_ai_results(browser, cfg, reply)
        return

    # Run the call on a background thread with a small inline indicator —
    # *not* the modal QProgressDialog from run_claude_json/_run_claude, which
    # is parented to the main window and yanks focus away from the browser.
    _set_native_ai_busy(checkbox, line_edit, True)
    cancel_ev = threading.Event()

    def _bg():
        core_api.set_cancel_event(cancel_ev)
        try:
            return core_api.ask_claude_json(show_errors=False, **kwargs)
        finally:
            core_api.set_cancel_event(None)

    def _done(fut):
        _set_native_ai_busy(checkbox, line_edit, False)
        try:
            reply = fut.result()
        except Exception as e:
            log.error(f"native AI search failed: {e}")
            tooltip("Claude call failed — see console for details.", period=4000)
            return
        if reply is None:
            tooltip("Claude call failed — see console for details.", period=4000)
            return
        _apply_native_ai_results(browser, cfg, reply)

    mw.taskman.run_in_background(_bg, _done)


def install_native_ai_search(browser) -> None:
    """Add the '✨ AI Search' checkbox next to the browser's search bar and
    wrap onSearchActivated so that, while it's checked, pressing Enter sends
    the typed topic to AI and searches the generated terms instead of the
    literal text. Called once per browser window from __init__.py via
    gui_hooks.browser_menus_did_init (which — despite the name — fires once
    at window construction, the usual place addons add one-off browser UI,
    and crucially *before* Browser.setupSearch() wires up the search bar)."""
    try:
        search_edit = browser.form.searchEdit
        line_edit = search_edit.lineEdit()

        cfg = tool_config("browse")
        cb = QCheckBox("✨ AI Search")
        cb.setToolTip(_AI_CHECKBOX_TOOLTIP)
        cb.setChecked(bool(cfg.get("native_ai_search_enabled", False)))
        cb.toggled.connect(_on_native_ai_toggle)

        # The search bar and the Cards/Notes switch both live in
        # browser.form.gridLayout, row 0 — switch at column 0, search box at
        # column 1 (see aqt.browser.browser.Browser.setup_table /
        # _aqt.forms.browser_qt6). Column 2 is free; placing the checkbox
        # there puts it inline with both, not appended after the whole page.
        grid = getattr(browser.form, "gridLayout", None)
        if grid is not None:
            grid.addWidget(cb, 0, 2)
        else:
            search_edit.parentWidget().layout().addWidget(cb)

        # Wrap the bound onSearchActivated as an instance attribute. Because
        # this hook fires before setupSearch() runs, Anki's own
        # `qconnect(line_edit.returnPressed, self.onSearchActivated)` resolves
        # `self.onSearchActivated` to *our* wrapper (instance attributes shadow
        # class methods) — so a real Enter-press in the search bar routes
        # through us. Other paths that trigger a search programmatically
        # (sidebar clicks via search_for, AI Browse's search_for_terms, etc.)
        # either skip onSearchActivated entirely or invoke it as a plain method
        # call rather than through the line edit's signal — neither carries its
        # signal context, so checking sender() lets us tell a genuine keypress
        # apart from those and only intercept the former.
        original_on_search_activated = browser.onSearchActivated

        def _wrapped_on_search_activated():
            if cb.isChecked() and browser.sender() is line_edit:
                topic = browser.current_search().strip()
                if topic:
                    _run_native_ai_search(browser, cb, line_edit, topic)
                    return
            original_on_search_activated()

        browser.onSearchActivated = _wrapped_on_search_activated
        browser._ankisstant_ai_search_checkbox = cb
    except Exception as e:
        log.error(f"native AI search install failed: {e}")


# ── Tool contract ────────────────────────────────────────────────────────────

_panel: BrowsePanel | None = None


def init(main_window) -> None:
    """No-op for now — tool has no startup work."""
    return None


_scroll = None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        from aqt.qt import QScrollArea
        _panel = BrowsePanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        # Re-read config on each open so settings changes propagate.
        _panel.cfg = tool_config("browse")
    _panel.refresh_setup_banner()
    return _scroll


def preload_for_kg(kg: dict) -> None:
    """Called by the Knowledge Gaps detail pane after switching to Browse."""
    panel = _panel
    if panel is None:
        # Lazily build so a fresh session can still preload.
        get_panel()
        panel = _panel
    if panel is not None:
        panel.preload_for_kg(kg)
