# Learning points → existing cards.
#
# Two complementary nets per concept, then a relevance pass:
#
#   Leg A (tags)  — the point's {system, subsystem, topic} against the tag tree.
#                   High precision, nearly free, catches the well-tagged bulk.
#                   Alone it under-recalls: lecture language rarely matches tag
#                   names.
#   Leg B (terms) — the AI's search terms through mw.col.find_notes(). Catches
#                   on-topic cards tagged somewhere unexpected. Alone it
#                   over-recalls: a card that merely mentions the concept hits.
#   Shave         — one AI call per concept asks which candidates actually TEST
#                   the point. This is what earns the whole premise: the deck is
#                   already vetted, so the only question is relevance.
#
# Collection access and AI calls are separated deliberately. Everything that
# touches mw.col must run on the main thread; only score_candidates() is safe on
# a worker. The panel marshals between them.

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from aqt import mw

from ...core import anki_utils, api as core_api, log
from .extractor import LearningPoint


class RelevanceUsageExhausted(Exception):
    """The relevance pass hit the provider's usage/session limit.

    Distinct from an ordinary flaky call. A one-off failure fails OPEN (keep the
    candidates, carry on) because the next call will probably work. But once
    usage is gone, EVERY remaining call fails identically — so the loop should
    stop and tell the user their results are only partially filtered, rather
    than silently fail-open point after point. That silent degradation is
    exactly what made a run that ran out of usage mid-relevance look successful.
    """

    def __init__(self, friendly: str):
        super().__init__(friendly)
        self.friendly = friendly


# Internal per-point ceiling applied during SEARCH, before scoring. This is not
# the user-facing number — that's the whole-lecture budget (see allocate()).
# It exists because the relevance prompt grows with the candidate list, so an
# unbounded net from a generic term ("pain") would blow up cost and latency.
# The cut is only safe because the pool is RANKED by term-specificity first
# (see _rank_candidates): the 15 kept are the 15 that best match the point's
# terms, not an arbitrary creation-order slice of a drug-class subtree.
#
# Was 10 while the allocator only ever handed a point 1-2 cards (its old
# minimum-cover behaviour) — reading 10+ candidates the point could never select
# was just paying the judge for nothing. The depth presets (see DEPTH_TARGETS)
# changed that: a "thorough" run hands a well-covered point up to 5 cards, so the
# judge now needs a deeper pool to draw them from, and a sparse point (e.g. the
# cortex-anatomy LP that kept only 1 of 10) needs more candidates before it can
# offer a cluster at all. Raised 10 -> 20 to feed that. It scales the relevance
# pass linearly (more calls, NOT bigger ones — batches still pack to
# CANDIDATES_PER_RELEVANCE_CALL, so per-call timeout risk is unchanged), which is
# the deliberate cost of depth. Lower it back toward 12-15 to trade depth for speed.
MAX_CANDIDATES_PER_POINT = 20

# How many tag-leg notes to carry INTO the ranking (before the cut above). The
# tag leg is a coverage net for correctly-tagged cards that happen to match no
# search term; capped here so a big drug-class subtree ("Antidepressants", ~100s
# of cards) can't flood the pool with creation-order cards and push the real
# term-matching card past the MAX_CANDIDATES_PER_POINT cut — the exact failure
# that fed the SSRI-mechanism point SIADH/ondansetron cards while its actual
# mechanism card sat uncut below the dump. Term hits are NOT pre-capped: they're
# the recall engine, and the whole union is ranked before the cut.
TAG_LEG_POOL = 40

# Fallback card estimate when config carries none. An estimate, never a quota:
# if a lecture's points genuinely only yield 40 good cards, the answer is 40 —
# padding it to the number with weaker matches is the exact failure this tool
# exists to avoid. The real default lives in config ("target_cards": 45); this
# only bites if that key is missing, so it matches it.
DEFAULT_TARGET_CARDS = 45

# How many cards one learning point is worth, when the estimate is sized from
# the lecture rather than typed in. Three is deliberately generous: the number
# is a CEILING that allocate() never pads to (see RUN-10 — "an estimate, not a
# quota"), so erring high costs nothing, while erring low silently caps a
# thorough lecture at fewer cards than it has facts.
CARDS_PER_POINT = 3
AUTO_TARGET_MIN = 15
AUTO_TARGET_MAX = 150


def auto_target_cards(n_points: int) -> int:
    """Size the card estimate from how many points the lecture actually yielded.

    A fixed target is wrong in both directions: on a 6-point tutorial it invites
    the allocator to spread thin, and on a 40-point pharmacology deck it binds
    before the content does. Clamped at both ends so a pathological extraction
    can't produce an absurd figure."""
    if n_points <= 0:
        return DEFAULT_TARGET_CARDS
    return max(AUTO_TARGET_MIN, min(AUTO_TARGET_MAX, n_points * CARDS_PER_POINT))

# Depth presets — how many cards ONE objective may claim, on top of guaranteeing
# its minimum non-redundant set-cover. This is the knob that turns the tool from
# a "minimum revision shortlist" into a "concept-coverage set":
#   tight    = the historical behaviour: exactly the set-cover weight, one card
#              per distinct sub-fact and no more (a lean shortlist).
#   standard = up to 3 vetted cards per objective — a few angles on each concept.
#   thorough = up to 5 — a proper study cluster per objective (the default now).
# The cap only ever RAISES a point's share to the preset; a point whose set-cover
# already earns more (a big compound point) keeps its higher count. The cards it
# hands out are ones the relevance judge already approved and were being
# discarded — so depth costs no extra AI, it just stops binning good candidates.
# The total note budget (target_cards) is still the hard ceiling over the sum.
DEPTH_TARGETS = {"tight": 1, "standard": 3, "thorough": 5}
DEFAULT_DEPTH = "thorough"

# The candidate pool the relevance judge reads, DERIVED from depth so the user
# never has to set it: a lean run (tight) needs fewer candidates to pick one card
# per sub-fact, a thorough run needs a deep pool to draw a 5-card cluster from.
# This is the "candidates per point" knob made automatic — it only ever LOWERS
# the configured cap (a user who hand-set a smaller advanced cap keeps it).
CANDIDATE_CAP_BY_DEPTH = {"tight": 12, "standard": 16, "thorough": 20}


def cap_for_depth(depth: str) -> int:
    return CANDIDATE_CAP_BY_DEPTH.get(depth, MAX_CANDIDATES_PER_POINT)


def resolve_candidate_cap(cfg) -> int:
    """The one place the candidate pool size is decided: the configured cap,
    never above what the depth preset asks for.

    The configured value WINS when it is lower, and that is deliberate, not a
    stale default to be cleaned up — a measured run found the judge's keeps
    concentrated in the top ~10 of the pre-rank, so a deeper pool mostly buys
    more adjacent cards to reject. If a point's pool is so big that the cut
    hurts, the fault is upstream: a 3-term point returning 127 notes has a
    search-breadth problem, and widening the cut just pays the judge to sift
    it. Fix the terms, not the cap.
    """
    cap = cap_for_depth(str(cfg.get("depth") or DEFAULT_DEPTH))
    try:
        configured = int(cfg.get("candidate_cap") or 0)
    except (TypeError, ValueError):
        configured = 0
    return min(cap, max(3, configured)) if configured > 0 else cap

# Ceiling on the per-point share the weighted first pass will hand out (see
# _point_weight). Four is the observed top of the range on real lectures: the
# worst compound point in the pharm deck ("TCAs are also used in bulimia,
# neuropathic pain, migraine prophylaxis and nocturnal enuresis") carries four
# independent sub-facts. Beyond that a "point" is really a slide summary, and
# spending five slots on it starves the rest of the lecture.
MAX_CARDS_PER_POINT = 4

# Minimum judge score for a card to count as "the only thing filling this slot"
# and so win a cross-point ownership fight. A 1 means the judge kept it only
# because dropping cards is costly (see RELEVANCE_SYSTEM) — not the basis on
# which to take a card off a point that scored it higher.
_CRITICAL_MIN_SCORE = 2

# Minimum judge score for a card to LEAD a sub-fact slot, and to count towards
# the point's weight. Same principle as _CRITICAL_MIN_SCORE: a 1 is a card the
# judge kept only because dropping is costly, and a slot is a claim that the
# point has another distinct thing worth showing — a 1 is not evidence of that.
_SLOT_MIN_SCORE = 2

# Minimum judge score for a card to SURVIVE the relevance pass at all. A
# deliberate 1 ("this barely relates, but dropping is costly") is exactly the
# tangential noise a no-inspection bulk pass can't afford — Fletcher: "I'd rather
# a clean no than a 1/4 yes." So a genuine 1 is dropped, and a point left with
# nothing becomes an honest gap rather than being filled with a weak card.
#
# This is SAFE against the fail-open guarantee only because _DEFAULT_KEEP_SCORE
# is also 2: a card the model FAILED to score (omitted / unparseable / whole
# batch died) defaults to 2 and clears this floor, so a judge failure still
# keeps everything. The floor bites only on a 1 the model chose to give.
_KEEP_MIN_SCORE = 2

# Minimum judge score for a card to COVER A KNOWLEDGE GAP. Three, not two.
#
# A lecture point is a heading and a 2 ("an adjacent aspect of the same
# subject") is legitimately part of it. A gap is not a heading — it is one claim
# the student wrote down because he didn't know it, and the only question is
# whether a card teaches that claim. A 2 answers "no": same subject, tests
# something else. Ticking it says his deck has the gap covered when it doesn't,
# which is the one thing this screen must never say. Both cards in the mTBI run
# he complained about were 2s and 3s of exactly this kind.
#
# Nothing is hidden by the higher bar. A dropped 2 stays in `nids`, so it is
# still listed under the gap in the picker, unticked, one click from being
# taken; what changes is that the gap stays OPEN and gets offered to the writing
# half instead of being quietly closed by an adjacent card.
#
# Applied only on the MAIN run (see parse_scoring_reply): a user-driven
# re-search asks for the lenient floor on purpose.
#
# THE FAIL-OPEN GUARANTEE SURVIVES IT, but not the way the ≥2 floor did. That
# one was safe because _DEFAULT_KEEP_SCORE is also 2, so a card the model never
# scored cleared the bar by arithmetic. Three is above the default, so an
# unscored card would have been DROPPED — a judge failure manufacturing exactly
# the false gap this module refuses to report. parse_scoring_reply therefore
# keeps an UNSCORED card explicitly, whatever the floor: only a score the model
# actually gave can drop one.
_GAP_MIN_SCORE = 3

# Re-search is a deliberately broader, more lenient second pass the user triggers
# on a point the first run left unaddressed: a wider candidate net (below) and a
# floor of 1 instead of 2, so a "closest match" tangential card (score 1) is
# OFFERED rather than dropped — but a genuine 0 (irrelevant) is still thrown out.
# "Low, but not too low." The extra 1s surface flagged "closest match" in the UI,
# so they read as what they are. Only ever used on an explicit re-search, never
# the main run — the main run's cleaner ≥2 bar is unchanged.
RESEARCH_MIN_SCORE = 1
RESEARCH_CANDIDATE_LIMIT = 25   # vs MAX_CANDIDATES_PER_POINT=10 on the main run

# If the tag leg pulls more notes than this, the matched branch is too generic
# to be evidence of anything and the leg is discarded — the term leg carries.
MAX_TAG_SUBTREE_NOTES = 400

# Cap on tags OR-ed into a single tag query. A short topic can substring-match
# a lot of the 58k-tag tree; past this the query is broad by definition.
MAX_TAGS_PER_POINT = 12

# How many of a topic_present gap's OWN-topic cards to surface for one-click
# taking (see PointMatch.topic_cards). These are provably on-topic — token-exact
# tag matches — not the judge's tangential 1s, but a gap can sit under a branch
# of dozens and dumping all of them re-creates the noise the tool exists to cut.
# Six recovers the point's coverage without turning the row into a deck browser;
# the full set is one "⌕ Browse" click away.
TOPIC_CARD_SURFACE_CAP = 6


def _topic_anchored(point) -> bool:
    """True when at least one search term is built AROUND the point's topic — the
    term's words contain the WHOLE topic phrase, not merely name-drop the parent
    disease.

    This is the line between two gap shapes that are otherwise token-identical
    (a card about the parent disease that doesn't test the narrow claim):

      * the point's OWN subject IS the topic — 'trigeminal neuralgia V2', topic
        trigeminal_neuralgia; 'febrile seizure vaccine', topic febrile_seizures.
        The topic's tagged cards are genuinely about this subject, so offering
        them (topic_cards) is useful even though none tests the exact point.
      * the point names a distinct SUB-ENTITY the deck lacks and only tags the
        parent — 'SynNeurGe Parkinson', topic Parkinsons_disease (no card says
        SynNeurGe); 'general movements assessment', topic cerebral_palsy (no
        card is about GMA). Here the tagged cards are generic parent-disease
        cards — the "options were shit" Fletcher saw on the PD run.

    The whole-topic-phrase test catches the second: 'SynNeurGe Parkinson' is
    missing 'disease', so it isn't a superset of {parkinson, disease}.

    Used ONLY to gate the opt-in topic_cards surfacing (the Take button). It
    never touches selection or the judge — a card the judge kept via its Extra
    field (e.g. the Braak-model card that answers via constipation) is unaffected.
    Errs toward KEEPING: if the extractor anchored any term on the topic, the
    offer stands.
    """
    def toks(s: str) -> set[str]:
        out = set()
        for w in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split():
            w = w[:-1] if w.endswith("s") else w  # crude singular
            if len(w) >= 3:
                out.add(w)
        return out
    tt = toks((getattr(point, "topic", "") or "").replace("_", " "))
    if not tt:
        return False
    return any(tt <= toks(t) for t in (getattr(point, "terms", None) or []))

# How much of a note to show the relevance judge. Enough to tell what's being
# tested; not so much that 15 candidates blow the context.
CARD_TEXT_CHARS = 260

# Relevance batching is packed by CANDIDATE COUNT, not a flat points-per-call.
# One-per-call is the most accurate (the model sees a single concept and its
# candidates) but a 40-concept lecture then costs 40 round trips. The old flat
# "3 points per call" cut that ~3x but ignored how lopsided candidate counts are:
# one point may carry 10 near-misses and the next only 2, so a fixed grouping
# swings between trivial calls and 30-candidate monsters — slow, and the ones
# that time out (which "just raise the timeout" was papering over). Packing to a
# candidate budget instead bounds the biggest call AND fuses the small points:
# fewer calls when candidates are sparse, smaller calls when they're dense, same
# candidates judged either way. See _interleave_by_system for why the members of
# a batch are deliberately unrelated (that anti-blur is the reason a POINTS
# ceiling still applies on top of the candidate budget).
# Budget = the old flat-3 grouping's WORST case (3 points x the 10-candidate cap),
# chosen so dense lectures behave exactly as before — no extra calls, same biggest
# call — while sparse points (few candidates each) pack up to the points ceiling
# instead of wasting a whole call on 3 tiny points. So this can only match or beat
# today's call count, never regress it. Lower it if a real timeout ever shows up
# on a dense point; there's no evidence one does today.
CANDIDATES_PER_RELEVANCE_CALL = 20   # target candidates per call (the real limiter)
MAX_POINTS_PER_RELEVANCE_CALL = 6    # ceiling so a batch never blurs too many points
# Lowered 30 -> 20 to cut CLI timeouts: a smaller call finishes well inside the
# window, at the cost of a few more round trips. The retry-once + fail-open flag
# downstream catch whatever still slips through.

# Relevance runs on the CLI for most users (no API key), whose default 180s cut
# -off is what times out on a dense batch. Give it the same 300s the extraction
# read gets — the work is bounded by the batch budget above, so this is headroom,
# not a licence to hang.
RELEVANCE_TIMEOUT = 300
POINTS_PER_RELEVANCE_CALL = 3        # retained for callers that still import it

