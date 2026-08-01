"""The one place in Ankisstant that knows SynapsePro exists.

SynapsePro keeps its whole look in a ``theme.py`` at its add-on root: a
``palette(night) -> dict`` of semantic colour tokens plus a ``FONT_FAMILY``
constant. If it's installed, Ankisstant borrows those tokens so the two add-ons
read as one product — including when the user switches SynapsePro's colour
theme, since ``palette()`` is asked fresh every time rather than cached.

Everything here is a **soft bridge**, per the house rule: SynapsePro is never a
dependency, every lookup is ``try/except``-wrapped, and when it's absent (or
broken, or the user has switched the bridge off) every call falls back to the
colour Ankisstant has always used. Standalone Ankisstant is unchanged.

Ported from AnkiBlitz's ``engine/theme_bridge.py``, which paid for most of the
bugs below once already. Three things worth knowing before you edit this:

- **Don't import SynapsePro by name.** Its ``manifest.json`` declares the package
  as ``SynapsePro1``, a git checkout is usually symlinked in as ``SynapsePro``,
  and an AnkiWeb install lands in a numeric folder. The module is resolved at
  runtime by reading manifests, not by a literal import.
- **Don't hold on to the palette.** SynapsePro's active theme is module state it
  rewrites when the user picks a different colour; a cached dict would go stale
  until the next Anki restart.
- **No import-time colour constants.** Anything like ``_STYLE = f"…{color(…)}"``
  at module level freezes the palette at Anki launch, and theme switching then
  silently does nothing until restart. Make them functions. This is the bug that
  actually ships.
"""

from __future__ import annotations

import hashlib
import importlib
import os

from aqt import mw

from .config import synapse_config

# The add-on's advertised name in its manifest — stable across the package-name
# variants above, and what we actually match on.
SYNAPSE_NAME = "SynapsePro"

# Resolved SynapsePro theme module, or None. Sentinel distinguishes "not looked
# yet" from "looked and it isn't there", so a missing add-on costs one scan.
_UNSET = object()
_theme_mod = _UNSET


def _find_theme_module():
    """Locate SynapsePro's theme module by manifest name. None if absent."""
    try:
        packages = mw.addonManager.allAddons()
    except Exception:
        return None
    for pkg in packages:
        try:
            meta = mw.addonManager.addon_meta(pkg)
            name = getattr(meta, "human_name", None)
            name = name() if callable(name) else name
            if name != SYNAPSE_NAME and pkg != SYNAPSE_NAME:
                continue
            if getattr(meta, "enabled", True) is False:
                continue
            return importlib.import_module(f"{pkg}.theme")
        except Exception:
            continue
    return None


def _theme():
    global _theme_mod
    if _theme_mod is _UNSET:
        try:
            _theme_mod = _find_theme_module()
        except Exception:
            _theme_mod = None
    return _theme_mod


def reset_cache() -> None:
    """Forget the resolved module — for after an add-on is enabled or installed."""
    global _theme_mod
    _theme_mod = _UNSET


def synapse_available() -> bool:
    """True when SynapsePro is installed, enabled, and exposes its palette."""
    return _theme() is not None


def theme_module():
    """SynapsePro's ``theme`` module, or None — for callers that need its path
    or its package name (icon files, the settings entry point)."""
    return _theme()


def package_name() -> str:
    """SynapsePro's actual add-on package name, whatever the folder is called."""
    mod = _theme()
    return mod.__name__.split(".")[0] if mod is not None else ""


def constants_module():
    """SynapsePro's ``constants`` module, or None.

    Its ``place_feature_dock`` is registered onto this at runtime and is the one
    real extension point it offers an outside add-on — see ``ui/main_window``.
    """
    pkg = package_name()
    if not pkg:
        return None
    try:
        return importlib.import_module(f"{pkg}.constants")
    except Exception:
        return None


def _bridge_on() -> bool:
    return bool(synapse_config().get("theme_bridge", True))


def _night() -> bool:
    """Anki's *resolved* dark state.

    ``theme_manager.night_mode`` accounts for "follow system"; ``pm.night_mode()``
    is only the stored preference, so it reads light while the OS is dark.
    SynapsePro itself uses the latter — don't copy that bit.
    """
    try:
        from aqt.theme import theme_manager
        return bool(theme_manager.night_mode)
    except Exception:
        try:
            return bool(mw.pm.night_mode())
        except Exception:
            return False


