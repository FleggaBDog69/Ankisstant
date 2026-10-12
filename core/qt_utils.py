# Small Qt helpers shared across the Ankisstant tools.
#  - loading(button, label):  busy-cursor + bold-disabled button while a
#    blocking Claude / network call runs in the foreground.
#  - attach_tag_completer(line_edit, multi=False):  Anki-tag autocomplete on
#    a QLineEdit. multi=True handles a comma-separated tag list.
#  - tips_on_checkbox_only(view):  quieten a dense QTreeWidget/QTableWidget so
#    item tooltips only fire over the checkbox, not over every cell.
#  - dock_beside(helper, host):  glue one of our windows to the edge of an Anki
#    window so the pair moves, resizes and closes as one. Returns the dock —
#    call detach() on it when the helper closes.
#  - FlowLayout / flow_row(*widgets):  a button row that wraps instead of
#    widening its window.
#  - resurface_at_home(win):  re-show a backgrounded window only once you're
#    back on Anki's main window, so a finished run never chases you across
#    desktops.

from __future__ import annotations

import threading
from contextlib import contextmanager

from aqt import mw
from aqt.qt import (
    QApplication, QCompleter, QDialog, QDialogButtonBox, QEvent, QEventLoop,
    QFrame, QHBoxLayout, QLabel, QLayout, QObject, QPlainTextEdit, QPoint,
    QProgressDialog, QPushButton, QRect, QSize, QSizePolicy, QStyle,
    QStyleOptionViewItem, Qt, QTimer, QToolTip, QVBoxLayout, QWidget,
)
from aqt.utils import tooltip

from . import log
from . import synapse


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

    # AI calls touch the network, never the collection — keep them off taskman's
    # serialized collection executor or Anki's backup queues behind them.
    mw.taskman.run_in_background(_bg, _done, uses_collection=False)
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
        att.setStyleSheet(f"color: {synapse.status_warn()};")
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
    err.setStyleSheet(f"color: {synapse.status_error()};")
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


def run_claude_json_async(button, on_done, **kwargs):
    """Non-blocking ask_claude_json. Runs the call on a background thread with
    NO modal progress dialog, so the rest of the UI stays usable while the AI
    thinks — `on_done(result)` is then invoked on the main thread (result is
    None on failure / no provider). `button`, if given, is disabled for the
    duration so a second click can't pile on a duplicate request.

    kwargs are forwarded to ask_claude_json (prompt, system, max_tokens, model).
    The manual provider is interactive (copy/paste dialog) so it runs inline on
    the main thread; on_done is still called with its result."""
    from . import api as core_api

    if is_manual_provider():
        on_done(core_api.ask_claude_json(**kwargs))
        return

    if not provider_configured():
        tooltip("Set up an AI provider in Ankisstant Settings first.", period=4000)
        on_done(None)
        return

    if button is not None:
        try:
            button.setEnabled(False)
        except RuntimeError:
            pass

    def _bg():
        return core_api.ask_claude_json(show_errors=False, **kwargs)

    def _finish(fut):
        if button is not None:
            try:
                button.setEnabled(True)
            except RuntimeError:
                pass
        result = None
        try:
            result = fut.result()
        except Exception as e:
            log.error(f"Claude call failed: {e}")
            tooltip("Claude call failed — see console for details.", period=4000)
        if result is None:
            # ask_claude already logged the cause (show_errors=False).
            tooltip("Claude call failed — see console for details.", period=4000)
        try:
            on_done(result)
        except Exception as e:  # pragma: no cover - defensive
            log.error(f"run_claude_json_async on_done failed: {e}")

    # See _run_claude: network-only work must not sit on the collection executor.
    mw.taskman.run_in_background(_bg, _finish, uses_collection=False)


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


def refresh_deck_browser() -> None:
    """Re-render the deck browser — but only when it is the screen on show.

    ``mw.deckBrowser.refresh()`` does not check ``mw.state``: it paints the deck
    list straight into ``mw.web``, which during review is the reviewer. Worse,
    the deck-list bridge command is installed by ``DeckBrowser.show()``, not by
    the render, so the deck list you get that way is inert — clicking a deck
    posts to the reviewer's link handler and nothing happens, until some other
    event redraws the real screen. Every refresh here is cosmetic (a heatmap or
    KG count on the deck page), so outside the deck browser the right answer is
    to do nothing: ``show()`` refreshes on the way back in anyway.
    """
    try:
        if mw is not None and mw.state == "deckBrowser":
            mw.deckBrowser.refresh()
    except Exception as e:
        log.warn(f"deck browser refresh failed: {e}")


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


