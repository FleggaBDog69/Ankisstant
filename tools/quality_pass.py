# Card Quality Pass — scores generated cloze cards against a retrieval-force
# rubric before they reach the review screen, and turns the score into an action
# (pass / flag / regenerate).
#
# This module is deliberately Qt-free and (almost) Anki-free so it can run on a
# background thread and be unit-tested in isolation. The only addon import is the
# pure-text helpers in core.anki_utils; everything else is stdlib.
#
# Design contract: NOTHING in here may raise out to the caller. Grading is a
# best-effort enhancement — if anything goes wrong we fail OPEN (treat the card
# as PASS) so card creation is never blocked. See apply_prefilters_then / the
# parse_* functions, which all swallow and degrade gracefully.

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    from ..core.anki_utils import strip_html
except Exception:  # pragma: no cover - allows standalone import in tests
    import html as _html

    def strip_html(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s or "")
        s = _html.unescape(s)
        return re.sub(r"\s+", " ", s).strip()


# The ceiling on a cloze front, in visible words. Mirrors
# card_creator.MAX_FRONT_WORDS — duplicated rather than imported because this
# module must stay importable without card_creator (Qt) for the tests and the
# background worker. test_concision.py pins the two together.
MAX_FRONT_WORDS = 25


# ── verdict model ───────────────────────────────────────────────────────────

PASS = "pass"
FLAG = "flag"
FAIL = "fail"   # == "regenerate" target


@dataclass
class Verdict:
    verdict: str = PASS              # PASS | FLAG | FAIL
    worst_dim: str = ""              # "D1".."D5" or ""
    reason: str = ""                 # one line; pre-fills the regen hint on FLAG
    percent: int | None = None       # 0-100, optional
    scores: dict | None = None       # {"D1":0,...}, optional
    source: str = "rubric"           # "prefilter" | "rubric" | "skill" | "error"

    def chip(self) -> str:
        """Short header chip text, e.g. '🟡 FLAG (D1) · 68%'. The score (percent)
        is appended when the grader returned one (deterministic pre-filters
        often don't)."""
        if self.verdict == PASS:
            label = "🟢 PASS"
        elif self.verdict == FLAG:
            label = f"🟡 FLAG ({self.worst_dim})" if self.worst_dim else "🟡 FLAG"
        else:
            label = f"🔴 {self.worst_dim}" if self.worst_dim else "🔴 FAIL"
        if self.percent is not None:
            label += f" · {self.percent}%"
        return label


def _passed() -> Verdict:
    """A clean PASS sentinel (used as the fail-open default)."""
    return Verdict(verdict=PASS, source="rubric")


# ── cloze parsing helpers ───────────────────────────────────────────────────

# {{c1::answer}} or {{c1::answer::hint}}. Non-greedy so adjacent clozes don't
# merge; DOTALL so multi-line answers are captured.
_CLOZE_RE = re.compile(r"\{\{c(\d+)::(.+?)(?:::(.+?))?\}\}", re.DOTALL)


def cloze_specs(front: str) -> list[tuple[int, str]]:
    """Return [(group_number, answer_text), ...] for every cloze in `front`.
    Answer text is HTML-stripped; hints are dropped."""
    out: list[tuple[int, str]] = []
    for m in _CLOZE_RE.finditer(front or ""):
        try:
            grp = int(m.group(1))
        except (TypeError, ValueError):
            continue
        ans = strip_html(m.group(2) or "")
        if ans:
            out.append((grp, ans))
    return out


def visible_front(front: str) -> str:
    """The front as the learner sees it with EVERY cloze blanked out — i.e. the
    non-cloze surrounding prose, HTML-stripped. Used by the answer-in-prompt
    check (does an answer leak into the visible surrounding text?)."""
    blanked = _CLOZE_RE.sub(" ____ ", front or "")
    return strip_html(blanked)


def front_word_count(front: str) -> int:
    """Visible words on a cloze front as the learner reads it once revealed:
    cloze ANSWERS count, hints and the {{c1:: }} markup don't, HTML and entities
    are stripped. A token with no letter or digit in it ("—", "→", "/") is
    punctuation, not a word. Never raises."""
    try:
        txt = _CLOZE_RE.sub(lambda m: " " + (m.group(2) or "") + " ", front or "")
        txt = strip_html(txt)
        return sum(1 for t in txt.split() if re.search(r"[A-Za-z0-9]", t))
    except Exception:  # pragma: no cover - defensive
        return 0


