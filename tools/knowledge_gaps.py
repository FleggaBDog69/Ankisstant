# Knowledge Gaps — the unified queue page.
#
# Replaces the old "Analyse Knowledge Gaps" sidebar entry. Holds every KG
# coming from any source (manual, analyse, qbank, browse) in a single
# persistent list (see tools/kg/store.py).
#
# A KG's lifecycle from here:
#   - "Send to Browse with Claude" → preloads the Browse panel with the KG
#     title; on a successful tag/unsuspend pass the KG is marked done.
#   - "+ Create card from this KG" → pushes the KG onto MainWindow.gap_queue
#     (richer dict shape — carries stem_html + notes) and switches to
#     Create with Claude. When Create adds the card the KG is marked done.

from __future__ import annotations

import html as _html
from typing import Callable

from aqt import mw
from aqt.qt import (
    QComboBox, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, Qt,
    QTextEdit, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, api as core_api
from ..core.config import tool_config
from ..core.qt_utils import (
    attach_tag_completer, loading, make_help_button, make_setup_banner,
    provider_configured,
)
from .kg import store as kg_store


NAME = "Knowledge Gaps"


# ── helpers ──────────────────────────────────────────────────────────────────

SOURCE_LABELS = {
    "manual":  "Manual",
    "analyse": "Analyse",
    "qbank":   "QBank",
    "browse":  "Browse",
}

STATUS_LABELS = {
    "open":      "Open",
    "done":      "Done",
    "dismissed": "Dismissed",
}


def _load_types() -> list[dict]:
    """Return the configured KG types. Always includes at least the three
    factory defaults if a user has wiped the list."""
    cfg = tool_config("knowledge_gaps")
    types = cfg.get("types") or []
    cleaned: list[dict] = []
    for t in types:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key", "") or "").strip().lower()
        name = str(t.get("name", "") or "").strip()
        if not key or not name:
            continue
        cleaned.append({
            "key":         key,
            "name":        name,
            "color":       str(t.get("color", "") or "#6b7280"),
            "description": str(t.get("description", "") or ""),
        })
    if not cleaned:
        cleaned = [
            {"key": "mq", "name": "MQ", "color": "#b45309",
             "description": "Missed question."},
            {"key": "kg", "name": "KG", "color": "#6b7280",
             "description": "Knowledge gap."},
            {"key": "lo", "name": "LO", "color": "#9333ea",
             "description": "Learning objective."},
        ]
    return cleaned


def _type_meta(key: str) -> dict:
    """Lookup a type by key, falling back to a muted '(removed)' shim so
    KGs that reference a deleted type still render."""
    key = (key or "").strip().lower()
    for t in _load_types():
        if t["key"] == key:
            return t
    return {"key": key or "?", "name": f"({key or '?'})",
            "color": "#9ca3af", "description": "Type removed from settings."}


def _short_date(iso: str) -> str:
    return (iso or "")[:10]


def _format_levels(kg: dict) -> str:
    levels = [kg_store.field(kg, "system"),
              kg_store.field(kg, "subsystem"),
              kg_store.field(kg, "topic")]
    return " :: ".join(l for l in levels if l)


# ── Add KG modal ─────────────────────────────────────────────────────────────

class AddKGDialog(QDialog):
    """Quick modal for the ＋ Add KG button (home screen + sidebar)."""

    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("＋ Add Knowledge Gap")
        self.setMinimumWidth(520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel("<b>Title</b>"))
        self._title = QLineEdit()
        self._title.setPlaceholderText("e.g. mechanism of digoxin toxicity in hypokalaemia")
        layout.addWidget(self._title)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("<b>Type</b>"))
        self._type = QComboBox()
        for t in _load_types():
            self._type.addItem(t["name"], t["key"])
            self._type.setItemData(self._type.count() - 1,
                                   t.get("description", ""), Qt.ItemDataRole.ToolTipRole)
        # Default to the configured default_type_on_add (falls back to first).
        cfg = tool_config("knowledge_gaps")
        default_type = (cfg.get("default_type_on_add") or "kg").lower()
        idx = self._type.findData(default_type)
        if idx >= 0:
            self._type.setCurrentIndex(idx)
        type_row.addWidget(self._type, 1)
        layout.addLayout(type_row)

        layout.addWidget(QLabel("<b>Tags</b> <span style='color:gray;font-size:10px'>"
                                "(space-separated; optional)</span>"))
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("School::Year3::Cardio::Digoxin")
        attach_tag_completer(self._tags, multi=True)
        layout.addWidget(self._tags)

        layout.addWidget(QLabel("<b>Notes</b> <span style='color:gray;font-size:10px'>"
                                "(optional — what specifically don't you know?)</span>"))
        self._notes = QPlainTextEdit()
        self._notes.setMinimumHeight(80)
        layout.addWidget(self._notes)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._save)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

        self._title.setFocus()

    def _save(self) -> None:
        title = self._title.text().strip()
        if not title:
            tooltip("Title is required.")
            return
        tags = [t for t in self._tags.text().split() if t.strip()]
        notes = self._notes.toPlainText().strip()
        kg_type = self._type.currentData() or "kg"
        kg_store.add(
            title=title,
            source="manual",
            type=kg_type,
            status="open",
            tags=tags,
            notes=notes,
        )
        try:
            mw.deckBrowser.refresh()
        except Exception:
            pass
        # Tell any open KG panel to reload.
        try:
            _refresh_open_panel()
        except Exception:
            pass
        self.accept()