def attach_source_completer(line_edit) -> None:
    """Attach a completer that offers SOURCE PATHS — "Placements::GP" — not
    whole tags.

    The distinction is the whole point. A source box wants the path that goes
    after "{base}::Sources::", but attach_tag_completer offers finished tags,
    so accepting its suggestion put "!!Fleg::Sources::Placements::GP" in a box
    that then prefixed it again. config.source_tag strips a doubled prefix now,
    but a box that offers the right thing in the first place is the better half
    of that fix.

    No-op if mw.col isn't ready or nothing is tagged with a source yet."""
    try:
        from ..tools.autotag import known_source_paths
        paths = known_source_paths()
        if not paths:
            return
        completer = QCompleter(paths, line_edit)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        line_edit.setCompleter(completer)
    except Exception as e:
        log.warn(f"source completer setup failed: {e}")


def attach_deck_completer(line_edit, multi: bool = False) -> None:
    """Attach an Anki-deck-name autocomplete to a QLineEdit. No-op if mw.col
    isn't ready (e.g. called before profile load)."""
    try:
        if mw is None or mw.col is None:
            return
        names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
        if not names:
            return
        if multi:
            completer = _MultiTagCompleter(names, line_edit)
        else:
            completer = QCompleter(names, line_edit)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        line_edit.setCompleter(completer)
    except Exception as e:
        log.warn(f"deck completer setup failed: {e}")


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
    # Amber wash. SynapsePro has no amber, so under the bridge this becomes its
    # `red` at low alpha — still clearly "something needs your attention", which
    # is what the banner is for. The banner always says what's wrong in words,
    # so nothing depends on reading the tint.
    frame.setStyleSheet(
        "QFrame#ankisstantSetupBanner {"
        f"  background: {synapse.tint('red', 0.18, 'rgba(255, 196, 0, 0.18)')};"
        f"  border: 1px solid {synapse.tint('red', 0.65, 'rgba(255, 196, 0, 0.65)')};"
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


def compact_button(btn: QPushButton) -> QPushButton:
    """Make a one-glyph button (?, ×, ↑, ⟳) small without clipping it.

    NOT setFixedWidth(28): the theme (core/synapse.qt_stylesheet) pads every
    QPushButton 14px a side, so a 28px button has no room left and draws as
    an empty grey square — which is what the help, refresh and remove buttons
    looked like. Shrinking the padding keeps the glyph and lets Qt size it."""
    btn.setStyleSheet(btn.styleSheet() + "QPushButton { padding: 4px 8px; }")
    return btn


class FlowLayout(QLayout):
    """A row of buttons that wraps onto the next line instead of widening.

    A QHBoxLayout's minimum width is every button side by side, and in a
    narrow window that pushes the last ones (Save, Delete) off the edge or
    gives the whole pane a sideways scrollbar. This one's minimum is its widest
    single button; the rest go to a new line when there isn't room."""

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._lay_out(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._lay_out(rect, apply=True)

    def sizeHint(self):
        w = h = 0
        for it in self._visible():
            s = it.sizeHint()
            w += s.width() + (self._gap if w else 0)
            h = max(h, s.height())
        return QSize(w, h)

    def minimumSize(self):
        w = h = 0
        for it in self._visible():
            s = it.minimumSize().expandedTo(it.sizeHint())
            w, h = max(w, s.width()), max(h, s.height())
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def _visible(self):
        return [it for it in self._items
                if it.widget() is None or not it.widget().isHidden()]

    def _lay_out(self, rect, *, apply: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._visible():
            s = it.sizeHint()
            if x > r.x() and x + s.width() > r.right() + 1:
                x, y = r.x(), y + line_h + self._gap
                line_h = 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), s))
            x += s.width() + self._gap
            line_h = max(line_h, s.height())
        return y + line_h - rect.y() + m.bottom()


def flow_row(*widgets, spacing: int = 6) -> QWidget:
    """A wrapping row holding these widgets, ready to add to any layout."""
    holder = QWidget()
    lay = FlowLayout(holder, spacing)
    for w in widgets:
        lay.addWidget(w)
    holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return holder


def make_help_button(title: str, body_html: str, parent: QWidget | None = None) -> QPushButton:
    """Small '?' button. Clicking opens a modal with the given help text.
    `body_html` is rendered as rich text inside the dialog."""
    btn = compact_button(QPushButton("?", parent))
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


