#!/usr/bin/env python3
"""Offline tests for core/synapse_chat.py — the ＋KG button in SynapsePro's chat.

Runs without Anki and without SynapsePro. `aqt` is stubbed into sys.modules
before the add-on is imported, and a fake `ai_assistant` module stands in for
SynapsePro's, so all four seams can be broken one at a time.

    python3 scratchpad/test_synapse_chat.py

What this harness is actually for: the bridge reads four **private,
undocumented** names out of another add-on. Clicking around a healthy install
proves nothing about the case that matters — a SynapsePro release renaming one
of them. So every test here removes or changes a seam and asserts the failure
is "the button doesn't appear", never a traceback.

Two traps get a test each because neither is visible by inspection:

- **`_trim_conversation()` rebinds the global** (`_conversation = system + tail`)
  rather than mutating in place. Cache that list and the feature keeps serving
  answers out of one SynapsePro has already discarded.
- **`ChatPage` resolves `_handle_action` through module globals at call time**,
  which is the only reason wrapping the attribute works at all. If that ever
  becomes a held reference, `test_action_hook_is_seen_by_caller` is what fails.

Nothing here touches the real config or the real KG queue: `getConfig` is backed
by a plain dict, and `store`'s queue path is redirected into a temp file.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "ankisstant_chat_under_test"


# ── stub Anki ─────────────────────────────────────────────────────────────────

_config_store: dict = {}
_tooltips: list = []


def _install_aqt_stubs():
    aqt = types.ModuleType("aqt")
    mw = MagicMock()
    mw.addonManager.getConfig.side_effect = lambda _pkg: dict(_config_store)
    mw.addonManager.writeConfig.side_effect = (
        lambda _pkg, cfg: _config_store.update(cfg)
    )
    mw.addonManager.allAddons.return_value = []
    # single_shot must not actually fire: register() would then run _ensure
    # against a half-built fake and the tests would be testing the retry timer.
    mw.progress.single_shot = MagicMock()
    aqt.mw = mw
    aqt.gui_hooks = MagicMock()
    aqt.gui_hooks.state_did_change = []

    theme = types.ModuleType("aqt.theme")
    theme.theme_manager = MagicMock()
    theme.theme_manager.night_mode = False

    # Permissive rather than an enumerated list: this harness imports
    # core/qt_utils, which pulls in a long and shifting set of Qt names, and a
    # fixed list turns "someone imported one more widget" into a test failure
    # that says nothing about the bridge.
    qt = types.ModuleType("aqt.qt")
    qt.__getattr__ = lambda name: MagicMock()

    utils = types.ModuleType("aqt.utils")
    utils.tooltip = lambda msg, **kw: _tooltips.append(str(msg))
    utils.showInfo = MagicMock()

    sys.modules.update({
        "aqt": aqt, "aqt.theme": theme, "aqt.qt": qt, "aqt.utils": utils,
    })
    return mw


MW = _install_aqt_stubs()


def _install_addon_package():
    """Register the add-on folder as a package WITHOUT running its __init__.py,
    which registers menus and hooks at import time and needs a live Anki."""
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ADDON_DIR]
    sys.modules[PKG] = pkg
    for sub in ("core", "tools", "tools.kg"):
        m = types.ModuleType(f"{PKG}.{sub}")
        m.__path__ = [os.path.join(ADDON_DIR, *sub.split("."))]
        sys.modules[f"{PKG}.{sub}"] = m


_install_addon_package()

# The failure paths below log errors on purpose. Point the log at a throwaway
# file, or every test run writes fake "webview died" lines into the real one.
_log = __import__(f"{PKG}.core.log", fromlist=["log"])
_log._LOG_PATH = os.path.join(tempfile.mkdtemp(prefix="ank-test-log-"), "test.log")

synapse =__import__(f"{PKG}.core.synapse", fromlist=["synapse"])
chat = __import__(f"{PKG}.core.synapse_chat", fromlist=["synapse_chat"])
store = __import__(f"{PKG}.tools.kg.store", fromlist=["store"])


# ── fake SynapsePro ───────────────────────────────────────────────────────────

def _register_addon(pkg_name="SynapsePro1", human_name="SynapsePro", enabled=True):
    meta = MagicMock()
    meta.human_name.return_value = human_name
    meta.enabled = enabled
    MW.addonManager.allAddons.return_value = [pkg_name]
    MW.addonManager.addon_meta.return_value = meta


def _make_fake_synapse(name="SynapsePro1", *, with_ai=True, with_webview=True,
                       with_handler=True):
    """Stand-in for SynapsePro: a theme module (so `synapse` resolves it) and an
    `ai_assistant` whose seams can each be withheld."""
    root = types.ModuleType(name)
    root.__path__ = []
    theme = types.ModuleType(f"{name}.theme")
    theme.FONT_FAMILY = "Fake Sans"
    theme.palette = lambda night: {"bg": "#fff", "text": "#111", "blue": "#05c"}
    sys.modules[name] = root
    sys.modules[f"{name}.theme"] = theme

    if not with_ai:
        return None

    ai = types.ModuleType(f"{name}.ai_assistant")
    ai._conversation = [{"role": "system", "content": "you are helpful"}]
    ai.handled = []

    if with_handler:
        def _handle_action(action, data):
            ai.handled.append((action, data))
        ai._handle_action = _handle_action

    if with_webview:
        ai._webview = _FakeView()

    sys.modules[f"{name}.ai_assistant"] = ai
    return ai


class _FakePage:
    """Mimics QWebEnginePage closely enough for injection: runJavaScript takes a
    callback, and loadFinished is a connectable signal."""

    def __init__(self):
        self.scripts = []
        self.load_slots = []
        self.loadFinished = _FakeSignal(self.load_slots)

    def runJavaScript(self, code, cb=None):
        self.scripts.append(code)
        if cb is not None:
            cb("ok")


class _FakeSignal:
    def __init__(self, sink):
        self._sink = sink

    def connect(self, slot):
        self._sink.append(slot)


class _FakeView:
    def __init__(self):
        self._page = _FakePage()

    def page(self):
        return self._page


def _trim_like_synapse(ai):
    """Reproduce `_trim_conversation`: it REBINDS the global rather than
    mutating in place. This is the trap `test_survives_conversation_rebind`
    exists for."""
    system = [dict(ai._conversation[0])]
    ai._conversation = system + ai._conversation[1:][-24:]


# ── base ──────────────────────────────────────────────────────────────────────

class ChatBridgeTest(unittest.TestCase):
    def setUp(self):
        _config_store.clear()
        _tooltips.clear()
        synapse.reset_cache()
        for key in [k for k in sys.modules if k.startswith("SynapsePro")]:
            del sys.modules[key]
        MW.addonManager.allAddons.return_value = []
        # Redirect the KG queue to a throwaway file — never the real one.
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        os.unlink(self._tmp.name)
        self._orig_path = store._queue_path
        store._queue_path = lambda: self._tmp.name

    def tearDown(self):
        store._queue_path = self._orig_path
        if os.path.exists(self._tmp.name):
            os.unlink(self._tmp.name)

    def _live(self, **kw):
        """A fully-present SynapsePro with a chat that has answered once."""
        _register_addon()
        ai = _make_fake_synapse(**kw)
        if ai is not None:
            ai._conversation += [
                {"role": "user", "content": "why does hypokalaemia worsen digoxin toxicity?"},
                {"role": "assistant", "content": "Digoxin competes with K+ at the Na/K ATPase."},
            ]
        return ai


# ── SynapsePro absent or partial ─────────────────────────────────────────────

class AbsentTest(ChatBridgeTest):
    def test_no_synapse_at_all(self):
        self.assertFalse(chat.chat_available())
        self.assertEqual(chat.last_assistant_turn(), "")
        chat._ensure()                       # must not raise

    def test_synapse_without_ai_assistant(self):
        _register_addon()
        _make_fake_synapse(with_ai=False)
        self.assertFalse(chat.chat_available())
        chat._ensure()

    def test_ai_assistant_without_handle_action(self):
        """A renamed dispatcher costs the button, not a traceback."""
        ai = self._live(with_handler=False)
        chat._ensure()
        self.assertEqual(ai._webview.page().scripts, [])

    def test_ai_assistant_without_webview(self):
        """Chat dock never opened: nothing to inject into, but the action hook
        still installs so it's ready when the view appears."""
        ai = self._live(with_webview=False)
        chat._ensure()
        self.assertTrue(getattr(ai, "_ank_kg_hook", False))

    def test_reading_still_works_without_webview(self):
        """The panel fallback depends only on _conversation — the whole reason
        it exists — so it must survive the other three seams being gone."""
        self._live(with_webview=False, with_handler=False)
        self.assertTrue(chat.chat_available())
        self.assertIn("Na/K ATPase", chat.last_assistant_turn())