def _is_one_by_one(card) -> bool:
    return isinstance(card, dict) and bool(card.get("one_by_one"))


# The most items a one-by-one list card should hold before it wants splitting.
# Fletcher's call (2026-10-10): one-by-one is for sets of 3-8.
MAX_ONE_BY_ONE_ITEMS = 8


def card_word_count(card) -> int:
    """The number the length limit is applied to. For an ordinary card, every
    visible word (front_word_count). For a ONE-BY-ONE list card, only the STEM —
    the text outside the clozes — because the items are revealed one at a time
    and each is held to 1-4 words separately; counting them would push every
    honest five-item list over the limit."""
    front = card.get("front", "") if isinstance(card, dict) else str(card or "")
    if not _is_one_by_one(card):
        return front_word_count(front)
    try:
        return sum(1 for t in visible_front(front).split()
                   if re.search(r"[A-Za-z0-9]", t))
    except Exception:  # pragma: no cover - defensive
        return 0


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    """Lowercased alphanumeric tokens; punctuation and HTML entities dropped."""
    return _WORD_RE.findall((s or "").lower())


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True if `needle` appears as a contiguous run inside `haystack`."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    n = len(needle)
    for i, tok in enumerate(haystack):
        if tok == first and haystack[i:i + n] == needle:
            return True
    return False


# ── deterministic pre-filters (no LLM, run on every provider) ───────────────

def prefilter_card(card: dict, max_clozes: int = 1,
                   max_words: int = MAX_FRONT_WORDS) -> Verdict | None:
    """Cheap deterministic checks. Returns a Verdict when a rule trips, or None
    ('no opinion — let the LLM decide'). Never raises.

    - answer-in-prompt  → FAIL / D1: a cloze answer is echoed verbatim in the
      visible surrounding text, so the prompt gives the answer away.
    - length             → FAIL / D4: the front is over `max_words` visible
      words. Deterministic because a grader asked "is this concise?" is lenient
      about exactly the cards that drifted long — the length is a number, so it
      is checked as one.
    - cloze count        → FLAG / D2: MORE than `max_clozes` DISTINCT cloze
      groups on one note (repeated same-group clozes are one fact, not flagged).
      `max_clozes` is the user-configured atomicity sensitivity
      (quality_pass.atomicity_max_clozes) — e.g. 2 means a clean 2-cloze note
      passes and only 3+ independent clozes flag.

    answer-in-prompt (FAIL) takes precedence over length (FAIL), which takes
    precedence over cloze-count (FLAG).
    """
    try:
        front = card.get("front", "") if isinstance(card, dict) else ""
        specs = cloze_specs(front)
        if not specs:
            return None

        vis_tokens = _tokens(visible_front(front))
        for _grp, ans in specs:
            ans_tokens = _tokens(ans)
            # Ignore trivially short answers ('a', 'an') — too noisy to gate on.
            if not ans_tokens or (len(ans_tokens) == 1 and len(ans_tokens[0]) < 3):
                continue
            if _is_subsequence(ans_tokens, vis_tokens):
                return Verdict(
                    FAIL, "D1",
                    "the cloze answer is echoed in the visible prompt — reword the "
                    "cue so the answer isn't in it (keep it short and direct).",
                    source="prefilter",
                )

        try:
            cap = int(max_words)
        except (TypeError, ValueError):
            cap = MAX_FRONT_WORDS
        n_words = card_word_count(card)
        if cap > 0 and n_words > cap:
            what = ("the text outside the clozes" if _is_one_by_one(card)
                    else "the front")
            return Verdict(
                FAIL, "D4",
                f"{what} is {n_words} words (limit {cap}) — cut it to the one "
                "fact being tested; move any teaching to Extra or split it into "
                "two cards.",
                source="prefilter",
            )
        if _is_one_by_one(card) and len(specs) > MAX_ONE_BY_ONE_ITEMS:
            return Verdict(
                FLAG, "D2",
                f"a one-by-one list of {len(specs)} items — over "
                f"{MAX_ONE_BY_ONE_ITEMS}, split it into separate cards or "
                "smaller sets.",
                source="prefilter",
            )

        # TODO follow-up: cross-cloze leakage — when {{c1}} and {{c2}} sit on one
        # note, each visible half can telegraph the other's hidden answer. Not
        # detected here (the substring check only looks at non-cloze prose). The
        # cleanest mitigation is the cloze-count FLAG below nudging toward atomic
        # single-cloze notes.
        distinct_groups = {grp for grp, _ in specs}
        try:
            limit = int(max_clozes)
        except (TypeError, ValueError):
            limit = 1
        if limit < 1:
            limit = 1
        if len(distinct_groups) > limit:
            return Verdict(
                FLAG, "D2",
                f"more than {limit} independent clozes on one note — consider "
                "splitting into separate atomic cards (or use hint syntax to make "
                "it one retrieval).",
                source="prefilter",
            )
    except Exception as e:  # pragma: no cover - defensive
        print(f"[ankisstant] prefilter skipped a card: {e}")
        return None
    return None


