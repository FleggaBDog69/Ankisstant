# Embedded browser for QBank platforms.
# Each platform gets its own persistent QWebEngineProfile (separate cookie jar)
# so sessions survive Anki restarts independently.

from __future__ import annotations

import os

from aqt import mw
from aqt.qt import (
    QComboBox, QDesktopServices, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QKeySequence, QLineEdit, QPushButton, QShortcut, QSpinBox, QSplitter, Qt,
    QTimer, QUrl, QVBoxLayout,
)

try:
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    from PyQt5.QtWebEngineCore import QWebEngineProfile
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage

from ...core.config import tool_config, save_tool_config
from ...core.qt_utils import theme_dialog
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


_popups: list = []   # keep popup windows alive (WA_DeleteOnClose frees them on close)


class _NewTabPage(QWebEnginePage):
    """Page that routes target=_blank / window.open() links to a new Anki window.

    Anki's embedded webview silently blocks new-tab navigation. Some QBank
    features (e.g. Apex Anesthesia mock exams) rely on opening a new tab, so we
    catch those requests in createWindow and host them in a fresh in-Anki
    BrowserWindow — same login/cookies and the same capture toolbar as the
    original. If no owner window is known we fall back to the system browser.
    """

    def __init__(self, profile, parent, owner=None):
        super().__init__(profile, parent)
        self._owner = owner

    def createWindow(self, _type):
        owner = self._owner
        if owner is None:
            # No QBank window to attach to — hand off to the default browser.
            stub = QWebEnginePage(self.profile(), self)

            def _open_external(url: QUrl):
                if url.isValid() and not url.isEmpty():
                    QDesktopServices.openUrl(url)
                    stub.deleteLater()

            stub.urlChanged.connect(_open_external)
            return stub

        # Build a new in-Anki QBank window and let Qt drive its page to the
        # new-tab URL. Returning the page is what tells Qt where to load it.
        new_page = _NewTabPage(self.profile(), None)
        win = BrowserWindow(
            owner._platform_key, owner._platform_name, None, mw,
            initial_page=new_page, is_popup=True,
        )
        new_page._owner = win  # further new-tabs from this window stay in Anki
        _popups.append(win)
        win.show()
        win.raise_()
        win.activateWindow()
        return new_page


def _make_page(profile, parent, owner=None):
    """Build a page that opens new-tab links in a fresh Anki QBank window."""
    return _NewTabPage(profile, parent, owner=owner)


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
        theme_dialog(self)   # chrome only — never the hosted page

    def values(self):
        return self.correct_spin.value(), self.incorrect_spin.value()


AI_OPTIONS = [
    ("Claude",       "https://claude.ai/chat/"),
    ("ChatGPT",      "https://chatgpt.com/"),
    ("OpenEvidence", "https://www.openevidence.com/"),
    ("Heidi",        "https://scribe.heidihealth.com/en/scribe/chat"),
]