# ── reading the conversation ─────────────────────────────────────────────────

class ConversationTest(ChatBridgeTest):
    def test_last_assistant_turn(self):
        self._live()
        self.assertEqual(chat.last_assistant_turn(),
                         "Digoxin competes with K+ at the Na/K ATPase.")

    def test_skips_system_and_user(self):
        ai = self._live()
        ai._conversation.append({"role": "user", "content": "and in renal failure?"})
        # The trailing user turn must not be mistaken for an answer.
        self.assertTrue(chat.last_assistant_turn().startswith("Digoxin competes"))

    def test_question_for_context(self):
        self._live()
        self.assertIn("hypokalaemia", chat._user_turn_before_last_answer())

    def test_question_is_the_one_before_the_answer(self):
        """Not simply the last user turn — a follow-up typed after the answer
        would otherwise be passed off as the question that produced it."""
        ai = self._live()
        ai._conversation.append({"role": "user", "content": "unrelated follow-up"})
        self.assertIn("hypokalaemia", chat._user_turn_before_last_answer())

    def test_survives_conversation_rebind(self):
        """_trim_conversation replaces the list object. Holding a reference to
        the old one would serve stale answers forever."""
        ai = self._live()
        before = ai._conversation
        ai._conversation = ai._conversation + [
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "A fresher answer entirely."},
        ]
        _trim_like_synapse(ai)
        self.assertIsNot(ai._conversation, before)
        self.assertEqual(chat.last_assistant_turn(), "A fresher answer entirely.")

    def test_empty_conversation(self):
        _register_addon()
        _make_fake_synapse()
        self.assertEqual(chat.last_assistant_turn(), "")

    def test_malformed_entries_are_skipped(self):
        ai = self._live()
        ai._conversation.append("not a dict")
        ai._conversation.append({"role": "assistant"})     # no content
        self.assertEqual(chat.last_assistant_turn(), "")   # empty content wins, honestly