def theme_dialog(widget) -> None:
    """Apply the SynapsePro bridge stylesheet to one of Ankisstant's dialogs.

    Call this at the *end* of __init__, once the layout is built — the blanket
    sheet is generic chrome, and the per-widget stylesheets that carry meaning
    (ok/warn/error status text, the "queued" highlight) are set afterwards in
    source order and must keep winning.

    A no-op when SynapsePro isn't installed, when the bridge is switched off, or
    when the "theme the smaller dialogs" kill switch is off — in each case the
    dialog looks exactly as it always did. Never raises: a themed dialog is nice
    to have, an unopenable one is not.
    """
    try:
        from . import synapse
        synapse.apply_stylesheet(widget, dialog=True)
    except Exception as e:
        log.error(f"theme_dialog failed: {e}")


class _CheckboxTips(QObject):
    """Event filter behind `tips_on_checkbox_only`. See that function."""

    # A few pixels of slack, so the target is the checkbox as you SEE it rather
    # than the exact indicator rect.
    SLACK = 3

    def __init__(self, view):
        super().__init__(view)
        self._view = view

    def _check_rect(self, index):
        """Where this cell's check indicator is drawn, in viewport coords."""
        view = self._view
        rect = view.visualRect(index)      # for a tree column 0 this already
        if not rect.isValid():             # has the indentation added on
            return None
        box = QRect()
        try:
            opt = QStyleOptionViewItem()
            opt.initFrom(view)
            opt.rect = rect
            opt.features = QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            box = view.style().subElementRect(
                QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, view)
        except Exception:
            box = QRect()
        if box.isNull() or box.width() <= 0:
            # Whatever the style does with the rest of the row, the indicator is
            # drawn at the leading edge; a fixed band there is a safe fallback.
            box = QRect(rect.left(), rect.top(), 20, rect.height())
        return box.adjusted(-self.SLACK, -self.SLACK, self.SLACK, self.SLACK)

    def eventFilter(self, obj, ev):
        try:
            if ev.type() != QEvent.Type.ToolTip:
                return False
            view = self._view
            pos = ev.pos()
            index = view.indexAt(pos)
            if (not index.isValid()
                    or index.data(Qt.ItemDataRole.CheckStateRole) is None):
                QToolTip.hideText()        # nothing here to aim at
                return True
            box = self._check_rect(index)
            tip = index.data(Qt.ItemDataRole.ToolTipRole)
            if box is None or not box.contains(pos) or not tip:
                QToolTip.hideText()
                return True
            QToolTip.showText(ev.globalPos(), str(tip), view)
            return True
        except Exception:
            return False               # never break hovering over a list


def tips_on_checkbox_only(view) -> None:
    """Only pop an item's tooltip while the cursor is over its CHECKBOX.

    A dense item view ends up with a tooltip on nearly every cell — the card
    text, the gap's notes, what the AI read it as — and the result is that you
    cannot rest the cursor anywhere on the page without something popping up
    over the thing you were reading. The information is still worth having, so
    the fix isn't to delete the tooltips: it is to make asking for one
    deliberate. The check indicator is a small, unambiguous target you only
    land on when you mean to, and everything else stays quiet.

    Cells with NO checkbox then show nothing at all. Where that text explained
    a column rather than a row, move it to the column HEADER — a thin strip you
    never rest the cursor on, and the right place for "what is this column"
    anyway.
    """
    filt = _CheckboxTips(view)
    view.viewport().installEventFilter(filt)
    # Parented to the view, but hold a reference anyway: the filter must not be
    # collected while the viewport is still sending it events.
    view._ankisstant_tip_filter = filt


