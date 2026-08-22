"""Log a knowledge gap straight from SynapsePro's AI chat.

SynapsePro's chat assistant has no tool-calling — it is five streaming text
paths piping tokens into a QWebEngine view, with no way for the model to *do*
anything. So this doesn't try to make the model act. It puts a **＋KG** button
in the chat's header; you press it when an answer has exposed something you
didn't know, and Ankisstant condenses that answer into one or more short
knowledge gaps and drops them in the queue.

The point is to stop the conversation being about whether to make cards. You
read, you press one button, the gap is banked, and Create/Browse deal with it
later from the queue like every other KG.

**Nothing in SynapsePro is modified.** There is no file edit, no config write
and no patched copy. Four seams it happens to leave open are used from outside:

===========================  =================================================
``ai_assistant._webview``    the live ``QWebEngineView`` — ``runJavaScript``
                             puts our button in its header
``sendToPython(a, d)``       a global JS function in ``chat_ui.html``; our
                             button calls it, so the click rides SynapsePro's
                             *existing* PYCALL console bridge rather than
                             needing a second one
``ai_assistant._handle_action``  a plain module function with a **local**
                             dispatch dict; wrapping the module attribute adds
                             our action without touching the dict. Unknown
                             actions currently only ``print``, so the wrapper
                             is purely additive
``ai_assistant._conversation``   module-global ``[{role, content}]`` — the
                             answer is read **in Python**, never scraped out
                             of the DOM
===========================  =================================================

This is deliberately harder coupling than ``core/synapse.py``, which only reads
a palette. Every one of those names is private and undocumented, and a
SynapsePro release may rename any of them. So:

* each seam is feature-detected **independently**, and a missing one degrades
  to "the button doesn't appear", never a traceback;
* the whole thing is behind ``synapse.chat_kg_button`` and dies quietly when
  off;
* and the same capture is on the Knowledge Gaps panel as
  **＋ KG from AI chat**, which needs only ``_conversation`` — one private name
  instead of four. If the injected button ever silently stops appearing, the
  feature is still there.

Conciseness is **not** asked of SynapsePro. Its ``_SYSTEM_MSG`` and chip
prompts are left exactly alone — the captured text goes through Ankisstant's
own AI path against a KG-shaped schema. That way the result is identical
whether the chat is on Anthropic, OpenAI, Gemini, Ollama or the Claude CLI, the
per-tool model matrix still applies, and nothing is written into another
add-on's config.
"""

from __future__ import annotations

import importlib
import json
import re
import sys

from aqt import gui_hooks, mw

from . import log, synapse
from .config import synapse_config, tool_model_for

# The PYCALL action name our injected button emits. Namespaced because it
# travels through SynapsePro's own dispatcher, where a collision with a future
# upstream action would mean our handler silently eats it.
CHAT_ACTION = "ankisstant_kg"

# DOM id of the injected button. Also how injection tells "already there" from
# "the page reloaded and took ours with it".
BUTTON_ID = "ank-kg-btn"

# Header container in chat_ui.html that holds the info/settings buttons.
_HEADER_ID = "header-right"

# Class those buttons carry, so ours inherits SynapsePro's chat styling instead
# of being an unstyled lump between them.
_HEADER_BTN_CLASS = "hbtn"

_hooks_registered = False


# ── seams ────────────────────────────────────────────────────────────────────

def _ai_module():
    """SynapsePro's ``ai_assistant`` module, or None.

    ``sys.modules`` is tried first and is the expected hit: SynapsePro imports
    ``ai_assistant`` itself during startup. Importing it ourselves is the
    fallback rather than the first move — a module we import is a module whose
    side effects we've caused, and this one owns a dock, hooks and a webview.
    """
    pkg = synapse.package_name()
    if not pkg:
        return None
    name = f"{pkg}.ai_assistant"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _enabled() -> bool:
    return (bool(synapse_config().get("chat_kg_button", True))
            and synapse.synapse_available())


