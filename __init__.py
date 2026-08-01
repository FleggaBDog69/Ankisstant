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
        # before the package is fully wired). Fall back to a plain print —
        # NOT stderr, which would trip Anki's global "error occurred" dialog
        # for a non-fatal, best-effort patch failure.
        print(f"[ankisstant] warn: progress patch failed: {e}")


_patch_progress_show_win()


from aqt import mw, gui_hooks
from aqt.qt import QAction, QKeySequence, QShortcut, Qt, QTimer

from .core.config import (
    ensure_config, load_config, synapse_config, tool_config, tool_enabled,
)
from .core.migration import run_once_if_needed
from .core import log
from .tools import qbank as _qbank
from .tools import browse as _browse
from .tools import knowledge_gaps as _knowledge_gaps
from .tools import card_creator as _creator
from .tools import update_by_tag as _update_by_tag
from .tools import lecture as _lecture
from .ui.main_window import open_main_window
from .ui.settings import open_settings
from .ui.welcome import open_welcome


TOOLS = [
    ("qbank",          _qbank),
    ("browse",         _browse),
    ("knowledge_gaps", _knowledge_gaps),
    ("card_creator",   _creator),
    ("update_by_tag",  _update_by_tag),
    ("lecture",        _lecture),
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


# ── Global capture shortcut ───────────────────────────────────────────────────
#
# A single application-wide QShortcut so "capture a missed Q" works from
# anywhere — including mid-review, where the reviewer eats most key events.
# Re-created on every profile load so a settings change takes effect without
# an Anki restart.
_capture_shortcut: QShortcut | None = None


def _setup_capture_shortcut() -> None:
    global _capture_shortcut
    try:
        if _capture_shortcut is not None:
            _capture_shortcut.setParent(None)
            _capture_shortcut.deleteLater()
            _capture_shortcut = None
        if not tool_enabled("qbank"):
            return
        seq = (tool_config("qbank").get("capture_shortcut") or "").strip()
        if not seq:
            return
        sc = QShortcut(QKeySequence(seq), mw)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.activated.connect(lambda: _qbank.open_capture("", ""))
        _capture_shortcut = sc
    except Exception as e:
        log.error(f"capture shortcut setup failed: {e}")


# ── Profile load — entry point for all per-profile init ──────────────────────

def _on_profile_loaded() -> None:
    try:
        ensure_config()
        run_once_if_needed()
    except Exception as e:
        log.error(f"config / migration failed: {e}")

    # Install the bundled Malleus Q&A notetype + its Create profile (idempotent).
    try:
        from .tools import notetypes
        notetypes.ensure_bundled()
    except Exception as e:
        log.error(f"bundled notetype install failed: {e}")

    for key, module in TOOLS:
        if not tool_enabled(key):
            continue
        try:
            module.init(main_window=None)
        except Exception as e:
            log.error(f"init({key}) failed: {e}")

    _setup_menu()
    _setup_capture_shortcut()

    # SynapsePro bridge — a no-op when SynapsePro isn't installed. Registered
    # per profile because its launcher strip is rebuilt per profile.
    try:
        from .core import synapse, synapse_sidebar
        synapse.reset_cache()          # add-ons may have been enabled since
        synapse_sidebar.register()
    except Exception as e:
        log.error(f"synapse sidebar registration failed: {e}")

    # First-run welcome — defer so the main window is up and ready to be
    # focused after the dialog closes. Also re-shown once after an upgrade that
    # bumps FORCE_WIZARD_VERSION (e.g. a new wizard page). We stamp the version
    # at open time, so a user who skips the wizard isn't nagged again.
    try:
        from .core.config import FORCE_WIZARD_VERSION, save_config
        cfg = load_config()
        first_run = not cfg.get("first_run_seen", False)
        forced = (not first_run
                  and cfg.get("forced_wizard_version") != FORCE_WIZARD_VERSION)
        if first_run or forced:
            cfg["forced_wizard_version"] = FORCE_WIZARD_VERSION
            save_config(cfg)
            QTimer.singleShot(500, open_welcome)
    except Exception as e:
        log.error(f"welcome scheduling failed: {e}")


# ── Top toolbar — adds an "Ankisstant" link next to Sync ─────────────────────

def _on_top_toolbar_init_links(links, top_toolbar) -> None:
    try:
        # When SynapsePro is running and our button is live in its launcher
        # strip, that button *is* this link — two front doors a centimetre apart
        # is just clutter. Checked live on every redraw rather than from a flag
        # set at startup: if injection ever fails, the link must still be here.
        from .core import synapse_sidebar
        if synapse_sidebar.injected_live() and \
                synapse_config().get("hide_toolbar_link", True):
            return
    except Exception:
        pass          # any doubt at all → keep the link
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


# ── Native browser — Ankisstant menu + AI Search checkbox ────────────────────
#
# Two things land in Anki's own card browser, both wired up here:
#  - An "Ankisstant" menu with a "Settings…" entry — always present, regardless
#    of which tools are enabled, so users can reach Ankisstant Settings (and
#    set up an AI provider in the first place) without leaving the browser or
#    needing the rest of Ankisstant "connected" first.
#  - A "✨ AI Search" checkbox next to the search bar (gated on the AI Browse
#    tool, since it reuses that tool's search-term-generation prompts/config).
#    When it's on, typing a topic and pressing Enter sends it to AI and
#    searches for the generated terms instead of the literal text.

def _on_browser_menus_did_init(browser) -> None:
    try:
        menu = browser.form.menubar.addMenu("Ankisstant")
        settings_action = QAction("Ankisstant Settings…", browser)
        # Jump straight to the AI Browse tab — this menu only exists inside
        # Anki's own Browse window, so that's almost always what's wanted.
        settings_action.triggered.connect(lambda _checked=False: open_settings("browse"))
        menu.addAction(settings_action)
    except Exception as e:
        log.error(f"browser settings menu failed: {e}")

    try:
        if tool_enabled("browse"):
            _browse.install_native_ai_search(browser)
    except Exception as e:
        log.error(f"native AI search setup failed: {e}")


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
            from practice_questions.launcher import show_launcher
            show_launcher()
        except Exception as e:
            log.error(f"practice_questions:open failed: {e}")
        return (True, None)

    return handled


# ── Register hooks ────────────────────────────────────────────────────────────

gui_hooks.profile_did_open.append(_on_profile_loaded)
gui_hooks.browser_menus_did_init.append(_on_browser_menus_did_init)
gui_hooks.deck_browser_will_render_content.append(_on_deck_browser_render)
gui_hooks.webview_did_receive_js_message.append(_on_js_message)
gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_init_links)
