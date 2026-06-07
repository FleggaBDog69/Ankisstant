# Small Qt helpers shared across the Ankisstant tools.
#  - loading(button, label):  busy-cursor + bold-disabled button while a
#    blocking Claude / network call runs in the foreground.
#  - attach_tag_completer(line_edit, multi=False):  Anki-tag autocomplete on
#    a QLineEdit. multi=True handles a comma-separated tag list.

from __future__ import annotations

import threading
from contextlib import contextmanager

from aqt import mw
from aqt.qt import (
    QApplication, QCompleter, QDialog, QDialogButtonBox, QEventLoop, QFrame,
    QHBoxLayout, QLabel, QPlainTextEdit, QProgressDialog, QPushButton, Qt,
    QVBoxLayout, QWidget,
)
from aqt.utils import tooltip

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


def _run_claude(button, label: str, work):
    """Run a Claude call (`work`, a zero-arg callable) on a background thread
    while showing a modal, cancellable progress dialog. Returns work()'s value,
    or None if the user cancelled or the call failed.

    `work` MUST NOT touch Qt or the collection — it runs off the main thread.
    Pass `show_errors=False` to the underlying ask_claude so no error UI is
    raised off-thread; this helper surfaces failures on the main thread."""
    from . import api as core_api
    if not provider_configured():
        tooltip("Set up an AI provider in Ankisstant Settings first.", period=4000)
        return None

    cancel_ev = threading.Event()
    parent = None
    try:
        parent = button.window() if button is not None else mw
    except Exception:
        parent = mw

    dlg = QProgressDialog(label, "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Ankisstant")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)

    state: dict = {"value": None, "error": None}
    loop = QEventLoop()

    def _bg():
        core_api.set_cancel_event(cancel_ev)
        try:
            return work()
        finally:
            core_api.set_cancel_event(None)

    def _done(fut):
        try:
            state["value"] = fut.result()
        except Exception as e:  # pragma: no cover - defensive
            state["error"] = e
        if loop.isRunning():
            loop.quit()

    def _on_cancel():
        cancel_ev.set()
        try:
            dlg.setLabelText("Cancelling…")
        except RuntimeError:
            pass

    dlg.canceled.connect(_on_cancel)
    if button is not None:
        try:
            button.setEnabled(False)
        except RuntimeError:
            pass

    mw.taskman.run_in_background(_bg, _done)
    dlg.show()
    loop.exec()

    try:
        dlg.close()
    except Exception:
        pass
    if button is not None:
        try:
            button.setEnabled(True)
        except RuntimeError:
            pass

    if state["error"] is not None:
        log.error(f"Claude call failed: {state['error']}")
        tooltip("Claude call failed — see console for details.", period=4000)
        return None
    if state["value"] is None and not cancel_ev.is_set():
        # ask_claude already logged the cause (show_errors=False).
        tooltip("Claude call failed — see console for details.", period=4000)
    return state["value"]


def is_manual_provider() -> bool:
    """True when the user picked the 'manual' provider — no in-app AI; they run
    the prompt in their own external LLM and paste the reply back."""
    try:
        from .config import load_config
        return (load_config().get("provider") or "auto").lower() == "manual"
    except Exception:
        return False


