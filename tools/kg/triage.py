# Knowledge gaps → learning points, scope-aware.
#
# The bridge that lets a batch of KGs run through the AI Lecture pipeline
# (tools/lecture/matcher.py) instead of being worked one at a time through AI
# Browse. One AI call over the whole selection returns, per KG:
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
# PURE-ish: one AI call, no Qt, no mw.col. Safe on a worker thread, which is
# what lets run_pipeline call it exactly where LO mode calls the extractor.

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...core import api as core_api, log
from ..lecture.extractor import ExtractionError, LearningPoint, _clean_level, _clean_terms


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
    "focused": {"points": (1, 3), "cards": 4,
                "label": "Focused", "hint": "one entity, one aspect"},
    "broad":   {"points": (3, 7), "cards": 10,
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
    "core":     {"points": (2, 4), "cards": 5,  "band": "essentials",
                 "label": "Core",     "hint": "one aspect, the must-knows"},
    "wide":     {"points": (5, 8), "cards": 12, "band": "essentials",
                 "label": "Wide",     "hint": "the whole entity, headline level"},
    "deep":     {"points": (5, 8), "cards": 22, "band": "mechanism",
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

_TRIAGE_SYSTEM = (
    "You read a medical student's KNOWLEDGE GAPS — rough notes-to-self about "
    "things they don't know yet — and turn each one into searchable learning "
    "points, so their existing Anki deck can be searched for cards that already "
    "cover it.\n\n"

    "THESE ARE NOT POLISHED LEARNING OBJECTIVES. Expect shorthand, typos, "
    "abbreviations, fragments, trailing markup ('- [ ] Baclofen'), question "
    "marks, and arrows meaning 'leads to'. Read through all of that to the "
    "clinical intent. 'Lugols iodine is K and I mixed. can be sued for wolff "
    "chaikoff' means Lugol's iodine (potassium iodide) and the Wolff-Chaikoff "
    "effect — 'sued' is a typo for 'used'.\n\n"

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

    "STEP 2 — DECOMPOSE TO MATCH THE SCOPE. Split the gap into learning points, "
    "each ONE idea a single card could test:\n"
    "  narrow  → exactly 1 point. Do not expand it. The student wrote one fact; "
    "finding cards on the surrounding topic is noise they did not ask for.\n"
    "  focused → 1 to 3 points.\n"
    "  broad   → 3 to 7 points that between them cover the entity properly: for "
    "a disease, its mechanism, features, diagnosis and management; for a drug, "
    "its mechanism, indications, key adverse effects and monitoring. Only mint a "
    "point a well-taught student would actually be examined on.\n\n"

    "STEP 3 — SET A CARD TARGET. \"cards\" is how many existing deck cards "
    "should surface for the WHOLE gap. BE STINGY — every card past the point "
    "the student needs is one they have to read and reject:\n"
    "  narrow  → 1. One assertion is one card. Use 2 ONLY if the fact has two "
    "genuinely separate halves that no single card could test together.\n"
    "  focused → 3-5.\n"
    "  broad   → 8-14, and only at the top of that range for a large disease "
    "with a real management arm.\n"
    "Scale it to how much there genuinely is to know, not to a formula.\n\n"

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

    "TAGGING — classify each point into {\"system\", \"subsystem\", \"topic\"}. "
    "PascalCase or snake_case, no spaces, no '::'. TOPIC IS THE SPECIFIC "
    "CLINICAL ENTITY — the drug, disease or structure — because it searches the "
    "tag tree on its own. A cross-cutting theme is NEVER a topic: 'mechanism', "
    "'treatment', 'adverse_effects' are ASPECTS. Put the entity in topic and let "
    "the aspect live in the terms. Leave a level empty rather than guessing, and "
    "never use 'General', 'Misc' or 'Other'.\n\n"

    "CONFIDENCE — set \"confidence\" to \"low\" and explain in \"note\" when a "
    "gap is genuinely unreadable ('Monoamines???' — which monoamines, and what "
    "about them?). STILL return your best-guess points for it: the student "
    "reviews and edits everything before it runs, so a rough guess they can fix "
    "beats an empty row. Reserve low confidence for real ambiguity, not for "
    "shorthand you can decode.\n\n"

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

    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per gap): one plain sentence naming what the student wants to "
    "know, instruction verbs and list markers stripped.\n"
    "- concept (per point): one plain sentence stating the testable fact.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
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

    "THESE ARE NOT POLISHED LEARNING OBJECTIVES. Expect shorthand, typos, "
    "abbreviations, fragments, trailing markup ('- [ ] Baclofen'), question "
    "marks, and arrows meaning 'leads to'. Read through all of that to the "
    "clinical intent. 'Lugols iodine is K and I mixed. can be sued for wolff "
    "chaikoff' means Lugol's iodine (potassium iodide) and the Wolff-Chaikoff "
    "effect — 'sued' is a typo for 'used'.\n\n"

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

    "STEP 2 — DECOMPOSE INTO WRITEABLE POINTS. Split the gap into learning "
    "points, each ONE idea that ONE card could test:\n"
    "  pinpoint → exactly 1 point. Do not expand it. The student wrote one fact; "
    "cards on the surrounding topic are noise they did not ask for.\n"
    "  core     → 2 to 4 points.\n"
    "  wide     → 5 to 8 points that between them cover the entity properly: for "
    "a disease, its mechanism, features, diagnosis and management; for a drug, "
    "its mechanism, indications, key adverse effects and monitoring.\n"
    "  deep     → 5 to 8 points, chosen so the set reaches mechanism and "
    "distinction, not just more headlines.\n"
    "A POINT IS A TESTABLE CLAIM, NOT A SEARCH QUERY OR A HEADING. "
    "'Graves: management' is a heading; 'Thionamides are first-line medical "
    "therapy in Graves, with carbimazole preferred outside the first trimester' "
    "is a point.\n"
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

    "STEP 3 — SET A CARD BUDGET. \"cards\" is how many NEW cards to write for "
    "the WHOLE gap. Roughly one to three per point:\n"
    "  pinpoint → 1. One assertion is one card.\n"
    "  core     → 4-6.\n"
    "  wide     → 10-14.\n"
    "  deep     → 18-26.\n"
    "The student reviews every card before any of it is added, so an extra card "
    "is cheap and a point left uncovered is a gap that stays open. But the "
    "number is A CEILING TO AIM AT, NOT A QUOTA TO FILL: a point that a single "
    "card fully tests gets a single card, and a gap with genuinely little in it "
    "gets a small number whatever its breadth bucket says. Padding to reach the "
    "bucket's number is worse than coming in under it.\n\n"

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

    "TAGGING — classify each point into {\"system\", \"subsystem\", \"topic\"}. "
    "PascalCase or snake_case, no spaces, no '::'. TOPIC IS THE SPECIFIC "
    "CLINICAL ENTITY — the drug, disease or structure. A cross-cutting theme is "
    "NEVER a topic: 'mechanism', 'treatment', 'adverse_effects' are ASPECTS. Put "
    "the entity in topic. Leave a level empty rather than guessing, and never use "
    "'General', 'Misc' or 'Other'.\n\n"

    "CONFIDENCE — set \"confidence\" to \"low\" and explain in \"note\" when a "
    "gap is genuinely unreadable ('Monoamines???' — which monoamines, and what "
    "about them?). STILL return your best-guess points: the student reviews and "
    "edits everything before anything is generated, so a rough guess they can fix "
    "beats an empty row. Reserve low confidence for real ambiguity, not for "
    "shorthand you can decode.\n\n"

    "OUTPUT — a JSON array with ONE object per gap, in the order given, using "
    "the \"n\" you were given. \"depth\" is \"headline\" or \"mechanism\":\n"
    '[{"n": 1, "concept": "Graves disease: pathogenesis, features, '
    'investigation and treatment", "breadth": "deep", "depth": "mechanism", '
    '"cards": 20, "confidence": "high", "note": "", "constraints": [], "points": ['
    '{"concept": "Graves disease is caused by IgG autoantibodies that stimulate '
    'the TSH receptor", "system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["Graves TSI", "Graves autoantibody"]}, '
    '{"concept": "Thionamides inhibit thyroid peroxidase, and propylthiouracil '
    'additionally blocks peripheral T4 to T3 conversion", '
    '"system": "Endocrine", "subsystem": "Thyroid", '
    '"topic": "graves_disease", "terms": ["carbimazole peroxidase", '
    '"propylthiouracil conversion"]}]}]\n\n'

    "RULES:\n"
    "- Emit one object for EVERY gap you were given, in order. Never merge two "
    "gaps, never drop one, never invent one.\n"
    "- concept (per gap): one plain sentence naming what the student wants to "
    "know, instruction verbs and list markers stripped.\n"
    "- concept (per point): one plain sentence stating the testable claim, "
    "specific enough that a card could be written from it alone.\n"
    "- terms: 2-4 strings per point, mostly two words.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
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

def _kg_prompt_line(kg: dict, lock: dict | None = None) -> str:
    """One gap as the model sees it: the title, plus any notes the student
    attached (which often carry the actual specifics the title elides).

    `lock` is a breadth/card count the student set BEFORE asking for a reading.
    It's stated in the prompt rather than only enforced afterwards, so the
    decomposition comes back at the right size — locking "core" after the fact
    would otherwise leave 14 points attached to a 4-card gap."""
    from . import store as kg_store
    title = str(kg.get("title") or "").strip()
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
    notes = kg_store.field(kg, "notes")
    if notes:
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", notes)).strip()
        if flat:
            bits.append(f"(their notes: {flat[:300]})")
    for level in ("system", "subsystem", "topic"):
        v = kg_store.field(kg, level)
        if v:
            bits.append(f"[{level}: {v}]")
    return " ".join(bits)


def triage(kgs: list[dict], *, model: str | None = None) -> list[KGPlan]:
    """Read a batch of knowledge gaps in ONE AI call.

    Raises ExtractionError on a hard provider failure so the panel can show the
    classified reason (same contract as extract_from_objectives, which is what
    lets run_pipeline treat the two branches identically).

    Runs on whatever thread the caller provides; does no Qt or collection work.
    """
    kgs = [k for k in kgs if isinstance(k, dict) and str(k.get("title") or "").strip()]
    if not kgs:
        raise ExtractionError("empty", "No knowledge gaps to read.")

    prompt = "\n".join(f"{i}. {_kg_prompt_line(k)}" for i, k in enumerate(kgs, 1))

    try:
        reply = core_api.ask_claude_json(
            prompt=prompt,
            system=_TRIAGE_SYSTEM,
            # A broad gap can mint 7 points each with terms and tag levels, so
            # this is budgeted per-gap at the BROAD cost, not the average — a
            # truncated JSON array is a total parse loss, and a batch of drug
            # names is exactly the case that would hit it.
            max_tokens=max(4000, len(kgs) * 700),
            model=model,
            show_errors=False,
            timeout=300,
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        log.error(f"kg triage failed [{code}]: {e}")
        raise ExtractionError(code, friendly) from e
    except Exception as e:
        log.error(f"kg triage failed: {e}")
        raise ExtractionError("unknown", f"Couldn't read those knowledge gaps: {e}") from e

    if reply is None:
        raise ExtractionError("empty", "The AI returned nothing for those gaps.")

    plans = parse_reply(reply, kgs)
    for p in plans:
        log.trace(f"KG {p.kg_id}: scope={p.scope} cards={p.cards} "
                  f"points={len(p.points)} | {p.title[:60]}")
    return plans


def triage_create(kgs: list[dict], *, model: str | None = None,
                  locks: dict | None = None) -> list[KGPlan]:
    """Read a batch of knowledge gaps for GENERATION, in ONE AI call.

    The sibling of triage(): same call shape, same error contract, same parsing
    — a different prompt and a different dial. See _CREATE_SYSTEM for why the
    two are separate rather than one function with a flag.

    Points come back WITHOUT the terms requirement: a point that can't be
    searched can still be written about, and the terms only matter if the user
    turns on the optional deck/tag coverage check.

    Pure: one AI call, no Qt, no mw.col. Safe on a worker thread.
    """
    kgs = [k for k in kgs if isinstance(k, dict) and str(k.get("title") or "").strip()]
    if not kgs:
        raise ExtractionError("empty", "No knowledge gaps to read.")

    locks = locks or {}
    prompt = "\n".join(
        f"{i}. {_kg_prompt_line(k, locks.get(str(k.get('id') or '')))}"
        for i, k in enumerate(kgs, 1))

    try:
        reply = core_api.ask_claude_json(
            prompt=prompt,
            system=_CREATE_SYSTEM,
            # Budgeted per-gap at the DEEP cost, not the average: a deep gap
            # mints 8 points each carrying a full-sentence concept, terms and
            # three tag levels, and a truncated JSON array is a total parse loss.
            # Slightly roomier than triage()'s 700 because the point concepts
            # here are written to be card-ready, so they're longer.
            max_tokens=max(4000, len(kgs) * 850),
            model=model,
            show_errors=False,
            timeout=300,
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        log.error(f"kg triage (create) failed [{code}]: {e}")
        raise ExtractionError(code, friendly) from e
    except Exception as e:
        log.error(f"kg triage (create) failed: {e}")
        raise ExtractionError("unknown", f"Couldn't read those knowledge gaps: {e}") from e

    if reply is None:
        raise ExtractionError("empty", "The AI returned nothing for those gaps.")

    plans = parse_reply(reply, kgs, flow="create", require_terms=False,
                        locks=locks)
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
               else "core" if n_points <= 4
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
