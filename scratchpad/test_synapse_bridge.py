#!/usr/bin/env python3
"""Offline tests for core/synapse.py — the SynapsePro bridge.

Runs without Anki. `aqt` and friends are stubbed into sys.modules before the
add-on is imported, and a fake SynapsePro theme module is installed so both
directions can be exercised: present, absent, disabled, and broken.

    python3 scratchpad/test_synapse_bridge.py

Two deliberate choices worth keeping:

- **The fake palette is thin on purpose** — four tokens, not the twenty-nine
  SynapsePro really ships. Code that only works against a complete palette is
  code that breaks the first time upstream renames something, and the whole
  point of `_WithFallbacks` is that a missing token costs one colour rather than
  the entire stylesheet. If you "fix" a failure here by fattening the palette,
  you've hidden the bug rather than found it.
- **Nothing here touches the real config.** `mw.addonManager.getConfig` is
  backed by a plain dict, so a test run can never write to meta.json.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "ankisstant_under_test"


# ── stub Anki ─────────────────────────────────────────────────────────────────

_config_store: dict = {}


def _install_aqt_stubs():
    aqt = types.ModuleType("aqt")
    mw = MagicMock()
    mw.addonManager.getConfig.side_effect = lambda _pkg: dict(_config_store)
    mw.addonManager.writeConfig.side_effect = (
        lambda _pkg, cfg: _config_store.update(cfg)
    )
    mw.addonManager.allAddons.return_value = []
    aqt.mw = mw
    aqt.gui_hooks = MagicMock()

    theme = types.ModuleType("aqt.theme")
    theme.theme_manager = MagicMock()
    theme.theme_manager.night_mode = False

    qt = types.ModuleType("aqt.qt")
    for name in (
        "QByteArray", "QPainter", "QPixmap", "Qt", "QWidget", "QDialog",
        "QVBoxLayout", "QHBoxLayout", "QLabel", "QPushButton", "QComboBox",
        "QCheckBox", "QSpinBox", "QFrame", "QStackedWidget", "QSizePolicy",
        "QScrollArea", "QToolButton", "QDockWidget", "QSize", "QIcon",
    ):
        setattr(qt, name, MagicMock())

    utils = types.ModuleType("aqt.utils")
    utils.tooltip = MagicMock()
    utils.showInfo = MagicMock()

    sys.modules.update({
        "aqt": aqt, "aqt.theme": theme, "aqt.qt": qt, "aqt.utils": utils,
    })
    return mw, theme


MW, THEME = _install_aqt_stubs()


def _install_addon_package():
    """Register the add-on folder as a package WITHOUT running its __init__.py.

    The real `ankisstant/__init__.py` registers menus and hooks at import time,
    which needs a live Anki. Giving a bare module a __path__ lets the import
    machinery find submodules under it while that top-level file never runs.
    """
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ADDON_DIR]
    sys.modules[PKG] = pkg


_install_addon_package()

synapse = __import__(f"{PKG}.core.synapse", fromlist=["synapse"])
config = __import__(f"{PKG}.core.config", fromlist=["config"])


# ── fake SynapsePro ───────────────────────────────────────────────────────────

# Four tokens. See the module docstring — this thinness is the test.
LIGHT = {"bg": "#ffffff", "text": "#111111", "blue": "#0055cc", "surface": "#fafafa"}
DARK = {"bg": "#1a1a1a", "text": "#eeeeee", "blue": "#4499ff", "surface": "#222222"}


def _make_fake_synapse(name="SynapsePro1", theme_name="ocean"):
    """A stand-in for SynapsePro's theme module, with a switchable colour theme."""
    mod = types.ModuleType(f"{name}.theme")
    mod.FONT_FAMILY = "Fake Sans"
    mod._active = theme_name

    def palette(night):
        base = dict(DARK if night else LIGHT)
        # Mirrors the real thing: switching colour theme rewrites the blue family
        # only, and does it as module state rather than a new object.
        if mod._active == "forest":
            base["blue"] = "#2e7d32"
        return base

    mod.palette = palette
    root = types.ModuleType(name)
    sys.modules[name] = root
    sys.modules[f"{name}.theme"] = mod
    return mod