def chat_available() -> bool:
    """True when the last chat answer can be read.

    This is the *narrow* check — only ``_conversation`` — and it's what gates
    the Knowledge Gaps panel button. The injected chat button needs three more
    seams on top of this one and checks them itself.
    """
    mod = _ai_module()
    return mod is not None and isinstance(getattr(mod, "_conversation", None), list)


def last_assistant_turn() -> str:
    """The most recent completed assistant message, or "".

    Two things make this safe to read from outside:

    * ``_conversation.append({"role": "assistant", ...})`` happens only **after**
      streaming finishes, so there is no way to catch a half-streamed answer.
    * ``_trim_conversation()`` **rebinds** the global (``_conversation = system +
      tail``) rather than mutating in place — so the list is fetched through
      ``getattr`` on every call. A cached reference would keep serving turns
      from a list SynapsePro has already thrown away.
    """
    mod = _ai_module()
    if mod is None:
        return ""
    convo = getattr(mod, "_conversation", None)
    if not isinstance(convo, list):
        return ""
    for entry in reversed(convo):
        if not isinstance(entry, dict):
            continue
        if entry.get("role") == "assistant":
            return str(entry.get("content") or "").strip()
    return ""


def _user_turn_before_last_answer() -> str:
    """The question that produced the last answer, or "".

    An answer alone is often a bare explanation with the topic only implied
    ("it's driven by the same receptor"). The question is what makes a title
    naming the actual gap possible, so it goes into the prompt as context.
    """
    mod = _ai_module()
    if mod is None:
        return ""
    convo = getattr(mod, "_conversation", None)
    if not isinstance(convo, list):
        return ""
    seen_assistant = False
    for entry in reversed(convo):
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role == "assistant" and not seen_assistant:
            seen_assistant = True
            continue
        if seen_assistant and role == "user":
            return str(entry.get("content") or "").strip()
    return ""


# ── the condense step ────────────────────────────────────────────────────────

_SYSTEM = (
    "You turn a study-assistant answer into knowledge gaps for a Year 3 "
    "Australian medical student's revision queue. A knowledge gap is one "
    "specific thing worth revisiting later — not a summary of the answer. "
    "Return JSON only, no prose and no code fence."
)


def _prompt(answer: str, question: str, limit: int) -> str:
    ask = (f'The student asked:\n"""{question}"""\n\n' if question else "")
    return (
        f"{ask}The assistant answered:\n\"\"\"{answer}\"\"\"\n\n"
        f"Return a JSON array of at most {limit} knowledge gaps from this "
        f"exchange. Fewer is better — return [] if the answer contains nothing "
        f"worth revisiting.\n\n"
        f"Each element is an object:\n"
        f'  "title" — the gap itself, at most 10 words, no trailing full stop. '
        f"Name the specific fact or mechanism, not the topic area. "
        f'Good: "Digoxin toxicity worsened by hypokalaemia". '
        f'Bad: "Cardiac pharmacology" or "Revise digoxin".\n'
        f'  "why"   — one sentence, at most 25 words, on what specifically '
        f"isn't known yet. Plain prose, no markdown.\n"
        f'  "tags"  — 0-3 lowercase single-word topic tags, or [].\n\n'
        f"Do not restate the whole answer. Do not invent specifics the answer "
        f"does not contain."
    )


def _max_gaps() -> int:
    try:
        n = int(synapse_config().get("chat_kg_max", 3))
    except (TypeError, ValueError):
        n = 3
    return max(1, min(n, 10))


def _clean_title(raw) -> str:
    """Trim to the shape the queue displays.

    The word cap is enforced here as well as in the prompt because the list
    truncates at 80 characters — a model that ignores "at most 10 words" would
    otherwise produce entries whose distinguishing half is cut off.
    """
    title = re.sub(r"\s+", " ", str(raw or "")).strip().rstrip(".")
    words = title.split(" ")
    if len(words) > 14:
        title = " ".join(words[:14]).rstrip(",;:") + "…"
    return title


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(title or "").lower()).strip()


