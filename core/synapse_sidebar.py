"""Ankisstant's two buttons inside SynapsePro's launcher strip.

When SynapsePro is installed, its 55px icon rail on the edge of the window is
where features live — so that's where Ankisstant's front door goes, and the
"Ankisstant" text link in Anki's own top toolbar stands down (see
``__init__._on_top_toolbar_init_links``).

Two buttons, and only two:

    ▣  open Ankisstant       — accent colour; toggles the panel
    ＋  add a knowledge gap   — text colour; opens the Add-KG dialog on its own

The per-tool icons (Knowledge Gaps, QBank, Create, …) deliberately do **not**
go here — they live on Ankisstant's own rail inside the panel. A launcher strip
with ten things in it is a launcher strip nobody can read, and SynapsePro's own
features already own most of it.

Only the first button is accented. The ＋ is drawn in the text colour, so the
strip has one coloured thing of ours rather than two competing for the eye.

**Nothing in SynapsePro is modified.** The buttons are ordinary ``QPushButton``s
appended to the live ``SidebarWidget``'s layout at runtime. They carry the
``isMainIconButton`` property its stylesheet keys off, plus the ``normalIcon`` /
``whiteIcon`` pair its ``eventFilter`` swaps between on hover — set the first
without the last two and the button styles correctly but goes dead on hover
while every SynapsePro button beside it lights up.

The trade-off of injecting rather than being invited in: SynapsePro rebuilds its
whole sidebar widget per profile open, taking ours with it. So injection is
idempotent and re-runs — see ``_ensure``.
"""

from __future__ import annotations

import weakref

from aqt import gui_hooks, mw
from aqt.qt import (
    QByteArray, QIcon, QPixmap, QPushButton, QSize, Qt, QWidget,
)

from . import synapse
from .config import synapse_config

# SynapsePro's SidebarWidget sets this on itself; findChild is a stabler handle
# than reaching for the add-on's module globals.
SIDEBAR_OBJECT_NAME = "SidebarContent"

# Marks the widgets we own, so re-injection can tell "already there" from
# "SynapsePro rebuilt the strip and ours went with it".
_MARK = "ankisstant_sidebar_item"

_injected_into = None       # weakref to the SidebarWidget we last populated
_buttons: list = []         # kept alive; also how we recolour on theme change


# ----- Icons -----
#
# Drawn as SVG here rather than shipped as files: they have to be recoloured to
# whatever accent SynapsePro is on, and to white when a button is hovered or
# active to match its own icons. Generating them is less fuss than maintaining a
# folder of colour variants that would drift out of step with its themes anyway.

# A card with a spark on it — Ankisstant is card tooling, and it has to stay
# legible at 30px and clearly distinct from SynapsePro's own icons above it.
_SVG_CARD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect x="3.2" y="5" width="17.6" height="14" rx="2.6" '
    'fill="none" stroke="{c}" stroke-width="1.8"/>'
    '<path d="M8 9.6h5.2M8 13h3.4" stroke="{c}" stroke-width="1.6" '
    'stroke-linecap="round"/>'
    '<path d="M16.4 12.2l.62 1.68 1.68.62-1.68.62-.62 1.68-.62-1.68'
    '-1.68-.62 1.68-.62z" fill="{c}"/></svg>'
)

_SVG_PLUS = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path d="M12 5.6v12.8M5.6 12h12.8" stroke="{c}" stroke-width="2.1" '
    'stroke-linecap="round"/></svg>'
)

# SynapsePro's BUTTON_ICON_SIZE is 30; render at 2x so it stays crisp on Retina.
_ICON_PX = 60