class _EdgeDock(QObject):
    """Event filter behind `dock_beside`. See that function."""

    MIN_H = 220
    # A host squeezed narrower than this to make room isn't usable any more, so
    # below this we stop taking room and let the helper overlap instead.
    MIN_HOST_W = 520

    def __init__(self, helper, host, side: str, make_room: bool = True):
        super().__init__(helper)
        self._helper, self._host, self._side = helper, host, side
        self._make_room = make_room
        self._alive = True
        # Did WE hide the helper (because the host hid), or did the user close
        # it? Only the first may be undone — see reposition().
        self._hidden_by_dock = False
        # Host geometry we shrank to make room, kept so it can be handed back —
        # alongside what we actually set it to, so a host the user has since
        # resized themselves is left exactly as they left it.
        self._host_geom = None
        self._host_set = None
        self._left_fullscreen = False
        host.installEventFilter(self)

    # ── letting go ──────────────────────────────────────────────────────────
    def detach(self) -> None:
        """Stop following the host, and give back any room we took.

        A dock that outlives its helper is not harmless. It stays installed on
        the host, and the host is usually a SHARED Anki window (aqt.dialogs
        hands out one Add window for the whole profile), so the next Move or
        Show event repositions — and re-shows — a helper the user closed ages
        ago. That is how a closed side-car walks back on screen beside the Add
        window, one zombie per time the feature was used. Detaching on close is
        the whole fix: no filter, no resurrection.
        """
        if not self._alive:
            return
        self._alive = False
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        self._restore_host()

    def eventFilter(self, obj, ev):
        if not self._alive:
            return False
        try:
            t = ev.type()
            if t in (QEvent.Type.Move, QEvent.Type.Resize,
                     QEvent.Type.WindowStateChange):
                # Deferred: during a drag the host's geometry is still settling,
                # and repositioning off the in-flight value makes the helper
                # visibly lag a frame behind.
                QTimer.singleShot(0, self.reposition)
            elif t == QEvent.Type.Show:
                QTimer.singleShot(0, self.reposition)
            elif t == QEvent.Type.WindowActivate:
                QTimer.singleShot(0, self._follow_up)
            elif t == QEvent.Type.Hide:
                self._helper.hide()
                self._hidden_by_dock = True
            elif t == QEvent.Type.Close:
                # Filters run before the target's own closeEvent, so the host
                # gets its full width back BEFORE Anki saves that window's
                # geometry — otherwise the squeeze we made for the helper is
                # what reopens next time.
                self._restore_host()
                QTimer.singleShot(0, self._helper.close)
        except RuntimeError:
            pass          # helper already deleted; the filter dies with it
        except Exception as e:
            log.error(f"dock_beside: {e}")
        return False      # never consume the host's own events

    def _follow_up(self) -> None:
        try:
            if self._alive and self._helper.isVisible():
                self._helper.show()
                self._helper.raise_()
        except RuntimeError:
            pass

    # ── geometry ────────────────────────────────────────────────────────────
    def reposition(self) -> None:
        """Put the helper flush against the host's edge, same height."""
        try:
            if not self._alive:
                return
            helper, host = self._helper, self._host
            if not host.isVisible():
                return
            if helper.isHidden():
                # Only un-hide what the dock itself hid. A helper the user
                # closed stays closed.
                if not self._hidden_by_dock:
                    return
                helper.show()
                self._hidden_by_dock = False
            hg = host.frameGeometry()
            fw = helper.frameGeometry().width() - helper.width()    # window border
            fh = helper.frameGeometry().height() - helper.height()  # + title bar
            w = helper.width() + fw
            side = self._side
            try:
                avail = host.screen().availableGeometry()
            except Exception:
                avail = None
            if avail is not None:
                fits_left  = hg.left() - avail.left() >= w
                fits_right = avail.right() - hg.right() >= w
                # Prefer the asked-for side, but never push the helper off-screen
                # — a docked window you cannot see is worse than one on the
                # other side.
                if side == "left" and not fits_left and fits_right:
                    side = "right"
                elif side == "right" and not fits_right and fits_left:
                    side = "left"
                if not fits_left and not fits_right:
                    # The host fills the screen — maximised, or a macOS
                    # full-screen space. "Beside" would mean "off the edge",
                    # which is exactly the case where the side-car silently
                    # never appeared. Take the room out of the host instead.
                    if self._make_room and self._take_room(w, side):
                        return            # a Resize event brings us back here
                    hg = host.frameGeometry()
            x = hg.left() - w if side == "left" else hg.right()
            if avail is not None:
                # Last resort: clamp on screen. The helper then overlaps the
                # host rather than hiding beside it.
                x = max(avail.left(), min(x, avail.right() - w + 1))
            helper.resize(helper.width(), max(self.MIN_H, hg.height() - fh))
            helper.move(x, hg.top())
        except RuntimeError:
            pass
        except Exception as e:
            log.error(f"dock_beside reposition: {e}")

    def _take_room(self, w: int, side: str) -> bool:
        """Narrow the host by `w` so the helper has somewhere to live.

        Returns True when it changed something and a fresh reposition is on its
        way. Done once only: the remembered geometry is also what gets handed
        back on detach, and shrinking twice would eat the window."""
        host = self._host
        try:
            if (host.isFullScreen() or host.isMaximized()) \
                    and not self._left_fullscreen:
                # Leaving full screen is enough on its own — Qt puts the window
                # back to its pre-full-screen size, which usually leaves room.
                # No geometry is remembered here: showNormal already restored
                # the one the user had, and macOS animates out of the space, so
                # anything read right now is still the old rect. Come back for
                # it once the animation has landed.
                self._left_fullscreen = True
                host.showNormal()
                QTimer.singleShot(300, self.reposition)
                return True
            if self._host_geom is not None:
                return False          # already narrowed once; don't eat it twice
            cur = host.geometry()
            new_w = max(self.MIN_HOST_W, cur.width() - w)
            if new_w >= cur.width():
                return False
            new_x = cur.left() + (cur.width() - new_w) if side == "left" else cur.left()
            self._host_geom = QRect(cur)
            host.setGeometry(new_x, cur.top(), new_w, cur.height())
            self._host_set = QRect(host.geometry())
            return True
        except Exception as e:
            log.error(f"dock_beside: couldn't make room: {e}")
            return False

    def _restore_host(self) -> None:
        """Hand back the width we borrowed — unless the user has since moved or
        resized the host themselves, in which case their geometry wins."""
        geom, self._host_geom = self._host_geom, None
        set_to, self._host_set = self._host_set, None
        if geom is None:
            return
        try:
            if set_to is not None and self._host.geometry() != set_to:
                return
            if self._host.isVisible():
                self._host.setGeometry(geom)
        except RuntimeError:
            pass
        except Exception as e:
            log.error(f"dock_beside: couldn't restore the host: {e}")