RELEVANCE_SYSTEM = (
    "You RANK existing Anki cards by how well each one tests a given learning "
    "point from a lecture.\n\n"
    "The cards come from a vetted, high-quality medical deck — you are NOT "
    "judging card quality, only RELEVANCE to the point. Give each card a score "
    "from 0 to 3:\n\n"
    "    3 — DIRECTLY tests this learning point. The card's whole subject IS the "
    "point. If the point is about a drug's MECHANISM / how it works, the card "
    "that states that mechanism (receptor, transporter, enzyme, agonist/"
    "antagonist) is the 3 — a mechanism-of-action card is the CORE card for a "
    "mechanism point, never a lesser 'adjacent' one. But a card that only names "
    "the drug's CLASS or identity ('What drug class does venlafaxine belong to? "
    "SNRI') is NOT its mechanism — for a mechanism point that scores 1 at most; "
    "the 3 is the card stating the receptor/transporter/enzyme action itself. "
    "(A class/identity card is the 3 only when the point is itself about "
    "classification.)\n"
    "    2 — Covers an adjacent aspect of the SAME subject: a specific instance, "
    "consequence, diagnosis, prevention, or treatment detail that sits under the "
    "point but is not the point itself. The learning point is a lecture heading, "
    "not one exam question — a point about amphotericin's nephrotoxicity "
    "legitimately covers the whole nephrotoxicity story.\n"
    "    1 — Related but tangential: the right broad area, but the card is really "
    "about something adjacent. Keep it only because dropping a real card is "
    "costly; it should sort LAST.\n"
    "    0 — About something ELSE that merely shares a word: a different drug, "
    "disease, or topic where the keyword appears in passing. This is the noise a "
    "keyword search returns and the reason you exist. 0 means DROP.\n\n"
    "WHEN THE POINT IS A COMPARISON OR RANKING, THE CLAIM IS WHAT MUST BE "
    "TESTED. For a point like 'in overdose SSRIs are the least harmful and TCAs "
    "the most harmful', a card that ASSERTS one end of that comparison is the 3 "
    "— 'What is the most common cause of death in TCA overdose? Cardiac "
    "toxicity' establishes that TCAs are the dangerous one. A card that merely "
    "MANAGES or DESCRIBES one of the things being compared, without speaking to "
    "which is worse ('what is the next step in managing SSRI overdose?', "
    "'list the adverse effects of TCA overdose'), is a 2 at most: it is the "
    "right drug and the right situation, but it is not the claim. Score the "
    "poles of the comparison ABOVE general material about the same drugs, so "
    "that a tight budget spends its slots on cards that carry the contrast.\n\n"
    "THE SCORE IS A RANK, NOT JUST KEEP/DROP. The highest-scored card is the one "
    "the student sees first and the one kept when the card budget is tight, so "
    "getting the ORDER right matters as much as excluding noise. When two cards "
    "both test the point, the more DIRECT and COMPLETE one scores higher. Do not "
    "give everything a 2 — separate the card that nails the point from the one "
    "that merely touches it.\n\n"
    "Examples for the point 'Amphotericin B causes nephrotoxicity via renal "
    "vasoconstriction':\n"
    "  3 — 'Amphotericin B is associated with renal toxicity.' (the point itself)\n"
    "  3 — 'How is amphotericin nephrotoxicity prevented? IV saline.' (adjacent "
    "aspect, still squarely the nephrotoxicity story)\n"
    "  2 — 'Which RTA does amphotericin cause? Distal (type 1).' (a specific "
    "instance)\n"
    "  2 — 'Which ions are supplemented with amphotericin? K+ and Mg2+.' (a "
    "consequence of the same renal toxicity)\n"
    "  0 — 'Ketoconazole inhibits glucocorticoid biosynthesis.' (different drug "
    "entirely)\n"
    "  0 — 'Polyenes bind ergosterol to form pores.' (right drug, but the "
    "antifungal mechanism, not the renal toxicity)\n\n"
    "DO NOT demand that a card restate the point's exact wording or mechanism. "
    "If the point says 'via renal vasoconstriction', a card about amphotericin "
    "damaging the kidneys still scores 3 — it is the same subject. (This "
    "licence does NOT extend to an item headed GAP — see below.)\n\n"
    "AN ITEM HEADED 'GAP' IS NOT A LECTURE HEADING. It is ONE claim the student "
    "wrote down because they did not know it, and the verdict decides whether "
    "their deck already tests that claim or whether they have to write a card "
    "for it tonight. Judge a GAP strictly, and against the CLAIM:\n"
    "    3 — the card ASSERTS the claim. You cannot answer the card without "
    "knowing it.\n"
    "    2 — the same subject, but the card tests something ELSE about it. A "
    "card that merely MENTIONS the claim's threshold, number, timeframe or "
    "finding while asking about something else — which patients need imaging, "
    "how the injury is managed, what to do next — is a 2, never a 3: the "
    "student could answer it having never learnt the claim. 'What are the "
    "contexts for the Canadian CT Head Rules in a patient post-TBI with GCS "
    "13-15?' is a 2 for the gap 'mTBI is typically defined by a GCS of 13-15'. "
    "It quotes the number; it does not teach the definition.\n"
    "    1 — right topic, different claim.\n"
    "    0 — something else that shares a word.\n"
    "A card whose scenario EXCLUDES the gap's condition is a 1 at most, however "
    "many of the gap's words it contains: 'a player who was wobbly but DID NOT "
    "lose consciousness' is not a card about loss of consciousness.\n"
    "'Is defined by', 'criteria', 'definition', 'involves', 'typically', a "
    "number or a cut-off — these mark a DEFINITIONAL claim, and the 3 is the "
    "card that states the definition. Never promote a management, imaging or "
    "next-step card to a 3 because it happens to quote the figure.\n\n"
    "WHEN GENUINELY UNSURE, score 1 rather than 0. The costs are not symmetric: "
    "an extra card is one the student unticks in a second, but a wrongly dropped "
    "card is invisible to them and makes the tool report a gap the deck does not "
    "have. Reserve 0 for cards that are plainly about something else.\n\n"
    "YOU WILL BE GIVEN SEVERAL UNRELATED LEARNING POINTS AT ONCE.\n"
    "Judge each one INDEPENDENTLY, against its own candidate list only. The "
    "points are deliberately unrelated — a card under point 1 is not evidence "
    "for point 2. Never let one point's topic leak into another's verdicts.\n\n"
    "SOME POINTS ARE COMPOUND and are listed with numbered SUB-FACTS. A compound "
    "point is not one question — it is several, and the student needs a card for "
    "EACH. For those points, score every card AGAINST THE SUB-FACT IT SERVES, "
    "not against the point as a whole, and say which sub-fact that is. A card "
    "that nails sub-fact 2 completely is a 3 even though it says nothing about "
    "sub-facts 0, 1 and 3 — judging it against the whole point would mark down "
    "every card that exists, since no single card covers a compound point. If a "
    "card serves no particular sub-fact (it is about the point's general subject) "
    "give it sub-fact -1.\n\n"
    'OUTPUT — a JSON object keyed by the point number you were given, each '
    "mapping every card id under that point to its score 0–3:\n"
    '{"1": {"1234567890": 3, "1234567891": 0}, "2": {"1234567892": 2}}\n'
    "For a point that listed SUB-FACTS, give each card a two-item list "
    "[score, sub-fact number] instead of a bare score:\n"
    '{"1": {"1234567890": [3, 0], "1234567891": [2, 3], "1234567892": [1, -1]}}\n\n'
    "Include EVERY point and EVERY id you were given. Return ONLY the JSON "
    "object. No prose, no code fences."
)


@dataclass
class TagIndex:
    """Snapshot of the collection's tag tree, built once per run.

    Two caches matter here. `all_tags` avoids re-walking mw.col.tags.all() for
    every one of a lecture's ~40 concepts, and `_nids_by_tags` avoids re-running
    a query several concepts happen to share — the expensive half.

    Measured on the real collection (118k cards, 58,929 tags), a single
    find_notes costs 0.3–7s, so the query COUNT is what matters: every matched
    tag gets OR-ed into one query rather than queried individually.
    """
    all_tags: list[str] = field(default_factory=list)
    _nids_by_tags: dict[tuple[str, ...], list[int]] = field(default_factory=dict)
    # Optional Anki query fragment narrowing every lookup to one deck or tag
    # subtree (see scope_query). "" — the default — searches everything, which
    # is what every existing caller does.
    scope: str = ""

    @classmethod
    def build(cls, scope: str = "") -> "TagIndex":
        try:
            return cls(all_tags=list(mw.col.tags.all()), scope=scope or "")
        except Exception as e:
            log.error(f"lecture: couldn't read tag tree: {e}")
            return cls(scope=scope or "")

    def nids_for_tags(self, tags: list[str]) -> list[int]:
        """Notes under ANY of `tags` (subtrees included), in one query."""
        if not tags:
            return []
        key = tuple(sorted(tags))
        cached = self._nids_by_tags.get(key)
        if cached is not None:
            return cached
        clause = " OR ".join(f'tag:"{t}" OR tag:"{t}::*"' for t in tags)
        # The scope has to bind here too, not just on the term leg: scoping one
        # and not the other lets the tag leg hoover up the whole collection, and
        # a scoped run would then report everything as already covered.
        scope = (self.scope or "").strip()
        query = f"{scope} ({clause})" if scope else f"({clause})"
        try:
            nids = list(mw.col.find_notes(query))
        except Exception as e:
            log.warn(f"lecture: tag query failed for {len(tags)} tag(s): {e}")
            nids = []
        self._nids_by_tags[key] = nids
        return nids

    def tags_matching(self, keyword: str) -> list[str]:
        """Tags with `keyword` in a :: segment, TRUNCATED at that segment.

        Mirrors browse.py's _resolve_tags match_idx: match on a segment rather
        than the whole path, so 'aortic_stenosis' finds
        '...::Cardio::aortic_stenosis' without knowing the levels above it.

        Truncating is what makes the query cheap. In the real deck, the parent
        '...::Coeliac_Disease' is not itself a tag on any note — only its six
        leaves are ('::01_Epidemiology', '::05_Pathophysiology', …), and they
        exist under two separate parents. Returning the leaves OR-ed twelve
        clauses into one query and cost ~7s. Truncating at the matched segment
        collapses them to the two real branches, and since each is queried as
        `tag:"X" OR tag:"X::*"` the children are still covered.
        """
        needle = (keyword or "").lower().strip()
        if len(needle) < 3:
            # 1–2 character needles substring-match half the tree.
            return []
        # Match on whole tag-path TOKENS, not a raw substring. `needle in segment`
        # collides on short topics: 'msa' is a substring of 'Ra{msa}y_Hunt' and of
        # AMBOSS hash tags ('MsaMvN'), so topic 'MSA' used to pull Ramsay-Hunt and
        # antiretroviral cards — 10 candidates of pure noise that the relevance
        # floor then had to bin, reporting a gap where real 'Multiple System
        # Atrophy' cards exist. Requiring every token of the needle to appear as a
        # WHOLE token of one segment fixes it without losing real matches:
        # 'parkinson_disease' still matches '::Parkinson_Disease' ({parkinson,
        # disease}), 'aortic_stenosis' still matches its leaf.
        want = set(re.findall(r"[a-z0-9]+", needle))
        if not want:
            return []
        out: list[str] = []
        for t in self.all_tags:
            segs = t.split("::")
            idx = next(
                (i for i, s in enumerate(segs)
                 if want <= set(re.findall(r"[a-z0-9]+", s.lower()))),
                None)
            if idx is None:
                continue
            root = "::".join(segs[:idx + 1])
            if root not in out:
                out.append(root)
        return out


@dataclass
class PointMatch:
    point: LearningPoint
    nids: list[int] = field(default_factory=list)          # candidates, ordered
    kept: list[int] = field(default_factory=list)          # survived relevance
    selected: list[int] = field(default_factory=list)      # after budget allocation
    per_term: list[tuple[str, int]] = field(default_factory=list)
    tag_hits: list[str] = field(default_factory=list)
    # nid -> (whole_terms_matched, term_words_matched, is_tag). Filled by the
    # candidate ranking; logged per candidate so a bad pick is diagnosable as a
    # recall miss (the right card matched 0 terms → not searchable by these
    # terms) vs a ranking/judge miss (it matched 3 terms but was still dropped).
    rank_debug: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    # nid -> indices of the point's terms this card matches WHOLLY. Drives the
    # term round-robin (_spread_by_term) and is logged so it's visible whether a
    # compound point's slots actually spread across its sub-facts.
    term_cover: dict[int, frozenset[int]] = field(default_factory=dict)
    # nid -> the judge's 0-3 relevance score, and nid -> which SUB-FACT of a
    # compound point the judge says the card serves. The slot is authoritative
    # over term_cover when present: term_cover is a substring test over the whole
    # note (Extra field included), so a card can register on a slot because of a
    # passing mention, which is how the hyponatraemia card landed on the
    # 'SSRI bleeding' slot.
    scores: dict[int, int] = field(default_factory=dict)
    judge_slot: dict[int, int] = field(default_factory=dict)
    scored: bool = False
    allocated: bool = False
    # How many cards this point earned in the first allocation pass — its term
    # set-cover size (see _point_weight). Recorded so the PICK log distinguishes
    # "the allocator only offered this point one slot" from "the judge only kept
    # one card", which look identical in the output.
    weight: int = 1
    # nid -> (suspended_cards, total_cards). Cards already in rotation need no
    # action, but they still prove the point is covered.
    susp: dict[int, tuple[int, int]] = field(default_factory=dict)
    # Cards the USER force-included, overruling the judge or the budget. The
    # judge is a good filter, not an oracle: when he can see the right card sat
    # in the candidate list and got dropped, he must be able to take it. Kept
    # separate from `selected` so an override is never mistaken for something
    # the pipeline chose, and so re-running allocation can't silently drop it.
    manual: list[int] = field(default_factory=list)
    # Cards the user removed by hand. The mirror of `manual`, and just as
    # necessary: an override list that can only ADD makes the tool argue with the
    # user — it could be told "you missed one" but not "this one is wrong".
    manual_drop: list[int] = field(default_factory=list)
    # The sibling point whose selected cards already cover this one. Set only when
    # this point ends up with NOTHING of its own to show because cross-point
    # ownership (see allocate) handed its cards to a neighbour that keeps the same
    # cards — e.g. 'PD rigidity' and 'cardinal motor features' both hold the
    # TRAPSS mnemonic card; one wins it, the other would otherwise read as an
    # empty hole. It isn't a hole: the content IS unsuspended, under the sibling.
    # A reference (not an index) so it survives the covered/gaps re-ordering the
    # panel does on every re-render. Recomputed each summarise().
    covered_by: "PointMatch | None" = None

    # The SOFTER cousin of covered_by, for a point that DID get a card of its own
    # but whose single best card (kept[0]) was handed to a sibling by ownership —
    # an "umbrella" point (e.g. 'the three cardinal features') out-competed for the
    # summary card (the TRAPSS mnemonic) by a narrower spoke ('bradykinesia'), so
    # it shows a weaker fallback while its strongest match sits under the sibling.
    # Purely informational: the point KEEPS its shown card; this only lets the row
    # say where the best card went, so the fallback isn't mistaken for the answer.
    # Recomputed each summarise(); set only when kept[0] is on show under a sibling.
    top_elsewhere: "PointMatch | None" = None

    @property
    def available(self) -> list[int]:
        """Everything that survived relevance, before the budget trim."""
        return self.kept if self.scored else self.nids

    @property
    def final(self) -> list[int]:
        base = self.selected if self.allocated else self.available
        dropped = set(self.manual_drop)
        # Overrides append rather than replace, and can't duplicate.
        return ([n for n in base if n not in dropped]
                + [n for n in self.manual if n not in base and n not in dropped])

    @property
    def candidates(self) -> list[int]:
        """EVERY card searched for this point, best-first, whether or not it made
        the cut. This is what the picker lists: the user needs to see the ones
        that were chosen (to untick) and the ones that weren't (to tick), in one
        place. Judge-dropped cards come last — least likely to be wanted, but
        exactly what a gap point has to offer, so never hidden.
        """
        kept = [n for n in self.available]
        rest = [n for n in self.nids if n not in set(kept)]
        return kept + rest

    @property
    def is_gap(self) -> bool:
        """The deck has nothing for this point.

        Deliberately keyed off `available`, NOT `selected`: a point whose cards
        exist but lost the budget lottery is covered, not a gap. Conflating the
        two would make the coverage report a function of the budget setting,
        which would be a lie.

        A manual override settles it the other way: if the user looked at the
        candidates and took one, the point is covered by his decree and calling
        it a gap would contradict what he can see on screen.
        """
        return self.scored and not self.kept and not self.manual

    @property
    def topic_present(self) -> bool:
        """A gap where the deck DOES carry this topic — the tag leg matched a
        real subtree tagged under this point's OWN topic — but no existing card
        tests this SPECIFIC point.

        Distinct from a true gap, where the deck has nothing on the topic at all
        (the tag leg matched no tag: `tag_hits` is empty). Measured on five real
        lectures, this is the single biggest source of misleading "gaps": 'PD
        rigidity', 'trigeminal neuralgia most commonly affects V2/V3' and four
        febrile-seizure risk-factor points all reported as gaps while the deck
        held 25 trigeminal-neuralgia and 10 febrile-seizure cards, correctly
        tagged — just none testing that one narrow claim. Reporting those as
        "your deck doesn't cover this" is the exact opposite of the truth.

        Why the tag leg and not the judge's scores: the judge's 0-vs-1 boundary
        is not a reliable "same topic?" line — on the real runs it scored every
        one of ten on-topic febrile-seizure cards a 0 for one narrow point, and
        five on-topic cards a 1 for another. The tag match is provable and
        token-exact (see tags_matching): a non-empty tag_hits means the deck has
        cards filed under this exact topic, full stop. It fires even when the
        subtree was too broad to use as candidates — which is a stronger signal
        of presence, not a weaker one.

        Not a card to select. Surfacing the topic's tangential cards is exactly
        the s1 noise the score floor drops on purpose; this only changes the
        LABEL, and the candidates are already listed in the picker for a manual
        add if he wants one.
        """
        return self.is_gap and bool(self.tag_hits)

    @property
    def topic_cards(self) -> list[int]:
        """For a topic_present gap: this point's OWN-topic cards, surfaced so
        they can be taken by hand. Each is filed token-exactly under a tag branch
        the point named (see topic_present), so it is provably on-topic even
        though no card tests the point's precise claim. Best-first, capped at
        TOPIC_CARD_SURFACE_CAP.

        NOT selected automatically. The judge kept none of them — that is what
        makes the point a gap — and auto-unsuspending cards the judge dropped is
        the "weak yes" Fletcher rules out. But tag membership is a stronger,
        provable signal than the judge's narrow-wording read, so these are worth
        OFFERING: one click takes them, and taking one settles the point as
        covered (see is_gap, which yields to a manual pick). The `is_tag` flag
        is the third slot of rank_debug, set by _rank_candidates; a point with no
        rank_debug (single candidate, no terms) yields nothing here rather than
        guessing, which is fine — that card is still in the picker.

        Gated by _topic_anchored: only offered when the point's OWN subject is
        its topic. For a point that names a sub-entity the deck lacks (SynNeurGe,
        GMA) the tag leg matched only the parent disease, so its tagged cards are
        generic parent cards — not worth offering. That gate removes an offer
        only; the cards remain one click away in the picker / "⌕ Browse".
        """
        if not self.topic_present or not _topic_anchored(self.point):
            return []
        tagged = [n for n in self.nids
                  if self.rank_debug.get(n, (0, 0, 0))[2]]
        return tagged[:TOPIC_CARD_SURFACE_CAP]

    def is_new(self, nid: int) -> bool:
        """Whether the note has any suspended card — i.e. there's something to
        unsuspend. Notes fully in rotation need no action."""
        susp, _total = self.susp.get(nid, (0, 0))
        return susp > 0

    @property
    def new_nids(self) -> list[int]:
        return [n for n in self.final if self.is_new(n)]

    @property
    def in_rotation_nids(self) -> list[int]:
        return [n for n in self.final if not self.is_new(n)]