# ── the action hook ──────────────────────────────────────────────────────────

class ActionHookTest(ChatBridgeTest):
    def test_unknown_actions_still_reach_synapse(self):
        """The wrapper is additive: everything that isn't ours is delegated."""
        ai = self._live()
        chat._ensure()
        ai._handle_action("send_message", {"text": "hi"})
        self.assertEqual(ai.handled, [("send_message", {"text": "hi"})])

    def test_our_action_is_intercepted(self):
        ai = self._live()
        chat._ensure()
        captured = []
        orig = chat.capture_last_turn
        chat.capture_last_turn = lambda button=None: captured.append(True)
        try:
            ai._handle_action(chat.CHAT_ACTION, {})
        finally:
            chat.capture_last_turn = orig
        self.assertEqual(len(captured), 1)
        self.assertEqual(ai.handled, [])     # not passed through

    def test_action_hook_is_seen_by_caller(self):
        """ChatPage looks _handle_action up in module globals at call time.
        If upstream ever holds a reference instead, this is what breaks."""
        ai = self._live()
        chat._ensure()
        looked_up = getattr(sys.modules["SynapsePro1.ai_assistant"], "_handle_action")
        self.assertIs(looked_up, ai._handle_action)

    def test_hook_installs_once(self):
        ai = self._live()
        chat._ensure()
        first = ai._handle_action
        chat._ensure()
        chat._ensure()
        self.assertIs(ai._handle_action, first)

    def test_kill_switch_delegates_instead_of_capturing(self):
        """Turned off mid-session, our action falls through to SynapsePro —
        which prints 'unknown action' and carries on. The wrapper is never
        removed: restoring a reference a third add-on may have wrapped since is
        how you delete someone else's hook."""
        ai = self._live()
        chat._ensure()
        _config_store["synapse"] = {"chat_kg_button": False}
        boom = lambda button=None: self.fail("capture ran with the switch off")
        orig, chat.capture_last_turn = chat.capture_last_turn, boom
        try:
            ai._handle_action(chat.CHAT_ACTION, {})
        finally:
            chat.capture_last_turn = orig
        self.assertEqual(ai.handled, [(chat.CHAT_ACTION, {})])

    def test_capture_exception_does_not_escape(self):
        """An exception here would surface inside SynapsePro's console handler."""
        ai = self._live()
        chat._ensure()
        def explode(button=None):
            raise RuntimeError("nope")
        orig, chat.capture_last_turn = chat.capture_last_turn, explode
        try:
            ai._handle_action(chat.CHAT_ACTION, {})       # must not raise
        finally:
            chat.capture_last_turn = orig


