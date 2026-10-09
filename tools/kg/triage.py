# Knowledge gaps → learning points, scope-aware.
#
# The bridge that lets a batch of KGs run through the AI Lecture pipeline
# (tools/lecture/matcher.py) instead of being worked one at a time through AI
# Browse. Reading the selection returns, per KG:
#
#   concept     a cleaned, self-contained statement of the gap
#   scope       narrow | focused | broad          (how much ground it covers)
#   cards       how many existing cards should surface for it
#   points      the gap DECOMPOSED into searchable learning points
#   confidence  high | low, with a note when the KG is too vague to read
#
# WHY SCOPE IS THE WHOLE POINT OF THIS MODULE.
# A KG is a note-to-self, not a syllabus line, and its breadth varies wildly:
#
#   "Graves + Tx"                        → a whole disease AND its management
#   "carbimazole"                        → a whole drug (MOA, uses, adverse effects)
#   "DOACS first line for AF long term"  → one assertion, one card's worth
#   "Monoamines???"                      → not readable; ask the user
#
# Handing all four the same card budget is wrong in both directions — it starves
# Graves and pads the DOACs one with tangents. So breadth is expressed TWICE,
# and deliberately so:
#
#   1. DECOMPOSITION — a broad KG mints several points, each with its own search
#      terms and its own candidate pool. This is what actually finds more cards:
#      one point can only ever match cards about one thing.
#   2. THE PER-KG CAP — `cards` is divided across that KG's points and handed to
#      matcher.allocate(cap_for=...), so a broad gap is allowed to keep a deeper
#      cluster per point than a narrow one.
#
# Decomposition alone under-delivers (each point still capped at the flat depth
# preset); a bigger cap alone just deepens ONE pool of candidates about ONE
# facet. Together they give the shape we want.
#
# PURE-ish: AI calls only, no Qt, no mw.col. Safe on a worker thread, which is
# what lets run_pipeline call it exactly where LO mode calls the extractor. It
# spawns its own worker threads for the decompose fan-out — see "the call"
# below for why, and for the measurements that forced it.

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from ...core import api as core_api, log
from ..lecture.extractor import ExtractionError, LearningPoint, _clean_level, _clean_terms
from . import read_cache


# ── scope vocabulary ─────────────────────────────────────────────────────────
#
# Three buckets, not five: the model can tell "one claim" from "one aspect of
# one entity" from "the whole entity" reliably, and finer grading than that is
# noise the user then has to correct in the table anyway.
#
# `points` is how many learning points the KG may decompose into, `cards` the
# default number of existing cards to surface. Both are DEFAULTS the model may
# move within the clamps below, and the user edits freely in the review table —
# they exist so a low-confidence or unparsed reply still lands somewhere sane.
#
# The card numbers were cut (narrow 2→1, focused 5→4, broad 14→10) after the
# first real batch: a gap like "DOACs are first line for AF" or "define
# persecutory delusions" is ONE fact, and asking for two cards just drags in a
# near-miss he then has to untick. One assertion, one card — if a second card
# genuinely tests the same claim the dedupe pass would have culled it anyway.
SCOPES: dict[str, dict] = {
    "narrow":  {"points": (1, 1), "cards": 1,
                "label": "Narrow",  "hint": "one assertion — one card"},
    "focused": {"points": (2, 5), "cards": 5,
                "label": "Focused", "hint": "one entity, one aspect"},
    "broad":   {"points": (6, 12), "cards": 12,
                "label": "Broad",   "hint": "a whole entity, or entity + management"},
}
DEFAULT_SCOPE = "focused"

# Hard clamps on the per-KG card target, whatever the model or a stale cache
# says. The floor guarantees a KG is never allocated out of existence; the
# ceiling stops one runaway "broad" reading from eating a whole batch's budget.
MIN_CARDS, MAX_CARDS = 1, 40


# ── breadth vocabulary (Mass AI Create) ──────────────────────────────────────
#
# The generation-side twin of SCOPES. Same idea — breadth expressed as points
# plus a per-KG card budget — but a DELIBERATELY DIFFERENT vocabulary, because
# "narrow/focused/broad" already means "how many EXISTING cards to surface" in
# the dialog next door and both are open in the same session. Same words with
# two meanings is the kind of thing you only notice after acting on the wrong one.
#
# Underneath there are two dials, not one:
#   COVERAGE — how many points the gap decomposes into (how much ground)
#   DEPTH    — how deep each card cuts, via `band` → _breadth_directive
# The four presets walk a sensible diagonal through that square rather than
# collapsing it: wide→deep holds coverage still and moves depth; pinpoint→core
# does the reverse. That's what earns the single front-end dial.
#
# The card numbers are NOT SCOPES' numbers. Those were deliberately cut to be
# stingy, because an extra surfaced card is one the student reads and rejects.
# Generating is the other way round: an extra card costs one untick in the review
# list, while a missing card leaves the gap open. So these are more generous.
CREATE_BREADTHS: dict[str, dict] = {
    "pinpoint": {"points": (1, 1), "cards": 1,  "band": "single",
                 "label": "Pinpoint", "hint": "one fact — one card"},
    "core":     {"points": (3, 6), "cards": 6,  "band": "essentials",
                 "label": "Core",     "hint": "one aspect, the must-knows"},
    "wide":     {"points": (8, 14), "cards": 14, "band": "essentials",
                 "label": "Wide",     "hint": "the whole entity, headline level"},
    "deep":     {"points": (12, 20), "cards": 22, "band": "mechanism",
                 "label": "Deep",     "hint": "the whole entity, down to mechanism"},
}
DEFAULT_BREADTH = "core"

# Soft ceiling on a whole mass-create batch. Not a hard block — a big deliberate
# run is legitimate — but past this the dialog turns the budget red and asks for
# confirmation, because 12 gaps set to "deep" is 264 cards and a lot of tokens.
MAX_BATCH_CARDS = 80

# The depth half of the dial, as passed to card_creator._breadth_directive(band=).
# Kept as plain strings rather than an enum so a job record round-trips through
# JSON unchanged.
VALID_BANDS = ("single", "essentials", "mechanism", "comprehensive")


def scope_meta(scope: str) -> dict:
    return SCOPES.get((scope or "").strip().lower(), SCOPES[DEFAULT_SCOPE])


def breadth_meta(breadth: str) -> dict:
    return CREATE_BREADTHS.get((breadth or "").strip().lower(),
                               CREATE_BREADTHS[DEFAULT_BREADTH])