def _keywords_for(point: LearningPoint) -> list[str]:
    """Tag-leg needles: the TOPIC only.

    Subsystem and system are deliberately excluded. Measured against the real
    deck, falling back to a subsystem needle ('antifungal') matched the whole
    04_Antifungal_Drugs subtree and filled the candidate budget with cards about
    the wrong drug entirely — crowding out the precise hits the term leg had
    already found. If the topic doesn't match a tag, the tag leg has nothing
    useful to say and the term leg carries the concept.
    """
    topic = (point.topic or "").strip()
    if not topic:
        return []
    out = [topic]
    # Tags are written in several conventions; 'aortic_stenosis' should also
    # find 'AorticStenosis' and 'aortic stenosis'.
    loose = topic.replace("_", " ").strip()
    if loose and loose != topic:
        out.append(loose)
    return out


def _collapse_to_roots(tags: list[str]) -> list[str]:
    """Drop tags that sit under another matched tag.

    A needle matches on any :: segment, so 'coeliac_disease' matches both
    '…::Coeliac_disease' and its children '…::Coeliac_disease::01_Epidemiology'.
    Since each tag is queried as `tag:"X" OR tag:"X::*"`, the ancestor already
    covers every descendant — keeping both OR-ed 12 redundant clauses into one
    query and cost seconds on the real deck.
    """
    roots: list[str] = []
    for tag in sorted(set(tags), key=len):   # ancestors are shorter, so first
        if any(tag == r or tag.startswith(r + "::") for r in roots):
            continue
        roots.append(tag)
    return roots


def tag_leg(point: LearningPoint, index: TagIndex, limit: int) -> tuple[list[int], list[str]]:
    """Leg A. Returns (nids, matched tag names). Touches the collection.

    One query for the whole point: every matched tag is OR-ed together rather
    than queried tag-by-tag, because query count — not tag count — is what
    costs seconds on a 118k-card collection.
    """
    tags: list[str] = []
    for kw in _keywords_for(point):
        for tag in index.tags_matching(kw):
            if tag not in tags:
                tags.append(tag)
    if not tags:
        return [], []

    tags = _collapse_to_roots(tags)[:MAX_TAGS_PER_POINT]
    nids = index.nids_for_tags(tags)
    if len(nids) > MAX_TAG_SUBTREE_NOTES:
        # The branch is generic, not a match — say so rather than pretending.
        log.warn(
            f"lecture: tag leg for '{point.topic}' hit {len(nids)} notes "
            f"across {len(tags)} tag(s) — too broad, discarding"
        )
        return [], tags
    return nids[:limit], tags


def _narrow(term: str) -> str:
    """Shorten a term that found nothing.

    Anki ANDs every word, so a term returning zero is usually one word too
    specific. Dropping the LAST word keeps the anchor (which leads) and sheds
    the qualifier — 'amphotericin renal vasoconstriction' → 'amphotericin
    renal'. Returns "" when there's nothing left to drop.
    """
    words = term.split()
    return " ".join(words[:-1]) if len(words) > 2 else ""


def _find(query: str) -> list[int] | None:
    """find_notes with the exception swallowed. None means the query errored,
    which is different from an empty result."""
    try:
        return list(mw.col.find_notes(query))
    except Exception as e:
        log.warn(f"lecture: query failed {query!r}: {e}")
        return None


# An abbreviation the deck uses as a word: TCA, QT, SSRI, GABA-A, CYP450, ARBs.
# Deliberately narrow — only ALL-CAPS/digit tokens of <=5 chars, optionally
# plural. Ordinary words must never match (see _anchor).
_ABBREV_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,4}s?$")


def _anchor(term: str) -> str:
    """Anchor bare abbreviations to a word START.

    Anki's default search is SUBSTRING, not word-matched, which turns short
    abbreviations into catastrophically non-selective terms: bare `TCA` matches
    'suiTCAse' and hits 15,776 notes on this collection. The union for the TCA
    cardiotoxicity point returned 111 notes, of which the first 15 (find_notes
    returns creation order) were Pott disease, DCIS and Brenner tumour — every
    one dropped by the relevance judge, so a point with ~10 perfect cards in the
    deck was reported as a GAP. That is the one failure the coverage report
    cannot survive, and it was a search bug, not a scoring bug.

    `w:` alone is NOT the fix: it also anchors the END, so `w:SSRI` misses the
    deck's "SSRIs" — measured, it drove 8 of 25 abbreviation-bearing terms to
    zero. `w:X*` anchors only the start, which is where the collisions are, and
    drove exactly one to zero (a term whose true answer was zero anyway).

    Ordinary words are left as substrings ON PURPOSE: that is what lets
    'cardiotoxic' match "cardiotoxicity" and 'fluoxetine' match "norfluoxetine".
    """
    return " ".join(f"w:{w}*" if _ABBREV_RE.match(w) else w for w in term.split())


# British/Australian -> American spelling. The lecture is Australian and the
# extracted terms inherit its spelling; the deck is mostly AnKing, which is
# American. Measured on the real run: the point about SSRI hyponatraemia never
# matched its own best card, "What electrolyte imbalance is associated with
# long-term SSRI use? HyponatrEmia" — the term and the card were the same word
# spelled two ways, and the point was left filling its slot with a bleeding
# card instead.
#
# Both spellings are always searched (the term is OR-ed with its variant), never
# swapped: parts of his collection ARE Australian (Malleus, his own cards), so
# replacing would just move the blind spot rather than close it.
_SPELLING_SUBS = (
    ("ae", "e"),      # haematology, anaemia, paediatric, aetiology
    ("oe", "e"),      # oedema, oesophagus, diarrhoea, foetal
)
_SPELLING_SUFFIXES = (
    ("isations", "izations"), ("isation", "ization"),
    ("ising", "izing"), ("ised", "ized"), ("ise", "ize"),
    ("ysed", "yzed"), ("yse", "yze"),
    ("ours", "ors"), ("our", "or"),    # tumour, behaviour, colour
)
# Words where the suffix rules would fire on an ordinary English word rather
# than a spelling difference. 'four' -> 'for' and 'exercise' -> 'exercize' cost
# nothing in results but make the logged query nonsense, and 'precise' is a real
# word a card may contain.
_SPELLING_KEEP = {
    "four", "hour", "hours", "your", "yours", "pour", "sour", "flour", "tour",
    "exercise", "precise", "concise", "wise", "advise", "revise", "promise",
    "rise", "raise", "noise", "cruise", "analysis", "diagnosis",
}


def _us_spelling(word: str) -> str:
    """The American spelling of one word, or "" if it doesn't differ."""
    low = word.lower()
    if low in _SPELLING_KEEP or len(low) < 4:
        return ""
    out = ""
    i = 0
    while i < len(low):
        pair = low[i:i + 2]
        # 'ae'/'oe' -> 'e', including at the start (oedema, oesophagus,
        # oestrogen, aetiology). The one systematic exception is 'aer-', which is
        # a real English/medical stem, not a spelling difference: aerobic,
        # ANaerobic, aerosol would all be mangled into non-words.
        if pair in ("ae", "oe") and low[i + 2:i + 3] != "r":
            out += "e"
            i += 2
            continue
        out += low[i]
        i += 1
    for be, us in _SPELLING_SUFFIXES:
        if out.endswith(be):
            out = out[: -len(be)] + us
            break
    return out if out != low else ""


def _spelling_variant(term: str) -> str:
    """The whole term respelled, or "" if no word in it differs.

    Returns "" rather than an identical string when nothing changes — otherwise
    every term would be OR-ed with a copy of itself, doubling the query text for
    no results.
    """
    changed = False
    words = []
    for w in term.split():
        alt = _us_spelling(w)
        if alt:
            changed = True
        words.append(alt or w)
    return " ".join(words) if changed else ""


def scope_query(kind: str, value: str) -> str:
    """An Anki query fragment restricting a search to one deck or tag subtree.

    Used by Mass AI Create's "skip what a deck or tag already covers": rather
    than measuring a gap against the whole collection, the user names the scope
    that matters — typically the tag their AI Search results were filed under —
    and only that is treated as coverage.

    Returns "" for no scope, which every caller treats as "search everything".
    """
    v = (value or "").strip().replace('"', "")
    if not v:
        return ""
    if (kind or "").strip().lower() == "deck":
        return f'deck:"{v}"'          # deck: already includes subdecks
    return f'(tag:"{v}" OR tag:"{v}::*")'


def _scoped(term: str, notetype_filter: str, scope: str = "") -> str:
    # Each term is internally AND-ed, so it must be parenthesised before being
    # OR-ed with its siblings — without the parens Anki's precedence turns
    # 'a b OR c d' into something else entirely.
    body = f"({_anchor(term)})"
    variant = _spelling_variant(term)
    if variant:
        # OR the spellings INSIDE the term's own parentheses, so the term still
        # behaves as one unit when it's OR-ed with the point's other terms.
        body = f"({_anchor(term)} OR {_anchor(variant)})"
    if notetype_filter:
        body = f'"note:*{notetype_filter}*" {body}'
    return f"{scope} {body}" if scope else body


def _rank_candidates(
    nids: list[int], terms: list[str], tag_set: set[int]
) -> tuple[list[int], dict[int, tuple[int, int, int]], dict[int, frozenset[int]]]:
    """Order the whole candidate pool (both legs) best-first, so the
    MAX_CANDIDATES_PER_POINT cut keeps the cards that actually match the point's
    specific terms — not the generic drug-class subtree Anki's creation order
    happens to surface first. Returns (ordered nids, per-nid debug tuple,
    per-nid set of WHOLE-matched term indices — the last is what
    _spread_by_term needs to give a compound point one card per sub-fact
    instead of N cards for its loudest sub-fact).

    Ranking key, per note:
      1. WHOLE terms matched — every word of a term present (the real signal:
         'SSRI'+'reuptake' both present = a mechanism card, not a coincidence)
      2. term WORDS matched — partial credit so a near-miss still beats noise
      3. is_tag — carries the point's TOPIC tag. A TIEBREAK ONLY, never a lead:
         a correctly-tagged card that matches no term must NOT outrank a card
         that squarely matches two terms. Ordering the legs the other way round
         (tags first) is exactly what buried the SSRI-mechanism card under a
         wall of SIADH/ondansetron cards that merely shared the ::SSRI tag.
      4. original position — stable, deterministic across runs.

    A note matching nothing sorts last but is NOT dropped: Anki matched it for a
    reason this crude substring check can't see (a stripped HTML field, a tag),
    and dropping it here would be an invisible loss the judge should get to veto.
    """
    def _words(t: str) -> list[str]:
        return [w.lower() for w in re.sub(r"[^\w\s-]", " ", t).split()]

    # Each term becomes a list of ACCEPTABLE word-sets: its own spelling and the
    # American one. Without this the search would find the card (the query ORs
    # both spellings) but the ranker wouldn't credit the term for it — the card
    # would land in the pool with no slot, which is how the hyponatraemia card
    # ended up filed under the 'SSRI bleeding' slot.
    term_forms: list[list[list[str]]] = []
    for t in terms:
        forms = [_words(t)]
        variant = _spelling_variant(t)
        if variant:
            forms.append(_words(variant))
        term_forms.append(forms)

    scored = []
    debug: dict[int, tuple[int, int, int]] = {}
    matched: dict[int, frozenset[int]] = {}
    for i, nid in enumerate(nids):
        text = note_text(nid, max_len=1200).lower()
        hit = {ti for ti, forms in enumerate(term_forms)
               if any(words and all(w in text for w in words) for words in forms)}
        whole = len(hit)
        # Partial credit: per term, the best-matching spelling's word count.
        partial = sum(
            max((sum(1 for w in words if w in text) for words in forms), default=0)
            for forms in term_forms
        )
        is_tag = 1 if nid in tag_set else 0
        debug[nid] = (whole, partial, is_tag)
        matched[nid] = frozenset(hit)
        # i keeps the sort stable and deterministic across runs.
        scored.append((-whole, -partial, -is_tag, i, nid))
    scored.sort()
    return [nid for *_rest, nid in scored], debug, matched


def _spread_by_term(ranked: list[int], matched: dict[int, frozenset[int]],
                    n_terms: int,
                    scores: dict[int, int] | None = None,
                    prefer: set[int] | None = None) -> list[int]:
    """Reorder a ranked pool so consecutive cards cover DIFFERENT terms.

    `prefer` (the cards already in the user's review rotation, passed only at
    allocate time) tips WHICH card represents a term-slot toward one already in
    rotation: among the strong cards covering a slot, an in-rotation card is
    picked ahead of a suspended one, so a facet a live card already covers is
    filled by that card and no near-duplicate is unsuspended for the same idea
    (Fletcher's "don't double up" ask). It only reorders WITHIN a slot's
    candidates — the score-based slot ORDER (slot_key) is untouched — and it is a
    no-op when nothing is in rotation (the usual retrieval case). His accepted
    trade-off: a live card may be marginally worse than a suspended one, but it
    is still judge-approved (only `strong` cards are eligible) and the tool fills
    gaps rather than re-litigating the rotation.

    A compound learning point already carries its own decomposition: the
    extractor emits one term per sub-fact. LP 15 of the real pharm run was
    'antidepressants' non-psychiatric uses' with terms ['fluoxetine bulimia',
    'amitriptyline neuropathic', 'amitriptyline migraine', 'imipramine
    enuresis'] — four distinct facts. Pure specificity ranking flattens those
    into one list where the loudest sub-fact wins every slot, so the allocator
    picked TWO near-identical migraine cards and left bulimia, enuresis and
    duloxetine uncovered despite 15 kept candidates.

    Round-robin over the terms fixes it without anyone having to know which
    points are compound, which is why this belongs here and not in the
    extraction prompt. Deal one card per term, best-first, skipping cards
    already taken — so a card that satisfies two terms is spent once and the
    second term falls through to its next-best DISTINCT card.

    Correct on both shapes, which is the property that makes it safe to apply
    unconditionally:
      * compound point (terms = different facts) -> one card per fact;
      * simple point (terms = paraphrases, e.g. ['SSRI reuptake',
        'SSRI serotonin', 'SSRI mechanism']) -> the same cards match most
        terms, so the first round drains them in rank order and this is
        very nearly a no-op.

    Ordering only. Nothing is dropped: cards matching no whole term keep their
    rank order and follow on the tail, so the judge still gets to see them.
    """
    if n_terms <= 1 or not ranked:
        # A single-facet point has no slots to spread, but the in-rotation
        # preference still applies: front-load live cards so, when the pool is
        # deeper than the point's cap, the cards that get selected (and the ones
        # NOT unsuspended) favour what's already in rotation. No-op without
        # `prefer`, and stable within each group so rank order is otherwise kept.
        if prefer and ranked:
            pos = {n: i for i, n in enumerate(ranked)}
            return sorted(ranked, key=lambda n: (n not in prefer, pos[n]))
        return ranked

    # This runs TWICE in a point's life: once at search time (search_point),
    # before the judge has scored anything, and again is available with scores
    # after relevance. At search time `scores` is None — normalise it to {} so
    # every `.get(n, _DEFAULT_KEEP_SCORE)` below falls back to the default and
    # the whole function degrades to a stable term-spread with no scoring, which
    # is exactly what's wanted before the judge has an opinion. (Without this,
    # slot_best's unguarded scores.get crashed the entire run — matcher.py:701.)
    sc = scores or {}

    # Only cards the judge actually rates may LEAD a slot. Run 6 showed why:
    # LP 1 (SSRI mechanism) had three score-3 mechanism cards, all assigned to
    # sub-fact 2, and slots 0 and 1 whose only candidates were score-1 noise
    # ('what sexual dysfunction may be treated with SSRIs?'). Filling every slot
    # regardless of quality put those two in front and pushed the real
    # mechanism card out of the point's budget entirely — a slot filled with
    # rubbish is worse than a slot left empty, because it also costs a card.
    # With no scores yet (sc empty), every card scores _DEFAULT_KEEP_SCORE, so
    # `strong` is the whole ranked pool — correct: nothing has been rejected.
    strong = [n for n in ranked
              if sc.get(n, _DEFAULT_KEEP_SCORE) >= _SLOT_MIN_SCORE]
    if not strong:
        strong = list(ranked)
    strong_set = set(strong)

    # Slots are visited BEST-FIRST, not 0,1,2. For a genuinely compound point
    # every slot holds a strong card and this changes nothing (ties keep slot
    # order). For a point whose "sub-facts" are really paraphrases of one fact,
    # it means the strongest card leads instead of whichever paraphrase happened
    # to be term 0.
    #
    # The tie-break, when two slots hold equally-scored champions, is the RANK of
    # the champion — not the slot index. This is what the CPP run needed: the
    # judge filed the clean definition ('CPP = MAP - ICP', kept#1) under sub-fact
    # j1 and a derivative ('CPP falls when MAP falls') under j0. Both scored 3, so
    # the old `ti` tie-break handed the point's single slot to j0 purely because
    # 0 < 1, burying the kept#1 definition. Breaking the tie by champion rank puts
    # the slot carrying the best card first, so a collapsed one-slot point leads
    # with its cleanest, highest-ranked card. `strong` is in rank order, so a
    # card's index in it IS its rank.
    rank = {n: i for i, n in enumerate(strong)}

    def slot_key(ti: int) -> tuple[int, int, int]:
        members = [n for n in strong if ti in matched.get(n, frozenset())]
        if not members:
            return (0, len(strong), ti)    # empty slots sort last
        best = max(sc.get(n, _DEFAULT_KEEP_SCORE) for n in members)
        champ_rank = min(rank[n] for n in members
                         if sc.get(n, _DEFAULT_KEEP_SCORE) == best)
        return (-best, champ_rank, ti)

    slots = sorted(range(n_terms), key=slot_key)

    # Order the WITHIN-slot search: in-rotation cards first (rank-ordered), then
    # the rest (rank-ordered). `strong` is already rank order, so with no
    # `prefer` this is exactly `strong` and the loop is unchanged; with it, the
    # first card a slot can claim is the live one covering that facet.
    pick_order = strong
    if prefer:
        pick_order = sorted(strong, key=lambda n: (n not in prefer, rank[n]))

    picked: list[int] = []
    seen: set[int] = set()
    while True:
        progressed = False
        for ti in slots:
            for nid in pick_order:
                if nid in seen or ti not in matched.get(nid, frozenset()):
                    continue
                picked.append(nid)
                seen.add(nid)
                progressed = True
                break
        if not progressed:
            break   # every term is out of unclaimed cards
    # Tail: too weak to lead a slot, or matched no term wholly. Kept (never
    # dropped) in rank order — they're still available to the user and to a
    # point with budget left over.
    picked.extend(nid for nid in ranked if nid not in seen)
    return picked


