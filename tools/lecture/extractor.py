# Lecture → learning points.
#
# Turns ingested slides into atomic, searchable concepts. Each point carries the
# Anki search terms and the hierarchical tag path needed to find cards for it,
# all produced in the SAME round-trip (via tools/kg-style object instructions)
# — a 60-slide lecture is ~15 AI calls, not 60+.
#
# Deliberately free of threading and Qt: the panel owns the loop and decides
# how to run it (background worker for a real provider, inline for the manual
# copy/paste provider). That keeps this module testable outside Anki.

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...core import api as core_api, log
from .ingest import Lecture, Segment


class ExtractionError(Exception):
    """A hard provider failure during extraction (out of usage, timeout, no
    provider) — as opposed to a batch that merely came back empty, which is a
    `[]` return. Carries a classified `code` and a `friendly`, ready-to-show
    message so the panel can tell the user WHAT broke instead of a generic
    'nothing came back'."""

    def __init__(self, code: str, friendly: str):
        super().__init__(friendly)
        self.code = code
        self.friendly = friendly


# A whole lecture below this many characters of prompt text goes to ONE call.
# Measured: a real 44-page pharmacology deck is 19,633 chars (~5k tokens), so the
# common case fits with room to spare. Batching only exists for the genuinely
# large document, because splitting costs correctness: independent batches can't
# consolidate globally and re-send the system prompt every call.
SINGLE_CALL_MAX_CHARS = 60_000

# Four segments per call, sliding by three, so consecutive batches share one
# segment of context — a concept introduced on slide 4 and completed on slide 5
# is seen whole by at least one batch. Only used ABOVE SINGLE_CALL_MAX_CHARS.
#
# The overlap is a liability as well as a safety net: it shows each seam segment
# to two batches, which produces two *paraphrases* of one fact, and merge()'s
# string-identity key cannot collapse those. Above the threshold that's the price
# of not truncating; below it, one call avoids the problem entirely.
SEGMENTS_PER_BATCH = 4
BATCH_OVERLAP = 1

# Default whole-lecture target. There is no correct number — it's a user setting
# (config `lecture.target_points`); this is only the fallback. A TARGET, never a
# cap: overflow is kept, because silently dropping the 26th point would surface
# it later as a fake gap in the user's deck.
DEFAULT_TARGET_POINTS = 40

# "Detail" is the user-facing preset for target_points: how finely the lecture is
# broken into topics. Focused = a tight checklist, Fine = every enumerable member
# its own line. The run panel shows these three; the number is derived from them,
# so nobody has to guess "how many learning points". Fine == DEFAULT_TARGET_POINTS.
DETAIL_TARGETS = {"focused": 20, "standard": 30, "fine": 40}
DEFAULT_DETAIL = "fine"


def detail_for_points(points: int) -> str:
    """Map a raw target_points back to the nearest Detail preset, so the panel
    can show the right choice even when the value came from the advanced field."""
    best, bestd = DEFAULT_DETAIL, 1 << 30
    for name, n in DETAIL_TARGETS.items():
        if abs(n - points) < bestd:
            best, bestd = name, abs(n - points)
    return best

# Vision is per-segment and expensive. Caps how many images ride along on one
# call — the text segments carry the thread for the rest.
MAX_IMAGES_PER_BATCH = 4
MAX_IMAGES_SINGLE_CALL = 14

_SYSTEMS = (
    "Cardio, Neuro, Endo, GI, Resp, Renal, Heme, MSK, Derm, Repro, Psych, ID, "
    "Onc, Pharm, Stats, Genetics, Biochem, Immuno"
)

