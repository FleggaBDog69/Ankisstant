# Ankisstant — merged Anki addon combining QBank, Browse and Card-Creator
# tools (originally three separate addons).
#
# All gui_hooks for every tool are registered here and nowhere else. Tools
# expose only `init(main_window)` and `get_panel()`; they never touch
# gui_hooks, config files, or the Anthropic API directly.


# ── Progress-dialog race patch (carried over from the QBank addon) ────────────
#
# Anki delays the progress dialog's `show()` by ~100ms so quick operations
# don't flash a dialog. If the operation finishes before the timer fires, the
# underlying ProgressDialog is deleted but the timer still runs and calls
# `self._win.show()` on the freed C++ object — RuntimeError.
#
# Reproduces reliably when the missed-Q review dialog opens the Browser and
# then immediately calls `search_for`, since both stages trigger their own
# progress dialogs. The fix swallows the specific RuntimeError; the original
# behaviour is otherwise preserved.
def _patch_progress_show_win() -> None:
    try:
        from aqt import progress as _progress
        if getattr(_progress.ProgressManager._showWin, "_ankisstant_patched", False):
            return
        _orig = _progress.ProgressManager._showWin

        def _safe_show_win(self):
            try:
                _orig(self)
            except RuntimeError:
                pass

        _safe_show_win._ankisstant_patched = True
        _progress.ProgressManager._showWin = _safe_show_win
    except Exception as e:
        # Logger isn't importable this early (this runs at module import,
        # before the package is fully wired). Fall back to print.
        import sys
        print(f"[ankisstant] warn: progress patch failed: {e}", file=sys.stderr)


_patch_progress_show_win()


from aqt import mw, gui_hooks
from aqt.qt import QAction, QTimer

from .core.config import ensure_config, load_config, tool_enabled
from .core.migration import run_once_if_needed
from .core import log
from .tools import qbank as _qbank
from .tools import browse as _browse
from .tools import knowledge_gaps as _knowledge_gaps
from .tools import card_creator as _creator
from .ui.main_window import open_main_window
from .ui.settings import open_settings
from .ui.welcome import open_welcome


TOOLS = [
    ("qbank",          _qbank),
    ("browse",         _browse),
    ("knowledge_gaps", _knowledge_gaps),
    ("card_creator",   _creator),
]


# ── Tools menu ────────────────────────────────────────────────────────────────

def _setup_menu() -> None:
    try:
        action = QAction("Ankisstant…", mw)
        action.setShortcut("Ctrl+Shift+L")
        action.triggered.connect(open_main_window)
        mw.form.menuTools.addAction(action)

        settings_action = QAction("Ankisstant Settings…", mw)
        settings_action.triggered.connect(open_settings)
        mw.form.menuTools.addAction(settings_action)
    except Exception as e:
        log.error(f"setup_menu failed: {e}")


# ── Profile load — entry point for all per-profile init ──────────────────────

def _on_profile_loaded() -> None:
    try:
        ensure_config()
        run_once_if_needed()
    except Exception as e:
        log.error(f"config / migration failed: {e}")

    for key, module in TOOLS:
        if not tool_enabled(key):
            continue
        try:
            module.init(main_window=None)
        except Exception as e:
            log.error(f"init({key}) failed: {e}")

    _setup_menu()

    # First-run welcome — defer so the main window is up and ready to be
    # focused after the dialog closes.
    try:
        if not load_config().get("first_run_seen", False):
            QTimer.singleShot(500, open_welcome)
    except Exception as e:
        log.error(f"welcome scheduling failed: {e}")


# ── Top toolbar — adds an "Ankisstant" link next to Sync ─────────────────────

def _on_top_toolbar_init_links(links, top_toolbar) -> None:
    try:
        links.append(
            top_toolbar.create_link(
                "ankisstant",
                "Ankisstant",
                open_main_window,
                tip="Open Ankisstant (Ctrl+Shift+L)",
                id="ankisstant-toolbar-button",
            )
        )
    except Exception as e:
        log.error(f"toolbar link failed: {e}")


def _on_deck_browser_render(deck_browser, content) -> None:
    try:
        # Heatmap (only when QBank is enabled and `show_heatmap` is true)
        if tool_enabled("qbank"):
            html = _qbank.render_heatmap_html()
            if html:
                content.stats += html
    except Exception as e:
        log.error(f"deck browser render failed: {e}")


# ── pycmd routing ────────────────────────────────────────────────────────────

def _on_js_message(handled, message, context):
    if not isinstance(message, str):
        return handled

    if message == "ankisstant:open":
        open_main_window()
        return (True, None)
    if message == "ankisstant:settings":
        open_settings()
        return (True, None)
    if message == "ankisstant:add_kg":
        try:
            _knowledge_gaps.open_add_kg_dialog()
        except Exception as e:
            log.error(f"add_kg failed: {e}")
        return (True, None)
    if message.startswith("ankisstant:open_kg"):
        # ankisstant:open_kg            -> default filter
        # ankisstant:open_kg:<filter>   -> set filter (e.g. qbank, open, all)
        open_main_window()
        try:
            from .ui.main_window import _current as _mw
            if _mw is not None:
                _mw._show_tool("knowledge_gaps")
                parts = message.split(":", 2)
                if len(parts) >= 3 and _knowledge_gaps._panel is not None:
                    _knowledge_gaps._panel.set_filter(parts[2])
        except Exception as e:
            log.error(f"open_kg failed: {e}")
        return (True, None)

    if message.startswith("qbank:") or message == "emedici:open":
        result = _qbank.handle_pycmd(message)
        if result[0]:
            return result

    if message == "practice_questions:open":
        try:
            from practice_questions.library import show_library
            show_library()
        except Exception as e:
            log.error(f"practice_questions:open failed: {e}")
        return (True, None)

    return handled


# ── Register hooks ────────────────────────────────────────────────────────────

gui_hooks.profile_did_open.append(_on_profile_loaded)
gui_hooks.deck_browser_will_render_content.append(_on_deck_browser_render)
gui_hooks.webview_did_receive_js_message.append(_on_js_message)
gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_init_links)