def _write(parsed) -> int:
    """Persist the parsed gaps. Returns how many were actually added."""
    from ..tools.kg import store

    if isinstance(parsed, dict):
        # A model that returns a single object instead of a one-element array
        # is common enough to be worth absorbing rather than discarding.
        parsed = [parsed]
    if not isinstance(parsed, list):
        log.error(f"chat KG: expected a list, got {type(parsed).__name__}")
        return 0

    # Deduplicate against what's already open. Pressing ＋KG twice on the same
    # answer is the obvious mistake, and silently doubling the queue is a worse
    # outcome than doing nothing.
    existing = {_norm(kg.get("title", "")) for kg in store.load_all()
                if kg.get("status") != "done"}

    added = 0
    for entry in parsed[:_max_gaps()]:
        if not isinstance(entry, dict):
            continue
        title = _clean_title(entry.get("title"))
        if not title or _norm(title) in existing:
            continue
        why = re.sub(r"\s+", " ", str(entry.get("why") or "")).strip()
        tags = entry.get("tags")
        tags = [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
        try:
            store.add(
                title=title,
                source="chat",
                type="kg",
                status="open",
                fields={"notes": why},
                tags=tags,
            )
        except Exception as e:
            log.error(f"chat KG: store.add failed: {e}")
            continue
        existing.add(_norm(title))
        added += 1
    return added


def capture_last_turn(button=None) -> None:
    """Read the last chat answer, condense it, and queue the result.

    Non-blocking: the AI call runs on a background thread with no modal
    progress dialog, so the chat stays usable while it thinks. ``button``, when
    given, is disabled for the duration so a second click can't pile on a
    duplicate request.
    """
    from aqt.utils import tooltip
    from .qt_utils import run_claude_json_async

    answer = last_assistant_turn()
    if not answer:
        tooltip("No AI answer to log yet — ask the assistant something first.",
                period=4000)
        return

    limit = _max_gaps()

    def _done(parsed) -> None:
        if parsed is None:
            return                      # run_claude_json_async already reported
        try:
            added = _write(parsed)
        except Exception as e:
            log.error(f"chat KG: writing gaps failed: {e}")
            tooltip("Couldn't save the knowledge gap — see console.", period=4000)
            return
        if added == 0:
            # Says which of the two it is. "Nothing happened" with no reason is
            # indistinguishable from a broken button.
            tooltip("Nothing new to log — already in your queue, or no gap found.",
                    period=4000)
        else:
            tooltip(f"Logged {added} knowledge gap{'s' if added != 1 else ''}.",
                    period=3000)
        try:
            from ..tools.knowledge_gaps import _refresh_open_panel
            _refresh_open_panel()
        except Exception:
            pass

    run_claude_json_async(
        button, _done,
        prompt=_prompt(answer, _user_turn_before_last_answer(), limit),
        system=_SYSTEM,
        max_tokens=700,
        model=tool_model_for("gap_analysis"),
    )


# ── the action hook ──────────────────────────────────────────────────────────

def _install_action_hook(mod) -> bool:
    """Wrap ``ai_assistant._handle_action`` so our action is dispatched too.

    Safe because ``ChatPage.javaScriptConsoleMessage`` resolves the name through
    module globals at call time rather than holding a reference — so rebinding
    the attribute is enough, and SynapsePro's own dispatch dict is untouched.

    The kill switch is re-read **inside** the wrapper rather than gating the
    install. Un-patching cleanly would mean restoring a function that a third
    add-on may have wrapped in the meantime, and putting back a stale reference
    is how you delete someone else's hook.
    """
    if getattr(mod, "_ank_kg_hook", False):
        return True
    original = getattr(mod, "_handle_action", None)
    if not callable(original):
        return False

    def wrapped(action, data):
        if action == CHAT_ACTION and _enabled():
            try:
                capture_last_turn()
            except Exception as e:
                log.error(f"chat KG capture failed: {e}")
            return
        return original(action, data)

    try:
        mod._handle_action = wrapped
        mod._ank_kg_hook = True
    except Exception as e:
        log.error(f"chat KG: couldn't install action hook: {e}")
        return False
    return True


# ── button injection ─────────────────────────────────────────────────────────

# A card with a plus on it, matching the ＋ already in SynapsePro's launcher
# strip. `currentColor` so it follows the header's own text colour through both
# light/dark and every SynapsePro accent theme — a hardcoded hex would go
# invisible on half of them.
_SVG = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round">'
    '<rect x="3" y="5" width="13" height="14" rx="2.4"/>'
    '<path d="M19 12v6M16 15h6"/></svg>'
)