_EXTRACT_SYSTEM_TMPL = (
    "You read a medical teaching document and extract the TESTABLE learning "
    "points in it, then produce the Anki search terms that would find existing "
    "cards for each one.\n\n"
    "You are NOT writing cards. You are finding what the material teaches so an "
    "existing, already-vetted deck can be searched for it.\n\n"
    "THE INPUT:\n"
    "@@INPUT_DESC@@"
    "Each chunk starts with a header like '## Slide 4', '## Page 12' or "
    "'## Section 3: Heart Failure'. The NUMBER in that header is what you cite "
    "in `refs`.\n\n"
    "HOW MANY POINTS — READ THIS TWICE:\n"
    "@@BUDGET_RULE@@"
    "This is a BUDGET, and it is the hardest part of this task. Material always "
    "*looks* like it contains more than it teaches. A 40-slide lecture carries "
    "roughly 20–30 things worth testing, NOT one per bullet. If your list is "
    "much longer than the target, you are transcribing, not extracting.\n"
    "Spend the budget on what an examiner would actually ask. When two candidate "
    "points would send a student to the SAME cards, they are ONE point.\n"
    "TWO things legitimately run the count up, and only these: an enumerable "
    "clinical set (see 'ENUMERATE THE MEMBERS' below) — five stroke territories "
    "are five points even under a tight budget, because dropping four of them is "
    "a content gap, not economy — and a bundled aspect list (see 'ONE IDEA PER "
    "POINT' below), where a single bullet's distinct, separately-testable aspects "
    "each earn their own point. Consolidate everywhere else; never collapse a "
    "class or an aspect list whose members find different cards.\n\n"
    "WHAT COUNTS AS A LEARNING POINT:\n"
    "- One fact, mechanism, association, or decision rule that could be tested "
    "on its own. 'Aortic stenosis causes a crescendo-decrescendo murmur' is one "
    "point; 'the cardiac exam' is not.\n"
    "- CONSOLIDATE by default. Split a compound bullet ONLY when the parts would "
    "need DIFFERENT search terms. 'Heart failure: reduced EF, raised BNP, treat "
    "with ACEi' is ONE point about heart failure — 'heart failure BNP' and "
    "'heart failure ACEi' are terms on that one point, not three points. But "
    "'Amphotericin: nephrotoxicity and infusion reactions' IS two points, "
    "because 'amphotericin nephrotoxicity' and 'amphotericin infusion' find "
    "different cards.\n"
    "- The test is always: WOULD THESE FIND THE SAME CARDS? Same cards -> one "
    "point with several terms. Different cards -> separate points.\n"
    "- ONE IDEA PER POINT — SPLIT A BUNDLED ASPECT LIST. A bullet that names a "
    "structure or topic and then several DISTINCT aspects of it — 'anatomy of the "
    "cerebral cortex (lobes, layers, and cytoarchitecture)', 'the ventricular "
    "system: structure, CSF production and circulation' — is NOT one point. Lobes, "
    "layers and cytoarchitecture sit on different cards, so that is THREE points, "
    "each anchored on its own aspect ('cortex lobes', 'cortical layers', 'Brodmann "
    "areas'). A parenthetical or comma list of aspects, or several nouns joined by "
    "'and', is the loudest signal that a bullet bundles separately-testable ideas: "
    "split until each point is ONE idea a single card could test. This is the "
    "same-cards test again — it splits ideas that find DIFFERENT cards, while "
    "consolidation (above) still fuses terms that find the SAME card.\n"
    "- ENUMERATE THE MEMBERS OF A NAMED CLASS. When an objective or slide names a "
    "CLASS whose members a student must know INDIVIDUALLY and which live on "
    "DIFFERENT cards, emit one point PER MEMBER — not a single point for the "
    "class. This is the same-cards test APPLIED, not an exception to it: the "
    "anterior, middle and posterior cerebral-artery stroke syndromes sit on "
    "different cards, so 'territorial stroke syndromes' is several points (ACA "
    "syndrome, MCA syndrome, PCA syndrome, lacunar, brainstem/Wallenberg), each "
    "anchored on its own entity — NOT one point whose terms happen to name only "
    "MCA. The same applies to the individual cranial-nerve palsies, the four "
    "hypersensitivity types, the shock subtypes, the named heart murmurs, the "
    "muscular dystrophies, the vasculitides. A single 'types of X' point that "
    "quietly covers only its loudest member is the exact failure this prevents — "
    "every member the material actually teaches earns its own point so none is "
    "silently missed. This does NOT license splitting a genuine single fact: "
    "expand a class only when its members are each independently examinable and "
    "card-distinct.\n"
    "- MECHANISM OF ACTION IS ALWAYS ITS OWN POINT. For every drug or drug class "
    "the material teaches, its mechanism / how it works is a SEPARATE point from "
    "its clinical use, its adverse effects, and its indications — they find "
    "different cards (the mechanism card is a distinct, high-yield, examiner-"
    "favourite card). Do NOT fold 'SSRIs block serotonin reuptake' into a general "
    "'SSRIs are first-line antidepressants' point. Give mechanism its own point "
    "with receptor / transporter / enzyme terms: 'SSRI reuptake', 'mirtazapine "
    "alpha-2', 'agomelatine melatonin', 'venlafaxine SNRI'. This applies even "
    "when the mechanism is only one clause on the slide — consolidation must "
    "never swallow it.\n"
    "- SKIP course admin, references, acknowledgements, timetables, assessment "
    "info, title and section-divider chunks, and anything that is a heading with "
    "no content. Return an empty list for input that is entirely that.\n"
    "- Prefer what an examiner would test over background colour. Drop minor "
    "asides rather than spend budget on them.\n\n"
    "@@FIDELITY_RULE@@"
    "@@YIELD_RULE@@"
    "LEARNING OBJECTIVES ARE IN SCOPE — DO NOT SKIP THEM:\n"
    "Some inputs are nothing but a learning-objective list ('Describe the "
    "mechanisms of gastric acid production by oxyntic cells'). These are the "
    "syllabus, and the student wants the existing cards that cover them. Turn "
    "each objective into a point about its SUBJECT MATTER, not about the "
    "instruction: 'Describe the mechanisms of gastric acid production by oxyntic "
    "cells' becomes concept 'Mechanisms of gastric acid production by oxyntic "
    "(parietal) cells' with terms like 'parietal cell acid', 'gastric H+ K+ "
    "ATPase'. Drop the verb ('describe', 'understand', 'outline') — it is not "
    "part of the fact. A broad objective covering several distinct things splits "
    "into one point each — but by the same same-cards test as any compound "
    "bullet, not automatically.\n\n"
    "ATTACHED IMAGES — THE CONTENT IS IN THE LAYOUT:\n"
    "Some chunks come with an attached image because their text layer is empty "
    "or because they carry a diagram, table or figure. For those, READ THE IMAGE, "
    "not just the text — the text layer of a diagram is a meaningless bag of "
    "words, and the actual teaching is in the spatial arrangement. A two-column "
    "matching table teaches every row's pairing (e.g. each drug to its class); "
    "extract those pairings, which the text alone cannot express.\n"
    "CRITICAL: a slide showing a BLANK exercise, a quiz prompt, or unanswered "
    "matching columns teaches NOTHING — skip it. Only the ANSWERED version is a "
    "source of learning points. Decks routinely show the blank first and the "
    "completed version a slide or two later; their text layers look identical, "
    "so you must look at the image to tell them apart.\n\n"
    "HOW ANKI SEARCH WORKS (critical — most failures come from ignoring this):\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. So a 3–4 word term like 'psoriasis cell-mediated immunity' almost "
    "always returns ZERO, because no single card contains all those words. Keep "
    "most terms to the concept anchor PLUS ONE discriminating keyword — usually "
    "two words (e.g. 'psoriasis IL-17', 'psoriasis Th17').\n\n"
    "TERM LENGTH IS A HARD CONSTRAINT, NOT A PREFERENCE:\n"
    "- TWO words is the target and the default. Aim for most terms to be two.\n"
    "- THREE words only when those exact words plainly co-occur on one card.\n"
    "- FOUR OR MORE words is always wrong. It returns nothing. Rewrite it.\n"
    "A long anatomical or descriptive phrase is the usual culprit — cut it to "
    "its most distinctive word plus the anchor:\n"
    "  'lower oesophageal sphincter GORD'  -> 'GORD sphincter' or 'GORD LOS'\n"
    "  'parietal cell proton pump'         -> 'parietal proton' or 'parietal ATPase'\n"
    "  'small intestine bacterial overgrowth' -> 'SIBO malabsorption'\n"
    "Before returning, re-read every term and count its words. If any has four "
    "or more, rewrite it.\n\n"
    "USE THE DECK'S VOCABULARY, NOT THE SOURCE'S PROSE:\n"
    "Cards are written in terse exam shorthand, not slide or handout prose. "
    "Approach each concept through the angles a deck actually uses — the key "
    "cytokine/mediator, the classic drug, the named sign, the tightly-linked "
    "condition. For material on psoriasis pathogenesis, strong terms are "
    "'psoriasis IL-17', 'psoriasis Th17', 'psoriasis secukinumab' — far more "
    "likely to hit than 'cell-mediated immunity' or 'pathogenesis T cells', "
    "which are prose, not card text.\n\n"
    "ANCHOR ON THE FULL NAME THE CARD USES — NOT THE ABBREVIATION:\n"
    "Anki matches literal tokens; it will NOT expand 'PD' to 'Parkinson' or know "
    "'mydriasis' means 'dilated pupil'. Cards spell disease and anatomy names "
    "OUT. Measured on a real deck: 'Parkinson' is on 343 cards but the bare token "
    "'PD' on 24 — an anchor of 'PD ...' throws away ~93% of the relevant cards "
    "before anything else runs; likewise 'MSA' (2 cards) vs 'Multiple System "
    "Atrophy' (5), 'CN3' (2) vs 'oculomotor'/'CN III'/'third nerve' (dozens). So "
    "the ENTITY anchor of every term MUST be the full name — 'Parkinson "
    "rigidity', 'multiple system atrophy autonomic', 'oculomotor herniation'. A "
    "short disease/anatomy abbreviation (PD, MSA, PSP, CBD, CN3) may appear ONLY "
    "as an EXTRA, separate term, never as the sole anchor: each term is one "
    "AND-query, so 'keep both forms' means TWO terms, ['Parkinson rigidity', 'PD "
    "rigidity']. Keep a token VERBATIM only when the deck itself prints it that "
    "way — molecular/receptor/genetic tokens (IL-17, TNF, HLA-B27, D2) and "
    "universally-abbreviated entities (HIV, COPD, ACE). A multi-word proper name "
    "counts as the single anchor unit, so 'multiple system atrophy autonomic' is "
    "fine — what returns zero is 3–4 words of PROSE. Prefer the common clinical "
    "word the card uses over the technical synonym: 'dilated pupil' not "
    "'mydriasis', 'cogwheel' (not 'lead-pipe') for Parkinson rigidity.\n\n"
    "TAGGING:\n"
    'Classify each point into {"system", "subsystem", "topic"}. system is one of: '
    f"{_SYSTEMS}. Use PascalCase or snake_case, no spaces, no '::', no slashes. "
    "Never use a generic placeholder like 'General', 'Misc' or 'Other' — return "
    "an empty string for a level that is genuinely unclear.\n"
    "TOPIC IS THE SPECIFIC CLINICAL ENTITY THE POINT IS ABOUT — the drug, "
    "disease, organism, or anatomical structure — because it is used on its own "
    "to search your tag tree, and a tag branch is named after an entity, not a "
    "theme. So topic for 'Mirtazapine causes weight gain' is 'mirtazapine', and "
    "for 'Lithium has a narrow therapeutic index' is 'lithium'.\n"
    "A CROSS-CUTTING THEME IS NEVER A TOPIC. 'adverse_effects', 'side_effects', "
    "'mechanism', 'pharmacokinetics', 'treatment', 'diagnosis', 'pregnancy', "
    "'pediatric', 'complications', 'prevention', 'epidemiology' describe an ASPECT "
    "of an entity, not the entity — as a topic they match a huge slice of the "
    "deck and are useless for finding this point's cards. Put the entity in "
    "topic and let the aspect live in the search terms. 'Antidepressants in "
    "pregnancy' → topic 'antidepressants' (or the specific drug), NOT "
    "'pregnancy'. If the point genuinely has no single entity, leave topic "
    "empty rather than filling it with a theme.\n\n"
    "@@COMPANION_RULE@@"
    "OUTPUT — a JSON array of objects, exactly:\n"
    '[{"concept": "...", "refs": [4], "system": "Cardio", "subsystem": "Valvular", '
    '"topic": "aortic_stenosis", "terms": ["aortic stenosis murmur", "aortic stenosis syncope"], '
    '"src": ""}]\n\n'
    "RULES:\n"
    "- concept: one plain sentence naming the testable fact. No chunk numbers in it.\n"
    "- refs: the number(s) from the '## ...' headers this point came from.\n"
    '- src: "" for a point from the teaching material. Only ever "notes" for a '
    "point drawn from the user's own notes (see below, when that section is "
    "present at all). note_refs: [] unless src is \"notes\".\n"
    "- terms: 2–4 search strings, mostly two words. Quality over quantity.\n"
    "- Do not pad the list to reach the target — under is fine, and admin-only "
    "input returns []. The target is a ceiling to aim at, not a quota to fill.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
)


