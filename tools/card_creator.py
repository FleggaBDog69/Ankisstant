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
from dataclasses import dataclass, field
from datetime import datetime as _dt

from aqt import mw, gui_hooks
from aqt.qt import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QIcon, QImage, QLabel, QLineEdit, QPixmap, QPushButton, QScrollArea,
    QSize, Qt, QTimer, QVBoxLayout, QWidget,
)
from aqt.utils import showWarning, tooltip

from . import autotag
from . import quality_pass as qp
from . import pipeline
from ..core import anki_utils, api as core_api, log
from ..core.config import (
    active_family, load_config, mq_explain_enabled,
    tool_config,
)
from ..core.qt_utils import (
    loading, theme_dialog,
)
from .kg import engine
from ..core import synapse


NAME = "AI Create"


# ── prompts ───────────────────────────────────────────────────────────────────

# The hard ceiling on a cloze front, in visible words (cloze answers count;
# HTML, hints and the {{c1:: }} markup don't). Fletcher's number, 2026-10-10:
# the AnKing deck's median front is 13 words and its 95th percentile ~25, while
# the cards this add-on wrote had drifted to a median of 19 and climbing. One
# constant, read by the writing prompt, the deterministic prefilter, the grader
# rubric and the tighten pass, so all four agree on what "too long" means.
MAX_FRONT_WORDS = 25


# The front-emphasis + allowed-HTML rules, hoisted out of CARD_GEN_SYSTEM so the
# prompts that REWRITE a card (split, one-by-one, tighten) carry the identical
# text. A rewritten card is still a card he has to read at 6am, and a rewrite
# prompt that says nothing about markup gets back plain prose — measured, see
# the emphasis repair in ensure_front_emphasis(). One definition, four prompts.
FRONT_EMPHASIS_RULES = """- FRONT EMPHASIS — do this on EVERY card, for fast visual recognition. Mark up key words
  INLINE with HTML so the eye lands on the anchor and the target instantly:
    • <b>Bold</b> the topic/disease anchor and any augmenting qualifier ('first-line',
      'most common', 'initial', 'acute', 'irreversible').
    • <u>Underline</u> the words in the cue that are ALREADY doing the retrieval
      work — the finding, the contrast, the qualifier that pins the answer. Mark up
      what is there. NEVER add a phrase to the front just so there is something to
      underline.
      A SHAPE WORD (the <u>investigation</u>, the <u>vessel</u>, the <u>protozoa</u>)
      earns its place ONLY when the cue would otherwise admit more than one
      answer — it is a disambiguator, not decoration.
      NECESSITY TEST — before writing a shape word, delete it and re-read the
      cue. Does more than one answer now fit? If no, leave it out.
        BAD:  "…carry a markedly elevated rate of <u>self-inflicted death</u>,
               i.e. {{c1::suicide}}."   (the underlined phrase IS the answer, said twice)
        GOOD: "…are at markedly elevated risk of {{c1::suicide}}."
      If nothing in the cue needs marking, underline NOTHING. A front with only
      a bold anchor is finished — an underline is not a quota.
      Underline the cue, never the {{c1::...}} itself.
    • <i>Italicise</i> genus/species names (e.g. <i>Cryptosporidium</i>).
  Purposeful and SPARING — a few words per card, never a whole clause, never the obvious
  filler words. Allowed in the front: inline <b>, <u>, <i> ONLY. NO bullets, lists,
  headers, or <br> in the front.
"""


