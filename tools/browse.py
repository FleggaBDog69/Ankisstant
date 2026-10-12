# AI Browse — find Anki cards for a topic via AI-generated search terms,
# then tag and unsuspend in one step.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

import re
import threading

from aqt import mw
from aqt.qt import (
    QCheckBox,
)
from aqt.utils import tooltip

from ..core import anki_utils, api as core_api, log
from ..core.config import (
    tool_config, tool_model_for, save_tool_config,
)
from ..core.qt_utils import (
    is_manual_provider,
    provider_configured,
)


NAME = "AI Browse"


# ── prompts ───────────────────────────────────────────────────────────────────

SEARCH_TERMS_SYSTEM = (
    "You generate Anki search terms for a medical student's deck. Given a topic, "
    "return a JSON array of 3 to 8 search strings that surface cards genuinely "
    "about that topic — not cards that just mention it in passing.\n\n"
    "HOW ANKI SEARCH WORKS (critical — most failures come from ignoring this):\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. So a 3–4 word term like 'psoriasis cell-mediated immunity' almost "
    "always returns ZERO, because no single card contains all those words. Keep "
    "most terms to the topic anchor PLUS ONE discriminating keyword — usually two "
    "words (e.g. 'psoriasis IL-17', 'psoriasis Th17'). Use a three-word term only "
    "when those exact words plausibly co-occur on one card.\n\n"
    "USE THE DECK'S VOCABULARY, NOT TEXTBOOK PROSE:\n"
    "Cards are written in terse exam shorthand, not prose. Approach the concept "
    "through the angles the deck actually uses — the key cytokine/mediator, the "
    "classic drug, the named sign, the tightly-linked condition — so terms match "
    "how cards are phrased. For 'psoriasis Th1/Th17 pathogenesis', strong terms "
    "are 'psoriasis IL-17', 'psoriasis Th17', 'psoriasis secukinumab', "
    "'psoriasis IL-23' — far more likely to hit than 'cell-mediated immunity' or "
    "'pathogenesis T cells', which are prose, not card text.\n\n"
    "ANCHOR ON THE FULL NAME THE CARD USES — NOT THE ABBREVIATION:\n"
    "The input is often casual shorthand ('sxs pd', 'ms flare'). Anki matches "
    "literal tokens; it will NOT expand 'PD' to 'Parkinson' or know 'sxs' means "
    "'symptoms'. Cards spell disease and anatomy names OUT. On a real deck "
    "'Parkinson' is on 343 cards but the bare token 'PD' on 24 — an anchor of "
    "'PD ...' misses ~93% of the deck; likewise 'MSA' (2) vs 'Multiple System "
    "Atrophy' (5), 'CN3' (2) vs 'oculomotor'/'CN III'/'third nerve' (dozens). So "
    "translate the input to the full clinical name and anchor THERE: 'sxs pd' → "
    "'Parkinson symptoms', 'Parkinson clinical features'. A short disease/anatomy "
    "abbreviation may appear ONLY as an EXTRA term, never the sole anchor (each "
    "term is one AND-query, so 'both forms' means TWO terms). Keep a token "
    "verbatim only where the deck prints it that way: molecular/receptor/genetic "
    "tokens (IL-17, Th17, TNF, HLA-B27, D2) and universally-abbreviated entities "
    "(HIV, COPD, ACE). Prefer the common clinical word over the technical synonym "
    "('dilated pupil' not 'mydriasis').\n\n"
    "DECOMPOSITION:\n"
    "If the input combines MORE THAN ONE distinct concept — joined by 'and', "
    "commas, semicolons, '=', 'causes', 'associated with', or a chain of reasoning "
    "failures ('failed to recognise X, missed Y') — SPLIT it into the underlying "
    "atomic concepts and generate terms for each. A relational input such as "
    "'psoriasis = Th1 cells' is an ASSOCIATION: produce terms pairing the two "
    "entities AND terms for each entity on its own. Don't hunt for one card "
    "matching the whole compound statement — the pieces are scattered across "
    "separate cards.\n"
    "Examples: 'MRI first line for HA with neuro deficit, missed red flags' → "
    "{headache red flags} and {headache focal neuro imaging} as separate groups; "
    "'aortic stenosis murmur and indications for valve replacement' → "
    "{aortic stenosis murmur} and {AVR indications}.\n\n"
    "RULES:\n"
    "- The anchor + one-discriminator (≈2-word) term is the workhorse. Favour "
    "eponyms, drugs, cytokines, and named signs over generic words like "
    "'pathogenesis', 'mechanism', or 'immunity', which rarely appear on cards.\n"
    "- Avoid generic 1–3 letter abbreviations ALONE (e.g. 'MS', 'DM', 'IV') — "
    "disambiguate to 'multiple sclerosis'. But DO keep meaningful domain shorthand "
    "the deck itself uses (IL-17, Th17, TNF, HLA-B27).\n"
    "- For narrow single-concept topics, return 3–4 terms. For decomposed or "
    "relational inputs, 2–3 terms per concept (up to 8 total). Quality over "
    "quantity.\n\n"
    "Return ONLY the JSON array of strings. No prose, no quotes around the array."
)