# ── rubric prompt (bundled inline so grading works on every provider) ───────

# THE SOURCE OF TRUTH for grading. The anki-card-scorer skill mirrors it for
# use outside the add-on — edit here first, then mirror. Since 2026-10-10 this is
# sent on EVERY provider: on the CLI the skill never loaded (generation runs with
# --tools ""), so a skill-only grader prompt was grading with no rubric at all.
RUBRIC_TEXT = """\
You grade Anki CLOZE cards for a Year-3 Australian medical student. The target style \
is the AnKing deck: short, direct, one fact per card. Score each card on five \
dimensions, 0/1/2.

D1 — Give-away check (HARD GATE, weight x3)
  0 = the answer can be READ OFF the card: it (or a shared word-stem) appears in the \
visible text; the cue is the answer's definition ("{{c1::Apoptosis}} is programmed cell \
death", "inflammation of the joint is called {{c1::arthritis}}"); a synonym or a second \
cloze names it; or grammar gives it away ("an {{c1::…}}").
  1 = partial leakage — a hint or phrase shortcuts the recall.
  2 = the cue pins exactly one answer without giving it away. A DIRECT, plain statement \
of the fact ("Kawasaki disease can cause coronary artery {{c1::aneurysms}}") is a 2. Do \
NOT mark a card down for being direct or for lacking a mechanism or vignette. \
  HINTS: in {{c1::answer::hint}} the hint is what the student sees in the blank. A hint \
that gives the answer's SHAPE ("increased/decreased", "which test?", "limb") is GOOD \
practice, not a leak; only a hint that contains the answer itself scores 0.

D2 — Atomicity (weight x2)
  0 = several independent facts on one note (an enumerated list), which desync under \
spaced repetition.
  1 = two coupled but separable facts.
  2 = one atomic idea, even if split across clozes that share the same core.
  CLOZE NUMBERS: {{c1}}+{{c1}} is ONE card with both blanks hidden (a pair, a range) — \
one idea. {{c1}}+{{c2}} is TWO sibling cards; fine when each sibling, with the other \
blank visible, is still a clean question. A list numbered c1, c2, c3… is a D2 0 (one \
card per item, the rest of the list visible).
  A card marked [ONE-BY-ONE] is a list whose items (all c1) are revealed one at a \
time: score it 2 for a coherent set of 3-8 short items, 1 for a loose or over-long set.

D3 — Answer precision / unambiguity (weight x2)
  0 = multiple defensible answers, or the expected shape is unclear.
  1 = one best answer but a synonym would also be defensible.
  2 = exactly one precise answer of an unambiguous shape.

D4 — Concision (weight x2)
  0 = over """ + str(MAX_FRONT_WORDS) + """ words, OR the front teaches a second fact: two clauses joined by \
"so", "yet", "which", "because", a semicolon or an em-dash where the second clause is a \
fact of its own; a mechanism chain (X → so Y → so Z); a vignette the fact doesn't need. \
Length under the limit does NOT rescue a two-fact front.
  1 = 18-""" + str(MAX_FRONT_WORDS) + """ words, or one clause that could be deleted without changing the answer.
  2 = about 17 words or fewer and nothing removable.
  On a [ONE-BY-ONE] card count only the text OUTSIDE the clozes.

D5 — Factual accuracy & clinical realism (HARD GATE, weight x3, binary)
  0 = inaccurate, hallucinated, implausible, or contradicts guidelines → fail regardless.
  2 = accurate and realistic. (If genuinely unsure, FLAG with reason "accuracy not \
verified" rather than guessing.)

VERDICT
  Weighted max = 24 (D1x3x2 + D2x2x2 + D3x2x2 + D4x2x2 + D5x3x2). Percent = weighted/24.
  - D5 = 0  → fail (accuracy non-negotiable).
  - D1 = 0  → fail, UNLESS the answer is unavoidably in the cue because the name IS \
the learning target (a mnemonic, a fixed eponym, an etymologically self-revealing term) \
→ flag.
  - D4 = 0  → fail (rewrite shorter).
  - >= 85% and no zero above → pass.
  - 60-84% → flag.
  - < 60% → fail.

Do NOT reject a card merely because a knowledgeable student could derive the answer — \
that is what knowing it means. The fault is only when the card's SURFACE hands it over.
Ignore the Extra field — it is not part of the score."""

