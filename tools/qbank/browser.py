# Embedded browser for QBank platforms.
# Each platform gets its own persistent QWebEngineProfile (separate cookie jar)
# so sessions survive Anki restarts independently.

from __future__ import annotations

import os

from aqt import mw
from aqt.qt import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QKeySequence, QPushButton, QShortcut, QSpinBox, QSplitter, Qt, QTimer,
    QUrl, QVBoxLayout,
)

try:
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    from PyQt5.QtWebEngineCore import QWebEngineProfile
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage

from ...core.config import tool_config, save_tool_config
from .stats import add_session


_profiles: dict = {}   # platform_key → QWebEngineProfile (cached for the session)
_windows:  dict = {}   # platform_key → BrowserWindow


def _profile_dir(platform_key: str) -> str:
    """Cookie/cache dir under ankisstant/user_files/profile_<key>."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    path = os.path.join(root, "user_files", f"profile_{platform_key}")
    os.makedirs(path, exist_ok=True)
    return path


def _get_profile(platform_key: str) -> "QWebEngineProfile":
    if platform_key in _profiles:
        return _profiles[platform_key]
    storage_dir = _profile_dir(platform_key)
    profile = QWebEngineProfile(platform_key, mw)
    profile.setPersistentStoragePath(storage_dir)
    profile.setCachePath(os.path.join(storage_dir, "cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
    )
    profile.setHttpUserAgent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    _profiles[platform_key] = profile
    return profile


class _QuestionCountDialog(QDialog):
    def __init__(self, platform_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{platform_name} — Log session")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)

        label = QLabel("How did this session go?")
        label.setStyleSheet("font-weight: bold; margin-bottom: 4px;")
        layout.addWidget(label)

        hint = QLabel(f"Enter the number of questions you answered in {platform_name}.")
        hint.setStyleSheet("color: gray; font-size: 11px; margin-bottom: 8px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        correct_row = QHBoxLayout()
        correct_row.addWidget(QLabel("Correct:"))
        self.correct_spin = QSpinBox()
        self.correct_spin.setRange(0, 9999)
        correct_row.addWidget(self.correct_spin)
        layout.addLayout(correct_row)

        incorrect_row = QHBoxLayout()
        incorrect_row.addWidget(QLabel("Incorrect:"))
        self.incorrect_spin = QSpinBox()
        self.incorrect_spin.setRange(0, 9999)
        incorrect_row.addWidget(self.incorrect_spin)
        layout.addLayout(incorrect_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Skip")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return self.correct_spin.value(), self.incorrect_spin.value()


AI_OPTIONS = [
    ("Claude",       "https://claude.ai/chat/"),
    ("ChatGPT",      "https://chatgpt.com/"),
    ("OpenEvidence", "https://www.openevidence.com/"),
    ("Heidi",        "https://scribe.heidihealth.com/en/scribe/chat"),
]


class BrowserWindow(QDialog):
    def __init__(self, platform_key: str, platform_name: str, url: str, parent=None):
        super().__init__(parent)
        self._platform_key = platform_key
        self._platform_name = platform_name
        self.setWindowTitle(platform_name)
        self.resize(1400, 850)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        cfg = tool_config("qbank")
        last_ai = cfg.get("ai_last_selected", "Claude")
        ai_visible_start = bool(cfg.get("ai_panel_visible", False))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 6, 8, 4)
        toolbar.setSpacing(6)

        capture_btn = QPushButton("📌 Capture missed Q")
        capture_btn.setStyleSheet("font-size: 12px; padding: 4px 10px;")
        capture_btn.setToolTip("Save the current question to review later (⌘⇧K)")
        capture_btn.clicked.connect(self._open_capture)
        toolbar.addWidget(capture_btn)

        review_btn = QPushButton("Review queue")
        review_btn.setStyleSheet("font-size: 12px; padding: 4px 10px;")
        review_btn.clicked.connect(self._open_review)
        toolbar.addWidget(review_btn)

        toolbar.addStretch()

        self._ai_combo = QComboBox()
        self._ai_combo.setStyleSheet("font-size: 12px;")
        for name, _u in AI_OPTIONS:
            self._ai_combo.addItem(name)
        try:
            self._ai_combo.setCurrentText(last_ai)
        except Exception:
            pass
        self._ai_combo.currentIndexChanged.connect(self._on_ai_changed)
        toolbar.addWidget(self._ai_combo)

        self._ai_toggle = QPushButton("AI ◀")
        self._ai_toggle.setCheckable(True)
        self._ai_toggle.setStyleSheet("font-size: 12px; padding: 4px 10px;")
        self._ai_toggle.setToolTip("Show/hide AI sidebar (⌘⇧A)")
        self._ai_toggle.clicked.connect(self._toggle_ai)
        toolbar.addWidget(self._ai_toggle)

        root.addLayout(toolbar)

        # ── Splitter: QBank view + (lazy) AI view ────────────────────────────
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.view = QWebEngineView(self)
        page = QWebEnginePage(_get_profile(platform_key), self.view)
        self.view.setPage(page)
        self.view.setUrl(QUrl(url))
        self._splitter.addWidget(self.view)

        self._ai_view = None
        root.addWidget(self._splitter, stretch=1)

        QShortcut(QKeySequence("Ctrl+Shift+K"), self, self._open_capture)
        QShortcut(QKeySequence("Meta+Shift+K"), self, self._open_capture)
        QShortcut(QKeySequence("Ctrl+Shift+A"), self, lambda: self._ai_toggle.click())
        QShortcut(QKeySequence("Meta+Shift+A"), self, lambda: self._ai_toggle.click())

        self._prompted = False

        if ai_visible_start:
            self._ai_toggle.setChecked(True)
            QTimer.singleShot(0, lambda: self._toggle_ai(True))

    def _current_ai(self):
        idx = self._ai_combo.currentIndex()
        if idx < 0 or idx >= len(AI_OPTIONS):
            idx = 0
        return AI_OPTIONS[idx]  # (name, url)

    def _ensure_ai_view(self):
        if self._ai_view is not None:
            return
        name, url = self._current_ai()
        self._ai_view = QWebEngineView(self)
        page = QWebEnginePage(_get_profile(f"ai_{name.lower()}"), self._ai_view)
        self._ai_view.setPage(page)
        self._ai_view.setUrl(QUrl(url))
        self._splitter.addWidget(self._ai_view)

    def _toggle_ai(self, checked):
        show = bool(checked) if checked is not None else self._ai_toggle.isChecked()
        if show:
            self._ensure_ai_view()
            self._ai_view.show()
            total = max(self._splitter.width(), 800)
            self._splitter.setSizes([int(total * 0.62), int(total * 0.38)])
            self._ai_toggle.setText("AI ▶")
        else:
            if self._ai_view is not None:
                self._ai_view.hide()
            self._ai_toggle.setText("AI ◀")
        self._persist_ai_state()

    def _on_ai_changed(self, _idx):
        name, url = self._current_ai()
        if self._ai_view is not None:
            page = QWebEnginePage(_get_profile(f"ai_{name.lower()}"), self._ai_view)
            self._ai_view.setPage(page)
            self._ai_view.setUrl(QUrl(url))
        self._persist_ai_state()

    def _persist_ai_state(self):
        try:
            cfg = tool_config("qbank")
            cfg["ai_last_selected"] = self._current_ai()[0]
            cfg["ai_panel_visible"] = self._ai_toggle.isChecked()
            save_tool_config("qbank", cfg)
        except Exception as e:
            print(f"[ankisstant] persist ai state failed: {e}")

    def _open_capture(self):
        from .capture_dialog import open_capture
        open_capture(self._platform_key, self._platform_name)

    def _open_review(self):
        from .panel import _open_kg_page_for_qbank
        _open_kg_page_for_qbank()

    def closeEvent(self, event):
        if not self._prompted:
            self._prompted = True
            dialog = _QuestionCountDialog(self._platform_name, self)
            if dialog.exec():
                correct, incorrect = dialog.values()
                add_session(correct, incorrect, self._platform_key)
                try:
                    mw.deckBrowser.refresh()
                except Exception:
                    pass
        _windows.pop(self._platform_key, None)
        event.accept()


def open_platform(platform_key: str, platform_name: str, url: str) -> None:
    win = _windows.get(platform_key)
    if win is not None and win.isVisible():
        win.raise_()
        win.activateWindow()
        return
    win = BrowserWindow(platform_key, platform_name, url, mw)
    _windows[platform_key] = win
    win.show()
