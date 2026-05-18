# Single-page first-run welcome modal.
#
# Triggered from __init__.py when cfg['first_run_seen'] is False. Also reachable
# from Settings → About → "Re-run welcome wizard".
#
# Collects: provider (CLI auto-detect / API key), default deck, default notetype,
# audit-tag prefix. Tests the chosen provider with a tiny Claude call.

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QRadioButton, Qt, QVBoxLayout, QWidget,
)
from aqt.utils import showWarning, tooltip

from ..core import api as core_api
from ..core.config import load_config, save_config
from ..core import log


_DEFAULT_AUDIT_PREFIX = "Ankisstant::AI"


class WelcomeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Welcome to Ankisstant")
        self.setMinimumWidth(680)
        self.cfg = load_config()
        self._build()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        title = QLabel("<h2 style='margin:0'>Welcome to Ankisstant</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(title)

        intro = QLabel(
            "Ankisstant adds four Claude-powered tools to Anki:"
            "<ul style='margin-top:4px; margin-bottom:4px;'>"
            "<li><b>QBank with Claude</b> — log missed QBank questions and "
            "generate cards from them.</li>"
            "<li><b>Browse with Claude</b> — natural-language search across "
            "your collection.</li>"
            "<li><b>Analyse Knowledge Gaps</b> — audit a tag against a learning "
            "objective.</li>"
            "<li><b>Create with Claude</b> — generate cards from text, URLs, "
            "PDFs, or PowerPoints.</li>"
            "</ul>"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        intro.setMinimumHeight(140)
        root.addWidget(intro)

        root.addWidget(_hsep())

        # ── Claude provider ──────────────────────────────────────────────────
        prov_label = QLabel("<b>1. Claude provider</b>")
        prov_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(prov_label)

        prov_hint = QLabel(
            "<small>Use the Claude Code CLI if you have a Claude subscription "
            "(no per-call billing). Or paste an Anthropic API key to pay-per-use. "
            "PDF / PPTX work on both paths.</small>"
        )
        prov_hint.setTextFormat(Qt.TextFormat.RichText)
        prov_hint.setStyleSheet("color: gray;")
        prov_hint.setWordWrap(True)
        root.addWidget(prov_hint)

        prov_grp = QButtonGroup(self)
        self.rb_cli = QRadioButton("Claude Code CLI")
        self.rb_api = QRadioButton("Anthropic API key")
        prov_grp.addButton(self.rb_cli)
        prov_grp.addButton(self.rb_api)
        root.addWidget(self.rb_cli)

        # CLI row
        cli_row = QHBoxLayout()
        cli_row.addSpacing(22)
        self.cli_status = QLabel("Not detected.")
        self.cli_status.setStyleSheet("color: gray; font-size: 11px;")
        self.detect_btn = QPushButton("Detect")
        self.detect_btn.setAutoDefault(False)
        self.detect_btn.clicked.connect(self._on_detect_cli)
        cli_row.addWidget(self.cli_status, 1)
        cli_row.addWidget(self.detect_btn)
        root.addLayout(cli_row)

        root.addWidget(self.rb_api)
        api_row = QHBoxLayout()
        api_row.addSpacing(22)
        self.api_input = QLineEdit(self.cfg.get("anthropic_api_key", ""))
        self.api_input.setPlaceholderText("sk-ant-…")
        self.api_input.setEchoMode(QLineEdit.EchoMode.Password)
        api_row.addWidget(self.api_input, 1)
        root.addLayout(api_row)

        # Test connection
        test_row = QHBoxLayout()
        test_row.addStretch(1)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setAutoDefault(False)
        self.test_btn.clicked.connect(self._on_test)
        self.test_status = QLabel("")
        self.test_status.setStyleSheet("color: gray;")
        test_row.addWidget(self.test_status)
        test_row.addWidget(self.test_btn)
        root.addLayout(test_row)

        # Auto-detect on open.
        self._on_detect_cli(silent=True)

        # Pick the radio that already has data; fall back to whichever has more
        # signal (detected CLI → CLI; API key set → API; neither → CLI selected).
        mode = (self.cfg.get("provider_mode") or "auto").lower()
        if mode == "api" or (self.cfg.get("anthropic_api_key") and not self._cli_found):
            self.rb_api.setChecked(True)
        else:
            self.rb_cli.setChecked(True)

        root.addWidget(_hsep())

        # ── Deck / notetype / tag prefix ────────────────────────────────────
        defaults_label = QLabel("<b>2. Defaults (you can change these later)</b>")
        defaults_label.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(defaults_label)

        # Deck
        deck_row = QHBoxLayout()
        deck_row.addWidget(QLabel("Default deck:"))
        self.deck = QComboBox()
        self.deck.setEditable(True)
        self._populate_decks()
        deck_row.addWidget(self.deck, 1)
        root.addLayout(deck_row)

        # Notetype
        nt_row = QHBoxLayout()
        nt_row.addWidget(QLabel("Default notetype:"))
        self.notetype = QComboBox()
        self.notetype.setEditable(False)
        self._populate_notetypes()
        nt_row.addWidget(self.notetype, 1)
        root.addLayout(nt_row)

        self.nt_warn = QLabel("")
        self.nt_warn.setStyleSheet("color: #b85c00; font-size: 11px;")
        self.nt_warn.setWordWrap(True)
        root.addWidget(self.nt_warn)
        self.notetype.currentTextChanged.connect(self._on_notetype_changed)
        self._on_notetype_changed(self.notetype.currentText())

        # Audit tag prefix
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Audit tag prefix:"))
        self.prefix = QLineEdit(_DEFAULT_AUDIT_PREFIX)
        self.prefix.setPlaceholderText(_DEFAULT_AUDIT_PREFIX)
        prefix_row.addWidget(self.prefix, 1)
        root.addLayout(prefix_row)

        prefix_hint = QLabel(
            "<small>Every card created by Ankisstant gets a tag under this prefix "
            "(e.g. <code>Ankisstant::AI::Creator</code>) so you can audit which "
            "cards came from AI.</small>"
        )
        prefix_hint.setTextFormat(Qt.TextFormat.RichText)
        prefix_hint.setStyleSheet("color: gray;")
        prefix_hint.setWordWrap(True)
        root.addWidget(prefix_hint)

        root.addStretch(1)

        # ── Buttons ──────────────────────────────────────────────────────────
        bb = QDialogButtonBox()
        self.skip_btn = bb.addButton("Skip for now", QDialogButtonBox.ButtonRole.RejectRole)
        self.save_btn = bb.addButton("Save & open Ankisstant", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_btn.setDefault(True)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self._on_skip)
        root.addWidget(bb)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _populate_decks(self):
        self.deck.clear()
        if mw.col is None:
            return
        try:
            names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
        except Exception as e:
            log.warn(f"welcome: failed to list decks: {e}")
            return
        self.deck.addItems(names)
        current = self.cfg.get("tools", {}).get("card_creator", {}).get("default_deck", "")
        if current:
            idx = self.deck.findText(current)
            if idx >= 0:
                self.deck.setCurrentIndex(idx)
            else:
                self.deck.setEditText(current)
        elif names:
            # Prefer "Default" if present, else first.
            for guess in ("Default", "default"):
                idx = self.deck.findText(guess)
                if idx >= 0:
                    self.deck.setCurrentIndex(idx)
                    return

    def _populate_notetypes(self):
        self.notetype.clear()
        self._notetype_names: list[str] = []
        if mw.col is None:
            return
        try:
            self._notetype_names = sorted(nt["name"] for nt in mw.col.models.all())
        except Exception as e:
            log.warn(f"welcome: failed to list notetypes: {e}")
            return
        self.notetype.addItems(self._notetype_names)
        current = self.cfg.get("tools", {}).get("card_creator", {}).get("default_notetype", "")
        if current:
            idx = self.notetype.findText(current)
            if idx >= 0:
                self.notetype.setCurrentIndex(idx)
                return
        # Sensible default: first cloze-capable notetype, else first.
        for name in self._notetype_names:
            if self._notetype_has_cloze(name):
                idx = self.notetype.findText(name)
                if idx >= 0:
                    self.notetype.setCurrentIndex(idx)
                    return

    def _notetype_has_cloze(self, name: str) -> bool:
        if mw.col is None:
            return False
        try:
            nt = mw.col.models.by_name(name)
        except Exception:
            return False
        if nt is None:
            return False
        # In Anki, model type 1 = cloze.
        return int(nt.get("type", 0)) == 1

    def _on_notetype_changed(self, name: str):
        if not name:
            self.nt_warn.setText("")
            return
        if self._notetype_has_cloze(name):
            self.nt_warn.setText("")
        else:
            self.nt_warn.setText(
                f"⚠ '{name}' isn't a cloze notetype — Create with Claude expects "
                "cloze cards. Pick a cloze notetype, or change it later in Settings."
            )

    # ── provider detection / test ────────────────────────────────────────────

    def _on_detect_cli(self, *_args, silent: bool = False):
        path = core_api.detect_cli_path(self.cfg.get("claude_cli_path", ""))
        self._cli_found = bool(path)
        if path:
            self.cli_status.setText(f"✓ Found: {path}")
            self.cli_status.setStyleSheet("color: #1c8000; font-size: 11px;")
        else:
            self.cli_status.setText(
                "Not detected. Install: https://docs.claude.com/claude-code"
            )
            self.cli_status.setStyleSheet("color: gray; font-size: 11px;")
            if not silent:
                tooltip("No Claude Code CLI on PATH or known fallback locations.")

    def _on_test(self):
        # Save the chosen provider to the in-memory cfg before testing, so
        # ask_claude() sees the right mode + key during this single call.
        if self.rb_cli.isChecked():
            self.cfg["provider_mode"] = "cli"
        else:
            self.cfg["provider_mode"] = "api"
            self.cfg["anthropic_api_key"] = self.api_input.text().strip()

        save_config(self.cfg)  # ask_claude reads from disk

        self.test_status.setText("Testing…")
        self.test_status.setStyleSheet("color: gray;")
        self.test_btn.setEnabled(False)
        try:
            from aqt.qt import QApplication
            QApplication.processEvents()
            reply = core_api.ask_claude(
                prompt="Reply with the single word: ok",
                system="You answer in one word, lowercase.",
                max_tokens=10,
                show_errors=False,
            )
        finally:
            self.test_btn.setEnabled(True)
        if reply and reply.strip().lower().startswith("ok"):
            self.test_status.setText("✓ Connection OK")
            self.test_status.setStyleSheet("color: #1c8000;")
        elif reply:
            self.test_status.setText(f"⚠ Unexpected reply: {reply.strip()[:40]}")
            self.test_status.setStyleSheet("color: #b85c00;")
        else:
            self.test_status.setText("✗ No reply — check setup")
            self.test_status.setStyleSheet("color: #c0392b;")

    # ── save / skip ──────────────────────────────────────────────────────────

    def _on_save(self):
        # Provider
        if self.rb_cli.isChecked():
            self.cfg["provider_mode"] = "cli"
        else:
            key = self.api_input.text().strip()
            if not key:
                showWarning("Paste an API key, or switch to CLI mode.")
                return
            self.cfg["provider_mode"] = "api"
            self.cfg["anthropic_api_key"] = key

        # Tools defaults
        deck = self.deck.currentText().strip()
        notetype = self.notetype.currentText().strip()
        prefix = self.prefix.text().strip() or _DEFAULT_AUDIT_PREFIX

        tools = self.cfg.setdefault("tools", {})
        cc = tools.setdefault("card_creator", {})
        cc["default_deck"] = deck
        cc["default_notetype"] = notetype
        cc["audit_tag"] = f"{prefix}::Creator"

        qb = tools.setdefault("qbank", {})
        qb["card_deck"] = qb.get("card_deck") or deck
        qb["card_notetype"] = qb.get("card_notetype") or notetype

        br = tools.setdefault("browse", {})
        br["audit_tag"] = f"{prefix}::Browse"

        self.cfg["first_run_seen"] = True
        save_config(self.cfg)
        tooltip("Setup saved — opening Ankisstant…")
        self.accept()
        # Open the main window so the mate sees the tools immediately.
        try:
            from .main_window import open_main_window
            open_main_window()
        except Exception as e:
            log.warn(f"welcome: failed to open main window: {e}")

    def _on_skip(self):
        # Still mark first_run_seen so we don't reopen on every profile load.
        # The mate can re-run from Settings → About.
        self.cfg["first_run_seen"] = True
        save_config(self.cfg)
        self.reject()


def _hsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def open_welcome() -> None:
    """Public entry — used by __init__.py on first run and by Settings → About."""
    dlg = WelcomeDialog()
    dlg.exec()
