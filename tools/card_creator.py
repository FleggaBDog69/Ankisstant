# AI Create — propose cloze cards from source or topic, review,
# then create or open in Add Screen.
#
# Exposes init() and get_panel() per the Ankisstant tool contract.

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime as _dt

from aqt import mw, gui_hooks
from aqt.qt import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QIcon,
    QImage, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPixmap,
    QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSize, QSpinBox, Qt,
    QTimer, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from . import autotag
from . import pdf_render
from . import quality_pass as qp
from ..grounding import guidelines as grounding
from ..core import anki_utils, api as core_api, log
from ..core.config import (
    active_family, auto_tag_base, kg_type_info,
    load_config, month_tag, mq_explain_enabled, tool_config, tool_model_for,
    save_tool_config,
)
from ..core.qt_utils import (
    attach_tag_completer, is_manual_provider, loading, make_help_button,
    make_setup_banner, provider_configured, run_claude_json,
    set_ai_buttons_enabled,
)
from .kg import engine


NAME = "AI Create"


# ── prompts ───────────────────────────────────────────────────────────────────

CARD_GEN_SYSTEM = """You generate high-yield Anki cloze cards for a Year 3 Australian medical student.

OUTPUT FORMAT
Return ONLY a JSON array. Each element:
  {"front": "<cloze text with {{c1::...}} / {{c2::...}} etc>",
   "extra": "<the clinical 'so what' or mechanism — do NOT repeat the front>"}
No prose outside the JSON array. No markdown fences.

THE ONE THING THAT MATTERS: RETRIEVAL FORCE
Every card has a CLOZE (what gets retrieved) and a CUE (the visible text around it). A
card is only worth making if the cue forces the student to RECALL the answer from memory
— not read it off the prompt or recognise a label.

Two kinds of cue. Only one is allowed:
- LABEL CUE (BANNED): names the answer's own category and asks you to fill it in. The
  retrieval path is keyword → answer; it doesn't exist on the ward.
    BAD: "The first-line treatment for anaphylaxis is {{c1::adrenaline}}."  ← the cue IS the answer's restated label.
- PROCESSING CUE (REQUIRED): gives a mechanism, presentation, contrast, cause, or
  consequence the student must work THROUGH to reach the answer.
    GOOD: "{{c1::Adrenaline}} reverses the bronchospasm, vasodilation, and capillary leak of anaphylaxis."  ← same fact, same atomicity, mechanism cue.

THE TEST (apply to every card before writing it): if I delete the answer, can the reader
reconstruct it from the TYPE OF THINKING the cue demands — or only from recognising a
label? If only the label, redesign the cue.

CUE TOOLKIT — build the visible part from one of:
  Mechanism ("the drug that reverses the bronchospasm of…") · Presentation ("a patient
  with stridor and hypotension after a sting…") · Contrast ("what distinguishes X from
  Y…") · Cause ("why does…") · Consequence ("what happens if…").
Function over anatomy: cue what a structure DOES, not its origin/insertion.

PRECISION vs LEAKAGE: give enough context that exactly ONE answer fits, WITHOUT restating
the answer's label. Specify the SHAPE of the answer (a drug? a vessel? a phase?), not
which specific one. "The immediate drug…" says it's a drug without saying which.

SUBSTRING CHECK (run on every card before finalising): does the cloze answer appear as a
literal substring or shared word-stem of any word in the visible cue? If yes it's a
morphological give-away — redesign.
    BAD: "Vasospastic angina is caused by coronary {{c1::spasm}}."  ("spasm" sits inside "Vasospastic")

ATOMICITY: one retrievable unit per card. Multi-fact cards spawn sibling clozes that
DESYNC under spaced repetition — the easy sub-fact keeps the card off the queue while the
hard one never matures.
- Split a multi-component fact into separate clozes rather than clozing the whole phrase:
    CORRECT: prevents {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}
    WRONG:   prevents {{c1::anterior shear of L5 on S1}}
- Coupled-but-separable facts (a disease name AND its vessel) are TWO cards, each cued
  from a different angle — not one card with two clozes.

LIST-SHAPED CONTENT ("the N X are A, B, C…", or enumeration words three/four/several):
STOP and pick a format deliberately —
  A) SPLIT into N cards, each cued from that item's own mechanism/presentation. Default
     for long lists (≥5) or items with independent meaning.
  B) ONE card with HINT SYNTAX {{c1::answer::hint}} — for short lists (≤4), conceptually
     paired, where the gestalt "what are the X" recall matters. Hints anchor each cloze
     to its retrieval angle without giving the answer away.
  C) ONE card without hints, ACCEPTING desync — only for fixed sequences / mnemonics /
     ordered cascades that lose meaning if split.
Never default to a multi-cloze enumeration without choosing A/B/C.

CLOZE NUMBERING (commonly done wrong):
- Separate facts each meant to be hidden on their OWN sibling card → number sequentially
  {{c1::…}}, {{c2::…}}, {{c3::…}}.
- Deletions meant to be revealed TOGETHER as a single card → give them the SAME number.
- Reusing {{c1::…}} for every separate fact collapses them into one card — WRONG. When in
  doubt, number c1, c2, c3.

INHERENT LABEL-ASSOCIATION (rare, allowed): some content is genuinely a name↔feature
mapping with no processing-cue equivalent — mnemonics, eponym→lesion (Marfan →
{{c1::cystic medial degeneration}}), etymologically self-revealing terms. Write these
honestly. But FIRST apply the test: could a processing cue preserve the same fact? If
YES, write the better version — a flag here is laziness. Definitional clozes and
"first-line treatment of X" are FIXABLE, never inherent.

CLOZE FORMAT & STYLE
- Cloze only (no Q&A). 1–3 clozes per card, each a discrete fact. Short answers (1–4 words).
- LEVEL: Year 3 Australian med student; Australian drugs/guidelines (eTG, RACGP, PBS,
  ANZCOR) where relevant.
- Ruthless concision — cut filler ("important to know that…", "remember that…"). No
  bold/headers/bullets inside the cloze front.
- Cloze the YIELD (the high-value fact), not background framing. If a fact only makes
  sense with framing, put the framing UNCLOZED in the front and cloze the testable part.

EXTRA FIELD: the clinical "so what", mechanism, or exam pearl — never a repeat of the
front. 2–4 sentences max. Skip if you have nothing meaningful to add. <b>/<u> sparingly;
no other HTML.

DO NOT include any prose outside the JSON array. No markdown fences. Just the JSON array."""


QA_GEN_SYSTEM = """You generate high-yield question/answer Anki cards for a Year 3 Australian medical student, in the Malleus Clinical Medicine submission style.

OUTPUT FORMAT
Return ONLY a JSON array. Each element:
  {"front": "<a single, specific question>",
   "extra": "<the complete answer — the text shown on the back>",
   "source": "<one acceptable Australian source with a URL, or '' if none>"}
No cloze deletions. No prose outside the JSON array. No markdown fences.

THE ONE THING THAT MATTERS: a clean question that forces recall.
- The FRONT is one focused question the student must answer from memory — not a
  recognition prompt, not a list of sub-parts. One question, one idea.
- The EXTRA (back) is the full answer: direct, clinically relevant, complete enough
  to stand alone. Use <b>/<u> and <br> sparingly; bullet lists with <br> where it aids
  clarity. Do not restate the question.
- Australian guidelines and spelling throughout. Clinically relevant, exam-useful.
- SOURCE: cite one acceptable source with a real URL. NEVER invent a URL — if you have
  no verifiable source, use an empty string. When a citation allow-list is provided
  below, cite only from it and carry through its [LIVE]/[TRAINING DATA] label.
- Repeat nothing verbatim from the source material; rephrase into a clean Q/A.

DO NOT include any prose outside the JSON array. No markdown fences. Just the JSON array."""


SPLIT_SYSTEM = (
    "Split the supplied Anki card into multiple ATOMIC single-cloze cards. Each output card "
    "must test ONE fact, contain exactly ONE cloze deletion ({{c1::...}}), and stand on its own. "
    "Keep cloze answers short (1–4 words). Preserve the clinical meaning of the original. "
    "Return ONLY a JSON array of {front, extra} objects — no prose, no markdown fences."
)

def _augment_system(base: str, profile: dict | None) -> str:
    """Append a NOTETYPE block to a system prompt so Claude knows which
    notetype the output will populate, which fields are available, and
    any user-supplied per-notetype style guidance (e.g. Malleus style).

    The skill file itself is NOT inlined here — instead, the CLI is told
    to Read it on demand (see _resolve_skill), keeping the system prompt
    small. API mode uses the server-side skills beta via `skill_id`."""
    if not profile:
        return base
    extra: list[str] = ["", "NOTETYPE", f"You are writing cards for the '{profile.get('name', '?')}' notetype."]
    extra.append(
        f"The student will store the card front in <{profile.get('front_field', 'Text')}> "
        f"and supporting text in <{profile.get('extra_field', 'Extra')}>."
    )
    nt = None
    try:
        nt = mw.col.models.by_name(profile.get("name", ""))
    except Exception:
        nt = None
    if nt is not None:
        fields = [f["name"] for f in nt.get("flds", [])]
        if fields:
            extra.append("All available fields on this notetype: " + ", ".join(fields) + ".")
    sources_field = (profile.get("sources_field") or "").strip()
    if sources_field:
        extra.append(
            f"This notetype has a dedicated source/citation field <{sources_field}>. "
            "Include a 'source' key in each JSON object alongside 'front' and 'extra' — "
            "its value will be written verbatim to that field. Use HTML "
            "(e.g. <a href='…'>title</a>, <br> between citations) when appropriate."
        )
    instructions = (profile.get("extra_instructions") or "").strip()
    if instructions:
        extra.append("Additional style instructions for this notetype:")
        extra.append(instructions)
    extra.append(
        "Do NOT change the JSON shape — still return {\"front\": ..., \"extra\": ...} "
        "(or an array of them, depending on the original instruction). The 'front' key "
        "always maps to the front field, 'extra' to the extra field."
    )
    return base + "\n" + "\n".join(extra)


ONE_BY_ONE_SYSTEM = (
    "Rewrite the supplied Anki card as a SINGLE card whose cloze deletions are revealed one "
    "at a time during review. Every cloze MUST use the same number — {{c1::...}} — so they "
    "act as siblings. The card will be paired with the AnKing 'One by one' field set to 'y', "
    "which causes the AnKing reviewer JS to reveal each c1 cloze in sequence. "
    "Rules:\n"
    "- Output ONE JSON object: {\"front\": ..., \"extra\": ...}.\n"
    "- ALL cloze deletions in front must be {{c1::...}} (no c2, c3, …).\n"
    "- Order the clozes in the sequence the student should reveal them.\n"
    "- Keep each cloze answer short (1–4 words).\n"
    "- Preserve the original clinical meaning; don't add new facts.\n"
    "- No prose outside the JSON object. No markdown fences."
)


def _kg_type_meta(kg_type_key: str) -> dict | None:
    """Look up a KG type's settings (auto_tag flag + prefix) from config.
    Returns None if no matching type exists."""
    if not kg_type_key:
        return None
    try:
        types = tool_config("knowledge_gaps").get("types") or []
    except Exception:
        return None
    key = kg_type_key.lower().strip()
    for t in types:
        if isinstance(t, dict) and str(t.get("key", "")).lower() == key:
            return t
    return None


def _apply_tag_levels(gap: dict, type_meta: dict, levels: dict) -> str:
    """Format {system, subsystem, topic} levels into the consolidated
    {base}::{type}::… tag, cache it on the gap, and persist it to the KG store.
    Whether to tag at all is the caller's call (the Auto-tag checkbox); this only
    needs the type's name segment. Returns '' if unusable (no base/levels)."""
    tag = autotag.tag_from_levels(levels, type_meta=type_meta)
    if not tag:
        return ""
    gap["auto_tag"] = tag
    kg_id = gap.get("kg_id")
    if kg_id:
        try:
            from .kg import store as kg_store
            existing = kg_store.get(kg_id)
            if existing:
                fields = dict(existing.get("fields") or {})
                fields["auto_tag"] = tag
                kg_store.update(kg_id, fields=fields)
        except Exception as e:
            print(f"[ankisstant] auto-tag persist failed: {e}")
    return tag


def _topic_tag_from_levels(levels: dict, type_meta: dict | None) -> str:
    """Build a {base}::{type}::… hierarchical tag from classification levels
    for a NO-GAP generation (plain topic/source, or pasted cards). Uses the
    given KG type's segment, falling back to the default type. Returns '' when
    unusable (no base, no levels, or it collapses to just the base). Unlike
    _apply_tag_levels this has no gap to cache on, so it's a pure format. Thin
    wrapper over the shared autotag formatter so Create and Browse agree."""
    return autotag.tag_from_levels(levels, type_meta=type_meta)


def _tag_material(gap: dict | None, topic_label: str | None,
                  source_label: str | None, focus: str) -> str:
    """The text handed to the shared classifier for a separate-call auto-tag
    (skill flows, where the tag can't be folded into the bare card reply).
    For a gap: its title + stem + notes; otherwise the topic / focus / source."""
    bits: list[str] = []
    if isinstance(gap, dict):
        if (gap.get("title") or "").strip():
            bits.append(gap["title"].strip())
        stem = re.sub(r"<[^>]+>", " ", str(gap.get("stem_html") or ""))
        stem = re.sub(r"\s+", " ", stem).strip()
        if stem:
            bits.append(stem[:600])
        if (gap.get("notes") or "").strip():
            bits.append(gap["notes"].strip()[:300])
    else:
        if topic_label:
            bits.append(str(topic_label))
        if focus:
            bits.append(str(focus))
        if source_label:
            bits.append(str(source_label))
    return "\n".join(bits).strip()


# Tag classification and the MQ explanation are folded into the card-gen request
# via _merged_gen_instructions() (built below), so they come back in the same
# round-trip — important in manual/BYO mode where each call is a copy/paste.


def _generate_auto_tag_for_gap(gap: dict, type_meta: dict, model: str | None = None) -> str:
    """Classify this KG (via the shared classifier) and format + cache the tag
    on the gap. Returns '' on failure. Used by skill flows, where the tag can't
    be folded into the bare card reply."""
    if not isinstance(gap, dict) or not isinstance(type_meta, dict):
        return ""
    # Cached on the gap dict (set by a previous Generate run for the same KG).
    cached = (gap.get("auto_tag") or "").strip()
    if cached:
        return cached
    if not auto_tag_base() or not (gap.get("title") or "").strip():
        return ""
    levels = autotag.classify(_tag_material(gap, None, None, ""), model=model)
    if not isinstance(levels, dict):
        return ""
    # _apply_tag_levels formats AND caches the tag on the gap + KG store.
    return _apply_tag_levels(gap, type_meta, levels)


# ── MQ knowledge-gap explanation ───────────────────────────────────────────────
#
# Missed-question (MQ) cards lead their Missed Questions field with the specific
# concept missed ("Knowledge gap: …") and a brief AI-written explanation of it,
# above the captured screenshot. The explanation is requested in the SAME AI
# round-trip as the cards (folded into an object reply — see
# _merged_gen_instructions), so connected-AI and BYO/manual users both get it
# without an extra call. It's then cached on the gap + persisted to the KG store
# so it's never regenerated. Gated by the qbank `mq_explain` setting.

def _wants_mq_explanation(gap: dict | None) -> bool:
    """True when this gap is an MQ gap with a concept to explain and no
    explanation cached yet, and the feature is enabled."""
    if not mq_explain_enabled() or not isinstance(gap, dict):
        return False
    if (gap.get("kg_type") or "").lower() != "mq":
        return False
    if (gap.get("explanation") or "").strip():
        return False
    return bool((gap.get("concept") or gap.get("title") or "").strip())


# Shared wording for what the explanation should be — reused by the merged
# instructions and the separate-call fallback so both ask for the same thing.
_MQ_EXPLANATION_GUIDANCE = (
    "a brief teaching explanation (1-3 plain sentences) of the specific concept "
    "the student missed — explain the underlying mechanism or principle (the "
    "WHY/HOW), not just a restatement; be factually careful and don't invent "
    "specifics; pitch it at a Year 3 Australian medical student; plain prose, no "
    "markdown"
)


def _persist_mq_explanation(gap: dict, text: str) -> str:
    """Cache an MQ explanation on the gap dict and write it back to the KG
    store so it survives and is reused. Returns the stored text."""
    text = (text or "").strip()
    if not isinstance(gap, dict) or not text:
        return ""
    gap["explanation"] = text
    kg_id = gap.get("kg_id")
    if kg_id:
        try:
            from .kg import store as kg_store
            existing = kg_store.get(kg_id)
            if existing:
                fields = dict(existing.get("fields") or {})
                fields["explanation"] = text
                kg_store.update(kg_id, fields=fields)
        except Exception as e:
            print(f"[ankisstant] mq explanation persist failed: {e}")
    return text


def _gap_field_values(gap: dict | None) -> dict:
    """Flatten a gap into {field_key: value} for the engine's note_field_plan —
    the stored fields blob plus any top-level scalar values (captured + AI). Used
    to route user-defined literal-field targets into their Anki fields."""
    if not isinstance(gap, dict):
        return {}
    out = dict(gap.get("fields") or {})
    for k, v in gap.items():
        if k != "fields" and isinstance(v, (str, int, float)):
            out.setdefault(k, v)
    return out


def _persist_ai_field_values(gap: dict, values: dict) -> None:
    """Cache per-note AI field values (the explanation + any custom AI fields the
    engine produced) on the gap dict and write them into the KG store so they
    survive and aren't regenerated. Generalises _persist_mq_explanation to N
    declarative fields. Pure-ish: only touches the gap dict + the KG store."""
    if not isinstance(gap, dict) or not values:
        return
    text_vals = {str(k): str(v).strip() for k, v in values.items()
                 if isinstance(v, (str, int, float)) and str(v).strip()}
    if not text_vals:
        return
    gap.update(text_vals)
    kg_id = gap.get("kg_id")
    if kg_id:
        try:
            from .kg import store as kg_store
            existing = kg_store.get(kg_id)
            if existing:
                fields = dict(existing.get("fields") or {})
                fields.update(text_vals)
                kg_store.update(kg_id, fields=fields)
        except Exception as e:
            print(f"[ankisstant] ai field persist failed: {e}")


MQ_EXPLANATION_SYSTEM = (
    "You write " + _MQ_EXPLANATION_GUIDANCE + ".\n\n"
    "Given the concept the student missed (and optional context from the question "
    "they got wrong), return ONLY the explanation text — no preamble, headings, or "
    "markdown."
)


def _generate_mq_explanation_for_gap(gap: dict, model: str | None = None) -> str:
    """Separate-call fallback for the MQ explanation — used only when it can't
    be folded into the card-gen request (skill flows produce a bare array, so
    the object trick doesn't apply). Returns '' on failure; result is cached +
    persisted via _persist_mq_explanation."""
    if not _wants_mq_explanation(gap):
        return (gap.get("explanation") or "").strip() if isinstance(gap, dict) else ""
    concept = (gap.get("concept") or gap.get("title") or "").strip()
    context_bits = [f"Concept missed: {concept}"]
    stem = (gap.get("stem_html") or "").strip()
    if stem:
        plain = re.sub(r"<[^>]+>", " ", stem)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            context_bits.append(f"Question they got wrong: {plain[:600]}")
    notes = (gap.get("notes") or "").strip()
    if notes:
        context_bits.append(f"Their notes: {notes[:300]}")
    try:
        text = core_api.ask_claude(
            prompt="\n".join(context_bits), system=MQ_EXPLANATION_SYSTEM,
            max_tokens=400, model=model, show_errors=False,
        )
    except Exception as e:
        print(f"[ankisstant] mq explanation call failed: {e}")
        return ""
    return _persist_mq_explanation(gap, text or "")


# ── small helpers ────────────────────────────────────────────────────────────

def _profile_for(cfg: dict, name: str) -> dict | None:
    """Look up a notetype profile by name from the config. Returns None if no
    profile exists; callers should fall back to the legacy top-level fields."""
    for p in cfg.get("notetypes", []) or []:
        if p.get("name") == name:
            return p
    return None


def _resolved_profile(cfg: dict, name: str) -> dict:
    """Return a profile dict for `name`, synthesising one from the legacy
    top-level field names if no curated profile exists. The synthesised
    profile has no extra_instructions."""
    p = _profile_for(cfg, name)
    if p:
        return p
    return {
        "name":               name,
        "front_field":        cfg.get("front_field", "Text"),
        "extra_field":        cfg.get("extra_field", "Extra"),
        "image_field":        cfg.get("extra_field", "Extra"),
        "one_by_one_field":   cfg.get("one_by_one_field", "One by one"),
        "extra_instructions": "",
    }


