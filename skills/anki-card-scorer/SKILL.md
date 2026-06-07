---
name: anki-card-scorer
description: Use this skill whenever the user wants to score, rate, evaluate, critique, or assess the quality of Anki cloze deletion cards against a structured rubric. Triggers include score this card, score these cards, rate this card, is this a good card, evaluate this card, check my cards, review these cards, run the rubric on these, or any request to assess cloze card quality. Designed for Year 3 Australian medical student cloze decks. Do NOT use this skill to generate cards — use anki-cards for that. Do NOT use for Q and A cards or non-medical content.
---

# Anki Cloze Card Quality Scorer

## Overview

Scores Anki cloze deletion cards against a four-dimension rubric focused on whether a card forces retrieval (vs rewards keyword-matching). Output is per-card scores plus the single most actionable insight: the worst-scoring dimension and a one-line fix.

This skill is for **assessment only** — never use it to generate new cards. If the user wants better cards, point them at the anki-cards skill.

## The dimensions

Each card is scored on four dimensions. D1 and D5 are hard gates (a zero on either fails the card regardless of total). D2 and D3 are weighted contributors.

### D1 — Retrieval Force / Cue Type (hard gate, ×3)

Does answering require genuine retrieval, or does the visible text give the answer away?

- **0** — Answer is derivable from the prompt alone. Includes:
  - The answer's label is restated in the visible text ("the first-line treatment for anaphylaxis is {{c1::adrenaline}}")
  - Definitional clozes ("{{c1::Apoptosis}} is programmed cell death")
  - Source-echo (the card is the textbook sentence with one word removed)
  - Syntactic give-aways (indefinite article "an" before vowel-initial answer)
  - The cloze answer appears as a literal substring elsewhere in the visible text
- **1** — Partial cue leakage. Some processing required but a hint shortcuts it. E.g., the cue names a closely related concept that narrows the answer to one of two or three.
- **2** — Cue forces processing. A genuine mechanism, presentation, contrast, cause, or consequence drives the retrieval path.

**First-pass check before judgment:** does the cloze answer (or any morphological variant) appear as a literal substring of the visible text? If yes → D1 = 0, no further consideration needed.

### D2 — Atomicity (×2)

One testable idea per card.

- **0** — Multi-fact enumeration where each fact is independent ("the 5 features of X are…"), or two clearly separable facts crammed into one card.
- **1** — Two coupled facts where one logically implies the other but they're still distinguishable.
- **2** — A single atomic unit, even if split across multiple clozes that share the same conceptual core. Example: `prevents {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}` is one fact (an anatomical relationship) split into three clozes — score 2, not 0.

**Note:** the hint syntax (`{{c1::answer::hint}}`) on enumerated lists can rescue atomicity by treating the list as a single retrieval target rather than independent facts. A four-item list with informative hints is acceptable; the same list without hints scores lower.

### D3 — Answer Precision / Unambiguity (×2)

Does the prompt specify exactly one correct answer, with a clear shape?

- **0** — Multiple defensible answers, or the expected shape of the answer is unclear ("Atherosclerosis affects {{c1::large vessels}}" — could also be "elastic arteries", "the aorta", etc.).
- **1** — One best answer but a knowledgeable reader could plausibly write a synonym or near-equivalent and not be wrong.
- **2** — Exactly one precise answer of an unambiguous shape.

### D5 — Factual Accuracy & Clinical Realism (hard gate, ×3)

Is the content correct and clinically plausible?

- **0** — Inaccurate, hallucinated, implausible, or contradicts current Australian/international guidelines → fail regardless of other scores.
- **2** — Accurate and realistic.

**This is a binary gate — no half-credit.** If unsure, flag for the user rather than guessing. The scorer should not invent expertise it doesn't have.

## Scoring procedure

For each card:

1. **Substring check** — does the cloze answer appear as a literal substring of the visible text? If yes, D1 = 0, proceed.
2. **D1 judgment** — if substring check passes, identify the cue type. Label cue → 0 or 1; processing cue → 2.
3. **D2 judgment** — count distinguishable testable facts.
4. **D3 judgment** — ask whether a knowledgeable peer would write the same answer.
5. **D5 judgment** — accuracy check; flag if uncertain.
6. **If D1 = 0, apply the inherent label-association test** (see Verdict section): could the card be rewritten with a processing cue while preserving the same retrieval target? If NO → verdict becomes FLAG-inherent, not FAIL.
7. **Identify the worst dimension** — the lowest-scoring one. If multiple tie at the lowest, pick D1 first, then D5, then D3, then D2 (in order of weighted impact).
8. **Write the one-line reason** — name the specific failure mode, not the score. For FLAG-inherent, also write a one-line *triage direction* (keep, drop, or reframe).

## Verdict

Compute the weighted total as a percentage of the maximum (max = 20 for cloze cards: D1×3×2 + D2×2×2 + D3×2×2 + D5×3×2 = 6 + 4 + 4 + 6 = 20).

- **D5 = 0 → FAIL**, regardless of total. Accuracy is non-negotiable.
- **D1 = 0 → FAIL**, *unless* the inherent label-association test applies (see below), in which case → **FLAG-inherent**.
- **≥ 85% and no hard-gate zero → PASS** — card forces retrieval; ship it.
- **60–84% → FLAG** — usable but improvable; surface the worst dimension.
- **< 60% and no hard-gate zero → FAIL** — recommend rewrite.

### The inherent label-association test

Before applying the D1 hard gate, ask:

> **Could this card be rewritten with a processing cue (mechanism, presentation, contrast, cause, consequence) while preserving the same retrieval target and pedagogical value?**

- **YES** → the failure is fixable. Verdict = **FAIL**. The card should be rewritten.
- **NO** → the content is inherently label-association. Verdict = **FLAG-inherent**. The card is the best version of itself; the user decides whether to keep it.

**Indicators of inherent label-association** (apply conservatively — when in doubt, FAIL):

- The card tests a name ↔ feature mapping itself (eponym → associated lesion, syndrome → defining feature), and reversing the cue would test a different fact rather than the same one differently.
- The cloze answer is etymologically derivable from a word in the cue, and that word is essential to what the card teaches (e.g., "vasospastic angina" → "spasm" — both decks fail this; no rewrite preserves the term).
- The card teaches a mnemonic, classification name, or fixed eponym where the retrieval target is the name itself.

**Indicators that the failure is fixable** (these are FAIL, not FLAG-inherent):

- A clinical scenario, mechanism, or presentation could replace the definitional cue while testing the same fact.
- The cloze answer appears literally in the cue because of lazy phrasing, not etymological necessity ("the first-line treatment for X is Y" — easily fixed with a mechanism cue).
- The card is a textbook sentence with one keyword removed.

The distinction matters because **FAIL means rewrite, FLAG-inherent means decide whether to keep**. They're different actions for the user.

## Output format

### Single card mode

For one card or a small handful, output a per-card block:

```
Card 1: "Atherosclerosis is a form of {{c1::arteriosclerosis}}..."

D1: 0 | D2: 2 | D3: 2 | D5: 2
Weighted: 14/20 (70%)
Verdict: FAIL (D1 hard-gate zero)
Worst: D1 — label cue ("Atherosclerosis is a form of" telegraphs the answer)
Fix: cue with the distinguishing feature (cholesterol plaques, intimal location) and cloze the term.
```

### Batch mode

For more than ~5 cards, use a compact table followed by detail only on failures and flags:

```
| #  | D1 | D2 | D3 | D5 | %   | Verdict        | Worst | Note                              |
|----|----|----|----|----|-----|----------------|-------|-----------------------------------|
| 1  | 0  | 2  | 2  | 2  | 70  | FAIL           | D1    | label cue restates the answer     |
| 2  | 2  | 2  | 2  | 2  | 100 | PASS           | —     | —                                 |
| 3  | 1  | 2  | 1  | 2  | 80  | FLAG           | D1    | partial leakage via "first-line"  |
| 4  | 0  | 2  | 2  | 2  | 70  | FLAG-inherent  | D1    | mnemonic — no processing cue      |
...
```

Then a short detail section for FAIL, FLAG, and FLAG-inherent cards, with the worst-dimension reason and a one-line direction (fix for FAIL/FLAG; triage for FLAG-inherent).

### Summary stats (batch only)

Conclude batch scoring with:
- Pass rate (count + %)
- Flag rate (count + %), split into FLAG (fixable) and FLAG-inherent (triage)
- Fail rate (count + %)
- Most common worst dimension
- One sentence on the dominant failure mode

## Handling edge cases

### Inherent label-association

See the verdict section above for the test and indicators. Three notes on applying it:

1. **Apply the test consciously, not by default.** Most cards that fail D1 do so because of lazy phrasing, not etymological necessity. The inherent label-association rule exists for the genuine residual — mnemonics, eponym mappings, etymologically self-revealing terms — not as an escape hatch.

2. **The FLAG-inherent verdict is not a pass.** It tells the user "this card is the best version of itself; the rubric still measures it as low-retrieval-force; decide whether the content needs to be carded at all." The action is *triage*, not import-as-is.

3. **When unsure, default to FAIL.** It is less harmful to flag a fixable card as needing rewrite than to wave through a genuinely lazy card under the inherent-label rule. Conservatism on this judgement preserves rubric power.

### Cards that fail D5 but the scorer isn't sure

If accuracy is uncertain, do not guess. Score D5 as `?` rather than 0 or 2, mark the card as FLAG with reason "accuracy not verified", and ask the user to confirm. Better to surface uncertainty than fabricate confidence.

### Cards where the same answer appears in multiple cloze deletions

If a card has `{{c1::X}}` and later `{{c1::X}}` again (same cloze group), that's intentional — one fact tested with multiple deletions. Score as one fact for D2.

If a card has `{{c1::X}}` and `{{c2::Y}}` where Y is a synonym or restatement of X, the second cloze gives away the first. Score D1 down accordingly.

### Cards with empty or filler Extra fields

Extra field quality is not part of the rubric and should not affect the score. Note it in passing if egregious ("Extra adds no value") but don't penalise D1–D5 for it.

## Worked examples

### Example 1 — clean PASS

```
Card: "{{c1::Adrenaline}} reverses the bronchospasm, vasodilation, and capillary leak of anaphylaxis."

D1: 2 — mechanism cue (the cue is the physiology, not the label "first-line treatment")
D2: 2 — single fact (the drug)
D3: 2 — one precise answer
D5: 2 — accurate

Weighted: 20/20 (100%)
Verdict: PASS
```

### Example 2 — D1 hard-gate FAIL

```
Card: "The first-line treatment for anaphylaxis is {{c1::adrenaline}}."

D1: 0 — label cue ("first-line treatment for anaphylaxis" restates the answer's category)
D2: 2 — single fact
D3: 2 — one precise answer
D5: 2 — accurate

Weighted: 14/20 (70%)
Verdict: FAIL (D1 hard-gate zero)
Worst: D1
Reason: the cue is the answer's label restated.
Fix: cue with the mechanism (bronchospasm, vasodilation, capillary leak) or the presentation (stridor, hypotension, urticaria post-sting).
```

### Example 3 — D2 atomicity FLAG