class BrowserWindow(QDialog):
    def __init__(self, platform_key: str, platform_name: str, url, parent=None,
                 initial_page=None, is_popup: bool = False,
                 track_session: bool = True, show_nav: bool = False):
        super().__init__(parent)
        self._platform_key = platform_key
        self._platform_name = platform_name
        self._is_popup = is_popup
        self._track_session = track_session
        self._address = None
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
        capture_btn.setAutoDefault(False)
        capture_btn.clicked.connect(self._open_capture)
        toolbar.addWidget(capture_btn)

        review_btn = QPushButton("Review queue")
        review_btn.setStyleSheet("font-size: 12px; padding: 4px 10px;")
        review_btn.setAutoDefault(False)
        review_btn.clicked.connect(self._open_review)
        toolbar.addWidget(review_btn)

        # ── Navigation (general browser only): back / forward / reload / URL ──
        if show_nav:
            back_btn = QPushButton("◀")
            back_btn.setStyleSheet("font-size: 12px; padding: 4px 8px;")
            back_btn.setToolTip("Back")
            back_btn.setAutoDefault(False)
            back_btn.clicked.connect(lambda: self.view.back())
            toolbar.addWidget(back_btn)

            fwd_btn = QPushButton("▶")
            fwd_btn.setStyleSheet("font-size: 12px; padding: 4px 8px;")
            fwd_btn.setToolTip("Forward")
            fwd_btn.setAutoDefault(False)
            fwd_btn.clicked.connect(lambda: self.view.forward())
            toolbar.addWidget(fwd_btn)

            reload_btn = QPushButton("⟳")
            reload_btn.setStyleSheet("font-size: 12px; padding: 4px 8px;")
            reload_btn.setToolTip("Reload")
            reload_btn.setAutoDefault(False)
            reload_btn.clicked.connect(lambda: self.view.reload())
            toolbar.addWidget(reload_btn)

            self._address = QLineEdit()
            self._address.setStyleSheet("font-size: 12px; padding: 4px 8px;")
            self._address.setPlaceholderText("Type a URL and press Enter…")
            self._address.setClearButtonEnabled(True)
            self._address.returnPressed.connect(self._navigate_to_address)
            toolbar.addWidget(self._address, stretch=1)
        else:
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
        self._ai_toggle.setAutoDefault(False)
        self._ai_toggle.clicked.connect(self._toggle_ai)
        toolbar.addWidget(self._ai_toggle)

        root.addLayout(toolbar)

        # ── Splitter: QBank view + (lazy) AI view ────────────────────────────
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.view = QWebEngineView(self)
        if initial_page is not None:
            # A new-tab popup: adopt the page Qt handed us and let it load the URL.
            page = initial_page
            page.setParent(self.view)
            self.view.setPage(page)
        else:
            page = _make_page(_get_profile(platform_key), self.view, owner=self)
            self.view.setPage(page)
            if url:
                self.view.setUrl(QUrl(url))
        if self._address is not None:
            self.view.urlChanged.connect(self._on_url_changed)
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
        theme_dialog(self)   # chrome only — never the hosted page

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
        page = _make_page(_get_profile(f"ai_{name.lower()}"), self._ai_view)
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
            page = _make_page(_get_profile(f"ai_{name.lower()}"), self._ai_view)
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

    def _on_url_changed(self, url: QUrl):
        # Keep the address bar in sync, unless the user is mid-edit.
        if not self._address.hasFocus():
            self._address.setText(url.toString())

    def _navigate_to_address(self):
        text = self._address.text().strip()
        if not text:
            return
        # Treat input with no scheme as a bare domain (e.g. "uworld.com").
        if "://" not in text:
            text = "https://" + text
        self.view.setUrl(QUrl(text))
        self.view.setFocus()

    def _open_capture(self):
        from .capture_dialog import open_capture
        open_capture(self._platform_key, self._platform_name)

    def _open_review(self):
        from .panel import _open_kg_page_for_qbank
        _open_kg_page_for_qbank()

    def closeEvent(self, event):
        if self._is_popup:
            # New-tab popups aren't a graded study session; just clean up.
            try:
                _popups.remove(self)
            except ValueError:
                pass
            event.accept()
            return
        if not self._track_session:
            # General browser — not a graded QBank session.
            _windows.pop(self._platform_key, None)
            event.accept()
            return
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


def active_platform() -> tuple[str, str]:
    """(platform_key, platform_name) of the QBank browser the user is looking at,
    so a global capture trigger doesn't have to be told which QBank is open.
    Prefers the active/focused window, then the single open one; ('', '') if none."""
    wins = [w for w in _windows.values() if _win_visible(w)]
    if not wins:
        return "", ""
    if len(wins) > 1:
        try:
            active = mw.app.activeWindow()
        except Exception:
            active = None
        for w in wins:
            try:
                if w is active or w.isActiveWindow():
                    return w._platform_key, w._platform_name
            except Exception:
                pass
    w = wins[0]
    return w._platform_key, w._platform_name


def _win_visible(w) -> bool:
    try:
        return w.isVisible()
    except Exception:
        return False


def open_platform(platform_key: str, platform_name: str, url: str) -> None:
    # Remember the QBank the user last opened so a capture triggered when no
    # embedded window is live (e.g. they're reviewing in their own browser) can
    # still fill in the source instead of leaving it blank — see open_capture.
    try:
        cfg = tool_config("qbank")
        if cfg.get("last_platform_key") != platform_key:
            cfg["last_platform_key"] = platform_key
            cfg["last_platform_name"] = platform_name
            save_tool_config("qbank", cfg)
    except Exception as e:
        print(f"[ankisstant] persist last_platform failed: {e}")

    win = _windows.get(platform_key)
    if win is not None and win.isVisible():
        win.raise_()
        win.activateWindow()
        return
    win = BrowserWindow(platform_key, platform_name, url, mw)
    _windows[platform_key] = win
    win.show()


def open_browser() -> None:
    """Open a general-purpose web browser window with the capture toolbar.

    Unlike a configured QBank, this starts blank so the user can navigate
    anywhere via the address bar. Its own profile keeps logins separate, and it
    doesn't prompt for a question count on close (it isn't a graded session).
    """
    key = "open_browser"
    win = _windows.get(key)
    if win is not None and win.isVisible():
        win.raise_()
        win.activateWindow()
        return
    win = BrowserWindow(key, "Browser", "", mw, show_nav=True)
    _windows[key] = win
    win.show()