_BATCH_OUTPUT = """\
Return ONLY a JSON array — no prose, no markdown fences — with exactly one object per \
input card, in the same order:
[{"index": 0, "verdict": "pass|flag|fail", "worst_dim": "D1", "reason": "one short line", \
"percent": 85}, ...]
Use "fail" for cards that need regeneration and "flag" for unavoidable give-aways. \
Keep each reason to one actionable line naming the failure mode."""

def atomicity_directive(max_clozes: int = 1) -> str:
    """A one-line D2 sensitivity override appended to every grader prompt so the
    LLM grader (and the bundled anki-card-scorer skill) match the user's
    configured atomicity tolerance and the deterministic prefilter. With
    max_clozes=2 a clean 2-cloze note is acceptable and only 3+ independent
    facts should drop D2 to 0."""
    try:
        n = int(max_clozes)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    return (
        f"\n\nATOMICITY SENSITIVITY (overrides D2 wording above): the user accepts "
        f"up to {n} independent cloze deletion(s) on one note. Do NOT score D2 = 0 "
        f"or flag for atomicity unless the note bundles MORE than {n} genuinely "
        f"independent facts. A note with {n} or fewer coupled clozes is acceptable."
    )


def rubric_batch_system(max_clozes: int = 1) -> str:
    """Full inline-rubric grader system prompt (every provider)."""
    return RUBRIC_TEXT + atomicity_directive(max_clozes) + "\n\n" + _BATCH_OUTPUT


def skill_batch_system(max_clozes: int = 1) -> str:
    """Grader system prompt when a scorer skill is attached (Anthropic API
    skill_id). Carries the FULL inline rubric anyway: the skill is a bonus, never
    the only copy of the rules. It used to send just "score using the
    anki-card-scorer rubric", and on the CLI — where the skill can't load with
    tools disabled — the grader was left scoring with no rubric at all."""
    return rubric_batch_system(max_clozes)


def merged_grade_instructions(max_clozes: int = 1) -> str:
    """Fold-in block appended to the GENERATION system prompt for same_call mode,
    so the model returns its own grades alongside the cards in one round-trip.
    The caller wraps the reply as a JSON object with a "grades" key (passed to
    engine.build_object_instructions via the want_grade plan flag)."""
    return (
        "\n\nAfter writing the cards, GRADE each one against this rubric and return a "
        '"grades" array (one object per card, same order) shaped '
        '{"index":0,"verdict":"pass|flag|fail","worst_dim":"D1","reason":"one line",'
        '"percent":85}. Be a strict grader — do not inflate your own cards.\n\n'
        + RUBRIC_TEXT + atomicity_directive(max_clozes)
    )


# ── batch grading request / response ────────────────────────────────────────

def batch_prompt(cards: list[dict]) -> str:
    """Build the user message for a one-shot batch grade of all cards."""
    blocks = []
    for i, c in enumerate(cards or []):
        front = (c.get("front") or "").strip() if isinstance(c, dict) else str(c)
        tag = " [ONE-BY-ONE]" if _is_one_by_one(c) else ""
        blocks.append(f"Card {i}{tag}:\n{front}")
    return (
        "Grade the following cloze cards. Reply with the JSON array described.\n\n"
        + "\n\n".join(blocks)
    )