# THE SOURCE OF TRUTH for how a cloze card is written. The anki-cards skill
# (skills/anki-cards/SKILL.md, mirrored to ~/.claude/skills) restates these rules
# for use outside the add-on — edit here first, then mirror.
#
# Rewritten 2026-10-10. The previous version BANNED direct cues ("X causes
# {{c1::Y}}") and required every card to be cued from a mechanism or a
# presentation. That is a rule that can only be obeyed by adding words, and it
# was: fronts ran ~50% longer than the AnKing cards beside them, a third carried
# an em-dash or "because" clause, and most read as a short story. The default is
# now the AnKing card — one fact, the shortest cue that pins it — and the story
# card is a rationed exception.
CARD_GEN_SYSTEM = """You generate high-yield Anki cloze cards for a Year 3 Australian medical student.
The target style is the AnKing deck: short, direct, one fact per card.


OUTPUT FORMAT
Return ONLY a JSON array. Each element:
  {"front": "<cloze text with {{c1::...}} / {{c2::...}} etc>",
   "extra": "<optional short note, or empty string>",
   "style": "fact" | "story"}
  …plus "one_by_one": true on a one-by-one list card (see LISTS below).
No prose outside the JSON array. No markdown fences.

THE DEFAULT CARD: ONE FACT, THE SHORTEST CUE THAT PINS IT
- Each card tests ONE fact. The visible text is the shortest cue that makes exactly
  one answer correct — nothing more.
- HARD LIMIT: the front is at most """ + str(MAX_FRONT_WORDS) + """ words (cloze answers count; HTML and
  hints don't). Aim for about 15. A front that needs more than """ + str(MAX_FRONT_WORDS) + """ words is
  carrying two facts — split it.
- Direct cues are GOOD. State the fact plainly and cloze the answer:
    GOOD: "<b>Clomiphene</b> {{c1::increases::increases/decreases}} pituitary release of FSH and LH."  (hint needed: "increases" alone could be read as any verb)
    GOOD: "Unopposed <b>oestrogen</b> is the usual cause of {{c1::endometrial hyperplasia}}."
    GOOD: "<b>Kawasaki disease</b> can cause coronary artery {{c1::aneurysms}}."
    GOOD: "<b>Second impact syndrome</b> kills through {{c1::diffuse cerebral swelling}}."
- HINTS ({{c1::answer::hint}}) SPARINGLY — only where the student couldn't otherwise
  tell what the blank wants: {{c1::increased::increased/decreased}},
  {{c1::echocardiogram::which test?}}. If the cue already makes the kind of answer
  obvious, no hint. Most cards have none.

STORY CARDS ARE RATIONED
A "story" card is one cued by a clinical vignette ("A 24-year-old … presents with …")
or a mechanism chain ("X blocks Y, so Z rises, which …"). Mark it "style": "story";
every other card is "style": "fact".
- AT MOST ONE IN FIVE cards may be a story card (fewer than five cards → none, unless
  the topic itself is a clinical scenario). Most facts do not need one.
- Use one only where the fact cannot be tested without the setting — e.g. the
  diagnosis that only a combination of findings points to.
- A story card still obeys the """ + str(MAX_FRONT_WORDS) + """-word limit.

ONE FACT IN THE FRONT
Atomicity is about the WHOLE front, not just how many clozes you used. A one-cloze card
is still non-atomic if the visible text smuggles a second fact in — an em-dash tail, a
parenthetical aside, a "because…" or "and it also…" clause. Every word in the front must
be there to make the ONE hidden fact retrievable. A clause that TEACHES rather than CUES
is cut, or goes in EXTRA.
  BAD:  "In newly diagnosed <b>heart failure</b>, {{c1::transthoracic echocardiography}}
         is the <u>initial</u> test — it quantifies ventricular function and detects
         stenotic or regurgitant lesions that may be a surgically correctable cause."
  GOOD: "The <u>initial</u> test in newly diagnosed <b>heart failure</b> is
         {{c1::transthoracic echocardiography}}."
  BAD:  "<b>Clomiphene</b> blocks hypothalamic oestrogen negative feedback, so GnRH pulses
         rise and the pituitary releases more {{c1::FSH}} — recruiting {{c2::multiple}}
         follicles at once."   (a mechanism chain carrying three facts)
  GOOD: two cards — "<b>Clomiphene</b> blocks oestrogen negative feedback at the
         {{c1::hypothalamus}}." and "<b>Clomiphene</b> raises the risk of
         {{c1::multiple}} pregnancy."
DELETION TEST (run on every card, clause by clause): take out one clause and re-read. If
the cue still forces the SAME answer without it, delete that clause.

GIVE-AWAYS (banned — these let the answer be read off the card)
- SUBSTRING: the cloze answer, or a shared word-stem, appears in the visible text.
    BAD: "Vasospastic angina is caused by coronary {{c1::spasm}}."  ("spasm" sits inside "Vasospastic")
- DEFINITION ↔ TERM: "{{c1::Apoptosis}} is programmed cell death." / "Inflammation of the
  joint is called {{c1::arthritis}}." A dictionary, not a fact. Test something the term
  DOES or CAUSES instead.
- SYNONYM SET-UP: the cue restates the answer in other words, or a second cloze names
  the first.
- GRAMMAR: "an {{c1::…}}" cues a vowel; write "a(n)" or restructure.

CLOZE MECHANICS — what the numbers do
Anki makes ONE card per cloze NUMBER. {{c1::…}} {{c1::…}} is one card with both blanks
hidden together; {{c1::…}} {{c2::…}} is two cards, each hiding one blank while the
other stays visible as part of the cue. Choose deliberately:
- SAME NUMBER (c1 + c1) — the bits only make sense TOGETHER: a pair, a range, both
  halves of a ratio, a direction-plus-thing.
    "<b>Clomiphene</b> raises both {{c1::FSH}} and {{c1::LH}}."
    "Normal adult serum sodium is {{c1::135}}–{{c1::145}} mmol/L."
- DIFFERENT NUMBERS (c1 + c2) — each bit is worth recalling ON ITS OWN, and the other
  bit is a fair cue for it (it doesn't give it away):
    "{{c1::Carbimazole}} inhibits {{c2::thyroid peroxidase}}."  (drug → target, and target → drug)
  Check each sibling: with c1 hidden and c2 shown, is the card still a clean question?
  Then the reverse. If either sibling leaks its answer or makes no sense, don't number
  them separately.
- SEPARATE NOTES — two facts that are related but neither is a fair cue for the other.
  Write two cards.
- Split a multi-part fact into its parts rather than clozing a whole phrase:
    CORRECT: prevents {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}
    WRONG:   prevents {{c1::anterior shear of L5 on S1}}
- At most 2–3 numbers on one note (one-by-one lists below are the exception).

LISTS — ONE-BY-ONE OR SPLIT
For list-shaped content ("the causes of X", criteria, features, steps, a sequence):
- 3 TO 8 ITEMS learnt as a SET → ONE ONE-BY-ONE card. Every item is {{c1::…}} (all the
  same number), items in the order they should be recalled, each item 1–4 words. Add
  "one_by_one": true to the card object. The student reveals the items one at a time,
  so one card holds the whole set without the sibling desync.
    "Causes of <b>pancreatitis</b>: {{c1::gallstones}}, {{c1::alcohol}},
     {{c1::hypertriglyceridaemia}}, {{c1::ERCP}}, {{c1::hypercalcaemia}}."
  The 25-word limit counts only the STEM (the text outside the clozes) on a one-by-one
  card; the stem should be short — a label for the set.
- SPLIT INTO SEPARATE CARDS instead when each item carries its own fact worth testing
  (its own mechanism, its own treatment), or the list is longer than 8.
- 2 items → it is a pair: use the SAME-NUMBER or DIFFERENT-NUMBERS rule above.
- Never number list items c1, c2, c3… — that makes a card per item, each with the rest
  of the list visible as a give-away.
A one-by-one card counts as a "fact" card for the story allowance.

CLOZE FORMAT & STYLE
- Cloze only (no Q&A). Usually ONE cloze. Short answers (1–4 words).
- LEVEL: Year 3 Australian med student; Australian drugs/guidelines (eTG, RACGP, PBS,
  ANZCOR) and spelling.
- Ruthless concision — no filler ("important to know that…", "remember that…"), no
  framing the answer doesn't need.
""" + FRONT_EMPHASIS_RULES + """- Cloze the YIELD (the high-value fact), not background framing.

EXTRA FIELD (the back): OPTIONAL and SHORT. An empty string is the right answer for
most cards.
- At most ~20 words: one or two bolded points, e.g. "<b>Pearl:</b> IM thigh, repeat
  every 5 min". Never a paragraph, never a second lesson, never a repeat of the front.
- Allowed HTML: <b>, <u>, <i>, <br>. Include it only for a high-yield mnemonic,
  distinction or pearl the card genuinely benefits from.

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
  recognition prompt, not a list of sub-parts. One question, one idea. Mark up key words
  INLINE for fast recognition: <b>bold</b> the topic/disease anchor and augmenting
  qualifiers ('first-line', 'most common'); <u>underline</u> what the question is
  specifically asking for (the investigation, the drug class); <i>italicise</i>
  genus/species. Purposeful and sparing — inline <b>/<u>/<i> only, no bullets/headers in
  the question.
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
    "Keep cloze answers short (1–4 words) and each front at most "
    + str(MAX_FRONT_WORDS) + " words — a short, direct statement of its one fact. "
    "Preserve the clinical meaning of the original. Extra is optional and short "
    "(≤ ~20 words, or an empty string). "
    "Return ONLY a JSON array of {front, extra} objects — no prose, no markdown fences.\n\n"
    + FRONT_EMPHASIS_RULES
)


# The deterministic length net — the generation-side twin of the quality pass's
# D4 prefilter. The prompt asks for <= MAX_FRONT_WORDS and mostly gets it; this
# fixes the rest in ONE batched call per job instead of leaving him red chips to
# regenerate by hand. Same idea as ensure_front_emphasis: prompting gets most of
# the way, a cheap check closes the last stretch.
TIGHTEN_SYSTEM = (
    "You shorten Anki cloze cards that came back too long or too story-like. "
    "The target is the AnKing style: one fact per card, the shortest direct cue "
    "that pins exactly one answer.\n"
    "For each card you are given, return replacement card(s):\n"
    "- Each front at most " + str(MAX_FRONT_WORDS) + " words (cloze answers count; "
    "HTML and hints don't). Aim for about 15.\n"
    "- Keep the SAME fact and the same cloze answer(s). Do not add facts.\n"
    "- Cut vignettes, mechanism chains, em-dash tails and 'because…' clauses. "
    "If the card genuinely holds TWO facts, return TWO cards, one each.\n"
    "- A card marked REWRITE AS FACT must come back as a direct statement, not a "
    "clinical vignette.\n"
    "- Never put the cloze answer, or a word sharing its stem, in the visible text.\n"
    "- Extra is optional: at most ~20 words, or an empty string.\n"
    "- A card marked ONE-BY-ONE stays one card and a one-by-one list: every item "
    "stays {{c1::…}}; shorten only the text outside the clozes.\n"
    "- Every returned card carries \"style\": \"fact\".\n\n"
    + FRONT_EMPHASIS_RULES
    + "\nOUTPUT: ONLY a JSON array, one object per input card, in order:\n"
    '[{"index": 0, "cards": [{"front": "...", "extra": "", "style": "fact"}]}]\n'
    "No prose, no markdown fences."
)


def story_allowance(n_cards: int) -> int:
    """How many "story" cards a batch of n may keep: one in five, rounded down.
    Fletcher's rule (2026-10-10) — vignette/mechanism-chain cards have their
    place, "but definitely not every card, probably not even every 5th card"."""
    try:
        return max(0, int(n_cards) // 5)
    except (TypeError, ValueError):
        return 0


def _is_story(card: dict) -> bool:
    return str((card or {}).get("style") or "").strip().lower() == "story"


def cards_needing_tighten(cards: list, max_words: int = MAX_FRONT_WORDS) -> dict:
    """{index: reason} for every card the tighten pass should rewrite: fronts
    over the word limit, plus the story cards past this batch's allowance (the
    LONGEST stories go first — the short ones are the least harmful to keep).
    Pure; no AI. Exposed for the tests and for callers that only want to know."""
    out: dict[int, str] = {}
    if not isinstance(cards, list):
        return out
    for i, c in enumerate(cards):
        if not isinstance(c, dict):
            continue
        n = qp.card_word_count(c)
        if n > max_words:
            out[i] = (f"TOO LONG ({n} words outside the clozes, limit {max_words})"
                      if c.get("one_by_one") else
                      f"TOO LONG ({n} words, limit {max_words})")
    stories = [i for i, c in enumerate(cards) if isinstance(c, dict) and _is_story(c)]
    allowance = story_allowance(len(cards))
    if len(stories) > allowance:
        by_len = sorted(stories, key=lambda i: -qp.front_word_count(cards[i].get("front", "")))
        for i in by_len[:len(stories) - allowance]:
            out[i] = (out[i] + "; " if i in out else "") + "REWRITE AS FACT (too many story cards)"
    return out


# Keys that describe the card's CONTENT and are rewritten by the tighten pass.
# Everything else on a card (point attribution, slide links, images, the
# image_query, a source citation) belongs to where the card came from, and a
# shortened card came from the same place.
_TIGHTEN_CONTENT_KEYS = ("front", "extra", "style", "_verdict")


def tighten_cards(cards: list, profile: dict | None, model: str | None, *,
                  max_words: int = MAX_FRONT_WORDS) -> tuple[list, bool]:
    """Shorten over-long cards, and demote the story cards past the 1-in-5
    allowance, in ONE AI call. Returns (cards, changed).

    Cloze only — a Q&A (Malleus) profile is returned untouched, its answers are
    meant to be complete. Qt-free and safe off the main thread (create_jobs runs
    it in the worker). Fail-open: any error, an unparseable reply, or a card the
    model drops leaves the original in place, and the D4 prefilter then shows a
    still-long card in red rather than silently passing it.
    """
    try:
        if not isinstance(cards, list) or not cards:
            return cards, False
        if str((profile or {}).get("card_format") or "").lower() == "qa":
            return cards, False
        todo = cards_needing_tighten(cards, max_words)
        if not todo:
            return cards, False
        blocks = []
        for i in sorted(todo):
            c = cards[i]
            obo = " — ONE-BY-ONE" if c.get("one_by_one") else ""
            blocks.append(
                f"Card {i}{obo} — {todo[i]}\nFront:\n{c.get('front', '')}\n"
                f"Extra:\n{c.get('extra', '')}")
        prompt = ("Rewrite these cards. Reply with the JSON array described.\n\n"
                  + "\n\n".join(blocks))
        raw = core_api.ask_claude_json(
            prompt=prompt, system=_augment_system(TIGHTEN_SYSTEM, profile),
            max_tokens=max(1024, 400 * len(todo) + 512), model=model,
            show_errors=False)
        if isinstance(raw, dict):
            raw = next((v for v in raw.values() if isinstance(v, list)), None)
        if not isinstance(raw, list):
            return cards, False
        repl: dict[int, list] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            new = [n for n in (item.get("cards") or [])
                   if isinstance(n, dict) and str(n.get("front") or "").strip()]
            if idx in todo and new:
                repl[idx] = new
        if not repl:
            return cards, False
        out: list = []
        for i, c in enumerate(cards):
            if i not in repl:
                out.append(c)
                continue
            carry = {k: v for k, v in c.items() if k not in _TIGHTEN_CONTENT_KEYS}
            for n in repl[i]:
                card = dict(carry)
                card["front"] = str(n.get("front") or "")
                card["extra"] = str(n.get("extra") or "")
                card["style"] = "fact"
                out.append(card)
        return out, True
    except Exception as e:
        print(f"[ankisstant] tighten pass skipped: {e}")
        return cards, False

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
        "Keep the core JSON shape — each card is still {\"front\": ..., \"extra\": ...} "
        "(or an array of them, depending on the original instruction); the 'front' key "
        "always maps to the front field, 'extra' to the extra field. You MAY add the "
        "extra per-card keys other instructions ask for (e.g. \"slide\", \"image_query\")."
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
    "- Keep each cloze answer short (1–4 words), and the text outside the clozes "
    "a short label for the set (at most " + str(MAX_FRONT_WORDS) + " words).\n"
    "- Preserve the original clinical meaning; don't add new facts.\n"
    "- No prose outside the JSON object. No markdown fences.\n\n"
    + FRONT_EMPHASIS_RULES
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


def _gap_group(gap: dict | None) -> str:
    """The group label for a queued gap.

    Queue entries are SNAPSHOTS taken when the gap was queued, so the group may
    have been assigned (or changed) since — hence the store read, with the
    snapshot's own value preferred when it has one. Returns '' for a manually
    typed gap with no KG behind it, which tags exactly as it always did."""
    if not gap:
        return ""
    snap = str(gap.get("group") or "").strip()
    if snap:
        return snap
    kg_id = gap.get("kg_id") or gap.get("id")
    if not kg_id:
        return ""
    try:
        from .kg import store as kg_store
        kg = kg_store.get(str(kg_id))
        return str((kg or {}).get("group") or "").strip()
    except Exception as e:
        print(f"[ankisstant] group lookup failed for {kg_id}: {e}")
        return ""


def _apply_tag_levels(gap: dict, type_meta: dict, levels: dict) -> str:
    """Format {system, subsystem, topic} levels into the consolidated
    {base}::{type}::[group]::… tag, cache it on the gap, and persist it to the
    KG store. Whether to tag at all is the caller's call (the Auto-tag
    checkbox); this only needs the type's name segment. Returns '' if unusable
    (no base/levels)."""
    # The tag BOX is where this gap's cards file: the content tag under the
    # shared base, or — for a group that owns an absolute course branch — that
    # branch, with the classification under it. One or the other, never both;
    # autotag.content_tag_from_levels decides which and is the only thing that
    # should.
    tag = autotag.content_tag_from_levels(levels, group=_gap_group(gap))
    if not tag:
        return ""
    cache_auto_tag(gap, tag)
    return tag


def cache_auto_tag(gap: dict, tag: str) -> None:
    """Remember the content tag this gap's cards actually carry, on the gap and
    in the KG store.

    It has to be the tag that WENT ON, not the one this module would have
    computed: it is what "Retag group" searches by to find a gap's cards when it
    repairs their missing group and source tags. Cached wrong, that repair looks
    for cards under a tag nothing carries and reports that there is nothing to
    fix."""
    tag = str(tag or "").strip()
    if not tag:
        return
    gap["auto_tag"] = tag
    kg_id = gap.get("kg_id") or gap.get("id")
    if not kg_id:
        return
    try:
        from .kg import store as kg_store
        existing = kg_store.get(str(kg_id))
        if existing:
            fields = dict(existing.get("fields") or {})
            fields["auto_tag"] = tag
            kg_store.update(str(kg_id), fields=fields)
    except Exception as e:
        print(f"[ankisstant] auto-tag persist failed: {e}")


def scheme_tags_for_gap(gap: dict | None, levels: dict | None,
                        *, content_tag: str = "") -> list:
    """EVERY tag a card WRITTEN for this gap earns, on all three axes.

    The counterpart of commit.tag_ops_for, which is what a card FOUND for a gap
    goes through. They were never counterparts: the found half applied content +
    course + source + audit + month, and the written half applied the content
    tag and nothing else. So a card the tool wrote for a Curtanki case landed
    outside the case, a card written on a placement had no provenance, and no
    written card anywhere could be found by the month it was made — while the
    card found for the very same gap, in the very same run, on the very same
    screen, got all four.

    autotag.tags_for() has stated the correct policy the whole time and had no
    callers. This is the call site it was missing.

    `content_tag` is the tag already resolved and cached by _apply_tag_levels.
    Passed in rather than recomputed because a gap whose levels came back on an
    earlier run has a cached tag and no levels to rebuild it from.
    """
    from ..core.config import month_tag
    out: list = []
    seen: set = set()

    def add(t) -> None:
        t = str(t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)

    add(content_tag)
    if isinstance(gap, dict):
        try:
            for t in autotag.tags_for(
                    levels, group=_gap_group(gap), tool="knowledge_gaps",
                    kg_type_key=str(gap.get("kg_type") or gap.get("type")
                                    or "").strip().lower()):
                add(t)
        except Exception as e:
            print(f"[ankisstant] scheme tags skipped: {e}")
    # Audit trail. Deduped, so the doors that already put it on lose nothing.
    try:
        add(month_tag())
    except Exception:
        pass
    return out


def _topic_tag_from_levels(levels: dict, type_meta: dict | None,
                           group: str = "") -> str:
    """Build a {base}::{type}::[group]::… hierarchical tag from classification
    levels for a NO-GAP generation (plain topic/source, or pasted cards). Uses
    the given KG type's segment, falling back to the default type. Returns ''
    when unusable (no base, no levels, or it collapses to just the base). Unlike
    _apply_tag_levels this has no gap to cache on, so it's a pure format. Thin
    wrapper over the shared autotag formatter so Create and Browse agree.

    `group` is normally '' here — a no-gap generation has no KG and so no group.
    Mass AI Create passes one when a whole batch is being written into a group."""
    return autotag.content_tag_from_levels(levels, group=group)


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
            bits.append(autotag.clip(stem, 1500))
        if (gap.get("notes") or "").strip():
            bits.append(autotag.clip(gap["notes"].strip(), 1500))
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
        "slide_field":        cfg.get("slide_field", ""),
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
            log.warn(
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
        f"split across clozes that share a core). Prefer fewer. A list is never "
        f"c1, c2, c3… — it is a one-by-one card (all c1) or separate cards."
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


def _breadth_directive(n: int, mode: str, *, band: str | None = None) -> str:
    """Scale card depth and breadth to the requested count. The same topic yields
    very different card sets at N=1 vs N=50: one card should be the single
    highest-yield fact (a quick, broad-strokes card); fifty should fan out across
    the topic's subtopics AND drill into mechanism and detail. Folded into every
    generation request. `mode` is 'topic' or 'source' — in source mode breadth is
    bounded by the supplied material (extract more/deeper from it) rather than
    ranging over the whole subject.

    `band` overrides the count-derived depth. Normally depth IS a function of the
    count, which is right when the count is all we know. Mass AI Create knows
    more: its dial carries a depth judgement of its own (a 12-card "wide" gap and
    a 12-card "deep" gap want the same number of cards at different depths), so
    it selects the band directly. One of "single", "essentials", "mechanism",
    "comprehensive"; anything else falls back to the count.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    source_bound = (mode == "source")
    where = "the source" if source_bound else "this topic"
    if band not in ("single", "essentials", "mechanism", "comprehensive"):
        band = ("single" if n <= 2 else "essentials" if n <= 8
                else "mechanism" if n <= 20 else "comprehensive")
    if band == "single":
        scope = (
            f"only {n} card(s) requested — make the SINGLE highest-yield, most "
            f"exam-critical card on {where}: the one thing you would test if you "
            "could test only one. Broad strokes, no minutiae or secondary detail."
        )
    elif band == "essentials":
        scope = (
            f"a small set — cover the core high-yield facts of {where} that a "
            "student must know first. Stay on the essentials; skip secondary detail."
        )
    elif band == "mechanism":
        scope = (
            f"a medium set — cover the main concepts across {where}: key "
            "mechanisms, presentations and management, not just headline facts. "
            "Depth means MORE single-fact cards (a mechanism is its own short "
            "card), never longer cards."
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
                "detail — as more single-fact cards, never longer ones."
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


# Per-slide generation: one AI call per rendered page, so each card's source
# slide is known by CONSTRUCTION (we set it ourselves) rather than guessed by
# the model. This sidesteps the JSON-shape lock that suppressed the old "slide"
# key, and lets the model focus on a single slide's worth of content at a time.
_SLIDE_PER_PAGE_USER = (
    "The attached image is ONE slide from a lecture deck (slide {n} of {total}).\n"
    "Generate the high-yield {card_word} cards THIS slide warrants — typically 0–4. "
    "Use ONLY what is on this slide; do not invent content from other slides.\n"
    "Return an EMPTY array [] for a slide that carries no testable content "
    "(title, learning-objectives, outline, references, acknowledgements, "
    "thank-you, or a purely decorative image).\n"
    "Output ONLY a JSON array of card objects — no prose, no markdown fences."
)


def _coerce_card_list(reply) -> list:
    """Normalise an AI reply into a list of card dicts. Accepts a bare array, a
    {"cards": [...]} object, or a single {"front","extra"} object."""
    if isinstance(reply, list):
        return [c for c in reply if isinstance(c, dict)]
    if isinstance(reply, dict):
        if isinstance(reply.get("cards"), list):
            return [c for c in reply["cards"] if isinstance(c, dict)]
        if "front" in reply:
            return [reply]
    return []


def generate_cards_per_slide(slide_pages: list, *, system: str, card_word: str,
                             model, skill_id: str = "", skill_invocation: str = "",
                             cancel_check=None) -> list:
    """Generate cards one rendered slide at a time. Qt-free, so it runs in the
    background worker thread. Each call sends a single page image; every returned
    card is tagged deterministically with its source slide (`slide`, 1-based) and
    `_slide_indices` so the review screen auto-attaches that slide with no model
    cooperation. Returns the concatenated card list (slides yielding [] are
    skipped). A truthy `cancel_check()` stops the loop between slides."""
    out: list = []
    total = len(slide_pages)
    for i, page in enumerate(slide_pages):
        if cancel_check is not None and cancel_check():
            break
        try:
            reply = core_api.ask_claude_json(
                prompt=_SLIDE_PER_PAGE_USER.format(n=i + 1, total=total, card_word=card_word),
                system=system, max_tokens=2048, model=model,
                skill_id=skill_id, skill_invocation=skill_invocation,
                attachments=[page], show_errors=False,
            )
        except Exception as e:
            log.error(f"slide {i + 1} generation failed: {e}")
            continue
        for card in _coerce_card_list(reply):
            if "front" not in card:
                continue
            card["slide"] = i + 1
            card["_slide_indices"] = [i]
            out.append(card)
    return out


# ── text-first slide matching ─────────────────────────────────────────────────
#
# The cheap lecture path: generate cards from the deck's extracted TEXT in one
# call (no vision), then attach each card's source slide by matching the card's
# words back to each page's text. Deterministic — no AI — so it runs free in the
# background worker. A card that doesn't confidently match any slide is left
# imageless (no slide key) rather than mis-attached.
_SLIDE_MATCH_STOPWORDS = frozenset(
    "the a an and or of to in is are be with for on as by from that this these "
    "those it its which who whom whose what when where why how than then thus can "
    "may will would should could into out over under more most less least also "
    "such not no any all both each per via due between within without your you".split()
)
# Attribution leans on the ABSOLUTE number of distinct content words a card shares
# with a page (a good cloze card adds its answer/mechanism, which inflates the
# card's own vocabulary and unfairly deflates a pure fraction), plus a clear
# margin over the runner-up page so an ambiguous card is left imageless rather
# than mis-attached. The fraction is only a light floor against long, thin cards.
_SLIDE_MATCH_MIN_HITS = 3       # ≥ this many distinct shared words with the best page
_SLIDE_MATCH_MARGIN = 2         # …and beat the 2nd-best page by ≥ this many words
_SLIDE_MATCH_MIN_SCORE = 0.25   # …and share ≥ this fraction of the card's own words


def _significant_words(text: str) -> set:
    """Lower-cased alphabetic words ≥4 chars that aren't stop-words. Cloze and
    HTML markup are stripped first so only the card's real content is scored."""
    text = re.sub(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", r"\1", str(text), flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    words = re.findall(r"[a-z][a-z'\-]{3,}", text.lower())
    return {w for w in words if w not in _SLIDE_MATCH_STOPWORDS}


def match_cards_to_slides(cards: list, page_texts: list) -> None:
    """Attach each card to the slide it most likely came from by word-overlap
    between the card and each page's extracted text. Sets `card["slide"]`
    (1-based) and `card["_slide_indices"]` in place; the review dialog's
    _sync_slide_images then attaches that page's image with no further work.
    Cards with no confident match are left imageless. Qt-free — safe in the
    background worker. Never raises."""
    try:
        page_words = [_significant_words(t or "") for t in page_texts]
    except Exception as e:
        log.error(f"match_cards_to_slides: page tokenise failed: {e}")
        return
    for card in cards:
        if not isinstance(card, dict) or "front" not in card:
            continue
        try:
            cw = _significant_words(
                str(card.get("front", "")) + " " + str(card.get("extra", "")))
            if not cw:
                continue
            best_i, best_hits, second_hits = -1, 0, 0
            for i, pw in enumerate(page_words):
                hits = len(cw & pw)
                if hits > best_hits:
                    best_hits, second_hits, best_i = hits, best_hits, i
                elif hits > second_hits:
                    second_hits = hits
            if (best_i >= 0
                    and best_hits >= _SLIDE_MATCH_MIN_HITS
                    and (best_hits - second_hits) >= _SLIDE_MATCH_MARGIN
                    and best_hits / len(cw) >= _SLIDE_MATCH_MIN_SCORE):
                card["slide"] = best_i + 1
                card["_slide_indices"] = [best_i]
        except Exception as e:
            log.error(f"match_cards_to_slides: card skipped: {e}")
            continue


def _grader_model(cfg: dict) -> str | None:
    """The grader model for the active family — the dedicated quality_pass model
    if set, else the generator model, else the family default (resolved later)."""
    fam = active_family()
    return pipeline.dial_for(pipeline.CHECK, family=fam)


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
    sid = (qpc.get("grader_skill_id") or "").strip()
    # The CLI runs every call with --tools "", so a skill named in the prompt
    # can never be loaded there — the invocation was dead text the model
    # answered with "I couldn't access that skill". The inline rubric (always
    # sent, see quality_pass.skill_batch_system) is the grader on the CLI; only
    # an uploaded API skill_id still rides along. (grader_skill_invocation is
    # left in config, unread, so his saved settings aren't disturbed.)
    if provider == "cli":
        return ("", "")
    return ("", sid)


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
            "Rewrite this card as a single, better card on the same fact — "
            "shorter and more direct (at most " + str(MAX_FRONT_WORDS) + " words "
            "in the front), keeping the same retrieval target."
        )
    # A rewrite prompt hands the model a finished card and one thing to fix, and
    # the reply comes back tuned to that one thing — in practice, as plain text.
    # The FRONT EMPHASIS rules are in the system prompt, but a local instruction
    # out-pulls them, so the local instruction has to carry the rule too.
    instruction += (
        "\nThe rewrite is a NEW card and must be formatted like one: bold the "
        "topic/disease anchor in the front, underline only what already earns it. "
        "Coming back as plain text is a failed rewrite, however good the wording."
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
    # A rewrite inherits the anchor the original already chose; if the original
    # had none either, the focus text is the only topic this call knows about.
    ensure_front_emphasis([new], *bolded_terms(old_card.get("front", "")), focus)
    if old_card.get("_image_paths"):
        new["_image_paths"] = list(old_card["_image_paths"])
    # Carry the slide attachment across a regeneration — the rewritten card is
    # the same fact from the same slide. Likewise the learning point it was
    # written for, so a regenerated card stays filed under its point.
    for k in ("slide", "_slide_indices", "_slide_paths", "point", "one_by_one"):
        if old_card.get(k) is not None:
            new[k] = list(old_card[k]) if isinstance(old_card[k], list) else old_card[k]
    return new


# ── front emphasis: the deterministic net ────────────────────────────────────
#
# The prompt says to bold the topic anchor on EVERY card and the model complies
# most of the time — measured over 165 generated notes, roughly one front in
# seven came through with no <b> at all, and the misses are scattered inside
# single batches rather than clustered on one broken path. So it is model
# non-compliance, not a route that lost the rule, and no amount of extra
# prompting closes the last stretch.
#
# What closes it is that we already KNOW the anchor. Every generation carries
# the thing it was written about — the topic label, the learning point, the
# gap's title — so when a front comes back unmarked we can put the bold where
# it belongs without asking anyone. Deterministic, free, off the main thread:
# the same shape as the slide-matching pass above.
#
# Conservative by construction. It bolds ONE run of text, only on an exact
# whole-word match against a known anchor, never inside a cloze and never
# inside markup. When nothing matches it changes nothing and says so, and the
# review row labels the card in words — a card is never silently mangled to
# satisfy a style rule.

_ANCHOR_STOPWORDS = frozenset(
    "the a an an the and or of to in is are be with for on as by from that this these those "
    "it its which who what when where why how than then thus can may will would should "
    "could into out over under more most less least also such not no any all both each "
    "per via due between within without".split()
)
# Fine as PART of an anchor ("Down syndrome", "coeliac disease"), useless on
# their own — a front bolding the bare word "condition" has learned nothing.
# Applied only to single-word candidates, so the phrases keep working.
_ANCHOR_GENERIC = frozenset(
    "condition conditions disease diseases disorder disorders syndrome syndromes "
    "patient patients therapy therapies treatment treatments management diagnosis "
    "screening symptom symptoms sign signs feature features risk score scoring "
    "test tests testing drug drugs agent agents cause causes effect effects "
    "presentation investigation investigations complication complications "
    "mechanism mechanisms indication indications".split()
)
_ANCHOR_MIN_WORD = 5      # a shorter word is too likely to be incidental
_ANCHOR_MAX_PHRASE = 4    # a longer phrase is a sentence, not an anchor


def _anchor_candidates(*sources: str) -> list[str]:
    """Phrases worth bolding as a card's anchor, best first.

    Sources are ranked in the order given — the first is the most authoritative
    thing the card is about (the learning point, then the topic label, then the
    gap's title), and a weak match on a strong source beats a strong match on a
    weak one. Within one source: every run of 2–4 adjacent content words
    (longest first), then the acronym, then the bare words.

    Phrases lead because a card about heart failure should bold "heart
    failure", not whichever single word happens to be longest — and a phrase
    can only match if those words are adjacent in the front, so a nonsense
    n-gram costs nothing. The acronym comes next because a front that says "AF"
    almost never also says "atrial". Single words are last and are held to a
    length floor and a generic-noun ban, since that is where a bad bold comes
    from."""
    out: list[str] = []
    seen: set[str] = set()

    def take(c: str) -> None:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)

    for raw in sources:
        text = anki_utils.strip_html(str(raw or "")).strip()
        if not text:
            continue
        phrases: list[str] = []
        acronyms: list[str] = []
        words: list[str] = []
        # Clause by clause, so an n-gram never straddles a comma and invents a
        # phrase ("sex called") that was never in the source.
        for clause in re.split(r"[,;:.()\[\]/\u2014\u2013]+", text):
            toks = [t for t in re.split(r"[^A-Za-z0-9\-]+", clause) if t]
            content = [t for t in toks if t.lower() not in _ANCHOR_STOPWORDS]
            if not content:
                continue
            for n in range(min(_ANCHOR_MAX_PHRASE, len(content)), 1, -1):
                for i in range(len(content) - n + 1):
                    phrases.append(" ".join(content[i:i + n]))
            if len(content) >= 2:
                ac = "".join(t[0] for t in content).upper()
                if 2 <= len(ac) <= 5:
                    acronyms.append(ac)
            for t in content:
                if t.lower() in _ANCHOR_GENERIC:
                    continue
                if len(t) >= _ANCHOR_MIN_WORD or t.isupper():
                    words.append(t)
        phrases.sort(key=len, reverse=True)
        words.sort(key=len, reverse=True)
        for c in phrases + acronyms + words:
            take(c)
    return out


def _plain_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) ranges of `text` that are ordinary readable words —
    everything outside an HTML tag and outside a {{c1::…}} cloze.

    Both exclusions matter for different reasons: bolding inside a tag would
    corrupt the HTML, and bolding inside a cloze would mark the answer, which
    is the one thing FRONT EMPHASIS explicitly forbids."""
    blocked: list[tuple[int, int]] = []
    for m in re.finditer(r"<[^>]*>", text):
        blocked.append(m.span())
    depth, start = 0, None
    i = 0
    while i < len(text) - 1:
        if text[i:i + 2] == "{{":
            if depth == 0:
                start = i
            depth += 1
            i += 2
            continue
        if text[i:i + 2] == "}}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blocked.append((start, i + 2))
                start = None
            i += 2
            continue
        i += 1
    if depth and start is not None:          # unclosed cloze: treat the tail as blocked
        blocked.append((start, len(text)))
    blocked.sort()
    spans: list[tuple[int, int]] = []
    cur = 0
    for b0, b1 in blocked:
        if b0 > cur:
            spans.append((cur, b0))
        cur = max(cur, b1)
    if cur < len(text):
        spans.append((cur, len(text)))
    return spans


def bold_anchor(front: str, anchors: list[str]) -> str | None:
    """Bold the first anchor that appears as plain text in `front`.

    Returns the repaired front, or None when the front already has a bold or no
    anchor could be found — in both cases the caller leaves the card alone."""
    if not front or "<b>" in front.lower():
        return None
    spans = _plain_spans(front)
    for anchor in anchors:
        # An acronym must match case ("AF"), a word or phrase need not
        # ("heart failure" / "Heart failure"). Otherwise every "It" in a front
        # is a candidate anchor for a gap whose title starts "Intracranial…".
        flags = 0 if (anchor.isupper() and len(anchor) <= 5) else re.IGNORECASE
        pat = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in anchor.split())
                         + r"\b", flags)
        for s0, s1 in spans:
            m = pat.search(front, s0, s1)
            if m is None:
                continue
            return front[:m.start()] + "<b>" + m.group(0) + "</b>" + front[m.end():]
    return None


def bolded_terms(front: str) -> list[str]:
    """The text already bolded in a front — the anchor its author chose.

    The best anchor source for a REWRITE: split, one-by-one and regenerate all
    start from a card that usually got its emphasis right, so the rewrite can
    inherit the same anchor instead of guessing at one."""
    return [anki_utils.strip_html(m).strip()
            for m in re.findall(r"<b>(.*?)</b>", str(front or ""), re.S | re.I)
            if anki_utils.strip_html(m).strip()]


def ensure_front_emphasis(cards: list, *sources: str, per_card=None) -> int:
    """Give every unbolded front in `cards` its anchor back. Returns how many
    were repaired; the ones that couldn't be are marked `_no_emphasis` so the
    review row can say so in words rather than letting them through unnoticed.

    `per_card(card) -> iterable[str]` supplies sources that outrank the shared
    ones for that card alone — a batch covering eight learning points should
    bold each card's OWN point, not whichever point happened to come first.

    Qt-free and collection-free, so it runs on the worker thread beside the
    generation it is cleaning up after. Never raises: an unformatted card is a
    small annoyance, a create that dies on the way to the review screen is not.
    """
    if not isinstance(cards, list):
        return 0
    fixed = 0
    try:
        base = _anchor_candidates(*sources)
        for card in cards:
            if not isinstance(card, dict):
                continue
            front = str(card.get("front") or "")
            if not front or "<b>" in front.lower():
                card.pop("_no_emphasis", None)
                continue
            anchors = base
            if per_card is not None:
                try:
                    own = _anchor_candidates(*(per_card(card) or ()))
                except Exception:
                    own = []
                if own:
                    seen = {a.lower() for a in own}
                    anchors = own + [a for a in base if a.lower() not in seen]
            repaired = bold_anchor(front, anchors) if anchors else None
            if repaired is not None:
                card["front"] = repaired
                card.pop("_no_emphasis", None)
                fixed += 1
            else:
                card["_no_emphasis"] = True
        if fixed:
            log.info(f"front emphasis: bolded the anchor on {fixed} card(s) "
                     f"the model left unmarked")
    except Exception as e:
        log.error(f"front emphasis repair skipped: {e}")
    return fixed


def ensure_front_emphasis_for_job(cards: list, rec: dict) -> int:
    """`ensure_front_emphasis` for a create_jobs record, which is where the
    anchors actually live: each card's own attributed learning point first
    (card["point"] is a 1-based index into the job's mass_points), then the
    job's topic label and title as the shared fallback."""
    points = [str(c or "") for c in ((rec or {}).get("mass_points") or [])]

    def own(card: dict):
        try:
            n = int(card.get("point"))
        except (TypeError, ValueError):
            return ()
        return (points[n - 1],) if 1 <= n <= len(points) else ()

    return ensure_front_emphasis(
        cards, str((rec or {}).get("topic_label") or ""),
        str((rec or {}).get("title") or ""), *points, per_card=own)


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


def _flatten_transparency_enabled() -> bool:
    try:
        icfg = (tool_config("card_creator").get("images") or {})
        return bool(icfg.get("flatten_transparency", True))
    except Exception:
        return True


def _flatten_image_onto_white(img):
    """Composite an image with alpha onto opaque white. Returns the original on
    any failure — a flatten that doesn't work must not lose the image."""
    try:
        from aqt.qt import QPainter
        out = QImage(img.size(), QImage.Format.Format_RGB32)
        out.fill(Qt.GlobalColor.white)
        p = QPainter(out)
        try:
            p.drawImage(0, 0, img)
        finally:
            p.end()
        return out if not out.isNull() else img
    except Exception as e:
        print(f"[ankisstant] image flatten failed: {e}")
        return img


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
        # Transparency is a correctness problem, not just a size one: a see-through
        # image shows the note background through it, so in night mode dark line-art
        # (rendered slides, most Wikipedia diagrams) becomes unreadable. Flatten it
        # onto white even when the file is otherwise small enough to pass through.
        flatten = _flatten_transparency_enabled() and img.hasAlphaChannel()
        if longest <= _IMG_MAX_DIM and size <= _IMG_REENCODE_BYTES and not flatten:
            return path  # already small enough; keep original bytes/quality
        if longest > _IMG_MAX_DIM:
            img = img.scaled(
                _IMG_MAX_DIM, _IMG_MAX_DIM,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        if flatten:
            img = _flatten_image_onto_white(img)
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


# Display size caps. _normalize_image_file keeps the STORED file up to
# _IMG_MAX_DIM so it stays sharp when zoomed; these cap how big it RENDERS on the
# card. Without them a 1600px figure fills the whole review screen. max-width /
# max-height (rather than a width= attribute) caps both axes, preserves aspect
# ratio, and never upscales an image that's already small.
_CARD_IMG_MAX_W = 400    # px — figures attached to a card
_CARD_IMG_MAX_H = 560
_SLIDE_IMG_MAX_W = 700   # px — whole rendered slides need more room to stay legible
_SLIDE_IMG_MAX_H = 560


def _img_display_cfg() -> tuple[int, int, int, int]:
    """(card_w, card_h, slide_w, slide_h) from config, falling back to the
    defaults above. Fail-open — a bad config never blocks card creation."""
    try:
        icfg = (tool_config("card_creator").get("images") or {})
        return (
            int(icfg.get("card_max_w", _CARD_IMG_MAX_W)),
            int(icfg.get("card_max_h", _CARD_IMG_MAX_H)),
            int(icfg.get("slide_max_w", _SLIDE_IMG_MAX_W)),
            int(icfg.get("slide_max_h", _SLIDE_IMG_MAX_H)),
        )
    except Exception:
        return (_CARD_IMG_MAX_W, _CARD_IMG_MAX_H, _SLIDE_IMG_MAX_W, _SLIDE_IMG_MAX_H)


def img_tag(fname: str, max_w: int, max_h: int) -> str:
    """One <img> tag for a filename already in Anki media, capped to a display
    box. The single place the add-on writes an <img>, so the cap can't be
    bypassed by one call site forgetting it."""
    style = f"max-width:{max_w}px; max-height:{max_h}px;"
    return f'<img src="{html.escape(fname, quote=True)}" style="{style}">'


def _img_tags_for(paths: list[str], *, slide: bool = False) -> str:
    """Import a list of image paths into the media folder and return an
    HTML fragment of <img> tags (one per line) to append to a field."""
    card_w, card_h, slide_w, slide_h = _img_display_cfg()
    max_w, max_h = (slide_w, slide_h) if slide else (card_w, card_h)
    bits: list[str] = []
    for p in paths or []:
        fname = _import_image_to_media(p)
        if not fname:
            continue
        bits.append(img_tag(fname, max_w, max_h))
    return "<br>".join(bits)


def _resolve_slide_field(profile: dict, field_names) -> str:
    """Field that source-slide images go into: the profile's `slide_field` if set
    and present on the notetype, else AnKing's 'Lecture Notes' field if it exists,
    else the extra field (so nothing breaks on notetypes without a slides field)."""
    fields = set(field_names or [])
    configured = (profile.get("slide_field") or "").strip()
    if configured and configured in fields:
        return configured
    if "Lecture Notes" in fields:
        return "Lecture Notes"
    return profile.get("extra_field", "Extra")


def _append_field(note, field: str, html_frag: str) -> None:
    """Append an HTML fragment to a note field (<br>-joined if non-empty)."""
    if not html_frag or field not in note:
        return
    existing = note[field] or ""
    note[field] = (existing + "<br>" if existing else "") + html_frag


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
            dup.setStyleSheet(f"color: {synapse.status_warn()}; font-size: 11px;")
            dup.setWordWrap(True)
            v.addWidget(dup)

        self.front_lbl = QLabel(card.get("front", ""))
        self.front_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.front_lbl.setWordWrap(True)
        v.addWidget(self.front_lbl)

        # Inline thumbnail of the source slide attached to this card (slide-deck
        # PDFs). Click to open the gallery and swap slides. Hidden when none.
        self.slide_thumb = QPushButton()
        self.slide_thumb.setFlat(True)
        self.slide_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slide_thumb.setIconSize(QSize(200, 150))
        self.slide_thumb.setToolTip("Source slide for this card — click to change.")
        self.slide_thumb.clicked.connect(self._on_pick_slides)
        self.slide_thumb.setVisible(False)
        v.addWidget(self.slide_thumb, alignment=Qt.AlignmentFlag.AlignLeft)

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
        self._refresh_slide_thumb()
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

    def _refresh_slide_thumb(self):
        """Show the first attached slide image inline (loaded from the local PNG),
        or hide the thumbnail when this card has no slide attached."""
        thumb = getattr(self, "slide_thumb", None)
        if thumb is None:
            return
        paths = self.card.get("_slide_paths") or []
        if not paths:
            thumb.setIcon(QIcon())
            thumb.setVisible(False)
            return
        try:
            pix = QPixmap(paths[0])
            if not pix.isNull():
                thumb.setIcon(QIcon(pix))
                thumb.setVisible(True)
                return
        except Exception:
            pass
        thumb.setVisible(False)

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
        theme_dialog(self)

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


# ── committing cards to the collection (shared) ───────────────────────────────
#
# Writing a card into Anki used to live entirely inside ReviewDialog, reading a
# dozen `self` attributes as it went. That was fine while exactly one screen ever
# committed anything. Mass AI Create commits a batch spanning several gaps in one
# checkpoint — same notetype and (usually) deck, but per-gap tags and per-gap KG
# content — so the field-writing had to stop being a method on the screen that
# happens to be showing.
#
# CommitCtx is that dozen attributes named once. ReviewDialog builds one on
# demand (so nothing goes stale if a field is reassigned mid-review) and mass
# create builds one per gap group.

@dataclass
class CommitCtx:
    """Everything needed to turn a generated card dict into an Anki note."""
    profile: dict
    tags: list                                  # applied to every note
    mode: str = "topic"
    source_label: str | None = None
    panel_image_paths: list = field(default_factory=list)
    # KG-attached content. For MQ-type gaps these compose the Missed Questions
    # field; for every other type only the images are used.
    kg_image_filenames: list = field(default_factory=list)
    kg_type_for_image: str = ""
    kg_stem_html: str = ""
    kg_notes: str = ""
    kg_concept: str = ""
    kg_explanation: str = ""
    # The gap's own reading list — [{"label", "url"}]. Rendered onto Extra so a
    # card knows where its answer came from and where to go back to.
    kg_resources: list = field(default_factory=list)
    # The KG type definition + this gap's per-note field values, for user-defined
    # fields whose declarative target is a literal Anki field (kind="field").
    type_meta: dict | None = None
    kg_field_values: dict = field(default_factory=dict)


def enrich_extra(extra: str, ctx: CommitCtx) -> str:
    """Optionally stamp the source label onto Extra. Off by default: the label is
    whatever the file was called, and a journal's PII filename on the back of
    every card is noise. Turn on with card_creator.cite_source."""
    out = extra or ""
    if not bool(tool_config("card_creator").get("cite_source", False)):
        return out
    if ctx.mode == "source" and ctx.source_label:
        cite = f"Source: {ctx.source_label}"
        if cite not in out:
            out = (out + "<br>" if out else "") + cite
    return out


def kg_image_html(ctx: CommitCtx) -> str:
    """<img> HTML for KG-attached images (filenames already in media)."""
    card_w, card_h, _sw, _sh = _img_display_cfg()
    return "<br>".join(img_tag(f, card_w, card_h) for f in ctx.kg_image_filenames if f)


def kg_content_html(ctx: CommitCtx) -> str:
    """Full HTML to append to the KG target field — for MQ-type KGs this leads
    with the specific knowledge gap (concept) and an AI-written explanation of
    it, then the captured question stem and any freeform notes above the
    screenshot(s). For non-MQ KGs it's just the images. "" if nothing to add."""
    parts: list[str] = []
    if ctx.kg_type_for_image == "mq":
        if ctx.kg_concept:
            parts.append(f"<b>Knowledge gap:</b> {html.escape(ctx.kg_concept)}")
        if ctx.kg_explanation:
            parts.append("<i>" + html.escape(ctx.kg_explanation).replace("\n", "<br>") + "</i>")
        if ctx.kg_stem_html.strip():
            parts.append(ctx.kg_stem_html.strip())
        if ctx.kg_notes.strip():
            # Notes are plain-text; escape for safety.
            parts.append(html.escape(ctx.kg_notes.strip()).replace("\n", "<br>"))
    img_html = kg_image_html(ctx)
    if img_html:
        parts.append(img_html)
    return "<br>".join(parts)


def kg_resources_html(ctx: CommitCtx) -> str:
    """The gap's resources as a small labelled block of links, for Extra.

    A resource on a gap is the thing that closed it — the chapter, the video,
    the guideline. It belongs on the card for the same reason the gap's notes
    do: months later the card is the only surviving trace of where the answer
    came from, and "where did I read this" is the question you actually ask.

    Bare labels (no URL) are kept as plain text; a URL with no label shows the
    URL. Off with card_creator.attach_resources = false."""
    if not bool(tool_config("card_creator").get("attach_resources", True)):
        return ""
    rows: list[str] = []
    for r in (ctx.kg_resources or []):
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or "").strip()
        url   = str(r.get("url") or "").strip()
        if url:
            safe = html.escape(url, quote=True)
            rows.append(f'<a href="{safe}">{html.escape(label or url)}</a>')
        elif label:
            rows.append(html.escape(label))
    if not rows:
        return ""
    return ("<div class='ankisstant-resources'><b>Resources:</b> "
            + " · ".join(rows) + "</div>")


def kg_image_target_field(ctx: CommitCtx) -> str:
    """Field that KG-attached content should land in. MQ-type KGs route to the
    qbank Missed Questions field; everything else uses Extra."""
    if ctx.kg_type_for_image == "mq":
        try:
            return tool_config("qbank").get("missed_q_field") or "Missed Questions"
        except Exception:
            return "Missed Questions"
    return ctx.profile.get("extra_field", "Extra")


def images_for(card: dict, ctx: CommitCtx) -> str:
    """<img>-tag fragment for the image field: panel-level images plus per-card
    images attached during review, EXCLUDING slide images (those route to the
    slide field via slide_imgs_for). Files are copied into Anki media here (NOT
    at generation time) so only what actually gets created is imported."""
    slide_paths = set(card.get("_slide_paths") or [])
    paths: list[str] = list(ctx.panel_image_paths)
    paths.extend(p for p in (card.get("_image_paths") or []) if p not in slide_paths)
    return _img_tags_for(paths) if paths else ""


def slide_imgs_for(card: dict) -> str:
    """<img> fragment for the card's source-slide image(s). Empty when none."""
    paths = card.get("_slide_paths") or []
    return _img_tags_for(list(paths), slide=True) if paths else ""


def set_fields(note, card: dict, ctx: CommitCtx) -> None:
    """Write one generated card into a fresh note. The single definition — both
    the single-gap review screen and mass create go through here."""
    profile       = ctx.profile
    front_field   = profile.get("front_field", "Text")
    extra_field   = profile.get("extra_field", "Extra")
    image_field   = profile.get("image_field", extra_field)
    slide_field   = _resolve_slide_field(profile, note.keys())
    sources_field = (profile.get("sources_field") or "").strip()
    obo_field     = profile.get("one_by_one_field", "One by one")
    try:
        note[front_field] = card.get("front", "")
    except KeyError:
        note.fields[0] = card.get("front", "")
    if extra_field in note:
        note[extra_field] = enrich_extra(card.get("extra", ""), ctx)
    # Non-slide images go into image_field (often == extra_field, in which case
    # we append; if it's a separate field, that field is set directly).
    img_html = images_for(card, ctx)
    if img_html and image_field in note:
        if image_field == extra_field:
            _append_field(note, image_field, img_html)
        else:
            note[image_field] = img_html
    # Source-slide image(s) go into the slide field (AnKing 'Lecture Notes' by
    # default), separate from the medical/searched images above.
    _append_field(note, slide_field, slide_imgs_for(card))
    source_html = (card.get("source") or "").strip()
    if source_html and sources_field and sources_field in note:
        if sources_field == extra_field:
            existing = note[sources_field] or ""
            note[sources_field] = (existing + "<br>" if existing else "") + source_html
        else:
            note[sources_field] = source_html
    # KG-attached content: for MQ-type KGs the captured question stem + freeform
    # notes + screenshot(s), routed to Missed Questions. Non-MQ KGs just
    # contribute their images to Extra (or image_field).
    kg_content = kg_content_html(ctx)
    if kg_content:
        target = kg_image_target_field(ctx)
        if target and target in note:
            existing = note[target] or ""
            note[target] = (existing + "<br>" if existing else "") + kg_content
    if card.get("one_by_one") and obo_field in note:
        note[obo_field] = "y"
    # User-defined fields whose declarative target is a literal Anki field
    # (kind="field"). Built-in role targets are handled above; this only adds the
    # custom field→field routing, so default types are unaffected.
    if ctx.type_meta and ctx.kg_field_values:
        try:
            qb_cfg = tool_config("qbank")
            custom = engine.note_field_plan(
                ctx.type_meta, profile, qb_cfg,
                lambda k: ctx.kg_field_values.get(k, ""), only_kinds=("field",))
            for fname, val in custom.items():
                if val and fname in note:
                    existing = note[fname] or ""
                    note[fname] = (existing + "<br>" if existing else "") + val
        except Exception as e:
            log.warn(f"custom field routing skipped: {e}")
    # The gap's reading list, last, on Extra — regardless of KG type. An MQ
    # gap's captured question belongs in Missed Questions, but a link to the
    # chapter that answers it belongs with the answer.
    _append_field(note, extra_field, kg_resources_html(ctx))
    for t in ctx.tags:
        note.add_tag(t)


def one_by_one_rewrite(old_card: dict, profile: dict) -> dict | None:
    """Rewrite a card as one note with sibling {{c1}} clozes for AnKing's
    'One by one' reveal. None if the model didn't return a usable card."""
    prompt = (
        "Original card front:\n" + old_card.get("front", "") + "\n\n"
        "Original card extra:\n" + old_card.get("extra", "") + "\n\n"
        "Rewrite as ONE card with multiple sibling {{c1::...}} clozes so AnKing's "
        "'One by one' addon reveals each in sequence."
    )
    skill_invocation, skill_id = _resolve_skill(profile)
    new = core_api.ask_claude_json(
        prompt=prompt,
        system=_augment_system(ONE_BY_ONE_SYSTEM, profile),
        max_tokens=1024, model=pipeline.dial_for(pipeline.WRITE),
        skill_id=skill_id, skill_invocation=skill_invocation,
    )
    if not isinstance(new, dict) or "front" not in new or "extra" not in new:
        return None
    front = new["front"]
    if re.search(r"\{\{c[2-9]\d*::", front):
        new["front"] = re.sub(r"\{\{c\d+::", "{{c1::", front)
    ensure_front_emphasis([new], *bolded_terms(old_card.get("front", "")))
    if old_card.get("_image_paths"):
        new["_image_paths"] = list(old_card["_image_paths"])
    # The flag that makes set_fields write 'y' to the One by one field. Set
    # HERE, not by the caller: AI Create's row used to add it after the call,
    # but Work through and Mass Create call this too and never did — their
    # rewritten card committed as plain sibling clozes with the field empty.
    new["one_by_one"] = True
    for k in ("slide", "_slide_indices", "_slide_paths", "point"):
        if old_card.get(k) is not None:
            new[k] = old_card[k]
    return new


def split_card(card: dict, profile: dict) -> list | None:
    """Break one card into the smallest set of atomic single-cloze cards. None
    when the model didn't return a usable list (the caller leaves the row be)."""
    prompt = (
        "Original card front:\n" + card.get("front", "") + "\n\n"
        "Original card extra:\n" + card.get("extra", "") + "\n\n"
        "Split this card into the smallest number of ATOMIC single-cloze cards "
        "that preserve all the testable facts. Each output card must have exactly "
        "one {{c1::...}} cloze."
    )
    skill_invocation, skill_id = _resolve_skill(profile)
    new_cards = core_api.ask_claude_json(
        prompt=prompt,
        system=_augment_system(SPLIT_SYSTEM, profile),
        max_tokens=2048, model=pipeline.dial_for(pipeline.WRITE),
        skill_id=skill_id, skill_invocation=skill_invocation,
    )
    if (not isinstance(new_cards, list) or not new_cards
            or not all(isinstance(c, dict) and "front" in c and "extra" in c
                       for c in new_cards)):
        return None
    # Every piece of a split is about the same thing the original was, so they
    # all inherit its anchor.
    ensure_front_emphasis(new_cards, *bolded_terms(card.get("front", "")))
    return new_cards


def dup_check(card: dict, front_field: str) -> str | None:
    """A warning string when the collection already has a note whose front field
    contains this card's opening text, else None."""
    plain = anki_utils.strip_cloze(anki_utils.strip_html(card.get("front", "")))[:50].strip()
    if len(plain) < 12:
        return None
    safe = plain.replace("\\", "\\\\").replace('"', '\\"')
    try:
        nids = mw.col.find_notes(f'"{front_field}:*{safe}*"')
    except Exception:
        return None
    if nids:
        return f"Possible duplicate — {len(nids)} note(s) already contain '{plain[:40]}…'"
    return None


def resolve_notetype(profile: dict, *, warn: bool = True):
    """The Anki notetype for a profile, or None with an explanatory warning."""
    name = (profile.get("name") or "").strip()
    if not name:
        if warn:
            showWarning("No notetype configured.\n\n"
                        "Open Ankisstant Settings → AI Create and add a notetype profile.")
        return None
    nt = mw.col.models.by_name(name)
    if nt is None:
        if warn:
            showWarning(f"Notetype not found: {name}\n\n"
                        "Edit or remove this notetype profile under Settings → AI Create.")
        return None
    front_field = (profile.get("front_field") or "Text").strip()
    field_names = {f["name"] for f in nt.get("flds", [])}
    if front_field and front_field not in field_names:
        if warn:
            showWarning(
                f"Notetype '{name}' has no field named '{front_field}'.\n\n"
                f"Available fields: {', '.join(sorted(field_names)) or '(none)'}\n\n"
                "Update the profile's 'Front field' under Settings → AI Create.")
        return None
    # Cloze sanity check — non-cloze notetypes silently swallow {{c1::…}}.
    if nt.get("type", 0) != 1:
        log.warn(f"notetype {name!r} is not a cloze type — generated cards may not "
                 "render as expected.")
    return nt


def commit_cards(cards: list, *, ctx: CommitCtx, deck_name: str, notetype) -> list:
    """Add cards to the collection. Returns the created note ids.

    Deliberately does NOT checkpoint, reset, confirm or log — the caller owns all
    four, because a batch commit wants ONE checkpoint and ONE reset around many
    calls to this, not one of each per gap.
    """
    deck_id = mw.col.decks.id(deck_name)
    created = []
    for card in cards:
        note = mw.col.new_note(notetype)
        set_fields(note, card, ctx)
        mw.col.add_note(note, deck_id)
        created.append(note.id)
    return created


# ── persistent Add-Screen queue (survives ReviewDialog dismissal) ────────────

_add_queue: list = []
_add_queue_ctx: dict | None = None
_add_dialog = None  # the AddCards instance we're driving
_hook_attached = False
# Who is being driven, and what they want told back. The queue predates having
# a second caller: AI Create starts a run and never asks what came of it,
# because the panel it started from is the only place the answer would show.
# The work-through pass does ask — it has gaps to close, and a gap closes on a
# card reaching the collection, not on one being offered. So the run now
# reports each add as it happens and says when it is over.
_add_on_added = None    # callable(index, note) — which queued entry was added
_add_on_done = None     # callable(discarded) — the run ended, however it ended
_add_idx = 0            # index of the entry currently loaded in the editor
# Whether that loaded entry is still unsent. Needed because _add_queue holds
# what is WAITING, not what is on screen: with one card in the editor and
# three behind it the queue is three long, so closing the window there was
# reported as discarding three cards when it discarded four.
_add_pending = False


def _fill_addcards(ac, card, ctx: dict) -> None:
    # A FILLER, not a card dict: a callable that builds and sets its own note.
    # Everything below reads one shared `ctx` — one notetype, one deck, one set
    # of tags, one gap's content — which is true of a Create panel batch and
    # false of a work-through pass, where each card belongs to a different gap.
    # Rather than teach ctx to vary per card, a caller in that position hands
    # over the filling.
    if callable(card):
        card(ac)
        return
    nt = ctx["nt"]
    new_note = mw.col.new_note(nt)
    front_field = ctx["front_field"]
    extra_field = ctx["extra_field"]
    image_field = ctx.get("image_field", extra_field)
    slide_field = ctx.get("slide_field", extra_field)
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
            _append_field(new_note, image_field, img_html)
        else:
            new_note[image_field] = img_html
    slide_html = ctx["slide_imgs_for"](card) if ctx.get("slide_imgs_for") else ""
    _append_field(new_note, slide_field, slide_html)
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


def _start_add_queue(cards: list, ctx: dict, *, on_added=None,
                     on_done=None) -> None:
    """Drive Anki's Add window through a list of cards, one at a time.

    `cards` is normally card dicts, described by `ctx`. It may instead be
    fillers — callables that set the window's note themselves — for a caller
    whose cards can't share one ctx; `ctx` then only needs "nt" and "deck_id"
    for the initial selection.

    `on_added(index, note)` fires each time the user presses Add, naming which
    queued entry it was. `on_done(discarded)` fires once when the run ends, by
    the last card or by the window being closed. Between them a caller can tell
    what actually reached the collection, which is not the same as what was
    offered — closing the window early is a normal way to stop."""
    global _add_queue, _add_queue_ctx, _add_dialog
    global _add_on_added, _add_on_done, _add_idx, _add_pending
    if not cards:
        return
    # A run already in flight is ended first, and told so. Two queues sharing
    # one Add window would interleave their cards and each credit the other's
    # adds to itself.
    if _add_queue or _add_on_done is not None:
        _stop_add_queue()
    _add_on_added = on_added
    _add_on_done = on_done
    _add_idx = 0
    mw.col.models.set_current(ctx["nt"])
    mw.col.decks.select(ctx["deck_id"])

    from aqt import dialogs
    ac = dialogs.open("AddCards", mw)
    _add_dialog = ac
    _add_queue = list(cards[1:])
    _add_queue_ctx = ctx

    _fill_addcards(ac, cards[0], ctx)
    _add_pending = True

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

    # Hooked whenever anyone is listening, not only when more cards follow: a
    # single-card run still has to report that its one card was added.
    if _add_queue or on_added is not None or on_done is not None:
        _attach_add_hook()
        try:
            if not getattr(ac, "_ankisstant_finished_hooked", False):
                ac.finished.connect(_on_add_dialog_finished)
                ac._ankisstant_finished_hooked = True
        except Exception as e:
            print(f"[ankisstant] couldn't hook AddCards.finished: {e}")
    if _add_queue:
        tooltip(
            f"{len(_add_queue) + 1} card(s) queued. Click Anki's Add button — "
            f"the next will load automatically. Close the window to discard the rest."
        )
    else:
        tooltip("Card sent to Add Cards. Review then click Add.")


def _advance_add_queue():
    global _add_queue, _add_queue_ctx, _add_dialog, _add_idx, _add_pending
    if not _add_queue or _add_queue_ctx is None or _add_dialog is None:
        _stop_add_queue()
        return
    ac = _add_dialog
    next_card = _add_queue.pop(0)
    _add_idx += 1
    _fill_addcards(ac, next_card, _add_queue_ctx)
    _add_pending = True
    remaining = len(_add_queue)
    if remaining:
        tooltip(f"{remaining} card(s) left in queue.")
    else:
        tooltip("Last queued card loaded. Click Add to finish.")


def _on_add_did_add_note(note):
    global _add_pending
    _add_pending = False
    # Say WHICH entry that was before the queue moves on — after the advance,
    # _add_idx already names the next one.
    if _add_on_added is not None:
        try:
            _add_on_added(_add_idx, note)
        except Exception as e:
            print(f"[ankisstant] add-queue listener failed: {e}")
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
    global _add_on_added, _add_on_done, _add_idx, _add_pending
    # The one in the editor counts: it was offered and never added.
    discarded = len(_add_queue) + (1 if _add_pending else 0)
    if discarded:
        tooltip(f"Discarded {discarded} unsent card(s) from the queue.")
    done = _add_on_done
    _add_queue = []
    _add_queue_ctx = None
    _add_dialog = None
    _add_on_added = None
    _add_on_done = None
    _add_idx = 0
    _add_pending = False
    if _hook_attached:
        try:
            gui_hooks.add_cards_did_add_note.remove(_on_add_did_add_note)
        except ValueError:
            pass
        _hook_attached = False
    # Last, and after the globals are already clear: the listener is entitled
    # to start a run of its own from in here, and would otherwise have it torn
    # down by the tail of the one it was told about.
    if done is not None:
        try:
            done(discarded)
        except Exception as e:
            print(f"[ankisstant] add-queue completion listener failed: {e}")


# ── Tool contract ────────────────────────────────────────────────────────────

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