def _resolve_skill(profile: dict | None) -> tuple[str, str]:
    """Decide which skill to apply for a card-creation request.

    Returns (skill_invocation, api_skill_id):
      - skill_invocation → free-text prefix prepended to the CLI prompt,
                           e.g. '/malleus-anki' or 'Use the malleus-anki
                           skill'. The SKILL.md body itself lives in
                           ~/.claude/skills/<name>/ and is auto-loaded by
                           Claude Code on demand.
      - api_skill_id     → Anthropic custom skill ID passed to the skills
                           beta (forces the API path in api.py).

    Mode rules (provider is cfg["provider"]):
      - cli       → invocation only (ignore API ID).
      - anthropic → API skill ID only (ignore invocation).
      - auto / gemini / openai → if API ID is set, use it (forces the Anthropic
                    API skills path); otherwise fall back to the CLI invocation.
                    (Skills are Anthropic-only, so gemini/openai behave like auto.)
    """
    if not profile:
        return ("", "")
    # Skill-vs-prompt is mutually exclusive per notetype. In 'prompt' mode the
    # profile's inline/field instructions are sent and NO skill is applied, so the
    # engine's declarative prompts aren't fighting a skill's tuned output. Default
    # (legacy profiles): 'skill' iff a skill is configured, else 'prompt'.
    mode = str(profile.get("card_creation_mode") or "").lower()
    skill_id = (profile.get("card_creation_skill_id") or "").strip()
    invocation = (profile.get("card_creation_skill_invocation") or "").strip()
    if not mode:
        mode = "skill" if (skill_id or invocation) else "prompt"
    if mode != "skill":
        return ("", "")
    cfg = load_config()
    provider = (cfg.get("provider") or "auto").lower()
    if provider == "cli":
        return (invocation, "")
    if provider == "anthropic":
        if invocation and not skill_id:
            log.warning(
                f"card_creator: profile '{profile.get('name', '?')}' has a CLI "
                "skill invocation but provider=anthropic; upload the skill to "
                "Anthropic and paste its skill_… ID to activate it."
            )
        return ("", skill_id)
    # auto / gemini / openai
    if skill_id:
        return ("", skill_id)
    return (invocation, "")


# ── quality pass helpers ────────────────────────────────────────────────────

def _quality_pass_active(cfg: dict, profile: dict | None) -> bool:
    """Whether the card quality pass runs for this creation. A per-notetype
    override ('on'/'off') beats the global toggle; 'inherit' (default) defers to
    the global `enabled` flag. Lets e.g. the Q&A Malleus notetype force it off
    while cloze decks use it."""
    qpc = cfg.get("quality_pass") or {}
    ov = (profile or {}).get("quality_pass_override", "inherit")
    if ov == "on":
        return True
    if ov == "off":
        return False
    return bool(qpc.get("enabled", False))


def _atomicity_max(cfg: dict) -> int:
    """User-configured atomicity sensitivity: the maximum number of independent
    clozes allowed on one note before the quality pass flags it (D2). Defaults to
    2 (a clean 2-cloze note passes; 3+ independent facts flag). Shared by the
    deterministic prefilter, the LLM grader prompt, and the generator guidance so
    all three agree. See tools/quality_pass.py."""
    qpc = cfg.get("quality_pass") or {}
    try:
        n = int(qpc.get("atomicity_max_clozes", 2))
    except (TypeError, ValueError):
        n = 2
    return n if n >= 1 else 1


def _gen_atomicity_note(max_clozes: int) -> str:
    """Generation-side counterpart to qp.atomicity_directive: tells the cloze
    generator how many coupled clozes the user tolerates, so the scorer doesn't
    flag cards it was instructed to produce. Appended to CARD_GEN_SYSTEM."""
    n = max(1, int(max_clozes))
    return (
        f"\n\nATOMICITY BUDGET: you may place up to {n} cloze deletion(s) on one "
        f"note when the facts are genuinely coupled (e.g. one multi-component fact "
        f"split across clozes that share a core). Prefer fewer. Only exceed {n} "
        f"for fixed sequences or mnemonics that lose meaning if split — never for "
        f"an enumeration of independent facts (split those into separate cards)."
    )


def _topic_directive(topic: str, n: int) -> str:
    """Extra instruction folded into the TOPIC user message so a topic phrased as
    a specific factual ASSERTION ("TB meningitis presents with hydrocephalus")
    yields a card testing exactly that stated fact — instead of the model
    treating it as a broad subject and drifting to adjacent facts. Generic: no
    fragile 'is this a sentence' detection, the model decides. Tightens further
    when only a few cards are requested."""
    lines = [
        "If this TOPIC is phrased as a specific factual statement or assertion, "
        "the FIRST card MUST test exactly that stated fact directly, before any "
        "further cards expand to closely-related high-yield facts. Do not drift "
        "to adjacent topics at the expense of the stated fact.",
    ]
    if n <= 2:
        lines.append(
            "Few cards were requested — stay tightly on the literal stated fact; "
            "do not pad with loosely-related material."
        )
    return " ".join(lines)