# Injected into the extract prompt when the user's "High-yield only" toggle is
# on. Targets the three shapes a real lecture pads testable content with — and
# which produced the low-yield half of the stroke-lecture run: bare statistics,
# emerging/editorial asides, and enumerated lists that the deck only ever stores
# as individual-fact cards. Off, the placeholder is "" and the prompt is
# byte-identical to before this feature.
_YIELD_RULE = (
    "LOW-YIELD FILTER — DO NOT EXTRACT THESE AS POINTS:\n"
    "A lecture pads testable content with things that make poor flashcards. A "
    "point must be a fact a card could TEST, not merely something a lecturer "
    "SAID. Drop, do not emit:\n"
    "- A BARE STATISTIC or epidemiological figure as the point itself: "
    "'30–50% of referrals are mimics', '80% of strokes are preventable', "
    "'DWI-positive in ~30%'. If a number decorates a real fact, KEEP the fact "
    "and DROP the number — 'seizure is a common stroke mimic' is a point; "
    "'seizure is 21% of mimics' is not.\n"
    "- EMERGING / EXPERIMENTAL / INVESTIGATIONAL / FUTURE therapies and "
    "'state of the field' asides: 'X is an emerging therapy', 'wearables are "
    "expanding the population…', 'trials are ongoing'. A student's deck tests "
    "established practice, not the lecturer's outlook.\n"
    "- An ENUMERATED LIST as ONE point (a workup panel, a risk-factor list, a "
    "differential). The deck stores these as individual-fact cards, never as "
    "'the list'. Extract the 1–2 HIGHEST-YIELD items as their own points, or "
    "drop it — never emit 'the basic workup includes CT, ECG, echo, Holter, "
    "bloods…' as one searchable point.\n"
    "The test: would an examiner write a question whose answer is exactly this? "
    "If not, drop it.\n\n"
)


# Injected at @@FIDELITY_RULE@@. OFF by default (the user's "enrich" toggle is
# off): the lecture is the source of truth and the model must not backfill
# canonical facts the slides omit — this is the fix for the run that invented a
# regional-V/Q (apex/base gravity) point the Oxygen lecture never taught. ON:
# the model may supplement with closely-related canonical facts, a deliberate,
# user-chosen behaviour. The flag is in the cache key, so flipping it re-reads.
_GROUND_RULE = (
    "STAY INSIDE THE LECTURE — IT IS THE SOURCE OF TRUTH:\n"
    "Extract ONLY what THIS document actually teaches. Do NOT add standard, "
    "canonical or 'they'd be expected to know it' facts the material omits, "
    "however tempting: if the slides cover the MCA stroke territory but never "
    "mention the ACA, do not add an ACA point; if a figure shows the V/Q "
    "relationship but the lecture never states the apex-to-base gradient, do not "
    "supply it. Every point must trace to specific content in the chunks you were "
    "given — cite it in `refs`. Enumerating the members of a class (above) still "
    "applies ONLY to members the material itself NAMES; never complete a class "
    "the lecture only partly lists. When unsure whether something is really in "
    "the lecture, leave it out.\n\n"
)
_ENRICH_RULE = (
    "ENRICH BEYOND THE LECTURE (enabled by the user):\n"
    "You MAY add closely-related canonical facts the material implies but does "
    "not state — the remaining members of a class it only partly lists, the "
    "textbook value behind a figure, the classic association a slide gestures at "
    "— as long as they belong to a topic the lecture actually teaches. Enrichment "
    "SUPPLEMENTS the lecture; it does not replace it with a textbook chapter, so "
    "stay on the topics the material raises and keep it tight.\n\n"
)


