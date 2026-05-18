# Small Qt helpers shared across the Ankisstant tools.
#  - loading(button, label):  busy-cursor + bold-disabled button while a
#    blocking Claude / network call runs in the foreground.
#  - attach_tag_completer(line_edit, multi=False):  Anki-tag autocomplete on
#    a QLineEdit. multi=True handles a comma-separated tag list.

from __future__ import annotations

from contextlib import contextmanager

from aqt import mw
from aqt.qt import (
    QApplication, QCompleter, QFrame, QHBoxLayout, QLabel, QPushButton, Qt,
    QVBoxLayout, QWidget,
)

from . import log


@contextmanager
def loading(button, label: str):
    """Mark `button` as busy for the duration of the block:
    disabled, bold text prefixed with ⏳, and a wait cursor on the app.
    Cleanly restored even if the block raises or returns early."""
    orig_text = button.text()
    orig_enabled = button.isEnabled()
    orig_font = button.font()
    bold = button.font()
    bold.setBold(True)
    try:
        button.setEnabled(False)
        button.setText(f"⏳ {label}")
        button.setFont(bold)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        yield
    finally:
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass
        try:
            button.setFont(orig_font)
            button.setEnabled(orig_enabled)
            button.setText(orig_text)
        except RuntimeError:
            pass  # button was deleted mid-call


class _MultiTagCompleter(QCompleter):
    """QCompleter variant that completes the token after the last comma,
    leaving prior tokens intact. Used for the comma-separated tag fields."""

    def __init__(self, tags, parent=None):
        super().__init__(tags, parent)
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterMode(Qt.MatchFlag.MatchContains)

    def splitPath(self, path):
        token = path.split(",")[-1].strip()
        return [token]

    def pathFromIndex(self, index):
        completion = super().pathFromIndex(index)
        widget = self.widget()
        if widget is None:
            return completion
        full = widget.text()
        cursor = widget.cursorPosition()
        before = full[:cursor]
        after = full[cursor:]
        last_comma = before.rfind(",")
        if last_comma < 0:
            return completion + after
        gap = "" if before[last_comma + 1:].startswith(" ") else " "
        return before[:last_comma + 1] + gap + completion + after


def attach_tag_completer(line_edit, multi: bool = False) -> None:
    """Attach an Anki-tag autocomplete to a QLineEdit. No-op if mw.col
    isn't ready (e.g. called before profile load)."""
    try:
        if mw is None or mw.col is None:
            return
        tags = sorted(mw.col.tags.all())
        if not tags:
            return
        if multi:
            completer = _MultiTagCompleter(tags, line_edit)
        else:
            completer = QCompleter(tags, line_edit)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        line_edit.setCompleter(completer)
    except Exception as e:
        log.warn(f"tag completer setup failed: {e}")


def provider_configured() -> bool:
    """True if either the Claude Code CLI is detectable OR an API key is set.
    Used by tool panels to decide whether to show a 'Set up Claude' banner."""
    try:
        from .config import load_config
        from . import api as core_api
        cfg = load_config()
        if cfg.get("anthropic_api_key", "").strip():
            return True
        if core_api.detect_cli_path(cfg.get("claude_cli_path", "")):
            return True
        return False
    except Exception as e:
        log.warn(f"provider_configured check failed: {e}")
        return True  # fail-open so we don't block the UI on a config glitch


def make_setup_banner(parent: QWidget | None = None) -> QFrame:
    """Returns a styled 'Set up Claude first →' banner with an 'Open setup'
    button. Caller is responsible for inserting/removing it from a layout."""
    frame = QFrame(parent)
    frame.setObjectName("ankisstantSetupBanner")
    frame.setStyleSheet(
        "QFrame#ankisstantSetupBanner {"
        "  background: rgba(255, 196, 0, 0.18);"
        "  border: 1px solid rgba(255, 196, 0, 0.65);"
        "  border-radius: 6px;"
        "}"
    )
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(12, 10, 12, 10)
    outer.setSpacing(4)
    title = QLabel("<b>⚙ Set up Claude first</b>")
    title.setTextFormat(Qt.TextFormat.RichText)
    title.setStyleSheet("color: palette(text);")
    outer.addWidget(title)

    msg = QLabel(
        "No Claude provider configured yet. Add an Anthropic API key, or install "
        "the Claude Code CLI, to start using this tool."
    )
    msg.setWordWrap(True)
    msg.setStyleSheet("color: palette(text);")
    outer.addWidget(msg)

    row = QHBoxLayout()
    row.addStretch(1)
    btn = QPushButton("Open setup →")
    btn.setAutoDefault(False)

    def _open():
        try:
            from ..ui.welcome import open_welcome
            open_welcome()
        except Exception as e:
            log.error(f"failed to open welcome: {e}")

    btn.clicked.connect(_open)
    row.addWidget(btn)
    outer.addLayout(row)
    return frame


def make_help_button(title: str, body_html: str, parent: QWidget | None = None) -> QPushButton:
    """Small '?' button. Clicking opens a modal with the given help text.
    `body_html` is rendered as rich text inside the dialog."""
    btn = QPushButton("?", parent)
    btn.setFixedWidth(28)
    btn.setToolTip("How this tool works")
    btn.setAutoDefault(False)

    def _show():
        try:
            from aqt.qt import QDialog, QDialogButtonBox, QTextBrowser
            dlg = QDialog(parent)
            dlg.setWindowTitle(title)
            dlg.resize(560, 460)
            v = QVBoxLayout(dlg)
            tb = QTextBrowser()
            tb.setOpenExternalLinks(True)
            tb.setHtml(body_html)
            v.addWidget(tb)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject)
            bb.accepted.connect(dlg.accept)
            v.addWidget(bb)
            dlg.exec()
        except Exception as e:
            log.error(f"help dialog failed: {e}")

    btn.clicked.connect(_show)
    return btn