def clamp_cards(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return SCOPES[DEFAULT_SCOPE]["cards"]
    return max(MIN_CARDS, min(MAX_CARDS, n))


# ── the plan for one KG ──────────────────────────────────────────────────────

@dataclass
class KGPlan:
    """One knowledge gap, read and decomposed. `points` carry kg_id == this
    plan's kg_id, which is how the panel reassembles per-KG results after the
    pipeline has flattened everything into one point list."""
    kg_id: str
    title: str                       # the KG's raw title, untouched
    kg_type: str = "kg"              # the KG's type key — the tag's type segment
    # The KG's group label ("SRS 4"), carried so every tag this plan produces
    # lands in the group's branch. Raw label, not a segment — autotag.group_segment
    # does the normalising, in one place.
    group: str = ""
    concept: str = ""                # the cleaned reading of it
    scope: str = DEFAULT_SCOPE
    cards: int = 0                   # per-KG card target (user-editable)
    # The generation-side dial. Only meaningful on plans built by triage_create;
    # search plans carry the defaults and never read them. `breadth` is what the
    # user picks in the mass-create table; `depth_band` is the half of it that
    # reaches the prompt, and the model may nudge it independently (a "wide" gap
    # it judges mechanism-heavy comes back as wide + mechanism).
    breadth: str = DEFAULT_BREADTH
    depth_band: str = "essentials"
    points: list[LearningPoint] = field(default_factory=list)
    confidence: str = "high"
    note: str = ""                   # why it's low-confidence / what's ambiguous
    # Directives the student wrote AT the AI rather than facts to learn ("do not
    # focus on dx"). Verbatim, because they're passed straight through to the
    # generation prompt — see gen_prompt._constraints_block. A constraint that
    # only reaches triage is one that shapes the points and is then forgotten by
    # the model that actually writes the cards.
    constraints: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # the KG's own curated tags
    # Set by the review table: unticked rows are carried (so the user can see
    # what they skipped) but never searched.
    included: bool = True

    @property
    def ok(self) -> bool:
        """Runnable: something was understood and it produced searchable points."""
        return bool(self.points)

    @property
    def per_point_cap(self) -> int:
        """The card target spread across this KG's points, for allocate(cap_for).

        Ceiling division, floored at 1: a 5-card focused KG over 2 points gives
        3 each (the allocator's round-robin and the total budget stop it
        actually spending 6), and a 1-card narrow KG still gets its one card.
        """
        n = max(1, len(self.points))
        return max(1, -(-clamp_cards(self.cards) // n))


# ── prompt ───────────────────────────────────────────────────────────────────
#
# Deliberately built on the SAME term rules as the LO extractor and AI Browse —
# a term that returns nothing is the single biggest cause of a false "gap", and
# those rules are the hard-won fix. What's new here is the scope judgement and
# the licence to read shorthand, which no other prompt in the addon needs.
#
# WHY THE PROMPT IS IN NAMED FRAGMENTS.
# Reading a batch used to be ONE call that both SIZED every gap and DECOMPOSED
# every gap. Sizing is cheap to write down — a scope word and a card number,
# maybe fifty tokens a gap — and it is the ONLY part that needs to see the whole
# batch at once, because the levels are calibrated against each other.
# Decomposition is the expensive part (a sentence, three tag levels and 2-4
# search terms per point) and it needs no cross-batch view whatever: a gap's
# points depend on that gap and its assigned size, nothing else.
#
# Fused, those two facts fight: the batch-wide call has to be one process, so
# every gap's decomposition queues behind every other gap's. Measured on 33 real
# gaps: 8 of them took 168s and the full 33 was still generating at 9m47s
# against a 300s timeout. There is no economy of scale to lose by splitting —
# the system prompt is 6KB and the answer is 37KB, so batching only ever saved
# on the cheap half.
#
# So the fragments below compose FOUR prompts, not two:
#   _TRIAGE_SYSTEM / _CREATE_SYSTEM   the original fused prompts, byte for byte,
#                                     still used for a batch small enough that
#                                     two round trips would cost more than they
#                                     save (see _SPLIT_MIN)
#   _SORT_* / _DECOMP_*               the split pair: one batch-wide sizing call,
#                                     then N small decomposition calls in
#                                     parallel, each told its gaps' decided size
#
# test_triage_split.py asserts the two rebuilt originals are byte-identical to
# what they were before the split, which is the whole safety story here.

# The licence to read a note-to-self. Shared verbatim by all four prompts: how
# to READ the student's shorthand is the one thing that never varies.
_SHORTHAND = (
    "THESE ARE NOT POLISHED LEARNING OBJECTIVES. Expect shorthand, typos, "
    "abbreviations, fragments, trailing markup ('- [ ] Baclofen'), question "
    "marks, and arrows meaning 'leads to'. Read through all of that to the "
    "clinical intent. 'Lugols iodine is K and I mixed. can be sued for wolff "
    "chaikoff' means Lugol's iodine (potassium iodide) and the Wolff-Chaikoff "
    "effect — 'sued' is a typo for 'used'.\n\n"
)


# STEP 1, the sizing judgement, and the ONLY fragment that needs the whole
# batch in front of it: 'the broadest gap on the list' is a comparison.
_S_SCOPE = (
    "STEP 1 — JUDGE THE SCOPE. This is the most important decision you make, "
    "because it sets how much of the deck gets surfaced for this gap:\n"
    "  \"narrow\"  — ONE specific assertion, fact, or relationship. The student "
    "has written the answer down; they want the card that tests it. Examples: "
    "'DOACs first line for AF long term', 'Dexamethasone useful in prevention of "
    "thyroid storm', '10g ethanol per standard drink'.\n"
    "  \"focused\" — ONE entity and ONE aspect of it, or a small comparison. "
    "Examples: 'thyroid storm symptoms', 'THC vs CBD', 'triptans block release "
    "of inflammatory mediators'.\n"
    "  \"broad\"   — a WHOLE entity with no aspect named, or an entity plus its "
    "management. A bare drug or disease name is ALWAYS broad: the student means "
    "the lot. Examples: 'Graves + Tx' (pathogenesis, antibodies, clinical "
    "features, investigations, thionamides, radioiodine, surgery), 'carbimazole' "
    "(mechanism, indications, dosing, agranulocytosis, pregnancy), 'Baclofen'.\n"
    "Do NOT let a short input read as narrow. 'carbimazole' is two syllables and "
    "the broadest gap on the list; 'Dexamethasone useful in prevention of thyroid "
    "storm' is a long sentence and one single fact. LENGTH IS NOT SCOPE — the "
    "number of distinct things a student must learn is.\n\n"

)


# A POINT IS ONE FACT. Shared by both decompositions, because a point is what
# a card gets written FROM: work-through writes one card per uncovered SEARCH
# point (bulk_search.create_record_for), Mass AI Create from the create points.
# Compound points ("X does A, and additionally B") were turning straight into
# compound cards — the long, two-fact fronts this rule exists to stop. Splitting
# them means more points per gap, so the point ranges in SCOPES and
# CREATE_BREADTHS were raised alongside (2026-10-10).
_ONE_FACT_POINT = (
    "A POINT IS ONE FACT — the single thing one cloze would hide. If your "
    "sentence contains 'and', 'additionally', 'while', 'whereas', a list, or a "
    "second clause that could be its own card, it is TWO points: split it. "
    "Keep each point short — a plain sentence of about 15 words or fewer. "
    "Many small points are right; a few big ones are the failure.\n"
)


_S_DECOMPOSE = (
    "STEP 2 — DECOMPOSE TO MATCH THE SCOPE. Split the gap into learning points, "
    "each ONE fact a single card could test:\n"
    "  narrow  → exactly 1 point. Do not expand it. The student wrote one fact; "
    "finding cards on the surrounding topic is noise they did not ask for.\n"
    "  focused → 2 to 5 points.\n"
    "  broad   → 6 to 12 points that between them cover the entity properly: for "
    "a disease, its mechanism, features, diagnosis and management; for a drug, "
    "its mechanism, indications, key adverse effects and monitoring. Only mint a "
    "point a well-taught student would actually be examined on.\n"
    + _ONE_FACT_POINT + "\n"

)


_S_CARDS = (
    "STEP 3 — SET A CARD TARGET. \"cards\" is how many existing deck cards "
    "should surface for the WHOLE gap. BE STINGY — every card past the point "
    "the student needs is one they have to read and reject:\n"
    "  narrow  → 1. One assertion is one card. Use 2 ONLY if the fact has two "
    "genuinely separate halves that no single card could test together.\n"
    "  focused → 4-6.\n"
    "  broad   → 10-16, and only at the top of that range for a large disease "
    "with a real management arm.\n"
    "Scale it to how much there genuinely is to know, not to a formula.\n\n"

)


_S_TERMS = (
    "STEP 4 — WRITE SEARCH TERMS for every point.\n"
    "HOW ANKI SEARCH WORKS (critical — most failures come from ignoring this):\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. So a 3-4 word term like 'Graves disease radioactive iodine' almost "
    "always returns ZERO. Keep terms to the entity anchor PLUS ONE discriminating "
    "keyword — usually two words ('Graves radioiodine', 'Graves TSI').\n"
    "TERM LENGTH IS A HARD CONSTRAINT:\n"
    "- TWO words is the target and the default.\n"
    "- THREE words only when those exact words plainly co-occur on one card.\n"
    "- FOUR OR MORE is always wrong. Rewrite by REPLACING a word, never "
    "appending: 'long term anticoagulation atrial fibrillation' → 'AF "
    "anticoagulation'.\n"
    "ANCHOR ON THE FULL NAME THE CARD USES, NOT THE ABBREVIATION. Anki matches "
    "literal tokens and cards spell entities out: anchor on 'atrial fibrillation' "
    "not 'AF', 'Parkinson' not 'PD'. A short disease abbreviation may appear ONLY "
    "as an EXTRA term, never the sole anchor. Keep a token verbatim only where "
    "decks print it that way (IL-17, TNF, HLA-B27, HIV, TSH, DOAC).\n"
    "USE THE DECK'S VOCABULARY, NOT PROSE. Cards are terse exam shorthand — "
    "reach the concept through the classic drug, the named sign, the key "
    "mediator ('Graves pretibial', 'carbimazole agranulocytosis'), never through "
    "'pathogenesis' or 'management', which rarely appear on cards.\n\n"

)


_S_TAGGING = (
    "TAGGING — classify each point into {\"system\", \"subsystem\", \"topic\"}. "
    "PascalCase or snake_case, no spaces, no '::'. TOPIC IS THE SPECIFIC "
    "CLINICAL ENTITY — the drug, disease or structure — because it searches the "
    "tag tree on its own. A cross-cutting theme is NEVER a topic: 'mechanism', "
    "'treatment', 'adverse_effects' are ASPECTS. Put the entity in topic and let "
    "the aspect live in the terms. Leave a level empty rather than guessing, and "
    "never use 'General', 'Misc' or 'Other'.\n\n"

)


_S_CONFIDENCE = (
    "CONFIDENCE — set \"confidence\" to \"low\" and explain in \"note\" when a "
    "gap is genuinely unreadable ('Monoamines???' — which monoamines, and what "
    "about them?). STILL return your best-guess points for it: the student "
    "reviews and edits everything before it runs, so a rough guess they can fix "
    "beats an empty row. Reserve low confidence for real ambiguity, not for "
    "shorthand you can decode.\n\n"

)


# The generation-side sizing judgement. It says CALIBRATE ACROSS THE BATCH in
# so many words — which is exactly why sizing stayed one whole-batch call when
# decomposition was cut loose from it.
_C_BREADTH = (
    "STEP 1 — JUDGE THE BREADTH. This is the most important decision you make, "
    "because it sets how much gets written:\n"
    "  \"pinpoint\" — ONE specific assertion, fact, or relationship. The student "
    "has written the answer down; they want the card that tests it. Examples: "
    "'DOACs first line for AF long term', 'Dexamethasone useful in prevention of "
    "thyroid storm', '10g ethanol per standard drink'.\n"
    "  \"core\"     — ONE entity and ONE aspect of it, or a small comparison. "
    "Examples: 'thyroid storm symptoms', 'THC vs CBD', 'triptans block release "
    "of inflammatory mediators'.\n"
    "  \"wide\"     — a WHOLE entity with no aspect named, or an entity plus its "
    "management, at headline level. A bare drug or disease name is ALWAYS at "
    "least wide: the student means the lot. Examples: 'Graves + Tx', "
    "'carbimazole', 'Baclofen'.\n"
    "  \"deep\"     — the same ground as wide, but where the entity is one a "
    "student is examined on MECHANISTICALLY, so the cards must go past the "
    "headlines into mechanisms, distinctions and the less-common but testable "
    "detail.\n"
    "WIDE IS THE DEFAULT FOR A WHOLE ENTITY; DEEP IS THE EXCEPTION. Deep is for "
    "the single mechanism-heavy entity a student is drilled on — a receptor "
    "cascade, an acid-base derangement, a drug's pharmacodynamics. Two tests, "
    "and it must pass BOTH: (a) the gap names ONE entity, not a class or a "
    "clinical approach, and (b) knowing the headlines would still leave the "
    "student unable to answer a 'why' question about it.\n"
    "A GAP THAT NAMES A CLASS IS WIDE, NEVER DEEP. 'Modalities of dialysis', "
    "'types of shock', 'the vasculitides', 'causes of X' need each member "
    "covered ONCE and properly. Going deep on a class spends the whole budget "
    "mechanistically dissecting the first member and leaves the rest unwritten "
    "— which is a worse outcome than any amount of missing depth.\n"
    "A GAP FRAMED AS AN APPROACH IS WIDE, NEVER DEEP. 'Approach to X', "
    "'workup of Y', 'investigation of Z' asks for the decision pathway — the "
    "steps, the branch points and what each result rules in or out. It is not "
    "an invitation to write mechanism cards on every condition the pathway "
    "mentions.\n"
    "CALIBRATE ACROSS THE BATCH. These four levels only mean anything relative "
    "to each other. If you are about to return the same breadth for most of the "
    "gaps you were given, you are miscalibrated — go back and rank them against "
    "each other, then assign. A batch where everything is deep is a batch you "
    "have not actually read.\n"
    "Do NOT let a short input read as pinpoint. 'carbimazole' is two syllables "
    "and the broadest gap on the list; 'Dexamethasone useful in prevention of "
    "thyroid storm' is a long sentence and one single fact. LENGTH IS NOT "
    "BREADTH — how much a student would have to be TAUGHT is.\n\n"

)


_C_CONSTRAINTS = (
    "STEP 1b — OBEY THE STUDENT'S OWN INSTRUCTIONS. A gap often carries a "
    "directive aimed at you rather than a fact: 'do not focus on dx', 'just the "
    "mechanism', 'skip the pharm', 'high yield only', 'not the rare causes'. "
    "These are NOT topic hints to weigh up — they are constraints, and they "
    "OVERRIDE every default in this prompt, including the breadth bands and the "
    "enumerate-the-class rule. If they wrote 'do not focus on dx', then no point "
    "you emit may be about diagnosing anything, however central diagnosis "
    "normally is to that entity.\n"
    "Copy each one, in the student's own words, into the \"constraints\" array "
    "on that gap. They are passed on verbatim to the model that writes the "
    "cards, so a constraint you drop here is a constraint that is silently "
    "ignored twice. Return [] when the gap carries none.\n\n"

)


_C_DECOMPOSE = (
    "STEP 2 — DECOMPOSE INTO WRITEABLE POINTS. Split the gap into learning "
    "points, each ONE fact that ONE short card could test:\n"
    "  pinpoint → exactly 1 point. Do not expand it. The student wrote one fact; "
    "cards on the surrounding topic are noise they did not ask for.\n"
    "  core     → 3 to 6 points.\n"
    "  wide     → 8 to 14 points that between them cover the entity properly: for "
    "a disease, its mechanism, features, diagnosis and management; for a drug, "
    "its mechanism, indications, key adverse effects and monitoring.\n"
    "  deep     → 12 to 20 points, chosen so the set reaches mechanism and "
    "distinction, not just more headlines.\n"
    "A POINT IS A TESTABLE FACT, NOT A SEARCH QUERY OR A HEADING. "
    "'Graves: management' is a heading; 'Thionamides are first-line medical "
    "therapy in Graves' is a point, and 'carbimazole is preferred over PTU "
    "outside the first trimester' is a SECOND point.\n"
    + _ONE_FACT_POINT +
    "POINTS MUST NOT OVERLAP. Two points covering the same claim produce two "
    "near-identical cards, and — unlike a search, where the same card simply "
    "matches twice and dedupes — nothing culls a duplicate that was newly "
    "written. Each point must be a claim no other point on this gap makes.\n"
    "ENUMERATE THE MEMBERS OF A NAMED CLASS. When the gap names a CLASS whose "
    "members a student must know INDIVIDUALLY, emit one point PER MEMBER — not "
    "one point for the class. 'Graves + Tx' has thionamides, radioiodine and "
    "surgery as separate management points, not one 'treatment of Graves'. The "
    "same applies to the individual cranial-nerve palsies, the four "
    "hypersensitivity types, the shock subtypes, the named murmurs, the "
    "vasculitides. A single 'types of X' point quietly covering only its loudest "
    "member is the exact failure this prevents. Expand a class only when its "
    "members are each independently examinable — this does NOT license splitting "
    "a genuine single fact.\n"
    "MECHANISM OF ACTION IS ALWAYS ITS OWN POINT. For any drug or drug class, "
    "how it works is a SEPARATE point from its clinical use, its adverse effects "
    "and its indications — the mechanism card is a distinct, high-yield, "
    "examiner-favourite card. Do not fold 'thionamides inhibit thyroid "
    "peroxidase' into a general 'thionamides are first-line' point.\n\n"

)


_C_CARDS = (
    "STEP 3 — SET A CARD BUDGET. \"cards\" is how many NEW cards to write for "
    "the WHOLE gap. Roughly ONE per point — each point is a single fact:\n"
    "  pinpoint → 1. One assertion is one card.\n"
    "  core     → 4-7.\n"
    "  wide     → 10-16.\n"
    "  deep     → 16-26.\n"
    "The student reviews every card before any of it is added, so an extra card "
    "is cheap and a point left uncovered is a gap that stays open. But the "
    "number is A CEILING TO AIM AT, NOT A QUOTA TO FILL: a point that a single "
    "card fully tests gets a single card, and a gap with genuinely little in it "
    "gets a small number whatever its breadth bucket says. Padding to reach the "
    "bucket's number is worse than coming in under it.\n\n"

)


_C_TERMS = (
    "STEP 4 — ALSO WRITE SEARCH TERMS for every point. These do NOT affect what "
    "gets written; they are used only to check whether the student's deck "
    "already covers a point, so it can be skipped. Write them anyway, and write "
    "them well:\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. So a 3-4 word term like 'Graves disease radioactive iodine' almost "
    "always returns ZERO. Keep terms to the entity anchor PLUS ONE discriminating "
    "keyword — usually two words ('Graves radioiodine', 'Graves TSI'). THREE "
    "words only when those exact words plainly co-occur on one card; FOUR OR MORE "
    "is always wrong. Anchor on the FULL name a card would use, not the "
    "abbreviation ('atrial fibrillation' not 'AF'), except where decks print the "
    "token that way (IL-17, TNF, HLA-B27, HIV, TSH, DOAC). Reach the concept "
    "through the classic drug, named sign or key mediator, never through "
    "'pathogenesis' or 'management', which rarely appear on cards.\n\n"

)


_C_TAGGING = (
    "TAGGING — classify each point into {\"system\", \"subsystem\", \"topic\"}. "
    "PascalCase or snake_case, no spaces, no '::'. TOPIC IS THE SPECIFIC "
    "CLINICAL ENTITY — the drug, disease or structure. A cross-cutting theme is "
    "NEVER a topic: 'mechanism', 'treatment', 'adverse_effects' are ASPECTS. Put "
    "the entity in topic. Leave a level empty rather than guessing, and never use "
    "'General', 'Misc' or 'Other'.\n\n"

)


_C_CONFIDENCE = (
    "CONFIDENCE — set \"confidence\" to \"low\" and explain in \"note\" when a "
    "gap is genuinely unreadable ('Monoamines???' — which monoamines, and what "
    "about them?). STILL return your best-guess points: the student reviews and "
    "edits everything before anything is generated, so a rough guess they can fix "
    "beats an empty row. Reserve low confidence for real ambiguity, not for "
    "shorthand you can decode.\n\n"

)


# The FUSED prompt — size and decompose in one pass. Unchanged in substance;
# it simply reads as its fragments now, so the split pair can reuse them. Still
# the right call for a batch small enough that two round trips would cost more
# than the serial generation they save (_SPLIT_MIN).
_TRIAGE_SYSTEM = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and turn each one into searchable learning "
    "points, so their existing Anki deck can be searched for cards that already "
    "cover it.\n\n"

    + _SHORTHAND
    + _S_SCOPE
    + _S_DECOMPOSE
    + _S_CARDS
    + _S_TERMS
    + _S_TAGGING
    + _S_CONFIDENCE
    + (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given:\n"
    '[{"n": 1, "concept": "Graves disease: pathogenesis, features, '
    'investigation and treatment", "scope": "broad", "cards": 12, '
    '"confidence": "high", "note": "", "points": ['
    '{"concept": "Graves disease is caused by TSH-receptor stimulating '
    'autoantibodies", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves TSI", "Graves autoantibody"]}, '
    '{"concept": "Thionamides (carbimazole, propylthiouracil) are first-line '
    'medical therapy in Graves", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves carbimazole", '
    '"propylthiouracil Graves"]}]}]\n\n'

    )
    + (
    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per gap): one plain sentence naming what the student wants to "
    "know, instruction verbs and list markers stripped.\n"
    "- concept (per point): one short plain sentence stating ONE testable fact.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
    )
)


# A SIBLING prompt, not a flag on the one above.
#
# The two agree on how to READ a knowledge gap — the shorthand licence, the
# length-is-not-scope argument, the tagging rules — and disagree on everything
# downstream of that, because they're sizing opposite operations:
#
#   triage()        sizes a SEARCH. Its STEP 3 is an explicit stinginess
#                   argument: every surfaced card past what the student needs is
#                   one they read and reject.
#   triage_create() sizes a GENERATION. That argument inverts. An extra card is
#                   one untick in a review list the student is already reading;
#                   a point with no card is a gap that stays open.
#
# Threading a flag through a 90-line prompt to say the opposite thing in four
# places would make both harder to read and neither easier to tune. The PARSING
# is what's genuinely shared, and that is shared (_plan_from_obj, parse_reply,
# clamp_cards, KGPlan) rather than duplicated.
_CREATE_SYSTEM = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and plan the Anki cards that should be WRITTEN "
    "for each one.\n\n"

    + _SHORTHAND
    + _C_BREADTH
    + _C_CONSTRAINTS
    + _C_DECOMPOSE
    + _C_CARDS
    + _C_TERMS
    + _C_TAGGING
    + _C_CONFIDENCE
    + (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given. \"depth\" is \"headline\" or \"mechanism\":\n"
    '[{"n": 1, "concept": "Graves disease: pathogenesis, features, '
    'investigation and treatment", "breadth": "deep", "depth": "mechanism", '
    '"cards": 20, "confidence": "high", "note": "", "constraints": [], "points": ['
    '{"concept": "Graves disease is caused by IgG autoantibodies that stimulate '
    'the TSH receptor", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves TSI", "Graves autoantibody"]}, '
    '{"concept": "Thionamides inhibit thyroid peroxidase", '
    '"system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["carbimazole peroxidase", '
    '"thionamide peroxidase"]}, '
    '{"concept": "Propylthiouracil also blocks peripheral T4 to T3 conversion", '
    '"system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["propylthiouracil conversion", '
    '"PTU peripheral"]}]}]\n\n'

    )
    + (
    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per gap): one plain sentence naming what the student wants to "
    "know, instruction verbs and list markers stripped.\n"
    "- concept (per point): one short plain sentence stating ONE testable fact, "
    "specific enough that a card could be written from it alone.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
    )
)


