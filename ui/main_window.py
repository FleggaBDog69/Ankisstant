# The Ankisstant main window — left sidebar of nav buttons + a stacked panel
# area on the right. Remembers its size + position between sessions.

from __future__ import annotations

import importlib

from aqt import mw
from aqt.qt import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, Qt, QVBoxLayout, QWidget,
)

from ..core.config import (
    get_window_geometry, set_window_geometry, tool_config, tool_enabled,
)


# Root package name as actually loaded. Installing from AnkiWeb names the
# add-on folder by its numeric ID (e.g. "123456789"), not "ankisstant", so the
# module path must be derived at runtime — hardcoding "ankisstant.*" only works
# for local "Install from file" builds and breaks every AnkiWeb download.
_PKG = __name__.split(".")[0]

# (tool_key, display_label, module_dotted_path)
TOOLS: list[tuple[str, str, str]] = [
    ("knowledge_gaps",   "Knowledge Gaps",  f"{_PKG}.tools.knowledge_gaps"),
    ("qbank",            "AI QBank",        f"{_PKG}.tools.qbank"),
    ("browse",           "AI Browse",       f"{_PKG}.tools.browse"),
    ("card_creator",     "AI Create",       f"{_PKG}.tools.card_creator"),
]


def _disabled_placeholder(label: str) -> QWidget:
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title = QLabel(f"<h2 style='margin:0'>{label}</h2>")
    title.setTextFormat(Qt.TextFormat.RichText)
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)
    msg = QLabel("Tool disabled — enable in Settings.")
    msg.setStyleSheet("color: gray; font-size: 13px;")
    msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(msg)
    return w


class MainWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Ankisstant")
        self.setMinimumSize(900, 600)
        # Allow this dialog to behave like a proper top-level window.
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)

        # Restore previous geometry.
        geom = get_window_geometry()
        self.resize(int(geom.get("width") or 900), int(geom.get("height") or 600))
        x, y = geom.get("x"), geom.get("y")
        if x is not None and y is not None:
            try:
                self.move(int(x), int(y))
            except Exception:
                pass

        self._panel_cache: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, QPushButton] = {}

        # Session-scoped queue of gaps waiting to become cards. Items are
        # dicts: {"title": str, "kg_id": str | None, "stem_html": str | None,
        # "notes": str | None}. The Knowledge Gaps page pushes here; Create
        # pops one at a time, and a kg_id (when set) is marked done in the
        # KG store on successful Add.
        self.gap_queue: list[dict] = []
        # Session-scoped queue of KGs waiting to be looked up in Browse.
        # Items are full KG dicts (id, title, fields, tags, type). The
        # Knowledge Gaps page pushes here; Browse works through them and a
        # successful Tag & Unsuspend marks the KG done and advances.
        self.browse_queue: list[dict] = []

        self._build()
        self.refresh_queue_badge()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setFrameShape(QFrame.Shape.StyledPanel)
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet(
            "QFrame { background-color: palette(window); border-right: 1px solid palette(mid); }"
        )
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(8, 14, 8, 8)
        side_layout.setSpacing(4)

        title = QLabel("<b style='font-size:14px'>Ankisstant</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setStyleSheet("padding: 4px 6px 10px; opacity: 0.85;")
        side_layout.addWidget(title)

        for key, label, _path in TOOLS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setMinimumHeight(34)
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 4px 10px; border: none; "
                "background: transparent; }"
                "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); border-radius: 6px; }"
                "QPushButton:hover:!checked { background: rgba(127,127,127,0.12); border-radius: 6px; }"
                "QPushButton:disabled { color: rgba(127,127,127,0.6); }"
            )
            btn.setEnabled(tool_enabled(key))
            btn.clicked.connect(lambda _checked=False, k=key: self._show_tool(k))
            self._nav_buttons[key] = btn
            side_layout.addWidget(btn)

        side_layout.addStretch(1)

        add_kg_btn = QPushButton("＋  Add KG")
        add_kg_btn.setMinimumHeight(30)
        add_kg_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 10px; border: none; background: transparent; }"
            "QPushButton:hover { background: rgba(127,127,127,0.12); border-radius: 6px; }"
        )
        add_kg_btn.clicked.connect(self._open_add_kg)
        side_layout.addWidget(add_kg_btn)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.setMinimumHeight(34)
        settings_btn.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 10px; border: none; background: transparent; }"
            "QPushButton:hover { background: rgba(127,127,127,0.12); border-radius: 6px; }"
        )
        settings_btn.clicked.connect(self._open_settings)
        side_layout.addWidget(settings_btn)

        root.addWidget(sidebar)

        # ── Stacked panel area ────────────────────────────────────────────────
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Placeholder welcome page so the window has content on first open.
        welcome = QWidget()
        wl = QVBoxLayout(welcome)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wl.addWidget(QLabel("<h2 style='margin:0'>Welcome to Ankisstant</h2>"))
        msg = QLabel("Pick a tool from the sidebar to get started.")
        msg.setStyleSheet("color: gray;")
        wl.addWidget(msg)
        self.stack.addWidget(welcome)
        root.addWidget(self.stack, 1)

        # Auto-select the first enabled tool on open.
        for key, _label, _path in TOOLS:
            if tool_enabled(key):
                self._nav_buttons[key].setChecked(True)
                self._show_tool(key)
                break

    # ── queue handoff (Browse → Create) ───────────────────────────────────────

    def refresh_queue_badge(self) -> None:
        """Update the Create and Browse nav button labels to show their queued
        counts. Called whenever either queue changes."""
        create_btn = self._nav_buttons.get("card_creator")
        if create_btn is not None:
            n = len(self.gap_queue)
            try:
                from ..tools import create_jobs
                ready = create_jobs.ready_count()
            except Exception:
                ready = 0
            base = "AI Create"
            # ●{gaps} for queued LO-gaps, ✓{ready} for generations awaiting review.
            badge = (f"  ●{n}" if n else "") + (f"  ✓{ready}" if ready else "")
            create_btn.setText(base + badge)
            tips = []
            if n:
                tips.append(f"{n} gap{'s' if n != 1 else ''} queued for Create")
            if ready:
                tips.append(f"{ready} generation{'s' if ready != 1 else ''} ready to review")
            create_btn.setToolTip(" · ".join(tips))
        browse_btn = self._nav_buttons.get("browse")
        if browse_btn is not None:
            n = len(self.browse_queue)
            base = "AI Browse"
            browse_btn.setText(f"{base}  ●{n}" if n else base)
            browse_btn.setToolTip(
                f"{n} KG{'s' if n != 1 else ''} queued for Browse" if n else ""
            )

    def show_create_tool(self) -> None:
        """Programmatic switch to the Create tool — used after Browse hands off
        gaps."""
        btn = self._nav_buttons.get("card_creator")
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
        self._show_tool("card_creator")

    def show_browse_tool(self) -> None:
        """Programmatic switch to the Browse tool — used after the KG page
        queues gaps for Browse."""
        btn = self._nav_buttons.get("browse")
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
        self._show_tool("browse")

    def _show_tool(self, key: str) -> None:
        # Pick a panel: tool's get_panel() if enabled, else a placeholder.
        if not tool_enabled(key):
            placeholder_label = dict((k, l) for k, l, _ in TOOLS).get(key, key)
            widget = _disabled_placeholder(placeholder_label)
            self.load_panel(widget)
            return

        cached = self._panel_cache.get(key)
        path = dict((k, p) for k, _l, p in TOOLS).get(key)
        if cached is None and path:
            try:
                module = importlib.import_module(path)
                widget = module.get_panel()
                self._panel_cache[key] = widget
            except Exception as e:
                print(f"[ankisstant] failed to load panel for {key}: {e}")
                widget = _disabled_placeholder(f"{key} (load error)")
                self._panel_cache[key] = widget
        elif path:
            # Already cached — let the tool refresh state on re-show.
            try:
                module = importlib.import_module(path)
                widget = module.get_panel()  # may refresh internally
            except Exception:
                widget = cached
        else:
            widget = cached or _disabled_placeholder(key)
        # Let the tool sync state with the queue (Create) before display.
        # Panels may be wrapped in a QScrollArea — look inside if so.
        from aqt.qt import QScrollArea
        target = widget.widget() if isinstance(widget, QScrollArea) else widget
        if hasattr(target, "refresh_queue_state"):
            try:
                target.refresh_queue_state(self)
            except Exception as e:
                print(f"[ankisstant] {key}.refresh_queue_state failed: {e}")
        self.load_panel(widget)

    def load_panel(self, widget: QWidget) -> None:
        # Avoid re-adding the same widget — QStackedWidget will keep it on
        # subsequent calls. setCurrentWidget is a no-op if already current.
        if self.stack.indexOf(widget) == -1:
            self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)

    def _open_add_kg(self) -> None:
        try:
            from ..tools.knowledge_gaps import open_add_kg_dialog
            open_add_kg_dialog()
            # If the KG panel is loaded, refresh.
            from ..tools import knowledge_gaps as kg_tool
            kg_tool._refresh_open_panel()
        except Exception as e:
            print(f"[ankisstant] open Add KG failed: {e}")

    def _open_settings(self) -> None:
        from .settings import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec():
            # Refresh nav-button enabled states; rebuild cached panels next click.
            for key, _label, _path in TOOLS:
                self._nav_buttons[key].setEnabled(tool_enabled(key))
            self._panel_cache.clear()

    # ── persist geometry on close ─────────────────────────────────────────────

    def closeEvent(self, event):
        try:
            geom = self.frameGeometry()
            set_window_geometry(self.width(), self.height(), geom.x(), geom.y())
        except Exception as e:
            print(f"[ankisstant] persist window geometry failed: {e}")
        super().closeEvent(event)


_current: MainWindow | None = None


def open_main_window() -> None:
    global _current
    if _current is not None:
        try:
            if _current.isVisible():
                _current.raise_()
                _current.activateWindow()
                return
        except RuntimeError:
            _current = None
    _current = MainWindow(mw)
    _current.show()
    _current.raise_()
    _current.activateWindow()