RESCOPE_SYSTEM = (
    "You regenerate Anki search terms for a medical student. You will be given a topic, "
    "the previous set of search terms, and a directive to go BROADER or NARROWER.\n\n"
    "BROADER: pull back a level of abstraction. Include adjacent / parent concepts, "
    "the wider disease category, related syndromes, broader exam topics. Replace "
    "narrow eponyms with their umbrella category.\n"
    "NARROWER: drill in. Use more specific sub-entities, drug names, exact "
    "diagnostic criteria, named signs, specific complications. Replace umbrella "
    "categories with their highest-yield sub-entries.\n\n"
    "Anki ANDs space-separated words (every word must be on the card), so keep "
    "each term to an anchor + one keyword (≈2 words); long phrases return nothing. "
    "Use the deck's shorthand (cytokines, drugs, eponyms), not textbook prose.\n\n"
    "Same JSON-array-of-strings format as before. 3–6 items. No prose."
)

# The search-terms request now folds a hierarchical tag suggestion and/or an MQ
# knowledge-gap explanation into the SAME round-trip via the declarative engine
# (tools/kg/engine.py) — see _search_with_system. When either is requested the
# reply becomes an object instead of a bare array.

TAG_SEARCH_SYSTEM = (
    "You generate Anki tag-name keywords for a medical student. Given a topic, "
    "return a JSON array of keyword strings that match tag names for THAT topic "
    "and its tightly-coupled disease entities only.\n\n"
    "STAY TIGHT — this is the most important rule:\n"
    "- Return the topic itself, plus AT MOST a couple of closely-related disease "
    "entities or direct differentials (e.g. for 'multiple sclerosis': "
    "multiple_sclerosis, optic_neuritis).\n"
    "- DO NOT branch out to the individual signs, symptoms, lab findings, "
    "investigations, or buzzwords associated with the topic. For 'multiple "
    "sclerosis' that means NO oligoclonal_bands, NO Uhthoff, NO MRI — those flood "
    "the results with tangents. Only widen to a separate condition, never to a "
    "feature of the topic.\n"
    "- 2 to 4 items, fewer is better. Quality over coverage.\n\n"
    "Format — JSON array of objects exactly like:\n"
    '[{"keyword": "multiple_sclerosis", "resource": "Boards & Beyond — Neuro: MS", "step": "Step 1"}, ...]\n\n'
    "RULES:\n"
    "- keyword is matched case-insensitively as a substring against existing tag names, "
    "so prefer the canonical form (snake_case or PascalCase, no spaces).\n"
    "- Avoid 1–3 letter abbreviations on their own. Disambiguate them (e.g. 'multiple_sclerosis', "
    "not 'MS').\n"
    "- resource is a concise study reference (Boards & Beyond chapter, Pathoma section, "
    "First Aid page-range, etc.).\n"
    "- step is one of 'Step 1', 'Step 2 CK', 'Step 3', or 'Step 1+2' if it spans both. "
    "Use 'AMC' for Australian-context entries when clearly post-graduation.\n\n"
    "Return ONLY the JSON array. No prose."
)

# ── helpers ──────────────────────────────────────────────────────────────────

_STEP_RE = re.compile(r"step[ _]?([123])", re.IGNORECASE)


