# Ankisstant's main UI — the Knowledge Gaps page, plus the pages it opens.
#
# It lives in one of two hosts, and can move between them without being rebuilt:
#
#   MainWindow      a free-floating QDialog. What Ankisstant has always used,
#                   and still the only option without SynapsePro.
#   AnkisstantDock  a QDockWidget on the right, sitting alongside SynapsePro's
#                   own AI / notebook / mind-map panels.
#
# The UI itself is AnkisstantBody, an ordinary QWidget that knows nothing about
# which host it's in beyond how wide it is. `_current` points at the *body*, not
# the host, because consumers elsewhere find it by duck-typing (queue_owner
# looks for gap_queue) rather than by it being a window.
#
# The pop-out button re-parents the body rather than rebuilding it, which is not
# just an optimisation: each page module caches its panel in a module global
# (see tools/knowledge_gaps.get_panel), so a rebuild would hand the same widget
# to two parents. It also means a half-typed gap survives the move.

from __future__ import annotations

import importlib

from aqt import gui_hooks, mw
from aqt.qt import (
    QDialog, QDockWidget, QEvent, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, Qt, QTimer, QVBoxLayout, QWidget,
)

from ..core import synapse
from ..core.config import (
    get_window_geometry, save_synapse_config, set_window_geometry,
    synapse_config, tool_config, tool_enabled,
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
    ("update_by_tag",    "Update by Tag",   f"{_PKG}.tools.update_by_tag"),
    ("lecture",          "AI Lecture",      f"{_PKG}.tools.lecture"),
]

# What can be shown. Knowledge Gaps is home; the other two are visited from
# its corner. The rest of TOOLS keeps its entry so settings and the panel
# cache still know the names, but nothing opens them.
PAGES = ("knowledge_gaps", "lecture", "qbank")

DOCK_OBJECT_NAME = "AnkisstantSidebarDock_v1"

# The panels are compacted to fit whatever they're given (see `ui/compact.py`),
# so this only has to leave the icon rail plus a usable column — not room for
# the original 500px form fields.
_DOCK_MIN_WIDTH = 210


def _tool_visible(key: str) -> bool:
    """Whether `key` should get a sidebar entry / panel in the Ankisstant
    window. Browse is special-cased: in "native search only" mode the tool
    stays enabled (so the in-browser AI search keeps working) but its
    dedicated panel is hidden — there's nothing useful to show here."""
    if not tool_enabled(key):
        return False
    if key == "browse" and tool_config("browse").get("native_only"):
        return False
    return True


def _compactify(root) -> None:
    """Fit a panel drawn for a 900px window into a side panel half that wide.

    The pass itself lives in `ui/compact.py`, where what it does and why is
    written down; it runs in the dock only.
    """
    try:
        from .compact import compactify
        compactify(root)
    except Exception as e:
        print(f"[ankisstant] could not compact panel: {e}")


def _uncompactify(root) -> None:
    """Undo the wording half of it — this panel is back in a full window."""
    try:
        from .compact import uncompactify
        uncompactify(root)
    except Exception as e:
        print(f"[ankisstant] could not restore panel wording: {e}")


def _disabled_placeholder(label: str, key: str | None = None) -> QWidget:
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title = QLabel(f"<h2 style='margin:0'>{label}</h2>")
    title.setTextFormat(Qt.TextFormat.RichText)
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)
    if key == "browse" and tool_enabled("browse") and tool_config("browse").get("native_only"):
        text = (
            "Native search only — the AI Browse panel is hidden.\n"
            "The ✨ AI Search checkbox in Anki's own Browse window still works.\n"
            "Switch modes in Settings → Tools."
        )
    else:
        text = "Tool disabled — enable in Settings → Tools."
    msg = QLabel(text)
    msg.setStyleSheet("color: gray; font-size: 13px;")
    msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(msg)
    return w


