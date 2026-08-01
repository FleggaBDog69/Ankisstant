# Ankisstant's main UI — a nav sidebar plus a stacked panel area.
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
# the host, because every consumer elsewhere in the add-on is duck-typed on the
# queue API (gap_queue, browse_queue, refresh_queue_badge, refresh_tool_queue,
# show_create_tool, show_browse_tool) rather than on it being a window.
#
# The pop-out button re-parents the body rather than rebuilding it, which is not
# just an optimisation: each tool module caches its panel in a module global
# (see tools/card_creator.get_panel), so a rebuild would hand the same widget to
# two parents. It also means a half-filled Create form survives the move.

from __future__ import annotations

import importlib

from aqt import gui_hooks, mw
from aqt.qt import (
    QDialog, QDockWidget, QEvent, QFrame, QHBoxLayout, QLabel, QPushButton, QSize,
    QSizePolicy, QStackedWidget, Qt, QToolButton, QVBoxLayout, QWidget,
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
    ("browse",           "AI Browse",       f"{_PKG}.tools.browse"),
    ("card_creator",     "AI Create",       f"{_PKG}.tools.card_creator"),
    ("update_by_tag",    "Update by Tag",   f"{_PKG}.tools.update_by_tag"),
    ("lecture",          "AI Lecture",      f"{_PKG}.tools.lecture"),
]

# Rail icons, one per tool, drawn as SVG and recoloured from the live palette —
# same reasoning as core/synapse_sidebar: they have to track whatever accent
# SynapsePro is on, and a folder of colour variants would drift out of step.
# `{c}` is substituted with the icon colour.
_TOOL_SVG: dict[str, str] = {
    # A checklist with one item still open — the things you don't know yet.
    # Deliberately *not* a magnifier: AI Browse two rows down is one, and at
    # 20px two magnifiers are indistinguishable.
    "knowledge_gaps":
        '<path d="M4.5 6.6 6.2 8.3 9.4 5.1M4.5 12 6.2 13.7 9.4 10.5" fill="none" '
        'stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
        '<rect x="4.2" y="15.4" width="5.6" height="5.2" rx="1.4" fill="none" '
        'stroke="{c}" stroke-width="1.8" stroke-dasharray="2.6 2"/>'
        '<path d="M12.6 6.6h7.2M12.6 12h7.2M12.6 18h5" stroke="{c}" '
        'stroke-width="1.8" stroke-linecap="round"/>',
    # A bank of questions: stacked bars with a question mark.
    "qbank":
        '<rect x="3" y="4.5" width="18" height="15" rx="2.4" fill="none" '
        'stroke="{c}" stroke-width="1.8"/>'
        '<path d="M9.4 9.6a2.6 2.6 0 1 1 2.6 2.6v1.4" fill="none" stroke="{c}" '
        'stroke-width="1.8" stroke-linecap="round"/>'
        '<circle cx="12" cy="16.4" r="1" fill="{c}"/>',
    # Magnifier with a spark — AI search.
    "browse":
        '<circle cx="10.5" cy="10.5" r="6.2" fill="none" stroke="{c}" stroke-width="1.8"/>'
        '<path d="M15.2 15.2 20.5 20.5" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>'
        '<path d="M10.5 7.2l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7z" fill="{c}"/>',
    # A card with a plus — make one.
    "card_creator":
        '<rect x="3" y="5" width="18" height="14" rx="2.4" fill="none" '
        'stroke="{c}" stroke-width="1.8"/>'
        '<path d="M12 9v6M9 12h6" stroke="{c}" stroke-width="1.8" stroke-linecap="round"/>',
    # A tag with a pencil stroke.
    "update_by_tag":
        '<path d="M4 4.6h7.6l8 8-7 7-8-8z" fill="none" stroke="{c}" '
        'stroke-width="1.8" stroke-linejoin="round"/>'
        '<circle cx="8.4" cy="9" r="1.5" fill="{c}"/>',
    # A lecture slide on a stand.
    "lecture":
        '<rect x="3.2" y="4" width="17.6" height="11.5" rx="2" fill="none" '
        'stroke="{c}" stroke-width="1.8"/>'
        '<path d="M12 15.5V20M8.4 20h7.2" stroke="{c}" stroke-width="1.8" '
        'stroke-linecap="round"/>'
        '<path d="M7.4 8.4h7M7.4 11.4h4" stroke="{c}" stroke-width="1.5" '
        'stroke-linecap="round"/>',
}

