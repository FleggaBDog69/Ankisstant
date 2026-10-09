---
name: anki-card-scorer
description: Use this skill whenever the user wants to score, rate, evaluate, critique, or assess the quality of Anki cloze deletion cards against a structured rubric. Triggers include score this card, score these cards, rate this card, is this a good card, evaluate this card, check my cards, review these cards, run the rubric on these, or any request to assess cloze card quality. Designed for Year 3 Australian medical student cloze decks. Do NOT use this skill to generate cards — use anki-cards for that. Do NOT use for Q and A cards or non-medical content.
---

# Anki Cloze Card Quality Scorer

> Mirrors the Ankisstant add-on's grader rubric (`tools/quality_pass.py` → `RUBRIC_TEXT`), which is the source of truth. Edit there first, then here.

The target style is the AnKing deck: short, direct, one fact per card. This skill is for **assessment only** — for new cards use anki-cards.

Score each card on five dimensions (0/1/2). **D1** and **D5** are hard gates; **D4 = 0**
also fails the card. Assess only — don't rewrite unless asked (a one-line *fix
direction* is fine). Ignore the Extra field.

### D1 — Give-away check (HARD GATE, ×3)

- **0** — the answer can be read off the card: it (or a shared stem) is in the visible
  text, the cue is its definition, a synonym or second cloze names it, or grammar gives
  it away.
- **1** — partial leakage: a hint or phrase shortcuts the recall.
- **2** — the cue pins exactly one answer without giving it away. A **direct**, plain
  statement of the fact scores 2 — never mark a card down for lacking a mechanism or
  vignette.

### D2 — Atomicity (×2)

- **0** — several independent facts on one note (an enumerated list).
- **1** — two coupled but separable facts.
- **2** — one atomic idea, even if split across clozes sharing one core.
- **Cloze numbers:** `c1`+`c1` is one card (a pair, a range) — one idea. `c1`+`c2` is two
  sibling cards — fine when each sibling is still a clean question with the other blank
  visible. A list numbered `c1, c2, c3…` is D2 = 0.
- A **one-by-one** card (all `c1`, One by one = y) is a list revealed item by item: 2 for a
  coherent set of 3–8 short items, 1 for a loose or over-long set. For D4, count only
  the text outside the clozes.

### D3 — Answer precision / unambiguity (×2)

- **0** — multiple defensible answers, or the expected shape is unclear.
- **1** — one best answer, but a defensible synonym exists.
- **2** — exactly one precise answer of an unambiguous shape.

### D4 — Concision (×2)

- **0** — over 25 words, or the front teaches a second fact (em-dash tail, "because…"
  clause, a mechanism chain or vignette the fact doesn't need).
- **1** — 18–25 words, or one clause that could go without changing the answer.
- **2** — about 17 words or fewer, nothing removable.

### D5 — Factual accuracy & clinical realism (HARD GATE, ×3, binary)

- **0** — inaccurate, hallucinated, implausible, or contradicts current guidelines.
- **2** — accurate and realistic. If genuinely unsure, FLAG ("accuracy not verified")
  rather than guess.

### Verdict

Weighted max = 24 (D1×3×2 + D2×2×2 + D3×2×2 + D4×2×2 + D5×3×2). Percent = weighted / 24.

- **D5 = 0 → FAIL.** **D4 = 0 → FAIL** (rewrite shorter).
- **D1 = 0 → FAIL**, unless the answer is unavoidably in the cue because the name *is*
  the learning target (a mnemonic, a fixed eponym, an etymologically self-revealing
  term) → **FLAG**.
- **≥ 85% and no zero above → PASS.** **60–84% → FLAG.** **< 60% → FAIL.**

Don't reject a card because a knowledgeable student could derive the answer — that is
what knowing it means. The fault is only when the card's surface hands it over.

### Output

In chat, give a compact table (`# | D1 | D2 | D3 | D4 | D5 | % | Verdict | Worst | Note`) then one line of fix direction per FAIL/FLAG. When a caller asks for JSON (the Ankisstant add-on does), return ONLY a JSON array — no prose, no markdown fences — one object per input card, in
the same order, e.g.:

[{"index": 0, "verdict": "fail", "worst_dim": "D4", "reason": "front is 31 words — cut the em-dash tail", "percent": 70}]

- `verdict` is lowercase `pass` | `flag` | `fail`.
- `worst_dim` is `"D1"`–`"D5"` (or `""` when PASS). `percent` is 0–100 (weighted ÷ 24 × 100).
- One actionable line per `reason`, naming the failure mode. Don't inflate scores to be
  agreeable — a card that fails should fail.


## What not to do

- Don't mark a card down for being direct, or for lacking a mechanism or vignette.
- Don't propose full rewrites unless asked — a one-line fix direction is fine.
- Don't score Q&A cards or non-medical content.
- Don't invent accuracy expertise — when unsure about D5, FLAG and ask.