_VERDICT_ALIASES = {
    "pass": PASS, "ok": PASS, "good": PASS, "accept": PASS,
    "flag": FLAG, "flag-inherent": FLAG, "flag_inherent": FLAG,
    "inherent": FLAG, "review": FLAG, "warn": FLAG,
    "fail": FAIL, "regenerate": FAIL, "regen": FAIL, "rewrite": FAIL, "reject": FAIL,
}


def _coerce_verdict_obj(obj) -> Verdict:
    """Turn one grader dict into a Verdict, tolerating shape/key drift. Anything
    unrecognised degrades to PASS (fail-open)."""
    if not isinstance(obj, dict):
        return _passed()
    raw = str(obj.get("verdict") or obj.get("status") or "pass").strip().lower()
    verdict = _VERDICT_ALIASES.get(raw, PASS)
    worst = str(obj.get("worst_dim") or obj.get("worst") or obj.get("dim") or "").strip()
    reason = str(obj.get("reason") or obj.get("note") or obj.get("fix") or "").strip()
    pct = obj.get("percent")
    try:
        pct = int(round(float(pct))) if pct is not None else None
    except (TypeError, ValueError):
        pct = None
    scores = obj.get("scores") if isinstance(obj.get("scores"), dict) else None
    return Verdict(verdict, worst, reason, pct, scores, source="rubric")


def parse_batch_verdicts(raw, cards: list[dict]) -> list[Verdict]:
    """Parse the grader's JSON into exactly len(cards) Verdicts, in card order.
    Tolerant: accepts a bare list, or a dict wrapping the list under common keys.
    Missing/extra/garbage entries default to PASS. Never raises."""
    n = len(cards or [])
    out = [_passed() for _ in range(n)]
    if n == 0:
        return out
    try:
        items = raw
        if isinstance(raw, dict):
            for key in ("grades", "verdicts", "results", "cards", "scores"):
                if isinstance(raw.get(key), list):
                    items = raw[key]
                    break
            else:
                items = [raw]  # a single object for a single card
        if not isinstance(items, list):
            return out
        # If every item carries an explicit index, honour it; else go positional.
        explicit = all(isinstance(it, dict) and isinstance(it.get("index"), int)
                       for it in items) and bool(items)
        if explicit:
            for it in items:
                idx = it.get("index")
                if isinstance(idx, int) and 0 <= idx < n:
                    out[idx] = _coerce_verdict_obj(it)
        else:
            for i, it in enumerate(items):
                if i < n:
                    out[i] = _coerce_verdict_obj(it)
    except Exception as e:  # pragma: no cover - defensive
        print(f"[ankisstant] couldn't parse grader output: {e}")
    return out


def parse_same_call_grades(grades, cards: list[dict]) -> list[Verdict]:
    """Parse the "grades" array folded into a same_call generation reply. Same
    tolerant handling as the batch path."""
    return parse_batch_verdicts(grades, cards)


# ── merge LLM verdicts with deterministic pre-filters ───────────────────────

def apply_prefilters_then(verdicts: list[Verdict] | None, cards: list[dict],
                          max_clozes: int = 1) -> list[Verdict]:
    """Combine deterministic pre-filters with the LLM verdicts:
      - a prefilter FAIL always wins (deterministic give-away beats a lenient
        self-grade);
      - a prefilter FLAG applies only when the LLM said PASS (never downgrade an
        LLM FAIL to a FLAG);
      - otherwise keep the LLM verdict.
    `verdicts` may be None/short (e.g. grading failed) — missing slots are PASS.
    Never raises."""
    n = len(cards or [])
    base = list(verdicts or [])
    while len(base) < n:
        base.append(_passed())
    out: list[Verdict] = []
    for i in range(n):
        llm = base[i] if isinstance(base[i], Verdict) else _passed()
        pre = prefilter_card(cards[i], max_clozes) if isinstance(cards[i], dict) else None
        if pre is None:
            out.append(llm)
        elif pre.verdict == FAIL:
            out.append(pre)
        elif pre.verdict == FLAG and llm.verdict == PASS:
            out.append(pre)
        else:
            out.append(llm)
    return out
