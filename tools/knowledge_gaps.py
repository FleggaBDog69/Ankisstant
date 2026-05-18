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

from ..core.config import tool_config
from ..core.qt_utils import (
    attach_tag_completer, make_help_button, make_setup_banner,
    provider_configured,
)
from .kg import store as kg_store


NAME = "Knowledge Gaps"


# ── helpers ──────────────────────────────────────────────────────────────────

SOURCE_LABELS = {
    "manual":  ("Manual",   "#6b7280"),
    "analyse": ("Analyse",  "#9333ea"),
    "qbank":   ("QBank",    "#b45309"),
    "browse":  ("Browse",   "#0284c7"),
}

STATUS_LABELS = {
    "open":        "Open",
    "in_progress": "In Progress",
    "done":        "Done",
    "dismissed":   "Dismissed",
}


def _short_date(iso: str) -> str:
    return (iso or "")[:10]


def _format_levels(kg: dict) -> str:
    levels = [kg.get("system", ""), kg.get("subsystem", ""), kg.get("topic", "")]
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
        kg_store.add(
            title=title,
            source="manual",
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
        source_label, _color = SOURCE_LABELS.get(kg.get("source", "manual"),
                                                 ("?", "#888"))
        status_label = STATUS_LABELS.get(kg.get("status", "open"), "Open")
        title = kg.get("title", "(untitled)") or "(untitled)"
        text = f"{title}"
        if len(text) > 70:
            text = text[:70].rstrip() + "…"
        self.setText(text)
        tt_lines = [
            f"Source: {source_label}",
            f"Status: {status_label}",
        ]
        levels = _format_levels(kg)
        if levels:
            tt_lines.append(f"Path: {levels}")
        tt_lines.append(f"Added: {_short_date(kg.get('created_at', ''))}")
        self.setToolTip("\n".join(tt_lines))


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
        self._source_badge = QLabel("")
        self._source_badge.setTextFormat(Qt.TextFormat.RichText)
        meta_row.addWidget(self._source_badge)
        meta_row.addSpacing(8)
        meta_row.addWidget(QLabel("Status:"))
        self._status = QComboBox()
        for key, label in STATUS_LABELS.items():
            self._status.addItem(label, key)
        meta_row.addWidget(self._status)
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

        # System / Subsystem / Topic (hidden when all blank)
        self._levels_box = QFrame()
        lv = QHBoxLayout(self._levels_box)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)
        self._system    = QLineEdit(); self._system.setPlaceholderText("System")
        self._subsystem = QLineEdit(); self._subsystem.setPlaceholderText("Subsystem")
        self._topic     = QLineEdit(); self._topic.setPlaceholderText("Topic")
        lv.addWidget(QLabel("Path:"))
        lv.addWidget(self._system)
        lv.addWidget(self._subsystem)
        lv.addWidget(self._topic)
        root.addWidget(self._levels_box)

        # Stem preview (only for KGs with captured stems)
        self._stem_label = QLabel("<b>Captured stem</b>")
        self._stem_view = QTextEdit()
        self._stem_view.setReadOnly(True)
        self._stem_view.setMaximumHeight(170)
        root.addWidget(self._stem_label)
        root.addWidget(self._stem_view)

        # Notes
        root.addWidget(QLabel("<b>Notes</b>"))
        self._notes = QPlainTextEdit()
        self._notes.setMinimumHeight(70)
        root.addWidget(self._notes)

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

    # ── state ────────────────────────────────────────────────────────────────

    def load(self, kg: dict | None) -> None:
        self._current = kg
        if kg is None:
            self._clear()
            return
        self._set_widgets_visible(True)
        self._empty_lbl.setVisible(False)

        self._title_input.setText(kg.get("title", ""))

        source = kg.get("source", "manual")
        label, color = SOURCE_LABELS.get(source, ("?", "#888"))
        self._source_badge.setText(
            f"<span style='background:{color};color:white;padding:2px 6px;"
            f"border-radius:4px;font-size:10px;'>{label}</span>"
        )

        status = kg.get("status", "open")
        idx = self._status.findData(status)
        self._status.setCurrentIndex(idx if idx >= 0 else 0)

        self._tags_input.setText(" ".join(kg.get("tags", [])))
        self._system.setText(kg.get("system", ""))
        self._subsystem.setText(kg.get("subsystem", ""))
        self._topic.setText(kg.get("topic", ""))

        has_levels = bool(kg.get("system") or kg.get("subsystem") or kg.get("topic")
                          or source == "qbank")
        self._levels_box.setVisible(has_levels)

        self._created_lbl.setText(f"added {_short_date(kg.get('created_at', ''))}")

        stem = kg.get("stem_html", "") or ""
        has_stem = bool(stem.strip())
        self._stem_label.setVisible(has_stem)
        self._stem_view.setVisible(has_stem)
        if has_stem:
            if "<" in stem:
                self._stem_view.setHtml(stem)
            else:
                self._stem_view.setPlainText(stem)

        self._notes.setPlainText(kg.get("notes", "") or "")

        # Resources
        self._clear_resource_rows()
        for r in kg.get("resources", []) or []:
            self._add_resource_row(r.get("label", ""), r.get("url", ""))

    def _clear(self) -> None:
        self._current = None
        self._set_widgets_visible(False)
        self._empty_lbl.setVisible(True)

    def _set_widgets_visible(self, visible: bool) -> None:
        for w in (self._title_input, self._source_badge, self._status,
                  self._created_lbl, self._tags_input, self._levels_box,
                  self._stem_label, self._stem_view, self._notes,
                  self._res_box, self._browse_btn, self._create_btn,
                  self._delete_btn, self._save_btn):
            w.setVisible(visible)

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
        kg["status"]    = self._status.currentData() or "open"
        kg["tags"]      = [t for t in self._tags_input.text().split() if t.strip()]
        kg["system"]    = self._system.text().strip()
        kg["subsystem"] = self._subsystem.text().strip()
        kg["topic"]     = self._topic.text().strip()
        kg["notes"]     = self._notes.toPlainText().strip()
        kg["resources"] = self._collect_resources()
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
        # Push as a richer dict so Create can use the stem as supplemental context.
        item = {
            "title":     kg.get("title", ""),
            "kg_id":     kg.get("id", ""),
            "stem_html": kg.get("stem_html", "") or None,
            "notes":     kg.get("notes", "") or None,
        }
        main.gap_queue.append(item)
        if hasattr(main, "refresh_queue_badge"):
            main.refresh_queue_badge()
        if hasattr(main, "show_create_tool"):
            main.show_create_tool()
        kg_store.update(kg["id"], status="in_progress")
        self._parent_panel.refresh_list(preserve_selection=kg["id"])