def _icon(svg_template: str, colour: str):
    """An SVG string rendered into a QIcon at the strip's icon size."""
    svg = svg_template.format(c=colour).encode("utf-8")
    # QSvgRenderer draws at whatever size we ask for; QPixmap's SVG loader only
    # honours the viewBox, so a 24px icon would come out blurry on the button.
    # QSvgRenderer lives in PyQt6.QtSvg, which aqt.qt does NOT re-export.
    try:
        from PyQt6.QtSvg import QSvgRenderer
        from aqt.qt import QPainter
        pix = QPixmap(_ICON_PX, _ICON_PX)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            QSvgRenderer(QByteArray(svg)).render(painter)
        finally:
            painter.end()
        return QIcon(pix)
    except Exception:
        pass
    try:
        pix = QPixmap()
        if not pix.loadFromData(QByteArray(svg), "SVG"):
            return None
        return QIcon(pix)
    except Exception:
        return None


def _accent() -> str:
    # The token SynapsePro colours its own feature icons with, so ours sits in
    # the strip rather than next to it.
    return synapse.color("blue_accent", "#0071D3")


def _ink() -> str:
    """The un-accented icon colour — dark on a light theme, light on a dark one."""
    return synapse.color("text", "#1d1d1f")


# Which of the two each button paints itself with, resolved fresh every time so a
# theme switch (SynapsePro's accent, or Anki's light/dark) is picked up live.
_ACCENT = "accent"
_INK = "ink"


def _colour_for(role: str) -> str:
    return _accent() if role == _ACCENT else _ink()


# ----- Buttons -----

def _paint(btn, sidebar=None) -> None:
    """(Re)generate a button's normal and hover icons from the live palette.

    SynapsePro's eventFilter swaps between the ``normalIcon`` and ``whiteIcon``
    dynamic properties on enter/leave, and its ``_update_button_active_state``
    uses the same pair for the checked state — so both must be real QIcons or
    the button simply stops responding to the mouse while its neighbours don't.
    """
    svg = getattr(btn, "_ank_svg", None)
    if svg is None:
        return
    normal = _icon(svg, _colour_for(getattr(btn, "_ank_role", _ACCENT)))
    white = _icon(svg, "#ffffff")
    if normal is None:
        btn.setText("?")
        return
    btn.setProperty("normalIcon", normal)
    btn.setProperty("whiteIcon", white if white is not None else normal)
    btn.setIcon(white if (btn.isCheckable() and btn.isChecked() and white) else normal)


def _make_button(svg: str, tooltip: str, on_click, role: str = _ACCENT,
                 checkable: bool = False, sidebar=None) -> "QPushButton":
    btn = QPushButton()
    btn.setToolTip(tooltip)
    btn.setFlat(True)
    btn.setCheckable(checkable)
    # The property SynapsePro's sidebar stylesheet selects on. Without it the
    # button is an unstyled grey lump in the middle of its strip.
    btn.setProperty("isMainIconButton", True)
    setattr(btn, _MARK, True)
    btn.setFixedSize(QSize(45, 40))     # SIDEBAR_WIDTH - 10, BUTTON_ICON_SIZE + 10
    btn.setIconSize(QSize(30, 30))
    btn._ank_svg = svg                  # for recolouring on theme change
    btn._ank_role = role
    _paint(btn)
    # The hover swap is driven by the SidebarWidget's own eventFilter, which it
    # installs on the buttons it creates. Ours has to ask for it.
    if sidebar is not None:
        try:
            btn.installEventFilter(sidebar)
        except Exception:
            pass
    btn.clicked.connect(lambda _checked=False: _safely(on_click))
    return btn


def _safely(fn) -> None:
    """A click must never propagate an exception into SynapsePro's strip."""
    try:
        fn()
    except Exception as e:
        print(f"[ankisstant] sidebar action failed: {e}")


def _on_open() -> None:
    from ..ui.main_window import open_main_window
    open_main_window()


def _on_add_kg() -> None:
    from ..tools.knowledge_gaps import open_add_kg_dialog, _refresh_open_panel
    open_add_kg_dialog()
    try:
        _refresh_open_panel()
    except Exception:
        pass


# ----- Injection -----