_RAIL_SVG_EXTRA: dict[str, str] = {
    "add_kg": '<path d="M12 5.6v12.8M5.6 12h12.8" stroke="{c}" stroke-width="2.1" '
              'stroke-linecap="round"/>',
    # A gear, not a sun: the teeth have to be short and thick relative to a
    # large rim, or short radiating strokes just read as rays.
    "settings":
        '<circle cx="12" cy="12" r="7.4" fill="none" stroke="{c}" stroke-width="1.8"/>'
        '<circle cx="12" cy="12" r="2.6" fill="none" stroke="{c}" stroke-width="1.8"/>'
        '<path d="M12 2.9v2.2M12 18.9v2.2M21.1 12h-2.2M5.1 12H2.9'
        'M18.4 5.6l-1.55 1.55M7.15 16.85 5.6 18.4M18.4 18.4l-1.55-1.55'
        'M7.15 7.15 5.6 5.6" stroke="{c}" stroke-width="2.4" stroke-linecap="round"/>',
    # Arrow leaving a box — pop out into a window of its own.
    "popout":
        '<path d="M13.5 4.5H19.5V10.5" fill="none" stroke="{c}" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M19.5 4.5 11.5 12.5" stroke="{c}" stroke-width="1.9" '
        'stroke-linecap="round"/>'
        '<path d="M18 14.5v4a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11'
        'A1.5 1.5 0 0 1 5.5 6h4" fill="none" stroke="{c}" stroke-width="1.9" '
        'stroke-linecap="round" stroke-linejoin="round"/>',
}

_SVG_WRAP = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
             '{body}</svg>')

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