# ── the split pair ───────────────────────────────────────────────────────────
#
# Same brief, cut along the seam between the two things a read does. The SORT
# call sees every gap at once and answers only the batch-relative question; the
# DECOMPOSE calls each see a handful of gaps WITH the answer already attached,
# and write the points. Neither is given the other's job, so neither can drift
# into doing it slowly.

def _sort_output(*, creating: bool, restate: bool) -> str:
    """The sizing call's OUTPUT block, in its two forms.

    `restate` is the only variable: with it the model rewrites each gap as a
    clean sentence (the "concept"), which is the longest thing this call writes
    — and output tokens are what a call costs in wall time. Without it the
    student's own wording is kept, which is faster and, on shorthand that was
    already clear, usually better: the rewrite tended to formalise a note into
    something broader than what was meant. Either way the concept is editable in
    the plan table afterwards. See read_restate_concept in core/config.py.

    The restate=True form is byte-identical to the block that was inline here
    before the setting existed."""
    concept_ex = ('"concept": "Graves disease: pathogenesis, features, '
                  'investigation and treatment", ' if restate else "")
    concept_rule = (
        "- concept: one plain sentence naming what the student wants to know, "
        "instruction verbs and list markers stripped.\n" if restate else
        "- Do NOT return a \"concept\". The student's own wording for the gap "
        "is what the plan table shows, so there is nothing to rewrite.\n")
    if creating:
        head = (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given. \"depth\" is \"headline\" or \"mechanism\":\n"
    '[{"n": 1, ' + concept_ex + '"breadth": "deep", "depth": "mechanism", '
    '"cards": 20, "confidence": "high", "note": "", "constraints": []}]\n\n')
    else:
        head = (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given:\n"
    '[{"n": 1, ' + concept_ex + '"scope": "broad", "cards": 12, '
    '"confidence": "high", "note": ""}]\n\n')
    return (head
    + "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    + concept_rule
    + "- Do NOT return a \"points\" array. Points are not your job and anything "
    "you write there is discarded.\n\n"
    "Return ONLY the JSON array. No prose, no code fences.")


