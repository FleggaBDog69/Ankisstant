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
    def strip_html(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s or "")
        return re.sub(r"\s+", " ", s).strip()


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

def prefilter_card(card: dict, max_clozes: int = 1) -> Verdict | None:
    """Cheap deterministic checks. Returns a Verdict when a rule trips, or None
    ('no opinion — let the LLM decide'). Never raises.

    - answer-in-prompt  → FAIL / D1: a cloze answer is echoed verbatim in the
      visible surrounding text, so the prompt gives the answer away.
    - cloze count        → FLAG / D2: MORE than `max_clozes` DISTINCT cloze
      groups on one note (repeated same-group clozes are one fact, not flagged).
      `max_clozes` is the user-configured atomicity sensitivity
      (quality_pass.atomicity_max_clozes) — e.g. 2 means a clean 2-cloze note
      passes and only 3+ independent clozes flag.

    answer-in-prompt (FAIL) takes precedence over cloze-count (FLAG).
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
                    "the cloze answer is echoed in the visible prompt — replace the "
                    "restated fact with a clinical cue (presentation or mechanism) so "
                    "the answer must be recalled, not read off the card.",
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

# Condensed from ~/.claude/skills/anki-card-scorer/SKILL.md. Kept verbatim on the
# load-bearing parts (hard gates, the inherent-label-association test) so the
# inline grade matches what the skill would produce on Anthropic.
RUBRIC_TEXT = """\
You grade Anki CLOZE cards for a Year-3 Australian medical student. Score each card on \
four dimensions, 0/1/2. The goal is RETRIEVAL FORCE: a good card makes the learner \
recall the answer; a bad one lets them read it off the prompt or recognise a label.

D1 — Retrieval force / cue type (HARD GATE, weight x3)
  0 = the answer is derivable from the visible prompt: its label/category is restated \
("the first-line treatment for anaphylaxis is {{c1::adrenaline}}"), a bare definition \
("{{c1::Apoptosis}} is programmed cell death"), a textbook sentence with one word removed, \
or the answer appears as a literal substring of the visible text.
  1 = partial cue leakage; some processing but a hint shortcuts it.
  2 = the cue forces processing — a genuine mechanism, presentation, contrast, cause, or \
consequence drives the retrieval.

D2 — Atomicity (weight x2)
  0 = several independent facts on one note (an enumerated list), which desync under \
spaced repetition.
  1 = two coupled but separable facts.
  2 = one atomic idea, even if split across clozes that share the same core. Informative \
hints on a list can rescue atomicity (treat the list as one retrieval).

D3 — Answer precision / unambiguity (weight x2)
  0 = multiple defensible answers, or the expected shape is unclear.
  1 = one best answer but a synonym would also be defensible.
  2 = exactly one precise answer of an unambiguous shape.

D5 — Factual accuracy & clinical realism (HARD GATE, weight x3, binary)
  0 = inaccurate, hallucinated, implausible, or contradicts guidelines → fail regardless.
  2 = accurate and realistic. (If genuinely unsure, FLAG with reason "accuracy not \
verified" rather than guessing.)

VERDICT
  Weighted max = 20 (D1x3x2 + D2x2x2 + D3x2x2 + D5x3x2). Percent = weighted/20.
  - D5 = 0  → fail (accuracy non-negotiable).
  - D1 = 0  → fail, UNLESS the inherent-label-association test applies → flag.
  - >= 85% and no hard-gate zero → pass.
  - 60-84% → flag.
  - < 60% and no hard-gate zero → fail.

INHERENT-LABEL-ASSOCIATION TEST (apply only when D1 = 0): could the card be rewritten \
with a processing cue while preserving the SAME retrieval target? If YES the failure is \
fixable → fail (rewrite). If NO (a mnemonic, a fixed eponym, an etymologically self-\
revealing term where the name IS the learning target) → flag, do not over-reject. \
When in doubt, fail. Do NOT reject a card merely because a knowledgeable solver could \
derive the answer — every good card's answer is derivable to someone who knows the topic; \
the fault is only when the cue's SURFACE FORM hands the answer over with no processing.

Ignore the Extra field — it is not part of the score."""

_BATCH_OUTPUT = """\
Return ONLY a JSON array — no prose, no markdown fences — with exactly one object per \
input card, in the same order:
[{"index": 0, "verdict": "pass|flag|fail", "worst_dim": "D1", "reason": "one short line", \
"percent": 85}, ...]
Use "fail" for cards that need regeneration and "flag" for inherent-label cards. Keep \
each reason to one actionable line naming the failure mode."""

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
    """Grader system prompt when the anki-card-scorer skill is invoked
    (Anthropic only): the skill carries the rubric server-side / on-demand, so we
    only pin the output shape and the atomicity override and save the inline
    rubric tokens."""
    return (
        "Score these Anki cloze cards using the anki-card-scorer rubric."
        + atomicity_directive(max_clozes) + "\n\n" + _BATCH_OUTPUT
    )


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
        blocks.append(f"Card {i}:\n{front}")
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