# Injected at @@COMPANION_RULE@@ when the user handed over their own notes or a
# transcript. Empty otherwise, so a run without notes sends byte-identical text
# to before this feature existed.
#
# The cap is the whole design. His steer: "slides should make up 95% of the LPs.
# transcript should really only be supplemental, especially those little gems."
# A transcript is 90 minutes of speech and will happily generate fifty plausible
# points that drown the lecture; a hard ceiling is what keeps the second document
# supplemental instead of dominant.
_COMPANION_TMPL = (
    "THE USER'S OWN NOTES — SUPPLEMENTAL ONLY:\n"
    "After the teaching material you will find a '## Your notes' section: what "
    "the user wrote down in the lecture, their full written notes, or a "
    "transcript of what was said. Each line is numbered '[note 3]'.\n"
    "The TEACHING MATERIAL is the spine and supplies almost every point. The "
    "notes are there for the handful of things the material never taught — a "
    "threshold the lecturer said out loud, an exam steer, a caveat that only "
    "exists in the spoken lecture.\n"
    "So: emit a point from the notes ONLY when it teaches something TESTABLE "
    "that no chunk of the teaching material covers. If a note merely repeats, "
    "rephrases or expands on something already in the material, it is NOT a new "
    "point — the material's own point already covers it.\n"
    "AT MOST @@COMPANION_CAP@@ point(s) may come from the notes, and usually "
    "fewer. This is a hard ceiling, not a target: zero is a perfectly good "
    "answer for a set of notes that just echoes the slides.\n"
    'Mark every such point with "src": "notes" and cite the note number(s) in '
    "`note_refs`, leaving `refs` empty. `refs` is only ever for '## ...' chunk "
    "numbers from the teaching material — never put a note number in it.\n\n"
)


def companion_cap(target_points: int, share: float) -> int:
    """How many points the user's notes may contribute.

    A floor of 2 rather than a bare proportion: on a short lecture the
    percentage rounds to zero, and the one thing he wrote down is exactly the
    thing worth keeping.
    """
    try:
        share = float(share)
    except (TypeError, ValueError):
        share = 0.12
    share = min(max(share, 0.0), 1.0)
    return max(2, round(int(target_points or 0) * share))


def build_extract_system(target_points: int, *, whole_document: bool,
                         high_yield: bool = False, enrich: bool = False,
                         companion_cap_points: int = 0) -> str:
    """The extract prompt, parameterised by the user's point budget.

    `target_points` is a whole-lecture number. When the document is split, each
    call only sees its own slice and must be given a proportional share —
    handing every batch the full target is what let 15 batches return 104 points.

    `high_yield` injects the low-yield filter (the user's panel toggle). When
    off, the @@YIELD_RULE@@ placeholder resolves to "" and the prompt is exactly
    what it was before the toggle existed — which is why the toggle is in the
    cache key rather than silently changing cached extractions.
    """
    if whole_document:
        input_desc = (
            "A complete lecture deck, a PDF export of one, a Word handout, or "
            "notes. You are seeing ALL of it, so you can and must deduplicate "
            "across the whole document: one point per distinct fact, citing "
            "every chunk it appears on.\n"
        )
        budget_rule = (
            f"Return about {target_points} learning points for this document — "
            f"the single most testable {target_points}. Fewer is fine.\n"
        )
    else:
        input_desc = (
            "A SLICE of a larger teaching document — you are not seeing all of "
            "it. Extract only what this slice teaches; a later pass merges the "
            "slices.\n"
        )
        budget_rule = (
            f"Return at most {target_points} learning points for this slice, and "
            "usually fewer. Most slices carry only two or three.\n"
        )
    companion_rule = ""
    if companion_cap_points > 0:
        companion_rule = _COMPANION_TMPL.replace(
            "@@COMPANION_CAP@@", str(companion_cap_points))
    # str.replace, not str.format — the template contains literal JSON braces
    # in its output example, which format() reads as fields.
    return (_EXTRACT_SYSTEM_TMPL
            .replace("@@INPUT_DESC@@", input_desc)
            .replace("@@BUDGET_RULE@@", budget_rule)
            .replace("@@FIDELITY_RULE@@", _ENRICH_RULE if enrich else _GROUND_RULE)
            .replace("@@YIELD_RULE@@", _YIELD_RULE if high_yield else "")
            .replace("@@COMPANION_RULE@@", companion_rule))


@dataclass
class LearningPoint:
    concept: str
    refs: list[int] = field(default_factory=list)
    system: str = ""
    subsystem: str = ""
    topic: str = ""
    terms: list[str] = field(default_factory=list)
    # KG mode only (tools/kg/triage.py): the knowledge gap this point was
    # decomposed out of. A broad KG ("Graves + Tx") mints several points that
    # all carry the same kg_id, which is how the panel groups the results, tags
    # each KG's notes under ITS own tag, and marks the right KG done. Empty on
    # every file/LO point, so nothing else in the pipeline changes behaviour.
    kg_id: str = ""
    # "" for a point the teaching material taught, "notes" for one drawn from
    # the user's own companion notes/transcript. Purely informational — the
    # search, judge and allocator all treat the two identically; it exists so
    # the results screen can badge where a point came from, and so a run's
    # slide-vs-notes balance is visible rather than assumed.
    src: str = ""
    # Companion-note indices this point came from, when src == "notes". Kept
    # SEPARATE from `refs` rather than sharing it: `refs` holds slide numbers and
    # is what drives slide-image attachment, so a note index landing in there
    # would silently staple an unrelated slide onto the card.
    note_refs: list[int] = field(default_factory=list)

    @property
    def tag_levels(self) -> dict:
        """Shape expected by config.format_hierarchical_tag()."""
        return {"system": self.system, "subsystem": self.subsystem, "topic": self.topic}


@dataclass
class Batch:
    index: int              # 0-based, for progress reporting
    segments: list[Segment]
    whole_document: bool = False   # True when this batch IS the entire lecture

    @property
    def label(self) -> str:
        if not self.segments:
            return "empty"
        unit = self.segments[0].unit.lower()
        lo, hi = self.segments[0].number, self.segments[-1].number
        return f"{unit} {lo}" if lo == hi else f"{unit}s {lo}–{hi}"

    @property
    def image_segments(self) -> list[int]:
        """Segments with no text layer at all — vision is the only way to read them."""
        return [s.number for s in self.segments if not s.has_text]

    def vision_targets(self, imagery: set[int] | None = None) -> list[int]:
        """Segments worth sending as images: those with no text layer, PLUS those
        whose text layer exists but doesn't hold the content — diagrams, tables,
        matching exercises. A slide's text can look complete while carrying only
        a bag of words whose meaning lives in the layout, so `imagery` (detected
        from pixels by ingest) is not redundant with `has_text`."""
        mine = {s.number for s in self.segments}
        return sorted(set(self.image_segments) | (mine & (imagery or set())))

    def as_prompt(self) -> str:
        return "\n\n".join(s.as_prompt_block() for s in self.segments)


def build_batches(lecture: Lecture) -> list[Batch]:
    """Slice a lecture into batches — or, for anything of normal size, ONE batch
    holding the whole document.

    Segments with neither text nor notes are still included: they carry a
    placeholder line so the model knows something exists there, and they're what
    the vision pass targets.

    A single batch is strongly preferred: it lets the model deduplicate globally,
    removes the seam paraphrases the overlap creates, and sends the system prompt
    once. Splitting is a truncation defence for genuinely large input, nothing
    more — so it only kicks in past SINGLE_CALL_MAX_CHARS.
    """
    segments = lecture.segments
    if not segments:
        return []

    size = sum(len(s.as_prompt_block()) for s in segments)
    if size <= SINGLE_CALL_MAX_CHARS:
        return [Batch(index=0, segments=list(segments), whole_document=True)]

    log.warn(
        f"lecture: {size} chars exceeds the {SINGLE_CALL_MAX_CHARS}-char "
        f"single-call limit — splitting into batches, which loses global dedupe"
    )
    step = max(1, SEGMENTS_PER_BATCH - BATCH_OVERLAP)
    batches: list[Batch] = []
    start = 0
    while start < len(segments):
        chunk = segments[start:start + SEGMENTS_PER_BATCH]
        if chunk:
            batches.append(Batch(index=len(batches), segments=chunk))
        if start + SEGMENTS_PER_BATCH >= len(segments):
            break
        start += step
    return batches