# Safety ceiling on the raw union carried into ranking. _anchor already tames
# the abbreviation collisions that used to return 15k notes, so real unions sit
# well under this; it only guards a pathological term from making the ranker
# load thousands of note texts.
_TERM_UNION_CAP = 200

# Ceiling on what a NARROWED term may return before it stops counting as a hit.
# Only applies after _narrow has chopped a word off — the extractor's own term is
# always honoured however many notes it matches. 40 is comfortably above a real
# two-word medical phrase's yield on this collection ('oxygen consumption' -> 17,
# 'FRC supine' -> 2) and well below the generic-pair floods it exists to stop.
_NARROWED_MAX_HITS = 40


# --- Abbreviation expansion: a deterministic recall backstop -----------------
# The extractor is TOLD to anchor terms on full names, but the model still slips
# and emits an abbreviation anchor ('PD rigidity', 'MSA autonomic'). Anki ANDs
# literal tokens, so 'PD rigidity' matches only the ~7% of Parkinson cards that
# literally write "PD" — the cogwheel-rigidity cards (which say "Parkinson") are
# missed. This rescues them WITHOUT another AI call, by expanding an abbreviation
# to the full name taken from the point's OWN text: a "Full Name (ABBR)" phrase
# in the concept, or the topic's initialism. The expanded variant is OR-ed in
# ALONGSIDE the original (never replaces it), so recall can only rise — and only
# ALL-CAPS tokens are touched, so a unit like '100mg' is never mistaken for the
# 'MG' of myasthenia gravis. Term_cover/slot logic still reads point.terms, so
# this changes what's FOUND, never how the point is decomposed.
# A named entity is 1–4 capitalised words; an abbreviation is 2–6 letters. Decks
# and lecturers write the pairing in BOTH orders — 'Multiple System Atrophy (MSA)'
# and 'PSP (Progressive Supranuclear Palsy)' — so match each.
_ENTITY = r"[A-Z][A-Za-z][\w'’/-]*(?:\s+[A-Za-z][\w'’/-]*){0,3}"
_ABBR = r"[A-Za-z]{2,6}"
_ABBR_DEF_RE = re.compile(rf"({_ENTITY})\s*\(({_ABBR})\)")        # Name (ABBR)
# The reverse form insists on an UPPERCASE abbreviation. With _ABBR's mixed case
# it read "endotracheal tube (ETT)" as tube -> ETT: an ordinary lowercase word
# registered as an abbreviation, pointing at the abbreviation as its "full name".
_ABBR_DEF_REV_RE = re.compile(rf"\b([A-Z]{{2,6}})\s*\(({_ENTITY})\)")  # ABBR (Name)


def _abbr_map(point: LearningPoint) -> dict[str, str]:
    """Abbreviation -> full name, from this point's own text only (no guessing)."""
    m: dict[str, str] = {}
    for name, abbr in _ABBR_DEF_RE.findall(point.concept or ""):
        m.setdefault(abbr.lower(), name.strip())
    for abbr, name in _ABBR_DEF_REV_RE.findall(point.concept or ""):
        m.setdefault(abbr.lower(), name.strip())
    words = [w for w in re.split(r"[_\s]+", point.topic or "") if w]
    if len(words) >= 2 and all(w[:1].isalpha() for w in words):
        # The WHOLE phrase, not words[0]. Mapping the initialism to its first
        # word alone is how 'FRC' became 'functional': 'GA FRC reduction'
        # expanded to 'GA functional reduction', which narrowed to 'GA
        # functional' and matched 56 unrelated notes, and 'FRC desaturation'
        # became 'functional desaturation'. A one-word expansion of an
        # abbreviation is never the concept — it is a stopword with a topic
        # attached, and it poisons the pool it was added to widen.
        m.setdefault("".join(w[0] for w in words).lower(), " ".join(words))
    return m


def _expand_terms(point: LearningPoint) -> list[str]:
    """The point's terms, plus a full-name variant for any that anchor on an
    expandable ALL-CAPS abbreviation. Original terms are always kept."""
    terms = [t for t in point.terms if t.strip()]
    m = _abbr_map(point)
    if not m:
        return terms
    seen = {t.lower() for t in terms}
    out = list(terms)
    for t in terms:
        new = t
        for tok in set(re.findall(r"\b[A-Z]{2,6}\b", t)):
            full = m.get(tok.lower())
            if full:
                new = re.sub(rf"\b{re.escape(tok)}\b", full, new)
        if new != t and new.lower() not in seen:
            out.append(new)
            seen.add(new.lower())
    return out


def term_leg(point: LearningPoint,
             notetype_filter: str,
             scope: str = "") -> tuple[list[int], list[tuple[str, int]]]:
    """Leg B. Returns (raw union nids, per-term hit counts). Touches the
    collection. The pool is NOT ranked or capped to the candidate limit here —
    search_point ranks the whole term ∪ tag pool together and cuts once, so
    ranking a term hit against a tag hit is apples-to-apples.

    The terms are OR-ed into ONE query — the union is what we want anyway, and
    a single find_notes costs 0.3–7s on this collection, so per-term querying
    would cost minutes per lecture.

    Only when the union finds NOTHING do we pay for per-term diagnosis, and
    then each dud term is retried narrowed. A silently-empty search is the
    failure this tool can least afford: it reports a gap for a concept whose
    cards exist, which is the one thing that would make the coverage report
    lie.
    """
    terms = _expand_terms(point)
    if not terms:
        return [], []

    union = " OR ".join(_scoped(t, notetype_filter, scope) for t in terms)
    hits = _find(union)
    if hits:
        if len(hits) > _TERM_UNION_CAP:
            log.warn(
                f"lecture: term union for '{point.topic}' hit {len(hits)} notes; "
                f"ranking the first {_TERM_UNION_CAP}"
            )
        # Counts per term aren't known from a union query, and paying N more
        # queries to learn them is not worth it on the happy path. Ranking (in
        # search_point) rescues the good cards from the arbitrary creation order.
        return hits[:_TERM_UNION_CAP], [("(union)", len(hits))]
    if hits is None:
        return [], [("(union)", -1)]

    # Union found nothing: diagnose each term, narrowing the duds.
    nids: list[int] = []
    seen: set[int] = set()
    per_term: list[tuple[str, int]] = []
    for term in terms:
        attempt = term
        narrowed_from = ""
        while attempt:
            found = _find(_scoped(attempt, notetype_filter, scope))
            if found is None:
                per_term.append((attempt, -1))
                break
            if found and narrowed_from and len(found) > _NARROWED_MAX_HITS:
                # A narrowed term that suddenly matches half the deck is no
                # longer about this point — it is two generic words. Taking its
                # hits would flood the pool and push the point's real cards past
                # the candidate cut: 'clinical groups' -> 127 notes and 'GA
                # functional' -> 56 did exactly that on the respiratory run,
                # burying the two well-targeted terms beside them. Treat it as
                # the dud its parent was.
                log.warn(f"lecture: narrowed '{attempt}' matched {len(found)} "
                         f"notes — too broad to be about '{narrowed_from}', dropping")
                per_term.append((attempt, 0))
                break
            if found:
                per_term.append((attempt, len(found)))
                for nid in found:
                    if nid not in seen:
                        seen.add(nid)
                        nids.append(nid)
                        if len(nids) >= _TERM_UNION_CAP:
                            return nids, per_term
                break
            narrowed = _narrow(attempt)
            if not narrowed:
                per_term.append((attempt, 0))
                break
            log.warn(f"lecture: '{attempt}' found nothing, retrying '{narrowed}'")
            narrowed_from = narrowed_from or term
            attempt = narrowed
    return nids, per_term


def _widened_terms(point: LearningPoint) -> list[str]:
    """A last-resort, deliberately broader term set for a point whose own terms
    found NOTHING.

    term_leg already narrows individual dud terms by dropping their trailing
    qualifier. This goes a step further and falls back to the point's own tag
    levels — topic, then subsystem — which are broad by construction. They
    over-recall, which is exactly right here: the alternative is reporting a gap
    for a concept whose cards exist, and a wrong "you have no card for this" is
    the one error that makes the coverage report lie.

    Returns [] when there is nothing broader to try, so the caller can tell a
    genuine miss from an untried one."""
    out: list[str] = []
    seen = {t.strip().lower() for t in point.terms if t.strip()}
    for level in (point.topic, point.subsystem):
        cand = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(level or "")).strip()
        if len(cand) > 2 and cand.lower() not in seen:
            out.append(cand)
            seen.add(cand.lower())
    return out


def search_point(point: LearningPoint, index: TagIndex, *,
                 notetype_filter: str = "",
                 limit: int = MAX_CANDIDATES_PER_POINT) -> PointMatch:
    """Both legs, unioned, RANKED by term-specificity, then cut. Touches the
    collection.

    The two legs are pooled and ranked TOGETHER (see _rank_candidates), so the
    cut to `limit` keeps the cards that best match the point's terms regardless
    of which leg found them. Tag membership is only a tiebreak — the previous
    'tag hits lead' ordering meant a large drug-class subtree filled all 15
    slots with generic cards and pushed the real term-matching card off the end,
    which is what handed the SSRI-mechanism point SIADH/ondansetron cards.
    """
    tag_nids, tag_hits = tag_leg(point, index, TAG_LEG_POOL)
    # The scope rides on the index, so every search_point caller inherits it
    # without a signature change of its own.
    term_nids, per_term = term_leg(point, notetype_filter, getattr(index, "scope", ""))

    tag_set = set(tag_nids)
    pool: list[int] = []
    seen: set[int] = set()
    for nid in [*term_nids, *tag_nids]:   # order irrelevant; ranking decides
        if nid not in seen:
            seen.add(nid)
            pool.append(nid)

    # Gap re-search. An empty pool means this point is about to be reported as a
    # gap having never been looked for in more than one way. Retry once, broader,
    # BEFORE anyone believes the miss. Costs one query, no AI, and only ever runs
    # on points that found nothing — so a lecture whose points all hit pays
    # nothing for it.
    widened = _widened_terms(point) if not pool else []
    if widened:
        scope = getattr(index, "scope", "")
        query = " OR ".join(_scoped(t, notetype_filter, scope) for t in widened)
        rescued = _find(query) or []
        if rescued:
            log.info(f"lecture: '{point.concept[:60]}' found nothing on its own "
                     f"terms; widened to {widened} and found {len(rescued)} "
                     "candidate(s)")
            for nid in rescued:
                if nid not in seen:
                    seen.add(nid)
                    pool.append(nid)
            # Recorded so the diagnostics can say the pool came from a widened
            # search — these candidates are looser than usual, and the relevance
            # judge is what decides whether any of them actually answer the point.
            per_term = list(per_term) + [(f"{t} (widened)", len(rescued))
                                         for t in widened]

    terms = [t for t in point.terms if t.strip()]
    if terms and len(pool) > 1:
        ranked, rank_debug, matched = _rank_candidates(pool, terms, tag_set)
        # Spread BEFORE the cut, so `limit` slots are shared across the point's
        # sub-facts rather than spent on the loudest one. Cutting first would
        # already have thrown away the only card for a scarce sub-fact.
        ranked = _spread_by_term(ranked, matched, len(terms))
    else:
        ranked, rank_debug, matched = pool, {}, {}

    return PointMatch(
        point=point,
        nids=ranked[:limit],
        per_term=per_term,
        tag_hits=tag_hits,
        rank_debug=rank_debug,
        term_cover=matched,
    )


def annotate_suspended(match: PointMatch) -> None:
    """Fill match.susp for every candidate. Touches the collection."""
    for nid in match.nids:
        if nid in match.susp:
            continue
        try:
            note = mw.col.get_note(nid)
        except Exception:
            match.susp[nid] = (0, 0)
            continue
        match.susp[nid] = anki_utils.note_suspended_state(note)


# Token overlap at or above this collapses two cards into one. Deliberately
# high: medical cards share heavy vocabulary ('SSRI', 'serotonin', 'cause'), so
# anything lower would fuse two DIFFERENT facts. At 0.90 only near-literal twins
# collide — which is exactly the tier this does: cross-deck copies and trivial
# rewordings. Real paraphrases in different words need an AI judge and are out.
_DEDUPE_MIN_JACCARD = 0.90


def _dedupe_normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'SSRIs cause
    hyponatraemia.' and 'ssris cause hyponatraemia' land on one signature."""
    t = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


def dedupe(matches: list[PointMatch]) -> int:
    """Collapse near-identical cards in the kept set so the budget isn't spent
    twice on one fact — the AnKing and Malleus versions of the same card, say.

    LOCAL only: normalised-text identity, plus a high token-overlap threshold
    (_DEDUPE_MIN_JACCARD) for trivial rewordings. Cards that say the same thing
    in genuinely different words survive — catching those needs an AI judge,
    deliberately not done here to keep it free and false-positive-proof.

    Fail-open, like the relevance pass: a card is never dropped if it is a
    point's LAST kept card, so dedupe can NEVER turn a covered point into a
    false gap. Reads note text, so main thread only. Returns the number of
    duplicate cards suppressed.
    """
    scored = [m for m in matches if m.scored]
    order: list[int] = []
    sig: dict[int, str] = {}
    tok: dict[int, frozenset[str]] = {}
    for m in scored:
        for nid in m.kept:
            if nid in sig:
                continue
            norm = _dedupe_normalise(note_text(nid))
            sig[nid] = norm
            tok[nid] = frozenset(norm.split())
            order.append(nid)
    if len(order) < 2:
        return 0

    parent = {nid: nid for nid in order}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # keep the older (smaller) nid as root

    # Exact normalised twins first (the cross-deck copy case)...
    by_norm: dict[str, list[int]] = {}
    for nid in order:
        by_norm.setdefault(sig[nid], []).append(nid)
    for group in by_norm.values():
        for other in group[1:]:
            union(group[0], other)
    # ...then a conservative token-overlap pass for light rewordings. O(n^2)
    # over a few hundred short token sets — milliseconds on the test lecture.
    for i, a in enumerate(order):
        ta = tok[a]
        if not ta:
            continue
        for b in order[i + 1:]:
            tb = tok[b]
            if not tb or find(a) == find(b):
                continue
            inter = len(ta & tb)
            if inter and inter / len(ta | tb) >= _DEDUPE_MIN_JACCARD:
                union(a, b)

    # Winner per group: prefer a card already in rotation (dropping it would just
    # re-suspend something you use, and it still proves coverage), else the older
    # nid.
    in_rotation: dict[int, bool] = {}
    for m in scored:
        for nid in m.kept:
            if not m.is_new(nid):
                in_rotation[nid] = True
            else:
                in_rotation.setdefault(nid, False)
    groups: dict[int, list[int]] = {}
    for nid in order:
        groups.setdefault(find(nid), []).append(nid)
    winner: dict[int, int] = {}
    for members in groups.values():
        best = sorted(members, key=lambda n: (not in_rotation.get(n, False), n))[0]
        for n in members:
            winner[n] = best

    removed = 0
    for m in scored:
        # A scored point with no kept cards is a genuine gap — leave it empty.
        # Without this guard the else-branch below does sorted([])[0], which is
        # the "list index out of range" that sinks the whole run the moment a
        # real gap coexists with real matches.
        if not m.kept:
            continue
        kept_winners = [nid for nid in m.kept if winner.get(nid, nid) == nid]
        if kept_winners:
            new_kept = kept_winners
        else:
            # Every card here is a loser whose winner lives in another point.
            # Keep the single best so the point stays covered — fail open.
            new_kept = [sorted(m.kept, key=lambda n: (not in_rotation.get(n, False), n))[0]]
        removed += len(m.kept) - len(new_kept)
        m.kept = new_kept

    if removed:
        log.info(f"lecture: dedupe suppressed {removed} duplicate card(s)")
    return removed