def open_add_kg_dialog() -> None:
    dlg = AddKGDialog()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


# ── KG list item ─────────────────────────────────────────────────────────────

class _KGListItem(QListWidgetItem):
    def __init__(self, kg: dict):
        super().__init__()
        self.kg_id = kg.get("id", "")
        self.refresh(kg)

    def refresh(self, kg: dict) -> None:
        from aqt.qt import QBrush, QColor, QFont
        source_label = SOURCE_LABELS.get(kg.get("source", "manual"), "?")
        type_meta = _type_meta(kg.get("type", "kg"))
        status = kg.get("status", "open")
        status_label = STATUS_LABELS.get(status, "Open")
        title = kg.get("title", "(untitled)") or "(untitled)"
        # Prefix the title with a compact type tag for at-a-glance scanning.
        text = f"[{type_meta['name']}]  {title}"
        if len(text) > 80:
            text = text[:80].rstrip() + "…"
        self.setText(text)

        # Grey done / dismissed items so they stay visible but recede.
        if status in ("done", "dismissed"):
            brush = QBrush(QColor(150, 150, 150))
            self.setForeground(brush)
            f = QFont(self.font())
            f.setItalic(True)
            self.setFont(f)
        else:
            # Reset to default if a previously-done item flipped back to open.
            self.setData(Qt.ItemDataRole.ForegroundRole, None)
            self.setFont(QFont())

        tt_lines = [
            f"Type: {type_meta['name']}" + (f" — {type_meta['description']}"
                                            if type_meta.get('description') else ""),
            f"Source: {source_label}",
            f"Status: {status_label}",
        ]
        levels = _format_levels(kg)
        if levels:
            tt_lines.append(f"Path: {levels}")
        tt_lines.append(f"Added: {_short_date(kg.get('created_at', ''))}")
        self.setToolTip("\n".join(tt_lines))


# ── LO analyser (inline section, only shown on type=lo KGs) ─────────────────

