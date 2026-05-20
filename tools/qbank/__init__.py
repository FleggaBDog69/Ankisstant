# QBank tool — entry point exposing init() and get_panel().
# Also exports a few helpers consumed by the merged __init__.py's gui_hooks
# (heatmap injection + pycmd dispatch).

from __future__ import annotations

from aqt.qt import QScrollArea, QWidget

from ...core.config import tool_config, tool_enabled
from . import heatmap as _heatmap
from .browser import open_platform
from .capture_dialog import open_capture
from .panel import QBankPanel, _open_kg_page_for_qbank
from .session_dialog import open_session_dialog
from .stats import ensure_storage


NAME = "QBank with Claude"

_panel: QBankPanel | None = None
_scroll: QScrollArea | None = None


def init(main_window) -> None:
    """Called once on profile load. Set up persistent storage for each
    configured platform, reset heatmap scroll state."""
    _heatmap.reset_offset()
    for p in tool_config("qbank").get("platforms", []):
        try:
            ensure_storage(p["key"])
        except Exception as e:
            print(f"[ankisstant] ensure_storage failed for {p}: {e}")


def get_panel() -> QWidget:
    """Return the singleton scrollable QBank panel for the main window."""
    global _panel, _scroll
    if _panel is None:
        _panel = QBankPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.refresh()
    return _scroll


# ── Hooks dispatched from the merged __init__.py ──────────────────────────────

def render_heatmap_html() -> str:
    """Return the heatmap HTML, or '' if disabled/empty platforms."""
    cfg = tool_config("qbank")
    if not cfg.get("show_heatmap", True):
        return ""
    platforms = cfg.get("platforms", [])
    if not platforms:
        return ""
    try:
        return _heatmap.build_card_html(platforms, cfg)
    except Exception as e:
        print(f"[ankisstant] heatmap render failed: {e}")
        return ""


def handle_pycmd(message: str) -> tuple[bool, object]:
    """Handle qbank:* pycmd messages from the deck browser HTML.
    Returns (handled, value) for chaining with the gui_hook return convention."""
    from aqt import mw

    if message.startswith("qbank:open:"):
        key = message[len("qbank:open:"):]
        p = _find_platform(key)
        if p:
            open_platform(p["key"], p["name"], p["url"])
        return (True, None)

    if message == "qbank:capture":
        open_capture("", "")
        return (True, None)

    if message == "qbank:review":
        # Legacy route kept for compatibility — open the unified KG page
        # filtered to qbank-source items.
        _open_kg_page_for_qbank()
        return (True, None)

    if message == "qbank:log_session":
        open_session_dialog()
        return (True, None)

    if message == "practice_questions:open":
        # Soft bridge to the practice_questions addon if it's installed.
        try:
            from practice_questions.library import show_library  # type: ignore
            show_library()
        except Exception as e:
            print(f"[ankisstant] practice_questions launch failed: {e}")
        return (True, None)

    if message in ("qbank:heatmap:prev", "qbank:heatmap:next", "qbank:heatmap:today"):
        if message == "qbank:heatmap:prev":
            _heatmap.shift_offset(-1)
        elif message == "qbank:heatmap:next":
            _heatmap.shift_offset(1)
        else:
            _heatmap.reset_offset()
        try:
            mw.deckBrowser.refresh()
        except Exception:
            pass
        return (True, None)

    # Back-compat with the original eMedici-only pycmd that the heatmap used
    # to emit. Harmless if nothing still sends it.
    if message == "emedici:open":
        p = _find_platform("emedici")
        if p:
            open_platform(p["key"], p["name"], p["url"])
        return (True, None)

    return (False, None)


def _find_platform(key: str) -> dict | None:
    for p in tool_config("qbank").get("platforms", []):
        if p.get("key") == key:
            return p
    return None