class AnkisstantBody(QWidget):
    """The Ankisstant UI, independent of whichever host it's parented to.

    `compact=True` swaps the 200px text nav for a 44px icon rail. That's the
    only difference between the dock and the window: a nav column a third as
    wide as the dock is a nav column you resent, and the panels inside hold
    fields with 500px minimums that need the room.
    """

    def __init__(self, compact: bool = False, adopt_from: "AnkisstantBody | None" = None,
                 parent=None):
        super().__init__(parent)
        self._compact = bool(compact)
        # Callback the host installs so the pop-out button can reach it without
        # the body knowing what a dock is.
        self.on_popout = None

        # The two work queues. Both are kg.queues.Queue — list-like, but they
        # persist themselves to user_files/queues.json, so a crash or a quit
        # mid-batch no longer loses the gaps you'd picked out. See queues.py
        # for the one rule new call sites have to follow.
        #
        # gap_queue: gaps waiting to become cards. Items are dicts
        # {"title", "kg_id", "stem_html", "notes", ...}. The Knowledge Gaps
        # page pushes here; Create takes one at a time, and a kg_id (when set)
        # is marked done in the KG store on successful Add.
        #
        # browse_queue: KGs waiting to be looked up in Browse. Items are full
        # KG dicts (id, title, fields, tags, type). Browse works through them
        # and a successful Tag & Unsuspend marks the KG done and advances.
        try:
            from ..tools.kg import queues as kg_queues
            self.gap_queue, self.browse_queue = kg_queues.load()
        except Exception as e:
            # Never let a bad queue file stop the window opening — an empty
            # session is recoverable, a window that won't build isn't.
            print(f"[ankisstant] queue restore failed, starting empty: {e}")
            self.gap_queue = []
            self.browse_queue = []
        self._panel_cache: dict[str, QWidget] = {}

        # Adoption has to happen BEFORE _build(). Building selects a tool, which
        # calls that panel's refresh_queue_state(self) — against an empty queue
        # that would clear a half-filled Create form, which is precisely the
        # state a pop-out is supposed to preserve.
        if adopt_from is not None:
            self.gap_queue = adopt_from.gap_queue
            self.browse_queue = adopt_from.browse_queue
            self._panel_cache = adopt_from._panel_cache
            # Orphan the cached panels before the old body is destroyed. Only
            # the one on screen gets re-parented by _show_tool; the rest would
            # be deleted along with the old stack, and each tool module holds
            # its panel in a module global (tools/browse.py::get_panel and
            # friends) that would then be a dangling C++ pointer — a crash the
            # next time you clicked that tool.
            for panel in self._panel_cache.values():
                try:
                    panel.setParent(None)
                except Exception:
                    pass

        self._build(select=_current_tool_key(adopt_from) if adopt_from else None)

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self, select: str | None = None) -> None:
        """No sidebar. The Knowledge Gaps page IS Ankisstant; AI Lecture and
        AI QBank are pages you visit from its corner and come back from.

        AI Browse, AI Create and Update by Tag have no way in any more: the
        queue (Find & write) does both halves, a free-text card search is
        Anki's own Browse window with ✨ AI Search, and nobody used the tag
        tool. Their code is left where it is."""
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._holders: dict[str, QWidget] = {}
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.stack, 1)
        key = select if select in PAGES else "knowledge_gaps"
        self._show_tool(key)

    def show_page(self, key: str) -> None:
        """Open one of the pages. Anything that isn't one goes home."""
        self._show_tool(key if key in PAGES else "knowledge_gaps")

    def _wrap_page(self, key: str, widget: QWidget) -> QWidget:
        """A visited page, with the way back above it."""
        holder = self._holders.get(key)
        if holder is not None:
            try:
                if widget.parent() is holder:
                    return holder
            except RuntimeError:
                pass
        holder = QWidget()
        holder._ank_key = key
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 2)
        back = QPushButton("← Gaps")
        back.setAutoDefault(False)
        back.setToolTip("Back to Knowledge Gaps")
        back.setStyleSheet(
            "QPushButton { padding: 3px 10px; font-size: 11px; "
            "border: 1px solid rgba(127,127,127,0.3); border-radius: 5px; "
            "background: transparent; }"
            "QPushButton:hover { background: rgba(127,127,127,0.12); }")
        back.clicked.connect(lambda: self.show_page("knowledge_gaps"))
        bar.addWidget(back)
        bar.addStretch(1)
        col.addLayout(bar)
        col.addWidget(widget, 1)
        self._holders[key] = holder
        return holder

    # ── queue handoff (Browse → Create) ──────────────────────────────────────

    def _show_tool(self, key: str) -> None:
        # Pick a panel: tool's get_panel() if visible, else a placeholder.
        if not _tool_visible(key):
            placeholder_label = dict((k, l) for k, l, _ in TOOLS).get(key, key)
            widget = _disabled_placeholder(placeholder_label, key=key)
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
        # Once per panel — the tools cache them, so a re-show would otherwise
        # walk the whole widget tree again for nothing.
        was = getattr(widget, "_ank_compacted", False)
        if self._compact != was:
            _compactify(widget) if self._compact else _uncompactify(widget)
            try:
                widget._ank_compacted = self._compact
            except Exception:
                pass
        if key != "knowledge_gaps":
            widget = self._wrap_page(key, widget)
        self.load_panel(widget)

    def load_panel(self, widget: QWidget) -> None:
        # Avoid re-adding the same widget — QStackedWidget will keep it on
        # subsequent calls. setCurrentWidget is a no-op if already current.
        if self.stack.indexOf(widget) == -1:
            self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)

    # ── actions ──────────────────────────────────────────────────────────────

    def _request_popout(self) -> None:
        if callable(self.on_popout):
            try:
                self.on_popout()
            except Exception as e:
                print(f"[ankisstant] pop-out failed: {e}")

    def _open_settings(self) -> None:
        from .settings import SettingsDialog
        dlg = SettingsDialog(self.window())
        if dlg.exec():
            self._panel_cache.clear()
            try:
                from ..core import synapse_sidebar
                synapse_sidebar.apply_settings_change()
            except Exception:
                pass