def share_of_target(target_points: int, n_batches: int) -> int:
    """Per-batch point budget. The whole-lecture target divided across batches,
    with headroom (batches are uneven and some slices are pure admin) and a floor
    of 2 so a dense slice isn't gagged. Returns the target unchanged for a single
    batch, which by definition sees the whole document."""
    if n_batches <= 1:
        return max(1, target_points)
    return max(2, -(-target_points // n_batches) + 1)


def _stem_key(text: str) -> str:
    """Normalised identity for a concept, for cross-batch dedupe. Lowercase,
    alphanumerics only, first 80 chars — so 'Aortic stenosis causes syncope.'
    and 'aortic stenosis causes syncope' collapse to one point."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:80]


def _clean_terms(raw) -> list[str]:
    out: list[str] = []
    seen = set()
    for t in raw if isinstance(raw, list) else []:
        s = str(t).strip()
        if not s:
            continue
        # Anki treats these as search operators; a stray one silently changes
        # the query's meaning rather than erroring.
        s = s.replace('"', " ").replace("*", " ").replace(":", " ")
        s = re.sub(r"\s+", " ", s).strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out[:4]


def _clean_level(v) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(v or "").strip()).strip("_")
    # The model is told not to, but placeholders still leak through; an empty
    # level is dropped by format_hierarchical_tag, a 'General' one poisons the tag.
    if s.lower() in {"general", "misc", "other", "none", "unknown", "na"}:
        return ""
    return s


def parse_batch_reply(reply, batch: Batch) -> list[LearningPoint]:
    """Coerce one AI reply into learning points, dropping malformed entries
    rather than failing the batch — a single bad object shouldn't cost the
    other seven."""
    if not isinstance(reply, list):
        log.warn(f"lecture: batch {batch.index} reply was {type(reply).__name__}, not a list")
        return []

    valid_numbers = {s.number for s in batch.segments}
    points: list[LearningPoint] = []
    # NOT truncated to the target. The budget is a prompt instruction, not a
    # slice: cutting the list here would drop points the model judged worth
    # keeping, and matcher.is_gap would then report them as holes in the deck
    # that don't exist. An over-long list is a visible prompt failure; a silently
    # truncated one is an invisible lie.
    for item in reply:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept") or "").strip()
        if not concept:
            continue
        terms = _clean_terms(item.get("terms"))
        if not terms:
            # No terms means nothing to search — the point is unusable.
            log.warn(f"lecture: dropping '{concept[:40]}' — no usable terms")
            continue

        src = "notes" if str(item.get("src") or "").strip().lower() == "notes" else ""

        nums = []
        for n in (item.get("refs") or []):
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            # Models occasionally cite a slide from a neighbouring batch; keep
            # only what this batch actually saw so citations stay truthful.
            if n in valid_numbers:
                nums.append(n)
        if not nums and src != "notes":
            nums = [batch.segments[0].number]
        if src == "notes":
            # A notes point cites notes, not slides. Anything that leaked into
            # `refs` is dropped rather than trusted: a stray slide citation here
            # would attach that slide's image to the card.
            nums = []

        note_nums = []
        if src == "notes":
            for n in (item.get("note_refs") or []):
                try:
                    note_nums.append(int(n))
                except (TypeError, ValueError):
                    continue

        points.append(LearningPoint(
            concept=concept,
            refs=sorted(set(nums)),
            system=_clean_level(item.get("system")),
            subsystem=_clean_level(item.get("subsystem")),
            topic=_clean_level(item.get("topic")),
            terms=terms,
            src=src,
            note_refs=sorted(set(note_nums)),
        ))
    return points


# Two concepts whose significant words overlap at or above this are treated as
# the same point. Deliberately high, for the same reason matcher._DEDUPE_MIN_JACCARD
# is: medical phrasing shares heavy vocabulary, so a lower bar would fuse two
# genuinely different facts and lose one of them silently.
#
# The case this exists for, from the ICP/herniation run: the model returned
# "Cushing reflex is hypertension, bradycardia and irregular respiration" and
# "Cushing triad: raised BP, bradycardia, abnormal breathing" as separate
# points. They then competed for the same cards, and the budget spent two slots
# restating one fact.
_TWIN_MIN_JACCARD = 0.72

# Words carrying no identity — two concepts sharing only these are not twins.
_TWIN_STOP = {
    "the", "a", "an", "of", "in", "on", "to", "and", "or", "is", "are", "for",
    "with", "by", "as", "at", "from", "that", "this", "it", "its", "be", "can",
    "may", "which", "when", "than", "then", "into", "via", "due", "causes",
    "cause", "caused", "leads", "lead", "results", "result",
}


def _twin_tokens(text: str) -> set[str]:
    """The significant words of a concept, for near-duplicate comparison."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _TWIN_STOP}


def _twin_of(tokens: set[str], candidates: list[tuple[set[str], LearningPoint]]):
    """The already-kept point this one is a near-duplicate of, or None."""
    if not tokens:
        return None
    best, best_score = None, 0.0
    for other_tokens, point in candidates:
        if not other_tokens:
            continue
        inter = len(tokens & other_tokens)
        if not inter:
            continue
        score = inter / len(tokens | other_tokens)
        if score > best_score:
            best, best_score = point, score
    return best if best_score >= _TWIN_MIN_JACCARD else None


def merge(batch_results: list[list[LearningPoint]]) -> list[LearningPoint]:
    """Flatten batches into one ordered list, collapsing the duplicates the
    overlap deliberately creates. First occurrence wins; later citations are
    folded in so a point spanning a seam cites both segments.

    Two kinds of duplicate are collapsed here:

    1. IDENTICAL concepts (same _stem_key) — what batch overlap produces.
    2. NEAR-identical concepts — twins the model returns as separate points
       within a single batch, restating one fact in two phrasings. These are
       worse than harmless: each twin searches for the same cards, the judge
       scores the same cards for both, and allocate() hands the pair two budget
       slots for one fact. Merging keeps the LONGER concept, on the grounds
       that the more specific phrasing is the more useful search input.

    Every twin collapse is logged with both concepts, because this is the one
    place a real point can be lost to a threshold — and a silent loss here would
    surface much later as an unexplained missing card.
    """
    out: list[LearningPoint] = []
    by_key: dict[str, LearningPoint] = {}
    tokenised: list[tuple[set[str], LearningPoint]] = []
    twins = 0

    def _fold(into: LearningPoint, other: LearningPoint) -> None:
        into.refs = sorted(set(into.refs) | set(other.refs))
        into.note_refs = sorted(set(into.note_refs) | set(other.note_refs))
        for t in other.terms:
            if t.lower() not in {x.lower() for x in into.terms}:
                into.terms.append(t)

    for result in batch_results:
        for p in result:
            key = _stem_key(p.concept)
            if not key:
                continue
            existing = by_key.get(key)
            if existing is not None:
                _fold(existing, p)
                continue

            tokens = _twin_tokens(p.concept)
            twin = _twin_of(tokens, tokenised)
            if twin is not None:
                twins += 1
                log.info(f"lecture: merged twin points — kept "
                         f"'{twin.concept[:70]}', folded in '{p.concept[:70]}'")
                _fold(twin, p)
                # The longer phrasing usually carries the extra specificity that
                # makes a search work, so it wins the surviving concept.
                if len(p.concept) > len(twin.concept):
                    twin.concept = p.concept
                by_key[key] = twin
                continue

            by_key[key] = p
            tokenised.append((tokens, p))
            out.append(p)

    if twins:
        log.info(f"lecture: collapsed {twins} twin point(s) — "
                 f"{len(out)} distinct point(s) remain")
    return out


def enforce_companion_cap(points: list[LearningPoint], cap: int) -> list[LearningPoint]:
    """Hard-trim points drawn from the user's notes down to `cap`.

    The prompt asks for the ceiling, but a prompt is a request and a transcript
    is very persuasive material — and on a split document each batch is told the
    cap independently, so the totals can add up past it. This is the enforcement:
    it runs once over the whole point list, after extraction and merging.

    Earlier points win. There's no quality ordering to sort by at this stage, and
    document order is at least stable and explicable ("it kept the first three").
    Points from the teaching material are never touched.
    """
    if cap < 0:
        return points
    kept, dropped = [], 0
    seen = 0
    for p in points:
        if p.src == "notes":
            seen += 1
            if seen > cap:
                dropped += 1
                continue
        kept.append(p)
    if dropped:
        log.warn(f"lecture: notes produced {seen} point(s), capped at {cap} "
                 f"— dropped {dropped}")
    return kept


def extract_batch(batch: Batch, lecture: Lecture, *, model: str | None = None,
                  use_vision: bool = True, target_points: int = DEFAULT_TARGET_POINTS,
                  imagery: set[int] | None = None,
                  high_yield: bool = False, enrich: bool = False,
                  companion=None, companion_cap_points: int = 0) -> list[LearningPoint]:
    """One AI round-trip for one batch. Returns [] on any failure — the caller
    logs it and carries on, because losing a slice beats losing the lecture.

    `imagery` is the set of segment numbers that ingest found to carry pictures,
    diagrams or tables. Those go to vision even though they HAVE text, because
    their text layer doesn't hold their meaning.

    `companion` is the user's own notes (an ingest.CompanionText). It rides along
    with EVERY batch rather than being split across them: the notes are a single
    small document, and the rule the model has to apply — "only if the teaching
    material never covers it" — needs the notes and the material in the same
    call. The cap is per-call, so a split document could in principle admit the
    cap more than once; the post-hoc trim in `merge` is what actually enforces it.

    Runs on whatever thread the caller provides; does no Qt work.
    """
    attachments: list[str] | None = None
    prompt = batch.as_prompt()

    companion_block = companion.as_prompt_block() if companion else ""
    if companion_block and companion_cap_points > 0:
        prompt += "\n\n" + companion_block
    else:
        companion_cap_points = 0

    image_nums = batch.vision_targets(imagery)
    if use_vision and image_nums:
        cap = MAX_IMAGES_SINGLE_CALL if batch.whole_document else MAX_IMAGES_PER_BATCH
        if len(image_nums) > cap:
            log.warn(f"lecture: {len(image_nums)} segments want vision, sending {cap}")
            image_nums = image_nums[:cap]
        from .ingest import render_segment_paths
        # PATHS, not bytes: core.api's `attachments` contract is absolute file
        # paths (see _call_cli, which Read-tools them). Passing bytes raises
        # inside the provider and the whole call is lost to the except below.
        imgs = render_segment_paths(lecture.path, image_nums)
        if imgs:
            unit = (batch.segments[0].unit if batch.segments else "Slide")
            sent = sorted(imgs)
            attachments = [imgs[n] for n in sent]
            # Name the numbers. "in order" alone makes the model guess the
            # mapping, and a misattributed diagram cites the wrong slide.
            listed = ", ".join(f"{unit} {n}" for n in sent)
            prompt += (
                f"\n\n[Attached images, in this order: {listed}. These chunks "
                "either have no text layer or carry a diagram/table whose meaning "
                "is in its layout — read the images, not their text. Skip any that "
                "show a blank or unanswered exercise.]"
            )

    system = build_extract_system(
        target_points, whole_document=batch.whole_document, high_yield=high_yield,
        enrich=enrich, companion_cap_points=companion_cap_points)
    # Extraction reasons over a whole lecture and is legitimately slow: measured
    # ~240s text-only and ~320s with vision on a 44-page deck. The CLI's 180s
    # default (300s with attachments) was killing it mid-thought — losing the
    # entire run to a timeout, not a real failure. Give both real headroom.
    call_timeout = 900 if attachments else 600
    try:
        reply = core_api.ask_claude_json(
            prompt=prompt,
            system=system,
            # ~90 tokens per point; headroom so a model at the budget isn't cut
            # off mid-array, which would surface as a JSON parse failure and
            # lose the whole call.
            max_tokens=max(2000, target_points * 220),
            model=model,
            # The caller may be on a worker thread, where a tooltip would be a
            # Qt call from the wrong thread. We raise instead: the panel catches
            # ExtractionError and shows the classified reason (out of usage,
            # timeout, …) rather than a generic "nothing came back".
            show_errors=False,
            attachments=attachments,
            timeout=call_timeout,
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        log.error(f"lecture: batch {batch.index} ({batch.label}) failed [{code}]: {e}")
        raise ExtractionError(code, friendly) from e
    except Exception as e:
        # A non-provider failure (JSON shape, unexpected bug): log and treat as
        # an empty slice so one bad batch can't sink a multi-batch document.
        log.error(f"lecture: batch {batch.index} ({batch.label}) failed: {e}")
        return []

    if reply is None:
        log.warn(f"lecture: batch {batch.index} ({batch.label}) returned nothing")
        return []
    return parse_batch_reply(reply, batch)


# ── learning-objective mode ──────────────────────────────────────────────────
# Skip document extraction entirely: the user hands over an LO list directly.
# This is the biggest single cost saving in the tool — no ~$0.6/5-minute read of
# a 44-page deck, no vision — because the input is already the distilled thing
# extraction spends all that effort trying to recover. It also sidesteps the
# LP-quality problems that dog document extraction: a Curtin LO ('Describe the
# mechanism of…') is already atomic and mechanism-shaped, which is exactly what
# the deck files cards under.
#
# It is still ONE AI call, because raw LO text still needs (a) the instruction
# verb stripped, (b) system/subsystem/topic classification, and (c) the ~2-word
# Anki search terms — but over a few hundred characters of already-atomic text,
# not a whole lecture, so it's cheap and fast.
#
# NOTE: the rules below (Anki-AND, 2-word terms, deck vocabulary, entity-not-
# theme topics, mechanism-is-its-own-point) are deliberately RESTATED here rather
# than shared with _EXTRACT_SYSTEM_TMPL. They must stay in sync in spirit, but
# keeping the two prompts as separate strings means an edit here can never change
# the bytes of the lecture prompt and silently invalidate its warm extraction
# cache. If you change a term/tag rule, change it in BOTH places.
_LO_EXTRACT_SYSTEM = (
    "You are given a list of medical LEARNING OBJECTIVES (the syllabus). For "
    "each one, produce the Anki search terms that would find existing, already-"
    "vetted cards for it. You are NOT writing cards.\n\n"
    "ONE OBJECTIVE → ONE LEARNING POINT, in order. These are already atomic — do "
    "NOT consolidate them, and do NOT drop any. The ONLY time you split one "
    "objective into several points is when it genuinely bundles things that live "
    "under DIFFERENT cards (same-cards test): 'Describe the causes and management "
    "of hyperkalaemia' is two points, because 'hyperkalaemia causes' and "
    "'hyperkalaemia management' find different cards; likewise 'Describe the major "
    "vessels of cerebral circulation and the clinical syndromes where this "
    "circulation is impaired' is two points (the cerebral vessels / Circle of "
    "Willis, and the territorial stroke syndromes). And MECHANISM OF ACTION is "
    "always its own point — 'SSRIs block serotonin reuptake' is a distinct, high-"
    "yield card from 'SSRIs are first-line', so give mechanism its own point with "
    "receptor/transporter/enzyme terms.\n\n"
    "SPLIT A BUNDLED ASPECT LIST THE SAME WAY. An objective that names a structure "
    "then lists distinct aspects of it — 'Anatomy of the cerebral cortex (lobes, "
    "layers and cytoarchitecture)', 'Structure and function of the ventricular "
    "system including CSF production and circulation' — bundles separately-"
    "testable ideas that live on different cards. Emit ONE point per aspect "
    "('cortex lobes'; 'cortical layers'; 'Brodmann cytoarchitecture'), each a "
    "single idea a card could test, not one sprawling point. A parenthetical or "
    "comma list of aspects, or nouns joined by 'and', is the signal to split.\n\n"
    "@@FIDELITY_RULE@@"
    "DROP THE INSTRUCTION VERB — it is not part of the fact. 'Describe the "
    "mechanisms of gastric acid production by oxyntic cells' becomes concept "
    "'Mechanisms of gastric acid production by oxyntic (parietal) cells'. Verbs "
    "to drop: describe, understand, outline, discuss, list, explain, define, "
    "recognise, identify.\n\n"
    "IGNORE ANYTHING THAT IS NOT A REAL OBJECTIVE. A pasted list can carry chrome "
    "from the source document — a course code, a page number, a date, a heading "
    "like 'Content knowledge' or a preamble like 'At the end of this problem, "
    "students should be able to:'. These are NOT objectives; emit no point for "
    "them.\n\n"
    "HOW ANKI SEARCH WORKS (critical — most failures come from ignoring this):\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. So a 3–4 word term like 'psoriasis cell-mediated immunity' almost "
    "always returns ZERO. Keep most terms to the concept anchor PLUS ONE "
    "discriminating keyword — usually two words ('psoriasis IL-17', "
    "'psoriasis Th17').\n\n"
    "TERM LENGTH IS A HARD CONSTRAINT:\n"
    "- TWO words is the target and the default.\n"
    "- THREE words only when those exact words plainly co-occur on one card.\n"
    "- FOUR OR MORE words is always wrong; it returns nothing. Rewrite it by "
    "REPLACING a word, never appending: 'lower oesophageal sphincter GORD' → "
    "'GORD LOS'. Re-read every term and count its words before returning.\n\n"
    "USE THE DECK'S VOCABULARY, NOT THE OBJECTIVE'S PROSE:\n"
    "Cards are terse exam shorthand. Approach each point through the angles a "
    "deck actually uses — the key cytokine/mediator, the classic drug, the named "
    "sign, the tightly-linked condition ('psoriasis secukinumab', not "
    "'pathogenesis T cells').\n"
    "ANCHOR ON THE FULL NAME THE CARD USES, NOT THE ABBREVIATION. Anki matches "
    "literal tokens: 'PD' is on ~7% of the Parkinson cards, 'MSA' on 2 of the 5 "
    "Multiple System Atrophy cards. So the entity anchor of every term must be "
    "the spelled-out name ('Parkinson rigidity', 'multiple system atrophy "
    "autonomic'); a short disease/anatomy abbreviation (PD, MSA, PSP, CN3) may "
    "appear ONLY as an EXTRA term, never the sole anchor. Keep a token verbatim "
    "only where the deck prints it that way (IL-17, TNF, HLA-B27, HIV). Prefer "
    "the common clinical word over the technical synonym ('dilated pupil' not "
    "'mydriasis').\n\n"
    "TAGGING:\n"
    'Classify each point into {"system", "subsystem", "topic"}. system is one of: '
    f"{_SYSTEMS}. PascalCase or snake_case, no spaces, no '::', no slashes. Never "
    "use 'General'/'Misc'/'Other' — return an empty string for a level that is "
    "genuinely unclear.\n"
    "TOPIC IS THE SPECIFIC CLINICAL ENTITY the point is about — the drug, "
    "disease, organism or structure — because it searches your tag tree on its "
    "own. A CROSS-CUTTING THEME IS NEVER A TOPIC: 'mechanism', 'adverse_effects', "
    "'treatment', 'diagnosis', 'pregnancy', 'complications' are ASPECTS, not "
    "entities. Put the entity in topic and let the aspect live in the terms; if "
    "there is no single entity, leave topic empty.\n\n"
    "@@COMPANION_RULE@@"
    "OUTPUT — a JSON array of objects, exactly:\n"
    '[{"concept": "...", "system": "Cardio", "subsystem": "Valvular", '
    '"topic": "aortic_stenosis", "terms": ["aortic stenosis murmur", '
    '"aortic stenosis syncope"], "src": ""}]\n\n'
    "RULES:\n"
    "- concept: one plain sentence naming the testable fact, verb dropped.\n"
    "- terms: 2–4 search strings, mostly two words. Quality over quantity.\n"
    '- src: "" for a point from the objectives. Only ever "notes" for a point '
    "drawn from the user's own notes (see below, when that section is present at "
    'all). note_refs: [] unless src is "notes".\n'
    "- Preserve the objectives' order. Do not invent objectives that weren't "
    "given.\n\n"
    "Return ONLY the JSON array. No prose, no code fences."
)


# @@FIDELITY_RULE@@ for LO mode. The objectives are the syllabus, so fidelity
# here means: cover exactly what they ask, and when one names a class, expand it
# only to the members it names. ON (enrich) lets the model supplement with the
# canonical facts a student is expected to know alongside an objective.
_LO_GROUND_RULE = (
    "STAY INSIDE THE OBJECTIVES — THEY ARE THE SOURCE OF TRUTH:\n"
    "Cover exactly what the objectives ask, nothing more. Do NOT bolt on "
    "canonical facts they don't mention; when an objective names a class, expand "
    "it only to the members the objective itself names. Invent no objective that "
    "wasn't given.\n\n"
)
_LO_ENRICH_RULE = (
    "ENRICH BEYOND THE OBJECTIVES (enabled by the user):\n"
    "You MAY expand an objective with the closely-related canonical facts a "
    "student is expected to know alongside it — the standard members of a class "
    "it only partly names, the classic mechanism or association behind it — "
    "provided they stay on the objective's own topic. Still invent no wholly new "
    "objective.\n\n"
)


def split_objectives(text: str) -> list[str]:
    """Best-effort split of pasted text into individual objectives, purely to
    COUNT them for the UI and to sanity-check the reply — the model does the
    authoritative splitting. Handles numbered ('1.', '1)'), bulleted ('-', '•',
    '*') and one-per-line lists; falls back to non-empty lines."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip a leading list marker: '1.', '1)', '-', '•', '*', 'a.'
        line = re.sub(r"^\s*(?:\d+[.)]|[a-z][.)]|[-•*·▪])\s+", "", line, flags=re.I)
        line = line.strip()
        if line:
            out.append(line)
    return out


# Lines that appear in a real LO *document* (a PBL sheet, a unit guide) but are
# chrome, not objectives: the course header/footer, page numbers, the standard
# preamble, section headings. Stripped when a whole document is loaded as the LO
# source so they don't get minted into junk points. Deliberately conservative —
# a numbered line is always treated as an objective and never tested against this.
_LO_ADMIN_RE = re.compile(
    r"""^(?:
        \d+                                          # a bare page number
      | page\s+\d+(?:\s+of\s+\d+)?                    # 'Page 3', 'Page 3 of 5'
      | at\s+the\s+end\s+of\s+this\s+problem.*        # the standard Curtin preamble
      | students?\s+should\s+be\s+able\s+to.*
      | content\s+knowledge                           # section heading
      | learning\s+objectives?\b.*                    # heading / footer
      | (?:problem[- ]based\s+learning|pbl\s+tutorial).*
      | .*\b(?:revised|updated|version)\s+\w+\s+\d{4}.*   # 'Revised November 2025'
      | [A-Z]{2,}\d{3,}\b.*                            # a course-code footer (GMED3012 …)
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def isolate_objectives(text: str) -> str:
    """Pull just the objective lines out of a whole LO *document* (e.g. a PDF's
    text layer), dropping the course header, section headings, page numbers and
    footer that would otherwise be minted into junk points, and folding a wrapped
    objective back onto one line.

    Anchors on the numbered list these documents always use. Falls back to the
    input UNCHANGED when there is no numbered list (a bulleted or one-per-line
    paste), leaving split_objectives and the model to cope as before — so pasted
    lists behave exactly as they did."""
    if not text or not text.strip():
        return text
    lines = [ln.strip() for ln in text.splitlines()]
    num_re = re.compile(r"^(\d+)[.)]\s+(.*)$")
    first = next((i for i, ln in enumerate(lines) if num_re.match(ln)), None)
    if first is None:
        return text                       # not a numbered document — leave it be
    objs: list[str] = []
    cur: str | None = None
    for ln in lines[first:]:
        if not ln:
            continue
        m = num_re.match(ln)
        if m:                             # a numbered line is always a new objective
            if cur:
                objs.append(cur)
            cur = m.group(2).strip()
        elif _LO_ADMIN_RE.match(ln):
            continue                      # header / footer / page number / heading
        elif cur is not None:
            cur = f"{cur} {ln}"           # a wrapped continuation of this objective
    if cur:
        objs.append(cur)
    return "\n".join(objs) if objs else text


def parse_lo_reply(reply) -> list[LearningPoint]:
    """Coerce an LO-mode AI reply into learning points. Like parse_batch_reply
    but with no slide refs to validate (LOs have no slides)."""
    if not isinstance(reply, list):
        log.warn(f"lecture: LO reply was {type(reply).__name__}, not a list")
        return []
    points: list[LearningPoint] = []
    for item in reply:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept") or "").strip()
        if not concept:
            continue
        terms = _clean_terms(item.get("terms"))
        if not terms:
            log.warn(f"lecture: dropping LO '{concept[:40]}' — no usable terms")
            continue
        src = "notes" if str(item.get("src") or "").strip().lower() == "notes" else ""
        note_nums = []
        if src == "notes":
            for n in (item.get("note_refs") or []):
                try:
                    note_nums.append(int(n))
                except (TypeError, ValueError):
                    continue
        points.append(LearningPoint(
            concept=concept,
            refs=[],                        # LOs have no source slides
            system=_clean_level(item.get("system")),
            subsystem=_clean_level(item.get("subsystem")),
            topic=_clean_level(item.get("topic")),
            terms=terms,
            src=src,
            note_refs=sorted(set(note_nums)),
        ))
    return points


def extract_from_objectives(text: str, *, model: str | None = None,
                            enrich: bool = False,
                            companion=None, companion_cap_points: int = 0) -> list[LearningPoint]:
    """Turn a pasted learning-objective list into learning points in one cheap
    AI call. Raises ExtractionError on a hard provider failure (so the panel can
    show the classified reason), returns [] only if the model genuinely finds
    nothing testable.

    `enrich` (off by default) is the user's toggle: off, the objectives are the
    source of truth and the model stays inside them; on, it may supplement with
    the canonical facts expected alongside an objective. In the cache key.

    Runs on whatever thread the caller provides; does no Qt work.
    """
    objectives = split_objectives(text)
    if not objectives:
        raise ExtractionError(
            "empty", "No learning objectives found in what you pasted.")

    # Number them so the model's ordering and count are anchored to the input.
    prompt = "\n".join(f"{i}. {o}" for i, o in enumerate(objectives, 1))

    companion_block = companion.as_prompt_block() if companion else ""
    companion_rule = ""
    if companion_block and companion_cap_points > 0:
        prompt += "\n\n" + companion_block
        # Same rule as file mode, with "teaching material" reading as "the
        # objectives" — the objectives are the syllabus and still the spine.
        companion_rule = _COMPANION_TMPL.replace(
            "@@COMPANION_CAP@@", str(companion_cap_points))

    system = (_LO_EXTRACT_SYSTEM
              .replace("@@FIDELITY_RULE@@",
                       _LO_ENRICH_RULE if enrich else _LO_GROUND_RULE)
              .replace("@@COMPANION_RULE@@", companion_rule))

    try:
        reply = core_api.ask_claude_json(
            prompt=prompt,
            system=system,
            # ~90 tokens per objective, generous headroom so a long list isn't
            # cut off mid-array (a truncated JSON array is a total parse loss).
            max_tokens=max(2000, len(objectives) * 220),
            model=model,
            show_errors=False,
            timeout=300,        # small input, no vision — the 44-page read this replaces was ~240s
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        log.error(f"lecture: LO extraction failed [{code}]: {e}")
        raise ExtractionError(code, friendly) from e
    except Exception as e:
        log.error(f"lecture: LO extraction failed: {e}")
        raise ExtractionError("unknown", f"Couldn't read those objectives: {e}") from e

    if reply is None:
        return []
    return parse_lo_reply(reply)