DEDUPE_SYSTEM = (
    "You are given a numbered list of Anki cards chosen for one study session. "
    "Some are near-duplicates: they test the SAME fact — typically the AnKing "
    "and Malleus versions of one card, or a lightly reworded copy. Seeing both "
    "back to back teaches the student nothing new.\n\n"
    "Find the groups that are redundant with each other. Group ONLY cards that "
    "test the SAME specific fact. Two cards about the same drug or disease but "
    "DIFFERENT facts — one on mechanism, one on side effects; one on diagnosis, "
    "one on treatment — are NOT duplicates; keep them separate. When unsure, do "
    "NOT group them: a wrong merge silently hides a card the student wanted.\n\n"
    'OUTPUT — a JSON object {"duplicates": [[id, id, ...], ...]}, each inner '
    "list a group of card ids that duplicate each other. Include only groups of "
    'two or more. If there are no duplicates, return {"duplicates": []}. Return '
    "ONLY the JSON. No prose, no code fences."
)

# Card text length for the dedupe judge — longer than the relevance judge's
# CARD_TEXT_CHARS, because telling two near-identical cards apart needs the whole
# fact, not just the stem.
DEDUPE_TEXT_CHARS = 200


def selected_card_text(matches: list[PointMatch]) -> tuple[list[tuple[int, str]], dict[int, "PointMatch"]]:
    """Gather (nid, text) for every SELECTED card plus its owning point, first
    occurrence winning. Reads note text, so MAIN THREAD only — split from
    ai_dedupe_selected precisely because that function's AI call must NOT run on
    the main thread (it would freeze the UI and, per the threading rules, a
    Claude call from the main thread can hard-crash Anki)."""
    owner: dict[int, PointMatch] = {}
    cards: list[tuple[int, str]] = []
    for m in matches:
        for nid in m.selected:
            if nid in owner:
                continue
            owner[nid] = m
            cards.append((nid, note_text(nid, max_len=DEDUPE_TEXT_CHARS)))
    return cards, owner


def ai_dedupe_selected(cards: list[tuple[int, str]], owner: dict[int, "PointMatch"],
                       *, model: str | None = None) -> int:
    """Semantic dedupe over the FINAL selected cards — the small set the user
    actually sees (~a budget's worth), so it costs one cheap AI call.

    Catches the same-fact-different-words twins that the local token-overlap
    pass (dedupe(), _DEDUPE_MIN_JACCARD=0.90) is deliberately too strict to fuse
    — the mirtazapine "two cards that are basically the same" case. Removes the
    losers from their point's `selected` list; keeps the first in selection
    order so a point never loses a card to a duplicate that came before it.

    Fails OPEN: an unparseable or failed reply removes nothing. Safe on a worker
    thread — takes pre-extracted text (see selected_card_text) and only mutates
    Python lists. Returns the number of cards removed.
    """
    if len(cards) < 2:
        return 0
    order = [nid for nid, _ in cards]
    lines = [f"{nid}: {text}" for nid, text in cards]
    try:
        reply = core_api.ask_claude_json(
            prompt="CARDS:\n" + "\n".join(lines),
            system=DEDUPE_SYSTEM,
            max_tokens=200 + 8 * len(cards),
            model=model,
            show_errors=False,
        )
    except core_api.ClaudeCancelled:
        raise
    except Exception as e:
        log.warn(f"lecture: AI dedupe call failed, keeping all: {e}")
        return 0

    if not isinstance(reply, dict):
        return 0
    groups = reply.get("duplicates")
    if not isinstance(groups, list):
        return 0

    valid = set(owner)
    removed = 0
    for group in groups:
        if not isinstance(group, list):
            continue
        ids: set[int] = set()
        for g in group:
            try:
                gi = int(g)
            except (TypeError, ValueError):
                continue
            if gi in valid:
                ids.add(gi)
        if len(ids) < 2:
            continue
        # Keep the first in selection order (earliest point, best-ranked within
        # it); drop the rest from wherever they were selected.
        in_order = [nid for nid in order if nid in ids]
        for nid in in_order[1:]:
            m = owner.get(nid)
            if m is not None and nid in m.selected:
                m.selected.remove(nid)
                valid.discard(nid)   # don't let a later group re-drop it
                removed += 1
    if removed:
        log.info(f"lecture: AI dedupe removed {removed} near-duplicate card(s)")
    return removed


# ── card notes: a line of context appended to a committed card ───────────────
#
# The bar is the entire feature, and it is deliberately hard to clear. His words
# on what must NOT happen: "what i meant about generic stuff is you could find it
# anywhere. what is the purpose of you attaching this to this card? fucking
# none." So a line has to add something the card does not already say AND have a
# reason to be on THAT card. Most cards get nothing, and that is the designed
# outcome — an empty result is a pass, never a failure to be padded around.
#
# Note the division of labour, which is not symmetrical:
#   - SLIDE material: the model decides whether it earns a place, and may tidy it.
#   - The USER'S OWN NOTES: they already decided by writing it down. The model
#     only works out WHERE it goes, and never rewords it.
# That asymmetry came straight from him — "the AI won't be good at distilling
# these gems... but i know you can associate the gem with a card."

CARD_NOTES_SYSTEM = (
    "You are given Anki cards a student is about to unsuspend for a lecture, and "
    "the source material behind them. For each card, decide whether any line of "
    "that material is worth appending to it as a note.\n\n"
    "THE BAR — READ THIS TWICE. Nearly everything fails it.\n"
    "A line may be attached ONLY if BOTH are true:\n"
    "  (a) it adds something the card does not already say, and\n"
    "  (b) it has a reason to be on THAT card specifically.\n\n"
    "REJECT, always:\n"
    "- A RESTATEMENT of what the card already tests. If the card asks which "
    "nerves exit the jugular foramen and the line says CN IX, X and XI exit the "
    "jugular foramen, it adds nothing. This is the single most common mistake.\n"
    "- GENERIC TEXTBOOK CONTENT — anything a student could find in any resource, "
    "unattached to this lecture. Being true and even useful is NOT enough. "
    "'The jugular foramen lies between the temporal and occipital bones' is a "
    "fact; it is not a reason to write on this card.\n"
    "- Background, definitions, and scene-setting.\n"
    "- Anything you are unsure about. Silence is free; noise is not.\n\n"
    "ACCEPT only these shapes:\n"
    "- A specific NUMBER, threshold, cutoff, dose or timeframe.\n"
    "- A CAVEAT or EXCEPTION to what the card teaches.\n"
    "- A MNEMONIC or memory hook.\n"
    "- A CLINICAL PEARL that changes what you would do or look for.\n"
    "- Explicit LECTURER EMPHASIS — 'this comes up every year', 'the key point "
    "is', an aside they leaned on.\n\n"
    "MOST CARDS MUST GET NOTHING. Returning an empty list for a card is the "
    "expected result and is never a failure. Do not pad, and do not attach "
    "something weak because a card looks bare.\n"
    f"At most @@MAX_PER_CARD@@ line(s) per card.\n\n"
    "TWO KINDS OF SOURCE, HANDLED DIFFERENTLY:\n"
    "- 'slide N' lines come from the teaching material. You choose which are "
    "worth attaching, and you MAY tidy the wording — fix punctuation, join a "
    "fragment into a sentence, expand an abbreviation. You may NOT add a fact "
    "that is not in the line you were given.\n"
    "- 'note N' lines are the STUDENT'S OWN WORDS. They already decided these "
    "matter by writing them down, so do not judge whether they are worth "
    "keeping and do NOT reword them — copy the text EXACTLY, character for "
    "character, including their shorthand. Your only job for these is deciding "
    "WHICH CARD each belongs on. A note may go on at most one card. Any note you "
    "cannot confidently place, list in `unplaced` — the student will place it "
    "themselves. Never discard one silently.\n"
    "- 'note N (transcript — may be tidied)' lines are an automatic transcript "
    "of the lecturer speaking. Nobody wrote these, so the verbatim rule does "
    "NOT apply: cut the filler ('uh', 'you know', 'OK?'), drop false starts and "
    "repeated words, and close the sentence so it reads as a written note. Keep "
    "every fact, number and qualifier exactly as said, add nothing, and keep it "
    "SHORT — one or two sentences. A rambling quote fails the bar even when the "
    "content behind it would pass.\n\n"
    "OUTPUT — a JSON object, exactly:\n"
    '{"cards": {"<card id>": [{"src": "slide 14", "text": "...", '
    '"why": "..."}]}, "unplaced": [3, 7]}\n'
    "- src: 'slide N' or 'note N', naming where the line came from.\n"
    "- text: the line to append (tidied for slides, VERBATIM for notes).\n"
    "- why: a few words on what it adds to this card. Not shown to the student "
    "on the card; it is there so a bad choice can be spotted.\n"
    "- unplaced: note numbers you could not confidently place. Omit a card "
    "entirely, or give it an empty list, when nothing clears the bar.\n\n"
    "Return ONLY the JSON. No prose, no code fences."
)

# Cards per call. This was 16, on the reasoning that the student's notes had to
# ride along in EVERY call, so fewer calls meant sending them fewer times. That
# reasoning died with notes_for_batch: each batch now carries only the notes its
# own cards could plausibly want, so an extra batch costs an extra handful of
# lines rather than the whole document.
#
# Twelve, then, chosen against the WORKER POOL rather than against token cost:
# what a user waits for is the number of parallel waves, not the number of
# calls. At 4 workers, a 41-card run is 4 batches — one wave, ~3 minutes. Eight
# was measured at 6 batches: two waves and 413s, i.e. twice the wall clock for
# the same work. Batching also bounds the blast radius: a failed call loses its
# own cards' notes, not the run's.
CARDS_PER_NOTES_CALL = 12

# The call is given longer than the 180s default. Measured: even a trimmed batch
# takes 2-3 minutes, because what costs time here is the model reasoning over
# each card against the bar, not the size of the prompt. Eight cards a call keeps
# any single one comfortably inside this, and the batches run in parallel, so the
# run doesn't get slower for being cut up.
CARD_NOTES_TIMEOUT = 420

# How much of a card's text the judge sees. Longer than the relevance judge's
# CARD_TEXT_CHARS: the whole question here is "does the card already say this",
# which cannot be answered from the stem alone.
CARD_NOTES_TEXT_CHARS = 300

# Longest line that may be appended to a card. A paragraph is not a note.
CARD_NOTE_MAX_CHARS = 400

# How many source lines one card may be offered. This is the main cost lever in
# the pass, and it's set low deliberately: the whole feature is supplemental, so
# it should never approach the relevance judge's spend. Eight lines is more than
# enough for the bar to be cleared or not — a card whose ninth-best slide line is
# the pearl was never going to be a good attach anyway.
CARD_NOTES_MAX_LINES_PER_CARD = 8
# Slide lines shorter than this are headings, page numbers and bullet stubs.
_SLIDE_LINE_MIN_CHARS = 20

# The companion notes are the expensive half of this pass: slide lines are
# already scoped to the card that cited them, but a jot doesn't know which card
# it belongs to, so the naive version sends ALL of them in EVERY batch. With a
# lecture transcript attached that is tens of thousands of characters per call,
# repeated per batch, and it pushed the live run to ~2 minutes a call — within a
# breath of the 180s CLI timeout, which is exactly where it landed on a rerun.
#
# So each batch is sent only the notes that share real vocabulary with at least
# one of its cards, best-scoring first. A free local filter in front of a paid
# one: the model still decides everything, it just isn't asked to read the parts
# of the transcript that provably mention nothing on the table. Chunks that
# reach no batch are reported unplaced, which is honest — they weren't placed —
# and the tray was already built to hand them back.
NOTES_PER_NOTES_CALL = 30
# Below this a shared word is a coincidence, not a subject in common.
_NOTE_MATCH_MIN_SCORE = 2.0
_NOTE_WORD_RE = re.compile(r"[a-z][a-z0-9'-]{3,}")
# Words that carry no topic: they'd match every card against every note. Kept
# deliberately short — this is a cheap pre-filter, not a language model, and an
# over-long list starts throwing away real medical vocabulary.
_NOTE_STOPWORDS = frozenset("""
about after also alone always another because been before being below between both
called cannot cause common commonly could depending during each either else
every first following from generally give given have here high higher into itself
just keep less like little llike made major make many more most much must need needs
never next often once only other over particular patient patients rather really
result same seen should since some sometimes still such take than that their them
then there these they thing things this those three through time treat treated
treatment used uses using usually very well were what when where which while will
with within without would your
""".split())


@dataclass
class CardNote:
    """One proposed line for one card, before the user has ticked anything."""
    nid: int
    src: str          # "slide 14" | "note 7"
    text: str
    why: str = ""
    kind: str = "slide"   # "slide" | "note"
    note_index: int = 0   # companion chunk index when kind == "note"
    # A "note" line that came from a transcript rather than the student's own
    # writing, so it was tidied on the way here. The panel names the difference:
    # "your note" and "from the transcript" carry different weight when the user
    # is deciding whether to keep a line.
    tidied: bool = False

    @property
    def key(self) -> str:
        """Stable identity for the (nid, note) pair in the panel's pick set.

        Hashed rather than the text itself because this crosses the pycmd
        bridge, where a colon or a quote in the line would break the message.
        """
        return hashlib.sha1(
            f"{self.nid}|{self.src}|{self.text}".encode("utf-8", "replace")
        ).hexdigest()[:12]