# ── hosts ─────────────────────────────────────────────────────────────────────

class MainWindow(QDialog):
    """The free-floating window. What Ankisstant has always been."""

    def __init__(self, body: AnkisstantBody | None = None, parent=None):
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.body = body if body is not None else AnkisstantBody(compact=False)
        self.body.setParent(self)
        layout.addWidget(self.body)
        synapse.apply_stylesheet(self)

    def changeEvent(self, event):
        # Coming back from SynapsePro's settings is how a colour-theme change
        # reaches us — there's no signal to subscribe to. See check_theme_drift.
        try:
            if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
                check_theme_drift()
        except Exception:
            pass
        super().changeEvent(event)

    def closeEvent(self, event):
        try:
            geom = self.frameGeometry()
            set_window_geometry(self.width(), self.height(), geom.x(), geom.y())
        except Exception as e:
            print(f"[ankisstant] persist window geometry failed: {e}")
        # Deliberately NOT forgetting the host here. Closing hides it; the next
        # open_main_window() re-shows this same one, so both queues and any
        # half-filled form are still there. (Before the dock existed, a close
        # threw the queues away and rebuilt from scratch.)
        _sync_sidebar(False)
        super().closeEvent(event)


class AnkisstantDock(QDockWidget):
    """A side panel, sitting alongside SynapsePro's own.

    Placement goes through ``constants.place_feature_dock`` when SynapsePro
    offers it — it's a genuine extension point, registered at runtime, and it
    already handles the case where the launcher strip is itself on the right.
    Falling back to a plain ``addDockWidget`` is fine; Qt splits docks in the
    same area rather than tabbing them, which is why several panels can be open
    side by side without anyone arranging it.
    """

    def __init__(self, body: AnkisstantBody | None = None):
        super().__init__("Ankisstant", mw)
        self.setObjectName(DOCK_OBJECT_NAME)
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        # SynapsePro's panels all suppress the native title bar; ours would
        # otherwise be the one thing in the row with a grey strip on top.
        self.setTitleBarWidget(QWidget())
        self.setMinimumWidth(_DOCK_MIN_WIDTH)

        self.body = body if body is not None else AnkisstantBody(compact=True)
        self.setWidget(self.body)
        synapse.apply_stylesheet(self)

        try:
            width = int(synapse_config().get("dock_width", 720) or 720)
        except Exception:
            width = 720
        width = max(width, _DOCK_MIN_WIDTH)
        # Never take more than two thirds of the window, however wide it was
        # last time — a saved width from a big external display would otherwise
        # swallow the review area on the laptop screen.
        try:
            width = min(width, max(_DOCK_MIN_WIDTH, int(mw.width() * 0.66)))
        except Exception:
            pass
        self.resize(width, self.height())

        self._place()
        # Width is asked for with a TEMPORARY minimum, not with resizeDocks.
        #
        # resizeDocks makes Qt find the pixels somewhere, and "somewhere" turned
        # out to include SynapsePro's launcher strip — a fixed-width dock that
        # has no give. Opening Ankisstant beside their AI panel and then closing
        # the AI left the strip mangled and the whole layout offset. None of
        # their own feature panels call resizeDocks, which is exactly why this
        # only ever happened to ours.
        #
        # A minimum, released on the next tick, gets the same opening width
        # while leaving Qt free to rebalance afterwards — including when a
        # sibling dock closes and its space has to go back.
        self.setMinimumWidth(width)
        QTimer.singleShot(0, self._release_width)
        self.visibilityChanged.connect(self._on_visibility)

    def _release_width(self) -> None:
        """Drop back to the real minimum once the opening width has been taken."""
        try:
            self.setMinimumWidth(_DOCK_MIN_WIDTH)
        except RuntimeError:
            pass      # dock already destroyed; nothing to relax

    def _place(self) -> None:
        try:
            constants = synapse.constants_module()
            place = getattr(constants, "place_feature_dock", None) if constants else None
            if callable(place):
                place(self)
                return
        except Exception:
            pass
        try:
            mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self)
        except Exception as e:
            print(f"[ankisstant] could not dock: {e}")

    def _on_visibility(self, visible: bool) -> None:
        _sync_sidebar(bool(visible))
        if visible:
            # The dock lives inside the main window, so it never gets its own
            # activation event — becoming visible again is the equivalent moment.
            check_theme_drift()
        else:
            self._persist_width()

    def _persist_width(self) -> None:
        try:
            width = self.width()
            if width >= _DOCK_MIN_WIDTH:
                save_synapse_config({"dock_width": int(width)})
        except Exception:
            pass

    def closeEvent(self, event):
        # Closing a dock hides it — the object stays. Forgetting it here would
        # make the next open build a *second* dock with the same objectName,
        # and Qt would then have two of them fighting over the same area.
        self._persist_width()
        super().closeEvent(event)