class _LOAnalyserSection(QGroupBox):
    """Inline Analyse-LO panel — appears on the KG detail pane for LO-type
    KGs. Reads the LO text + tag from the KG's own schema fields, runs the
    gap analysis, and appends accepted gaps into the KG's notes field."""

    def __init__(self, detail_pane):
        super().__init__("🔬 Analyse this LO")
        self._pane = detail_pane
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        hint = QLabel(
            "Pulls cards under this LO's tag and flags concepts the cards "
            "don't cover. Accepted gaps are appended to this LO's <b>Notes</b>."
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        btn_row = QHBoxLayout()
        self._count_btn = QPushButton("Count cards")
        self._count_btn.setAutoDefault(False)
        self._count_btn.clicked.connect(self._on_count)
        btn_row.addWidget(self._count_btn)
        self._analyse_btn = QPushButton("🔍 Analyse Gaps")
        self._analyse_btn.setAutoDefault(False)
        self._analyse_btn.clicked.connect(self._on_analyse)
        btn_row.addWidget(self._analyse_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self._gaps_box = QGroupBox("Suggested gaps")
        gv = QVBoxLayout(self._gaps_box)
        gv.setContentsMargins(8, 6, 8, 6)
        gv.setSpacing(4)
        self._gaps_list = QListWidget()
        self._gaps_list.setAlternatingRowColors(True)
        self._gaps_list.setMaximumHeight(180)
        gv.addWidget(self._gaps_list, 1)
        ctrl_row = QHBoxLayout()
        sa = QPushButton("Select all")
        sa.setAutoDefault(False)
        sa.clicked.connect(self._select_all)
        sn = QPushButton("Select none")
        sn.setAutoDefault(False)
        sn.clicked.connect(self._select_none)
        ctrl_row.addWidget(sa)
        ctrl_row.addWidget(sn)
        ctrl_row.addStretch(1)
        self._append_btn = QPushButton("Append checked → Notes")
        self._append_btn.setAutoDefault(False)
        self._append_btn.clicked.connect(self._append_to_notes)
        ctrl_row.addWidget(self._append_btn)
        gv.addLayout(ctrl_row)
        self._gaps_box.setVisible(False)
        v.addWidget(self._gaps_box)

    def reset(self) -> None:
        """Wipe transient state — called when the selected KG changes."""
        self._gaps_list.clear()
        self._gaps_box.setVisible(False)
        self._status.setText("")

    # ── inputs ───────────────────────────────────────────────────────────────

    def _current_lo_and_tag(self) -> tuple[str, str]:
        widgets = self._pane._schema_widgets
        lo = ""
        tag = ""
        if "lo" in widgets:
            try:
                lo = widgets["lo"]["getter"]()
            except Exception:
                pass
        if "lo_tag" in widgets:
            try:
                tag = widgets["lo_tag"]["getter"]()
            except Exception:
                pass
        # Fall back to KG title if there's no `lo` field on the schema.
        if not lo and self._pane._current:
            lo = self._pane._current.get("title", "")
        return lo.strip(), tag.strip()

    # ── helpers (re-use gap_analyser's prompt + tag query) ───────────────────

    def _config(self) -> dict:
        return tool_config("gap_analyser")

    # ── actions ──────────────────────────────────────────────────────────────

    def _on_count(self) -> None:
        if not anki_utils.require_col():
            return
        _, tag = self._current_lo_and_tag()
        if not tag:
            tooltip("This LO has no tag set yet — fill the LO tag field above.")
            return
        from .gap_analyser import _build_tag_query
        cfg = self._config()
        notetype_filter = (cfg.get("notetype_filter") or "").strip()
        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        suffix = f" (notetype filter: {notetype_filter})" if notetype_filter else ""
        self._status.setText(f"{len(nids)} note(s) under '{tag}'{suffix}")

    def _on_analyse(self) -> None:
        if not anki_utils.require_col():
            return
        lo, tag = self._current_lo_and_tag()
        if not lo or not tag:
            tooltip("Set both the LO text and the LO tag on this KG first.")
            return
        from .gap_analyser import GAP_SYSTEM_TMPL, _build_tag_query, _front_preview
        cfg = self._config()
        notetype_filter = (cfg.get("notetype_filter") or "").strip()
        front_field = cfg.get("front_field", "Text")
        max_cards = int(cfg.get("max_cards", 80))
        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        if not nids:
            self._status.setText(f"No notes under tag '{tag}' — nothing to analyse.")
            return
        if len(nids) > max_cards:
            if not askUser(
                f"{len(nids)} cards under '{tag}' — that's more than the cap "
                f"({max_cards}). Only the first {max_cards} will be sent. Continue?"
            ):
                return
            nids = nids[:max_cards]

        fronts: list[str] = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            p = _front_preview(note, front_field)
            if p:
                fronts.append(p)
        if not fronts:
            showWarning("Couldn't read the front field on any card under that tag.")
            return

        max_n = int(cfg.get("max_gaps", 10))
        system = GAP_SYSTEM_TMPL.format(max_n=max_n)
        user_msg = (
            "Learning objective:\n" + lo + "\n\n"
            f"Cards currently tagged for this LO ({len(fronts)}):\n"
            + "\n".join(f"- {c}" for c in fronts)
        )
        self._status.setText(
            f"Asking Claude what's missing from {len(fronts)} card(s) under '{tag}'…"
        )
        model = cfg.get("model") or None
        with loading(self._analyse_btn, "Analysing…"):
            gaps = core_api.ask_claude_json(
                prompt=user_msg, system=system, max_tokens=1024, model=model,
            )

        if not isinstance(gaps, list):
            self._status.setText("")
            return
        gaps = [g.strip() for g in gaps if isinstance(g, str) and g.strip()]
        if not gaps:
            self._gaps_box.setVisible(False)
            self._status.setText(
                f"No gaps found — your cards under '{tag}' appear to cover this LO well."
            )
            return

        self._gaps_list.clear()
        for g in gaps:
            item = QListWidgetItem(g)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._gaps_list.addItem(item)
        self._gaps_box.setVisible(True)
        self._status.setText(
            f"Claude found {len(gaps)} gap(s) across {len(fronts)} card(s)."
        )

    def _select_all(self) -> None:
        for i in range(self._gaps_list.count()):
            self._gaps_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self._gaps_list.count()):
            self._gaps_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _append_to_notes(self) -> None:
        if self._pane._current is None:
            return
        approved: list[str] = []
        for i in range(self._gaps_list.count()):
            it = self._gaps_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                t = it.text().strip()
                if t:
                    approved.append(t)
        if not approved:
            tooltip("Nothing checked.")
            return
        notes_widget = self._pane._schema_widgets.get("notes")
        if notes_widget is None:
            showWarning(
                "This LO type doesn't have a 'notes' field. Add one in "
                "Settings → Knowledge Gaps → edit the LO type."
            )
            return
        try:
            current = notes_widget["getter"]() or ""
        except Exception:
            current = ""
        bullets = "\n".join(f"- {g}" for g in approved)
        new_val = (current.rstrip() + "\n\n" + bullets).strip() if current.strip() else bullets
        try:
            notes_widget["setter"](new_val)
        except Exception as e:
            showWarning(f"Couldn't write to notes: {e}")
            return
        # Persist so the appended gaps survive a navigate-away.
        self._pane._save()
        self._gaps_list.clear()
        self._gaps_box.setVisible(False)
        self._status.setText(f"Appended {len(approved)} gap(s) to this LO's notes.")


# ── Detail pane ──────────────────────────────────────────────────────────────

class KGDetailPane(QWidget):
    """Right column — shows / edits / actions a single KG."""

    def __init__(self, parent_panel, parent=None):
        super().__init__(parent)
        self._parent_panel = parent_panel
        self._current: dict | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 4, 4)
        root.setSpacing(6)

        # Header row
        self._title_input = QLineEdit()
        self._title_input.setPlaceholderText("Title — what you don't know")
        f = self._title_input.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self._title_input.setFont(f)
        root.addWidget(self._title_input)

        meta_row = QHBoxLayout()
        meta_row.addWidget(QLabel("Type:"))
        self._type = QComboBox()
        self._rebuild_type_combo()
        meta_row.addWidget(self._type)
        meta_row.addSpacing(8)
        meta_row.addWidget(QLabel("Status:"))
        self._status = QComboBox()
        for key, label in STATUS_LABELS.items():
            self._status.addItem(label, key)
        meta_row.addWidget(self._status)
        meta_row.addSpacing(8)
        self._source_lbl = QLabel("")
        self._source_lbl.setStyleSheet("color: gray; font-size: 11px;")
        meta_row.addWidget(self._source_lbl)
        meta_row.addStretch(1)
        self._created_lbl = QLabel("")
        self._created_lbl.setStyleSheet("color: gray; font-size: 11px;")
        meta_row.addWidget(self._created_lbl)
        root.addLayout(meta_row)

        # Tags
        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tags:"))
        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("space-separated — School::Year3::Cardio")
        attach_tag_completer(self._tags_input, multi=True)
        tag_row.addWidget(self._tags_input, 1)
        root.addLayout(tag_row)

        # ── Dynamic schema-driven fields container ───────────────────────────
        # Built from the active type's `fields` list. Each entry yields a
        # widget keyed by its field key; values round-trip through kg["fields"].
        self._schema_container = QWidget()
        self._schema_layout = QVBoxLayout(self._schema_container)
        self._schema_layout.setContentsMargins(0, 0, 0, 0)
        self._schema_layout.setSpacing(6)
        self._schema_widgets: dict[str, dict] = {}  # key -> {widget, kind, getter, setter}
        root.addWidget(self._schema_container)
        # Rebuild whenever the type combo changes (preserves overlapping keys).
        self._type.currentIndexChanged.connect(self._on_type_changed)

        # Resources
        self._res_box = QGroupBox("Resources")
        rl = QVBoxLayout(self._res_box)
        rl.setContentsMargins(8, 6, 8, 6)
        rl.setSpacing(2)
        self._res_inner = QWidget()
        self._res_layout = QVBoxLayout(self._res_inner)
        self._res_layout.setContentsMargins(0, 0, 0, 0)
        self._res_layout.setSpacing(2)
        rl.addWidget(self._res_inner)
        add_res_btn = QPushButton("＋ Add resource")
        add_res_btn.setAutoDefault(False)
        add_res_btn.clicked.connect(self._add_resource_row)
        rl.addWidget(add_res_btn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._res_box)

        # LO analyser — only shown when the active KG's type is "lo".
        self._lo_analyser = _LOAnalyserSection(self)
        self._lo_analyser.setVisible(False)
        root.addWidget(self._lo_analyser)

        # Actions
        actions_row = QHBoxLayout()
        self._browse_btn = QPushButton("🔍 Send to Browse with Claude")
        self._browse_btn.setAutoDefault(False)
        self._browse_btn.clicked.connect(self._send_to_browse)
        actions_row.addWidget(self._browse_btn)
        self._create_btn = QPushButton("＋ Create card from this KG")
        self._create_btn.setAutoDefault(False)
        self._create_btn.clicked.connect(self._send_to_create)
        actions_row.addWidget(self._create_btn)
        actions_row.addStretch(1)
        self._delete_btn = QPushButton("🗑 Delete")
        self._delete_btn.setAutoDefault(False)
        self._delete_btn.clicked.connect(self._delete)
        actions_row.addWidget(self._delete_btn)
        self._save_btn = QPushButton("Save")
        self._save_btn.setAutoDefault(False)
        self._save_btn.clicked.connect(self._save)
        actions_row.addWidget(self._save_btn)
        root.addLayout(actions_row)

        # Empty-state overlay
        self._empty_lbl = QLabel("Select a knowledge gap, or click ＋ Add KG to start.")
        self._empty_lbl.setStyleSheet("color: gray;")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._empty_lbl)

        root.addStretch(1)

        self._clear()

    def _rebuild_type_combo(self) -> None:
        """Refresh the dropdown items from current config (called on load and
        after settings save)."""
        prev = self._type.currentData() if self._type.count() else None
        self._type.blockSignals(True)
        self._type.clear()
        for t in _load_types():
            self._type.addItem(t["name"], t["key"])
            self._type.setItemData(self._type.count() - 1,
                                   t.get("description", ""),
                                   Qt.ItemDataRole.ToolTipRole)
        if prev is not None:
            idx = self._type.findData(prev)
            if idx >= 0:
                self._type.setCurrentIndex(idx)
        self._type.blockSignals(False)

    # ── state ────────────────────────────────────────────────────────────────

    def load(self, kg: dict | None) -> None:
        self._current = kg
        if kg is None:
            self._clear()
            return
        self._set_widgets_visible(True)
        self._empty_lbl.setVisible(False)

        self._title_input.setText(kg.get("title", ""))

        # Type combo — may have changed since panel was built.
        self._rebuild_type_combo()
        type_key = kg.get("type", "kg")
        idx = self._type.findData(type_key)
        if idx < 0:
            # Type has been deleted from settings — show a sentinel so the
            # selection isn't silently coerced to the first entry.
            meta = _type_meta(type_key)
            self._type.addItem(meta["name"], type_key)
            idx = self._type.count() - 1
            self._type.setItemData(idx, "Type removed from settings",
                                   Qt.ItemDataRole.ToolTipRole)
        self._type.setCurrentIndex(idx)

        source = kg.get("source", "manual")
        source_label = SOURCE_LABELS.get(source, "?")
        self._source_lbl.setText(f"from <i>{source_label}</i>")
        self._source_lbl.setTextFormat(Qt.TextFormat.RichText)

        status = kg.get("status", "open")
        idx = self._status.findData(status)
        self._status.setCurrentIndex(idx if idx >= 0 else 0)

        self._tags_input.setText(" ".join(kg.get("tags", [])))

        self._created_lbl.setText(f"added {_short_date(kg.get('created_at', ''))}")

        # Build schema-driven fields from the KG's current type.
        self._rebuild_schema_for_type(type_key, kg=kg)

        # Resources
        self._clear_resource_rows()
        for r in kg.get("resources", []) or []:
            self._add_resource_row(r.get("label", ""), r.get("url", ""))

        # LO analyser visibility — reset its transient state on KG change.
        self._lo_analyser.reset()
        self._lo_analyser.setVisible(type_key == "lo")

    def _clear(self) -> None:
        self._current = None
        self._set_widgets_visible(False)
        self._empty_lbl.setVisible(True)

    def _set_widgets_visible(self, visible: bool) -> None:
        for w in (self._title_input, self._type, self._source_lbl, self._status,
                  self._created_lbl, self._tags_input, self._schema_container,
                  self._res_box, self._browse_btn, self._create_btn,
                  self._delete_btn, self._save_btn):
            w.setVisible(visible)
        # The LO analyser stays hidden when nothing is selected; load() re-
        # shows it only for type=lo KGs.
        if not visible:
            self._lo_analyser.setVisible(False)

    # ── schema-driven fields ─────────────────────────────────────────────────

    def _schema_for_type(self, type_key: str) -> list[dict]:
        for t in _load_types():
            if t["key"] == type_key:
                fields = t.get("fields") or []
                # Coerce + de-duplicate.
                seen: set[str] = set()
                out: list[dict] = []
                for spec in fields:
                    if not isinstance(spec, dict):
                        continue
                    key = str(spec.get("key", "")).strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "key":         key,
                        "label":       str(spec.get("label", key)),
                        "kind":        str(spec.get("kind", "text")).lower(),
                        "placeholder": str(spec.get("placeholder", "")),
                    })
                return out
        # Unknown type — no schema beyond a generic notes field.
        return [{"key": "notes", "label": "Notes", "kind": "longtext",
                 "placeholder": ""}]

    def _on_type_changed(self, _idx: int) -> None:
        if self._current is None:
            return
        # Persist current values to a stash keyed by field key so values
        # survive a type switch where keys overlap.
        prev_values = self._collect_schema_fields()
        new_type = self._type.currentData() or "kg"
        merged = dict(self._current.get("fields") or {})
        merged.update(prev_values)
        # Build with merged values so overlapping keys keep what was typed.
        pseudo_kg = {"fields": merged}
        self._rebuild_schema_for_type(new_type, kg=pseudo_kg)
        # Show/hide the inline LO analyser to match the new type.
        self._lo_analyser.reset()
        self._lo_analyser.setVisible(new_type == "lo")

    def _rebuild_schema_for_type(self, type_key: str, kg: dict | None = None) -> None:
        # Tear down existing widgets.
        while self._schema_layout.count():
            it = self._schema_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._schema_widgets = {}

        schema = self._schema_for_type(type_key)
        if not schema:
            empty = QLabel("<i>This type has no fields configured. Edit it in "
                           "Settings → Knowledge Gaps to add some.</i>")
            empty.setTextFormat(Qt.TextFormat.RichText)
            empty.setStyleSheet("color: gray; font-size: 11px;")
            empty.setWordWrap(True)
            self._schema_layout.addWidget(empty)
            return

        existing_values = (kg or {}).get("fields") or {}
        # Legacy fallback — if old top-level keys exist on the KG dict and the
        # store hasn't normalised yet, surface those too.
        if kg is not None and not existing_values:
            for legacy in ("notes", "stem_html", "system", "subsystem", "topic",
                           "platform", "lo", "lo_tag"):
                if kg.get(legacy):
                    existing_values[legacy] = kg[legacy]

        for spec in schema:
            row, getter, setter = self._build_field_widget(spec)
            label_text = spec["label"]
            kind = spec["kind"]
            label = QLabel(f"<b>{label_text}</b>")
            label.setTextFormat(Qt.TextFormat.RichText)
            self._schema_layout.addWidget(label)
            self._schema_layout.addWidget(row)
            self._schema_widgets[spec["key"]] = {
                "kind":   kind,
                "widget": row,
                "getter": getter,
                "setter": setter,
            }
            # Pre-fill from existing values.
            val = existing_values.get(spec["key"], "")
            if val:
                try:
                    setter(val)
                except Exception as e:
                    print(f"[ankisstant] set field {spec['key']!r}: {e}")

    def _build_field_widget(self, spec: dict):
        """Return (widget, getter, setter) for the given field spec."""
        kind = spec["kind"]
        ph   = spec.get("placeholder", "")
        if kind == "html":
            # Reuse the screenshot-aware editor from QBank capture.
            from .qbank.capture_dialog import _StemEdit
            w = _StemEdit()
            w.setAcceptRichText(True)
            w.setPlaceholderText(ph or "Paste text or a screenshot (Cmd/Ctrl+V)")
            w.setMinimumHeight(110)
            return w, (lambda: ("" if w.is_empty() else w.get_html())), \
                      (lambda v: w.setHtml(v) if v else w.clear())
        if kind == "longtext":
            w = QPlainTextEdit()
            w.setPlaceholderText(ph)
            w.setMinimumHeight(60)
            return w, (lambda: w.toPlainText().strip()), \
                      (lambda v: w.setPlainText(str(v) if v is not None else ""))
        if kind == "url":
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            le = QLineEdit()
            le.setPlaceholderText(ph or "https://…")
            open_btn = QPushButton("Open")
            open_btn.setFixedWidth(58)
            open_btn.setAutoDefault(False)
            open_btn.clicked.connect(lambda _c=False, e=le: self._open_url(e.text()))
            h.addWidget(le, 1)
            h.addWidget(open_btn)
            return w, (lambda: le.text().strip()), \
                      (lambda v: le.setText(str(v) if v is not None else ""))
        if kind == "tag":
            w = QLineEdit()
            w.setPlaceholderText(ph or "School::Year3::…")
            attach_tag_completer(w, multi=False)
            return w, (lambda: w.text().strip()), \
                      (lambda v: w.setText(str(v) if v is not None else ""))
        # Default: text
        w = QLineEdit()
        w.setPlaceholderText(ph)
        return w, (lambda: w.text().strip()), \
                  (lambda v: w.setText(str(v) if v is not None else ""))

    def _collect_schema_fields(self) -> dict:
        out: dict = {}
        for key, info in self._schema_widgets.items():
            try:
                val = info["getter"]()
            except Exception:
                val = ""
            if val:
                out[key] = val
        return out

    # ── resources ────────────────────────────────────────────────────────────

    def _add_resource_row(self, label: str = "", url: str = "") -> None:
        # Ignore the QPushButton-emitted "checked" bool when triggered via click.
        if isinstance(label, bool):
            label, url = "", ""
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        lbl_input = QLineEdit(label or "")
        lbl_input.setPlaceholderText("Label (e.g. BNF chapter)")
        lbl_input.setMaximumWidth(220)
        url_input = QLineEdit(url or "")
        url_input.setPlaceholderText("https://…")
        open_btn = QPushButton("Open")
        open_btn.setFixedWidth(58)
        open_btn.setAutoDefault(False)
        open_btn.clicked.connect(lambda _c=False, u=url_input: self._open_url(u.text()))
        rm_btn = QPushButton("×")
        rm_btn.setFixedWidth(28)
        rm_btn.setAutoDefault(False)
        rm_btn.clicked.connect(lambda _c=False, r=row: self._remove_resource_row(r))
        rl.addWidget(lbl_input)
        rl.addWidget(url_input, 1)
        rl.addWidget(open_btn)
        rl.addWidget(rm_btn)
        row._lbl = lbl_input
        row._url = url_input
        self._res_layout.addWidget(row)

    def _remove_resource_row(self, row: QWidget) -> None:
        self._res_layout.removeWidget(row)
        row.deleteLater()

    def _clear_resource_rows(self) -> None:
        while self._res_layout.count():
            it = self._res_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _collect_resources(self) -> list[dict]:
        out = []
        for i in range(self._res_layout.count()):
            row = self._res_layout.itemAt(i).widget()
            if row is None:
                continue
            lbl = getattr(row, "_lbl", None)
            url = getattr(row, "_url", None)
            if lbl is None or url is None:
                continue
            l = lbl.text().strip()
            u = url.text().strip()
            if l or u:
                out.append({"label": l, "url": u})
        return out

    def _open_url(self, url: str) -> None:
        url = url.strip()
        if not url:
            tooltip("No URL.")
            return
        from aqt.qt import QDesktopServices, QUrl
        QDesktopServices.openUrl(QUrl(url))

    # ── persistence ──────────────────────────────────────────────────────────

    def _gather(self) -> dict:
        kg = dict(self._current or {})
        kg["title"]     = self._title_input.text().strip()
        kg["type"]      = self._type.currentData() or kg.get("type", "kg")
        kg["status"]    = self._status.currentData() or "open"
        kg["tags"]      = [t for t in self._tags_input.text().split() if t.strip()]
        kg["fields"]    = self._collect_schema_fields()
        kg["resources"] = self._collect_resources()
        # Strip legacy top-level keys so they don't shadow the new fields dict.
        for legacy in ("notes", "stem_html", "system", "subsystem", "topic",
                       "platform", "lo", "lo_tag"):
            kg.pop(legacy, None)
        return kg

    def _save(self) -> None:
        if not self._current:
            return
        merged = self._gather()
        if not merged.get("title"):
            tooltip("Title is required.")
            return
        updated = kg_store.update(self._current["id"], **{
            k: v for k, v in merged.items() if k != "id"
        })
        if updated:
            self._current = updated
            self._parent_panel.refresh_list(preserve_selection=updated["id"])
            tooltip("Saved.")

    def _delete(self) -> None:
        if not self._current:
            return
        if not askUser(
            f"Delete this knowledge gap?\n\n{self._current.get('title', '')}",
            defaultno=True,
        ):
            return
        kg_store.remove(self._current["id"])
        self._parent_panel.refresh_list()

    # ── actions ──────────────────────────────────────────────────────────────

    def _send_to_browse(self) -> None:
        if not self._current:
            return
        # Persist any local edits first so the Browse handoff sees fresh data.
        self._save()
        kg = self._current
        main = self.window()
        if not hasattr(main, "_show_tool"):
            showWarning("Open the KG page from Tools → Ankisstant first.")
            return
        # Switch to Browse and preload it.
        try:
            from .browse import preload_for_kg
            main._show_tool("browse")
            preload_for_kg(kg)
        except Exception as e:
            print(f"[ankisstant] send to Browse failed: {e}")
            showWarning(f"Couldn't open Browse: {e}")

    def _send_to_create(self) -> None:
        if not self._current:
            return
        self._save()
        kg = self._current
        main = self.window()
        if not hasattr(main, "gap_queue"):
            showWarning("Open the KG page from Tools → Ankisstant first.")
            return
        # Pull supplemental context out of the type-specific fields blob.
        stem_html = kg_store.field(kg, "stem_html")
        notes_bits = []
        for k in ("notes", "concept", "lo"):
            v = kg_store.field(kg, k)
            if v:
                notes_bits.append(v)
        item = {
            "title":     kg.get("title", ""),
            "kg_id":     kg.get("id", ""),
            "stem_html": stem_html or None,
            "notes":     "\n\n".join(notes_bits) or None,
        }
        main.gap_queue.append(item)
        if hasattr(main, "refresh_queue_badge"):
            main.refresh_queue_badge()
        if hasattr(main, "show_create_tool"):
            main.show_create_tool()
        # Leave status as open — Create marks it done on a successful Add.
        self._parent_panel.refresh_list(preserve_selection=kg["id"])