def _slide_lines(text: str) -> list[str]:
    """A slide's text into candidate lines, dropping the stubs.

    Bullets are already one idea each, which is the granularity a note wants;
    joining them back into a paragraph would produce a wall nobody reads on the
    back of a card.
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.strip().lstrip("-*•·–—").strip()
        if len(ln) < _SLIDE_LINE_MIN_CHARS:
            continue
        out.append(ln[:CARD_NOTE_MAX_CHARS])
    return out


def build_card_notes_payload(cards: list[tuple[int, str]],
                             owner: dict[int, "PointMatch"],
                             segments_by_number: dict[int, str],
                             companion=None) -> list[dict]:
    """Per-card payloads: the card, and only the source lines that could
    plausibly belong to it.

    Slide lines are scoped to the slides the card's OWN point cited — sending
    every slide to every card would be both enormous and an invitation to
    attach something unrelated. Companion notes are NOT scoped: the whole point
    of the placement step is that a jot doesn't know which card it belongs to.
    """
    out: list[dict] = []
    note_lines = []
    if companion is not None:
        note_lines = [
            {"src": f"note {c.index}", "text": c.text[:CARD_NOTE_MAX_CHARS]}
            for c in (getattr(companion, "chunks", None) or [])
        ]

    for nid, text in cards:
        m = owner.get(nid)
        point = getattr(m, "point", None) if m is not None else None
        lines: list[dict] = []
        for ref in (getattr(point, "refs", None) or []):
            for ln in _slide_lines(segments_by_number.get(ref, "")):
                lines.append({"src": f"slide {ref}", "text": ln})
                if len(lines) >= CARD_NOTES_MAX_LINES_PER_CARD:
                    break
            if len(lines) >= CARD_NOTES_MAX_LINES_PER_CARD:
                break
        if not lines and not note_lines:
            continue
        out.append({
            "id": nid,
            "card": text,
            "point": getattr(point, "concept", "") or "",
            "lines": lines,
        })
    return out


def _note_terms(text: str) -> set[str]:
    """Topic-carrying words in a piece of text, for the cheap pre-filter."""
    return {w for w in _NOTE_WORD_RE.findall((text or "").lower())
            if w not in _NOTE_STOPWORDS}


def notes_for_batch(batch: list[dict], note_lines: list[dict],
                    limit: int = NOTES_PER_NOTES_CALL) -> list[dict]:
    """The subset of the companion notes worth sending with THIS batch of cards.

    A note is worth sending if it shares real vocabulary with at least one card
    in the batch. Words common across the batch are discounted — in a deck of
    antibiotics cards "antibiotic" tells you nothing about which card a note
    belongs on, while "trimethoprim" tells you almost everything.

    Deliberately generous: this is a pre-filter that decides what the MODEL gets
    to consider, so it should only exclude the provably irrelevant. Under the
    limit, everything scoring at all goes.
    """
    if not note_lines:
        return []
    if len(note_lines) <= limit:
        return note_lines

    card_terms = [_note_terms(f"{item.get('card', '')} {item.get('point', '')}")
                  for item in batch]
    if not any(card_terms):
        return note_lines[:limit]

    freq: dict[str, int] = {}
    for terms in card_terms:
        for w in terms:
            freq[w] = freq.get(w, 0) + 1
    # A word on most of the batch's cards is the batch's subject, not a link to
    # any one card.
    common = max(2, int(len(card_terms) * 0.5))

    scored: list[tuple[float, int, dict]] = []
    for i, ln in enumerate(note_lines):
        terms = _note_terms(ln.get("text", ""))
        best = 0.0
        for ct in card_terms:
            shared = terms & ct
            if not shared:
                continue
            score = sum(1.0 if freq.get(w, 0) <= common else 0.35 for w in shared)
            best = max(best, score)
        if best >= _NOTE_MATCH_MIN_SCORE:
            # Index breaks ties by the order they were written, so the pick is
            # stable across runs rather than dependent on dict ordering.
            scored.append((best, -i, ln))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [ln for _s, _i, ln in scored[:limit]]


def build_card_notes_prompt(payload: list[dict], note_lines: list[dict]) -> str:
    parts: list[str] = []
    for item in payload:
        parts.append(f"CARD {item['id']}")
        parts.append(f"  the card: {item['card']}")
        if item.get("point"):
            parts.append(f"  what it was found for: {item['point']}")
        if item["lines"]:
            parts.append("  source lines from the material behind it:")
            for ln in item["lines"]:
                parts.append(f"    [{ln['src']}] {ln['text']}")
        else:
            parts.append("  (no source lines from the material for this card)")
        parts.append("")
    if note_lines:
        parts.append("NOTES THE STUDENT HANDED OVER — place each on whichever "
                     "card it belongs to, or list it in `unplaced`. Their own "
                     "words go on verbatim; a line marked as transcript is to "
                     "be tidied first:")
        for ln in note_lines:
            parts.append(f"  [{ln['src']}{ln.get('label', '')}] {ln['text']}")
    return "\n".join(parts)


def parse_card_notes_reply(reply, valid_nids: set[int],
                           allowed_text: dict[str, str],
                           max_per_card: int,
                           tidyable: frozenset = frozenset()) -> tuple[list[CardNote], set[int]]:
    """Coerce one reply into (notes, unplaced note indices).

    `allowed_text` maps a note source ("note 7") to the chunk's exact text. For
    anything the student wrote, that text is RESTORED rather than trusted from
    the reply: the model is told not to reword their notes, and this is what
    makes that true instead of merely requested.

    `tidyable` names the sources exempt from that — transcript chunks, where the
    model was asked to cut the filler, so its version is the one wanted. The
    source must still be one we actually sent; a note number we never issued is
    dropped either way, because inventing material is the worst failure here.
    """
    notes: list[CardNote] = []
    unplaced: set[int] = set()
    if not isinstance(reply, dict):
        return notes, unplaced

    placed: set[int] = set()
    per_card: dict[int, int] = {}
    cards = reply.get("cards")
    if isinstance(cards, dict):
        for raw_nid, items in cards.items():
            try:
                nid = int(raw_nid)
            except (TypeError, ValueError):
                continue
            if nid not in valid_nids or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("src") or "").strip().lower()
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                kind, idx = "slide", 0
                if src.startswith("note"):
                    kind = "note"
                    exact = allowed_text.get(src)
                    if exact is None:
                        # A note number we never sent. Dropped rather than
                        # attached — inventing the student's own words is the
                        # worst failure this pass could have.
                        continue
                    if src not in tidyable:
                        text = exact
                    try:
                        idx = int(src.split()[-1])
                    except (ValueError, IndexError):
                        idx = 0
                    if idx in placed:
                        continue          # one card per note
                    placed.add(idx)
                if per_card.get(nid, 0) >= max_per_card:
                    continue
                per_card[nid] = per_card.get(nid, 0) + 1
                notes.append(CardNote(
                    nid=nid,
                    src=str(item.get("src") or "").strip(),
                    text=text[:CARD_NOTE_MAX_CHARS],
                    why=str(item.get("why") or "").strip()[:160],
                    kind=kind,
                    note_index=idx,
                    tidied=(kind == "note" and src in tidyable),
                ))

    for n in (reply.get("unplaced") or []):
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n not in placed:
            unplaced.add(n)
    return notes, unplaced


def pick_card_notes(cards: list[tuple[int, str]], owner: dict[int, "PointMatch"],
                    segments_by_number: dict[int, str], *, companion=None,
                    max_per_card: int = 2, model: str | None = None,
                    workers: int = 1) -> tuple[list[CardNote], set[int]]:
    """Choose the lines worth appending to the selected cards.

    FAILS CLOSED, and this is the one place in the pipeline that does. The
    relevance judge fails open because a card it wrongly drops is invisible — the
    student never learns what they missed. Here the failure is the other way
    round: a line that shouldn't be there gets WRITTEN INTO their notes. So a
    call that fails contributes nothing rather than everything.

    Safe on a worker thread: takes pre-extracted card and slide text, makes only
    AI calls, and returns plain data for the panel to render.
    """
    note_lines = []
    allowed_text: dict[str, str] = {}
    tidyable: set[str] = set()
    if companion is not None:
        for c in (getattr(companion, "chunks", None) or []):
            src = f"note {c.index}"
            text = c.text[:CARD_NOTE_MAX_CHARS]
            # The transcript label travels in the prompt line, so the model can
            # see which rule applies to which note without a second listing.
            note_lines.append({"src": src, "text": text,
                               "label": "" if c.verbatim
                                        else " (transcript — may be tidied)"})
            allowed_text[src] = text
            if not c.verbatim:
                tidyable.add(src)

    payload = build_card_notes_payload(cards, owner, segments_by_number, companion)
    if not payload:
        return [], {c.index for c in (getattr(companion, "chunks", None) or [])}

    system = CARD_NOTES_SYSTEM.replace("@@MAX_PER_CARD@@", str(max_per_card))
    batches = [payload[i:i + CARDS_PER_NOTES_CALL]
               for i in range(0, len(payload), CARDS_PER_NOTES_CALL)]

    def _one(batch: list[dict]) -> tuple[list[CardNote], set[int]]:
        valid = {item["id"] for item in batch}
        try:
            reply = core_api.ask_claude_json(
                prompt=build_card_notes_prompt(batch, notes_for_batch(batch, note_lines)),
                system=system,
                max_tokens=400 + 120 * len(batch),
                model=model,
                show_errors=False,
                timeout=CARD_NOTES_TIMEOUT,
            )
        except core_api.ClaudeCancelled:
            raise
        except Exception as e:
            log.warn(f"lecture: card-note call failed, attaching nothing "
                     f"for {len(batch)} card(s): {e}")
            return [], set()
        return parse_card_notes_reply(reply, valid, allowed_text, max_per_card,
                                      frozenset(tidyable))

    results: list[tuple[list[CardNote], set[int]]] = []
    if workers > 1 and len(batches) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, b) for b in batches]
            for f in as_completed(futures):
                results.append(f.result())
    else:
        results = [_one(b) for b in batches]

    notes: list[CardNote] = []
    placed: set[int] = set()
    for got, _ in results:
        for n in got:
            # Each batch judged the notes independently, so two batches can both
            # claim the same jot. First one wins — a note goes on one card.
            if n.kind == "note":
                if n.note_index in placed:
                    continue
                placed.add(n.note_index)
            notes.append(n)

    all_notes = {c.index for c in (getattr(companion, "chunks", None) or [])}
    unplaced = all_notes - placed
    if notes or unplaced:
        log.info(f"lecture: card notes — {len(notes)} line(s) across "
                 f"{len({n.nid for n in notes})} card(s); {len(unplaced)} note(s) unplaced")
    return notes, unplaced


def slot_cover(m: PointMatch) -> dict[int, frozenset[int]]:
    """nid -> the sub-fact slots that card fills, for THIS point.

    The judge's assignment wins where it exists. It is a judgment about what the
    card is FOR; term_cover is a substring test that can't tell a card's subject
    from a word in its Extra field. Cards the judge didn't assign fall back to
    term_cover, so a partial reply degrades instead of collapsing.
    """
    out: dict[int, frozenset[int]] = {}
    for nid in m.nids:
        slot = m.judge_slot.get(nid)
        out[nid] = frozenset({slot}) if slot is not None and slot >= 0 else \
            m.term_cover.get(nid, frozenset())
    return out


def _point_weight(pool: list[int], term_cover: dict[int, frozenset[int]],
                  n_terms: int, scores: dict[int, int] | None = None) -> int:
    """How many cards it takes to say everything this point is about.

    A greedy set-cover over the point's own terms, walked in rank order: how
    many cards are needed before every term that ANY candidate matches has been
    matched by something selected. That number IS the point's card count, and it
    separates the two compound shapes correctly without a second AI call:

      - A single-fact point ("SSRIs inhibit serotonin reuptake") — its best card
        covers every term at once, so one card closes the cover. Weight 1,
        exactly today's behaviour.
      - A compound of INDEPENDENT facts (the TCA point: bulimia / neuropathic
        pain / migraine / enuresis) — no card covers more than its own sub-fact,
        so it takes four. Weight 4, and the round-robin already put one card for
        each sub-fact at the front of the pool (_spread_by_term), so the four it
        deals are the four different ones.

    Note this measures the CANDIDATES, not the point's wording: a point with
    four terms whose deck coverage is one card gets weight 1, because a second
    card would add nothing the first didn't already say. That's the right answer
    and it's why this is computed here rather than guessed at extraction time.
    """
    if n_terms <= 1 or not pool:
        return 1
    # Only notes the judge rates count. A slot whose sole candidate scored 1 is
    # not a sub-fact the deck covers — it's a term that caught noise — and
    # counting it inflates the point's share, which at a binding budget is taken
    # straight out of another point.
    strong = [n for n in pool
              if scores.get(n, _DEFAULT_KEEP_SCORE) >= _SLOT_MIN_SCORE] if scores \
        else list(pool)
    if not strong:
        return 1
    universe: set[int] = set()
    for nid in strong:
        universe |= term_cover.get(nid, frozenset())
    if len(universe) <= 1:
        return 1
    uncovered = set(universe)
    weight = 0
    for nid in strong:
        gained = term_cover.get(nid, frozenset()) & uncovered
        if not gained:
            continue
        uncovered -= gained
        weight += 1
        if not uncovered:
            break
    return max(1, min(weight, MAX_CARDS_PER_POINT))


def allocate(matches: list[PointMatch], budget: int,
             depth: str = DEFAULT_DEPTH, cap_for=None) -> int:
    """Trim the whole run to a total card budget. Returns the number selected.

    `depth` (see DEPTH_TARGETS) sets how many cards each objective may claim
    beyond its minimum set-cover: "tight" is the historical one-per-sub-fact
    shortlist, "standard"/"thorough" hand each point a cluster of the cards the
    judge already approved. The budget is still the hard ceiling over the total.

    `cap_for(m) -> int` overrides that flat preset with a PER-POINT ceiling.
    KG mode uses it (see tools/kg/triage.py): a broad gap like "Graves + Tx"
    earns a bigger share than a one-assertion gap like "DOACs first line for AF
    long-term", instead of every point being levelled to one depth preset. It
    only ever replaces the preset — a point's set-cover weight still wins when
    it is higher, exactly as with `depth`. None keeps the historical behaviour.

    Round-robin, not first-come. Points are searched in document order, so a
    greedy early point would otherwise eat the entire budget before later
    points got a look in — the failure mode of any total-budget scheme. Dealing
    one card to each point per pass means every point gets its BEST card before
    any point gets a second, and points with only a few cards are never
    penalised for it.

    Within a point, candidate order is already meaningful (tag hits lead, then
    term hits), so taking from the front takes the best.

    A budget is a target, never a quota: nothing is padded to reach it. If the
    deck only has 40 relevant cards, 40 is the answer.
    """
    pools = [list(m.available) for m in matches]
    for m in matches:
        m.selected = []
        m.allocated = True

    covers = [slot_cover(m) for m in matches]

    # Resolve cross-point ownership BEFORE dealing. A note relevant to several
    # points must go to ONE of them — otherwise two points show the same card and
    # the budget pays twice for one fact.
    #
    # Rank alone is the wrong tiebreak, and the real run proved it. The
    # comparative point "in overdose SSRIs are least harmful, TCAs most harmful"
    # lost EVERY TCA overdose card to the dedicated "TCA adverse effects" point,
    # which naturally ranked them higher, and was left showing only a card about
    # managing SSRI overdose — one pole of a two-pole claim. A point dedicated to
    # a drug will always outrank a point that merely compares it.
    #
    # So ownership asks who NEEDS it: a point for which this card is the only
    # candidate filling one of its sub-fact slots beats a point that has other
    # cards for the same slot, regardless of rank. Ties, and cards critical to
    # nobody, fall back to rank exactly as before.
    #
    # Criticality requires a decent score. "There is no point keeping a candidate
    # if it is shit" — a slot filled by a card the judge barely tolerated is not
    # coverage, it's a worse card shown in a better card's place.
    #
    # BUT CRITICALITY CANNOT BEAT A STRICTLY HIGHER SCORE. It used to, and that
    # is a false negative the user can see on his own screen: the mTBI run had
    # one card ("LOC and retrograde amnesia following head injury") scored 3 by
    # 'mTBI involves LOC of less than 30 minutes' and 2 by its neighbour
    # 'post-traumatic amnesia of less than 24 hours'. It was the neighbour's only
    # candidate, so criticality gave it away — and left the LOC point showing a
    # card about a player who explicitly DID NOT lose consciousness. Score leads
    # the key now, so need only decides between points that rate a card the
    # SAME; the SSRI/TCA case that criticality was written for is a tie on 3s
    # and is unaffected.
    critical: list[set[int]] = []
    for m, cover in zip(matches, covers):
        by_slot: dict[int, list[int]] = {}
        for nid in m.available:
            if m.scores.get(nid, _DEFAULT_KEEP_SCORE) < _CRITICAL_MIN_SCORE:
                continue
            for slot in cover.get(nid, frozenset()):
                by_slot.setdefault(slot, []).append(nid)
        critical.append({nids[0] for nids in by_slot.values() if len(nids) == 1})

    # nid -> (-score, 0 if critical else 1, rank, pi). Lowest key wins.
    best_point: dict[int, tuple[int, int, int, int]] = {}
    for pi, pool in enumerate(pools):
        for rank, nid in enumerate(pool):
            score = matches[pi].scores.get(nid, _DEFAULT_KEEP_SCORE)
            key = (-score, 0 if nid in critical[pi] else 1, rank, pi)
            prev = best_point.get(nid)
            if prev is None or key < prev:
                best_point[nid] = key
    pools = [[nid for nid in pool if best_point[nid][3] == pi]
             for pi, pool in enumerate(pools)]

    # Each point's fair share, computed from what its candidates actually cover
    # (see _point_weight). A flat one-card-per-pass deal was the last big source
    # of misses: a point carrying four independent sub-facts got the same two
    # slots as a point making one claim, so three of its four cards were sitting
    # in `kept`, correctly found and correctly judged, and never shown.
    #
    # The pool is then re-spread ACROSS SLOTS. This is the step whose absence
    # made the whole slot design invisible: the searcher spread the candidates by
    # sub-fact, then the judge returned them in SCORE order and the allocator
    # dealt from that, so a 3-card point could spend two of its three slots on
    # the same sub-fact while another sat empty (4 of 9 multi-card points did
    # exactly that). Spreading a score-ordered list means slot 0 gets its
    # best-judged card, then slot 1 gets its best-judged card, and so on.
    # WEIGHT and SPREADING read different covers, on purpose.
    #
    # Spreading (which card lands in which slot) uses the judge's assignment
    # (covers[i] = slot_cover), because the judge knows what a card is FOR and
    # term_cover can't tell subject from a passing mention.
    #
    # But the weight — HOW MANY slots the point earns — must use the raw term
    # footprint (m.term_cover), not the judge's slots. The judge over-splits: on
    # the ICP run it read the CPP point's three near-synonym terms ('CPP MAP',
    # 'CPP ICP formula', 'cerebral perfusion pressure') as three sub-facts and
    # assigned j0/j1/j2, so the set-cover over judge slots gave weight 3 and the
    # allocator dealt two restatements of CPP = MAP - ICP. By the term footprint
    # those cards all occupy the SAME slots ({0,2}), so the cover closes in one
    # card → weight 1, and the dupe never gets a second slot. A genuinely
    # compound point (the TCA bulimia/neuropathic/migraine/enuresis point) still
    # occupies four distinct term footprints → weight 4, unchanged. This is the
    # cover _point_weight was documented around; it had drifted onto slot_cover.
    for i, (m, pool) in enumerate(zip(matches, pools)):
        n_slots = len([t for t in m.point.terms if t.strip()])
        m.weight = _point_weight(pool, m.term_cover, n_slots, m.scores)
        # Cards already in rotation for this point — spread prefers them per
        # facet so a live card fills the slot and its suspended near-duplicate is
        # never unsuspended (the "don't double up on the same idea" rule). Counts
        # toward the point's cap, so an objective a live card already covers pulls
        # fewer NEW cards. is_new reads m.susp, populated by annotate_suspended in
        # search_all before this runs.
        in_rotation = {nid for nid in pool if not m.is_new(nid)}
        pools[i] = _spread_by_term(pool, covers[i], n_slots, m.scores,
                                   prefer=in_rotation)
    if log.enabled():
        heavy = [(i + 1, m.weight) for i, m in enumerate(matches) if m.weight > 1]
        log.trace(f"alloc weights >1: {heavy}")

    # `seen` is now belt-and-suspenders (ownership above already makes pools
    # disjoint), but kept so the budget counts distinct notes even if that
    # invariant ever changes.
    taken = 0
    seen: set[int] = set()

    # Deal in PASSES, and within a pass take the heaviest points first.
    #
    # The pass number is what keeps it fair: nobody gets a 2nd card until every
    # point that wants one has its 1st, and nobody gets a 3rd until every point
    # that wants two has its 2nd. Weight only decides who KEEPS going.
    #
    # The weight-descending order inside a pass matters only at the moment the
    # budget dies, which is the moment that decides what the user actually sees.
    # Dealing in document order meant the last slots went to whichever points sat
    # late in the lecture; now they go to the points with the most left to say,
    # and a point that lost its share lost it to a point carrying more.
    order = sorted(range(len(matches)), key=lambda i: -matches[i].weight)

    def _deal(cap) -> None:
        """Round-robin passes: one card at a time to every point still under
        `cap(m)` that has one left, until the budget or the supply runs out."""
        nonlocal taken
        while taken < budget:
            progressed = False
            for i in order:
                m, pool = matches[i], pools[i]
                if len(m.selected) >= cap(m):
                    continue
                nid = None
                while pool:
                    cand = pool.pop(0)
                    if cand not in seen:
                        nid = cand
                        break
                if nid is None:
                    continue
                seen.add(nid)
                m.selected.append(nid)
                taken += 1
                progressed = True
                if taken >= budget:
                    break
            if not progressed:
                break   # everything exhausted before the budget — that's fine

    # Each point gets up to max(its set-cover weight, the depth preset). "tight"
    # (preset 1) reproduces the old behaviour exactly — weight and NO MORE, one
    # card per distinct sub-fact — for anyone who wants a lean, non-redundant
    # shortlist. "standard"/"thorough" (3/5) deliberately hand a point MORE than
    # its set-cover: a second card on a sub-fact the first already covered is, by
    # the weight's own definition, redundant — but redundancy is exactly the
    # point of a study set, where three cards on cortical layers from three
    # angles IS the coverage. _spread_by_term still runs first, so a point's
    # earliest slots go to DIFFERENT sub-facts (variety before depth), then the
    # preset tops it up with the next-best judged cards. The budget stays the
    # hard ceiling over the sum; under-spending it (a point whose deck simply
    # hasn't got five good cards) is still the correct answer.
    target = DEPTH_TARGETS.get(depth, DEPTH_TARGETS[DEFAULT_DEPTH])
    if cap_for is None:
        _deal(lambda m: max(m.weight, target))
    else:
        # Per-point ceiling (KG mode). Guarded so a bad cap_for can't starve a
        # point below one card or crash the deal mid-allocation.
        def _cap(m):
            try:
                return max(m.weight, max(1, int(cap_for(m))))
            except Exception:
                return max(m.weight, target)
        _deal(_cap)
    return taken


# Field NAMES that hold reference/further-reading material rather than what a
# card TESTS. Matched as substrings, case-folded, against a note's field names.
# Fletcher's rule: a card that only MENTIONS a term in its Extra (e.g. the
# constipation card whose Extra explains the Braak model) is not testing that
# term — so point-searching must not see these fields. Cloze cards are handled
# more precisely (the cloze-bearing field only); this list is the Q&A fallback
# and the safety net for a cloze note with no visible deletion.
_REFERENCE_FIELD_RE = re.compile(
    r"extra|lecture notes|missed question|additional|resources|one by one|"
    r"pathoma|boards and beyond|first aid|sketchy|picmonic|pixorize|physeo|"
    r"bootcamp|\bome\b|mnemonic|ankihub|personal|comment|bibliography|source",
    re.I,
)


def _tested_fields(note) -> list[str]:
    """The field values that represent what a card TESTS, not its reference
    material — see _REFERENCE_FIELD_RE.

    * Cloze notes: only the field(s) actually carrying a cloze deletion — "the
      front" in Fletcher's terms. On AnKing that is Text; Extra/First Aid/Sketchy
      and the rest are reference and excluded, so a term appearing only there no
      longer makes the card look like it tests that term.
    * Q&A notes: front + back with reference-named fields dropped.

    Falls back to all non-empty fields if the structure is unreadable or nothing
    survives the filter — recall-safe, never returns empty when the note has text.
    """
    vals = list(getattr(note, "fields", []) or [])
    try:
        model = note.note_type()
        is_cloze = model.get("type") == 1
        names = [f["name"] for f in model.get("flds", [])]
    except Exception:
        return [v for v in vals if v and v.strip()]
    if is_cloze:
        cloze = [v for v in vals if v and re.search(r"\{\{c\d+::", v)]
        if cloze:
            return cloze
    out = [v for name, v in zip(names, vals)
           if v and v.strip() and not _REFERENCE_FIELD_RE.search(name or "")]
    return out or [v for v in vals if v and v.strip()]


def note_text(nid: int, max_len: int = CARD_TEXT_CHARS) -> str:
    """Plain text of a note for ranking and the relevance judge: the TESTED
    fields only (see _tested_fields), cloze markers removed, truncated. Main
    thread only."""
    try:
        note = mw.col.get_note(nid)
    except Exception:
        return ""
    raw = " · ".join(f for f in _tested_fields(note) if f and f.strip())
    plain = anki_utils.strip_cloze(anki_utils.strip_html(raw))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:max_len - 1] + "…" if len(plain) > max_len else plain


# One scoring unit: a point and its candidates as (nid, card text).
ScoreItem = tuple[LearningPoint, list[tuple[int, str]]]


def _interleave_by_system(items: list[ScoreItem]) -> list[ScoreItem]:
    """Reorder so neighbours are topically distant.

    Batching several points into one call risks the model blurring them —
    exactly the over-inclusion the relevance pass exists to prevent. Extracted
    points arrive in document order, so consecutive ones are usually about the
    same disease, which is the worst possible thing to put in one call. Dealing
    round-robin across systems means a batch is typically Cardio + Neuro + GI,
    where a bleed is obvious to the model rather than tempting.
    """
    buckets: dict[str, list[ScoreItem]] = {}
    for item in items:
        buckets.setdefault(item[0].system or "?", []).append(item)
    out: list[ScoreItem] = []
    while buckets:
        for key in list(buckets):
            out.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return out


def build_relevance_batches(
    items: list[ScoreItem],
    cand_budget: int = CANDIDATES_PER_RELEVANCE_CALL,
    max_points: int = MAX_POINTS_PER_RELEVANCE_CALL,
) -> list[list[ScoreItem]]:
    """Group scorable points into per-call batches, packing by CANDIDATE COUNT.

    Points with no candidates are dropped — there is nothing to judge, and
    they're already gaps. The rest are interleaved across systems (anti-blur),
    then packed greedily: a point joins the current batch unless doing so would
    breach the candidate budget OR the points ceiling, in which case a new batch
    starts. A single point whose candidate count alone exceeds the budget stands
    in a batch of its own — a point is never split across calls, since the
    allocator indexes verdicts by whole point.

    This changes only how points are BUNDLED into calls, never which candidates
    are judged, so it cannot alter a verdict or manufacture a gap — it just bounds
    the work per call (fewer timeouts on dense points) and fuses small points
    (fewer calls when candidates are sparse).
    """
    scorable = [it for it in items if it[1]]
    spread = _interleave_by_system(scorable)
    batches: list[list[ScoreItem]] = []
    cur: list[ScoreItem] = []
    cur_cands = 0
    for it in spread:
        n = len(it[1])
        if cur and (len(cur) >= max_points or cur_cands + n > cand_budget):
            batches.append(cur)
            cur, cur_cands = [], 0
        cur.append(it)
        cur_cands += n
    if cur:
        batches.append(cur)
    return batches


def build_scoring_prompt(batch: list[ScoreItem]) -> str:
    """The relevance request for one batch. Split out so the panel can build it
    on the main thread (note text needs mw.col) and send it from a worker."""
    lines: list[str] = []
    total = 0
    for n, (point, cards) in enumerate(batch, 1):
        # "GAP" rather than "POINT" for an item that came from a knowledge gap
        # (kg_id is set only by tools/kg/triage.py). The number is what the
        # reply is keyed by and is unchanged; the WORD is what switches the
        # judge onto the strict rubric, because a gap is one claim and a lecture
        # point is a heading. See RELEVANCE_SYSTEM.
        head = "GAP" if getattr(point, "kg_id", "") else "POINT"
        lines.append(f"{head} {n}: {point.concept}")
        # The sub-facts ARE the point's search terms, numbered. Using the terms
        # rather than a fresh AI decomposition is deliberate: these same indices
        # are the slots the allocator fills, so a verdict of "sub-fact 2" lands
        # in the slot the searcher built for sub-fact 2. Any other decomposition
        # would have to be reconciled back to them, and a mismatch would be
        # silent.
        subs = [t for t in point.terms if t.strip()]
        if len(subs) > 1:
            lines.append("SUB-FACTS (score each card against the one it serves):")
            lines += [f"  {i} — {t}" for i, t in enumerate(subs)]
        lines.append("CANDIDATES:")
        for nid, text in cards:
            lines.append(f"  {nid}: {text}")
        lines.append("")
        total += len(cards)
    lines.append(
        f"Return verdicts for all {len(batch)} item(s) and all {total} id(s), "
        "keyed by the item number above (1, 2, 3 …), whether it is headed "
        "POINT or GAP."
    )
    return "\n".join(lines)


# Score given to a candidate the model returned but didn't score, or scored
# unparseably. Neutral-KEEP: never drop an unjudged card (fail open), but don't
# promote it above cards the model actively rated a 3 either.
_DEFAULT_KEEP_SCORE = 2


# ── re-search ────────────────────────────────────────────────────────────────
# A second attempt at ONE point, driven by the user. The first search is a
# best guess made blind; by the time he's looking at the result he knows
# something the pipeline never did — that the point is worth cards and these
# aren't them. Modelled on Browse's RESCOPE_SYSTEM (topic + previous terms +
# directive) and AI Create's _regenerate_one (show the model what it produced
# last time, so the retry is informed rather than a re-roll of the same dice).
#
# The critical input is the REJECTED cards. Terms that returned nothing are a
# weak signal — they could be wrong vocabulary or a genuinely absent fact. Terms
# that returned the WRONG cards are a strong one: they say precisely which
# neighbouring vocabulary the deck files under those words, so the model can
# steer away from it. 'benzodiazepine hallucinations' pulling ketamine cards is
# only diagnosable from the ketamine cards.
RESEARCH_SYSTEM = (
    "You are re-writing the Anki search terms for ONE learning point, after the "
    "first attempt failed. You are given the point, the terms already tried with "
    "their hit counts, and — crucially — the cards those terms actually returned "
    "and why they were rejected. Return a JSON array of 3 to 6 new search "
    "strings.\n\n"
    "HOW ANKI SEARCH WORKS (most failures come from ignoring this):\n"
    "Words separated by spaces are ANDed — EVERY word must appear on the same "
    "card. A 3-4 word term like 'benzodiazepine withdrawal seizures' almost "
    "always returns ZERO. Keep terms to an anchor PLUS ONE discriminating "
    "keyword, usually TWO words. To make a term more specific, REPLACE a word, "
    "never append one.\n\n"
    "READ THE REJECTED CARDS — that is what you are being given them for:\n"
    "- Terms that returned NOTHING were the wrong vocabulary, or the fact isn't "
    "in the deck under any wording. Try the deck's angle instead: the drug, the "
    "receptor, the named sign, the classic complication.\n"
    "- Terms that returned the WRONG cards are the informative case. Those cards "
    "show what the deck files under those words. Pick a discriminator that the "
    "rejected cards do NOT contain, so the new term can't land on them again. If "
    "'benzodiazepine hallucinations' returned ketamine cards, 'hallucinations' is "
    "the leaky word — replace it ('benzodiazepine delirium', "
    "'benzodiazepine withdrawal').\n"
    "- Do NOT simply return the old terms with small edits. If a term already "
    "returned only rejects, that route is spent.\n\n"
    "USE THE DECK'S VOCABULARY, NOT LECTURE PROSE. Cards are terse exam "
    "shorthand. Favour drugs, receptors, cytokines, eponyms and named signs over "
    "'mechanism', 'pathogenesis', 'management', which rarely appear as card "
    "words.\n\n"
    "If the point is COMPOUND (several independent facts), give at least one "
    "term per fact — the searcher deals cards out per term, so a fact with no "
    "term of its own cannot be found.\n\n"
    "Return ONLY the JSON array of strings. No prose."
)


def build_research_prompt(m: PointMatch, snippets: dict[int, str]) -> str:
    """The re-search request for one point. `snippets` is nid -> card text,
    gathered by the caller on the main thread (this must stay collection-free
    so it can run on a worker)."""
    lines = [f"Learning point: {m.point.concept}"]
    if m.point.topic:
        lines.append(f"Topic: {m.point.topic}")
    if m.per_term:
        lines.append("Terms already tried (term -> cards found): "
                     + ", ".join(f"'{t}' -> {n}" for t, n in m.per_term))
    else:
        lines.append(f"Terms already tried: {m.point.terms}")
    if m.tag_hits:
        lines.append(f"Tag branches matched: {m.tag_hits[:6]}")

    kept = set(m.kept)
    rejected = [n for n in m.nids if n not in kept]
    if rejected:
        lines.append("\nCards these terms returned that were REJECTED as not "
                     "testing this point:")
        lines += [f"- {snippets.get(n, '')}" for n in rejected[:8] if snippets.get(n)]
    accepted = [n for n in m.nids if n in kept]
    if accepted:
        # Only reachable when he re-searches a point that DID get cards. Say so
        # explicitly, or the model treats them as more rejects and steers away
        # from the one angle that was working.
        lines.append("\nCards already accepted for this point (find DIFFERENT "
                     "angles, don't re-find these):")
        lines += [f"- {snippets.get(n, '')}" for n in accepted[:5] if snippets.get(n)]
    if not rejected and not accepted:
        lines.append("\nThese terms returned NO cards at all.")
    return "\n".join(lines)


def research_terms(prompt: str, *, model: str | None = None) -> list[str]:
    """One AI call for fresh terms. Worker-thread only. Returns [] on any
    failure — the caller keeps the existing terms rather than searching with
    nothing, so a flaky call can't wipe a point's search."""
    try:
        reply = core_api.ask_claude_json(
            prompt=prompt,
            system=RESEARCH_SYSTEM,
            max_tokens=400,
            model=model,
            show_errors=False,
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, friendly = core_api.classify_ai_error(str(e))
        if code == "out_of_usage":
            raise RelevanceUsageExhausted(friendly) from e
        log.error(f"lecture: re-search terms failed [{code}]: {e}")
        return []
    except Exception as e:
        log.error(f"lecture: re-search terms failed: {e}")
        return []
    if isinstance(reply, dict):
        reply = reply.get("terms") or reply.get("search_terms") or []
    if not isinstance(reply, list):
        log.warn(f"lecture: re-search returned {type(reply).__name__}, not a list")
        return []
    out: list[str] = []
    for t in reply:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out[:6]


def _coerce_score(v) -> int:
    """A model's per-card verdict → an int rank 0–3. Tolerates the old boolean
    form, bare numbers, and word verdicts, so a prompt drift can't silently zero
    the whole pass."""
    if isinstance(v, bool):
        return 3 if v else 0
    if isinstance(v, (int, float)):
        return max(0, min(3, int(v)))
    s = str(v).strip().lower()
    if s in {"true", "keep", "yes", "y"}:
        return 3
    if s in {"false", "drop", "no", "n"}:
        return 0
    try:
        return max(0, min(3, int(float(s))))
    except ValueError:
        return _DEFAULT_KEEP_SCORE


@dataclass
class Verdict:
    """One point's relevance result.

    The scores used to be discarded the moment the kept list was sorted, which
    made every later decision blind: allocation couldn't tell a 3 from a 1, so
    "should this point keep a card another point also wants?" had no quality
    input and had to be answered on rank alone. Keeping them is what lets
    ownership refuse to hand a slot a card that merely scraped in.
    """
    kept: list[int] = field(default_factory=list)      # best-first
    scores: dict[int, int] = field(default_factory=dict)   # nid -> 0..3
    slots: dict[int, int] = field(default_factory=dict)    # nid -> sub-fact index
    # False when this verdict is a FAIL-OPEN, not a real judgement — the call
    # timed out / errored, or the reply couldn't be parsed for this point, so its
    # candidates were kept unjudged. The caller uses it to retry, and (if the
    # retry also fails) to flag the point as unfiltered instead of pretending it
    # was scored. A genuine judgement — even one that keeps everything — is True.
    filtered: bool = True
    # WHY it failed open, when filtered is False: "timeout", "network",
    # "cli_error", "ai_error", "empty reply", "unparseable reply", "omitted from
    # reply". Carried through so the flag can say what actually went wrong —
    # timing out is fixable differently from a parse error or a network drop.
    reason: str = ""


def _coerce_verdict(v) -> tuple[int, int]:
    """A per-card reply value → (score, slot). Accepts a bare score, or the
    [score, sub-fact] pair asked for on compound points. Slot -1 means the model
    didn't assign one."""
    if isinstance(v, (list, tuple)) and v:
        slot = -1
        if len(v) > 1:
            try:
                slot = int(v[1])
            except (TypeError, ValueError):
                slot = -1
        return _coerce_score(v[0]), slot
    if isinstance(v, dict):
        score = v.get("score", v.get("s", _DEFAULT_KEEP_SCORE))
        try:
            slot = int(v.get("subfact", v.get("slot", -1)))
        except (TypeError, ValueError):
            slot = -1
        return _coerce_score(score), slot
    return _coerce_score(v), -1