def _front_preview(note, front_field: str, limit: int = 140) -> str:
    fld = note[front_field] if front_field in note else note.fields[0]
    text = anki_utils.strip_html(fld)
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


# ── panel ────────────────────────────────────────────────────────────────────

# ── Native-browser AI search ─────────────────────────────────────────────────
#
# A small "✨ AI Search" checkbox sits right next to Anki's own search bar in
# the native card browser. When it's on, pressing Enter doesn't run the typed
# text as a literal Anki query — instead it's sent to AI as a topic (the exact
# same SEARCH_TERMS_SYSTEM prompt and run_claude_json plumbing as AI Browse),
# and the returned terms are combined into one query and searched instead.
# Toggle it off to use the search bar normally. This keeps the workflow fully
# inside Anki's familiar browser — no separate panel or popup to learn.

_AI_CHECKBOX_TOOLTIP = (
    "When on, typing a topic and pressing Enter sends it to AI for Anki search "
    "terms (the same process as AI Browse) and searches for those instead of "
    "the literal text. Turn off to search normally."
)


def _on_native_ai_toggle(checked: bool) -> None:
    try:
        cfg = tool_config("browse")
        cfg["native_ai_search_enabled"] = bool(checked)
        save_tool_config("browse", cfg)
    except Exception as e:
        print(f"[ankisstant] persist native AI search toggle failed: {e}")


def _scoped_native_ai_query(cfg: dict, terms: list[str]) -> str:
    """AND the AI-generated OR-group with the user's deck/tag/suspended scope
    filters from Settings → AI Browse → "Native browser AI Search — scope"."""
    clauses = ["(" + " OR ".join(f"({t})" for t in terms) + ")"]
    scope = cfg.get("native_ai_search_scope") or {}
    decks = [d.strip() for d in (scope.get("decks") or []) if d.strip()]
    if decks:
        clauses.append("(" + " OR ".join(f'deck:"{d}"' for d in decks) + ")")
    tags = [t.strip() for t in (scope.get("tags") or []) if t.strip()]
    if tags:
        clauses.append("(" + " OR ".join(f'tag:"{t}"' for t in tags) + ")")
    suspended = (scope.get("suspended") or "any").strip().lower()
    if suspended == "suspended":
        clauses.append("is:suspended")
    elif suspended == "unsuspended":
        clauses.append("-is:suspended")
    return " ".join(clauses)


def _apply_native_ai_results(browser, cfg: dict, reply) -> None:
    if not isinstance(reply, list) or not all(isinstance(t, str) for t in reply):
        return  # ask_claude_json already reported the failure
    terms = [t.strip() for t in reply if t.strip()]
    if not terms:
        tooltip("AI returned no usable search terms.")
        return

    query = _scoped_native_ai_query(cfg, terms)
    try:
        # search_for() (lowercase) sets _lastSearchTxt, updates the line edit
        # text and runs the search directly — unlike search_for_terms(), it
        # does NOT call back into onSearchActivated, so it can't re-enter our
        # wrapper above and mistake the generated query for a fresh topic.
        browser.search_for(query)
    except Exception as e:
        print(f"[ankisstant] AI search couldn't run query: {e}")
        return
    tooltip("AI search for: " + ", ".join(terms), period=4000)


def _set_native_ai_busy(checkbox, line_edit, busy: bool) -> None:
    """Toggle the inline '⏳ Thinking…' state on the checkbox itself — a
    lightweight in-place indicator next to the search bar instead of the modal
    progress dialog (which yanks focus to the main Anki window)."""
    try:
        checkbox.setText("✨ AI Search ⏳ Thinking…" if busy else "✨ AI Search")
        checkbox.setEnabled(not busy)
        line_edit.setEnabled(not busy)
    except RuntimeError:
        pass  # browser closed mid-call