def _find_sidebar():
    try:
        return mw.findChild(QWidget, SIDEBAR_OBJECT_NAME)
    except Exception:
        return None


def _already_ours(sidebar) -> bool:
    """True when our buttons are still in this sidebar."""
    try:
        for child in sidebar.findChildren(QPushButton):
            if getattr(child, _MARK, False):
                return True
    except Exception:
        pass
    return False


def _enabled() -> bool:
    return (bool(synapse_config().get("sidebar_buttons", True))
            and synapse.synapse_available())


def injected_live() -> bool:
    """Are our buttons in the strip *right now*?

    This is what gates hiding the top toolbar link, and it is deliberately the
    live fact rather than a flag set once at startup. ``top_toolbar_did_init_links``
    fires on every toolbar redraw; if injection had failed and we answered from a
    stale flag, the link would disappear on the first redraw and leave no way in
    at all bar the Tools menu.
    """
    try:
        if not _enabled():
            return False
        sidebar = _injected_into() if _injected_into is not None else None
        if sidebar is None:
            return False
        return _already_ours(sidebar)
    except Exception:
        return False


def _separator_index(layout) -> int:
    """Where SynapsePro's strip stops being features and starts being tools.

    Its layout is: logo, stretch, **feature buttons**, stretch, timer, the
    ``bottomSeparatorLine`` rule, then the bottom section — the timer icon, the
    music button, and whatever other add-ons have appended (AnkiBlitz's lightning
    lives here). Returns -1 if the rule isn't there.
    """
    for i in range(layout.count()):
        item = layout.itemAt(i)
        w = item.widget() if item is not None else None
        if w is None or getattr(w, _MARK, False):
            continue
        try:
            if w.objectName() == "bottomSeparatorLine":
                return i
        except Exception:
            pass
    return -1


def _feature_group_end(layout, before: int) -> int:
    """Index just after the last of SynapsePro's own feature buttons, or -1.

    Found by the ``isMainIconButton`` property rather than a hardcoded index, so
    a reordered or extended strip upstream can't strand us. Our own marked
    widgets are skipped, or a re-injection would chase itself down the strip.
    """
    last = -1
    end = layout.count() if before < 0 else before
    for i in range(end):
        item = layout.itemAt(i)
        w = item.widget() if item is not None else None
        if w is None or getattr(w, _MARK, False):
            continue
        try:
            if w.property("isMainIconButton"):
                last = i
        except Exception:
            pass
    return last + 1 if last >= 0 else -1


def _inject(sidebar) -> bool:
    """Ankisstant with the features, ＋ at the head of the bottom section.

    The two buttons aren't the same kind of thing, so they don't belong in the
    same place. Ankisstant is a front door like the AI assistant and the mind
    map, and sits with them. ＋ is a quick action that opens a dialog and nothing
    else; it goes at the top of the bottom section, above the other add-ons'
    entries, where it reads as a shortcut rather than a fourth app.
    """
    global _injected_into, _buttons
    layout = sidebar.layout()
    if layout is None:
        return False

    open_btn = _make_button(
        _SVG_CARD, "Ankisstant — knowledge gaps, cards and search",
        # Ink, not accent: it sits among SynapsePro's own feature buttons, and a
        # guest in that row shouldn't be the loudest thing in it. The accent
        # goes to ＋ instead, which is the one that does something on click
        # rather than opening a panel.
        _on_open, _INK, checkable=True, sidebar=sidebar,
    )
    add_btn = _make_button(
        _SVG_PLUS, "Add a knowledge gap",
        _on_add_kg, _ACCENT, checkable=False, sidebar=sidebar,
    )

    sep = _separator_index(layout)
    feature_end = _feature_group_end(layout, sep)

    # Bottom-up: inserting the lower one first keeps the upper index valid.
    if sep >= 0:
        layout.insertWidget(sep + 1, add_btn)
    else:
        layout.addWidget(add_btn)

    if feature_end >= 0:
        layout.insertWidget(feature_end, open_btn)
    else:
        # No recognisable feature group — keep the pair together at the bottom
        # rather than guessing at a position in someone else's strip.
        layout.insertWidget(layout.indexOf(add_btn), open_btn)

    _buttons = [open_btn, add_btn]
    _injected_into = weakref.ref(sidebar)
    return True