def _breadth_directive(n: int, mode: str) -> str:
    """Scale card depth and breadth to the requested count. The same topic yields
    very different card sets at N=1 vs N=50: one card should be the single
    highest-yield fact (a quick, broad-strokes card); fifty should fan out across
    the topic's subtopics AND drill into mechanism and detail. Folded into every
    generation request. `mode` is 'topic' or 'source' — in source mode breadth is
    bounded by the supplied material (extract more/deeper from it) rather than
    ranging over the whole subject."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    source_bound = (mode == "source")
    where = "the source" if source_bound else "this topic"
    if n <= 2:
        scope = (
            f"only {n} card(s) requested — make the SINGLE highest-yield, most "
            f"exam-critical card on {where}: the one thing you would test if you "
            "could test only one. Broad strokes, no minutiae or secondary detail."
        )
    elif n <= 8:
        scope = (
            f"a small set — cover the core high-yield facts of {where} that a "
            "student must know first. Stay on the essentials; skip secondary detail."
        )
    elif n <= 20:
        scope = (
            f"a medium set — cover the main concepts across {where} with moderate "
            "depth: key mechanisms, presentations, and management, not just "
            "headline facts."
        )
    else:
        if source_bound:
            scope = (
                "a large set — be comprehensive: exhaustively extract the testable "
                "facts in the source, including secondary detail and mechanism, "
                "not just the headline points."
            )
        else:
            scope = (
                "a large set — be comprehensive: fan out across ALL the topic's "
                "subtopics (epidemiology, pathophysiology, diagnosis, the relevant "
                "organ systems, management, complications, monitoring) AND go "
                "deeper into mechanisms, distinctions, and less-common but testable "
                "detail."
            )
    cards_word = "card" if n == 1 else "cards"
    return (
        "CARD COUNT & SCOPE: " + scope +
        " Match depth and breadth to the count; never pad with low-yield filler to "
        f"hit the number — if {where} genuinely can't support {n} strong "
        f"{cards_word}, make fewer excellent ones."
    )


def _grounding_active(cfg: dict, profile: dict | None, mode: str) -> bool:
    """Whether to inject the guideline citation allow-list for this creation.
    Topic-mode only — in source mode the user's pasted material IS the source.
    A per-notetype override ('on'/'off') beats the global `grounding.enabled`
    toggle; 'inherit' (default) defers to it. Mirrors _quality_pass_active."""
    if mode != "topic":
        return False
    ov = str((profile or {}).get("grounding_override", "inherit")).lower()
    if ov == "on":
        return True
    if ov == "off":
        return False
    gc = cfg.get("grounding") or {}
    return bool(gc.get("enabled", True))


def _grounding_fetch_live(cfg: dict) -> bool:
    gc = cfg.get("grounding") or {}
    return bool(gc.get("fetch_live", False))


# ── auto-image (model-flagged visual relevance) ───────────────────────────────

# Appended to the system prompt when the "🖼️ Auto-image" toggle is on. The
# model emits an `image_query` ONLY for cards a real image would materially help
# (derm, radiology, anatomy, histology, ECG, gross pathology); the review screen
# then auto-fetches candidate thumbnails for exactly those cards.
IMAGE_QUERY_INSTRUCTION = (
    "\n\nIMAGE RELEVANCE\n"
    "For any card where a real medical image would materially aid learning "
    "(e.g. dermatology lesions/rashes, radiology X-ray/CT/MRI, anatomy, "
    "histology, ECG tracings, gross pathology, clinical signs), add an "
    "\"image_query\" key to that card's JSON object: a short, specific image "
    "search phrase (e.g. \"erythema multiforme target lesions\", "
    "\"tension pneumothorax chest x-ray\"). OMIT the key entirely for purely "
    "conceptual/definitional cards where an image adds nothing. Do not change "
    "any other key. Never let image_query alter the front/extra content."
)


# ── slide decks (PDF) ─────────────────────────────────────────────────────────
#
# When the source is a slide-deck PDF, we rasterise each page to an image up
# front and ask the model to tag every card with the slide it came from. The
# review screen then auto-attaches that slide image to the card (and lets the
# user swap it via a slide gallery). Appended to the system prompt only when a
# single PDF is attached and we managed to render its pages.
def _slide_index_instruction(n_slides: int) -> str:
    return (
        "\n\nSLIDE SOURCE\n"
        f"The attached PDF is a slide deck with {n_slides} slides (slide 1 is the "
        "first page). For EACH card, add an integer \"slide\" key = the single "
        "slide number the card is primarily based on (1-based). If a card draws "
        "from no particular slide, omit the key or use 0. The slide image is "
        "attached to the card automatically — do not describe the slide in the "
        "card content, and never let \"slide\" change the front/extra text."
    )


def _sync_slide_images(card: dict, slide_pages: list) -> None:
    """Reconcile a card's slide-derived images with its current slide selection.

    `card["_slide_indices"]` is the list of 0-based page indices chosen for this
    card (seeded from the model's 1-based "slide" the first time). Their resolved
    temp-PNG paths are kept in `card["_slide_paths"]` and mirrored at the FRONT
    of `card["_image_paths"]`, so the existing image pipeline imports + injects
    them with no other change. Non-slide images the user attached are preserved.
    Idempotent — safe to re-run after a re-render or a gallery edit."""
    if not slide_pages:
        return
    idxs = card.get("_slide_indices")
    if idxs is None:
        s = card.get("slide")
        try:
            si = int(s)
        except (TypeError, ValueError):
            si = 0
        idxs = [si - 1] if 1 <= si <= len(slide_pages) else []
        card["_slide_indices"] = idxs
    new_slide = [slide_pages[i] for i in idxs if 0 <= i < len(slide_pages)]
    old_slide = card.get("_slide_paths") or []
    rest = [p for p in (card.get("_image_paths") or []) if p not in old_slide]
    card["_image_paths"] = new_slide + rest
    card["_slide_paths"] = new_slide


def _auto_image_active(cfg: dict) -> bool:
    """Global default for the per-batch auto-image toggle."""
    return bool((cfg.get("images") or {}).get("auto_find", False))


def _grader_model(cfg: dict) -> str | None:
    """The grader model for the active family — the dedicated quality_pass model
    if set, else the generator model, else the family default (resolved later)."""
    qpc = cfg.get("quality_pass") or {}
    fam = active_family()
    return tool_model_for("quality_pass", fam)


def _grader_skill(cfg: dict, profile: dict | None) -> tuple[str, str]:
    """(skill_invocation, skill_id) for grading. Skills are Anthropic-only, so
    return ('','') off Anthropic or when 'prefer_skill' is off — the caller then
    falls back to the bundled inline rubric, which works on every provider."""
    qpc = cfg.get("quality_pass") or {}
    if not qpc.get("prefer_skill", True):
        return ("", "")
    provider = (load_config().get("provider") or "auto").lower()
    if provider not in ("auto", "cli", "anthropic"):
        return ("", "")
    inv = (qpc.get("grader_skill_invocation") or "").strip()
    sid = (qpc.get("grader_skill_id") or "").strip()
    if provider == "cli":
        return (inv, "")
    if provider == "anthropic":
        return ("", sid)
    return ("", sid) if sid else (inv, "")  # auto: prefer the API id


def _regenerate_one(old_card: dict, hint: str, profile: dict | None,
                    focus: str, model: str | None) -> dict | None:
    """Regenerate ONE card on the same subject, honouring an optional hint and
    focus. Returns a new {front, extra} dict (preserving per-card images) or None
    on failure. Shared by the review dialog's Regenerate button and the
    quality-pass auto-regenerate loop.

    Routes through the SAME creator system prompt + skill used for fresh
    generation (not a stripped-down regen prompt), so the rewritten card keeps
    the full style/formatting conventions (e.g. <b>/<u>, Extra layout) and any
    notetype guidance. A quality-pass FAIL reason / user feedback rides along as
    improvement notes — the card is re-created with that fix, not regenerated
    from a thin prompt."""
    is_qa = str((profile or {}).get("card_format", "cloze")).lower() == "qa"
    if hint:
        instruction = (
            "This card needs improvement. Rewrite it as a single, better card that "
            "tests the SAME fact, fixing the following: " + hint
        )
    else:
        instruction = (
            "Rewrite this card as a single, better card on the same fact — a "
            "stronger cue or wording while keeping the same retrieval target."
        )
    prompt = (
        "Existing card to improve.\n"
        "Front:\n" + old_card.get("front", "") + "\n\n"
        "Extra:\n" + old_card.get("extra", "") + "\n\n"
        + instruction + "\n"
        + (f"Focus: {focus}\n" if focus else "")
        + "Return a JSON array containing EXACTLY ONE card object."
    )
    skill_invocation, skill_id = _resolve_skill(profile)
    base_system = QA_GEN_SYSTEM if is_qa else CARD_GEN_SYSTEM
    new = core_api.ask_claude_json(
        prompt=prompt,
        system=_augment_system(base_system, profile),
        max_tokens=1024, model=model,
        skill_id=skill_id, skill_invocation=skill_invocation,
    )
    # The creator system prompt + skill emit an array; accept a bare object too.
    if isinstance(new, list):
        new = next((c for c in new if isinstance(c, dict)), None)
    if not isinstance(new, dict) or "front" not in new or "extra" not in new:
        return None
    if old_card.get("_image_paths"):
        new["_image_paths"] = list(old_card["_image_paths"])
    # Carry the slide attachment across a regeneration — the rewritten card is
    # the same fact from the same slide.
    for k in ("slide", "_slide_indices", "_slide_paths"):
        if old_card.get(k) is not None:
            new[k] = list(old_card[k]) if isinstance(old_card[k], list) else old_card[k]
    return new


def resolve_card_validity(cards) -> bool:
    """True when `cards` is a non-empty list of {front, extra} dicts."""
    return (isinstance(cards, list) and bool(cards)
            and all(isinstance(c, dict) and "front" in c and "extra" in c
                    for c in cards))


def grade_cards_sync(cards: list, profile: dict | None, cfg: dict,
                     *, same_call_grades=None) -> list | None:
    """Qt-free quality-pass grading for the background worker (and the manual
    main-thread path). Mirrors CreatorPanel._run_quality_pass but uses a direct
    core_api.ask_claude_json call (no modal dialog) and does NOT auto-regenerate
    (that needs Qt). Returns a list of qp.Verdict (one per card, fail-open to
    PASS) or None when the quality pass is inactive. Never raises."""
    try:
        if not _quality_pass_active(cfg, profile):
            return None
        llm_verdicts = None
        try:
            if same_call_grades is not None:
                llm_verdicts = qp.parse_same_call_grades(same_call_grades, cards)
            else:
                skill_inv, skill_id = _grader_skill(cfg, profile)
                use_skill = bool(skill_inv or skill_id)
                max_clozes = _atomicity_max(cfg)
                system = (qp.skill_batch_system(max_clozes) if use_skill
                          else qp.rubric_batch_system(max_clozes))
                raw = core_api.ask_claude_json(
                    prompt=qp.batch_prompt(cards), system=system,
                    max_tokens=2048, model=_grader_model(cfg),
                    skill_id=skill_id, skill_invocation=skill_inv,
                    show_errors=False,
                )
                if raw is not None:
                    llm_verdicts = qp.parse_batch_verdicts(raw, cards)
        except Exception as e:
            print(f"[ankisstant] background quality pass grade skipped: {e}")
            llm_verdicts = None
        verdicts = qp.apply_prefilters_then(llm_verdicts, cards, _atomicity_max(cfg))
        graded = llm_verdicts is not None
        # Mirror the panel rule: when ungraded, only surface deterministic
        # pre-filter findings (don't paint everything PASS).
        return [vd if (graded or vd.source == "prefilter") else None
                for vd in verdicts]
    except Exception as e:
        print(f"[ankisstant] background quality pass skipped: {e}")
        return None


# Cards built here are loaded straight into Anki's native editor webview
# (`ac.set_note`). A full-resolution clinical image (Radiopaedia / search-engine
# results are routinely 5000px+ and tens of MB) decoded in that webview can
# exhaust memory and beach-ball the whole app. Downscale + re-encode anything
# oversized BEFORE it enters the media collection.
_IMG_MAX_DIM = 1600          # px on the longest side
_IMG_REENCODE_BYTES = 1_500_000  # files larger than this get re-encoded even if small-dim


def _normalize_image_file(path: str) -> str:
    """Return a path to a web-safe (downscaled, re-encoded) copy of `path` when
    it is oversized; otherwise return `path` unchanged. Never raises — on any
    problem the original path is returned so attachment still works."""
    try:
        if not path or not os.path.exists(path):
            return path
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        img = QImage(path)
        if img.isNull():
            return path  # not a raster Qt understands (e.g. SVG) — leave as-is
        w, h = img.width(), img.height()
        longest = max(w, h)
        if longest <= _IMG_MAX_DIM and size <= _IMG_REENCODE_BYTES:
            return path  # already small enough; keep original bytes/quality
        if longest > _IMG_MAX_DIM:
            img = img.scaled(
                _IMG_MAX_DIM, _IMG_MAX_DIM,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        has_alpha = img.hasAlphaChannel()
        ext = ".png" if has_alpha else ".jpg"
        fd, out = tempfile.mkstemp(prefix="ankisstant_img_norm_", suffix=ext)
        os.close(fd)
        ok = img.save(out, "PNG" if has_alpha else "JPG", -1 if has_alpha else 85)
        if ok and os.path.getsize(out) > 0:
            return out
        try:
            os.remove(out)
        except OSError:
            pass
        return path
    except Exception as e:
        print(f"[ankisstant] image normalize failed for {path}: {e}")
        return path


def _import_image_to_media(path: str) -> str | None:
    """Copy an image into Anki's media folder; return the stored filename
    (which may differ from the original basename if there's a collision).
    Oversized images are downscaled first to keep the editor responsive."""
    try:
        return mw.col.media.add_file(_normalize_image_file(path))
    except Exception as e:
        print(f"[ankisstant] media.add_file failed for {path}: {e}")
        return None


def _img_tags_for(paths: list[str]) -> str:
    """Import a list of image paths into the media folder and return an
    HTML fragment of <img> tags (one per line) to append to a field."""
    bits: list[str] = []
    for p in paths or []:
        fname = _import_image_to_media(p)
        if not fname:
            continue
        bits.append(f'<img src="{html.escape(fname, quote=True)}">')
    return "<br>".join(bits)


def _focus_directive(focus: str) -> str:
    """Wrap the user's free-text Focus into a hard, high-priority instruction.

    A bare "Focus: …" line gets treated as a soft hint and is easily ignored
    (e.g. the model won't cloze the *name of a test* when asked). Phrasing it
    as an overriding requirement makes the model actually follow it."""
    return (
        "FOCUS — these instructions are MANDATORY and override the default card "
        "selection. Follow them exactly, even if it means clozing things you "
        "would normally leave unclozed (e.g. the name of a test/sign/criterion):\n"
        f"{focus}"
    )


def _fetch_url(url: str) -> str:
    # Many sites (e.g. austroads.gov.au) reject requests whose User-Agent
    # doesn't look like a real browser, returning HTTP 403. Send a full
    # browser-like header set so we read like Safari/Chrome rather than a bot.
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Safari/605.1.15"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = anki_utils.strip_html(text)
    return text[:30000]  # Claude does fine with ~30k chars


# OOXML drawing-text namespace, used by both PPTX slides and notes.
_PPTX_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _natural_key(name: str):
    """Sort 'slide2.xml' before 'slide10.xml'."""
    return [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", name)]


def _extract_pptx_text(path: str) -> str:
    """Pull text out of a .pptx without external deps: it's a zip of XML.
    Walks each slide in order, then notes."""
    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slide_names = sorted(
            (n for n in zf.namelist()
             if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=_natural_key,
        )
        note_names = sorted(
            (n for n in zf.namelist()
             if n.startswith("ppt/notesSlides/notesSlide") and n.endswith(".xml")),
            key=_natural_key,
        )
        for i, name in enumerate(slide_names, 1):
            with zf.open(name) as fh:
                try:
                    root = ET.parse(fh).getroot()
                except ET.ParseError:
                    continue
            paragraphs: list[str] = []
            for para in root.iter(_PPTX_A_NS + "p"):
                runs = [t.text or "" for t in para.iter(_PPTX_A_NS + "t")]
                line = "".join(runs).strip()
                if line:
                    paragraphs.append(line)
            if paragraphs:
                out.append(f"\n## Slide {i}\n" + "\n".join(paragraphs))
        for name in note_names:
            with zf.open(name) as fh:
                try:
                    root = ET.parse(fh).getroot()
                except ET.ParseError:
                    continue
            texts = [(t.text or "").strip() for t in root.iter(_PPTX_A_NS + "t")]
            joined = "\n".join(t for t in texts if t)
            if joined.strip():
                out.append(f"\n## Notes ({os.path.basename(name)})\n{joined}")
    return "\n".join(out).strip()


def _log_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return os.path.join(uf, "card_creation_log.json")


def _append_session(mode, source, topic, cards_proposed, cards_created, card_ids):
    entry = {
        "timestamp": _dt.now().isoformat(timespec="seconds"),
        "mode": mode,
        "source": source,
        "topic": topic,
        "cards_proposed": cards_proposed,
        "cards_created": cards_created,
        "card_ids": card_ids,
    }
    path = _log_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
    data.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── input panel ──────────────────────────────────────────────────────────────

def _prompt_for_pasted_cards(parent):
    """A single paste box for the user to drop the card JSON their own AI
    produced — a bare [...] array, or a {"tags":…, "cards":[…]} object. Code
    fences and surrounding chatter are tolerated (core_api.extract_json).
    Returns the parsed value, or None if cancelled / nothing usable found."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Paste cards from your AI")
    dlg.setMinimumSize(560, 460)
    v = QVBoxLayout(dlg)

    intro = QLabel(
        "Paste the JSON your AI produced — either a bare array of "
        "<code>{\"front\", \"extra\"}</code> cards, or a "
        "<code>{\"tags\": …, \"cards\": […]}</code> object. Code fences and "
        "surrounding text are fine."
    )
    intro.setTextFormat(Qt.TextFormat.RichText)
    intro.setWordWrap(True)
    v.addWidget(intro)

    box = QPlainTextEdit()
    box.setPlaceholderText('[{"front": "…{{c1::…}}…", "extra": "…"}]')
    v.addWidget(box, 1)

    err = QLabel("")
    err.setStyleSheet("color: #c0392b;")
    err.setWordWrap(True)
    err.setVisible(False)
    v.addWidget(err)

    bb = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    bb.button(QDialogButtonBox.StandardButton.Ok).setText("Review cards")
    v.addWidget(bb)

    holder: dict = {"value": None}

    def _accept():
        raw = box.toPlainText().strip()
        if not raw:
            err.setText("Paste your AI's reply first.")
            err.setVisible(True)
            return
        parsed = core_api.extract_json(raw)
        if parsed is None:
            err.setText(
                "Couldn't find valid JSON in that paste. Make sure you copied "
                "the whole reply (the array or object), then try again."
            )
            err.setVisible(True)
            return
        holder["value"] = parsed
        dlg.accept()

    bb.accepted.connect(_accept)
    bb.rejected.connect(dlg.reject)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return holder["value"]


class CreatorPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("card_creator")
        # Set by refresh_queue_state when the panel is driven by Browse's
        # LO-gap queue. self._current_gap holds the gap text we pre-filled
        # the form with; we pop it from the queue only on a successful
        # ReviewDialog acceptance.
        self._main_window = None
        # _current_gap is the dict at queue[0] we pre-filled the form with —
        # popped only on a successful ReviewDialog acceptance.
        self._current_gap: dict | None = None
        self._build()
        self._rebuild_queue_view()
        self.refresh_ready_lists()

    @staticmethod
    def _gap_title(gap) -> str:
        if isinstance(gap, dict):
            return gap.get("title", "") or ""
        return str(gap or "")

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>AI Create</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "AI Create — help",
            "<h3>What it does</h3>"
            "<p>Draft Anki cloze cards from a topic, pasted text, a URL, "
            "or attached PDFs / PowerPoints. Review each card before adding.</p>"
            "<h3>Input modes</h3>"
            "<ul>"
            "<li><b>From topic</b> — AI writes cards from scratch on a topic.</li>"
            "<li><b>From source</b> — paste text, fetch a URL, or attach a PDF / PPTX. "
            "PDFs are passed to AI directly. PPTX files have their text "
            "extracted locally (image-only slides won't work).</li>"
            "</ul>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Pick mode, set focus / tags, choose deck.</li>"
            "<li>Click <b>Generate</b>. Review each draft in the dialog.</li>"
            "<li>Approve / split / regenerate cards. Then either:</li>"
            "<ul>"
            "<li><b>Create all</b> — adds directly to the deck.</li>"
            "<li><b>Open Add screen</b> — feeds them one by one into Anki's "
            "Add Cards window so you can tweak before adding. Closing the Add "
            "window stops the queue.</li>"
            "</ul></ol>"
            "<h3>Settings</h3>"
            "<p>Default deck, notetype, audit tag, and field names are in "
            "<b>Ankisstant Settings → AI Create</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        # ── Queue panel — shows ALL queued LO gaps, lets the user add / remove
        # / reorder, and pre-fills the form below from the top item. ──────────
        self.queue_box = QFrame()
        self.queue_box.setObjectName("queueBox")
        self.queue_box.setFrameShape(QFrame.Shape.StyledPanel)
        self.queue_box.setStyleSheet(
            "QFrame#queueBox { background: rgba(80,160,255,0.16); "
            "border: 1px solid rgba(80,160,255,0.55); border-radius: 6px; }"
        )
        qbl = QVBoxLayout(self.queue_box)
        qbl.setContentsMargins(10, 8, 10, 8)
        qbl.setSpacing(5)

        self.queue_header = QLabel()
        self.queue_header.setStyleSheet("font-weight: 600; color: palette(text);")
        self.queue_header.setTextFormat(Qt.TextFormat.RichText)
        self.queue_header.setWordWrap(True)
        qbl.addWidget(self.queue_header)

        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(140)
        self.queue_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; color: palette(text); }"
            "QListWidget::item:selected { background: rgba(80,160,255,0.30); color: palette(text); }"
        )
        qbl.addWidget(self.queue_list)

        # Manual-add row.
        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Add gap:"))
        self.queue_add_input = QLineEdit()
        self.queue_add_input.setMinimumWidth(420)
        self.queue_add_input.setPlaceholderText("Type a gap statement and press Enter to queue it.")
        self.queue_add_input.returnPressed.connect(self._on_add_gap_manually)
        add_row.addWidget(self.queue_add_input, 1)
        self.queue_add_btn = QPushButton("+ Add")
        self.queue_add_btn.setAutoDefault(False)
        self.queue_add_btn.clicked.connect(self._on_add_gap_manually)
        add_row.addWidget(self.queue_add_btn)
        qbl.addLayout(add_row)

        # Action buttons.
        queue_btn_row = QHBoxLayout()
        self.queue_remove_btn = QPushButton("Remove selected")
        self.queue_remove_btn.setAutoDefault(False)
        self.queue_remove_btn.clicked.connect(self._on_remove_selected_gap)
        queue_btn_row.addWidget(self.queue_remove_btn)
        queue_btn_row.addStretch(1)
        # Load Next force-fills the form from queue[0]. Replaces the
        # implicit behaviour where typing in the topic field silently
        # blocked the queue from ever populating.
        self.queue_load_btn = QPushButton("Load next →")
        self.queue_load_btn.setAutoDefault(False)
        self.queue_load_btn.setToolTip(
            "Take the top item from the queue and fill the form with it "
            "(topic, focus context, KG images). Asks before overwriting "
            "anything you've already typed."
        )
        self.queue_load_btn.clicked.connect(self._on_load_top_gap)
        self.queue_skip_btn = QPushButton("Skip current")
        self.queue_skip_btn.setAutoDefault(False)
        self.queue_skip_btn.clicked.connect(self._on_skip_gap)
        self.queue_clear_btn = QPushButton("Clear queue")
        self.queue_clear_btn.setAutoDefault(False)
        self.queue_clear_btn.clicked.connect(self._on_clear_queue)
        queue_btn_row.addWidget(self.queue_load_btn)
        queue_btn_row.addWidget(self.queue_skip_btn)
        queue_btn_row.addWidget(self.queue_clear_btn)
        qbl.addLayout(queue_btn_row)

        root.addWidget(self.queue_box)

        # Mode toggle
        mode_box = QGroupBox("Mode")
        ml = QHBoxLayout(mode_box)
        self.rb_source = QRadioButton("Source-based (more reliable)")
        self.rb_topic = QRadioButton("Topic-based (verify carefully)")
        self.rb_source.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_source)
        grp.addButton(self.rb_topic)
        ml.addWidget(self.rb_source)
        ml.addWidget(self.rb_topic)
        ml.addStretch(1)
        root.addWidget(mode_box)

        # Source area
        self.source_box = QGroupBox("Source")
        sl = QVBoxLayout(self.source_box)
        sl.addWidget(QLabel("Paste text below, enter a URL, and/or attach PDFs / PowerPoints."))
        self.url = QLineEdit()
        self.url.setMinimumWidth(500)
        self.url.setPlaceholderText("https://… (optional)")
        sl.addWidget(self.url)
        self.text = QPlainTextEdit()
        self.text.setMinimumHeight(140)
        self.text.setPlaceholderText("Paste lecture notes / textbook excerpt / etc.")
        sl.addWidget(self.text, 1)

        # Attachments row
        self._attached_paths: list[str] = []
        attach_row = QHBoxLayout()
        self.attach_btn = QPushButton("📎 Attach PDF / PPTX…")
        self.attach_btn.setAutoDefault(False)
        self.attach_btn.setToolTip(
            "Attach a PDF (e.g. a lecture slide deck) or PowerPoint as the source.\n"
            "Slide-deck PDFs: each card gets its source slide image attached, and "
            "image-only slides work on any AI (the pages are sent as images)."
        )
        self.attach_btn.clicked.connect(self._on_attach_files)
        self.attach_clear_btn = QPushButton("Clear")
        self.attach_clear_btn.setAutoDefault(False)
        self.attach_clear_btn.clicked.connect(self._on_attach_clear)
        self.attach_clear_btn.setVisible(False)
        attach_row.addWidget(self.attach_btn)
        attach_row.addWidget(self.attach_clear_btn)
        attach_row.addStretch(1)
        sl.addLayout(attach_row)

        self.attach_list = QListWidget()
        self.attach_list.setMaximumHeight(80)
        self.attach_list.setStyleSheet(
            "QListWidget { background: transparent; color: palette(text); "
            "border: 1px dashed palette(mid); border-radius: 4px; }"
        )
        self.attach_list.setVisible(False)
        sl.addWidget(self.attach_list)
        root.addWidget(self.source_box, 1)

        # Topic field
        self.topic_box = QGroupBox("Topic")
        tl = QVBoxLayout(self.topic_box)
        tl.addWidget(QLabel("Topic (Claude generates from its own knowledge — verify before exam):"))
        self.topic = QLineEdit()
        self.topic.setMinimumWidth(500)
        tl.addWidget(self.topic)
        warn = QLabel("⚠ Topic-based cards carry more hallucination risk. Always verify.")
        warn.setStyleSheet("color: #b85c00;")
        tl.addWidget(warn)
        root.addWidget(self.topic_box)
        self.topic_box.setVisible(False)

        self.rb_source.toggled.connect(self._sync_mode)
        self.rb_topic.toggled.connect(self._sync_mode)

        # Spec
        spec_box = QGroupBox("Card spec")
        f = QFormLayout(spec_box)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.n_cards = QSpinBox()
        self.n_cards.setRange(1, 40)
        self.n_cards.setValue(int(self.cfg.get("default_n_cards", 10)))
        f.addRow("Number of cards:", self.n_cards)

        # Notetype dropdown — populated from the curated profile list in
        # settings. We persist the last choice into `selected_notetype` so
        # it survives a panel rebuild. The legacy `default_notetype` is
        # honoured as a seed when nothing else is set.
        self.notetype = QComboBox()
        self._rebuild_notetype_combo()
        # Settings link so users can add profiles without leaving the panel.
        nt_row = QHBoxLayout()
        nt_row.addWidget(self.notetype, 1)
        self.notetype_settings_btn = QPushButton("Manage…")
        self.notetype_settings_btn.setAutoDefault(False)
        self.notetype_settings_btn.setToolTip(
            "Open Ankisstant Settings → AI Create to add or edit notetype profiles."
        )
        self.notetype_settings_btn.clicked.connect(self._on_open_notetype_settings)
        nt_row.addWidget(self.notetype_settings_btn)
        f.addRow("Notetype:", nt_row)
        self.notetype.currentIndexChanged.connect(self._on_notetype_changed)

        self.deck = QComboBox()
        self.deck.setEditable(True)
        if mw.col is not None:
            deck_names = sorted(d.name for d in mw.col.decks.all_names_and_ids())
            for n in deck_names:
                self.deck.addItem(n)
        default_deck = self.cfg.get("default_deck", "")
        if default_deck:
            idx = self.deck.findText(default_deck)
            if idx >= 0:
                self.deck.setCurrentIndex(idx)
            else:
                self.deck.setEditText(default_deck)
        f.addRow("Deck:", self.deck)

        tags_row = QHBoxLayout()
        self.tags = QLineEdit(", ".join(self.cfg.get("default_tags", [])))
        self.tags.setMinimumWidth(420)
        attach_tag_completer(self.tags, multi=True)
        tags_row.addWidget(self.tags, 1)
        self.cb_autotag = QCheckBox("Auto-tag")
        self.cb_autotag.setChecked(autotag.is_enabled(self.cfg))
        self.cb_autotag.setToolTip(
            "Add an AI-generated hierarchical tag "
            "(base::Type::System::Subsystem::Topic) to every card, alongside any "
            "tags you type. Uses the same scheme as AI Browse."
        )
        self.cb_autotag.toggled.connect(self._on_autotag_toggled)
        tags_row.addWidget(self.cb_autotag)
        # Grounding checkbox — reflects the global + per-notetype resolution; only
        # meaningful in topic mode (disabled in source mode, where the pasted
        # material is the source). State is (re)set by _sync_mode / _sync_grounding.
        self.cb_ground = QCheckBox("📚 Ground")
        self.cb_ground.setToolTip(
            "Inject the Australian/WA clinical-guideline citation allow-list so "
            "topic-based cards cite real URLs instead of invented ones.\n"
            "Topic mode only — in source mode your pasted material is the source.\n"
            "Default follows Settings → Create and the notetype's Grounding override."
        )
        tags_row.addWidget(self.cb_ground)
        # Auto-image — when on, generation asks the model to flag visual cards
        # (image_query) and the review screen auto-fetches candidate thumbnails
        # for exactly those. Default follows Settings → Create.
        self.cb_autoimg = QCheckBox("🖼️ Auto-image")
        self.cb_autoimg.setChecked(_auto_image_active(self.cfg))
        self.cb_autoimg.setToolTip(
            "Let the AI flag cards that would benefit from a real medical image "
            "(derm, radiology, anatomy, histology, ECG…). The review screen then "
            "suggests images for just those cards — pick one to attach.\n"
            "Default follows Settings → Create → Images."
        )
        tags_row.addWidget(self.cb_autoimg)
        f.addRow("Tags (comma-sep):", tags_row)
        audit_tag = self.cfg.get("audit_tag", "")
        if audit_tag:
            audit_hint = QLabel(
                f"<small>Audit tag <code>{audit_tag}</code> is applied automatically "
                "to every card created.</small>"
            )
            audit_hint.setTextFormat(Qt.TextFormat.RichText)
            audit_hint.setStyleSheet("color: gray;")
            f.addRow("", audit_hint)

        # Shown only when the loaded KG's type has auto-tag on: a hierarchical
        # tag is generated and added automatically, so the user needn't add
        # their own. Hidden until a qualifying gap is loaded.
        self._autotag_hint = QLabel("")
        self._autotag_hint.setTextFormat(Qt.TextFormat.RichText)
        self._autotag_hint.setStyleSheet("color: #2563eb;")
        self._autotag_hint.setWordWrap(True)
        self._autotag_hint.setVisible(False)
        f.addRow("", self._autotag_hint)

        self.focus = QLineEdit()
        self.focus.setMinimumWidth(500)
        self.focus.setPlaceholderText("e.g. focus on clinical presentation not mechanism")
        f.addRow("Focus (optional):", self.focus)

        # Panel-level images — copied into Anki media and appended to every
        # approved card's image field at create time. Per-card overrides are
        # added in the review dialog (see _CardRow).
        self._extra_image_paths: list[str] = []
        # Rendered page-images of an attached slide-deck PDF (set in _on_generate);
        # initialised here so the Paste path can reference it safely.
        self._slide_pages: list[str] = []
        img_row = QHBoxLayout()
        self.extra_img_btn = QPushButton("📷 Attach images for Extra…")
        self.extra_img_btn.setAutoDefault(False)
        self.extra_img_btn.setToolTip(
            "Pick image files (PNG/JPG/etc). They'll be added to the Extra field "
            "of every card created in this batch. Useful for flowcharts / guideline "
            "excerpts that apply to all generated cards."
        )
        self.extra_img_btn.clicked.connect(self._on_attach_extra_images)
        self.extra_img_clear_btn = QPushButton("Clear")
        self.extra_img_clear_btn.setAutoDefault(False)
        self.extra_img_clear_btn.clicked.connect(self._on_clear_extra_images)
        self.extra_img_clear_btn.setVisible(False)
        self.extra_img_find_btn = QPushButton("🔍 Find image online…")
        self.extra_img_find_btn.setAutoDefault(False)
        self.extra_img_find_btn.setToolTip(
            "Search Wikipedia and NLM Open-i for a medical image and add it to the "
            "Extra field of every card in this batch."
        )
        self.extra_img_find_btn.clicked.connect(self._on_find_image_online)
        self.extra_img_browse_btn = QPushButton("🌐 Browse…")
        self.extra_img_browse_btn.setAutoDefault(False)
        self.extra_img_browse_btn.setToolTip(
            "Open an image search inside Anki; right-click any image → Attach to "
            "add it to the Extra field of every card in this batch."
        )
        self.extra_img_browse_btn.clicked.connect(self._on_browse_image_online)
        img_row.addWidget(self.extra_img_btn)
        img_row.addWidget(self.extra_img_find_btn)
        img_row.addWidget(self.extra_img_browse_btn)
        img_row.addWidget(self.extra_img_clear_btn)
        img_row.addStretch(1)
        f.addRow("Extra images:", img_row)

        self.extra_img_list = QListWidget()
        self.extra_img_list.setMaximumHeight(70)
        self.extra_img_list.setStyleSheet(
            "QListWidget { background: transparent; color: palette(text); "
            "border: 1px dashed palette(mid); border-radius: 4px; }"
        )
        self.extra_img_list.setVisible(False)
        f.addRow("", self.extra_img_list)

        root.addWidget(spec_box)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.paste_btn = QPushButton("📋 Paste cards")
        self.paste_btn.setAutoDefault(False)
        self.paste_btn.setToolTip(
            "Already made the cards in your own ChatGPT / Claude? Paste their "
            "JSON straight in — no prompt round-trip, no copying from a box."
        )
        self.paste_btn.clicked.connect(self._on_paste_cards)
        self.paste_btn.setVisible(is_manual_provider())  # no-sub provider only
        btn_row.addWidget(self.paste_btn)
        self.go_btn = QPushButton("Generate")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self.go_btn)
        root.addLayout(btn_row)

        # One-line reassurance that a background click isn't a no-op.
        self.bg_hint = QLabel("Runs in the background — watch “In progress” below.")
        self.bg_hint.setStyleSheet("color: gray; font-size: 11px;")
        self.bg_hint.setVisible(False)
        root.addWidget(self.bg_hint)

        # ── background jobs: in-progress + ready-to-review ───────────────────
        self.jobs_box = QFrame()
        self.jobs_box.setObjectName("jobsBox")
        self.jobs_box.setFrameShape(QFrame.Shape.StyledPanel)
        self.jobs_box.setStyleSheet(
            "QFrame#jobsBox { background: rgba(80,160,255,0.16); "
            "border: 1px solid rgba(80,160,255,0.55); border-radius: 6px; }"
        )
        jbl = QVBoxLayout(self.jobs_box)
        jbl.setContentsMargins(10, 8, 10, 8)
        jbl.setSpacing(5)

        _job_list_qss = (
            "QListWidget { background: transparent; border: none; color: palette(text); }"
            "QListWidget::item:selected { background: rgba(80,160,255,0.30); color: palette(text); }"
        )

        self.inprogress_header = QLabel("In progress")
        self.inprogress_header.setStyleSheet("font-weight: 600; color: palette(text);")
        jbl.addWidget(self.inprogress_header)
        self.inprogress_list = QListWidget()
        self.inprogress_list.setMaximumHeight(90)
        self.inprogress_list.setStyleSheet(_job_list_qss)
        jbl.addWidget(self.inprogress_list)
        ip_row = QHBoxLayout()
        ip_row.addStretch(1)
        self.cancel_job_btn = QPushButton("Cancel")
        self.cancel_job_btn.setAutoDefault(False)
        self.cancel_job_btn.clicked.connect(self._on_cancel_job)
        ip_row.addWidget(self.cancel_job_btn)
        jbl.addLayout(ip_row)

        self.ready_header = QLabel("Ready to review")
        self.ready_header.setStyleSheet("font-weight: 600; color: palette(text);")
        jbl.addWidget(self.ready_header)
        self.ready_list = QListWidget()
        self.ready_list.setMaximumHeight(130)
        self.ready_list.setStyleSheet(_job_list_qss)
        self.ready_list.itemDoubleClicked.connect(lambda _it: self._on_open_job())
        jbl.addWidget(self.ready_list)
        self.ready_hint = QLabel("Double-click a generation (or select it and press Open) to review its cards.")
        self.ready_hint.setWordWrap(True)
        self.ready_hint.setStyleSheet("color: palette(text); font-size: 11px;")
        jbl.addWidget(self.ready_hint)
        rd_row = QHBoxLayout()
        rd_row.addStretch(1)
        self.open_job_btn = QPushButton("Open")
        self.open_job_btn.setAutoDefault(False)
        self.open_job_btn.clicked.connect(self._on_open_job)
        self.retry_job_btn = QPushButton("Retry")
        self.retry_job_btn.setAutoDefault(False)
        self.retry_job_btn.clicked.connect(self._on_retry_job)
        self.discard_job_btn = QPushButton("Discard")
        self.discard_job_btn.setAutoDefault(False)
        self.discard_job_btn.clicked.connect(self._on_discard_job)
        for b in (self.open_job_btn, self.retry_job_btn, self.discard_job_btn):
            rd_row.addWidget(b)
        jbl.addLayout(rd_row)
        root.addWidget(self.jobs_box)

    def _sync_mode(self):
        self.source_box.setVisible(self.rb_source.isChecked())
        self.topic_box.setVisible(self.rb_topic.isChecked())
        self._sync_grounding()

    def _sync_grounding(self) -> None:
        """Reflect the grounding resolution on the checkbox: in topic mode,
        enabled and checked to the resolved default (global + per-notetype
        override); in source mode, disabled + unchecked (the pasted material is
        the source). Fail-open — never block the panel."""
        try:
            cb = getattr(self, "cb_ground", None)
            if cb is None:
                return
            if not self.rb_topic.isChecked():
                cb.setChecked(False)
                cb.setEnabled(False)
                return
            cb.setEnabled(True)
            name = self._current_notetype_name()
            profile = (_resolved_profile(self.cfg, name)
                       if name and not name.startswith("(") else None)
            cb.setChecked(_grounding_active(self.cfg, profile, "topic"))
        except Exception as e:
            print(f"[ankisstant] grounding sync failed: {e}")

    def _on_autotag_toggled(self, checked: bool) -> None:
        self.cfg["auto_tag"] = bool(checked)
        save_tool_config("card_creator", self.cfg)
        self._update_autotag_hint(self._current_gap
                                  if isinstance(getattr(self, "_current_gap", None), dict)
                                  else None)

    def showEvent(self, ev):
        super().showEvent(ev)
        # Tag completer reads mw.col.tags.all() once at attach time. Rebuild
        # on every show so tags added since (via Anki, Browse, or another
        # session) appear in the completion list.
        try:
            attach_tag_completer(self.tags, multi=True)
        except Exception as e:
            print(f"[ankisstant] tag completer refresh failed: {e}")
        # Reflect the saved checkbox state + show the auto-tag hint on open.
        try:
            self.cb_autotag.setChecked(autotag.is_enabled(self.cfg))
            self._update_autotag_hint(self._current_gap
                                      if isinstance(getattr(self, "_current_gap", None), dict)
                                      else None)
        except Exception as e:
            print(f"[ankisstant] autotag state refresh failed: {e}")
        try:
            self._sync_grounding()
        except Exception as e:
            print(f"[ankisstant] grounding state refresh failed: {e}")

    # ── notetype dropdown ────────────────────────────────────────────────────

    def _rebuild_notetype_combo(self) -> None:
        """Reload the notetype dropdown from the configured profile list.
        Falls back to a single legacy entry if no profiles are configured.
        Restores the previously-selected notetype where possible."""
        # Block signals so the rebuild itself doesn't trigger save side-effects.
        self.notetype.blockSignals(True)
        self.notetype.clear()
        profiles = self.cfg.get("notetypes") or []
        if not profiles:
            legacy = (self.cfg.get("default_notetype") or "").strip()
            if legacy:
                self.notetype.addItem(legacy)
            else:
                self.notetype.addItem("(no notetype configured)")
                self.notetype.setEnabled(False)
        else:
            self.notetype.setEnabled(True)
            for p in profiles:
                name = p.get("name", "")
                label = name
                if p.get("extra_instructions"):
                    label = f"{name}  ✦"  # marker for custom prompt
                self.notetype.addItem(label, name)
        # Restore selection.
        selected = (self.cfg.get("selected_notetype")
                    or self.cfg.get("default_notetype") or "").strip()
        if selected:
            for i in range(self.notetype.count()):
                data = self.notetype.itemData(i) or self.notetype.itemText(i)
                if data == selected:
                    self.notetype.setCurrentIndex(i)
                    break
        self.notetype.blockSignals(False)

    def _current_notetype_name(self) -> str:
        data = self.notetype.itemData(self.notetype.currentIndex())
        if data:
            return data
        return self.notetype.currentText().strip()

    def _on_notetype_changed(self, _idx: int) -> None:
        name = self._current_notetype_name()
        if not name or name.startswith("("):
            return
        self.cfg["selected_notetype"] = name
        save_tool_config("card_creator", self.cfg)
        self._sync_grounding()

    def _on_open_notetype_settings(self) -> None:
        try:
            from ..ui.settings import open_settings
            open_settings()
        except Exception as e:
            showWarning(f"Couldn't open settings: {e}")
            return
        # Reload config + rebuild combo so newly-added profiles appear.
        self.cfg = tool_config("card_creator")
        self._rebuild_notetype_combo()

    # ── panel-level image attachment for Extra ───────────────────────────────

    def _on_attach_extra_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach images for Extra",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;All files (*)",
        )
        if not paths:
            return
        added = 0
        for p in paths:
            if p in self._extra_image_paths:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
                tooltip(f"Skipped {os.path.basename(p)} — not an image type.")
                continue
            self._extra_image_paths.append(p)
            added += 1
        if added:
            self._refresh_extra_image_list()

    def _on_find_image_online(self):
        # Seed the search with the topic (topic mode) or the focus text.
        seed = ""
        try:
            if self.rb_topic.isChecked():
                seed = self.topic.text().strip()
            if not seed:
                seed = self.focus.text().strip()
        except Exception:
            seed = ""
        try:
            from . import images
            paths = images.pick_images(self, seed)
        except Exception as e:
            showWarning(f"Image search failed: {e}")
            return
        added = 0
        for p in paths:
            if p and p not in self._extra_image_paths:
                self._extra_image_paths.append(p)
                added += 1
        if added:
            self._refresh_extra_image_list()
            tooltip(f"Added {added} image{'s' if added != 1 else ''} to Extra.")

    def _on_browse_image_online(self):
        seed = ""
        try:
            if self.rb_topic.isChecked():
                seed = self.topic.text().strip()
            if not seed:
                seed = self.focus.text().strip()
        except Exception:
            seed = ""
        try:
            from . import images
            paths = images.browse_for_images(self, seed)
        except Exception as e:
            showWarning(f"Image browse failed: {e}")
            return
        added = 0
        for p in paths:
            if p and p not in self._extra_image_paths:
                self._extra_image_paths.append(p)
                added += 1
        if added:
            self._refresh_extra_image_list()
            tooltip(f"Added {added} image{'s' if added != 1 else ''} to Extra.")

    def _on_clear_extra_images(self):
        self._extra_image_paths.clear()
        self._refresh_extra_image_list()

    def _refresh_extra_image_list(self):
        self.extra_img_list.clear()
        for p in self._extra_image_paths:
            self.extra_img_list.addItem(QListWidgetItem("🖼 " + os.path.basename(p)))
        has = bool(self._extra_image_paths)
        self.extra_img_list.setVisible(has)
        self.extra_img_clear_btn.setVisible(has)

    # ── LO-gap queue integration ─────────────────────────────────────────────
    #
    # Called by MainWindow whenever this panel is shown. Pre-fills the form
    # with the next queued gap; leaves the form alone if the queue is empty.

    def refresh_setup_banner(self) -> None:
        try:
            ok = provider_configured()
            self._setup_banner.setVisible(not ok)
            set_ai_buttons_enabled([getattr(self, "go_btn", None)], ok)
            # Paste cards is only for the no-subscription "BYO AI — paste from
            # any chatbot" provider; hide it for every other provider, which
            # generates cards in-app and has no reply to paste back.
            paste_btn = getattr(self, "paste_btn", None)
            if paste_btn is not None:
                paste_btn.setVisible(is_manual_provider())
        except Exception:
            pass

    # ── background jobs UI ───────────────────────────────────────────────────

    @staticmethod
    def _job_label(it: dict) -> str:
        st = it.get("status")
        title = (it.get("title") or it.get("topic_label")
                 or it.get("source_label") or "(generation)")
        title = title if len(title) <= 48 else title[:47] + "…"
        n = len(it.get("cards") or [])
        noun = "card" if n == 1 else "cards"
        return {
            "running":     f"⏳ {title} — generating…",
            "queued":      f"…  {title} — queued",
            "ready":       f"✅ {title} — {n} {noun} ready",
            "error":       f"⚠️ {title} — failed (Retry to re-run)",
            "interrupted": f"⛔ {title} — interrupted (Retry to re-run)",
        }.get(st, title)

    def _update_go_btn_label(self, n_active: int | None = None) -> None:
        if n_active is None:
            try:
                from . import create_jobs
                n_active = create_jobs.active_count()
            except Exception:
                n_active = 0
        self.go_btn.setText(f"Generate ({n_active} running…)" if n_active else "Generate")

    def refresh_ready_lists(self) -> None:
        """Repopulate the in-progress and ready-to-review lists from the job
        store. Cheap, fail-open; called on show and on job completion."""
        from . import create_jobs
        try:
            items = create_jobs.load_all()
        except Exception:
            items = []
        active = [it for it in items if it.get("status") in ("queued", "running")]
        done = [it for it in items if it.get("status") in ("ready", "error", "interrupted")]

        self.inprogress_list.clear()
        for it in active:
            row = QListWidgetItem(self._job_label(it))
            row.setData(Qt.ItemDataRole.UserRole, it.get("id"))
            self.inprogress_list.addItem(row)
        self.inprogress_header.setText(f"In progress ({len(active)})")
        for w in (self.inprogress_header, self.inprogress_list, self.cancel_job_btn):
            w.setVisible(bool(active))

        self.ready_list.clear()
        for it in done:
            row = QListWidgetItem(self._job_label(it))
            row.setData(Qt.ItemDataRole.UserRole, it.get("id"))
            self.ready_list.addItem(row)
        n_ready = sum(1 for it in done if it.get("status") == "ready")
        self.ready_header.setText(f"Ready to review ({n_ready})")
        for w in (self.ready_header, self.ready_list, self.ready_hint,
                  self.open_job_btn, self.retry_job_btn, self.discard_job_btn):
            w.setVisible(bool(done))

        self.jobs_box.setVisible(bool(active or done))
        bg_on = bool(self.cfg.get("background_generation", True))
        self.bg_hint.setVisible(bg_on and not (active or done))
        self._update_go_btn_label(len(active))

    def _selected_job_id(self, listw) -> str | None:
        it = listw.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _on_open_job(self) -> None:
        jid = self._selected_job_id(self.ready_list)
        if not jid:
            tooltip("Select a generation to open.")
            return
        self.open_ready_job(jid)

    def _on_retry_job(self) -> None:
        jid = self._selected_job_id(self.ready_list)
        if not jid:
            tooltip("Select a failed/interrupted generation to retry.")
            return
        from . import create_jobs
        create_jobs.retry_job(jid)
        self.refresh_ready_lists()
        self._refresh_badge()

    def _on_discard_job(self) -> None:
        jid = self._selected_job_id(self.ready_list)
        if not jid:
            tooltip("Select a generation to discard.")
            return
        if not askUser("Discard this generation and its cards?"):
            return
        from . import create_jobs
        create_jobs.remove(jid)
        self.refresh_ready_lists()
        self._refresh_badge()

    def _on_cancel_job(self) -> None:
        jid = self._selected_job_id(self.inprogress_list)
        if not jid:
            tooltip("Select a running generation to cancel.")
            return
        from . import create_jobs
        create_jobs.cancel(jid)
        self.refresh_ready_lists()
        self._refresh_badge()

    def _refresh_badge(self) -> None:
        if self._main_window is not None and hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()

    def refresh_queue_state(self, main_window) -> None:
        self._main_window = main_window
        self._rebuild_queue_view()
        self.refresh_ready_lists()
        queue = getattr(main_window, "gap_queue", []) or []
        if not queue:
            self._current_gap = None
            self._update_autotag_hint(None)
            return

        gap = queue[0]
        if gap == self._current_gap:
            return  # already pre-filled with this gap

        # Only pre-fill if the user hasn't typed their own content. The
        # explicit "Load next →" button bypasses this guard so the queue
        # never gets silently stuck.
        current_topic = self.topic.text().strip()
        prev_title = self._gap_title(self._current_gap)
        can_overwrite = (not current_topic) or (current_topic == prev_title)
        if not can_overwrite:
            self._current_gap = None
            return
        self._prefill_form_from_gap(gap)

    def _prefill_form_from_gap(self, gap) -> None:
        """Unconditionally write the gap's title / focus / card-count into
        the form. Sets `_current_gap` so the post-Accept pop-on-success
        path knows to dequeue."""
        self._current_gap = gap
        gap_text = self._gap_title(gap)
        try:
            self.rb_topic.setChecked(True)
            self._sync_mode()
            self.topic.setText(gap_text)
            # Focus is left for the user to fill in. We only pre-seed it with
            # supplemental source context (stem / notes) so Claude still sees
            # the underlying material — the topic itself already lives in the
            # Topic field, so we don't duplicate it here.
            focus_bits = []
            if isinstance(gap, dict):
                # Strip HTML from any captured stem so we can surface the
                # underlying question text as a focus hint. Image filenames
                # ride along on the gap dict (`images`) and get rendered
                # into the MQ field at create time via _kg_content_html.
                stem = gap.get("stem_html") or ""
                notes = gap.get("notes") or ""
                if stem:
                    import re as _re
                    plain = _re.sub(r"<[^>]+>", " ", stem)
                    plain = _re.sub(r"\s+", " ", plain).strip()
                    if plain:
                        focus_bits.append("Context from source question: " + plain[:400])
                if notes:
                    focus_bits.append("Notes: " + notes[:300])
            self.focus.setText(" — ".join(focus_bits))
            # Carry the KG's own tags into the tag field (on top of the user's
            # standing default tags) so curated tags aren't lost. The auto-tag,
            # if any, is appended later at create time — it doesn't replace these.
            default_tags = list(self.cfg.get("default_tags", []))
            gap_tags = list(gap.get("tags") or []) if isinstance(gap, dict) else []
            merged_tags: list[str] = []
            for t in default_tags + gap_tags:
                t = (t or "").strip()
                if t and t not in merged_tags:
                    merged_tags.append(t)
            self.tags.setText(", ".join(merged_tags))
            self.n_cards.setValue(int(self.cfg.get("gap_n_cards", 3)))
            self._update_autotag_hint(gap)
        except Exception as e:
            print(f"[ankisstant] gap pre-fill failed: {e}")

    def _update_autotag_hint(self, gap) -> None:
        """Reflect what the Auto-tag checkbox will produce: the type segment is
        the loaded gap's KG type, else the default type for free generation."""
        try:
            base = auto_tag_base()
            active = bool(autotag.is_enabled(self.cfg) and base)
            if active:
                kg_type = ""
                if isinstance(gap, dict):
                    kg_type = (gap.get("kg_type") or gap.get("type") or "").lower()
                type_seg, _ = kg_type_info(kg_type or autotag.default_type_key())
                self._autotag_hint.setText(
                    f"<small>🏷️ Auto-tag on — a tag under "
                    f"<code>{base}::{type_seg}</code> is added to every card.</small>"
                )
            self._autotag_hint.setVisible(active)
        except Exception as e:
            print(f"[ankisstant] autotag hint update failed: {e}")
            self._autotag_hint.setVisible(False)

    def _form_is_dirty(self) -> bool:
        """True if the user has typed something that the next pre-fill
        would clobber."""
        return bool(self.topic.text().strip() or self.focus.text().strip())

    def _on_load_top_gap(self) -> None:
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None) or []
        if not queue:
            tooltip("Queue is empty.")
            return
        gap = queue[0]
        # Only ask before clobbering content that doesn't match what would
        # already be there from a prior pre-fill of the same gap.
        if (self._form_is_dirty()
                and self.topic.text().strip() != self._gap_title(gap)
                and not askUser(
                    "Overwrite the topic / focus fields with the next queued KG?",
                    defaultno=True,
                )):
            return
        self._prefill_form_from_gap(gap)
        tooltip(f"Loaded: {self._gap_title(gap)[:60]}")

    def _rebuild_queue_view(self) -> None:
        """Rebuild the queue list widget from the main-window queue. Does NOT
        touch the form fields. Queue panel stays visible even when empty so
        the user can manually add gaps."""
        queue = (
            getattr(self._main_window, "gap_queue", []) if self._main_window else []
        ) or []
        self.queue_list.clear()
        self.queue_box.setVisible(True)
        if not queue:
            self.queue_header.setText(
                "Queue is empty — add a gap below, or send one from the Knowledge Gaps tab."
            )
            self.queue_list.setVisible(False)
            self.queue_remove_btn.setVisible(False)
            self.queue_load_btn.setVisible(False)
            self.queue_skip_btn.setVisible(False)
            self.queue_clear_btn.setVisible(False)
            return
        for i, gap in enumerate(queue):
            prefix = "▶ " if i == 0 else "    "
            item = QListWidgetItem(prefix + self._gap_title(gap))
            if i == 0:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.queue_list.addItem(item)
        n = len(queue)
        self.queue_header.setText(
            f"{n} gap{'s' if n != 1 else ''} queued — top item drives the form below"
        )
        self.queue_list.setVisible(True)
        self.queue_remove_btn.setVisible(True)
        self.queue_load_btn.setVisible(True)
        self.queue_skip_btn.setVisible(True)
        self.queue_clear_btn.setVisible(True)

    def _on_add_gap_manually(self):
        text = self.queue_add_input.text().strip()
        if not text:
            return
        if self._main_window is None:
            showWarning(
                "Open this panel via Tools → Ankisstant so the queue is available."
            )
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if queue is None:
            return
        queue.append({"title": text, "kg_id": None, "stem_html": None, "notes": None})
        self.queue_add_input.clear()
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Queued: {text[:60]}")

    def _on_remove_selected_gap(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        row = self.queue_list.currentRow()
        if row < 0 or row >= len(queue):
            tooltip("Select a gap in the list first.")
            return
        if row == 0:
            self._current_gap = None  # active gap is being removed
        removed = queue.pop(row)
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Removed: {self._gap_title(removed)[:60]}")

    def _on_skip_gap(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        skipped = queue.pop(0)
        self._current_gap = None
        # Skipping is an explicit "advance to the next gap" — clear the form so
        # the new top gap pre-fills cleanly (and its Focus replaces the old one).
        self.topic.setText("")
        self.focus.setText("")
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)
        tooltip(f"Skipped: {self._gap_title(skipped)[:60]}")

    def _on_clear_queue(self):
        if self._main_window is None:
            return
        queue = getattr(self._main_window, "gap_queue", None)
        if not queue:
            return
        if not askUser(f"Discard all {len(queue)} queued gap(s)?"):
            return
        queue.clear()
        self._current_gap = None
        if hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()
        self.refresh_queue_state(self._main_window)

    # ── file attachment handlers ─────────────────────────────────────────────

    def _on_attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach PDFs / PowerPoints",
            "", "Documents (*.pdf *.pptx);;PDF (*.pdf);;PowerPoint (*.pptx);;All files (*)",
        )
        if not paths:
            return
        added = 0
        for p in paths:
            if p in self._attached_paths:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext not in (".pdf", ".pptx"):
                tooltip(f"Skipped {os.path.basename(p)} — only .pdf / .pptx supported.")
                continue
            try:
                size = os.path.getsize(p)
            except OSError as e:
                showWarning(f"Couldn't read {os.path.basename(p)}: {e}")
                continue
            mb = size / (1024 * 1024)
            if ext == ".pdf":
                if mb > 30:
                    showWarning(
                        f"{os.path.basename(p)} is {mb:.1f} MB — too large for Claude to read "
                        "in a single request (>30 MB). Split the PDF or extract relevant pages."
                    )
                    continue
                if mb > 10:
                    ok = askUser(
                        f"{os.path.basename(p)} is {mb:.1f} MB. Large PDFs can be slow "
                        "and may exhaust the model's context. Attach anyway?"
                    )
                    if not ok:
                        continue
            self._attached_paths.append(p)
            added += 1
        if added:
            self._refresh_attach_list()

    def _on_attach_clear(self):
        self._attached_paths.clear()
        self._refresh_attach_list()

    def _refresh_attach_list(self):
        self.attach_list.clear()
        for p in self._attached_paths:
            self.attach_list.addItem(QListWidgetItem(os.path.basename(p)))
        has = bool(self._attached_paths)
        self.attach_list.setVisible(has)
        self.attach_clear_btn.setVisible(has)

    def _on_generate(self):
        if not anki_utils.require_col():
            return
        # Warm the existing-tag vocabulary on the UI thread so the (possibly
        # background) generation + auto-tag classify reuse the user's own tag
        # branches instead of inventing synonyms.
        autotag.refresh_vocab_cache()
        mode = "source" if self.rb_source.isChecked() else "topic"
        n = self.n_cards.value()
        deck_name = self.deck.currentText().strip()
        focus = self.focus.text().strip()
        tags_raw = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        audit_tag = (self.cfg.get("audit_tag") or "").strip()
        if audit_tag and audit_tag not in tags_raw:
            tags_raw.append(audit_tag)
        # Month tag for temporality (global toggle in Settings → Global).
        mtag = month_tag()
        if mtag and mtag not in tags_raw:
            tags_raw.append(mtag)

        notetype_name = self._current_notetype_name()
        if not notetype_name or notetype_name.startswith("("):
            showWarning(
                "No notetype selected.\n\n"
                "Open Ankisstant Settings → AI Create and add at least "
                "one notetype profile, then pick it in the dropdown."
            )
            return
        profile = _resolved_profile(self.cfg, notetype_name)

        # Card format drives both the request wording and the base system prompt:
        # cloze → CARD_GEN_SYSTEM; Q&A (e.g. the bundled Malleus Q&A notetype) →
        # QA_GEN_SYSTEM (front=question, extra=answer, no cloze).
        is_qa = str(profile.get("card_format", "cloze")).lower() == "qa"
        card_word = "question/answer" if is_qa else "high-yield cloze"

        attachments_for_api: list[str] = []  # what's sent to the model (PDF or page-images)
        attachment_labels: list[str] = []    # for the source label
        pdf_paths: list[str] = []            # original PDF(s), for slide rasterisation
        self._slide_pages: list[str] = []    # rendered page images of a single slide deck

        if mode == "source":
            url = self.url.text().strip()
            text_body = self.text.toPlainText().strip()
            if url:
                fetched = None
                fetch_err: Exception | None = None
                with loading(self.go_btn, "Fetching URL…"):
                    try:
                        fetched = _fetch_url(url)
                    except (urllib.error.URLError, urllib.error.HTTPError) as e:
                        fetch_err = e
                if fetch_err is not None:
                    code = getattr(fetch_err, "code", None)
                    if code in (401, 403):
                        showWarning(
                            f"That site blocked the request (HTTP {code}).\n\n"
                            "Some sites (paywalled or bot-protected, e.g. parts of "
                            "austroads.gov.au) won't let the add-on fetch the page "
                            "directly. Open the page in your browser, copy the "
                            "relevant text, and paste it into the box below instead."
                        )
                    else:
                        showWarning(f"Could not fetch URL: {fetch_err}")
                    return
                text_body = (text_body + "\n\n" + fetched).strip() if text_body else fetched

            # PPTX → in-process text extract (works on any provider).
            # PDF  → rasterised to page-images; the original goes to Claude (CLI
            #        Read / API doc) while vision providers get the page-images,
            #        and the pages double as per-card slide attachments.
            if self._attached_paths:
                pptx_blocks: list[str] = []
                with loading(self.go_btn, "Reading attachments…"):
                    for p in self._attached_paths:
                        ext = os.path.splitext(p)[1].lower()
                        name = os.path.basename(p)
                        if ext == ".pptx":
                            try:
                                t = _extract_pptx_text(p)
                            except Exception as e:
                                showWarning(f"Couldn't read {name}: {e}")
                                return
                            if t:
                                pptx_blocks.append(
                                    f"\n---FILE: {name}---\n{t}\n---END FILE---"
                                )
                            else:
                                showWarning(
                                    f"{name} contains no readable text.\n\n"
                                    "PowerPoint extraction only sees text on slides and "
                                    "in speaker notes — image-only slides won't work. "
                                    "Try a PDF export instead, or paste the relevant text directly."
                                )
                                return
                            attachment_labels.append(name)
                        elif ext == ".pdf":
                            pdf_paths.append(p)
                            attachment_labels.append(name)
                if pptx_blocks:
                    text_body = (text_body + "\n\n" + "\n".join(pptx_blocks)).strip() \
                        if text_body else "\n".join(pptx_blocks)

            # Resolve the PDF(s): render to page-images and pick what to send the
            # model based on the provider family. Slide auto-attach + the gallery
            # need exactly one deck, so they only engage for a single PDF; with
            # several PDFs we still generate (page-images on vision providers),
            # just without slide numbering.
            family = active_family()
            if pdf_paths:
                with loading(self.go_btn, "Rendering slides…"):
                    rendered = {p: pdf_render.render_pdf_to_images(p) for p in pdf_paths}
                if family == "anthropic":
                    attachments_for_api.extend(pdf_paths)  # Claude reads the PDF directly
                else:
                    pages_flat = [pg for p in pdf_paths for pg in rendered.get(p, [])]
                    if not pages_flat:
                        showWarning(
                            "Couldn't render that PDF to images for this AI provider.\n\n"
                            "Switch to Claude (which reads PDFs directly), or paste the "
                            "text instead."
                        )
                        return
                    if len(pages_flat) > 25 and not askUser(
                        f"This deck has {len(pages_flat)} slides. They'll all be sent to "
                        "your AI as images, which can be slow and use a lot of tokens.\n\n"
                        "Generate anyway?"
                    ):
                        return
                    attachments_for_api.extend(pages_flat)
                if len(pdf_paths) == 1:
                    self._slide_pages = list(rendered.get(pdf_paths[0], []))

            if not text_body and not attachments_for_api:
                showWarning("Provide pasted text, a URL, or attach a PDF / PPTX.")
                return

            # Source label: URL wins, else listed attachments, else (pasted).
            if url:
                source_label = url
            elif attachment_labels:
                source_label = ", ".join(attachment_labels)
            else:
                source_label = "(pasted text)"

            parts = [f"Generate {n} {card_word} cards from the SOURCE below."]
            parts.append(_breadth_directive(n, "source"))
            if focus:
                parts.append(_focus_directive(focus))
            if attachments_for_api:
                parts.append(
                    "Attachment(s) provided — use them as the primary source."
                    if self._slide_pages else
                    "PDF attachment(s) provided — use them as the primary source."
                )
            if text_body:
                parts.append(f"\n---SOURCE---\n{text_body}\n---END SOURCE---")
            user_msg = "\n".join(parts)
            topic_label = None
        else:
            topic_label = self.topic.text().strip()
            if not topic_label:
                showWarning("Type a topic.")
                return
            source_label = None
            user_msg = (
                f"Generate {n} {card_word} cards on the TOPIC: {topic_label}\n"
                + _topic_directive(topic_label, n) + "\n"
                + _breadth_directive(n, "topic") + "\n"
                + (_focus_directive(focus) + "\n" if focus else "")
            )

        model = tool_model_for("card_creation")
        loading_label = (
            "Asking AI (reading attachments)…" if attachments_for_api
            else "Asking AI…"
        )
        skill_invocation, skill_id = _resolve_skill(profile)
        base_system = QA_GEN_SYSTEM if is_qa else CARD_GEN_SYSTEM
        system_prompt = _augment_system(base_system, profile)
        # Keep generation in sync with the quality pass's atomicity sensitivity
        # (cloze only): tell the generator how many coupled clozes are acceptable
        # so it isn't penalised by the scorer for cards it was told to write.
        if not is_qa:
            system_prompt = system_prompt + _gen_atomicity_note(_atomicity_max(self.cfg))

        # Source grounding (topic mode only): append the Australian/WA guideline
        # citation allow-list so the model cites real URLs. Gated by the per-batch
        # checkbox (which itself reflects the global + per-notetype resolution).
        # Fail-open — a grounding failure must never block card creation.
        try:
            want_ground = (
                _grounding_active(self.cfg, profile, mode)
                and getattr(self, "cb_ground", None) is not None
                and self.cb_ground.isChecked()
            )
            if want_ground and topic_label:
                block = grounding.build_guidelines_block(
                    topic_label, tags_raw,
                    fetch_content=_grounding_fetch_live(self.cfg),
                )
                if block:
                    system_prompt = system_prompt + "\n\n" + block
        except Exception as e:
            log.error(f"grounding injection skipped: {e}")

        # Auto-image: when the per-batch toggle is on, ask the model to flag
        # visual cards with an `image_query`. The review screen auto-fetches
        # candidate thumbnails for exactly those cards. Fail-open.
        try:
            if getattr(self, "cb_autoimg", None) is not None and self.cb_autoimg.isChecked():
                system_prompt = system_prompt + IMAGE_QUERY_INSTRUCTION
        except Exception as e:
            log.error(f"image-query instruction skipped: {e}")

        # Slide deck: ask the model to tag each card with its source slide so the
        # review screen can auto-attach that slide's image. Only when a single
        # PDF rendered to pages. Fail-open.
        try:
            if self._slide_pages:
                system_prompt = system_prompt + _slide_index_instruction(len(self._slide_pages))
        except Exception as e:
            log.error(f"slide-index instruction skipped: {e}")

        # The MQ knowledge-gap explanation is normally a separate AI call, but
        # with no skill in play we fold it into the SAME request (one object
        # reply) — critical in manual/BYO mode, where each call is its own
        # copy/paste dialog. Skill flows keep the separate call so the skill's
        # tuned array output isn't disturbed.
        gap = self._current_gap if isinstance(self._current_gap, dict) else None
        no_skill = not skill_id and not skill_invocation
        # Auto-tag (the shared scheme used by AI Browse too) is controlled by the
        # "Auto-tag" checkbox next to the tag box. Ticked → add a hierarchical
        # base::Type::System::Subsystem::Topic tag to every card. A loaded gap
        # uses that gap's KG type as the segment; free topic/source generation
        # uses the default KG type. When no skill forces a bare reply we fold the
        # tag into the card-gen call (cheap); with a skill we fall back to a
        # separate classification call (tools/autotag.py), so skilled notetypes
        # still get tagged. `tag_material` feeds that separate call.
        want_auto = autotag.is_enabled(self.cfg) and bool(auto_tag_base())
        if gap is not None:
            type_meta = _kg_type_meta((gap.get("kg_type") or "").lower())
        elif want_auto:
            type_meta = autotag.type_meta_for(autotag.default_type_key())
        else:
            type_meta = None
        want_tag = bool(want_auto and gap is not None)
        topic_autotag = bool(want_auto and gap is None)
        # A gap's previously-cached tag is reused only while auto-tag is on, so
        # unticking the box means no tag at all (not even a stale cached one).
        cached_tag = (gap.get("auto_tag") or "").strip() if (gap and want_auto) else ""
        tag_material = _tag_material(gap, topic_label, source_label, focus)
        merge_tag = ((want_tag and not cached_tag) or topic_autotag) and no_skill
        want_explanation = _wants_mq_explanation(gap)
        merge_explanation = want_explanation and no_skill
        # same_call quality pass: fold the self-grade into this one reply. Only
        # possible with no skill (skills force a bare array). When a skill is
        # active, or grades don't come back, _finalize_review falls back to a
        # separate scoring call.
        qpc = self.cfg.get("quality_pass") or {}
        merge_grade = (
            _quality_pass_active(self.cfg, profile)
            and qpc.get("grading_mode") == "same_call"
            and no_skill
        )
        # Build the one-call request from the KG type's declarative field specs
        # (tools/kg/engine.py): the AI returns the tag + every AI-source field
        # (e.g. the MQ explanation) + cards in a SINGLE JSON object. Skill flows
        # force a bare card array, so the object form — and its per-field routing
        # — is used only when no_skill. `exclude` drops a field already satisfied
        # (a cached explanation) so it isn't re-requested. The plan is stashed for
        # reply routing and (for background jobs) serialised into the record.
        self._gen_plan = None
        if no_skill:
            exclude = set() if want_explanation else {"explanation"}
            plan, req_fields = engine.plan_for(
                type_meta, "create", want_cards=True, want_terms=False,
                want_tag=merge_tag, want_grade=merge_grade, exclude=exclude)
            grade_instr = qp.merged_grade_instructions(_atomicity_max(self.cfg)) if merge_grade else ""
            obj = engine.build_object_instructions(plan, req_fields, grade_instructions=grade_instr)
            if obj:
                system_prompt += obj
            self._gen_plan = plan
            if gap is not None and req_fields:
                refs = engine.build_refs_context(req_fields, gap)
                if refs:
                    user_msg += "\n\nCONTEXT for the AI-written fields:\n" + refs
        if merge_tag:
            ctx = []
            if gap is not None:
                if (gap.get("title") or "").strip():
                    ctx.append(f"Title: {gap['title'].strip()}")
                stem = re.sub(r"<[^>]+>", " ", str(gap.get("stem_html") or ""))
                stem = re.sub(r"\s+", " ", stem).strip()
                if stem:
                    ctx.append(f"Question/stem: {stem[:600]}")
                if (gap.get("notes") or "").strip():
                    ctx.append(f"Notes: {gap['notes'].strip()[:300]}")
            else:
                # No gap: classify the topic/source material above.
                if topic_label:
                    ctx.append(f"Topic: {topic_label}")
                if focus:
                    ctx.append(f"Focus: {focus}")
                if not ctx and source_label:
                    ctx.append(f"Source: {source_label}")
            if ctx:
                user_msg += "\n\nCLASSIFY THIS MATERIAL for the tag:\n" + "\n".join(ctx)

        # Background generation: enqueue and return immediately so the user keeps
        # using Anki. Manual/paste can't background (its dialog needs the main
        # thread) — it falls through to the interactive path below.
        bg_on = bool(self.cfg.get("background_generation", True))
        if bg_on and not is_manual_provider():
            from . import create_jobs
            dup = create_jobs.find_active_duplicate(
                mode=mode, topic_label=topic_label, source_label=source_label,
                notetype_name=notetype_name, focus=focus)
            if dup is not None and not askUser(
                    "A generation for this is already running.\n\nStart another anyway?"):
                return
            record = self._make_job_record(
                mode=mode, source_label=source_label, topic_label=topic_label,
                focus=focus, deck_name=deck_name, notetype_name=notetype_name,
                tags_raw=tags_raw, gap=gap, type_meta=type_meta, cached_tag=cached_tag,
                want_tag=want_tag, want_explanation=want_explanation, merge_tag=merge_tag,
                merge_explanation=merge_explanation, merge_grade=merge_grade,
                user_msg=user_msg, system_prompt=system_prompt, model=model,
                skill_id=skill_id, skill_invocation=skill_invocation,
                attachments=attachments_for_api, slide_pages=self._slide_pages,
                want_auto=want_auto, tag_material=tag_material)
            self.cfg["default_n_cards"] = n
            self.cfg["selected_notetype"] = notetype_name
            save_tool_config("card_creator", self.cfg)
            create_jobs.enqueue(record)
            tooltip(f"Generating “{record['title']}” in the background…", period=3000)
            self.refresh_ready_lists()
            self._refresh_badge()
            return

        reply = run_claude_json(
            self.go_btn, loading_label,
            prompt=user_msg, system=system_prompt, max_tokens=4096, model=model,
            skill_id=skill_id, skill_invocation=skill_invocation,
            attachments=attachments_for_api or None,
        )

        # Route the one-call reply via the engine: an object {tags, <ai fields>,
        # grades, cards} when a plan was built, else a bare card array (skill
        # flow). Per-note AI field values (the explanation, plus any custom AI
        # fields) are persisted to the KG store; the explanation also leads the
        # Missed Questions field.
        if self._gen_plan is not None:
            routed = engine.route(reply, self._gen_plan)
            cards = routed.cards
            merged_tag_levels = routed.tag_levels
            same_call_grades = routed.grades
            mq_explanation = str(routed.field_values.get("explanation") or "")
            if gap is not None:
                _persist_ai_field_values(gap, routed.field_values)
        else:
            cards, merged_tag_levels, same_call_grades, mq_explanation = reply, None, None, ""

        if reply is None:
            return  # cancelled, or a parse failure already surfaced

        # Skill flows can't fold the explanation in (array output), so fall back
        # to a separate call there. Only the AI Generate flow does this — the
        # BYO Paste flow never calls out.
        if want_explanation and not merge_explanation and not (gap and gap.get("explanation")):
            try:
                with loading(self.go_btn, "Explaining the knowledge gap…"):
                    _generate_mq_explanation_for_gap(gap, model=model)
            except Exception as e:
                print(f"[ankisstant] mq explanation fallback skipped: {e}")

        self.cfg["default_n_cards"] = n

        # Manual provider with background on: its paste happened just now (on the
        # main thread); instead of opening review immediately, stash the cards in
        # the ready queue for later. (Non-manual providers returned earlier.)
        if bg_on and resolve_card_validity(cards):
            from . import create_jobs
            from dataclasses import asdict
            verdicts = grade_cards_sync(cards, profile, self.cfg,
                                        same_call_grades=same_call_grades)
            record = self._make_job_record(
                mode=mode, source_label=source_label, topic_label=topic_label,
                focus=focus, deck_name=deck_name, notetype_name=notetype_name,
                tags_raw=tags_raw, gap=gap, type_meta=type_meta, cached_tag=cached_tag,
                want_tag=want_tag, want_explanation=want_explanation, merge_tag=merge_tag,
                merge_explanation=merge_explanation, merge_grade=merge_grade,
                user_msg=user_msg, system_prompt=system_prompt, model=model,
                skill_id=skill_id, skill_invocation=skill_invocation,
                attachments=attachments_for_api, slide_pages=self._slide_pages,
                want_auto=want_auto, tag_material=tag_material)
            job = create_jobs.add(**record, status="running")
            # No folded levels (skill array) but auto-tag wanted: classify now so
            # this path tags too. Skipped for the manual provider — a classify
            # call there would pop another paste dialog.
            if want_auto and not merged_tag_levels and not is_manual_provider():
                merged_tag_levels = autotag.classify(tag_material, model=model)
            result = {
                "cards": cards, "merged_tag_levels": merged_tag_levels,
                "mq_explanation": mq_explanation,
                "verdicts": ([asdict(v) if v else None for v in verdicts]
                             if verdicts else None),
            }
            create_jobs._apply_result_main_thread(job["id"], result)
            self.cfg["selected_notetype"] = notetype_name
            save_tool_config("card_creator", self.cfg)
            tooltip("Cards saved to your review queue (open AI Create).", period=3000)
            self.refresh_ready_lists()
            self._refresh_badge()
            return

        self._finalize_review(
            cards=cards, merged_tag_levels=merged_tag_levels, mode=mode,
            source_label=source_label, topic_label=topic_label, focus=focus,
            deck_name=deck_name, notetype_name=notetype_name, tags_raw=tags_raw,
            profile=profile, gap=gap, type_meta=type_meta,
            cached_tag=cached_tag, want_tag=want_tag, model=model,
            same_call_grades=same_call_grades,
            topic_autotag=topic_autotag, tag_material=tag_material,
            slide_pages=self._slide_pages,
        )

    def _on_paste_cards(self):
        """No-AI 'paste straight in' path: the user generated cards (and tags)
        in their own ChatGPT / Claude using the standalone Ankisstant prompt,
        and pastes the JSON here. No prompt is built and no model is called —
        we parse, apply tags, and jump straight to the review dialog."""
        if not anki_utils.require_col():
            return
        deck_name = self.deck.currentText().strip()
        focus = self.focus.text().strip()
        tags_raw = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        audit_tag = (self.cfg.get("audit_tag") or "").strip()
        if audit_tag and audit_tag not in tags_raw:
            tags_raw.append(audit_tag)
        mtag = month_tag()
        if mtag and mtag not in tags_raw:
            tags_raw.append(mtag)

        notetype_name = self._current_notetype_name()
        if not notetype_name or notetype_name.startswith("("):
            showWarning(
                "No notetype selected.\n\n"
                "Open Ankisstant Settings → AI Create and add at least "
                "one notetype profile, then pick it in the dropdown."
            )
            return
        profile = _resolved_profile(self.cfg, notetype_name)

        parsed = _prompt_for_pasted_cards(self)
        if parsed is None:
            return  # cancelled or unparseable (a message was already shown)

        # Accept a bare [...] array, a {"cards": [...]} / {"tags":…, "cards":…}
        # object, or even a single {"front","extra"} object. An "mq_explanation"
        # key (present when the user ran our merged prompt) is honoured too.
        merged_tag_levels = None
        mq_explanation = ""
        same_call_grades = None
        cards = parsed
        if isinstance(parsed, dict):
            if isinstance(parsed.get("tags"), dict):
                merged_tag_levels = parsed["tags"]
            mq_explanation = str(parsed.get("mq_explanation") or "").strip()
            # Honour grades if the user ran our merged same_call prompt externally.
            if isinstance(parsed.get("grades"), list):
                same_call_grades = parsed["grades"]
            cards = parsed.get("cards")
            if cards is None and "front" in parsed:
                cards = [parsed]

        gap = self._current_gap if isinstance(self._current_gap, dict) else None
        type_meta = _kg_type_meta((gap.get("kg_type") or "").lower()) if gap else None
        # Respect the Auto-tag checkbox here too (consistent with Generate).
        cached_tag = (gap.get("auto_tag") or "").strip() if (gap and autotag.is_enabled(self.cfg)) else ""
        # Persist any pasted explanation onto the gap. No model is ever called
        # here — BYO users supply it via the prompt they ran externally.
        if mq_explanation and _wants_mq_explanation(gap):
            _persist_mq_explanation(gap, mq_explanation)

        # Background on: funnel pasted cards into the ready queue too, so every
        # result is reviewed from one place. (There's no AI wait here, but it
        # keeps the workflow consistent.)
        bg_on = bool(self.cfg.get("background_generation", True))
        if bg_on and resolve_card_validity(cards):
            from . import create_jobs
            from dataclasses import asdict
            verdicts = grade_cards_sync(cards, profile, self.cfg,
                                        same_call_grades=same_call_grades)
            record = self._make_job_record(
                mode="source", source_label="(pasted from your AI)", topic_label=None,
                focus=focus, deck_name=deck_name, notetype_name=notetype_name,
                tags_raw=tags_raw, gap=gap, type_meta=type_meta, cached_tag=cached_tag,
                want_tag=False, want_explanation=False, merge_tag=False,
                merge_explanation=False, merge_grade=False,
                user_msg="", system_prompt="", model=None, skill_id="",
                skill_invocation="", attachments=None)
            job = create_jobs.add(**record, status="running")
            result = {
                "cards": cards, "merged_tag_levels": merged_tag_levels,
                "mq_explanation": mq_explanation,
                "verdicts": ([asdict(v) if v else None for v in verdicts]
                             if verdicts else None),
            }
            create_jobs._apply_result_main_thread(job["id"], result)
            tooltip("Cards saved to your review queue (open AI Create).", period=3000)
            self.refresh_ready_lists()
            self._refresh_badge()
            return

        # want_tag=False: in paste mode we never fall back to an AI auto-tag
        # call (that would defeat the point). Pasted tag levels still apply.
        self._finalize_review(
            cards=cards, merged_tag_levels=merged_tag_levels, mode="source",
            source_label="(pasted from your AI)", topic_label=None, focus=focus,
            deck_name=deck_name, notetype_name=notetype_name, tags_raw=tags_raw,
            profile=profile, gap=gap, type_meta=type_meta,
            cached_tag=cached_tag, want_tag=False, model=None,
            same_call_grades=same_call_grades,
        )

    def _grader_system(self, use_skill: bool) -> str:
        max_clozes = _atomicity_max(self.cfg)
        return (qp.skill_batch_system(max_clozes) if use_skill
                else qp.rubric_batch_system(max_clozes))

    def _grade_one_sync(self, card: dict, profile: dict | None) -> "qp.Verdict":
        """Grade a single card synchronously (used by the auto-regen loop, which
        already runs inside a `loading()` busy context). Direct ask_claude_json
        keeps it on the main thread; manual provider pops a paste dialog."""
        skill_inv, skill_id = _grader_skill(self.cfg, profile)
        raw = core_api.ask_claude_json(
            prompt=qp.batch_prompt([card]),
            system=self._grader_system(bool(skill_inv or skill_id)),
            max_tokens=512, model=_grader_model(self.cfg),
            skill_id=skill_id, skill_invocation=skill_inv, show_errors=False,
        )
        verdicts = qp.parse_batch_verdicts(raw, [card]) if raw is not None else None
        return qp.apply_prefilters_then(verdicts, [card], _atomicity_max(self.cfg))[0]

    def _auto_regenerate_failures(self, cards: list, profile: dict | None,
                                  qpc: dict) -> None:
        """For each FAIL card, regenerate up to N times (re-grading each attempt,
        feeding the worst-dimension reason back as the hint) and keep the best
        attempt. Suppressed for the manual/paste provider unless explicitly
        allowed (each retry is another round-trip)."""
        if qpc.get("verdict_action") != "auto_regenerate":
            return
        if is_manual_provider() and not qpc.get("auto_regen_in_manual"):
            return
        max_retries = int(qpc.get("auto_regen_max_retries", 2) or 0)
        if max_retries <= 0:
            return
        targets = [i for i, c in enumerate(cards)
                   if isinstance(c.get("_verdict"), qp.Verdict)
                   and c["_verdict"].verdict == qp.FAIL]
        if not targets:
            return
        model = tool_model_for("card_creation")
        focus = self.focus.text().strip() if hasattr(self, "focus") else ""
        rank = {qp.PASS: 2, qp.FLAG: 1, qp.FAIL: 0}
        with loading(self.go_btn, "Improving low-scoring cards…"):
            for i in targets:
                cur, cur_vd = cards[i], cards[i].get("_verdict")
                best, best_vd = cur, cur_vd
                for _ in range(max_retries):
                    hint = cur_vd.reason if isinstance(cur_vd, qp.Verdict) else ""
                    regen = _regenerate_one(cur, hint, profile, focus, model)
                    if regen is None:
                        break
                    rv = self._grade_one_sync(regen, profile)
                    regen["_verdict"] = rv
                    cur, cur_vd = regen, rv
                    if rank.get(rv.verdict, 0) > rank.get(
                            best_vd.verdict if isinstance(best_vd, qp.Verdict) else qp.FAIL, 0):
                        best, best_vd = regen, rv
                    if rv.verdict == qp.PASS:
                        break
                cards[i] = best

    def _run_quality_pass(self, cards: list, profile: dict | None,
                          same_call_grades=None) -> None:
        """Score `cards` and attach a Verdict to each as card['_verdict']. Fully
        fail-open: any grader error leaves cards un-chipped (deterministic
        pre-filter findings are still surfaced) and never blocks card creation.
        Runs auto-regeneration of FAILs when configured."""
        qpc = self.cfg.get("quality_pass") or {}
        llm_verdicts = None  # None = LLM grade unavailable (cancel/fail/skill-array)
        try:
            if same_call_grades is not None:
                llm_verdicts = qp.parse_same_call_grades(same_call_grades, cards)
            else:
                skill_inv, skill_id = _grader_skill(self.cfg, profile)
                raw = run_claude_json(
                    self.go_btn, "Scoring cards…",
                    prompt=qp.batch_prompt(cards),
                    system=self._grader_system(bool(skill_inv or skill_id)),
                    max_tokens=2048, model=_grader_model(self.cfg),
                    skill_id=skill_id, skill_invocation=skill_inv,
                )
                if raw is not None:
                    llm_verdicts = qp.parse_batch_verdicts(raw, cards)
        except Exception as e:
            print(f"[ankisstant] quality pass grade call skipped: {e}")
            llm_verdicts = None

        # Merge with deterministic pre-filters. When the LLM grade is unavailable
        # we still surface free pre-filter findings, but don't paint every card
        # PASS — un-flagged cards simply get no chip.
        try:
            verdicts = qp.apply_prefilters_then(llm_verdicts, cards, _atomicity_max(self.cfg))
        except Exception as e:
            print(f"[ankisstant] quality pass merge skipped: {e}")
            return
        graded = llm_verdicts is not None
        for c, vd in zip(cards, verdicts):
            if graded or vd.source == "prefilter":
                c["_verdict"] = vd
        if graded:
            self._auto_regenerate_failures(cards, profile, qpc)

    def _finalize_review(self, *, cards, merged_tag_levels, mode, source_label,
                         topic_label, focus, deck_name, notetype_name, tags_raw,
                         profile, gap, type_meta, cached_tag, want_tag, model,
                         same_call_grades=None, topic_autotag=False, tag_material="",
                         slide_pages=None):
        """Validate the card list, resolve the auto-tag, and open the review
        dialog. Shared by Generate (AI round-trip) and Paste cards (user brings
        their own JSON). Returns False on malformed input (a warning is shown)."""
        if not isinstance(cards, list) or not cards or not all(
            isinstance(c, dict) and "front" in c and "extra" in c for c in cards
        ):
            showWarning(
                "That isn't in the expected card format.\n\n"
                "It needs a JSON array where each card has a \"front\" and an "
                "\"extra\" field. If you pasted from your own AI, include its "
                "FULL reply (the whole JSON), then try again."
            )
            return False

        self.cfg["default_deck"] = deck_name
        self.cfg["selected_notetype"] = notetype_name
        save_tool_config("card_creator", self.cfg)

        kg_image_filenames: list[str] = []
        kg_type_for_image = ""
        kg_stem_html = ""
        kg_notes = ""
        kg_concept = ""
        kg_explanation = ""
        if gap is not None:
            kg_image_filenames = list(gap.get("images") or [])
            kg_type_for_image = (gap.get("kg_type") or "").lower()
            # Captured MQ question + freeform notes flow into the MQ field
            # alongside any screenshots so the card carries the original
            # source of confusion, not just the image.
            kg_stem_html = str(gap.get("stem_html") or "")
            kg_notes = str(gap.get("notes") or "")
            # MQ-type gaps lead the Missed Questions field with the specific
            # concept missed (the knowledge gap) plus an AI-written explanation,
            # above the screenshot. Both were resolved by the caller (folded
            # into the generation request, or pulled from a pasted reply) and
            # cached on the gap / persisted to the KG store.
            if kg_type_for_image == "mq":
                kg_concept = str(gap.get("concept") or "").strip()
                kg_explanation = str(gap.get("explanation") or "").strip()
            # Resolve the auto-tag: cached → merged-from-this-reply → separate
            # call (skill flows). Append it to every card from this gap.
            auto_tag = cached_tag
            try:
                if not auto_tag and merged_tag_levels is not None and type_meta:
                    auto_tag = _apply_tag_levels(gap, type_meta, merged_tag_levels)
                elif not auto_tag and want_tag and type_meta is not None:
                    with loading(self.go_btn, "Generating auto-tag…"):
                        auto_tag = _generate_auto_tag_for_gap(
                            gap, type_meta, model=model,
                        )
            except Exception as e:
                print(f"[ankisstant] auto-tag generation skipped: {e}")
            if auto_tag and auto_tag not in tags_raw:
                tags_raw.append(auto_tag)
        elif auto_tag_base() and (merged_tag_levels or topic_autotag):
            # No loaded KG (pasted cards, or topic/source auto-tag). Use the
            # levels the model folded into the reply; if a skill forced a bare
            # array (no folded levels) but auto-tag is on, classify separately —
            # the same shared classifier Browse uses.
            try:
                levels = merged_tag_levels
                if levels is None and topic_autotag and not is_manual_provider():
                    with loading(self.go_btn, "Generating auto-tag…"):
                        levels = autotag.classify(tag_material, model=model)
                tag = _topic_tag_from_levels(levels, type_meta)
                if tag and tag not in tags_raw:
                    tags_raw.append(tag)
            except Exception as e:
                print(f"[ankisstant] no-gap tag build skipped: {e}")

        # Quality pass: score cards (and optionally auto-regenerate failures)
        # before the review screen. Fail-open — never blocks card creation.
        if _quality_pass_active(self.cfg, profile):
            self._run_quality_pass(cards, profile, same_call_grades)

        dlg = ReviewDialog(
            cards=cards, mode=mode,
            source_label=source_label, topic_label=topic_label, focus=focus,
            deck_name=deck_name, tags=tags_raw,
            profile=profile, panel_image_paths=list(self._extra_image_paths),
            kg_image_filenames=kg_image_filenames,
            kg_type_for_image=kg_type_for_image,
            kg_stem_html=kg_stem_html,
            kg_notes=kg_notes,
            kg_concept=kg_concept,
            kg_explanation=kg_explanation,
            type_meta=type_meta,
            kg_field_values=_gap_field_values(gap),
            slide_pages=slide_pages,
            parent=self,
        )
        result = dlg.exec()

        # LO-gap queue: only pop on a successful acceptance — Cancel/reject
        # leaves the gap in place so the user can retry it.
        try:
            accepted = result == QDialog.DialogCode.Accepted
        except Exception:
            accepted = bool(result)
        if (
            accepted
            and self._current_gap is not None
            and self._main_window is not None
            and getattr(self._main_window, "gap_queue", None)
            and self._main_window.gap_queue
            and self._main_window.gap_queue[0] == self._current_gap
        ):
            popped = self._main_window.gap_queue.pop(0)
            # If this gap traces back to a KG, mark it done in the store and
            # nudge the KG panel to refresh.
            try:
                kg_id = popped.get("kg_id") if isinstance(popped, dict) else None
                if kg_id:
                    from .kg import store as kg_store
                    kg_store.update(kg_id, status="done")
                    from . import knowledge_gaps
                    knowledge_gaps._refresh_open_panel()
            except Exception as e:
                print(f"[ankisstant] mark KG done failed: {e}")
            self._current_gap = None
            # Clear the form so the next queued gap pre-fills cleanly. Without
            # this, the just-finished gap's topic/focus linger and the dirty-form
            # guard in refresh_queue_state blocks the next gap from auto-loading
            # (leaving a stale Focus from the previous KG).
            self.topic.setText("")
            self.focus.setText("")
            if hasattr(self._main_window, "refresh_queue_badge"):
                self._main_window.refresh_queue_badge()
            self.refresh_queue_state(self._main_window)
        return True

    def _make_job_record(self, *, mode, source_label, topic_label, focus, deck_name,
                         notetype_name, tags_raw, gap, type_meta, cached_tag, want_tag,
                         want_explanation, merge_tag, merge_explanation, merge_grade,
                         user_msg, system_prompt, model, skill_id, skill_invocation,
                         attachments, slide_pages=None, want_auto=False,
                         tag_material="") -> dict:
        """Snapshot everything a background job needs to generate, grade, and
        later reconstruct its review dialog — independent of live panel state.
        Imports Extra images into media and copies attachments so the record
        survives a restart."""
        import uuid as _uuid
        from . import create_jobs
        job_id = _uuid.uuid4().hex[:12]
        panel_fns = [fn for fn in (_import_image_to_media(p) for p in self._extra_image_paths)
                     if fn]
        attach_copied = create_jobs.copy_attachments(job_id, attachments or [])
        # Slide images live in the job folder (NOT media — they're only imported
        # if a card is actually created). Ordered filenames (slide_NNNN.png) mean
        # open_ready_job can restore page order with a plain sort after a restart.
        slide_copied = create_jobs.copy_attachments(job_id, list(slide_pages or []))
        is_mq = bool(gap and (gap.get("kg_type") or "").lower() == "mq")
        return {
            "id": job_id,
            "title": (topic_label or source_label or "Generation"),
            "mode": mode, "source_label": source_label, "topic_label": topic_label,
            "focus": focus, "deck_name": deck_name, "notetype_name": notetype_name,
            "tags_raw": list(tags_raw), "profile_name": notetype_name,
            "kg_image_filenames": list(gap.get("images") or []) if gap else [],
            "kg_type_for_image": (gap.get("kg_type") or "").lower() if gap else "",
            "kg_stem_html": str(gap.get("stem_html") or "") if gap else "",
            "kg_notes": str(gap.get("notes") or "") if gap else "",
            "kg_concept": str(gap.get("concept") or "").strip() if is_mq else "",
            "kg_explanation": str(gap.get("explanation") or "").strip() if is_mq else "",
            "panel_image_filenames": panel_fns,
            "attachment_paths": attach_copied,
            "slide_pages": slide_copied,
            "user_msg": user_msg, "system_prompt": system_prompt, "model": model,
            "skill_id": skill_id, "skill_invocation": skill_invocation, "max_tokens": 4096,
            "merge_tag": bool(merge_tag), "merge_explanation": bool(merge_explanation),
            "merge_grade": bool(merge_grade), "want_tag": bool(want_tag),
            "want_explanation": bool(want_explanation), "cached_tag": cached_tag or "",
            # Auto-tag: when a skill forced a bare reply (no folded levels), the
            # worker does a separate classification call on `tag_material`.
            "want_auto": bool(want_auto), "tag_material": tag_material or "",
            # Per-note AI field keys the engine requested in this object reply, so
            # the off-thread worker can route the reply the same way the modal does.
            "note_field_keys": (list(self._gen_plan.note_fields)
                                if getattr(self, "_gen_plan", None) is not None else []),
            "type_meta": type_meta, "gap": gap,
            "provider_label": (load_config().get("provider") or "auto"),
        }

    def open_ready_job(self, job_id: str) -> None:
        """Reconstruct and open the review dialog for a stored 'ready' job.
        Works even in a freshly-restarted session: the profile is rebuilt by
        name and image paths are rebuilt from media filenames (re-import is
        idempotent so ReviewDialog needs no change). On accept the job is
        removed; reject/cancel leaves it in the ready list."""
        from . import create_jobs
        rec = create_jobs.get(job_id)
        if rec is None:
            tooltip("That job is no longer available.")
            self.refresh_ready_lists()
            return
        if rec.get("status") != create_jobs.READY:
            tooltip("That generation isn't ready to review yet.")
            return

        cfg = tool_config("card_creator")
        profile = _resolved_profile(cfg, rec.get("profile_name", ""))
        try:
            media_dir = mw.col.media.dir()
        except Exception:
            media_dir = ""

        def _media_path(fn: str) -> str:
            return os.path.join(media_dir, fn) if media_dir and fn else fn

        panel_image_paths = [_media_path(fn) for fn in rec.get("panel_image_filenames", [])]
        # Slide pages were copied into the job folder with page-ordered names;
        # sort by basename to restore order and drop any that went missing.
        slide_pages = sorted(
            (p for p in rec.get("slide_pages", []) if p and os.path.exists(p)),
            key=lambda p: os.path.basename(p),
        )
        cards = [dict(c) for c in (rec.get("cards") or [])]
        verdicts = rec.get("verdicts") or []
        for i, c in enumerate(cards):
            vd = verdicts[i] if i < len(verdicts) else None
            if isinstance(vd, dict):
                try:
                    c["_verdict"] = qp.Verdict(**vd)
                except Exception:
                    pass
            imgs = c.pop("_image_filenames", None)
            if imgs:
                c["_image_paths"] = [_media_path(fn) for fn in imgs]

        dlg = ReviewDialog(
            cards=cards, mode=rec.get("mode", "source"),
            source_label=rec.get("source_label"), topic_label=rec.get("topic_label"),
            focus=rec.get("focus", ""), deck_name=rec.get("deck_name", ""),
            tags=list(rec.get("tags_raw", [])), profile=profile,
            panel_image_paths=panel_image_paths,
            kg_image_filenames=list(rec.get("kg_image_filenames", [])),
            kg_type_for_image=rec.get("kg_type_for_image", ""),
            kg_stem_html=rec.get("kg_stem_html", ""),
            kg_notes=rec.get("kg_notes", ""),
            kg_concept=rec.get("kg_concept", ""),
            kg_explanation=rec.get("kg_explanation", ""),
            type_meta=rec.get("type_meta") if isinstance(rec.get("type_meta"), dict) else None,
            kg_field_values=_gap_field_values(rec.get("gap") if isinstance(rec.get("gap"), dict) else None),
            slide_pages=slide_pages,
            parent=self,
        )
        result = dlg.exec()
        try:
            accepted = result == QDialog.DialogCode.Accepted
        except Exception:
            accepted = bool(result)
        if accepted:
            create_jobs.remove(job_id)
            try:
                gap = rec.get("gap") if isinstance(rec.get("gap"), dict) else None
                kg_id = gap.get("kg_id") if gap else None
                if kg_id:
                    from .kg import store as kg_store
                    kg_store.update(kg_id, status="done")
                    from . import knowledge_gaps
                    knowledge_gaps._refresh_open_panel()
            except Exception as e:
                print(f"[ankisstant] mark KG done (job) failed: {e}")
        self.refresh_ready_lists()
        if self._main_window is not None and hasattr(self._main_window, "refresh_queue_badge"):
            self._main_window.refresh_queue_badge()