def _inject_js(limit: int) -> str:
    return """
(function () {
  try {
    if (document.getElementById(%(id)s)) { return 'exists'; }
    var host = document.getElementById(%(header)s);
    if (!host) { return 'no-header'; }
    if (typeof sendToPython !== 'function') { return 'no-bridge'; }
    var b = document.createElement('button');
    b.id = %(id)s;
    b.className = %(cls)s;
    b.setAttribute('aria-label', 'Log as knowledge gap');
    b.title = %(tip)s;
    b.innerHTML = %(svg)s;
    b.addEventListener('click', function () {
      sendToPython(%(action)s, {});
    });
    host.insertBefore(b, host.firstChild);
    return 'ok';
  } catch (e) {
    return 'error: ' + e;
  }
})();
""" % {
        "id": json.dumps(BUTTON_ID),
        "header": json.dumps(_HEADER_ID),
        "cls": json.dumps(_HEADER_BTN_CLASS),
        "svg": json.dumps(_SVG),
        "action": json.dumps(CHAT_ACTION),
        "tip": json.dumps(
            f"Log this answer as a knowledge gap in Ankisstant "
            f"(up to {limit})"
        ),
    }


def _on_js_result(result) -> None:
    # 'exists' is the overwhelmingly common result (every _ensure after the
    # first) and is not worth a line in the log.
    if result in ("ok", "exists", None):
        return
    log.error(f"chat KG button injection: {result}")


def _inject(view) -> None:
    try:
        page = view.page()
    except Exception:
        return
    if page is None:
        return
    try:
        page.runJavaScript(_inject_js(_max_gaps()), _on_js_result)
    except Exception as e:
        log.error(f"chat KG: runJavaScript failed: {e}")


def _connect_reload(view) -> None:
    """Re-inject after the chat page reloads.

    ``_action_page_ready`` fires on every load and resets the conversation, so
    the button goes with the old document. The connection is flagged on the
    *page* object rather than on this module: SynapsePro can build a new page,
    and a module-level flag would then leave the new one unhooked forever.
    """
    try:
        page = view.page()
    except Exception:
        return
    if page is None or getattr(page, "_ank_kg_reload_hooked", False):
        return
    try:
        page.loadFinished.connect(lambda _ok, v=view: _inject(v))
        page._ank_kg_reload_hooked = True
    except Exception as e:
        log.error(f"chat KG: couldn't hook page reload: {e}")


def _ensure() -> None:
    """Idempotent: resolve the seams, hook the action, put the button back."""
    if not _enabled():
        return
    mod = _ai_module()
    if mod is None:
        return
    if not _install_action_hook(mod):
        return
    view = getattr(mod, "_webview", None)
    if view is None:
        return                          # chat dock not built yet; a retry will get it
    _connect_reload(view)
    _inject(view)


def _on_state_change(*_args) -> None:
    _ensure()


def register() -> None:
    """Called once per profile open, alongside the launcher-strip injection.

    SynapsePro creates its chat webview lazily — not at startup, but the first
    time the AI dock is opened — so a single attempt at profile open would
    almost always find nothing. Hence retries *and* a state hook, and hence
    ``_ensure`` being cheap and idempotent: it runs a lot.
    """
    global _hooks_registered
    for delay in (600, 1500, 3500):
        try:
            mw.progress.single_shot(delay, _ensure, False)
        except Exception:
            pass
    if _hooks_registered:
        return
    try:
        gui_hooks.state_did_change.append(_on_state_change)
        _hooks_registered = True
    except Exception as e:
        log.error(f"chat KG: hook registration failed: {e}")