# ── the singleton ─────────────────────────────────────────────────────────────

# The *body*, not the host. Everything else in the add-on reaches Ankisstant
# through this and is duck-typed on the queue API — see the module docstring.
_current: AnkisstantBody | None = None
_host: QWidget | None = None


def queue_owner(widget=None):
    """The live widget that owns the queues (gap_queue / browse_queue).

    That's the *body* — never `widget.window()`, which lands on the host: the
    free-floating MainWindow, or Anki's own main window when we're a dock under
    SynapsePro. Neither has the queues, so anything resolving a hand-off by
    walking to the top-level fails in both hosts.

    Walks up from `widget` first (so a panel inside a body finds its own body,
    even if a second one somehow exists), then falls back to the singleton —
    which is what dialogs parented outside the body need.
    """
    w = widget
    while w is not None:
        if hasattr(w, "gap_queue"):
            return w
        try:
            w = w.parent()
        except RuntimeError:      # underlying C++ object deleted mid-walk
            break
    if _current is not None and _is_live(_current) and hasattr(_current, "gap_queue"):
        return _current
    return None


def _sync_sidebar(open_: bool) -> None:
    """Keep the SynapsePro strip button's lit/unlit state honest.

    SynapsePro drives its own buttons off each dock's visibilityChanged, but the
    helper that does it (`sync_dock_button`) only knows the docks in its
    hardcoded feature map. So we tell our own button instead — same effect, and
    still nothing edited on their side.
    """
    try:
        from ..core import synapse_sidebar
        synapse_sidebar.sync_open_state(open_)
    except Exception:
        pass


def _forget() -> None:
    """Drop the singleton — the widgets behind it are gone or about to be.

    Called on profile close, where Anki tears the main window down. Everything
    here is per-profile (the KG queues, the panels, the dock), so carrying any
    of it into the next profile would be wrong as well as unsafe.
    """
    global _current, _host
    # Flush the queues BEFORE the widgets go: the save is debounced, so the
    # last few seconds of work may only exist in memory at this point.
    try:
        from ..tools.kg import queues as kg_queues
        kg_queues.forget()
    except Exception as e:
        print(f"[ankisstant] queue flush on profile close failed: {e}")
    if _host is not None:
        try:
            if isinstance(_host, AnkisstantDock):
                mw.removeDockWidget(_host)
            _host.deleteLater()
        except Exception:
            pass
    _current = None
    _host = None


def _dock_mode() -> bool:
    """Should we open as a side panel?

    Requires SynapsePro to actually be there — a dock inside a plain Anki window
    would be a strange thing to inflict on someone who never asked for it, and
    "standalone is exactly as it was" is the rule the whole bridge is built on.
    """
    try:
        cfg = synapse_config()
        return (cfg.get("open_mode", "dock") == "dock"
                and synapse.synapse_available()
                and bool(cfg.get("sidebar_buttons", True)))
    except Exception:
        return False