# ── review dialog (separate window) ──────────────────────────────────────────

class _CardRow(QFrame):
    APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"

    def __init__(self, idx: int, card: dict, dup_msg: str | None,
                 on_state_change, on_regenerate, on_split, on_one_by_one,
                 slide_pages=None, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.card = card
        self.state = self.APPROVED
        self._on_state_change = on_state_change
        self._on_regenerate = on_regenerate
        self._on_split = on_split
        self._on_one_by_one = on_one_by_one
        self.slide_pages: list[str] = list(slide_pages or [])

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame { border: 1px solid #ccc; border-radius: 4px; padding: 6px; }")

        v = QVBoxLayout(self)

        header = QHBoxLayout()
        self.header_label = QLabel()
        self.header_label.setStyleSheet("font-weight: 600;")
        header.addWidget(self.header_label)
        header.addStretch(1)
        # Cards start included by default — single toggle button handles reject/undo.
        self.reject_btn = QPushButton()
        self.reject_btn.clicked.connect(self._toggle_reject)
        self.split_btn = QPushButton("✂️ Split")
        self.split_btn.setToolTip("Break this card into multiple atomic single-cloze cards.")
        self.split_btn.clicked.connect(self._on_split_clicked)
        self.obo_btn = QPushButton("1️⃣ One-by-one")
        self.obo_btn.setToolTip(
            "Rewrite as a single card with sibling {{c1}} clozes that AnKing reveals "
            "sequentially. Sets the 'One by one' field to y."
        )
        self.obo_btn.clicked.connect(self._on_obo_clicked)
        self.img_btn = QPushButton("📷 Image")
        self.img_btn.setToolTip(
            "Attach images to JUST this card's Extra field (e.g. a specific flowchart). "
            "Panel-level images are still applied to every card."
        )
        self.img_btn.clicked.connect(self._on_attach_image)
        self.img_find_btn = QPushButton("🔍")
        self.img_find_btn.setToolTip(
            "Search medical image sources and attach a result to JUST this card."
        )
        self.img_find_btn.clicked.connect(self._on_find_image)
        self.img_browse_btn = QPushButton("🌐")
        self.img_browse_btn.setToolTip(
            "Browse for an image inside Anki; right-click any image → Attach to add "
            "it to JUST this card."
        )
        self.img_browse_btn.clicked.connect(self._on_browse_image)
        # Slide picker — only when the source was a slide-deck PDF. Lets the user
        # change which slide image is attached to this card.
        self.slide_btn = QPushButton("🖼 Slide")
        self.slide_btn.setToolTip(
            "Choose which slide image(s) from the source deck attach to this card."
        )
        self.slide_btn.clicked.connect(self._on_pick_slides)
        self.slide_btn.setVisible(bool(self.slide_pages))
        for b in (self.slide_btn, self.img_btn, self.img_find_btn, self.img_browse_btn,
                  self.reject_btn, self.split_btn, self.obo_btn):
            header.addWidget(b)
        v.addLayout(header)

        # Auto-image suggestion strip — populated asynchronously when the model
        # flagged this card with an `image_query`. Hidden until candidates load.
        self.img_strip = QWidget()
        self.img_strip_layout = QHBoxLayout(self.img_strip)
        self.img_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.img_strip_layout.setSpacing(4)
        self.img_strip.setVisible(False)
        v.addWidget(self.img_strip)
        self._auto_thumbs_started = False
        self._candidate_buttons: list = []

        if dup_msg:
            dup = QLabel(f"⚠ {dup_msg}")
            dup.setStyleSheet("color: #b85c00; font-size: 11px;")
            dup.setWordWrap(True)
            v.addWidget(dup)

        self.front_lbl = QLabel(card.get("front", ""))
        self.front_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.front_lbl.setWordWrap(True)
        v.addWidget(self.front_lbl)

        self.extra_toggle = QPushButton("Show extra ▾")
        self.extra_toggle.setCheckable(True)
        self.extra_toggle.setFlat(True)
        self.extra_toggle.setStyleSheet("text-align: left; padding: 2px;")
        self.extra_toggle.clicked.connect(self._toggle_extra)
        v.addWidget(self.extra_toggle)

        self.extra_lbl = QLabel(card.get("extra", ""))
        self.extra_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.extra_lbl.setWordWrap(True)
        self.extra_lbl.setStyleSheet("color: #555; padding-left: 12px;")
        self.extra_lbl.setVisible(False)
        v.addWidget(self.extra_lbl)

        hint_row = QHBoxLayout()
        hint_row.addWidget(QLabel("Regen hint:"))
        self.hint_input = QLineEdit()
        self.hint_input.setPlaceholderText(
            "(optional) e.g. shorter, focus on mechanism, use simpler wording…"
        )
        self.hint_input.returnPressed.connect(self._on_regen_clicked)
        hint_row.addWidget(self.hint_input, 1)
        self.regen_btn = QPushButton("🔄 Regenerate")
        self.regen_btn.clicked.connect(self._on_regen_clicked)
        hint_row.addWidget(self.regen_btn)
        v.addLayout(hint_row)

        # Quality pass: pre-fill the worst-dimension reason as the regen hint on
        # FLAGged cards, so the user sees why and can regenerate with one click.
        vd = self.card.get("_verdict")
        if (isinstance(vd, qp.Verdict) and vd.verdict == qp.FLAG
                and vd.reason and not self.hint_input.text()):
            self.hint_input.setText(vd.reason)

        self._refresh_header()
        self._maybe_start_auto_image()

    # ── auto-image suggestions ────────────────────────────────────────────────

    def _maybe_start_auto_image(self):
        """If the model flagged this card with an `image_query`, fetch candidate
        thumbnails in the background and show them as a clickable strip."""
        if self._auto_thumbs_started:
            return
        query = str(self.card.get("image_query") or "").strip()
        if not query:
            return
        self._auto_thumbs_started = True
        from . import images
        try:
            cfg = tool_config("card_creator")
            limit = int((cfg.get("images") or {}).get("max_per_card", 4))
        except Exception:
            cfg, limit = None, 4
        lbl = QLabel("🖼 finding images…")
        lbl.setStyleSheet("color: gray; font-size: 11px;")
        self.img_strip_layout.addWidget(lbl)
        self._strip_status = lbl
        self.img_strip.setVisible(True)

        def _run():
            try:
                res = images.search_images(query, cfg=cfg, limit_per_source=2)
            except Exception:
                res = []
            res = res[:max(limit, 1)]
            mw.taskman.run_on_main(lambda: self._on_candidates(res))

        # Throttled shared pool — concurrent card rows won't all fire at once.
        images.submit(_run)

    def _on_candidates(self, res: list):
        try:
            self._strip_status.setParent(None)
        except Exception:
            pass
        if not res:
            self.img_strip.setVisible(False)
            return
        cap = QLabel("🖼 Suggested — click a preview to attach it to this card:")
        cap.setStyleSheet("color: gray; font-size: 11px;")
        self.img_strip_layout.addWidget(cap)
        from . import images
        for r in res:
            btn = QPushButton(f"{r.get('source', '?')}\n…")
            btn.setCheckable(True)
            btn.setMinimumSize(QSize(104, 104))
            btn.setIconSize(QSize(96, 96))
            btn.setToolTip((r.get("title", "") or "")
                           + "\nClick to attach this image to the card.")
            btn.clicked.connect(lambda _c=False, rr=r, bb=btn: self._attach_candidate(rr, bb))
            self.img_strip_layout.addWidget(btn)
            self._candidate_buttons.append((r, btn))
        self.img_strip_layout.addStretch(1)

        def _thumbs():
            for r, btn in list(self._candidate_buttons):
                data = None
                # Try the thumbnail; fall back to the full-size URL if the
                # thumb host rejects us, so a preview still shows.
                for u in (r.get("thumb_url"), r.get("url")):
                    if not u:
                        continue
                    try:
                        data = images._get(u, timeout=8)
                    except Exception:
                        data = None
                    if data:
                        break
                mw.taskman.run_on_main(
                    lambda d=data, b=btn, rr=r: self._set_candidate_thumb(b, d, rr))
        images.submit(_thumbs)

    def _set_candidate_thumb(self, btn, data, r=None):
        try:
            if data:
                pix = QPixmap()
                if pix.loadFromData(data):
                    btn.setIcon(QIcon(pix))
                    btn.setText("")
                    return
        except Exception:
            pass
        # No preview available (couldn't render) — keep it attachable with a
        # clear label so the suggestion is never a blank button.
        src = (r or {}).get("source", "image")
        btn.setText(f"📎 {src}\n(no preview)")

    def _attach_candidate(self, r: dict, btn):
        """Download the chosen candidate and attach it to this card."""
        from . import images
        btn.setEnabled(False)
        url = r.get("url", "")

        def _run():
            path = images.download_to_temp(url)
            mw.taskman.run_on_main(lambda: self._on_candidate_attached(path, btn))
        images.submit(_run)

    def _on_candidate_attached(self, path, btn):
        btn.setEnabled(True)
        if not path:
            tooltip("Couldn't download that image.")
            btn.setChecked(False)
            return
        imgs = list(self.card.get("_image_paths") or [])
        if path not in imgs:
            imgs.append(path)
        self.card["_image_paths"] = imgs
        btn.setChecked(True)
        btn.setToolTip("Attached ✓")
        self._refresh_header()

    def _toggle_extra(self):
        on = self.extra_toggle.isChecked()
        self.extra_lbl.setVisible(on)
        self.extra_toggle.setText("Hide extra ▴" if on else "Show extra ▾")

    def _set_state(self, state):
        self.state = state
        self._refresh_header()
        self._on_state_change()

    def _toggle_reject(self):
        self._set_state(self.APPROVED if self.state == self.REJECTED else self.REJECTED)

    def _refresh_header(self):
        suffix = ""
        vd = self.card.get("_verdict")
        if isinstance(vd, qp.Verdict):
            suffix += f"  · {vd.chip()}"
            self.setToolTip(vd.reason or "")
        n_slides = len(self.card.get("_slide_paths") or [])
        n_imgs = len(self.card.get("_image_paths") or [])
        if n_slides:
            suffix += f"  · 🖼 slide {', '.join(str(i + 1) for i in (self.card.get('_slide_indices') or []))}"
        n_other = n_imgs - n_slides
        if n_other > 0:
            suffix += f"  · 📷 {n_other}"
        if self.state == self.REJECTED:
            self.header_label.setText(f"Card {self.idx + 1} — ✗ rejected{suffix}")
            self.reject_btn.setText("↺ Re-include")
        else:
            self.header_label.setText(f"Card {self.idx + 1} — ✓ included{suffix}")
            self.reject_btn.setText("✗ Reject")

    def _on_regen_clicked(self):
        hint = self.hint_input.text().strip()
        with loading(self.regen_btn, "Regenerating…"):
            new = self._on_regenerate(self.card, hint)
        if new is None:
            return
        self.card = new
        self.front_lbl.setText(new.get("front", ""))
        self.extra_lbl.setText(new.get("extra", ""))
        self.hint_input.clear()
        self._set_state(self.APPROVED)

    def _on_split_clicked(self):
        with loading(self.split_btn, "Splitting…"):
            self._on_split(self)

    def _on_obo_clicked(self):
        with loading(self.obo_btn, "Rewriting…"):
            new = self._on_one_by_one(self.card)
        if new is None:
            return
        new["one_by_one"] = True
        self.card = new
        self.front_lbl.setText(new.get("front", ""))
        self.extra_lbl.setText(new.get("extra", ""))
        self.header_label.setText(
            f"{self.header_label.text()}  · 1️⃣ one-by-one"
        )
        self._set_state(self.APPROVED)

    def _on_attach_image(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach images to this card",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;All files (*)",
        )
        if not paths:
            return
        imgs = list(self.card.get("_image_paths") or [])
        for p in paths:
            if p not in imgs:
                imgs.append(p)
        self.card["_image_paths"] = imgs
        self._refresh_header()

    def _image_seed(self) -> str:
        seed = str(self.card.get("image_query") or "").strip()
        if seed:
            return seed[:80]
        seed = re.sub(r"<[^>]+>|\{\{[^}]+\}\}", " ", self.card.get("front", "") or "")
        return re.sub(r"\s+", " ", seed).strip()[:80]

    def _add_card_images(self, paths: list) -> None:
        imgs = list(self.card.get("_image_paths") or [])
        for p in paths:
            if p and p not in imgs:
                imgs.append(p)
        self.card["_image_paths"] = imgs
        self._refresh_header()

    def _on_find_image(self):
        try:
            from . import images
            paths = images.pick_images(self, self._image_seed())
        except Exception as e:
            showWarning(f"Image search failed: {e}")
            return
        if paths:
            self._add_card_images(paths)

    def _on_browse_image(self):
        try:
            from . import images
            paths = images.browse_for_images(self, self._image_seed())
        except Exception as e:
            showWarning(f"Image browse failed: {e}")
            return
        if paths:
            self._add_card_images(paths)

    def _on_pick_slides(self):
        """Open the slide gallery and update which slide image(s) attach here."""
        if not self.slide_pages:
            return
        current = list(self.card.get("_slide_indices") or [])
        dlg = _SlideGalleryDialog(self.slide_pages, current, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.card["_slide_indices"] = dlg.selected_indices()
        _sync_slide_images(self.card, self.slide_pages)
        self._refresh_header()


class _SlideGalleryDialog(QDialog):
    """A thumbnail grid of a slide deck's pages; the user ticks the slide(s) to
    attach to a card. Slides are local PNGs already rendered from the PDF, so
    thumbnails load synchronously (no network)."""

    def __init__(self, slide_pages: list, selected: list, parent=None):
        super().__init__(parent)
        self.slide_pages = list(slide_pages or [])
        self._buttons: list = []
        self.setWindowTitle("Choose slide images")
        self.setMinimumSize(720, 560)
        self._build(set(selected or []))

    def _build(self, selected: set):
        root = QVBoxLayout(self)
        cap = QLabel(
            "Click slides to attach them to this card. Click again to remove. "
            f"({len(self.slide_pages)} slides)"
        )
        cap.setStyleSheet("color: gray; font-size: 11px;")
        cap.setWordWrap(True)
        root.addWidget(cap)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(8)
        cols = 4
        for i, path in enumerate(self.slide_pages):
            btn = QPushButton(f"Slide {i + 1}")
            btn.setCheckable(True)
            btn.setChecked(i in selected)
            btn.setMinimumSize(QSize(150, 120))
            btn.setIconSize(QSize(140, 96))
            try:
                pix = QPixmap(path)
                if not pix.isNull():
                    btn.setIcon(QIcon(pix))
            except Exception:
                pass
            grid.addWidget(btn, i // cols, i % cols)
            self._buttons.append(btn)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def selected_indices(self) -> list:
        return [i for i, b in enumerate(self._buttons) if b.isChecked()]


class ReviewDialog(QDialog):
    def __init__(self, cards, mode, source_label, topic_label, focus,
                 deck_name, tags, profile=None, panel_image_paths=None,
                 kg_image_filenames=None, kg_type_for_image="",
                 kg_stem_html="", kg_notes="",
                 kg_concept="", kg_explanation="",
                 type_meta=None, kg_field_values=None,
                 slide_pages=None,
                 parent=None):
        super().__init__(parent)
        self.cfg = tool_config("card_creator")
        self.cards_in = cards
        self.mode = mode
        self.source_label = source_label
        self.topic_label = topic_label
        self.focus = focus
        self.deck_name = deck_name
        self.tags = tags
        # Selected notetype profile + panel-level image attachments. Falls
        # back to a synthesised legacy-fields profile when called from older
        # code paths that don't pass `profile`.
        self.profile = profile or _resolved_profile(
            self.cfg, (self.cfg.get("selected_notetype") or self.cfg.get("default_notetype") or "").strip()
        )
        self.panel_image_paths: list[str] = list(panel_image_paths or [])
        # KG-attached images live in the Anki media folder already (added on
        # KG save), so we carry filenames + emit <img> HTML directly. MQ-type
        # KGs route the image into the Missed Questions field; everything
        # else falls through to the normal Extra/image field.
        self.kg_image_filenames: list[str] = list(kg_image_filenames or [])
        self.kg_type_for_image: str = (kg_type_for_image or "").lower()
        # MQ-type KGs also carry the captured question stem (HTML) and any
        # freeform notes. We append these into the Missed Questions field
        # together with the images so every card created from the MQ
        # carries the original missed question for reference.
        self.kg_stem_html: str = str(kg_stem_html or "")
        self.kg_notes: str = str(kg_notes or "")
        # MQ-type KGs: the specific concept missed (the knowledge gap) and an
        # optional AI-written explanation of it. Both are rendered above the
        # screenshot in the Missed Questions field (see _kg_content_html).
        self.kg_concept: str = str(kg_concept or "").strip()
        self.kg_explanation: str = str(kg_explanation or "").strip()
        # The KG type definition + this gap's per-note field values, used to route
        # any USER-DEFINED fields that target a literal Anki field (kind="field")
        # into that field. Built-in role composition (the Missed Questions field)
        # stays in _kg_content_html; this only adds custom field→field targets.
        self.type_meta: dict | None = type_meta if isinstance(type_meta, dict) else None
        self.kg_field_values: dict = dict(kg_field_values or {})
        # Rendered page-images of the source slide deck (if a single PDF was the
        # source). Each card's model-chosen `slide` is resolved to its page image
        # here and the user can swap it via the per-card slide gallery.
        self.slide_pages: list[str] = list(slide_pages or [])
        if self.slide_pages:
            for c in self.cards_in:
                _sync_slide_images(c, self.slide_pages)
        self.rows: list[_CardRow] = []
        self.setWindowTitle("Review proposed cards")
        self.setMinimumSize(800, 700)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)

        self.header = QLabel()
        self.header.setStyleSheet("font-weight: 600;")
        root.addWidget(self.header)

        meta = QLabel(self._meta_text())
        meta.setStyleSheet("color: gray; font-size: 11px;")
        meta.setWordWrap(True)
        root.addWidget(meta)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.inner_layout = QVBoxLayout(inner)
        self.inner_layout.setSpacing(8)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        for i, card in enumerate(self.cards_in):
            dup = self._dup_check(card)
            row = _CardRow(i, card, dup, self._on_state_change,
                           self._on_regenerate, self._on_split,
                           self._on_one_by_one,
                           slide_pages=self.slide_pages)
            self.rows.append(row)
            self.inner_layout.addWidget(row)
        self.inner_layout.addStretch(1)

        bulk = QHBoxLayout()
        ba = QPushButton("Approve all")
        ba.clicked.connect(lambda: self._bulk(_CardRow.APPROVED))
        br = QPushButton("Reject all")
        br.clicked.connect(lambda: self._bulk(_CardRow.REJECTED))
        bulk.addWidget(ba)
        bulk.addWidget(br)
        bulk.addStretch(1)
        root.addLayout(bulk)

        bb = QHBoxLayout()
        self.create_btn = QPushButton("Create All (trust mode)")
        self.create_btn.clicked.connect(self._on_create_all)
        self.add_screen_btn = QPushButton("Open in Add Screen")
        self.add_screen_btn.clicked.connect(self._on_add_screen)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(cancel)
        bb.addWidget(self.add_screen_btn)
        bb.addWidget(self.create_btn)
        root.addLayout(bb)

        self._refresh_header()

    def _meta_text(self):
        parts = [f"Deck: {self.deck_name}",
                 f"Notetype: {self.profile.get('name', '')}",
                 f"Tags: {', '.join(self.tags)}"]
        if self.panel_image_paths:
            n = len(self.panel_image_paths)
            parts.append(f"Extra image{'s' if n != 1 else ''}: {n} (applied to every card)")
        if self.mode == "source":
            parts.append(f"Source: {self.source_label or '(pasted)'}")
        else:
            parts.append(f"Topic: {self.topic_label}")
        if self.focus:
            parts.append(f"Focus: {self.focus}")
        return " · ".join(parts)

    def _bulk(self, state):
        for r in self.rows:
            r.state = state
            r._refresh_header()
        self._on_state_change()

    def _on_state_change(self):
        self._refresh_header()

    def _refresh_header(self):
        approved = sum(1 for r in self.rows if r.state == _CardRow.APPROVED)
        rejected = sum(1 for r in self.rows if r.state == _CardRow.REJECTED)
        self.header.setText(
            f"{approved} approved · {rejected} rejected · {len(self.rows)} total"
        )

    # ── duplicate detection ───────────────────────────────────────────────────

    def _dup_check(self, card) -> str | None:
        plain = anki_utils.strip_cloze(anki_utils.strip_html(card.get("front", "")))[:50].strip()
        if len(plain) < 12:
            return None
        safe = plain.replace("\\", "\\\\").replace('"', '\\"')
        front_field = self.profile.get("front_field", "Text")
        try:
            nids = mw.col.find_notes(f'"{front_field}:*{safe}*"')
        except Exception:
            return None
        if nids:
            return f"Possible duplicate — {len(nids)} note(s) already contain '{plain[:40]}…'"
        return None

    # ── regenerate / one-by-one / split ───────────────────────────────────────

    def _on_regenerate(self, old_card, hint: str = ""):
        # Shared with the quality-pass auto-regenerate loop. Preserves per-card
        # images the user attached pre-regen.
        return _regenerate_one(
            old_card, hint, self.profile, self.focus,
            tool_model_for("card_creation"),
        )

    def _on_one_by_one(self, old_card):
        prompt = (
            "Original card front:\n" + old_card.get("front", "") + "\n\n"
            "Original card extra:\n" + old_card.get("extra", "") + "\n\n"
            "Rewrite as ONE card with multiple sibling {{c1::...}} clozes so AnKing's "
            "'One by one' addon reveals each in sequence."
        )
        skill_invocation, skill_id = _resolve_skill(self.profile)
        new = core_api.ask_claude_json(
            prompt=prompt,
            system=_augment_system(ONE_BY_ONE_SYSTEM, self.profile),
            max_tokens=1024, model=tool_model_for("card_creation"),
            skill_id=skill_id, skill_invocation=skill_invocation,
        )
        if not isinstance(new, dict) or "front" not in new or "extra" not in new:
            return None
        front = new["front"]
        if re.search(r"\{\{c[2-9]\d*::", front):
            new["front"] = re.sub(r"\{\{c\d+::", "{{c1::", front)
        if old_card.get("_image_paths"):
            new["_image_paths"] = list(old_card["_image_paths"])
        return new

    def _on_split(self, row):
        prompt = (
            "Original card front:\n" + row.card.get("front", "") + "\n\n"
            "Original card extra:\n" + row.card.get("extra", "") + "\n\n"
            "Split this card into the smallest number of ATOMIC single-cloze cards "
            "that preserve all the testable facts. Each output card must have exactly "
            "one {{c1::...}} cloze."
        )
        skill_invocation, skill_id = _resolve_skill(self.profile)
        new_cards = core_api.ask_claude_json(
            prompt=prompt,
            system=_augment_system(SPLIT_SYSTEM, self.profile),
            max_tokens=2048, model=tool_model_for("card_creation"),
            skill_id=skill_id, skill_invocation=skill_invocation,
        )
        if (not isinstance(new_cards, list) or not new_cards
                or not all(isinstance(c, dict) and "front" in c and "extra" in c for c in new_cards)):
            return

        idx_in_layout = self.inner_layout.indexOf(row)
        idx_in_rows = self.rows.index(row)
        self.inner_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self.rows.pop(idx_in_rows)

        # Split children inherit the parent card's slide attachment.
        parent_slide_idxs = list(row.card.get("_slide_indices") or [])
        for offset, card in enumerate(new_cards):
            if self.slide_pages and parent_slide_idxs:
                card["_slide_indices"] = list(parent_slide_idxs)
                _sync_slide_images(card, self.slide_pages)
            dup = self._dup_check(card)
            new_row = _CardRow(
                idx=0, card=card, dup_msg=dup,
                on_state_change=self._on_state_change,
                on_regenerate=self._on_regenerate,
                on_split=self._on_split,
                on_one_by_one=self._on_one_by_one,
                slide_pages=self.slide_pages,
            )
            self.inner_layout.insertWidget(idx_in_layout + offset, new_row)
            self.rows.insert(idx_in_rows + offset, new_row)

        for new_idx, r in enumerate(self.rows):
            r.idx = new_idx
            r._refresh_header()
        self._on_state_change()

    # ── confirm paths ─────────────────────────────────────────────────────────

    def _approved_cards(self):
        return [r.card for r in self.rows if r.state == _CardRow.APPROVED]

    def _enrich_extra(self, extra: str) -> str:
        out = extra or ""
        if self.mode == "source" and self.source_label:
            cite = f"Source: {self.source_label}"
            if cite not in out:
                out = (out + "<br>" if out else "") + cite
        return out

    def _kg_image_html(self) -> str:
        """<img> HTML for KG-attached images (filenames already in media)."""
        bits = [f'<img src="{html.escape(f, quote=True)}">'
                for f in self.kg_image_filenames if f]
        return "<br>".join(bits)

    def _kg_content_html(self) -> str:
        """Full HTML to append to the KG target field — for MQ-type KGs this
        leads with the specific knowledge gap (concept) and an AI-written
        explanation of it, then the captured question stem and any freeform
        notes above the screenshot(s). For non-MQ KGs it's just the images.

        Returns "" if there's nothing to append."""
        parts: list[str] = []
        if self.kg_type_for_image == "mq":
            if self.kg_concept:
                parts.append(
                    f"<b>Knowledge gap:</b> {html.escape(self.kg_concept)}"
                )
            if self.kg_explanation:
                parts.append(
                    "<i>" + html.escape(self.kg_explanation).replace("\n", "<br>")
                    + "</i>"
                )
            if self.kg_stem_html.strip():
                parts.append(self.kg_stem_html.strip())
            if self.kg_notes.strip():
                # Notes are plain-text; escape for safety.
                parts.append(html.escape(self.kg_notes.strip()).replace("\n", "<br>"))
        img_html = self._kg_image_html()
        if img_html:
            parts.append(img_html)
        return "<br>".join(parts)

    def _kg_image_target_field(self) -> str:
        """Field that KG-attached images should land in. MQ-type KGs route to
        the qbank Missed Questions field; everything else uses Extra."""
        if self.kg_type_for_image == "mq":
            try:
                qb_cfg = tool_config("qbank")
                return qb_cfg.get("missed_q_field") or "Missed Questions"
            except Exception:
                return "Missed Questions"
        return self.profile.get("extra_field", "Extra")

    def _images_for(self, card) -> str:
        """Build an <img>-tag fragment combining panel-level images and any
        per-card images attached during review. Files are copied into Anki
        media here (NOT at generation time) so we only import what actually
        gets created."""
        paths: list[str] = []
        paths.extend(self.panel_image_paths)
        paths.extend(card.get("_image_paths") or [])
        if not paths:
            return ""
        return _img_tags_for(paths)

    def _get_notetype(self):
        name = (self.profile.get("name") or "").strip()
        if not name:
            showWarning(
                "No notetype configured.\n\n"
                "Open Ankisstant Settings → AI Create and add a "
                "notetype profile."
            )
            return None
        nt = mw.col.models.by_name(name)
        if nt is None:
            showWarning(
                f"Notetype not found: {name}\n\n"
                "Edit or remove this notetype profile under Settings → "
                "AI Create."
            )
            return None
        front_field = (self.profile.get("front_field") or "Text").strip()
        field_names = {f["name"] for f in nt.get("flds", [])}
        if front_field and front_field not in field_names:
            showWarning(
                f"Notetype '{name}' has no field named '{front_field}'.\n\n"
                f"Available fields: {', '.join(sorted(field_names)) or '(none)'}\n\n"
                "Update the profile's 'Front field' under Settings → AI Create."
            )
            return None
        # Cloze sanity check — non-cloze notetypes will silently swallow {{c1::…}}.
        if nt.get("type", 0) != 1:
            log.warn(f"notetype {name!r} is not a cloze type — generated cards may not render as expected.")
        return nt

    def _get_deck_id(self):
        return mw.col.decks.id(self.deck_name)

    def _set_fields(self, note, card):
        front_field   = self.profile.get("front_field", "Text")
        extra_field   = self.profile.get("extra_field", "Extra")
        image_field   = self.profile.get("image_field", extra_field)
        sources_field = (self.profile.get("sources_field") or "").strip()
        obo_field     = self.profile.get("one_by_one_field", "One by one")
        try:
            note[front_field] = card.get("front", "")
        except KeyError:
            note.fields[0] = card.get("front", "")
        if extra_field in note:
            note[extra_field] = self._enrich_extra(card.get("extra", ""))
        # Images go into image_field (often == extra_field, in which case we
        # append; if it's a separate field, that field is set directly).
        img_html = self._images_for(card)
        if img_html and image_field in note:
            if image_field == extra_field:
                existing = note[image_field] or ""
                note[image_field] = (existing + "<br>" if existing else "") + img_html
            else:
                note[image_field] = img_html
        source_html = (card.get("source") or "").strip()
        if source_html and sources_field and sources_field in note:
            if sources_field == extra_field:
                existing = note[sources_field] or ""
                note[sources_field] = (existing + "<br>" if existing else "") + source_html
            else:
                note[sources_field] = source_html
        # KG-attached content: for MQ-type KGs this is the captured question
        # stem + freeform notes + screenshot(s) routed to Missed Questions.
        # Non-MQ KGs just contribute their images to Extra (or image_field).
        kg_content = self._kg_content_html()
        if kg_content:
            target = self._kg_image_target_field()
            if target and target in note:
                existing = note[target] or ""
                note[target] = (existing + "<br>" if existing else "") + kg_content
        if card.get("one_by_one") and obo_field in note:
            note[obo_field] = "y"
        # User-defined fields whose declarative target is a literal Anki field
        # (kind="field") are composed and written here. Built-in role targets
        # (front/extra/image/missed_q) are handled above; this only adds the
        # custom field→field routing, so default types are unaffected.
        if self.type_meta and self.kg_field_values:
            try:
                qb_cfg = tool_config("qbank")
                custom = engine.note_field_plan(
                    self.type_meta, self.profile, qb_cfg,
                    lambda k: self.kg_field_values.get(k, ""), only_kinds=("field",))
                for fname, val in custom.items():
                    if val and fname in note:
                        existing = note[fname] or ""
                        note[fname] = (existing + "<br>" if existing else "") + val
            except Exception as e:
                log.warn(f"custom field routing skipped: {e}")
        for t in self.tags:
            note.add_tag(t)

    def _on_create_all(self):
        if not anki_utils.require_col():
            return
        approved = self._approved_cards()
        if not approved:
            tooltip("No approved cards.")
            return
        if not askUser(f"Create {len(approved)} card(s) directly in deck '{self.deck_name}'?"):
            return
        nt = self._get_notetype()
        if nt is None:
            return
        deck_id = self._get_deck_id()
        created_ids = []
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("AI Create")
        for card in approved:
            note = mw.col.new_note(nt)
            self._set_fields(note, card)
            mw.col.add_note(note, deck_id)
            created_ids.append(note.id)
        mw.reset()
        _append_session(
            mode=self.mode, source=self.source_label, topic=self.topic_label,
            cards_proposed=len(self.cards_in), cards_created=len(created_ids),
            card_ids=created_ids,
        )
        tooltip(f"Created {len(created_ids)} card(s).")
        self.accept()

    def _on_add_screen(self):
        if not anki_utils.require_col():
            return
        approved = self._approved_cards()
        if not approved:
            tooltip("No approved cards.")
            return
        nt = self._get_notetype()
        if nt is None:
            return
        deck_id = self._get_deck_id()

        ctx = {
            "nt": nt,
            "deck_id": deck_id,
            "tags": list(self.tags),
            "front_field": self.profile.get("front_field", "Text"),
            "extra_field": self.profile.get("extra_field", "Extra"),
            "image_field":   self.profile.get("image_field", self.profile.get("extra_field", "Extra")),
            "sources_field": (self.profile.get("sources_field") or "").strip(),
            "obo_field":     self.profile.get("one_by_one_field", "One by one"),
            "enricher":      self._enrich_extra,
            "images_for":    self._images_for,
            # `kg_content_html` includes the captured MQ stem + notes + images
            # for MQ-type KGs (just images otherwise). Replaces the older
            # `kg_image_html` ctx key, which only carried screenshots.
            "kg_content_html": self._kg_content_html(),
            "kg_image_field":  self._kg_image_target_field(),
        }
        # First card now, rest auto-loaded after each Anki "Add" click.
        _start_add_queue(approved, ctx)

        _append_session(
            mode=self.mode, source=self.source_label, topic=self.topic_label,
            cards_proposed=len(self.cards_in), cards_created=0, card_ids=[],
        )
        self.accept()


# ── persistent Add-Screen queue (survives ReviewDialog dismissal) ────────────

_add_queue: list[dict] = []
_add_queue_ctx: dict | None = None
_add_dialog = None  # the AddCards instance we're driving
_hook_attached = False


def _fill_addcards(ac, card: dict, ctx: dict) -> None:
    nt = ctx["nt"]
    new_note = mw.col.new_note(nt)
    front_field = ctx["front_field"]
    extra_field = ctx["extra_field"]
    image_field = ctx.get("image_field", extra_field)
    obo_field   = ctx["obo_field"]
    try:
        new_note[front_field] = card.get("front", "")
    except KeyError:
        new_note.fields[0] = card.get("front", "")
    if extra_field in new_note:
        new_note[extra_field] = ctx["enricher"](card.get("extra", ""))
    img_html = ctx["images_for"](card) if ctx.get("images_for") else ""
    if img_html and image_field in new_note:
        if image_field == extra_field:
            existing = new_note[image_field] or ""
            new_note[image_field] = (existing + "<br>" if existing else "") + img_html
        else:
            new_note[image_field] = img_html
    sources_field = ctx.get("sources_field") or ""
    source_html = (card.get("source") or "").strip()
    if source_html and sources_field and sources_field in new_note:
        if sources_field == extra_field:
            existing = new_note[sources_field] or ""
            new_note[sources_field] = (existing + "<br>" if existing else "") + source_html
        else:
            new_note[sources_field] = source_html
    kg_content = ctx.get("kg_content_html") or ""
    if kg_content:
        target = ctx.get("kg_image_field") or extra_field
        if target in new_note:
            existing = new_note[target] or ""
            new_note[target] = (existing + "<br>" if existing else "") + kg_content
    if card.get("one_by_one") and obo_field in new_note:
        new_note[obo_field] = "y"
    for t in ctx["tags"]:
        new_note.add_tag(t)
    try:
        ac.set_note(new_note, deck_id=ctx["deck_id"])
    except Exception as e:
        print(f"[ankisstant] ac.set_note failed: {e}; falling back to editor.set_note")
        ac.editor.set_note(new_note)
    # Defensive: re-apply tags on the editor in case sticky-tag behaviour clobbered them.
    try:
        if hasattr(ac.editor, "tags") and hasattr(ac.editor.tags, "setText"):
            ac.editor.tags.setText(" ".join(new_note.tags))
    except Exception as e:
        print(f"[ankisstant] couldn't refresh editor tag widget: {e}")


def _start_add_queue(cards: list[dict], ctx: dict) -> None:
    global _add_queue, _add_queue_ctx, _add_dialog
    if not cards:
        return
    mw.col.models.set_current(ctx["nt"])
    mw.col.decks.select(ctx["deck_id"])

    from aqt import dialogs
    ac = dialogs.open("AddCards", mw)
    _add_dialog = ac
    _add_queue = list(cards[1:])
    _add_queue_ctx = ctx

    _fill_addcards(ac, cards[0], ctx)

    # Float AddCards over the Ankisstant window so it isn't buried under
    # the Create panel after opening. WindowStaysOnTopHint is sticky — we
    # only want it raised, not pinned, so use show()+raise_()+activate
    # plus a deferred call after Qt's own focus settles.
    try:
        ac.show()
        ac.raise_()
        ac.activateWindow()
        QTimer.singleShot(50, lambda a=ac: (a.raise_(), a.activateWindow()))
    except Exception as e:
        print(f"[ankisstant] couldn't raise AddCards: {e}")

    if _add_queue:
        _attach_add_hook()
        try:
            if not getattr(ac, "_ankisstant_finished_hooked", False):
                ac.finished.connect(_on_add_dialog_finished)
                ac._ankisstant_finished_hooked = True
        except Exception as e:
            print(f"[ankisstant] couldn't hook AddCards.finished: {e}")
        tooltip(
            f"{len(_add_queue) + 1} card(s) queued. Click Anki's Add button — "
            f"the next will load automatically. Close the window to discard the rest."
        )
    else:
        tooltip("Card sent to Add Cards. Review then click Add.")


def _advance_add_queue():
    global _add_queue, _add_queue_ctx, _add_dialog
    if not _add_queue or _add_queue_ctx is None or _add_dialog is None:
        _stop_add_queue()
        return
    ac = _add_dialog
    next_card = _add_queue.pop(0)
    _fill_addcards(ac, next_card, _add_queue_ctx)
    remaining = len(_add_queue)
    if remaining:
        tooltip(f"{remaining} card(s) left in queue.")
    else:
        tooltip("Last queued card loaded. Click Add to finish.")


def _on_add_did_add_note(note):
    # Defer so Anki's own post-Add reset (which blanks the editor) finishes first.
    QTimer.singleShot(0, _advance_add_queue)


def _on_add_dialog_finished(*_args):
    _stop_add_queue()


def _attach_add_hook():
    global _hook_attached
    if _hook_attached:
        return
    gui_hooks.add_cards_did_add_note.append(_on_add_did_add_note)
    _hook_attached = True


def _stop_add_queue():
    global _add_queue, _add_queue_ctx, _add_dialog, _hook_attached
    if _add_queue:
        tooltip(f"Discarded {len(_add_queue)} unsent card(s) from the queue.")
    _add_queue = []
    _add_queue_ctx = None
    _add_dialog = None
    if _hook_attached:
        try:
            gui_hooks.add_cards_did_add_note.remove(_on_add_did_add_note)
        except ValueError:
            pass
        _hook_attached = False


# ── Tool contract ────────────────────────────────────────────────────────────

_panel: CreatorPanel | None = None


def init(main_window) -> None:
    # Any background job left queued/running when Anki last quit had its work
    # abandoned — mark it interrupted (recoverable via Retry) so it doesn't look
    # stuck. Runs once per session.
    try:
        from . import create_jobs
        create_jobs.mark_interrupted_on_startup()
    except Exception as e:
        print(f"[ankisstant] create_jobs startup cleanup failed: {e}")
    return None


_scroll = None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        from aqt.qt import QScrollArea
        _panel = CreatorPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.cfg = tool_config("card_creator")
        _panel._rebuild_notetype_combo()
    _panel.refresh_setup_banner()
    return _scroll