# ── button injection ─────────────────────────────────────────────────────────

class InjectionTest(ChatBridgeTest):
    def test_injects_once_and_reconnects_reload(self):
        ai = self._live()
        chat._ensure()
        page = ai._webview.page()
        self.assertEqual(len(page.scripts), 1)
        self.assertEqual(len(page.load_slots), 1)

    def test_reload_hook_connects_only_once(self):
        ai = self._live()
        chat._ensure()
        chat._ensure()
        self.assertEqual(len(ai._webview.page().load_slots), 1)

    def test_reload_reinjects(self):
        """page_ready resets the conversation and the document; the button has
        to go back in or it silently disappears for the session."""
        ai = self._live()
        chat._ensure()
        page = ai._webview.page()
        for slot in list(page.load_slots):
            slot(True)
        self.assertEqual(len(page.scripts), 2)

    def test_new_page_is_hooked(self):
        """The reload flag lives on the page, not on this module — SynapsePro
        can build a fresh page, and a module flag would strand it."""
        ai = self._live()
        chat._ensure()
        ai._webview._page = _FakePage()          # SynapsePro rebuilt it
        chat._ensure()
        self.assertEqual(len(ai._webview.page().load_slots), 1)
        self.assertEqual(len(ai._webview.page().scripts), 1)

    def test_js_is_idempotent_and_guarded(self):
        js = chat._inject_js(3)
        self.assertIn(json.dumps(chat.BUTTON_ID), js)
        self.assertIn(json.dumps(chat.CHAT_ACTION), js)
        # Guards, in order: already injected, no host, no bridge.
        self.assertIn("getElementById", js)
        self.assertIn("'exists'", js)
        self.assertIn("typeof sendToPython !== 'function'", js)
        self.assertIn("'no-bridge'", js)

    def test_js_has_no_unescaped_injection(self):
        """Every value interpolated into the JS goes through json.dumps. A raw
        f-string here would break the script the first time a tooltip contained
        an apostrophe."""
        js = chat._inject_js(3)
        self.assertNotIn("\n</script", js)
        # Balanced enough to parse: the wrapper IIFE closes.
        self.assertTrue(js.strip().endswith("})();"))

    def test_svg_follows_theme(self):
        """currentColor, not a hex — a fixed colour goes invisible on half of
        SynapsePro's themes."""
        self.assertIn("currentColor", chat._SVG)
        self.assertNotIn("#", chat._SVG)

    def test_runjavascript_failure_is_contained(self):
        ai = self._live()
        def explode(code, cb=None):
            raise RuntimeError("webview died")
        ai._webview.page().runJavaScript = explode
        chat._ensure()                            # must not raise

    def test_kill_switch_blocks_injection(self):
        ai = self._live()
        _config_store["synapse"] = {"chat_kg_button": False}
        chat._ensure()
        self.assertEqual(ai._webview.page().scripts, [])


# ── writing gaps ─────────────────────────────────────────────────────────────