def manual_ai_complete(prompt: str, system: str = "",
                       attachments=None, parent=None, parse=None):
    """The 'manual' provider's stand-in for a model call. Shows the fully
    assembled prompt for the user to copy into ChatGPT / Gemini / Claude.ai /
    any LLM, then collects their pasted reply. Must run on the main (UI) thread.

    If `parse` is given it's called with the pasted text on OK; a raised
    exception is shown inline and the dialog stays open so the user can fix the
    paste. Returns parse(text) when `parse` is set, else the raw text — or None
    if cancelled."""
    combined = ((f"[SYSTEM INSTRUCTIONS]\n{system.strip()}\n\n" if system and system.strip() else "")
                + f"[YOUR TASK]\n{prompt.strip()}")
    # Task-only payload for users who run the prebuilt Ankisstant Custom GPT / Gem:
    # the instructions already live in the GPT, so they paste just the task.
    task_only = prompt.strip()

    from .config import load_config as _load_cfg, DEFAULTS as _DEF
    _mcfg = _load_cfg()
    # `or _DEF[...]` so a stale empty saved value doesn't shadow the shipped link.
    gpt_url = (_mcfg.get("manual_gpt_url") or _DEF.get("manual_gpt_url") or "").strip()
    gem_url = (_mcfg.get("manual_gem_url") or _DEF.get("manual_gem_url") or "").strip()
    has_assistant = bool(gpt_url or gem_url)

    dlg = QDialog(parent or mw)
    dlg.setWindowTitle("Run this in your AI")
    dlg.setMinimumSize(640, 600)
    v = QVBoxLayout(dlg)

    if has_assistant:
        intro = QLabel(
            "<b>Easiest:</b> open the <b>Ankisstant</b> assistant below — it already "
            "knows the instructions, so just <b>Copy task only</b>, paste it in, and "
            "paste the reply back here.<br>"
            "<small>Or use any AI with the full prompt (Copy prompt).</small>"
        )
    else:
        intro = QLabel(
            "<b>Step 1.</b> Copy the prompt below and paste it into any AI "
            "(ChatGPT, Gemini, Claude.ai — the free versions all work).<br>"
            "<b>Step 2.</b> Paste the AI's full reply into the bottom box and click OK."
        )
    intro.setTextFormat(Qt.TextFormat.RichText)
    intro.setWordWrap(True)
    v.addWidget(intro)

    prompt_box = QPlainTextEdit()
    prompt_box.setPlainText(combined)
    prompt_box.setReadOnly(True)
    prompt_box.setMinimumHeight(200)
    v.addWidget(prompt_box, 1)

    paths = [p for p in (attachments or []) if isinstance(p, str)]
    if paths:
        import os
        names = ", ".join(os.path.basename(p) for p in paths)
        att = QLabel(
            "⚠ This task references attached file(s): <b>" + names + "</b>. "
            "Upload them to your AI alongside the prompt (the free web chats "
            "let you attach PDFs / images)."
        )
        att.setTextFormat(Qt.TextFormat.RichText)
        att.setWordWrap(True)
        att.setStyleSheet("color: #b85c00;")
        v.addWidget(att)

    copy_row = QHBoxLayout()
    copy_btn = QPushButton("📋 Copy prompt")
    copy_btn.setAutoDefault(False)

    def _copy():
        QApplication.clipboard().setText(combined)
        tooltip("Prompt copied — paste it into your AI.", period=2500)

    copy_btn.clicked.connect(_copy)
    copy_row.addWidget(copy_btn)

    if has_assistant:
        from aqt.utils import openLink

        task_btn = QPushButton("📋 Copy task only")
        task_btn.setAutoDefault(False)

        def _copy_task():
            QApplication.clipboard().setText(task_only)
            tooltip("Task copied — paste it into the Ankisstant assistant.", period=2500)

        task_btn.clicked.connect(_copy_task)
        copy_row.addWidget(task_btn)

        if gpt_url:
            gpt_btn = QPushButton("↗ Open Ankisstant GPT")
            gpt_btn.setAutoDefault(False)
            gpt_btn.clicked.connect(lambda: openLink(gpt_url))
            copy_row.addWidget(gpt_btn)
        if gem_url:
            gem_btn = QPushButton("↗ Open Gemini Gem")
            gem_btn.setAutoDefault(False)
            gem_btn.clicked.connect(lambda: openLink(gem_url))
            copy_row.addWidget(gem_btn)

    copy_row.addStretch(1)
    v.addLayout(copy_row)

    v.addWidget(QLabel("Paste the AI's reply here:"))
    reply_box = QPlainTextEdit()
    reply_box.setPlaceholderText("Paste the AI's full response…")
    reply_box.setMinimumHeight(160)
    v.addWidget(reply_box, 1)

    err = QLabel("")
    err.setStyleSheet("color: #c0392b;")
    err.setWordWrap(True)
    err.setVisible(False)
    v.addWidget(err)

    bb = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
    ok_btn.setText("OK — use this reply")
    result: dict = {"value": None}

    def _accept():
        text = reply_box.toPlainText().strip()
        if not text:
            err.setText("Paste the AI's reply first.")
            err.setVisible(True)
            return
        if parse is not None:
            try:
                result["value"] = parse(text)
            except Exception as e:
                err.setText(
                    f"That reply isn't complete — {e}. Paste the AI's <b>full</b> "
                    "response (including the JSON), then click OK again."
                )
                err.setTextFormat(Qt.TextFormat.RichText)
                err.setVisible(True)
                return
        else:
            result["value"] = text
        dlg.accept()

    bb.accepted.connect(_accept)
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return result["value"]


def run_claude_json(button, label: str, **kwargs):
    """Cancellable ask_claude_json. kwargs are forwarded to ask_claude_json
    (prompt, system, max_tokens, model, ...). Errors are shown on the main
    thread; cancel returns None quietly."""
    from . import api as core_api
    # Manual provider is interactive: run it on the main thread (no background
    # worker / progress dialog) so the copy/paste dialog can show.
    if is_manual_provider():
        return core_api.ask_claude_json(**kwargs)
    return _run_claude(
        button, label,
        lambda: core_api.ask_claude_json(show_errors=False, **kwargs),
    )


def run_claude_text(button, label: str, **kwargs):
    """Cancellable ask_claude (text reply)."""
    from . import api as core_api
    if is_manual_provider():
        return core_api.ask_claude(**kwargs)
    return _run_claude(
        button, label,
        lambda: core_api.ask_claude(show_errors=False, **kwargs),
    )


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
    """True if the selected AI provider is usable — the Claude Code CLI is
    detectable, or the relevant API key is set. Used by tool panels to decide
    whether to show a 'set up a provider' banner."""
    try:
        from .config import load_config
        from . import api as core_api
        cfg = load_config()
        provider = (cfg.get("provider") or "auto").lower()
        if provider in ("auto", "cli") and core_api.detect_cli_path(cfg.get("claude_cli_path", "")):
            return True
        if provider in ("auto", "anthropic") and cfg.get("anthropic_api_key", "").strip():
            return True
        if provider == "gemini" and cfg.get("gemini_api_key", "").strip():
            return True
        if provider == "openai" and cfg.get("openai_api_key", "").strip():
            return True
        if provider == "ollama":
            return True  # keyless local server — reachability checked at call time
        if provider == "manual":
            return True  # no provider needed — user pastes from their own AI
        return False
    except Exception as e:
        log.warn(f"provider_configured check failed: {e}")
        return True  # fail-open so we don't block the UI on a config glitch


def set_ai_buttons_enabled(buttons, enabled: bool) -> None:
    """Enable/disable a list of AI-action buttons together. When disabled
    (no Claude provider configured), each shows a setup-pointing tooltip so a
    click attempt is self-explaining. Tolerant of None / deleted widgets."""
    tip = "" if enabled else "Set up an AI provider in Ankisstant Settings first."
    for b in buttons:
        if b is None:
            continue
        try:
            b.setEnabled(enabled)
            b.setToolTip(tip)
        except RuntimeError:
            pass  # widget deleted


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
        "No AI provider configured yet. Add an API key or install "
        "the Claude Code CLI to start using this tool."
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