def parse_scoring_reply(reply, batch: list[ScoreItem],
                        min_score: int = _KEEP_MIN_SCORE) -> list[Verdict]:
    """Kept nids per point, ORDERED BEST-FIRST by the model's relevance score.

    The order is the whole reason this now returns a rank, not a keep/drop set:
    allocate() takes cards from the FRONT of each point's list, so a point's
    best card must lead. Previously the survivors kept their arbitrary search
    order and the budget picked whatever happened to be first — the "wrong
    winner" bug where a tangential card beat the one that actually tested the
    point. A score of 0 drops the card; 1–3 keep it, higher first, with original
    order as a stable tiebreak.

    Fails OPEN at every level — a missing point, a missing id, or an
    unparseable reply keeps the candidates (unranked, original order). Over-
    inclusion shows extra cards the user unticks; under-inclusion silently hides
    cards they wanted and reports a false gap. A report that lies is worse than
    a noisy one.
    """
    all_nids = [[nid for nid, _ in cards] for _, cards in batch]
    # Per-item keep floor: _GAP_MIN_SCORE for an item that came from a knowledge
    # gap, `min_score` for a lecture point. Raised only when `min_score` is the
    # main run's default — a re-search passes RESEARCH_MIN_SCORE to be lenient
    # and must not be overridden into being the strictest pass of all.
    floors = [
        _GAP_MIN_SCORE
        if (min_score == _KEEP_MIN_SCORE and getattr(point, "kg_id", ""))
        else min_score
        for point, _cards in batch
    ]
    if not isinstance(reply, dict):
        log.warn(f"lecture: relevance reply was {type(reply).__name__}, keeping all")
        return [Verdict(kept=list(nids), filtered=False, reason="unparseable reply")
                for nids in all_nids]
    # min_score is the keep floor; the main run uses _KEEP_MIN_SCORE (2), a
    # re-search passes RESEARCH_MIN_SCORE (1) for a broader, more lenient pass.

    # Accept both {"1": {...}} and a bare {nid: score} when only one point was
    # sent — models collapse the outer level when there's nothing to key on. A
    # compound point's values are LISTS now, so "does any value look like a
    # section" must not be fooled by [3, 0].
    single = len(batch) == 1 and not any(
        isinstance(v, dict) and not {"score", "s", "subfact", "slot"} & set(v)
        for v in reply.values())
    out: list[Verdict] = []
    for n, nids in enumerate(all_nids, 1):
        section = reply if single else reply.get(str(n), reply.get(n))
        if not isinstance(section, dict):
            log.warn(f"lecture: relevance reply omitted point {n}, keeping its {len(nids)} card(s)")
            out.append(Verdict(kept=list(nids), filtered=False, reason="omitted from reply"))
            continue
        scores: dict[int, int] = {}
        slots: dict[int, int] = {}
        for k, v in section.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            score, slot = _coerce_verdict(v)
            scores[key] = score
            if slot >= 0:
                slots[key] = slot
        missing = [x for x in nids if x not in scores]
        if missing:
            log.warn(f"lecture: point {n} reply omitted {len(missing)} id(s), keeping them")
        # Keep score >= this item's floor; sort by score desc, original index
        # as a stable tiebreak. An unscored card is kept WHATEVER the floor (it
        # ranks at _DEFAULT_KEEP_SCORE), so a judge FAILURE still fails open —
        # only a score the model actually gave can drop a card. See
        # _KEEP_MIN_SCORE and _GAP_MIN_SCORE.
        ranked = sorted(
            ((x, scores.get(x, _DEFAULT_KEEP_SCORE), i) for i, x in enumerate(nids)),
            key=lambda t: (-t[1], t[2]),
        )
        out.append(Verdict(
            kept=[x for x, s, _ in ranked
                  if s >= floors[n - 1] or x not in scores],
            scores={x: scores.get(x, _DEFAULT_KEEP_SCORE) for x in nids},
            slots={x: s for x, s in slots.items() if x in set(nids)},
        ))
    return out