# ── Main panel ───────────────────────────────────────────────────────────────

_FILTERS = [
    ("all",      "All"),
    ("open",     "Open"),
    ("in_progress", "In Progress"),
    ("manual",   "Manual"),
    ("analyse",  "Analyse"),
    ("qbank",    "QBank"),
    ("browse",   "Browse"),
    ("done",     "Done"),
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
            "manual notes, the Analyse LO feature, captured QBank misses, or "
            "things Browse couldn't find.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Add a KG via <b>＋ Add KG</b> (here or from the home screen), "
            "or generate gaps via <b>Analyse LO…</b>.</li>"
            "<li>Pick a KG. Either <b>Send to Browse</b> to find existing cards, "
            "or <b>Create card from this KG</b> to draft a new one.</li>"
            "<li>When the card is added (or matching cards re-graded), the KG "
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

        # Filter chips
        chip_row = QHBoxLayout()
        chip_row.setSpacing(4)
        self._chip_buttons: dict[str, QPushButton] = {}
        for key, label in _FILTERS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setMinimumHeight(24)
            btn.setStyleSheet(
                "QPushButton { padding: 2px 10px; font-size: 11px; border: 1px solid rgba(127,127,127,0.3); "
                "border-radius: 12px; background: transparent; }"
                "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
                "QPushButton:hover:!checked { background: rgba(127,127,127,0.12); }"
            )
            btn.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
            chip_row.addWidget(btn)
            self._chip_buttons[key] = btn
        chip_row.addStretch(1)
        self._chip_buttons[self._filter].setChecked(True)
        root.addLayout(chip_row)

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
        analyse_btn = QPushButton("Analyse LO…")
        analyse_btn.setAutoDefault(False)
        analyse_btn.clicked.connect(self._on_analyse)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(28)
        refresh_btn.setAutoDefault(False)
        refresh_btn.clicked.connect(lambda: self.refresh_list())
        btn_row.addWidget(add_btn)
        btn_row.addWidget(analyse_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(refresh_btn)
        lcol.addLayout(btn_row)
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

    # ── filters / list ───────────────────────────────────────────────────────

    def refresh_setup_banner(self) -> None:
        try:
            self._setup_banner.setVisible(not provider_configured())
        except Exception:
            pass

    def set_filter(self, key: str) -> None:
        """Programmatic filter switch (used by QBank's Review button)."""
        if key not in self._chip_buttons:
            return
        self._chip_buttons[key].setChecked(True)
        self._set_filter(key)

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self.refresh_list()

    def _matches_filter(self, kg: dict) -> bool:
        f = self._filter
        if f == "all":
            return True
        if f in ("manual", "analyse", "qbank", "browse"):
            return kg.get("source") == f
        if f in ("open", "in_progress", "done"):
            return kg.get("status") == f
        return True

    def refresh_list(self, preserve_selection: str | None = None) -> None:
        items = kg_store.load_all()
        # Sort: open first (status open > in_progress > done > dismissed),
        # then newest first within each group.
        status_order = {"open": 0, "in_progress": 1, "done": 2, "dismissed": 3}
        items.sort(key=lambda k: (
            status_order.get(k.get("status", "open"), 4),
            -1 * len(k.get("created_at", "")),  # newer ISO-strings sort first within the tie
            k.get("created_at", ""),
        ), reverse=False)
        # Quick win: newest by created_at descending.
        items.sort(key=lambda k: k.get("created_at", ""), reverse=True)

        visible = [k for k in items if self._matches_filter(k)]
        self._count_lbl.setText(
            f"{len(visible)} shown · {len(items)} total"
        )

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

    def _on_analyse(self) -> None:
        # Embed the existing GapAnalyserPanel in a dialog. On close we
        # refresh the list to surface any newly-queued gaps.
        from .gap_analyser import GapAnalyserPanel
        dlg = QDialog(self)
        dlg.setWindowTitle("Analyse Knowledge Gaps")
        dlg.resize(720, 600)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        panel = GapAnalyserPanel()
        scroll.setWidget(panel)
        layout.addWidget(scroll)
        dlg.exec()
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
        _panel.refresh_list()
    return _scroll


def _refresh_open_panel() -> None:
    """If the KG panel is currently visible, reload it. Called by AddKGDialog."""
    if _panel is not None:
        try:
            _panel.refresh_list()
        except Exception:
            pass