_SORT_SEARCH_HEAD = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and SIZE each one: how much ground it covers, "
    "and how many of their existing Anki cards should be surfaced for it.\n\n"
    "You are NOT decomposing the gaps here. A separate call does that, gap by "
    "gap, and it is given the size you set. Your whole job is the judgement that "
    "needs the batch in front of it.\n\n"
    + _SHORTHAND
    + _S_SCOPE
    + _S_CARDS
    + _S_CONFIDENCE
)


_SORT_SEARCH_SYSTEM = _SORT_SEARCH_HEAD + _sort_output(creating=False,
                                                       restate=True)
_SORT_SEARCH_PLAIN = _SORT_SEARCH_HEAD + _sort_output(creating=False,
                                                      restate=False)


_DECOMP_SEARCH_SYSTEM = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and turn each one into searchable learning "
    "points, so their existing Anki deck can be searched for cards that already "
    "cover it.\n\n"
    "EACH GAP ARRIVES WITH ITS SCOPE ALREADY DECIDED, in brackets after the "
    "text. That judgement was made with the student's whole batch in view; it is "
    "settled, and it tells you how many points to mint. Do not second-guess it, "
    "and do not restate it — decompose to match it.\n\n"
    + _SHORTHAND
    + _S_DECOMPOSE
    + _S_TERMS
    + _S_TAGGING
    + (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given:\n"
    '[{"n": 1, "points": ['
    '{"concept": "Graves disease is caused by TSH-receptor stimulating '
    'autoantibodies", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves TSI", "Graves autoantibody"]}, '
    '{"concept": "Thionamides (carbimazole, propylthiouracil) are first-line '
    'medical therapy in Graves", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves carbimazole", '
    '"propylthiouracil Graves"]}]}]\n\n'

    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per point): one short plain sentence stating ONE testable fact.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
    )
)


_SORT_CREATE_HEAD = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and SIZE each one: how much needs writing for "
    "it, and how many new Anki cards that comes to.\n\n"
    "You are NOT decomposing the gaps here. A separate call does that, gap by "
    "gap, and it is given the breadth you set and the constraints you copy out. "
    "Your whole job is the judgement that needs the batch in front of it.\n\n"
    + _SHORTHAND
    + _C_BREADTH
    + _C_CONSTRAINTS
    + _C_CARDS
    + _C_CONFIDENCE
)


_SORT_CREATE_SYSTEM = _SORT_CREATE_HEAD + _sort_output(creating=True,
                                                       restate=True)
_SORT_CREATE_PLAIN = _SORT_CREATE_HEAD + _sort_output(creating=True,
                                                      restate=False)


_DECOMP_CREATE_SYSTEM = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and plan the Anki cards that should be WRITTEN "
    "for each one.\n\n"
    "EACH GAP ARRIVES WITH ITS BREADTH ALREADY DECIDED, in brackets after the "
    "text, along with any instructions the student wrote at you. That judgement "
    "was made with the student's whole batch in view; it is settled, and it "
    "tells you how many points to mint. Do not second-guess it, and do not "
    "restate it — decompose to match it. The student's own instructions "
    "OVERRIDE every default below, exactly as they would if you had read them "
    "off the gap yourself.\n\n"
    + _SHORTHAND
    + _C_DECOMPOSE
    + _C_TERMS
    + _C_TAGGING
    + (
    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given:\n"
    '[{"n": 1, "points": ['
    '{"concept": "Graves disease is caused by IgG autoantibodies that stimulate '
    'the TSH receptor", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves TSI", "Graves autoantibody"]}, '
    '{"concept": "Thionamides inhibit thyroid peroxidase", '
    '"system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["carbimazole peroxidase", '
    '"thionamide peroxidase"]}, '
    '{"concept": "Propylthiouracil also blocks peripheral T4 to T3 conversion", '
    '"system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["propylthiouracil conversion", '
    '"PTU peripheral"]}]}]\n\n'

    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per point): one short plain sentence stating ONE testable fact, "
    "specific enough that a card could be written from it alone.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
    )
)


# ── parsing ──────────────────────────────────────────────────────────────────

def _clean_scope(v) -> str:
    s = str(v or "").strip().lower()
    return s if s in SCOPES else DEFAULT_SCOPE


def _clean_confidence(v) -> str:
    return "low" if str(v or "").strip().lower() == "low" else "high"


def _clean_breadth(v) -> str:
    s = str(v or "").strip().lower()
    return s if s in CREATE_BREADTHS else DEFAULT_BREADTH


def _clean_band(v, breadth: str) -> str:
    """The depth half of the dial. The model may say "headline"/"mechanism" to
    nudge a gap deeper than its breadth bucket implies; anything else (or
    nothing) falls back to the bucket's own band."""
    s = str(v or "").strip().lower()
    if s == "mechanism":
        return "mechanism"
    if s == "headline":
        return "essentials"
    if s in VALID_BANDS:
        return s
    return breadth_meta(breadth)["band"]