def _ensure() -> None:
    """Put our buttons in the strip if they aren't already there.

    Cheap enough to call often, and it needs to be: SynapsePro recreates its
    whole sidebar widget per profile open, taking ours with it.
    """
    try:
        if not _enabled():
            return
        sidebar = _find_sidebar()
        if sidebar is None or _already_ours(sidebar):
            return
        if _inject(sidebar):
            # The toolbar was drawn before this — SynapsePro builds its strip
            # behind a 300ms timer, so `top_toolbar_did_init_links` has already
            # fired and answered "not injected yet", leaving the link in place
            # with nothing to trigger another redraw. Ask for one.
            try:
                mw.toolbar.draw()
            except Exception:
                pass
    except Exception as e:
        print(f"[ankisstant] sidebar injection failed: {e}")


def refresh_icons() -> None:
    """Recolour — SynapsePro's accent or Anki's light/dark changed."""
    try:
        for btn in _buttons:
            _paint(btn)
    except Exception:
        pass


def sync_open_state(visible: bool) -> None:
    """Reflect the panel's open/closed state on the Ankisstant button.

    SynapsePro does this for its own features by connecting each dock's
    ``visibilityChanged``; its ``sync_dock_button`` helper only knows about docks
    in its hardcoded feature map, so we drive our own button here instead. Same
    six lines, no SynapsePro edit.
    """
    try:
        if not _buttons:
            return
        btn = _buttons[0]
        if not btn.isCheckable() or btn.isChecked() == bool(visible):
            return
        btn.setChecked(bool(visible))
        icon = btn.property("whiteIcon" if visible else "normalIcon")
        if isinstance(icon, QIcon):
            btn.setIcon(icon)
        btn.update()
    except Exception:
        pass


def remove() -> None:
    """Take our buttons back out — the integration was switched off."""
    global _buttons, _injected_into
    sidebar = _injected_into() if _injected_into is not None else None
    try:
        if sidebar is not None:
            for child in list(sidebar.findChildren(QWidget)):
                if getattr(child, _MARK, False):
                    child.setParent(None)
                    child.deleteLater()
    except Exception:
        pass
    _buttons = []
    _injected_into = None


def apply_settings_change() -> None:
    """Add or drop the buttons right after Settings closes, no restart needed."""
    try:
        synapse.reset_cache()
        if _enabled():
            _ensure()
            refresh_icons()
        else:
            remove()
        # The toolbar link is gated on injected_live(), so a change either way
        # needs the toolbar redrawn to take effect.
        try:
            mw.toolbar.draw()
        except Exception:
            pass
    except Exception:
        pass


def _on_state_change(new_state, old_state) -> None:
    _ensure()


_hooks_registered = False


def register() -> None:
    """Called once per profile open.

    The injection retries run every time — SynapsePro recreates its sidebar
    widget per profile, so ours have to go back in. The *hooks* are registered
    only once: gui_hooks lists are global and appending on every profile switch
    would stack duplicate callbacks for the rest of the session.
    """
    global _hooks_registered
    # SynapsePro builds its sidebar behind a 300ms timer at profile open, and
    # add-on load order isn't guaranteed — so retry a few times rather than
    # racing it once and losing.
    for delay in (400, 1200, 3000):
        try:
            mw.progress.single_shot(delay, _ensure, False)
        except Exception:
            pass
    if _hooks_registered:
        return
    # Belt and braces: cheap, and covers a rebuild we didn't predict.
    gui_hooks.state_did_change.append(_on_state_change)
    gui_hooks.theme_did_change.append(refresh_icons)
    _hooks_registered = True