def tokens() -> dict:
    """SynapsePro's palette for the current light/dark state, or {} without it.

    Asked fresh every call on purpose — see the module docstring.
    """
    if not _bridge_on():
        return {}
    mod = _theme()
    if mod is None:
        return {}
    try:
        pal = mod.palette(_night())
        return pal if isinstance(pal, dict) else {}
    except Exception:
        return {}


def color(key: str, fallback: str) -> str:
    """One token, with Ankisstant's own colour as the fallback.

    Written as ``color("blue", "#4a90d9")`` so every call site carries the value
    it used before the bridge existed — which is what makes "standalone looks
    identical" checkable by reading the diff.
    """
    try:
        value = tokens().get(key)
    except Exception:
        value = None
    return value if isinstance(value, str) and value else fallback


def tint(key: str, alpha: float, fallback: str) -> str:
    """A palette token as ``rgba(r, g, b, alpha)``, with our own literal as the
    fallback.

    Several Ankisstant surfaces wash a colour over whatever's behind them — the
    "queued" boxes in Create and Browse, the setup banner, list selections — and
    a flat hex would hide the widget underneath. SynapsePro's tokens are opaque
    hex, so they get unpacked here rather than at eleven call sites.

    Anything that isn't a plain ``#rrggbb`` / ``#rgb`` (SynapsePro's border
    tokens are whole CSS ``border`` values, and "none" is a legal one) falls
    through to the caller's literal untouched.
    """
    raw = color(key, "")
    if not raw.startswith("#"):
        return fallback
    body = raw[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) != 6:
        return fallback
    try:
        r, g, b = (int(body[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback
    return f"rgba({r}, {g}, {b}, {alpha})"


# ----- Status colours -----
#
# Ankisstant says ok / warning / error in green, amber and red all over the
# place. Rather than a `color(...)` call at every one of those sites, the
# mapping lives here once — including the compromise, so it's written down
# somewhere other than a commit message.
#
# **SynapsePro has no amber token.** `red` is the nearest thing that still reads
# as "pay attention", so warnings and errors converge on one colour when the
# bridge is on. That loses a real distinction, which is why every call site that
# uses these also says which it is *in words* — a warning that only differs from
# an error by being slightly more orange was never a good signal anyway.

def status_ok(fallback: str = "#3a9e6a") -> str:
    return color("green", fallback)


def status_warn(fallback: str = "#b85c00") -> str:
    return color("red", fallback)


def status_error(fallback: str = "#c0392b") -> str:
    return color("red", fallback)


def font_family(fallback: str) -> str:
    if not _bridge_on() or not synapse_config().get("match_font", False):
        return fallback
    mod = _theme()
    if mod is None:
        return fallback
    try:
        fam = getattr(mod, "FONT_FAMILY", None)
        return fam if isinstance(fam, str) and fam else fallback
    except Exception:
        return fallback


# ----- Webview side -----
#
# Ankisstant owns exactly two webview surfaces: the AI Lecture results list and
# the QBank heatmap fragment on the deck browser. Both style themselves with
# `var(--ank-NAME, <today's colour>)`. When SynapsePro is absent this block is
# empty, none of the vars resolve, and every fallback applies — i.e. exactly the
# old stylesheet.
#
# CSS var name -> (SynapsePro token, the literal Ankisstant used before)
_CSS_MAP = {
    # Surfaces and text
    "--ank-bg":         ("bg",          ""),
    "--ank-surface":    ("surface",     ""),
    "--ank-text":       ("text",        ""),
    "--ank-muted":      ("text_muted",  ""),
    "--ank-faint":      ("text_faint",  ""),
    "--ank-border":     ("grey_mid",    ""),
    "--ank-border-soft": ("grey_light", ""),
    "--ank-hover":      ("hover_subtle", ""),
    "--ank-selection":  ("selection_bg", ""),
    # Accents. The lecture results list uses four fixed accents today; they map
    # onto blue / green / red, with `blue_bright` kept for the one that must
    # stay distinct from `blue` under every SynapsePro colour theme (its themes
    # rewrite blue/blue_accent together, so those two collapse).
    "--ank-accent":     ("blue",         "#4a90d9"),
    "--ank-accent-alt": ("blue_bright",  ""),
    "--ank-ok":         ("green",        "#3c8f5a"),
    # No amber token exists in SynapsePro's palette. `red` is the nearest thing
    # that still reads as urgency; the loss of the amber "nearly" step is a
    # known compromise, same as AnkiBlitz's.
    "--ank-warn":       ("red",          "#b8860b"),
    "--ank-error":      ("red",          "#c05050"),
}


def css_vars() -> str:
    """A ``<style>:root{…}</style>`` block, or "" when there's nothing to theme."""
    pal = tokens()
    if not pal:
        return ""
    decls = []
    for var, (token, _fallback) in _CSS_MAP.items():
        value = pal.get(token)
        if isinstance(value, str) and value:
            decls.append(f"{var}:{value};")
    fam = font_family("")
    if fam:
        decls.append(f"--ank-font:{fam};")
    if not decls:
        return ""
    return "<style>:root{" + "".join(decls) + "}</style>"


# ----- Qt side -----

# Anki-neutral stand-ins, used only if SynapsePro's palette is missing a key.
_QSS_FALLBACKS = {
    "bg": "#f5f5f7", "surface": "#ffffff", "text": "#1d1d1f",
    "text_muted": "#86868b", "text_faint": "#aaaaaa",
    "grey_light": "#e5e5ea", "grey_mid": "#d1d1d6",
    "hover_subtle": "#f0f0f0", "selection_bg": "#e4f2ff",
    "blue": "#0071d3", "blue_hover": "#0062c4", "blue_pressed": "#004990",
    "blue_bright": "#007aff", "blue_accent": "#0071d3", "blue_border": "none",
}


class _WithFallbacks:
    """dict-style lookup that can't KeyError — ``c['thing']`` always yields a colour.

    Not a nicety: build the sheet with ``pal[key]`` instead and one renamed token
    upstream raises inside the f-string, the whole sheet collapses to ``""``, and
    the window silently un-themes with nothing in the log.
    """

    def __init__(self, palette: dict, fallbacks: dict):
        self._p = palette
        self._f = fallbacks

    def __getitem__(self, key: str) -> str:
        value = self._p.get(key)
        if isinstance(value, str) and value:
            return value
        return self._f.get(key, "inherit")


# ----- Spin-box arrows -----
#
# The moment a QSpinBox gets *any* stylesheet, Qt stops drawing its native
# up/down buttons and expects the sheet to supply them — so styling the box and
# saying nothing about the sub-controls silently deletes the arrows. Ankisstant
# has 25 spin boxes and 43 combos, so getting this wrong is not subtle. There's
# no built-in arrow image to point at and Qt stylesheets don't take data: URIs,
# so we render two small chevrons to PNG and reference them by path. If that
# fails for any reason, `qt_stylesheet` leaves the widgets native rather than
# shipping a control you can't click.

_SPIN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
    '<path d="{d}" fill="none" stroke="{c}" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_SPIN_UP = "M3 7.5 6 4.5 9 7.5"
_SPIN_DOWN = "M3 4.5 6 7.5 9 4.5"

_ARROW_PX = 24          # 12px chevron at 2x, for Retina
_arrow_cache: dict = {}


def _arrow_dir() -> str:
    # user_files/ survives add-on updates, and these are disposable anyway.
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "user_files", "theme")
    os.makedirs(path, exist_ok=True)
    return path


def _spin_arrows(colour: str):
    """(up_path, down_path) as forward-slashed strings, or None if we can't."""
    if colour in _arrow_cache:
        return _arrow_cache[colour]
    result = None
    try:
        from PyQt6.QtSvg import QSvgRenderer      # NOT re-exported by aqt.qt
        from aqt.qt import QByteArray, QPainter, QPixmap, Qt

        tag = hashlib.md5(colour.encode("utf-8")).hexdigest()[:8]
        folder = _arrow_dir()
        paths = []
        for name, d in (("up", _SPIN_UP), ("down", _SPIN_DOWN)):
            target = os.path.join(folder, f"spin_{name}_{tag}.png")
            if not os.path.exists(target):
                svg = _SPIN_SVG.format(d=d, c=colour).encode("utf-8")
                pix = QPixmap(_ARROW_PX, _ARROW_PX)
                pix.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pix)
                try:
                    QSvgRenderer(QByteArray(svg)).render(painter)
                finally:
                    painter.end()
                if not pix.save(target, "PNG"):
                    raise RuntimeError("could not write " + target)
            paths.append(target.replace("\\", "/"))
        result = (paths[0], paths[1])
    except Exception:
        result = None
    # Only successes are cached. A one-off failure (asked before the Qt app is up,
    # say) would otherwise disable the arrows for the rest of the session, and
    # retrying costs a failed import.
    if result is not None:
        _arrow_cache[colour] = result
    return result


def _spinbox_rules(c) -> str:
    """Spin-box styling, or "" to leave them native with their arrows intact."""
    arrows = _spin_arrows(c["text_muted"])
    if arrows is None:
        return ""
    up, down = arrows
    return f"""
        QSpinBox, QDoubleSpinBox {{
            background-color: {c['surface']};
            border: 1px solid {c['grey_mid']};
            border-radius: 8px; padding: 4px 7px; color: {c['text']};
        }}
        QSpinBox:focus, QDoubleSpinBox:focus {{
            border: 2px solid {c['blue_bright']};
        }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{
            subcontrol-origin: border; subcontrol-position: top right;
            width: 17px; height: 11px; margin: 1px 2px 0 0;
            border: none; background: transparent;
        }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: border; subcontrol-position: bottom right;
            width: 17px; height: 11px; margin: 0 2px 1px 0;
            border: none; background: transparent;
        }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
            background: {c['hover_subtle']}; border-radius: 4px;
        }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
            image: url({up}); width: 9px; height: 9px;
        }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
            image: url({down}); width: 9px; height: 9px;
        }}
        """


def _combo_rules(c) -> str:
    """*Everything* QComboBox — the box, the drop-down and the chevron — or "".

    This has to be all-or-nothing, and that's subtler than it looks. Styling the
    box is what makes Qt stop drawing the chevron, so a sheet that styles
    `QComboBox` in a shared input rule and then omits `::down-arrow` (because the
    arrow render failed) leaves 43 combos looking like flat text fields with no
    affordance that they open. The escape hatch only works if the box rule is
    inside the same conditional as the arrow rule — which is why QComboBox is
    *not* in `_input_rules` with the line edits.
    """
    arrows = _spin_arrows(c["text_muted"])
    if arrows is None:
        return ""
    return f"""
        QComboBox {{
            background-color: {c['surface']};
            border: 1px solid {c['grey_mid']};
            border-radius: 8px; padding: 4px 7px; color: {c['text']};
        }}
        QComboBox:focus {{ border: 2px solid {c['blue_bright']}; }}
        QComboBox::drop-down {{
            subcontrol-origin: padding; subcontrol-position: center right;
            width: 18px; border: none; background: transparent;
        }}
        QComboBox::down-arrow {{ image: url({arrows[1]}); width: 10px; height: 10px; }}
        QComboBox QAbstractItemView {{
            background-color: {c['surface']}; color: {c['text']};
            selection-background-color: {c['selection_bg']};
            selection-color: {c['text']};
            border: 1px solid {c['grey_mid']};
        }}
        """


def qt_stylesheet(*, dialog: bool = False) -> str:
    """A Qt stylesheet for Ankisstant's own windows, from SynapsePro's palette.

    Modelled on how SynapsePro styles its own native dialogs
    (``settings_dialog.py::_build_settings_style`` and
    ``pomodoro.py::_build_pomodoro_style``) so the windows sit together: same
    card surfaces, same accent buttons, same input radii.

    ``dialog=True`` marks the caller as one of Ankisstant's sub-dialogs, which
    are behind their own kill switch (``theme_dialogs``) — someone may well want
    the main window themed and the twenty dialogs left alone.

    Returns "" when there's nothing to theme, and the window keeps inheriting
    Anki's own look exactly as before.

    **Checkbox and radio indicators are deliberately left alone.** Restyling them
    is where this kind of sheet usually goes wrong — a mis-specified indicator
    reads as permanently unchecked, and with 56 checkboxes across Settings, a
    window you can't read the state of is worse than one that doesn't match.

    Spin boxes and combos are the other side of the same coin: styling the box at
    all makes Qt stop drawing its sub-controls, so their arrows have to be
    supplied here or they vanish. See ``_spinbox_rules``.
    """
    cfg = synapse_config()
    switch = "theme_dialogs" if dialog else "theme_settings"
    if not cfg.get(switch, True):
        return ""
    pal = tokens()
    if not pal:
        return ""
    # Per-token fallbacks rather than pal[key] — see _WithFallbacks.
    c = _WithFallbacks(pal, _QSS_FALLBACKS)
    try:
        fam = font_family("")
        font_line = f"font-family: {fam};" if fam else ""
        return f"""
        QDialog {{ background-color: {c['bg']}; color: {c['text']}; {font_line} }}
        QWidget {{ color: {c['text']}; }}
        QLabel {{ color: {c['text']}; background: transparent; }}
        QCheckBox, QRadioButton {{ color: {c['text']}; background: transparent; }}

        QTabWidget::pane {{
            background-color: {c['surface']};
            border: 1px solid {c['grey_light']};
            border-radius: 10px;
        }}
        QTabBar::tab {{
            background: transparent; color: {c['text_muted']};
            padding: 6px 12px; margin-right: 2px;
            border-top-left-radius: 8px; border-top-right-radius: 8px;
        }}
        QTabBar::tab:hover {{ color: {c['text']}; background: {c['hover_subtle']}; }}
        QTabBar::tab:selected {{
            color: {c['text']}; background: {c['surface']};
            border-bottom: 2px solid {c['blue_accent']};
        }}

        QScrollArea {{ background: transparent; border: none; }}
        QScrollArea > QWidget > QWidget {{ background: transparent; }}
        QSplitter::handle {{ background: {c['grey_light']}; }}
        QSplitter::handle:horizontal {{ width: 3px; }}
        QSplitter::handle:vertical {{ height: 3px; }}

        QLineEdit, QPlainTextEdit, QTextEdit {{
            background-color: {c['surface']};
            border: 1px solid {c['grey_mid']};
            border-radius: 8px; padding: 4px 7px; color: {c['text']};
        }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
            border: 2px solid {c['blue_bright']};
        }}
        QTextBrowser {{
            background-color: {c['surface']}; color: {c['text']};
            border: 1px solid {c['grey_light']}; border-radius: 8px;
        }}
        {_spinbox_rules(c)}
        {_combo_rules(c)}

        QListWidget, QTreeWidget, QTableWidget {{
            background-color: {c['surface']};
            border: 1px solid {c['grey_light']};
            border-radius: 8px; color: {c['text']};
        }}
        QListWidget::item, QTreeWidget::item {{ padding: 5px 7px; }}
        QListWidget::item:selected, QTreeWidget::item:selected,
        QTableWidget::item:selected {{
            background-color: {c['selection_bg']}; color: {c['text']};
        }}
        QHeaderView::section {{
            background-color: {c['bg']}; color: {c['text_muted']};
            border: none; border-bottom: 1px solid {c['grey_light']};
            padding: 4px 7px;
        }}

        QPushButton {{
            background-color: {c['grey_light']}; color: {c['text']};
            border: none; border-radius: 8px; padding: 6px 14px; font-weight: 600;
        }}
        QPushButton:hover {{ background-color: {c['grey_mid']}; }}
        QPushButton:disabled {{ background-color: {c['grey_light']};
            color: {c['text_faint']}; }}
        QToolButton {{
            background: transparent; border: none; border-radius: 8px;
            padding: 4px; color: {c['text']};
        }}
        QToolButton:hover {{ background-color: {c['hover_subtle']}; }}
        QToolButton:checked {{ background-color: {c['selection_bg']}; }}
        QDialogButtonBox QPushButton {{
            background-color: {c['blue']}; color: #ffffff; border: {c['blue_border']};
            min-width: 76px;
        }}
        QDialogButtonBox QPushButton:hover {{ background-color: {c['blue_hover']}; }}
        QDialogButtonBox QPushButton:pressed {{ background-color: {c['blue_pressed']}; }}

        QGroupBox {{ border: 1px solid {c['grey_light']}; border-radius: 10px;
            margin-top: 8px; padding-top: 8px; }}
        QGroupBox::title {{ color: {c['text_muted']}; subcontrol-origin: margin;
            left: 10px; padding: 0 4px; }}
        """
    except Exception:
        return ""


def apply_stylesheet(widget, *, dialog: bool = False) -> None:
    """Set the bridge sheet on `widget`, if there is one.

    A no-op when SynapsePro is absent or the kill switch is off — deliberately
    *not* clearing an existing stylesheet, since some Ankisstant widgets set
    their own and clearing would be a visible regression rather than a return to
    baseline.
    """
    try:
        sheet = qt_stylesheet(dialog=dialog)
        if sheet:
            widget.setStyleSheet(sheet)
    except Exception as e:
        print(f"[ankisstant] synapse.apply_stylesheet failed: {e}")