def _clean_constraints(raw) -> list[str]:
    """The student's own directives, verbatim but sanity-bounded."""
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        c = re.sub(r"\s+", " ", str(c or "")).strip()
        if c and c.lower() not in {x.lower() for x in out}:
            out.append(c[:200])
    return out[:8]


def _plan_from_obj(obj: dict, kg: dict, *,
                   flow: str = "search", require_terms: bool = True,
                   lock: dict | None = None) -> KGPlan:
    """Build one KGPlan from the model's object for it. Tolerant: a missing or
    malformed piece degrades that piece, never the whole run.

    `flow` picks which dial the reply is read against — "search" reads `scope`
    against SCOPES, "create" reads `breadth`/`depth` against CREATE_BREADTHS.
    `require_terms` decides whether a point with no usable search terms survives
    (see below); it is independent of `flow` only so the two are testable apart.

    `lock` is the breadth/cards the USER already set on this gap before asking
    for a reading. It wins outright. The model is told about it too (see
    _kg_prompt_line) so it decomposes to the right size rather than being
    overridden after the fact — but if it argues anyway, the user still wins.
    """
    kg_id = str(kg.get("id") or "")
    title = str(kg.get("title") or "").strip()
    scope = _clean_scope(obj.get("scope"))
    creating = flow == "create"
    breadth = _clean_breadth(obj.get("breadth")) if creating else DEFAULT_BREADTH
    default_cards = (breadth_meta(breadth) if creating else scope_meta(scope))["cards"]
    lock = lock or {}
    if creating and lock.get("breadth"):
        breadth = _clean_breadth(lock["breadth"])
        default_cards = breadth_meta(breadth)["cards"]
    plan = KGPlan(
        kg_id=kg_id,
        title=title,
        kg_type=str(kg.get("type") or "kg").strip().lower() or "kg",
        group=str(kg.get("group") or "").strip(),
        concept=str(obj.get("concept") or "").strip() or title,
        scope=scope,
        breadth=breadth,
        depth_band=(breadth_meta(breadth)["band"] if (creating and lock.get("breadth"))
                    else (_clean_band(obj.get("depth"), breadth) if creating
                          else "essentials")),
        cards=clamp_cards(lock.get("cards") or obj.get("cards", default_cards)),
        confidence=_clean_confidence(obj.get("confidence")),
        note=str(obj.get("note") or "").strip(),
        constraints=_clean_constraints(obj.get("constraints")) if creating else [],
        tags=[t for t in (kg.get("tags") or []) if t],
    )
    raw_points = obj.get("points")
    if not isinstance(raw_points, list):
        raw_points = []
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        concept = str(p.get("concept") or "").strip()
        if not concept:
            continue
        terms = _clean_terms(p.get("terms"))
        if not terms and require_terms:
            # Same rule as the LO path: a point with no usable terms can only
            # ever search on its tag leg, which under-recalls badly enough to
            # manufacture a false gap. Drop it and say so.
            #
            # Generation is the opposite case, which is why this is a parameter:
            # terms are irrelevant to WRITING a card (they only feed the optional
            # coverage check), so dropping a perfectly good writeable point
            # because its search terms came back unusable would silently shrink
            # the batch for a reason that doesn't apply.
            log.warn(f"kg triage: dropping point '{concept[:40]}' — no usable terms")
            continue
        plan.points.append(LearningPoint(
            concept=concept,
            refs=[],
            system=_clean_level(p.get("system")),
            subsystem=_clean_level(p.get("subsystem")),
            topic=_clean_level(p.get("topic")),
            terms=terms,
            kg_id=kg_id,
        ))
    # ONE CARD PER POINT is the floor when writing. Points are single facts
    # now, and gen_prompt._points_block CUTS the list to the budget — so a model
    # that minted 12 points but guessed "cards": 10 would silently lose two
    # facts. A budget the USER set (a lock) is theirs and is left alone.
    if creating and plan.points and not lock.get("cards"):
        plan.cards = clamp_cards(max(plan.cards, len(plan.points)))
    if not plan.points:
        plan.confidence = "low"
        plan.note = plan.note or (
            "Nothing writeable came back for this gap." if creating
            else "Nothing searchable came back for this gap.")
    return plan


def parse_reply(reply, kgs: list[dict], *,
                flow: str = "search", require_terms: bool = True,
                locks: dict | None = None) -> list[KGPlan]:
    """Coerce the triage reply into one KGPlan per input KG.

    Indexed by the "n" the model was given rather than by position, so a reply
    that drops or reorders an object still lands every other gap on the right
    KG. Anything unmatched falls back to a low-confidence stub — a KG the user
    can see and fix, never one that silently vanishes from their batch.
    """
    by_n: dict[int, dict] = {}
    if isinstance(reply, list):
        for i, obj in enumerate(reply):
            if not isinstance(obj, dict):
                continue
            try:
                n = int(obj.get("n", i + 1))
            except (TypeError, ValueError):
                n = i + 1
            by_n.setdefault(n, obj)

    creating = flow == "create"
    stub_cards = (CREATE_BREADTHS[DEFAULT_BREADTH] if creating
                  else SCOPES[DEFAULT_SCOPE])["cards"]

    locks = locks or {}
    plans: list[KGPlan] = []
    for i, kg in enumerate(kgs, 1):
        obj = by_n.get(i)
        lock = locks.get(str(kg.get("id") or "")) or {}
        if obj is None:
            title = str(kg.get("title") or "").strip()
            log.warn(f"kg triage: no reply object for gap {i} ('{title[:40]}')")
            plans.append(KGPlan(
                kg_id=str(kg.get("id") or ""), title=title,
                kg_type=str(kg.get("type") or "kg").strip().lower() or "kg",
                group=str(kg.get("group") or "").strip(),
                concept=title,
                breadth=(_clean_breadth(lock["breadth"]) if (creating and lock.get("breadth"))
                         else DEFAULT_BREADTH),
                cards=clamp_cards(lock.get("cards") or stub_cards), confidence="low",
                note="The AI didn't return a reading for this gap.",
                tags=[t for t in (kg.get("tags") or []) if t],
            ))
            continue
        plans.append(_plan_from_obj(obj, kg, flow=flow, require_terms=require_terms,
                                    lock=lock))
    return plans


# ── the call ─────────────────────────────────────────────────────────────────
#
# A read is TWO shapes of call, not one, and which shape runs depends only on
# how many gaps there are:
#
#   small batch  → one fused call. Two round trips would cost more in process
#                  startup (~9s each, measured) than they save.
#   large batch  → one SORT call over everything, then N DECOMPOSE calls in
#                  parallel, each carrying a few gaps and their decided size.
#
# The numbers that forced this, measured 2026-08-23 on 33 of his real gaps
# through the CLI on claude-sonnet-4-6:
#
#   8 gaps, fused          168s   (9.0KB of JSON out)
#   33 gaps, fused         still generating at 9m47s, against a 300s timeout
#   a trivial call         8.6s   ← fixed process overhead, the tax per call
#
# ~20s per gap, and it does not improve with batch size because output tokens
# are generated serially whatever else is in the call. The only thing a big
# batch ever saved was sending the 6KB system prompt once instead of N times,
# against a 37KB answer. That is why splitting is a straight win here and not a
# trade.

# Below this many gaps, run the fused prompt: one call, sized and decomposed
# together. Two round trips at ~9s of process startup each only pay for
# themselves once there is enough decomposition to actually parallelise.
def _restate_concept() -> bool:
    """Whether the sizing call rewrites each gap as a clean sentence.

    Off by default, deliberately: it is the longest field that call writes, and
    on shorthand that was already clear the rewrite tended to broaden what was
    meant. With it off the student's own wording is what the plan table shows —
    still editable there, and one AI call's worth of output shorter."""
    try:
        from ...core.config import load_config
        return bool(load_config().get("read_restate_concept", False))
    except Exception:
        return False


def _sort_system(*, creating: bool) -> str:
    if _restate_concept():
        return _SORT_CREATE_SYSTEM if creating else _SORT_SEARCH_SYSTEM
    return _SORT_CREATE_PLAIN if creating else _SORT_SEARCH_PLAIN


_SPLIT_MIN = 5

# Gaps per decompose call. Sized against the WORKER POOL, not against token
# cost — the same reasoning as matcher.CARDS_PER_NOTES_CALL. What the user waits
# for is the number of WAVES: 33 gaps at 3-per-call is 11 calls, which is three
# waves at four workers, ~45s of generation each. Bigger chunks mean fewer,
# slower waves; smaller chunks pay the 9s startup tax more often.
_DECOMP_CHUNK = 3

# Wall-clock ceilings, all generous against the measured rates — what they exist
# to catch is a wedged subprocess, not a slow one. The sort call reads
# everything but writes ~50 tokens a gap; a decompose call writes a lot, but
# only ever for _DECOMP_CHUNK gaps. The fused one keeps the 300s it always had,
# which is safe now that only a small batch can reach it.
_SORT_TIMEOUT = 420
_DECOMP_TIMEOUT = 300
_FUSED_TIMEOUT = 300


