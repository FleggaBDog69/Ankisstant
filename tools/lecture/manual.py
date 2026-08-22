"""File hand-off fallback for AI Lecture — the no-provider path.

The whole pipeline needs the AI at exactly two points: extract learning points
from the lecture, and judge which candidate cards actually test each point. A
local deck search sits between them. So a user with no CLI and no API key can run
the tool in two file round-trips through *any* browser AI (ChatGPT, Claude,
Gemini):

    Round 1  export_extract_request(lecture)  -> a .md the user pastes into the AI
             the AI saves its answer as a file
             parse_extract_reply(file_text)    -> list[LearningPoint]
      (the tool searches the user's deck locally — no AI)
    Round 2  export_relevance_request(items)   -> a .md the user pastes into the AI
             the AI saves its answer as a file
             parse_relevance_reply(file_text)  -> list[Verdict]

The prompts and parsers here are the SAME ones the automated provider uses
(extractor.build_extract_system / batch.as_prompt / parse_batch_reply and
matcher.RELEVANCE_SYSTEM / build_scoring_prompt / parse_scoring_reply), so a
manual run produces byte-identical points and verdicts to a CLI run — only the
transport differs.

NO VISION. A text file can't carry the slide images, so this path is text-only:
slides whose meaning lives in layout (a drug-to-class matching table) are lost.
That's the honest cost of the free path, and the export says so.
"""
from __future__ import annotations

import json
import re

from ...core import log
from . import extractor, matcher

# One whole-document batch, no size gate: the automated path splits big files to
# dodge the CLI's timeout, but a human-mediated web AI has no such limit (and a
# split would cost global dedupe), so manual mode always sends the lecture whole.
def _whole_batch(lecture) -> extractor.Batch:
    return extractor.Batch(index=0, segments=list(lecture.segments), whole_document=True)


# ── JSON extraction ───────────────────────────────────────────────────────────
# The reply file might be pristine JSON (the AI saved a real .json), a fenced
# ```json block (it pasted into a .md/.txt), or prose wrapped around either. Be
# permissive: strip fences, then fall back to the first balanced [...] / {...}.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str):
    """Best-effort parse of an AI reply file into Python data, or None."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    # 1. straight JSON
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 2. the largest fenced code block
    blocks = _FENCE_RE.findall(raw)
    for b in sorted(blocks, key=len, reverse=True):
        try:
            return json.loads(b.strip())
        except Exception:
            continue
    # 3. first balanced array or object by bracket matching
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = raw.find(open_ch)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(raw)):
            c = raw[i]
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except Exception:
                        break
    return None


# ── shared wrapper ────────────────────────────────────────────────────────────
def _wrap(step: str, human: str, system: str, body: str,
          out_name: str, schema_hint: str) -> str:
    """Assemble one hand-off file: instructions for the person at the top, then
    the exact system + content the automated call would send, then a closing
    instruction to the AI to save its answer as a file."""
    return (
        f"# AI Lecture — {step}\n\n"
        f"{human}\n\n"
        "**What to do:**\n"
        "1. Copy everything below the line into ChatGPT, Claude, or Gemini.\n"
        f"2. When it answers, save the reply as a file called `{out_name}`\n"
        "   (most web AIs can create a downloadable file — just ask; or copy the\n"
        "   JSON code block it returns into a new text file yourself).\n"
        "3. Back in Anki, load that file to continue.\n\n"
        "---------------------------------------------------------------\n\n"
        f"{system}\n\n"
        "════════════════════════════ CONTENT ════════════════════════════\n\n"
        f"{body}\n\n"
        "════════════════════════════ OUTPUT ═════════════════════════════\n\n"
        f"Return ONLY {schema_hint} No commentary before or after it.\n"
        f"If you can create a file, save it as `{out_name}`. Otherwise put the "
        "JSON in a single ```json code block so it can be copied cleanly."
    )


# ── Round 1: extraction ───────────────────────────────────────────────────────
def export_extract_request(lecture, *, target_points: int = extractor.DEFAULT_TARGET_POINTS,
                           high_yield: bool = True) -> str:
    """The .md a no-provider user feeds to their browser AI to get learning
    points. Text-only (no vision)."""
    batch = _whole_batch(lecture)
    system = extractor.build_extract_system(
        target_points, whole_document=True, high_yield=high_yield)
    human = (
        "This is step 1 of 2. Your AI reads the lecture text and lists the "
        "learning points worth finding cards for. (No slide images are sent — "
        "if a slide's meaning is only in a diagram or table, it may be missed.)"
    )
    return _wrap(
        "Step 1 of 2 · extract learning points", human, system, batch.as_prompt(),
        out_name="points.json",
        schema_hint="the JSON array of learning-point objects described above.",
    )


def parse_extract_reply(text: str, lecture) -> list:
    """Turn the AI's step-1 file back into LearningPoints, via the same parser
    and dedupe the automated path uses. Returns [] if nothing parseable."""
    data = extract_json(text)
    if data is None:
        log.warn("lecture/manual: step-1 reply had no readable JSON")
        return []
    points = extractor.parse_batch_reply(data, _whole_batch(lecture))
    # merge() runs the same first-occurrence dedupe the automated single-batch
    # path applies, so the two transports converge on the same list.
    return extractor.merge([points])


# ── Round 2: relevance ────────────────────────────────────────────────────────
def export_relevance_request(items: list) -> str:
    """The .md for step 2. `items` is [(point, [(nid, card_text), ...])], built
    by the panel on the main thread (note text needs the collection) — exactly
    what the automated relevance call scores, but as ONE prompt (no batching:
    the web AI has no timeout to dodge)."""
    scorable = [it for it in items if it[1]]
    body = matcher.build_scoring_prompt(scorable)
    human = (
        "This is step 2 of 2. Your deck has been searched; below are the "
        "candidate cards for each learning point. Your AI decides which cards "
        "actually TEST each point, so the weak matches get filtered out."
    )
    return _wrap(
        "Step 2 of 2 · judge card relevance", human, matcher.RELEVANCE_SYSTEM, body,
        out_name="relevance.json",
        schema_hint="the JSON verdicts object described above, keyed by point number.",
    ), scorable


def parse_relevance_reply(text: str, scorable: list) -> list:
    """Apply the AI's step-2 file. Returns one Verdict per scorable point (same
    order as `scorable`), via the automated parser. Fails OPEN: unreadable JSON
    keeps every candidate rather than inventing a gap."""
    data = extract_json(text)
    if data is None:
        log.warn("lecture/manual: step-2 reply had no readable JSON — keeping all")
        return [matcher.Verdict(kept=[nid for nid, _ in cards]) for _, cards in scorable]
    return matcher.parse_scoring_reply(data, scorable)