@dataclass
class Coverage:
    """What the lecture asked for vs what the deck actually has.

    The gap list is the point of this: it's the honest answer to "what does my
    deck NOT cover", and it's surfaced for the user to act on rather than
    auto-filled. Generating cards for the gaps would rebuild the very thing
    this tool exists to replace.
    """
    total_points: int = 0
    covered: list[PointMatch] = field(default_factory=list)
    gaps: list[PointMatch] = field(default_factory=list)
    selected_cards: int = 0
    new_cards: int = 0          # suspended — the ones there's action to take on
    in_rotation_cards: int = 0  # already studying these
    budget: int = 0
    # Set when the relevance pass couldn't finish (e.g. usage ran out partway):
    # some points show raw, unranked search hits rather than filtered cards, and
    # the user needs to be told so a degraded run isn't mistaken for a good one.
    degraded_note: str = ""
    # Proposed lines to append to committed cards, and the companion-note
    # indices the pass couldn't place. Both empty unless "Add notes to cards" is
    # on. Kept on Coverage rather than on each PointMatch because a note is
    # attached to a CARD, and one card can be shared between points.
    card_notes: list = field(default_factory=list)
    unplaced_notes: list = field(default_factory=list)

    def carry_run_state(self, old: "Coverage") -> "Coverage":
        """Copy the fields that belong to the RUN, not to the current selection.

        `summarise` re-derives a Coverage from the matches every time the user
        takes, drops or re-searches a card — but it only sees the matches, so
        anything the pipeline computed once and hung on Coverage is silently
        reset to its default. That cost the card-note pass its entire output the
        first time out: the lines were found, then the first untick replaced the
        Coverage and both the chips and the commit had nothing left to read.

        So every run-scoped field goes through here, and adding a new one to the
        dataclass means adding it to this list — not remembering to copy it at
        each of the call sites.
        """
        self.degraded_note = old.degraded_note
        self.card_notes = old.card_notes
        self.unplaced_notes = old.unplaced_notes
        return self

    @property
    def budget_capped(self) -> bool:
        """Whether the budget, not the deck, limited the result — the user is
        being shown less than exists and should know they can raise it."""
        return self.selected_cards >= self.budget > 0

    @property
    def covered_elsewhere(self) -> list[PointMatch]:
        """Points that show nothing of their own only because a sibling holds the
        same card (see _resolve_covered_by). Genuinely covered — the card gets
        unsuspended under the sibling — so counted with the covered, not the
        squeezed or the gaps."""
        return [m for m in self.covered if m.covered_by is not None and not m.final]

    @property
    def uncovered_by_budget(self) -> list[PointMatch]:
        """Points with cards that the budget squeezed out entirely. Not gaps —
        the deck covers them — but the user saw nothing for them. Excludes points
        covered by a sibling: those aren't a budget casualty, their card is on
        screen under the neighbour."""
        return [m for m in self.covered
                if not m.final and m.covered_by is None]

    @property
    def shown(self) -> list[PointMatch]:
        """Points the user actually walks away with cards for. THIS is what the
        headline percentage must count. A point covered by a sibling counts too:
        the user does walk away with a card for it, just one shared with its
        neighbour."""
        return [m for m in self.covered
                if m.final or m.covered_by is not None]

    @property
    def true_gaps(self) -> list[PointMatch]:
        """Gaps where the deck genuinely lacks the topic — no tag matched.
        These are the honest "make a card" candidates."""
        return [g for g in self.gaps if not g.topic_present]

    @property
    def topic_gaps(self) -> list[PointMatch]:
        """Gaps where the deck HAS the topic tagged, but no card tests this
        specific point (see PointMatch.topic_present). Reported apart from true
        gaps so "gap" never implies "your deck is empty here" when it plainly
        isn't — the point's own topic cards are sitting in the picker."""
        return [g for g in self.gaps if g.topic_present]

    def summary(self) -> str:
        n = self.total_points
        # Count points the user got CARDS for, not points that merely had
        # candidates. A point whose cards all lost the budget lottery, or whose
        # candidates the judge rejected outright, shows the user nothing —
        # counting it as "covered" inflates the headline against what's on
        # screen. Budget-squeezed points are still not gaps (the deck does cover
        # them), so they're reported separately rather than folded into gaps.
        c = len(self.shown)
        pct = round(100 * c / n) if n else 0
        bits = [
            f"{c}/{n} learning points covered ({pct}%)",
            f"{self.selected_cards} notes",
        ]
        if self.new_cards:
            bits.append(f"{self.new_cards} to unsuspend")
        if self.in_rotation_cards:
            bits.append(f"{self.in_rotation_cards} already in rotation")
        elsewhere = len(self.covered_elsewhere)
        if elsewhere:
            bits.append(f"{elsewhere} covered by a sibling")
        squeezed = len(self.uncovered_by_budget)
        if squeezed:
            bits.append(f"{squeezed} no notes shown")
        # Split gaps: a "no direct card" point sits on a topic the deck DOES
        # carry (its cards are in the picker), so lumping it with true gaps
        # ("your deck doesn't cover this") is misleading — count them apart.
        tgap = len(self.topic_gaps)
        if tgap:
            bits.append(f"{tgap} no direct card")
        true_gaps = len(self.true_gaps)
        if true_gaps:
            bits.append(f"{true_gaps} gaps")
        return " · ".join(bits)


def _resolve_covered_by(matches: list[PointMatch]) -> None:
    """Flag points that render empty ONLY because a sibling won their cards.

    Cross-point ownership (see allocate) makes a card that several points want go
    to exactly one of them, so the budget never pays twice for one fact. That is
    right for unrelated points, but it makes a granular point read as a gap when
    its neighbour holds the same card: 'PD rigidity' keeps the TRAPSS mnemonic,
    'the cardinal motor features' keeps it too, ownership hands it to one, and the
    other shows 'no notes' — a hole the deck does not actually have. The content
    is on a card the user unsuspends; it just lives under the sibling.

    Distinguish that from a genuine budget squeeze: a point squeezed out keeps its
    OWN cards in its pool (they were never contested), so none of its kept cards
    turn up in another point's final. A cannibalised point's kept cards DO. So the
    rule is exact — final is empty AND some other point shows a card this one
    kept → covered by that sibling; final is empty and nobody else shows its cards
    → really squeezed by the budget, reported as before.

    Idempotent: recomputed every summarise() so a manual add/remove that gives the
    point its own card again clears the flag (the property guards on `not final`).
    """
    # Every nid on show, and which points show it. A point with an empty `final`
    # contributes nothing here, so an empty point can never become an "owner" —
    # covered_by therefore always points at a point that has cards, never a cycle.
    owner_of: dict[int, list[PointMatch]] = {}
    for o in matches:
        for nid in o.final:
            owner_of.setdefault(nid, []).append(o)

    for m in matches:
        m.covered_by = None
        m.top_elsewhere = None
    # Umbrella case: a point that HAS a card, but whose single best card (kept[0])
    # was handed to a sibling by ownership, so it shows a weaker fallback. Not a
    # gap and not empty — just flagged so the row can point at where the strong
    # card went. Kept separate from the empty-point covered_by logic below.
    for m in matches:
        if not m.scored or not m.final or not m.kept:
            continue
        top = m.kept[0]
        if top in set(m.final):
            continue                    # it DID get its best card — nothing to say
        sib = next((o for o in owner_of.get(top, ()) if o is not m), None)
        if sib is not None:
            m.top_elsewhere = sib
            if log.enabled():
                log.trace(
                    f"top-elsewhere: LP '{m.point.topic}' best card shown under "
                    f"LP '{sib.point.topic}'")
    for m in matches:
        if not m.scored or m.final or not m.kept:
            continue
        tally: dict[int, int] = {}      # id(owner) -> shared-card count
        owners: dict[int, PointMatch] = {}
        for nid in m.kept:
            for o in owner_of.get(nid, ()):
                if o is m:
                    continue
                tally[id(o)] = tally.get(id(o), 0) + 1
                owners[id(o)] = o
        if not tally:
            continue      # nothing of this point's is on show elsewhere — a real squeeze
        best = max(tally, key=lambda k: tally[k])
        m.covered_by = owners[best]
        if log.enabled():
            log.trace(
                f"covered-elsewhere: LP '{m.point.topic}' -> "
                f"LP '{owners[best].point.topic}' "
                f"({tally[best]}/{len(m.kept)} kept card(s) on show there)")


def summarise(matches: list[PointMatch], budget: int) -> Coverage:
    """Fold the finished matches into the coverage report.

    annotate_suspended() must have run first. Without it every card reads as
    already-in-rotation and the report claims there is nothing to unsuspend —
    the precise opposite of the truth, and silent. Loud is better.
    """
    _resolve_covered_by(matches)
    unannotated = [m for m in matches if m.final and not m.susp]
    if unannotated:
        log.error(
            f"lecture: {len(unannotated)} match(es) reached summarise() without "
            "suspension data — new/in-rotation counts will be wrong. "
            "annotate_suspended() must run before summarise()."
        )

    cov = Coverage(total_points=len(matches), budget=budget)
    for m in matches:
        (cov.gaps if m.is_gap else cov.covered).append(m)
        cov.selected_cards += len(m.final)
        cov.new_cards += len(m.new_nids)
        cov.in_rotation_cards += len(m.in_rotation_nids)
    return cov


def score_batch(batch: list[ScoreItem], *, model: str | None = None,
                min_score: int = _KEEP_MIN_SCORE) -> list[Verdict]:
    """One AI call for a batch of points: which candidates actually test each?

    Safe on a worker thread — takes pre-extracted card text and never touches
    mw.col. Fails open (keeps everything) so a flaky call can't manufacture a
    gap for a concept whose cards exist. `min_score` is the keep floor — the
    default ≥2 for a main run, RESEARCH_MIN_SCORE (1) for a lenient re-search.
    """
    if not batch:
        return []
    # Fail-open fallback: keep every candidate, flagged unfiltered so the caller
    # can retry and, failing that, tell the user this point wasn't judged.
    all_nids = [Verdict(kept=[nid for nid, _ in cards], filtered=False)
                for _, cards in batch]
    names = "; ".join((p.topic or p.concept or "?")[:50] for p, _ in batch)

    def _failed(reason: str) -> list[Verdict]:
        for v in all_nids:
            v.reason = reason
        return all_nids

    try:
        reply = core_api.ask_claude_json(
            prompt=build_scoring_prompt(batch),
            system=RELEVANCE_SYSTEM,
            # Scales with the batch: every candidate id needs a verdict, and a
            # truncated reply would silently drop the tail of the last point.
            max_tokens=400 + 60 * sum(len(c) for _, c in batch),
            model=model,
            timeout=RELEVANCE_TIMEOUT,
            show_errors=False,
            # Raise instead of returning None on failure so we can tell the
            # out-of-usage case (abort the whole pass) from a one-off flake
            # (fail open on this batch, keep going).
            raise_on_error=True,
        )
    except core_api.ClaudeCancelled:
        raise
    except core_api.ClaudeError as e:
        code, _friendly = core_api.classify_ai_error(str(e))
        if code == "out_of_usage":
            raise RelevanceUsageExhausted(_friendly) from e
        # A transient provider failure: keep this batch's candidates and let the
        # caller try the next one. `code` (timeout / network / cli_error / …) is
        # carried on the verdict so the flag can name the actual cause.
        log.error(f"lecture: relevance call failed [{code}] ({len(batch)} point(s): {names}): {e}")
        return _failed(code)
    except Exception as e:
        log.error(f"lecture: relevance call failed ({len(batch)} point(s): {names}): {e}")
        return _failed("error")

    if reply is None:
        log.warn(f"lecture: relevance returned nothing for {len(batch)} point(s): {names}")
        return _failed("empty reply")
    return parse_scoring_reply(reply, batch, min_score=min_score)