def _read_workers() -> int:
    """How many decompose calls run at once.

    Four, matching relevance_workers, and it is MEMORY that caps it rather than
    politeness: a `claude` CLI process peaks at ~434MB resident (measured
    2026-08-23), so four is already ~1.7GB running alongside Anki on an 8GB
    machine. Raising this is the wrong lever — balance the chunks instead, which
    _pack_chunks does. 1 restores fully serial decomposition.
    """
    from ...core.config import load_config
    try:
        return max(1, int(load_config().get("read_workers") or 4))
    except Exception:
        return 4


# How much of a gap's notes the model is shown. It used to be 300 CHARACTERS,
# flattened onto one line — and that is not a truncation, it is a different
# gap. "Other groin lumps" carried 1,282 characters of differential, sorted
# under its own headings (Lymph nodes / Vascular / Skin-soft tissue /
# Undescended testis / Muscle-tendon). The model was handed the first 300,
# cut mid-word inside "saphenofemoral", and read it back as "lymph node causes
# (reactive, lymphoma, metastatic) and saphena varix" — a faithful summary of
# the only third it was given. Everything from Sebaceous cyst onwards was
# never in the prompt at all.
#
# 2000 characters, because notes are the single biggest input to how a gap is
# read and a cap that silently eats them is worse than a long prompt. A cap
# still exists so one pathological note can't swallow a batch's budget, and
# when it bites it SAYS SO in the prompt rather than pretending the note
# ended there.
_NOTES_CHARS = 2000


def _notes_for_prompt(notes) -> str:
    """The student's notes as the model should see them — STRUCTURE INTACT.

    The old version collapsed every run of whitespace into one space,
    which turns a
    headed list into one run-on sentence: "Lymph nodes Reactive lymphadenopathy
    — … Vascular Saphena varix — …". The headings stop being headings, so the
    thing most likely to produce one point per category reads as prose about
    whatever came first. Line breaks are kept; only runs of spaces and runs of
    blank lines are tidied.

    Indented four spaces because every gap is a numbered item in the prompt
    ("1. ", "2. " …). Indenting keeps a multi-line block visibly part of its
    own number instead of looking like the start of the next gap.
    """
    text = str(notes or "")
    if not text.strip():
        return ""
    # HTML to text, keeping the breaks that carry the structure.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines: list = []
    for raw in text.splitlines():
        ln = re.sub(r"[ \t\xa0]+", " ", raw).strip()
        if ln:
            lines.append(ln)
        elif lines and lines[-1] != "":
            lines.append("")          # one blank line, never three
    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines)
    if not text:
        return ""
    if len(text) > _NOTES_CHARS:
        head = text[:_NOTES_CHARS]
        # Cut at a line end when there is one nearby, so the last thing the
        # model sees is a whole item rather than half a word.
        nl = head.rfind("\n")
        if nl > _NOTES_CHARS * 0.5:
            head = head[:nl]
        text = (head.rstrip()
                + "\n[THEIR NOTES CONTINUE PAST HERE and were cut for length "
                  "— do not treat this as the end of the list.]")
    body = "\n".join("    " + ln if ln else "" for ln in text.split("\n"))
    return ("\n  THEIR OWN NOTES ON THIS GAP — read ALL of it. This is the "
            "substance of the gap and the title is only its label; where the "
            "notes are a list, every entry is part of what they want to "
            "know, and any headings in them are the structure to decompose "
            "along:\n" + body)


def _kg_prompt_line(kg: dict, lock: dict | None = None) -> str:
    """One gap as the model sees it: the title, plus any notes the student
    attached (which often carry the actual specifics the title elides).

    `lock` is a breadth/card count the student set BEFORE asking for a reading.
    It's stated in the prompt rather than only enforced afterwards, so the
    decomposition comes back at the right size — locking "core" after the fact
    would otherwise leave 14 points attached to a 4-card gap.

    `read_as` is the student's own rewrite of what the gap is asking for, typed
    over the reading on the run screen. It is the single most direct thing they
    can say about a gap, so it goes in the prompt AS the subject and the title
    drops to context.

    That it lands in this function is the whole fix. The reading was editable
    for a long time and the edit reached nothing: it was folded into the plan's
    `concept`, which the writer only ever uses as a heading, and it never came
    near the decompose call. Putting it here means the model re-reads the gap
    from the student's wording, the read cache invalidates itself (this line IS
    the cache key — see read_cache.key), and the edit survives the run. All
    three fell out of putting it in the one place it should always have been."""
    from . import store as kg_store
    title = str(kg.get("title") or "").strip()
    read_as = str(kg_store.field(kg, "read_as") or "").strip()
    if read_as:
        bits = [read_as,
                "[THE STUDENT REWROTE WHAT THIS GAP IS ASKING FOR. The line "
                "above is theirs and is the subject — decompose THAT. The "
                f'original title was "{title}" and is context only.]']
    else:
        bits = [title]
    lock = lock or {}
    if lock.get("breadth") or lock.get("cards"):
        want = []
        if lock.get("breadth"):
            want.append(f'breadth="{_clean_breadth(lock["breadth"])}"')
        if lock.get("cards"):
            want.append(f'cards={clamp_cards(lock["cards"])}')
        bits.append("[THE STUDENT HAS ALREADY SET " + " and ".join(want) +
                    " FOR THIS GAP — this is their decision, not a suggestion. "
                    "Return exactly these values and decompose to match: the "
                    "number of points must fit that budget.]")
    block = _notes_for_prompt(kg_store.field(kg, "notes"))
    if block:
        bits.append(block)
    for level in ("system", "subsystem", "topic"):
        v = kg_store.field(kg, level)
        if v:
            bits.append(f"[{level}: {v}]")
    return " ".join(bits)


def _decomp_prompt_line(kg: dict, obj: dict, n: int, *, creating: bool) -> str:
    """One gap for a DECOMPOSE call: the same text, plus the size the sort call
    already settled on, spelled out as the point range rather than just the
    word.

    The range is stated because it is the operative instruction — "broad" only
    means "6 to 12 points" via a table the decompose prompt also carries, and
    saying both costs a dozen tokens and removes the chance of disagreement.

    Constraints ride along verbatim. They were read off the gap by the sort
    call, and a constraint that reaches only the sort call is one that shapes
    nothing: the points are written here.
    """
    base = _kg_prompt_line(kg)
    if creating:
        breadth = _clean_breadth(obj.get("breadth"))
        lo, hi = breadth_meta(breadth)["points"]
        band = _clean_band(obj.get("depth"), breadth)
        size = (f"[BREADTH: {breadth} — {lo} to {hi} points"
                + (", reaching mechanism and distinction" if band == "mechanism" else "")
                + "]")
    else:
        scope = _clean_scope(obj.get("scope"))
        lo, hi = scope_meta(scope)["points"]
        size = (f"[SCOPE: {scope} — "
                + (f"exactly {lo} point" if lo == hi else f"{lo} to {hi} points")
                + "]")
    bits = [f"{n}. {base}", size]
    cons = _clean_constraints(obj.get("constraints")) if creating else []
    if cons:
        bits.append("[THE STUDENT'S OWN INSTRUCTIONS, WHICH OVERRIDE EVERYTHING: "
                    + "; ".join(f'"{c}"' for c in cons) + "]")
    return " ".join(bits)


def _ask(prompt: str, system: str, *, model, max_tokens: int, timeout: int,
         what: str):
    """One AI call with this module's error contract.

    Every failure lands as ExtractionError with a classified code, because that
    is what the panel renders — the same contract extract_from_objectives has,
    which is what lets run_pipeline treat the two branches identically.
    """
    try:
        return core_api.ask_claude_json(
            prompt=prompt, system=system, max_tokens=max_tokens, model=model,
            show_errors=False, timeout=timeout, raise_on_error=True)
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        log.error(f"kg triage ({what}) failed [{code}]: {e}")
        raise ExtractionError(code, friendly) from e
    except Exception as e:
        log.error(f"kg triage ({what}) failed: {e}")
        raise ExtractionError("unknown", f"Couldn't read those knowledge gaps: {e}") from e


def _objs_by_n(reply) -> dict[int, dict]:
    """The model's reply keyed by the "n" it was given, position as fallback.

    Same indexing rule as parse_reply, and for the same reason: a reply that
    drops or reorders an object must still land every other one on the right
    gap.
    """
    out: dict[int, dict] = {}
    if isinstance(reply, list):
        for i, obj in enumerate(reply):
            if not isinstance(obj, dict):
                continue
            try:
                n = int(obj.get("n", i + 1))
            except (TypeError, ValueError):
                n = i + 1
            out.setdefault(n, obj)
    return out


def _sort_batch(kgs: list[dict], *, creating: bool, model, locks: dict) -> dict[int, dict]:
    """The batch-wide sizing call. One round trip, ~50 tokens of answer per gap.

    Returns the model's object per gap, WITHOUT points — every other key
    _plan_from_obj reads (concept, scope/breadth/depth, cards, confidence, note,
    constraints) comes from here, so a gap whose decomposition later fails still
    lands in the table with a real reading attached.
    """
    prompt = "\n".join(
        f"{i}. {_kg_prompt_line(k, locks.get(str(k.get('id') or '')))}"
        for i, k in enumerate(kgs, 1))
    reply = _ask(prompt, _sort_system(creating=creating), model=model,
                 # No points here, so this scales with the gap COUNT rather than
                 # with how much there is to say about each one.
                 max_tokens=max(2000, len(kgs) * 120),
                 timeout=_SORT_TIMEOUT, what="sort")
    if reply is None:
        raise ExtractionError("empty", "The AI returned nothing for those gaps.")
    return _objs_by_n(reply)