def _register_addon(pkg_name, human_name="SynapsePro", enabled=True):
    meta = MagicMock()
    meta.human_name.return_value = human_name
    meta.enabled = enabled
    MW.addonManager.allAddons.return_value = [pkg_name]
    MW.addonManager.addon_meta.return_value = meta


def _clear_addons():
    MW.addonManager.allAddons.return_value = []
    MW.addonManager.addon_meta.return_value = MagicMock()


class BridgeTest(unittest.TestCase):
    def setUp(self):
        _config_store.clear()
        synapse.reset_cache()
        synapse._arrow_cache.clear()
        THEME.theme_manager.night_mode = False
        for key in [k for k in sys.modules if k.startswith("SynapsePro")]:
            del sys.modules[key]
        _clear_addons()

    def _with_synapse(self, theme_name="ocean"):
        mod = _make_fake_synapse(theme_name=theme_name)
        _register_addon("SynapsePro1")
        synapse.reset_cache()
        return mod

    # ── absent / disabled / broken ────────────────────────────────────────────

    def test_absent_yields_nothing(self):
        self.assertFalse(synapse.synapse_available())
        self.assertEqual(synapse.tokens(), {})
        self.assertEqual(synapse.css_vars(), "")
        self.assertEqual(synapse.qt_stylesheet(), "")
        self.assertEqual(synapse.package_name(), "")

    def test_absent_falls_back_to_our_own_colour(self):
        self.assertEqual(synapse.color("blue", "#4a90d9"), "#4a90d9")
        self.assertEqual(synapse.font_family("Helvetica"), "Helvetica")

    def test_disabled_addon_is_ignored(self):
        _make_fake_synapse()
        _register_addon("SynapsePro1", enabled=False)
        synapse.reset_cache()
        self.assertFalse(synapse.synapse_available())
        self.assertEqual(synapse.qt_stylesheet(), "")

    def test_wrong_human_name_is_ignored(self):
        _make_fake_synapse(name="SomethingElse")
        _register_addon("SomethingElse", human_name="Not SynapsePro")
        synapse.reset_cache()
        self.assertFalse(synapse.synapse_available())

    def test_broken_palette_degrades_quietly(self):
        mod = self._with_synapse()
        mod.palette = lambda night: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertTrue(synapse.synapse_available())
        self.assertEqual(synapse.tokens(), {})
        self.assertEqual(synapse.color("blue", "#4a90d9"), "#4a90d9")
        self.assertEqual(synapse.qt_stylesheet(), "")

    def test_palette_returning_junk_degrades_quietly(self):
        mod = self._with_synapse()
        mod.palette = lambda night: "not a dict"
        self.assertEqual(synapse.tokens(), {})

    # ── present ───────────────────────────────────────────────────────────────

    def test_present_serves_tokens(self):
        self._with_synapse()
        self.assertTrue(synapse.synapse_available())
        self.assertEqual(synapse.package_name(), "SynapsePro1")
        self.assertEqual(synapse.color("blue", "#4a90d9"), "#0055cc")

    def test_unknown_token_still_falls_back(self):
        """The thin palette earning its keep: `red` isn't in it."""
        self._with_synapse()
        self.assertEqual(synapse.color("red", "#c05050"), "#c05050")

    def test_night_mode_tracks_the_resolved_theme_manager(self):
        self._with_synapse()
        self.assertEqual(synapse.color("bg", "x"), "#ffffff")
        THEME.theme_manager.night_mode = True
        self.assertEqual(synapse.color("bg", "x"), "#1a1a1a")

    def test_live_theme_switch_is_seen_without_restart(self):
        """The palette must be asked fresh every call, not cached."""
        mod = self._with_synapse()
        self.assertEqual(synapse.color("blue", "x"), "#0055cc")
        mod._active = "forest"
        self.assertEqual(synapse.color("blue", "x"), "#2e7d32")

    def test_font_is_opt_in(self):
        self._with_synapse()
        self.assertEqual(synapse.font_family("Helvetica"), "Helvetica")
        _config_store["synapse"] = {"match_font": True}
        self.assertEqual(synapse.font_family("Helvetica"), "Fake Sans")

    # ── tint() ────────────────────────────────────────────────────────────────

    def test_tint_unpacks_hex_to_rgba(self):
        self._with_synapse()
        self.assertEqual(synapse.tint("blue", 0.16, "rgba(80,160,255,0.16)"),
                         "rgba(0, 85, 204, 0.16)")

    def test_tint_falls_back_without_synapse(self):
        self.assertEqual(synapse.tint("blue", 0.16, "rgba(80,160,255,0.16)"),
                         "rgba(80,160,255,0.16)")

    def test_tint_rejects_non_hex_tokens(self):
        """SynapsePro's *_border tokens are whole CSS border values, and "none"
        is a legal one — unpacking those as colours would be nonsense."""
        mod = self._with_synapse()
        base = mod.palette
        mod.palette = lambda night: {**base(night), "blue_border": "none",
                                     "weird": "2px solid red"}
        self.assertEqual(synapse.tint("blue_border", 0.5, "FALLBACK"), "FALLBACK")
        self.assertEqual(synapse.tint("weird", 0.5, "FALLBACK"), "FALLBACK")

    def test_tint_handles_short_hex(self):
        mod = self._with_synapse()
        base = mod.palette
        mod.palette = lambda night: {**base(night), "blue": "#0af"}
        self.assertEqual(synapse.tint("blue", 1, "x"), "rgba(0, 170, 255, 1)")

    # ── the Qt sheet ──────────────────────────────────────────────────────────

    def test_stylesheet_survives_a_thin_palette(self):
        """The bug this whole harness exists for: one missing token must cost
        one colour, not the entire sheet."""
        self._with_synapse()
        sheet = synapse.qt_stylesheet()
        self.assertTrue(sheet.strip(), "sheet collapsed to empty on a thin palette")
        self.assertIn("#0055cc", sheet)              # a token we do have
        self.assertIn("#d1d1d6", sheet)              # grey_mid, from the fallbacks

    def test_stylesheet_leaves_checkbox_indicators_native(self):
        self._with_synapse()
        sheet = synapse.qt_stylesheet()
        self.assertNotIn("QCheckBox::indicator", sheet)
        self.assertNotIn("QRadioButton::indicator", sheet)

    def test_spin_and_combo_are_all_or_nothing(self):
        """Either the arrows are supplied or the widget is left unstyled.

        The failure this guards against is subtle and was live in the first
        draft: `_combo_rules` bailed correctly when the arrow render failed, but
        QComboBox was *also* named in the shared input rule alongside QLineEdit,
        so the box still got styled — which is precisely what makes Qt stop
        drawing the chevron. The escape hatch only works if every QComboBox rule
        sits behind the same conditional.

        Under this harness there's no real Qt, so the render always fails and
        both widgets must come out completely unmentioned.
        """
        self._with_synapse()
        sheet = synapse.qt_stylesheet()
        for widget in ("QSpinBox", "QComboBox"):
            if f"{widget}::" in sheet:
                # Styled: the arrow rule is then mandatory.
                self.assertIn(f"{widget}::down-arrow", sheet)
            else:
                # Not styled: it must not be mentioned *anywhere*, or Qt drops
                # the sub-controls anyway.
                self.assertNotIn(widget, sheet,
                                 f"{widget} is styled but has no arrow rules")

    def test_arrow_render_failure_leaves_widgets_native(self):
        self._with_synapse()
        pal = synapse._WithFallbacks(synapse.tokens(), synapse._QSS_FALLBACKS)
        original = synapse._spin_arrows
        synapse._spin_arrows = lambda colour: None
        try:
            self.assertEqual(synapse._spinbox_rules(pal), "")
            self.assertEqual(synapse._combo_rules(pal), "")
        finally:
            synapse._spin_arrows = original

    def test_failed_arrow_render_is_not_cached(self):
        """A one-off failure (asked before the Qt app is up) must not disable the
        arrows for the rest of the session."""
        synapse._arrow_cache.clear()
        self.assertIsNone(synapse._spin_arrows("#000000"))
        self.assertEqual(synapse._arrow_cache, {})

    def test_lookup_wrapper_never_raises(self):
        c = synapse._WithFallbacks({}, {"text": "#111"})
        self.assertEqual(c["text"], "#111")
        self.assertEqual(c["a_token_that_does_not_exist"], "inherit")

    # ── the webview vars ──────────────────────────────────────────────────────

    def test_css_vars_only_emits_tokens_that_exist(self):
        self._with_synapse()
        block = synapse.css_vars()
        self.assertTrue(block.startswith("<style>:root{"))
        self.assertIn("--ank-accent:#0055cc;", block)
        # `red` isn't in the thin palette, so --ank-warn must simply not appear
        # and the CSS fallback in the stylesheet applies instead.
        self.assertNotIn("--ank-warn", block)

    def test_css_vars_empty_without_synapse(self):
        self.assertEqual(synapse.css_vars(), "")

    # ── kill switches ─────────────────────────────────────────────────────────

    def test_theme_bridge_off_disables_everything_colour(self):
        self._with_synapse()
        _config_store["synapse"] = {"theme_bridge": False}
        self.assertEqual(synapse.tokens(), {})
        self.assertEqual(synapse.css_vars(), "")
        self.assertEqual(synapse.qt_stylesheet(), "")
        self.assertEqual(synapse.color("blue", "#4a90d9"), "#4a90d9")
        # ...but detection itself still works, so the settings tab can say so.
        self.assertTrue(synapse.synapse_available())

    def test_theme_settings_and_theme_dialogs_are_independent(self):
        self._with_synapse()
        _config_store["synapse"] = {"theme_settings": False, "theme_dialogs": True}
        self.assertEqual(synapse.qt_stylesheet(), "")
        self.assertTrue(synapse.qt_stylesheet(dialog=True).strip())

        _config_store["synapse"] = {"theme_settings": True, "theme_dialogs": False}
        self.assertTrue(synapse.qt_stylesheet().strip())
        self.assertEqual(synapse.qt_stylesheet(dialog=True), "")

    # ── config plumbing ───────────────────────────────────────────────────────

    def test_synapse_defaults_are_backfilled(self):
        """Existing users get the new block without a schema bump."""
        _config_store.clear()
        _config_store["provider"] = "cli"          # a pre-existing config
        cfg = config.synapse_config()
        self.assertTrue(cfg.get("theme_bridge"))
        self.assertEqual(cfg.get("open_mode"), "dock")
        self.assertEqual(cfg.get("dock_width"), 720)

    def test_synapse_config_never_raises(self):
        MW.addonManager.getConfig.side_effect = RuntimeError("no profile open")
        try:
            self.assertEqual(config.synapse_config(), {})
        finally:
            MW.addonManager.getConfig.side_effect = lambda _p: dict(_config_store)