def _rail_icon(body_svg: str, colour: str, badge: str | None = None):
    """An SVG fragment rendered to a QIcon, optionally with a queue dot.

    The dot is *never* the whole story — every caller also puts the count in the
    button's tooltip. A mark you can only read by its colour isn't a state.
    """
    from aqt.qt import QByteArray, QIcon, QPixmap
    svg = _SVG_WRAP.format(body=body_svg.format(c=colour))
    if badge:
        # A filled disc in the top-right corner of the 24-unit viewBox.
        svg = svg.replace(
            "</svg>",
            f'<circle cx="19.4" cy="4.6" r="3.5" fill="{badge}" '
            f'stroke="none"/></svg>',
        )
    data = QByteArray(svg.encode("utf-8"))
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from aqt.qt import QPainter
        pix = QPixmap(48, 48)          # 24px at 2x for Retina
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            QSvgRenderer(data).render(painter)
        finally:
            painter.end()
        return QIcon(pix)
    except Exception:
        try:
            pix = QPixmap()
            if not pix.loadFromData(data, "SVG"):
                return None
            return QIcon(pix)
        except Exception:
            return None


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
        self._nav_buttons: dict[str, QWidget] = {}
        self._rail_buttons: list = []
        # Callback the host installs so the pop-out button can reach it without
        # the body knowing what a dock is.
        self.on_popout = None

        # Session-scoped queue of gaps waiting to become cards. Items are
        # dicts: {"title": str, "kg_id": str | None, "stem_html": str | None,
        # "notes": str | None}. The Knowledge Gaps page pushes here; Create
        # pops one at a time, and a kg_id (when set) is marked done in the
        # KG store on successful Add.
        self.gap_queue: list[dict] = []
        # Session-scoped queue of KGs waiting to be looked up in Browse.
        # Items are full KG dicts (id, title, fields, tags, type). The
        # Knowledge Gaps page pushes here; Browse works through them and a
        # successful Tag & Unsuspend marks the KG done and advances.
        self.browse_queue: list[dict] = []
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
        self.refresh_queue_badge()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self, select: str | None = None) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_rail() if self._compact else self._build_sidebar())

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Placeholder welcome page so the window has content on first open.
        welcome = QWidget()
        wl = QVBoxLayout(welcome)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wl.addWidget(QLabel("<h2 style='margin:0'>Welcome to Ankisstant</h2>"))
        msg = QLabel("Pick a tool from the sidebar to get started.")
        msg.setStyleSheet("color: gray;")
        wl.addWidget(msg)
        self.stack.addWidget(welcome)
        root.addWidget(self.stack, 1)

        # Land on the tool that was showing before a pop-out, else the first.
        wanted = [select] if select in self._nav_buttons else []
        for key, _label, _path in TOOLS:
            if key in self._nav_buttons:
                wanted.append(key)
        if wanted:
            key = wanted[0]
            self._nav_buttons[key].setChecked(True)
            self._show_tool(key)

    def _build_sidebar(self) -> QWidget:
        """The wide, text-labelled nav — unchanged from before the dock existed."""
        sidebar = QFrame()
        sidebar.setFrameShape(QFrame.Shape.StyledPanel)
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet(
            "QFrame { background-color: %s; border-right: 1px solid %s; }"
            % (synapse.color("bg", "palette(window)"),
               synapse.color("grey_light", "palette(mid)"))
        )
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(8, 14, 8, 8)
        side_layout.setSpacing(4)

        title = QLabel("<b style='font-size:14px'>Ankisstant</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setStyleSheet("padding: 4px 6px 10px; opacity: 0.85;")
        side_layout.addWidget(title)

        # `palette(highlight)` is Anki's accent; with SynapsePro present we use
        # its accent instead so the nav strip matches the rest of the shell.
        sel_bg = synapse.color("blue", "palette(highlight)")
        sel_fg = synapse.color("surface", "palette(highlighted-text)")
        hover = synapse.color("hover_subtle", "rgba(127,127,127,0.12)")
        for key, label, _path in TOOLS:
            if not _tool_visible(key):
                continue
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setMinimumHeight(34)
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 4px 10px; border: none; "
                "background: transparent; }"
                f"QPushButton:checked {{ background: {sel_bg}; color: {sel_fg}; "
                "border-radius: 6px; }"
                f"QPushButton:hover:!checked {{ background: {hover}; border-radius: 6px; }}"
                "QPushButton:disabled { color: rgba(127,127,127,0.6); }"
            )
            btn.clicked.connect(lambda _checked=False, k=key: self._show_tool(k))
            self._nav_buttons[key] = btn
            side_layout.addWidget(btn)

        side_layout.addStretch(1)

        extras = []
        # The way back from the pop-out. The rail has this as an icon; without
        # it here the trip is one-way, since the wide sidebar is exactly what
        # you get *after* popping out.
        if synapse.synapse_available():
            extras.append(("⧉  Dock to side panel", self._request_popout))
        extras += [("＋  Add KG", self._open_add_kg),
                   ("⚙  Settings", self._open_settings)]

        for text, slot in extras:
            b = QPushButton(text)
            b.setMinimumHeight(32)
            b.setStyleSheet(
                "QPushButton { text-align: left; padding: 4px 10px; border: none; "
                "background: transparent; }"
                f"QPushButton:hover {{ background: {hover}; border-radius: 6px; }}"
            )
            b.clicked.connect(slot)
            side_layout.addWidget(b)
        return sidebar

    def _build_rail(self) -> QWidget:
        """The narrow icon rail used in the dock.

        Every button is icon-only with a tooltip, so nothing here is conveyed by
        shape alone either — hovering names it, and the queue counts are spelled
        out in the tooltip rather than left to the dot.
        """
        rail = QFrame()
        rail.setFrameShape(QFrame.Shape.NoFrame)
        rail.setFixedWidth(44)
        rail.setStyleSheet(
            "QFrame { background-color: %s; border-right: 1px solid %s; }"
            % (synapse.color("bg", "palette(window)"),
               synapse.color("grey_light", "palette(mid)"))
        )
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(4)

        for key, label, _path in TOOLS:
            if not _tool_visible(key):
                continue
            btn = self._rail_button(_TOOL_SVG.get(key, ""), label, accent=True)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.clicked.connect(lambda _checked=False, k=key: self._show_tool(k))
            self._nav_buttons[key] = btn
            layout.addWidget(btn)

        layout.addStretch(1)

        pop = self._rail_button(_RAIL_SVG_EXTRA["popout"],
                                "Open in a separate window")
        pop.clicked.connect(self._request_popout)
        layout.addWidget(pop)

        add = self._rail_button(_RAIL_SVG_EXTRA["add_kg"], "Add a knowledge gap")
        add.clicked.connect(self._open_add_kg)
        layout.addWidget(add)

        gear = self._rail_button(_RAIL_SVG_EXTRA["settings"], "Ankisstant settings")
        gear.clicked.connect(self._open_settings)
        layout.addWidget(gear)
        return rail

    def _rail_button(self, svg: str, tooltip: str, accent: bool = False) -> QToolButton:
        btn = QToolButton()
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.setFixedSize(QSize(36, 32))
        btn.setIconSize(QSize(20, 20))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        btn._ank_svg = svg
        btn._ank_accent = accent
        btn._ank_badge = False
        self._paint_rail_button(btn)
        self._rail_buttons.append(btn)
        return btn

    def _paint_rail_button(self, btn) -> None:
        svg = getattr(btn, "_ank_svg", None)
        if not svg:
            return
        colour = (synapse.color("blue_accent", "#0071D3")
                  if getattr(btn, "_ank_accent", False)
                  else synapse.color("text_muted", "#6b6b70"))
        badge = (synapse.color("blue", "#3b82f6")
                 if getattr(btn, "_ank_badge", False) else None)
        icon = _rail_icon(svg, colour, badge)
        if icon is not None:
            btn.setIcon(icon)
        else:
            btn.setText(btn.toolTip()[:1])

    def refresh_theme(self) -> None:
        """Re-render everything that baked a colour in — SynapsePro's accent or
        Anki's light/dark changed under a live window."""
        try:
            # Only our own rail buttons — a findChildren sweep would also catch
            # QToolButtons living inside the tool panels and repaint them with
            # a rail icon.
            for btn in self._rail_buttons:
                self._paint_rail_button(btn)
            self.refresh_queue_badge()
        except Exception as e:
            print(f"[ankisstant] refresh_theme failed: {e}")

    # ── queue handoff (Browse → Create) ──────────────────────────────────────

    def refresh_queue_badge(self) -> None:
        """Show the Create and Browse queue counts.

        In the wide sidebar that's a suffix on the button label. On the rail
        there's no room for text, so the icon gets a dot and the **count goes in
        the tooltip** — the dot says "something's queued", the tooltip says what
        and how many. Neither state is left to colour alone.
        """
        try:
            from ..tools import create_jobs
            ready = create_jobs.ready_count()
        except Exception:
            ready = 0

        n_gaps = len(self.gap_queue)
        create_tips = []
        if n_gaps:
            create_tips.append(f"{n_gaps} gap{'s' if n_gaps != 1 else ''} queued for Create")
        if ready:
            create_tips.append(f"{ready} generation{'s' if ready != 1 else ''} ready to review")

        n_browse = len(self.browse_queue)
        browse_tip = (f"{n_browse} KG{'s' if n_browse != 1 else ''} queued for Browse"
                      if n_browse else "")

        self._set_badge("card_creator", "AI Create",
                        (f"  ●{n_gaps}" if n_gaps else "") + (f"  ✓{ready}" if ready else ""),
                        bool(n_gaps or ready), " · ".join(create_tips))
        self._set_badge("browse", "AI Browse",
                        f"  ●{n_browse}" if n_browse else "",
                        bool(n_browse), browse_tip)

    def _set_badge(self, key: str, label: str, suffix: str,
                   dot: bool, tooltip: str) -> None:
        btn = self._nav_buttons.get(key)
        if btn is None:
            return
        if self._compact:
            btn._ank_badge = dot
            self._paint_rail_button(btn)
            # The rail's tooltip must always name the tool as well as the
            # count, since the label isn't on screen.
            btn.setToolTip(f"{label} — {tooltip}" if tooltip else label)
        else:
            btn.setText(label + suffix)
            btn.setToolTip(tooltip)

    def refresh_tool_queue(self, key: str) -> None:
        """Re-sync one tool's queue view WITHOUT switching to it.

        Queueing a gap shouldn't yank you onto another screen, but the target
        panel — if it's already been built — would otherwise show a stale queue
        until the next time you visited it.
        """
        widget = self._panel_cache.get(key)
        if widget is None:
            return          # not built yet; it'll read the queue when it is
        from aqt.qt import QScrollArea
        target = widget.widget() if isinstance(widget, QScrollArea) else widget
        if hasattr(target, "refresh_queue_state"):
            try:
                target.refresh_queue_state(self)
            except Exception as e:
                print(f"[ankisstant] {key}.refresh_queue_state failed: {e}")

    def show_create_tool(self) -> None:
        """Programmatic switch to the Create tool — used after Browse hands off
        gaps."""
        btn = self._nav_buttons.get("card_creator")
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
        self._show_tool("card_creator")

    def show_browse_tool(self) -> None:
        """Programmatic switch to the Browse tool — used after the KG page
        queues gaps for Browse."""
        btn = self._nav_buttons.get("browse")
        if btn is not None and btn.isEnabled():
            btn.setChecked(True)
        self._show_tool("browse")

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

    def _open_add_kg(self) -> None:
        try:
            from ..tools.knowledge_gaps import open_add_kg_dialog
            open_add_kg_dialog()
            # If the KG panel is loaded, refresh.
            from ..tools import knowledge_gaps as kg_tool
            kg_tool._refresh_open_panel()
        except Exception as e:
            print(f"[ankisstant] open Add KG failed: {e}")

    def _open_settings(self) -> None:
        from .settings import SettingsDialog
        dlg = SettingsDialog(self.window())
        if dlg.exec():
            # Refresh nav-button enabled states for tools whose sidebar entry
            # already exists. Newly (in)visible tools need a restart to add
            # or remove their entry — see the restart hint in Settings.
            for key, _label, _path in TOOLS:
                btn = self._nav_buttons.get(key)
                if btn is not None:
                    btn.setEnabled(_tool_visible(key))
            self._panel_cache.clear()
            try:
                from ..core import synapse_sidebar
                synapse_sidebar.apply_settings_change()
            except Exception:
                pass
            self.refresh_theme()


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
        try:
            mw.resizeDocks([self], [width], Qt.Orientation.Horizontal)
        except Exception:
            pass
        self.visibilityChanged.connect(self._on_visibility)

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
            if _current is not None:
                _current.refresh_theme()
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