def dock_beside(helper, host, side: str = "left", make_room: bool = True):
    """Glue `helper` to the edge of `host` so the two behave as one window.

    The helper follows every move, resize, minimise, raise, hide and close of
    the host, and matches its height. It is NOT reparented — the host stays a
    perfectly ordinary Anki window, with its own close/save behaviour and its
    own saved geometry intact, which is the whole reason for doing it this way
    rather than embedding one window inside the other.

    Side is a preference, not a promise: a helper that would land off the edge
    of the screen goes on the other side instead. With `make_room` (the
    default) a host that fills the screen — maximised, or in a macOS
    full-screen space, where there is no "beside" at all — is taken out of full
    screen and narrowed by the helper's width, and gets that width back when
    the helper closes. Without it, a helper with nowhere to go overlaps the
    host rather than vanishing off the edge.

    Returns the dock; keep it if you need to `detach()` early — and DO detach
    when the helper closes, or the filter outlives it and re-shows it later.
    """
    dock = _EdgeDock(helper, host, side, make_room=make_room)
    helper._ankisstant_dock = dock      # keep it alive with the helper
    dock.reposition()
    return dock


class _HomeWaiter(QObject):
    """Holds a finished, hidden window until Anki's main window is next active."""

    def __init__(self, win):
        super().__init__(mw)
        self._win = win
        mw.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is mw and ev.type() == QEvent.Type.WindowActivate:
            # Next turn of the loop, not inside the activation itself: showing
            # a window mid-activate can steal it straight back from mw.
            QTimer.singleShot(0, self._release)
        return False

    def _release(self) -> None:
        try:
            mw.removeEventFilter(self)
        except RuntimeError:
            pass
        _show_over_mw(self._win)
        if getattr(self._win, "_ankisstant_home_waiter", None) is self:
            self._win._ankisstant_home_waiter = None
        self.deleteLater()


def _show_over_mw(win) -> None:
    try:
        win.show()
        win.raise_()
        win.activateWindow()
    except RuntimeError:        # closed for real while it waited
        pass


def resurface_at_home(win) -> None:
    """Bring a backgrounded window back WITHOUT chasing the user.

    On macOS a hidden window that's shown again lands on whatever desktop
    (Space) you're on right now, and activateWindow drags Anki to the front
    with it — so a run finishing while you were in another app or Space popped
    up there, every stage. Instead: if Anki's main window is the one in front,
    show it now; otherwise bounce the Dock icon and wait, and the window comes
    back the next time you go to Anki's main window.
    """
    if mw.isActiveWindow():
        _show_over_mw(win)
        return
    if getattr(win, "_ankisstant_home_waiter", None) is not None:
        return                   # already waiting; one is plenty
    win._ankisstant_home_waiter = _HomeWaiter(win)
    try:
        QApplication.alert(mw, 0)
        tooltip("Ankisstant finished — it's waiting on Anki's main window.",
                period=5000, parent=mw)
    except Exception:
        pass