def _is_live(widget) -> bool:
    try:
        widget.isVisible()
        return True
    except RuntimeError:      # underlying C++ object deleted
        return False


def open_main_window() -> None:
    """Open (or focus) Ankisstant. Signature unchanged — a dozen callers rely on it."""
    global _current, _host
    if _host is not None and _is_live(_host) and _current is not None:
        _host.show()
        _host.raise_()
        _host.activateWindow()
        return
    _current, _host = None, None
    _open_in("dock" if _dock_mode() else "window")


def toggle_main_window() -> None:
    """Open Ankisstant, or hide it if it's already on screen.

    Only the sidebar button uses this — `open_main_window()` keeps its
    open-or-focus behaviour, because a menu item or a hand-off from another tool
    that closed the panel would be baffling.

    Hiding, never destroying: the queues, the panel cache and any half-typed
    form have to survive being dismissed, exactly as they do when the free
    window is closed with its own button.
    """
    global _host
    if _host is not None and _is_live(_host) and _host.isVisible():
        _host.hide()
        # The dock's visibilityChanged does this for us; a hidden QDialog emits
        # nothing, so tell the button ourselves or it stays lit over a panel
        # that isn't there.
        if not isinstance(_host, AnkisstantDock):
            _sync_sidebar(False)
        return
    open_main_window()


def _open_in(mode: str, body: AnkisstantBody | None = None) -> None:
    global _current, _host
    if mode == "dock":
        host = AnkisstantDock(body)
        host.setVisible(True)
    else:
        host = MainWindow(body)
        host.show()
        host.raise_()
        host.activateWindow()
    _current = host.body
    _host = host
    _current.on_popout = _toggle_mode
    _sync_sidebar(True)


def _toggle_mode() -> None:
    """Move the live UI between the dock and a window, keeping all its state.

    The body is re-parented rather than rebuilt: the panel cache, both queues, a
    half-typed Create form and any in-flight AI job all survive. That isn't
    optional — each tool caches its panel in a module global, so a rebuild would
    try to give the same widget two parents.
    """
    global _current, _host
    if _current is None or _host is None:
        return
    was_dock = isinstance(_host, AnkisstantDock)
    body, old_host = _current, _host

    # Detach before the old host is destroyed, or Qt takes the body with it.
    if was_dock:
        old_host._persist_width()
        old_host.setWidget(None)
    else:
        old_host.layout().removeWidget(body)
    body.setParent(None)

    _current, _host = None, None
    try:
        old_host.close()
        old_host.deleteLater()
    except Exception:
        pass

    # The rail vs. the wide sidebar is baked in at construction, so the chrome
    # is rebuilt — but the *panels* hold the state, and `adopt_from` carries
    # them and both queues across before anything is drawn.
    new_body = AnkisstantBody(compact=not was_dock, adopt_from=body)
    body.deleteLater()
    _open_in("window" if was_dock else "dock", new_body)
    save_synapse_config({"open_mode": "window" if was_dock else "dock"})


def _current_tool_key(body) -> str | None:
    try:
        showing = body.stack.currentWidget()
        key = getattr(showing, "_ank_key", None)
        if key:
            return key
        for key, widget in body._panel_cache.items():
            if widget is showing:
                return key
    except Exception:
        pass
    return None


_theme_sig = None


def _on_theme_change() -> None:
    """Repaint for an Anki light/dark switch or a SynapsePro accent change."""
    global _theme_sig
    try:
        _theme_sig = synapse.theme_signature()
        if _host is not None and _is_live(_host):
            synapse.apply_stylesheet(_host)
    except Exception:
        pass


def check_theme_drift() -> None:
    """Repaint if SynapsePro's colour theme changed while we weren't looking.

    Anki's ``theme_did_change`` covers light/dark, but SynapsePro's own colour
    themes don't fire anything — it sets a module global and repaints its own
    widgets by name. Changing one means visiting its settings and coming back,
    so checking on window activation catches it without polling on a timer.
    """
    global _theme_sig
    try:
        sig = synapse.theme_signature()
        if sig != _theme_sig:
            _on_theme_change()
    except Exception:
        pass


gui_hooks.theme_did_change.append(_on_theme_change)
gui_hooks.profile_will_close.append(_forget)