# ── Main panel ───────────────────────────────────────────────────────────────

_STATUS_FILTERS = [
    ("open",      "Open"),
    ("done",      "Done"),
    ("dismissed", "Dismissed"),
]


class KnowledgeGapsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter = "open"
        self._build()
        self.refresh_list()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        # Title row
        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>Knowledge Gaps</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "Knowledge Gaps — help",
            "<h3>What it does</h3>"
            "<p>One unified queue of things you don't know — from any source: "
            "manual notes, captured QBank misses, or things Browse couldn't find.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Add a KG via <b>＋ Add KG</b> (here or from the home screen).</li>"
            "<li>Pick a KG. Either <b>Send to Browse</b> to find existing cards, "
            "or <b>Create card from this KG</b> to draft a new one.</li>"
            "<li>For <b>LO</b>-type KGs, the detail pane shows an inline "
            "<i>Analyse this LO</i> section that pulls cards under the LO's tag "
            "and flags concepts they don't cover — accepted gaps append into "
            "the LO's Notes.</li>"
            "<li>When a card is added (or matching cards re-graded), the KG "
            "is marked Done automatically.</li>"
            "</ol>",
            self,
        ))
        root.addLayout(title_row)

        intro = QLabel(
            "Every gap from every source. Pick one, then send it to Browse or "
            "straight to Create."
        )
        intro.setStyleSheet("color: gray; font-size: 11px;")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Filter chips — two rows: status filters on top, type filters below.
        self._chip_buttons: dict[str, QPushButton] = {}
        self._status_chip_row = self._make_chip_row(root, _STATUS_FILTERS)
        # Types row is rebuilt on every settings save (see refresh_chips).
        self._type_chip_row_holder = QHBoxLayout()
        root.addLayout(self._type_chip_row_holder)
        self._rebuild_type_chip_row()
        if self._filter in self._chip_buttons:
            self._chip_buttons[self._filter].setChecked(True)

        # Splitter: list / detail
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Left side
        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 6, 0)
        lcol.setSpacing(4)
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("color: gray; font-size: 11px;")
        lcol.addWidget(self._count_lbl)
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.itemSelectionChanged.connect(self._on_select)
        lcol.addWidget(self._list, 1)
        btn_row = QHBoxLayout()
        add_btn = QPushButton("＋ Add KG")
        add_btn.setAutoDefault(False)
        add_btn.clicked.connect(self._on_add_kg)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(28)
        refresh_btn.setAutoDefault(False)
        refresh_btn.clicked.connect(lambda: self.refresh_list())
        btn_row.addWidget(add_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(refresh_btn)
        lcol.addLayout(btn_row)

        # Clear-completed row — sits below add/analyse so it's out of the way.
        clear_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear completed (0)")
        self._clear_btn.setAutoDefault(False)
        self._clear_btn.setStyleSheet("font-size: 11px;")
        self._clear_btn.setToolTip(
            "Remove every KG with status Done or Dismissed. "
            "Other items stay where they are."
        )
        self._clear_btn.clicked.connect(self._on_clear_completed)
        clear_row.addWidget(self._clear_btn)
        clear_row.addStretch(1)
        lcol.addLayout(clear_row)
        splitter.addWidget(left)

        # Right side
        right_wrap = QScrollArea()
        right_wrap.setWidgetResizable(True)
        right_wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        self._detail = KGDetailPane(self)
        right_wrap.setWidget(self._detail)
        splitter.addWidget(right_wrap)

        splitter.setSizes([280, 620])
        root.addWidget(splitter, 1)

    # ── chip rows ────────────────────────────────────────────────────────────

    def _make_chip_row(self, parent_layout, entries) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        for entry in entries:
            self._add_chip(row, entry)
        row.addStretch(1)
        parent_layout.addLayout(row)
        return row

    def _add_chip(self, row, entry) -> None:
        # entry = (key, label)  OR (key, label, color)
        if len(entry) == 3:
            key, label, color = entry
        else:
            key, label = entry
            color = None
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setAutoExclusive(True)
        btn.setMinimumHeight(24)
        if color:
            btn.setStyleSheet(
                f"QPushButton {{ padding: 2px 10px; font-size: 11px; "
                f"border: 1px solid {color}; border-radius: 12px; "
                f"background: transparent; color: {color}; }}"
                f"QPushButton:checked {{ background: {color}; color: white; }}"
                f"QPushButton:hover:!checked {{ background: rgba(127,127,127,0.12); }}"
            )
        else:
            btn.setStyleSheet(
                "QPushButton { padding: 2px 10px; font-size: 11px; "
                "border: 1px solid rgba(127,127,127,0.3); border-radius: 12px; "
                "background: transparent; }"
                "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
                "QPushButton:hover:!checked { background: rgba(127,127,127,0.12); }"
            )
        btn.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
        row.addWidget(btn)
        self._chip_buttons[key] = btn

    def _rebuild_type_chip_row(self) -> None:
        # Clear out any existing type chips first.
        for key in list(self._chip_buttons.keys()):
            if key.startswith("type:"):
                btn = self._chip_buttons.pop(key)
                btn.setParent(None)
                btn.deleteLater()
        # Strip the prior row's items.
        while self._type_chip_row_holder.count():
            item = self._type_chip_row_holder.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # Build fresh row.
        row = QHBoxLayout()
        row.setSpacing(4)
        prefix_label = QLabel("Types:")
        prefix_label.setStyleSheet("color: gray; font-size: 11px;")
        # Wrap in a widget so we can put it inside the holder layout.
        from aqt.qt import QWidget as _QW
        wrapper = _QW()
        wrapper.setLayout(row)
        row.addWidget(prefix_label)
        for t in _load_types():
            self._add_chip(row, (f"type:{t['key']}", t["name"], t["color"]))
        row.addStretch(1)
        self._type_chip_row_holder.addWidget(wrapper)

    # ── filters / list ───────────────────────────────────────────────────────

    def refresh_setup_banner(self) -> None:
        try:
            self._setup_banner.setVisible(not provider_configured())
        except Exception:
            pass

    def set_filter(self, key: str) -> None:
        """Programmatic filter switch (used by QBank's Review button)."""
        # Translate legacy/short keys: source names → type keys, where it
        # makes sense.
        translated = {
            "qbank":   "type:mq",
            "analyse": "type:lo",
            "manual":  "type:kg",
            "browse":  "all",
        }.get(key, key)
        if translated not in self._chip_buttons:
            return
        self._chip_buttons[translated].setChecked(True)
        self._set_filter(translated)

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self.refresh_list()

    def _matches_filter(self, kg: dict) -> bool:
        f = self._filter
        status = kg.get("status", "open")
        if f in ("open", "done", "dismissed"):
            return status == f
        if f.startswith("type:"):
            return kg.get("type", "kg") == f.split(":", 1)[1]
        return True

    def refresh_list(self, preserve_selection: str | None = None) -> None:
        items = kg_store.load_all()
        # Active items above done/dismissed; newest first within each bucket.
        # Python's sort is stable so we can apply secondary then primary.
        status_order = {"open": 0, "done": 1, "dismissed": 2}
        items.sort(key=lambda k: k.get("created_at", ""), reverse=True)
        items.sort(key=lambda k: status_order.get(k.get("status", "open"), 4))

        visible = [k for k in items if self._matches_filter(k)]
        self._count_lbl.setText(
            f"{len(visible)} shown · {len(items)} total"
        )
        done_count = sum(
            1 for k in items
            if k.get("status") in ("done", "dismissed")
        )
        self._clear_btn.setText(f"Clear completed ({done_count})")
        self._clear_btn.setEnabled(done_count > 0)

        prev_id = preserve_selection
        if prev_id is None:
            sel = self._list.currentItem()
            if isinstance(sel, _KGListItem):
                prev_id = sel.kg_id

        self._list.blockSignals(True)
        self._list.clear()
        target_row = -1
        for i, kg in enumerate(visible):
            li = _KGListItem(kg)
            self._list.addItem(li)
            if prev_id and kg.get("id") == prev_id:
                target_row = i
        self._list.blockSignals(False)

        if target_row >= 0:
            self._list.setCurrentRow(target_row)
        elif visible:
            self._list.setCurrentRow(0)
        else:
            self._detail.load(None)

    def _on_select(self) -> None:
        items = self._list.selectedItems()
        if not items:
            self._detail.load(None)
            return
        li = items[0]
        if not isinstance(li, _KGListItem):
            return
        kg = kg_store.get(li.kg_id)
        self._detail.load(kg)

    # ── add / analyse ────────────────────────────────────────────────────────

    def _on_add_kg(self) -> None:
        dlg = AddKGDialog(self)
        if dlg.exec():
            self.refresh_list()

    def _on_clear_completed(self) -> None:
        items = kg_store.load_all()
        done_ids = [k["id"] for k in items
                    if k.get("status") in ("done", "dismissed") and k.get("id")]
        if not done_ids:
            tooltip("Nothing to clear — no completed items.")
            return
        if not askUser(
            f"Remove {len(done_ids)} completed knowledge gap(s) from the list?\n\n"
            "This deletes them permanently — open items are untouched.",
            defaultno=True,
        ):
            return
        for kid in done_ids:
            kg_store.remove(kid)
        tooltip(f"Cleared {len(done_ids)} completed gap(s).")
        self.refresh_list()

# ── Tool contract ────────────────────────────────────────────────────────────

_panel: KnowledgeGapsPanel | None = None
_scroll: QScrollArea | None = None


def init(main_window) -> None:
    return None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        _panel = KnowledgeGapsPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.refresh_setup_banner()
        # Pick up any newly-added or renamed types from Settings.
        try:
            _panel._rebuild_type_chip_row()
        except Exception:
            pass
        _panel.refresh_list()
    return _scroll


def _refresh_open_panel() -> None:
    """If the KG panel is currently visible, reload it. Called by AddKGDialog."""
    if _panel is not None:
        try:
            _panel.refresh_list()
        except Exception:
            pass