class WriteTest(ChatBridgeTest):
    def test_writes_a_gap(self):
        n = chat._write([{"title": "Digoxin toxicity worsened by hypokalaemia",
                          "why": "Unclear which transporter they compete at.",
                          "tags": ["cardio"]}])
        self.assertEqual(n, 1)
        kg = store.load_all()[0]
        self.assertEqual(kg["source"], "chat")
        self.assertEqual(kg["type"], "kg")
        self.assertEqual(kg["status"], "open")
        self.assertEqual(kg["fields"]["notes"], "Unclear which transporter they compete at.")
        self.assertEqual(kg["tags"], ["cardio"])

    def test_chat_is_a_valid_source(self):
        """Guards the store's whitelist: an unlisted source is silently coerced
        to 'manual' by _normalise, losing the provenance without any error."""
        self.assertIn("chat", store.VALID_SOURCES)

    def test_dedupes_against_open_gaps(self):
        entry = {"title": "Digoxin and hypokalaemia", "why": "x"}
        self.assertEqual(chat._write([entry]), 1)
        self.assertEqual(chat._write([entry]), 0)       # pressing ＋KG twice
        self.assertEqual(len(store.load_all()), 1)

    def test_dedupe_ignores_case_and_punctuation(self):
        chat._write([{"title": "Digoxin and hypokalaemia", "why": "x"}])
        self.assertEqual(chat._write([{"title": "digoxin and, hypokalaemia!", "why": "y"}]), 0)

    def test_done_gaps_do_not_block_relogging(self):
        """A gap you closed months ago shouldn't stop it being logged again."""
        chat._write([{"title": "Digoxin and hypokalaemia", "why": "x"}])
        kg = store.load_all()[0]
        store.update(kg["id"], status="done")
        self.assertEqual(chat._write([{"title": "Digoxin and hypokalaemia", "why": "y"}]), 1)

    def test_respects_the_cap(self):
        _config_store["synapse"] = {"chat_kg_max": 2}
        n = chat._write([{"title": f"gap number {i}", "why": ""} for i in range(5)])
        self.assertEqual(n, 2)

    def test_cap_is_bounded(self):
        _config_store["synapse"] = {"chat_kg_max": 999}
        self.assertEqual(chat._max_gaps(), 10)
        _config_store["synapse"] = {"chat_kg_max": 0}
        self.assertEqual(chat._max_gaps(), 1)
        _config_store["synapse"] = {"chat_kg_max": "nonsense"}
        self.assertEqual(chat._max_gaps(), 3)

    def test_single_object_is_absorbed(self):
        self.assertEqual(chat._write({"title": "one gap", "why": "x"}), 1)

    def test_garbage_returns_zero(self):
        self.assertEqual(chat._write("not json at all"), 0)
        self.assertEqual(chat._write([1, 2, 3]), 0)
        self.assertEqual(chat._write([]), 0)

    def test_untitled_entries_are_dropped(self):
        self.assertEqual(chat._write([{"title": "   ", "why": "something"}]), 0)

    def test_long_titles_are_trimmed(self):
        """The list truncates at 80 chars, so an over-long title would lose the
        half that distinguishes it."""
        long = " ".join(f"word{i}" for i in range(40))
        chat._write([{"title": long, "why": ""}])
        self.assertLessEqual(len(store.load_all()[0]["title"].split(" ")), 15)

    def test_trailing_stop_removed(self):
        chat._write([{"title": "Digoxin competes at the pump.", "why": ""}])
        self.assertFalse(store.load_all()[0]["title"].endswith("."))

    def test_bad_tags_do_not_break_the_write(self):
        chat._write([{"title": "a gap", "why": "", "tags": "cardio"}])
        self.assertEqual(store.load_all()[0]["tags"], [])


# ── the capture flow ─────────────────────────────────────────────────────────