def _decompose_chunk(chunk: list[tuple[int, dict, dict]], *,
                     creating: bool, model) -> dict[int, list]:
    """One decompose call over a few gaps. Returns {n: raw points list}.

    `chunk` is (n, kg, sort_obj) triples — the original 1-based n is kept as the
    key the model is asked to echo, so a chunk's replies re-key onto the batch
    with no second mapping to get wrong.
    """
    prompt = "\n".join(
        _decomp_prompt_line(kg, obj, n, creating=creating) for n, kg, obj in chunk)
    reply = _ask(prompt,
                 _DECOMP_CREATE_SYSTEM if creating else _DECOMP_SEARCH_SYSTEM,
                 model=model,
                 # Budgeted per-gap at the DEEP/BROAD cost, not the average: a
                 # truncated JSON array is a total parse loss for the chunk.
                 max_tokens=max(3000, len(chunk) * 900),
                 timeout=_DECOMP_TIMEOUT, what="decompose")
    out: dict[int, list] = {}
    for n, obj in _objs_by_n(reply).items():
        pts = obj.get("points")
        out[n] = pts if isinstance(pts, list) else []
    return out


def _pack_chunks(triples: list, *, creating: bool, size: int) -> list[list]:
    """Group gaps into decompose calls of `size`, BALANCED BY EXPECTED COST and
    ordered dearest-first.

    Packing them in the order they happen to sit in the queue is what made the
    first measured run 29% idle: a chunk of three broad gaps took 118s while
    another took 25s, and the pool sat on its hands waiting for the long one.
    Sizing already told us what each gap will cost — a narrow gap mints one
    point, a broad one up to twelve — so the cost is known before any of these
    calls is made, and there is no excuse for an unbalanced deal.

    Longest-processing-time-first, with a hard cap of `size` per bin: fill the
    emptiest bin that still has room, dearest gap first, then submit the
    heaviest bins first so nothing expensive is left to start alone in the last
    wave.
    """
    n_bins = max(1, -(-len(triples) // size))

    def cost(obj: dict) -> int:
        # The TOP of the band, not the middle: what hurts is the long chunk, and
        # a gap that mints its maximum is the one that produces it.
        meta = (breadth_meta(_clean_breadth(obj.get("breadth"))) if creating
                else scope_meta(_clean_scope(obj.get("scope"))))
        return meta["points"][1]

    bins: list[list] = [[] for _ in range(n_bins)]
    load = [0] * n_bins
    for t in sorted(triples, key=lambda t: -cost(t[2])):
        j = min((k for k in range(n_bins) if len(bins[k]) < size),
                key=lambda k: load[k])
        bins[j].append(t)
        load[j] += cost(t[2])
    return [bins[j] for j in sorted(range(n_bins), key=lambda k: -load[k])
            if bins[j]]


def _read_split(kgs: list[dict], *, creating: bool, model, locks: dict,
                report=None) -> list[dict]:
    """Sort once, decompose in parallel, and return one merged object per gap.

    The merge is deliberately shallow: the sort object IS the gap's object, with
    the decompose call's points spliced in. Everything downstream — the clamps,
    the locks, the low-confidence stubbing — stays in _plan_from_obj, which
    neither call knows about.

    A decompose chunk that fails costs its own gaps their POINTS and nothing
    else; they still arrive with the sort call's reading, and _plan_from_obj
    marks a point-less plan low-confidence with a visible note. That is the same
    policy parse_reply already applies to a gap the model skipped: a row the
    student can see and fix, never one that silently vanishes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _say(msg):
        if report:
            try:
                report(msg)
            except Exception:
                pass

    _say(f"Sizing {len(kgs)} gap(s)…")
    sort_objs = _sort_batch(kgs, creating=creating, model=model, locks=locks)

    triples = [(i, kg, sort_objs.get(i) or {}) for i, kg in enumerate(kgs, 1)]
    chunks = _pack_chunks(triples, creating=creating, size=_DECOMP_CHUNK)

    # Thread-local in core/api.py: a worker that never re-arms it cannot be
    # cancelled, and Stop would look ignored until the call timed out.
    cancel_ev = core_api._current_cancel_event()

    def _one(chunk):
        core_api.set_cancel_event(cancel_ev)
        try:
            return _decompose_chunk(chunk, creating=creating, model=model)
        finally:
            core_api.set_cancel_event(None)

    points_by_n: dict[int, list] = {}

    def _run(chunks: list, verb: str) -> None:
        """Run a set of decompose chunks, up to `workers` at a time, folding
        what comes back into points_by_n. A chunk that fails logs and leaves its
        own gaps empty; the batch carries on."""
        if not chunks:
            return
        workers = min(_read_workers(), len(chunks))
        done = 0
        total = len(chunks)
        _say(f"{verb} {len(kgs)} gap(s)… 0/{total}"
             + (f" ({workers} at a time)" if workers > 1 else ""))
        if workers <= 1:
            for chunk in chunks:
                try:
                    points_by_n.update(_one(chunk))
                except core_api.ClaudeCancelled:
                    raise
                except ExtractionError as e:
                    log.error("kg triage: decompose chunk failed "
                              f"[{e.code}]: {e.friendly}")
                done += 1
                _say(f"{verb} {len(kgs)} gap(s)… {done}/{total}")
                if cancel_ev is not None and cancel_ev.is_set():
                    return
            return
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="ankisstant-read") as pool:
            futures = {pool.submit(_one, c): c for c in chunks}
            try:
                for fut in as_completed(futures):
                    try:
                        points_by_n.update(fut.result())
                    except core_api.ClaudeCancelled:
                        raise
                    except ExtractionError as e:
                        # Its own gaps lose their points; the batch does not.
                        log.error("kg triage: decompose chunk failed "
                                  f"[{e.code}]: {e.friendly}")
                    done += 1
                    _say(f"{verb} {len(kgs)} gap(s)… {done}/{total} "
                         f"({workers} at a time)")
                    if cancel_ev is not None and cancel_ev.is_set():
                        break
            finally:
                # Nothing that hasn't started should start: on Stop every
                # remaining call is unwanted.
                for fut in futures:
                    fut.cancel()

    _run(chunks, "Reading")

    # One retry over whatever came back empty. A point-less gap is a FAILED
    # decompose rather than a reading — the brief never allows zero points — so
    # this is the difference between a quota blip costing 18 gaps their points
    # and costing nothing. It is free when nothing failed, and when the provider
    # really is down each retry returns its error in seconds instead of
    # generating anything. Smaller chunks, because whatever went wrong is more
    # likely to be size-related than anything else.
    if cancel_ev is None or not cancel_ev.is_set():
        missing = [t for t in triples if not points_by_n.get(t[0])]
        if missing:
            _run(_pack_chunks(missing, creating=creating,
                              size=max(1, _DECOMP_CHUNK - 1)), "Re-reading")

    merged = []
    for n, kg, obj in triples:
        o = dict(obj)
        o["n"] = n
        o["points"] = points_by_n.get(n) or []
        merged.append(o)
    return merged


def _cache_keys(kgs: list[dict], *, creating: bool, model,
                locks: dict) -> list[str]:
    """One cache key per gap, in order.

    The key is built from the exact line the gap contributes to the prompt, so
    editing its text, its notes or its locks is a miss by construction. The
    signature covers every brief this read could use — including the change the
    restate-concept setting makes — so a prompt edit invalidates the cache
    instead of quietly serving readings written to the old brief."""
    sig = read_cache.prompt_signature(
        _sort_system(creating=creating),
        _DECOMP_CREATE_SYSTEM if creating else _DECOMP_SEARCH_SYSTEM,
        _CREATE_SYSTEM if creating else _TRIAGE_SYSTEM)
    flow = "create" if creating else "search"
    return [read_cache.key(
        gap_line=_kg_prompt_line(k, locks.get(str(k.get("id") or ""))),
        flow=flow, model=model, prompt_sig=sig) for k in kgs]


def forget(kg_ids: list[str]) -> int:
    """Drop these gaps' cached readings so the next run reads them fresh.

    By gap id rather than by key, because one gap can have several entries — a
    search reading and a create reading, with locks and without — and the point
    of the button is "read this again", not "read this again under exactly the
    conditions I last used".
    """
    try:
        return read_cache.drop_for_gaps([str(i) for i in (kg_ids or []) if i])
    except Exception as e:
        log.warn(f"kg triage: couldn't forget readings ({e})")
        return 0


def _read_objs(kgs: list[dict], *, creating: bool, model, locks: dict,
               report=None) -> list[dict]:
    """Read gaps the cache did not have, one raw object per gap, in order.

    The fused single call below _SPLIT_MIN, the sort/decompose split above it —
    the size where the split's extra round trips start costing less than the
    serial generation they save."""
    flow = "create" if creating else "search"
    if len(kgs) < _SPLIT_MIN:
        prompt = "\n".join(
            f"{i}. {_kg_prompt_line(k, locks.get(str(k.get('id') or '')))}"
            for i, k in enumerate(kgs, 1))
        reply = _ask(prompt, _CREATE_SYSTEM if creating else _TRIAGE_SYSTEM,
                     model=model,
                     # Budgeted per-gap at the BROAD/DEEP cost, not the average
                     # — a truncated JSON array is a total parse loss, and a
                     # batch of drug names is exactly the case that would hit it.
                     max_tokens=max(4000, len(kgs) * (850 if creating else 700)),
                     timeout=_FUSED_TIMEOUT, what=flow)
        if reply is None:
            raise ExtractionError("empty", "The AI returned nothing for those gaps.")
        by_n = _objs_by_n(reply)
        return [dict(by_n.get(i) or {}) for i in range(1, len(kgs) + 1)]
    return _read_split(kgs, creating=creating, model=model, locks=locks,
                       report=report)


def _read_batch(kgs: list[dict], *, creating: bool, require_terms: bool,
                model=None, locks: dict | None = None,
                report=None) -> list[KGPlan]:
    """Read a batch of knowledge gaps into plans. Shared by both flows.

    Every gap is looked up in the read cache FIRST and only the misses are sent,
    which is what makes re-running a batch cheap: 32 of yesterday's 33 gaps cost
    nothing, and the one you edited costs one read. The cache is keyed on the
    exact prompt line a gap produces, so an edit to its text, its notes or its
    locks is a miss by construction — see tools/kg/read_cache.py.

    Raises ExtractionError on a hard provider failure so the panel can show the
    classified reason (same contract as extract_from_objectives, which is what
    lets run_pipeline treat the two branches identically).

    Runs on whatever thread the caller provides; does no Qt or collection work.
    `report` is an optional one-arg status callback — it is called from worker
    threads, so it must marshal to the main thread itself.
    """
    kgs = [k for k in kgs if isinstance(k, dict) and str(k.get("title") or "").strip()]
    if not kgs:
        raise ExtractionError("empty", "No knowledge gaps to read.")
    locks = locks or {}
    flow = "create" if creating else "search"

    def _say(msg):
        if report:
            try:
                report(msg)
            except Exception:
                pass

    keys = _cache_keys(kgs, creating=creating, model=model, locks=locks)
    hits = read_cache.get_many(keys)

    objs: dict[int, dict] = {i: dict(hits[k]) for i, k in enumerate(keys)
                             if k in hits}
    todo = [i for i in range(len(kgs)) if i not in objs]
    if todo:
        if objs:
            _say(f"{len(objs)} of {len(kgs)} gap(s) already read — "
                 f"reading {len(todo)}…")
        got = _read_objs([kgs[i] for i in todo], creating=creating, model=model,
                         locks=locks, report=report)
        store: dict[str, dict] = {}
        owners: dict[str, str] = {}
        for j, i in enumerate(todo):
            o = dict(got[j]) if j < len(got) and isinstance(got[j], dict) else {}
            objs[i] = o
            store[keys[i]] = o
            owners[keys[i]] = str(kgs[i].get("id") or "")
        # put_many drops the point-less ones itself: a failed decompose must
        # stay a miss, or a quota blip would be cached as an answer.
        read_cache.put_many(store, gap_ids=owners)
    elif kgs:
        _say(f"All {len(kgs)} gap(s) already read.")

    merged = []
    for i in range(len(kgs)):
        o = dict(objs.get(i) or {})
        o["n"] = i + 1
        merged.append(o)
    return parse_reply(merged, kgs, flow=flow, require_terms=require_terms,
                       locks=locks)


def triage(kgs: list[dict], *, model: str | None = None,
           report=None) -> list[KGPlan]:
    """Read a batch of knowledge gaps for SEARCHING."""
    plans = _read_batch(kgs, creating=False, require_terms=True, model=model,
                        report=report)
    for p in plans:
        log.trace(f"KG {p.kg_id}: scope={p.scope} cards={p.cards} "
                  f"points={len(p.points)} | {p.title[:60]}")
    return plans


def triage_create(kgs: list[dict], *, model: str | None = None,
                  locks: dict | None = None, report=None) -> list[KGPlan]:
    """Read a batch of knowledge gaps for GENERATION.

    The sibling of triage(): same call shape, same error contract, same parsing
    — a different prompt pair and a different dial. See _CREATE_SYSTEM for why
    the two are separate rather than one function with a flag.

    Points come back WITHOUT the terms requirement: a point that can't be
    searched can still be written about, and the terms only matter if the user
    turns on the optional deck/tag coverage check.
    """
    plans = _read_batch(kgs, creating=True, require_terms=False, model=model,
                        locks=locks, report=report)
    for p in plans:
        log.trace(f"KG {p.kg_id}: breadth={p.breadth} band={p.depth_band} "
                  f"cards={p.cards} points={len(p.points)} | {p.title[:60]}")
    return plans


def _group_for_queued(gap: dict, kg_id: str) -> str:
    """The group for a gap that came off a queue.

    Queue entries are snapshots, so the group is read back from the store when
    the snapshot doesn't carry one — a gap queued before it was grouped should
    still tag into its group when it's finally generated. Silent on failure:
    no group is a worse outcome than no cards."""
    snap = str(gap.get("group") or "").strip()
    if snap or not kg_id:
        return snap
    try:
        from . import store as kg_store
        kg = kg_store.get(kg_id)
        return str((kg or {}).get("group") or "").strip()
    except Exception as e:
        log.warn(f"triage: group lookup failed for {kg_id}: {e}")
        return ""


def plan_from_queued_gap(gap: dict) -> KGPlan | None:
    """Rebuild a KGPlan from a queued gap that already carries `points` and
    `target_cards`, with no AI call.

    Mass AI Search writes exactly that shape onto every gap it hands to Create
    (bulk_search._on_send_to_create) — the uncovered points, and how many cards
    they're worth — and until now nothing read it back. A gap that arrives
    already planned is excluded from the triage call: the work is done, the
    points are the ones the user's own deck was measured against, and re-reading
    them would both cost tokens and throw away that measurement.

    Returns None when the gap carries no usable points, so the caller can fall
    through to triage_create for it.
    """
    raw = gap.get("points")
    if not isinstance(raw, list) or not raw:
        return None
    kg_id = str(gap.get("kg_id") or gap.get("id") or "")
    title = str(gap.get("title") or "").strip()

    points: list[LearningPoint] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        concept = str(p.get("concept") or "").strip()
        if not concept:
            continue
        points.append(LearningPoint(
            concept=concept,
            refs=[],
            system=_clean_level(p.get("system")),
            subsystem=_clean_level(p.get("subsystem")),
            topic=_clean_level(p.get("topic")),
            terms=_clean_terms(p.get("terms")),
            kg_id=kg_id,
        ))
    if not points:
        return None

    # Breadth is inferred from the shape rather than stored, because Search
    # never had a breadth to record. The point count is the honest signal: it's
    # what Search's own scope judgement produced, already filtered down to what
    # the deck didn't cover.
    n_points = len(points)
    breadth = ("pinpoint" if n_points <= 1
               else "core" if n_points <= 6
               else "wide")
    try:
        cards = clamp_cards(int(gap.get("target_cards") or 0) or
                            breadth_meta(breadth)["cards"])
    except (TypeError, ValueError):
        cards = breadth_meta(breadth)["cards"]

    return KGPlan(
        kg_id=kg_id,
        title=title,
        kg_type=str(gap.get("kg_type") or gap.get("type") or "kg").strip().lower() or "kg",
        group=_group_for_queued(gap, kg_id),
        concept=str(gap.get("concept") or "").strip() or title,
        breadth=breadth,
        depth_band=breadth_meta(breadth)["band"],
        cards=cards,
        points=points,
        tags=[t for t in (gap.get("tags") or []) if t],
    )


# ── plans → pipeline inputs ──────────────────────────────────────────────────

def points_of(plans: list[KGPlan]) -> list[LearningPoint]:
    """Every plan's points, flattened, in plan order — what the matcher runs on."""
    return [pt for p in plans for pt in p.points]


def total_cards(plans: list[KGPlan]) -> int:
    """The batch budget: the sum of the per-KG targets. Used as
    matcher.allocate's hard ceiling, so the user's edited numbers in the review
    table are what actually bounds the run."""
    return sum(clamp_cards(p.cards) for p in plans if p.ok)


def cand_cap_for(plans: list[KGPlan]):
    """Per-point CANDIDATE cap for search_all(cand_cap_for=...).

    Not the same knob as cap_for: that's how many cards a point may KEEP, this
    is how many it hands the relevance judge to choose from. The judge is the
    slow, expensive leg — one AI call per ~20 candidates — and a narrow gap
    entitled to ONE card has no use for a pool of twenty.

    Four candidates per card the point could keep, floored at 8. The floor is
    what protects quality: the known cost of a shallower pool (measured on
    lectures: 10→7 candidates loses roughly one good card per point) bites when
    a point is trying to keep 3-5 cards, not when it's picking a single best
    one out of eight. The caller's global candidate_cap is still the ceiling.
    """
    by_kg = {p.kg_id: max(8, 4 * p.per_point_cap) for p in plans if p.kg_id}

    def _cap(point) -> int:
        return by_kg.get(getattr(point, "kg_id", ""), 12)

    return _cap


def cap_for(plans: list[KGPlan]):
    """Build the per-point cap callable for matcher.allocate(cap_for=...).

    Keyed on kg_id, so every point decomposed out of a broad gap gets that
    gap's deeper per-point share while a narrow gap's single point stays at one
    or two. A point with no kg_id (there shouldn't be one in KG mode) falls back
    to the focused default rather than blowing up the allocation.
    """
    by_kg = {p.kg_id: p.per_point_cap for p in plans if p.kg_id}
    fallback = SCOPES[DEFAULT_SCOPE]["cards"]

    def _cap(m) -> int:
        return by_kg.get(getattr(m.point, "kg_id", ""), fallback)

    return _cap
