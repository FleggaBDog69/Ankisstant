# Browse with Claude — find Anki cards for a topic via Claude-generated search
# terms, then tag and unsuspend in one step.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QApplication, QButtonGroup, QCheckBox, QDialogButtonBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QRadioButton, Qt, QVBoxLayout, QWidget,
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

TAG_SEARCH_SYSTEM = (
    "You generate Anki tag-name keywords for a medical student. Given a topic, "
    "return a JSON array of 3 to 6 short, specific keyword strings that are "
    "likely to appear inside that student's Anki tag tree for that topic. "
    "For each keyword, also suggest a favourite study resource and the USMLE "
    "step level it's most relevant to.\n\n"
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
    "Use 'AMC' for Australian-context entries when clearly post-graduation.\n"
    "- 3–6 items, quality over quantity.\n\n"
    "Return ONLY the JSON array. No prose."
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
        # "notes" — Claude → search terms → notes (the original behaviour).
        # "tags" — Claude → tag keywords → matching tags (study-planning mode).
        self._mode: str = "notes"
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

    def _on_mode_changed(self, _checked: bool) -> None:
        self._mode = "tags" if self._tags_radio.isChecked() else "notes"
        # Different result kinds → clear what's there so the UI is consistent.
        self.results_list.clear()
        self._results = []
        self._last_terms = []
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.confirm_btn.setEnabled(False)
        if self._mode == "tags":
            self.status.setText("Tags mode — find tag groups for study planning.")
        else:
            self.status.setText("")

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
        # Broader / Narrower are notes-mode-only — tag-mode prompts are different.
        ok = bool(self._last_topic and self._last_terms) and self._mode == "notes"
        self.broader_btn.setEnabled(ok)
        self.narrower_btn.setEnabled(ok)

    # ── tag-search mode ───────────────────────────────────────────────────────

    def _search_tags(self, topic: str) -> None:
        self.broader_btn.setEnabled(False)
        self.narrower_btn.setEnabled(False)
        self.status.setText("Asking Claude for tag keywords…")

        model = self.cfg.get("model") or None
        with loading(self.search_btn, "Asking Claude…"):
            entries = core_api.ask_claude_json(
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
            self.status.setText("Claude returned no usable tag keywords.")
            return

        self._last_terms = [e["keyword"] for e in cleaned]
        self.status.setText("Scanning your tags…")
        QApplication.processEvents()

        with loading(self.search_btn, "Scanning tags…"):
            tag_rows = self._resolve_tags(cleaned)
        self._populate_tag_results(tag_rows, cleaned)

    def _resolve_tags(self, entries: list[dict]) -> list[dict]:
        """For each keyword, find matching tag names in the collection and
        produce one row per unique tag (preferring the first keyword that
        matched, so display order is stable)."""
        all_tags = list(mw.col.tags.all())
        seen: dict[str, dict] = {}
        for entry in entries:
            needle = entry["keyword"].lower()
            for t in all_tags:
                if needle in t.lower() and t not in seen:
                    nids = list(mw.col.find_notes(f'tag:"{t}"'))
                    seen[t] = {
                        "tag":      t,
                        "keyword":  entry["keyword"],
                        "resource": entry["resource"],
                        "step":     entry["step"],
                        "nids":     nids,
                        "n_notes":  len(nids),
                    }
        # Sort by note-count desc — most populated tags first.
        return sorted(seen.values(), key=lambda r: -r["n_notes"])

    def _populate_tag_results(self, tag_rows: list[dict], entries: list[dict]) -> None:
        self.results_list.blockSignals(True)
        self.results_list.clear()
        self._results = tag_rows
        for row in tag_rows:
            meta_bits = [f"{row['n_notes']} note{'s' if row['n_notes'] != 1 else ''}"]
            if row["resource"]:
                meta_bits.append(row["resource"])
            if row["step"]:
                meta_bits.append(row["step"])
            label = f"{row['tag']}   ·   {'  ·  '.join(meta_bits)}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, row["tag"])
            self.results_list.addItem(item)
        self.results_list.blockSignals(False)

        breakdown = ", ".join(e["keyword"] for e in entries)
        self.status.setText(
            f"Found {len(tag_rows)} matching tag(s) for: {breakdown}. "
            "Double-click to open a tag in the browser."
        )
        self._update_count()
        self.confirm_btn.setEnabled(len(tag_rows) > 0)

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
            query = f'tag:"{tag}"'
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
