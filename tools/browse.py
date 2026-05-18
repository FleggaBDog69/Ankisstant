# Browse with Claude — find Anki cards for a topic via Claude-generated search
# terms, then tag and unsuspend in one step.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

import html as _html

from aqt import mw
from aqt.qt import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, Qt,
    QVBoxLayout, QWidget,
)
from aqt.utils import showWarning, tooltip

from ..core import anki_utils, api as core_api, log
from ..core.config import tool_config, save_tool_config
from ..core.qt_utils import (
    attach_tag_completer, loading, make_help_button, make_setup_banner,
    provider_configured,
)


NAME = "Browse with Claude"


# ── prompts ───────────────────────────────────────────────────────────────────

SEARCH_TERMS_SYSTEM = (
    "You generate Anki search terms for a medical student's deck. Given a topic, "
    "return a JSON array of 3 to 6 HIGHLY SPECIFIC search strings that surface "
    "cards genuinely about that topic — not cards that just mention it in passing.\n\n"
    "RULES:\n"
    "- Prefer multi-word phrases and eponyms over single common words.\n"
    "- Avoid generic 1–3 letter abbreviations on their own (e.g. 'MS', 'DM', 'IV') — "
    "they collide with too many unrelated cards. Disambiguate them: 'multiple sclerosis', "
    "'McDonald criteria', not 'MS'.\n"
    "- Include classic exam-relevant entities: pathognomonic signs, key drugs, "
    "diagnostic criteria, eponyms — but ONLY if tightly bound to the topic.\n"
    "- For narrow topics, return 3–4 terms. For broad topics, max 6. Quality over quantity.\n\n"
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

GAP_SYSTEM = (
    "You audit Anki tag coverage for a medical topic. Given the topic and a list "
    "of tags present across the matched cards, identify what important sub-areas "
    "of the topic appear to be missing or under-represented. "
    "Return a JSON array of short strings (3–8 items), each naming one gap. "
    "Keep each item under 12 words. No prose, just the JSON array."
)


# ── helpers ──────────────────────────────────────────────────────────────────

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
        # When preloaded from a KG, stamp the id here so a successful
        # tag+unsuspend can mark that KG done.
        self._linked_kg_id: str | None = None
        self._build()

    def preload_for_kg(self, kg: dict) -> None:
        """Preload the panel from a KG and remember its id so a successful
        tag/unsuspend can mark the KG done."""
        title = (kg.get("title") or "").strip()
        if title:
            self.topic.setText(title)
        self._linked_kg_id = kg.get("id") or None
        # If the KG has a system-tag like System::Subsystem::Topic, surface it.
        tags = kg.get("tags") or []
        if tags and not self.tag_input.text().strip():
            self.tag_input.setText(tags[0])
        self._last_topic = title
        self._last_terms = []

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>Browse with Claude</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "Browse with Claude — help",
            "<h3>What it does</h3>"
            "<p>You type a topic. Claude returns a short list of Anki search "
            "terms. The addon runs each search against your collection and shows "
            "the union, ranked by Claude's term order.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Type a topic (broad like 'MS' or specific like 'McDonald criteria').</li>"
            "<li>Click <b>Search</b>. Results appear with [deck] tags.</li>"
            "<li>Use <b>Broader</b> / <b>Narrower</b> to rescope if needed.</li>"
            "<li>Tick the cards you want, type a tag, click <b>Tag &amp; Unsuspend</b>.</li>"
            "</ol>"
            "<h3>Settings</h3>"
            "<p>Notetype filter, max results, source-tag filter and audit tag are "
            "in <b>Ankisstant Settings → Browse with Claude</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        root.addWidget(QLabel("Topic (broad or narrow — Claude figures out the search terms):"))
        topic_row = QHBoxLayout()
        self.topic = QLineEdit()
        self.topic.setMinimumWidth(500)
        self.topic.setPlaceholderText("e.g. MS, or 'progression of disease in MS'")
        self.topic.returnPressed.connect(self._on_search)
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

        self.results_list = QListWidget()
        self.results_list.setAlternatingRowColors(True)
        self.results_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        root.addWidget(self.results_list, 1)

        sel_row = QHBoxLayout()
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(self._select_all)
        sel_none = QPushButton("Select none")
        sel_none.clicked.connect(self._select_none)
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
        tag_row.addWidget(self.tag_input, 1)
        root.addLayout(tag_row)
        audit_hint = QLabel(
            f"<small>Audit tag <code>{self.cfg.get('audit_tag', '')}</code> "
            "is also applied to every unsuspended note so you can find them later.</small>"
        )
        audit_hint.setTextFormat(Qt.TextFormat.RichText)
        audit_hint.setStyleSheet("color: gray;")
        root.addWidget(audit_hint)

        confirm_row = QHBoxLayout()
        confirm_row.addStretch(1)
        self.confirm_btn = QPushButton("Tag + Unsuspend")
        self.confirm_btn.setAutoDefault(False)
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(self._on_confirm)
        confirm_row.addWidget(self.confirm_btn)
        root.addLayout(confirm_row)

        self.results_list.itemChanged.connect(self._update_count)

    def refresh_setup_banner(self) -> None:
        try:
            self._setup_banner.setVisible(not provider_configured())
        except Exception:
            pass

    # ── search ────────────────────────────────────────────────────────────────

    def _on_search(self):
        if not anki_utils.require_col():
            return
        topic = self.topic.text().strip()
        if not topic:
            return
        self._last_topic = topic
        self._search_with_system(
            SEARCH_TERMS_SYSTEM, f"Topic: {topic}",
            status_label="Asking Claude for search terms…",
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
            status_label=f"Asking Claude for {direction} terms…",
        )

    def _search_with_system(self, system_prompt, user_prompt, status_label):
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.status.setText(status_label)

        model = self.cfg.get("model") or None
        with loading(self.search_btn, "Asking Claude…"):
            terms = core_api.ask_claude_json(
                prompt=user_prompt,
                system=system_prompt,
                max_tokens=512,
                model=model,
            )
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
        ok = bool(self._last_topic and self._last_terms)
        self.broader_btn.setEnabled(ok)
        self.narrower_btn.setEnabled(ok)

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
        nid = int(item.data(Qt.ItemDataRole.UserRole))
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            query = f"nid:{nid}"
            if hasattr(browser, "search_for_terms"):
                browser.search_for_terms(query)
            else:
                browser.form.searchEdit.lineEdit().setText(query)
                browser.onSearchActivated()
        except Exception as e:
            print(f"[ankisstant] couldn't open browser for nid {nid}: {e}")

    # ── confirm ───────────────────────────────────────────────────────────────

    def _refresh_badges(self):
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

    def _on_confirm(self):
        if not anki_utils.require_col():
            return
        nids = self._selected_nids()
        if not nids:
            tooltip("No notes selected.")
            return
        tag = self.tag_input.text().strip()
        if not tag:
            showWarning("Tag is empty. Type a tag before confirming.")
            return

        cids = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            cids.extend(note.card_ids() if hasattr(note, "card_ids") else [c.id for c in note.cards()])

        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Browse with Claude: tag + unsuspend")
        anki_utils.tag_notes(nids, tag)
        audit_tag = (self.cfg.get("audit_tag") or "").strip()
        if audit_tag and audit_tag != tag:
            anki_utils.tag_notes(nids, audit_tag)

        before_suspended = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
        unsuspend_ok = anki_utils.unsuspend_cards(cids) if cids else True
        after_suspended = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
        actually_unsuspended = before_suspended - after_suspended

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
            tooltip(
                f"Tagged {len(nids)} note(s) ({len(cids)} card(s)). "
                f"Unsuspended {actually_unsuspended} of {before_suspended} previously-suspended."
            )

        # If preloaded from a KG, mark it done.
        if self._linked_kg_id:
            try:
                from .kg import store as kg_store
                kg_store.update(self._linked_kg_id, status="done")
                from . import knowledge_gaps
                knowledge_gaps._refresh_open_panel()
            except Exception as e:
                print(f"[ankisstant] mark KG done from Browse failed: {e}")
            self._linked_kg_id = None

        if self.cfg.get("enable_gap_report", False):
            self._show_gap_report()

    def _show_gap_report(self):
        topic = self.topic.text().strip()
        all_tags = set()
        for _nid, _p, tags, _src, _s, _t in self._results:
            for t in tags:
                all_tags.add(t)

        self.status.setText("Asking Claude what's missing…")
        QApplication.processEvents()

        prompt = (
            f"Topic: {topic}\n\n"
            f"Tags present across the {len(self._results)} matched notes:\n"
            + "\n".join(f"- {t}" for t in sorted(all_tags))
        )
        gaps = core_api.ask_claude_json(
            prompt=prompt, system=GAP_SYSTEM, max_tokens=400,
            model=self.cfg.get("model") or None,
        )
        if not isinstance(gaps, list):
            self.status.setText("")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Possible gaps for: {topic}")
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"<b>Possible gaps for &quot;{_html.escape(topic)}&quot;:</b>"))
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText("\n".join(f"• {g}" for g in gaps))
        v.addWidget(body, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        v.addWidget(close)
        self.status.setText("Done.")
        dlg.exec()


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