def _run_native_ai_search(browser, checkbox, line_edit, topic: str) -> None:
    if not provider_configured():
        tooltip("Set up an AI provider in Ankisstant Settings first.", period=4000)
        return

    cfg = tool_config("browse")
    model = tool_model_for("native_search")
    kwargs = dict(prompt=f"Topic: {topic}", system=SEARCH_TERMS_SYSTEM,
                  max_tokens=512, model=model)

    if is_manual_provider():
        # Interactive: the manual provider shows its own copy/paste dialog and
        # must run on the main thread — no inline indicator needed here, the
        # dialog itself is the "loading" state.
        reply = core_api.ask_claude_json(**kwargs)
        _apply_native_ai_results(browser, cfg, reply)
        return

    # Run the call on a background thread with a small inline indicator —
    # *not* the modal QProgressDialog from run_claude_json/_run_claude, which
    # is parented to the main window and yanks focus away from the browser.
    _set_native_ai_busy(checkbox, line_edit, True)
    cancel_ev = threading.Event()

    def _bg():
        core_api.set_cancel_event(cancel_ev)
        try:
            return core_api.ask_claude_json(show_errors=False, **kwargs)
        finally:
            core_api.set_cancel_event(None)

    def _done(fut):
        _set_native_ai_busy(checkbox, line_edit, False)
        try:
            reply = fut.result()
        except Exception as e:
            log.error(f"native AI search failed: {e}")
            tooltip("Claude call failed — see console for details.", period=4000)
            return
        if reply is None:
            tooltip("Claude call failed — see console for details.", period=4000)
            return
        _apply_native_ai_results(browser, cfg, reply)

    # Network-only AI call — keep it off the collection executor.
    mw.taskman.run_in_background(_bg, _done, uses_collection=False)


def install_native_ai_search(browser) -> None:
    """Add the '✨ AI Search' checkbox next to the browser's search bar and
    wrap onSearchActivated so that, while it's checked, pressing Enter sends
    the typed topic to AI and searches the generated terms instead of the
    literal text. Called once per browser window from __init__.py via
    gui_hooks.browser_menus_did_init (which — despite the name — fires once
    at window construction, the usual place addons add one-off browser UI,
    and crucially *before* Browser.setupSearch() wires up the search bar)."""
    try:
        search_edit = browser.form.searchEdit
        line_edit = search_edit.lineEdit()

        cfg = tool_config("browse")
        cb = QCheckBox("✨ AI Search")
        cb.setToolTip(_AI_CHECKBOX_TOOLTIP)
        cb.setChecked(bool(cfg.get("native_ai_search_enabled", False)))
        cb.toggled.connect(_on_native_ai_toggle)

        # The search bar and the Cards/Notes switch both live in
        # browser.form.gridLayout, row 0 — switch at column 0, search box at
        # column 1 (see aqt.browser.browser.Browser.setup_table /
        # _aqt.forms.browser_qt6). Column 2 is free; placing the checkbox
        # there puts it inline with both, not appended after the whole page.
        grid = getattr(browser.form, "gridLayout", None)
        if grid is not None:
            grid.addWidget(cb, 0, 2)
        else:
            search_edit.parentWidget().layout().addWidget(cb)

        # Wrap the bound onSearchActivated as an instance attribute. Because
        # this hook fires before setupSearch() runs, Anki's own
        # `qconnect(line_edit.returnPressed, self.onSearchActivated)` resolves
        # `self.onSearchActivated` to *our* wrapper (instance attributes shadow
        # class methods) — so a real Enter-press in the search bar routes
        # through us. Other paths that trigger a search programmatically
        # (sidebar clicks via search_for, AI Browse's search_for_terms, etc.)
        # either skip onSearchActivated entirely or invoke it as a plain method
        # call rather than through the line edit's signal — neither carries its
        # signal context, so checking sender() lets us tell a genuine keypress
        # apart from those and only intercept the former.
        original_on_search_activated = browser.onSearchActivated

        def _wrapped_on_search_activated():
            if cb.isChecked() and browser.sender() is line_edit:
                topic = browser.current_search().strip()
                if topic:
                    _run_native_ai_search(browser, cb, line_edit, topic)
                    return
            original_on_search_activated()

        browser.onSearchActivated = _wrapped_on_search_activated
        browser._ankisstant_ai_search_checkbox = cb
    except Exception as e:
        log.error(f"native AI search install failed: {e}")


# ── Tool contract ────────────────────────────────────────────────────────────

def init(main_window) -> None:
    """No-op for now — tool has no startup work."""
    return None