```
Card: "The 5 features of nephrotic syndrome are {{c1::proteinuria}}, {{c2::hypoalbuminaemia}}, {{c3::oedema}}, {{c4::hyperlipidaemia}}, and {{c5::lipiduria}}."

D1: 1 — partial leakage; "5 features of nephrotic syndrome" hints at the category but individual features still require retrieval
D2: 0 — five independent facts on one card; will desync under spaced repetition
D3: 2 — answers are precise
D5: 2 — accurate

Weighted: 12/20 (60%)
Verdict: FLAG (D2 = 0 is severe but not a hard gate)
Worst: D2
Reason: enumeration of five independent facts on one card; the easiest will keep the card off the review queue while the hardest never matures.
Fix: split into five separate cards, or use hint syntax to make this one retrieval ({{c1::proteinuria::>3.5g/day}}, etc.).
```

### Example 4 — inherent label-association (FLAG-inherent, not FAIL)

```
Card: "The mnemonic for vessels affected by atherosclerosis is {{c1::A CoPy Cat named Willis}}."

D1: 0 — definitional; the cue *is* asking for the mnemonic
D2: 2 — single fact
D3: 2 — one precise answer
D5: 2 — accurate

Weighted: 14/20 (70%)
Verdict: FLAG-inherent
Worst: D1
Reason: inherent label-association — a mnemonic has no processing cue. The card is the best version of itself.
Action: triage decision — keep if the mnemonic is high-yield, otherwise drop or fold into the vessels card's Extra field. Do not rewrite.
```

### Example 5 — etymologically self-revealing term (FLAG-inherent)

```
Card: "{{c1::Vasospastic}} angina is caused by transient coronary {{c2::artery spasm}}."

D1: 0 — "spasm" is a substring of "Vasospastic"; the two cloze answers mutually telegraph
D2: 1 — two coupled but separable facts (the name and the mechanism)
D3: 2 — precise
D5: 2 — accurate

Weighted: 12/20 (60%)
Verdict: FLAG-inherent
Worst: D1
Reason: inherent label-association — the term and the mechanism are etymologically locked. Cuing from clinical scenario (e.g., "rest pain with transient ST elevation in a young patient with clean coronaries") would fix this card; but if the explicit term ↔ mechanism mapping is the learning target, no rewrite preserves it.
Action: triage — either reframe as a scenario-cued card (different retrieval target, also valid), or accept the flag and import.
```

### Example 6 — looks inherent but is actually fixable (FAIL)

```
Card: "Angina is chest pain due to myocardial {{c1::ischaemia}}."

D1: 0 — definitional; the visible text is the textbook definition with one word removed
D2: 2 — single fact
D3: 2 — precise
D5: 2 — accurate

Weighted: 14/20 (70%)
Verdict: FAIL
Worst: D1
Reason: definitional cloze. Applying the inherent-label test: could this be rewritten with a processing cue? YES — cue from clinical presentation ("exertional substernal chest pain that resolves with rest, no troponin rise, ST depression on stress testing — diagnosis is {{c1::stable angina}}") or contrast ("the form of myocardial oxygen mismatch that is reversible and does not raise troponin is {{c1::angina}}"). The card is lazy, not inherent.
Fix: replace the dictionary definition with a clinical scenario or contrast cue.
```

The contrast with Example 5 is the point: both score D1 = 0, both look definitional at first glance, but Example 5's term-mechanism mapping is etymologically locked while Example 6's term-definition mapping can be reframed without loss. Apply the test honestly.

## What not to do

- **Do not propose rewrites unless asked.** The scoring skill identifies failure modes; rewrites belong to the anki-cards skill. A one-line *fix direction* is fine; a full rewritten card is not.
- **Do not score Q&A cards.** This skill is cloze-only. If a Q&A card is provided, decline and point at the rubric for Q&A scoring.
- **Do not inflate scores to be agreeable.** A card that fails should fail. The rubric is worthless if it can be flattered.
- **Do not score the Extra field as part of D1–D5.** Extra is separate.
- **Do not invent accuracy expertise.** When unsure about D5, mark `?` and ask.

## Common triggers

- "Score this card"
- "Score these cards"
- "Rate this card / these"
- "Is this a good card?"
- "Run the rubric on these"
- "Check my cards"
- "Evaluate these"
- "How does this card score?"