class StaticSweepTest(unittest.TestCase):
    """Checks that don't need Anki and would otherwise only show up in use."""

    def test_no_import_time_colour_constants(self):
        """The one trap that actually ships.

        A module-level `X = f"...{synapse.color(...)}"` is evaluated once at
        Anki launch, so it holds whatever palette SynapsePro had at that moment
        and stops tracking theme changes until a restart — with no error to
        notice. Every bridge colour has to be resolved inside a function.
        """
        import re
        bad = []
        for path in _addon_py_files():
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    # Column 0 == module level.
                    if re.match(r"^[A-Za-z_][\w]*\s*(:[^=]+)?=", line) and (
                            "synapse.color(" in line or "synapse.tint(" in line
                            or "synapse.status_" in line):
                        bad.append(f"{path}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "import-time colour constants:\n" + "\n".join(bad))

    def test_bridge_imports_do_not_cycle(self):
        """core.qt_utils -> core.synapse -> core.config must stay a chain.

        qt_utils is imported by nearly every UI module, so a back-edge from
        config or synapse into it would deadlock the import graph at startup —
        and Anki reports that as a bare "add-on failed to load".
        """
        import ast
        for module, forbidden in (("core/synapse.py", {"qt_utils"}),
                                  ("core/config.py", {"qt_utils", "synapse"})):
            tree = ast.parse(open(_p(module), encoding="utf-8").read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[-1])
                    names.update(a.name for a in node.names)
            clash = names & forbidden
            self.assertEqual(clash, set(), f"{module} must not import {clash}")

    def test_every_theme_dialog_call_can_resolve_its_name(self):
        """A `theme_dialog(self)` with no import is a NameError the first time
        that dialog opens — and these are dialogs you might not open for weeks."""
        import re
        bad = []
        for path in _addon_py_files():
            src = open(path, encoding="utf-8").read()
            if "theme_dialog(" not in src:
                continue
            # Imports may be single-line or a parenthesised block, so match the
            # whole `from ... import (...)` form rather than one line of it.
            imported = re.search(
                r"^from [\w.]*qt_utils import (?:\(((?:[^)]*\n)*?)\)|([^\n]*))",
                src, re.M)
            names = "".join(g or "" for g in (imported.groups() if imported else ()))
            if "theme_dialog" not in names and not re.search(r"^def theme_dialog", src, re.M):
                bad.append(os.path.relpath(path, ADDON_DIR))
        self.assertEqual(bad, [], f"theme_dialog used without importing it: {bad}")


    def test_every_css_var_use_carries_a_fallback(self):
        """`var(--ank-x)` with no second argument resolves to nothing without
        SynapsePro — a black-on-black label, or an invisible border, on a plain
        Anki install. The fallback is what makes standalone unchanged, so it is
        not optional anywhere.
        """
        import re
        bad = []
        for path in _addon_py_files():
            src = open(path, encoding="utf-8").read()
            for m in re.finditer(r"(.?)var\(\s*(--ank-[\w-]+)\s*([,)])", src):
                if m.group(1) == "`":
                    continue          # a backticked mention in prose, not CSS
                if m.group(3) == ")":
                    line = src[:m.start()].count("\n") + 1
                    bad.append(f"{os.path.relpath(path, ADDON_DIR)}:{line}: {m.group(2)}")
        self.assertEqual(bad, [], "var() without a fallback:\n" + "\n".join(bad))

    def test_every_css_var_used_is_one_the_bridge_emits(self):
        """A typo'd var name fails silently — it just always takes the fallback,
        so the surface looks fine standalone and never themes with SynapsePro on.
        """
        import re
        known = set(synapse._CSS_MAP) | {"--ank-font"}
        bad = []
        for path in _addon_py_files():
            src = open(path, encoding="utf-8").read()
            for m in re.finditer(r"(.?)var\(\s*(--ank-[\w-]+)", src):
                if m.group(1) == "`":
                    continue          # a backticked mention in prose, not CSS
                if m.group(2) not in known:
                    line = src[:m.start()].count("\n") + 1
                    bad.append(f"{os.path.relpath(path, ADDON_DIR)}:{line}: {m.group(2)}")
        self.assertEqual(bad, [], "unknown --ank-* names:\n" + "\n".join(bad))


def _p(rel):
    return os.path.join(ADDON_DIR, rel)


def _addon_py_files():
    skip = ("_phaseA_backup", "user_files", "__pycache__", "scratchpad")
    for root, dirs, files in os.walk(ADDON_DIR):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


if __name__ == "__main__":
    unittest.main(verbosity=2)