class CaptureTest(ChatBridgeTest):
    def setUp(self):
        super().setUp()
        self._calls = []
        qt_utils = __import__(f"{PKG}.core.qt_utils", fromlist=["qt_utils"])
        self._qt_utils = qt_utils
        self._orig = getattr(qt_utils, "run_claude_json_async", None)

        def fake(button, on_done, **kw):
            self._calls.append(kw)
            on_done(self._reply)
        qt_utils.run_claude_json_async = fake
        self._reply = []

    def tearDown(self):
        if self._orig is not None:
            self._qt_utils.run_claude_json_async = self._orig
        super().tearDown()

    def test_no_answer_yet(self):
        _register_addon()
        _make_fake_synapse()
        chat.capture_last_turn()
        self.assertEqual(self._calls, [])
        self.assertIn("No AI answer", _tooltips[0])

    def test_prompt_carries_answer_and_question(self):
        self._live()
        self._reply = [{"title": "a gap", "why": "x"}]
        chat.capture_last_turn()
        prompt = self._calls[0]["prompt"]
        self.assertIn("Na/K ATPase", prompt)
        self.assertIn("hypokalaemia", prompt)

    def test_prompt_constrains_shape(self):
        """Conciseness is enforced here, not in SynapsePro's system prompt."""
        self._live()
        self._reply = []
        chat.capture_last_turn()
        prompt = self._calls[0]["prompt"]
        self.assertIn("at most 10 words", prompt)
        self.assertIn("at most 25 words", prompt)
        self.assertIn("Return a JSON array of at most 3", prompt)

    def test_success_reports_the_count(self):
        self._live()
        self._reply = [{"title": "gap one", "why": "x"},
                       {"title": "gap two", "why": "y"}]
        chat.capture_last_turn()
        self.assertEqual(len(store.load_all()), 2)
        self.assertIn("Logged 2 knowledge gaps", _tooltips[-1])

    def test_singular_wording(self):
        self._live()
        self._reply = [{"title": "gap one", "why": "x"}]
        chat.capture_last_turn()
        self.assertIn("Logged 1 knowledge gap.", _tooltips[-1])

    def test_nothing_found_says_so(self):
        """Not by silence. A no-op button is indistinguishable from a broken one."""
        self._live()
        self._reply = []
        chat.capture_last_turn()
        self.assertIn("Nothing new to log", _tooltips[-1])

    def test_ai_failure_is_quiet(self):
        """None means run_claude_json_async already reported it — a second
        tooltip would just be noise."""
        self._live()
        self._reply = None
        chat.capture_last_turn()
        self.assertEqual(_tooltips, [])
        self.assertEqual(store.load_all(), [])


# ── static sweeps ────────────────────────────────────────────────────────────

class StaticTest(unittest.TestCase):
    def _source(self):
        """The module's source with comments and string literals stripped out.

        These sweeps look for *code* that does the forbidden thing. Searching
        raw text instead makes them fire on the docstring that documents the
        trap — which is exactly backwards: the file would pass by staying
        undocumented and fail for explaining itself.
        """
        import io
        import tokenize

        path = os.path.join(ADDON_DIR, "core", "synapse_chat.py")
        with open(path) as f:
            src = f.read()
        out = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
        return " ".join(out)

    def test_conversation_is_never_cached(self):
        """The whole module must reach _conversation through getattr on a
        freshly-resolved module. A module-level cache would survive
        _trim_conversation's rebind and go stale."""
        src = self._source()
        self.assertNotIn("_conversation =", src)
        self.assertNotIn("global _convo", src)

    def test_does_not_write_to_synapse_config(self):
        """Reading SynapsePro is the bridge; writing to it is the hard coupling
        the add-on-independence rule exists to prevent."""
        src = self._source()
        for bad in ("_cfg_set", "_save_settings", "writeConfig"):
            self.assertNotIn(bad, src)

    def test_does_not_touch_synapse_prompts(self):
        """Conciseness comes from our own AI call. Rewriting its _SYSTEM_MSG or
        chips would change SynapsePro's behaviour for every other use."""
        src = self._source()
        self.assertNotIn("_SYSTEM_MSG", src)
        self.assertNotIn("_build_chip_prompt", src)

    def test_action_name_is_namespaced(self):
        """It travels through SynapsePro's own dispatcher; a bare name could
        collide with a future upstream action and be silently eaten."""
        self.assertTrue(chat.CHAT_ACTION.startswith("ankisstant"))

    def test_seams_are_documented(self):
        """Every private name borrowed from SynapsePro must be named in the
        docstring — that list is the upgrade checklist."""
        doc = chat.__doc__ or ""
        for seam in ("_webview", "_handle_action", "_conversation", "sendToPython"):
            self.assertIn(seam, doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
