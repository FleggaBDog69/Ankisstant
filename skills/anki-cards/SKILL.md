---
name: anki-cards
description: Use this skill whenever the user wants to create Anki cloze deletion cards from medical or clinical content. Triggers include requests like make a card, give me a card, anki card, cloze, that's yield card, or any request to convert clinical content into flashcard format. Designed for Year 3 Australian medical students using a cloze-deletion based Anki workflow. Do NOT use for Q and A format cards or non-medical content.
---

# Anki Cloze Card Creation — Australian Medical Student

## Overview

Creates high-yield Anki cloze deletion cards from clinical content. Cards follow a strict format optimised for medical school exam preparation, aligned with Australian clinical guidelines where relevant. The core design priority is **retrieval force** — every card must require pulling the answer from memory, not reading it off the prompt.

## Core Principle: Cue Design

Every cloze card has two parts: the **cloze** (what gets retrieved) and the **cue** (what's left visible). Atomicity rules govern the cloze. Cue design governs whether the card actually forces retrieval. Both matter; only the first is intuitive.

There are two kinds of cue. Only one is useful.

**Label cues** name the answer's category and ask you to fill it in. The retrieval path is keyword → answer. It feels productive because you keep getting the card "right", but the path doesn't exist in the real world — nobody hands you the label.

> BAD: `The first-line treatment for anaphylaxis is {{c1::adrenaline}}.`
> The cue ("first-line treatment for anaphylaxis") is the answer's own label restated.

**Processing cues** give you something you have to work through — a mechanism, a presentation, a contrast, a cause, a consequence — to reach the answer. The retrieval path matches what you actually do on the ward.

> GOOD: `{{c1::Adrenaline}} reverses the bronchospasm, vasodilation, and capillary leak of anaphylaxis.`
> Same fact, same atomicity. The cue is the mechanism, not the label.

### The test

Before writing a card, ask: **if I deleted the answer, could the reader reconstruct it from the type of thinking the cue demands — or only from recognising a label?** If the latter, redesign the cue.

### Cue toolkit

Reach for one of these when constructing the visible part of the card:

- **Mechanism** — "the drug that reverses the bronchospasm of…"
- **Presentation** — "a patient with stridor and hypotension after a sting…"
- **Contrast** — "what distinguishes X from Y…"
- **Cause** — "why does…"
- **Consequence** — "what happens if…"

Function over anatomy is a special case of this: anatomy cards built on origin/insertion are label cues; anatomy cards built on what the structure does are processing cues.

### Navigating the precision–leakage tension

Cards need enough context to specify exactly one correct answer (otherwise the prompt is ambiguous) without restating the answer's label (otherwise it's a give-away). The resolution: context should specify the **shape** of the answer (a drug? a vessel? a phase?), not signpost which specific one. "The immediate drug…" tells you the answer is a drug without telling you which drug.

## Core Principle: Atomicity

Cue design governs what surrounds the cloze. Atomicity governs what's inside it, and how many clozes belong on one card. A card should test one retrievable unit — multi-fact cards generate sibling clozes that desync under spaced repetition: once the easiest sub-fact is learned, you press "Good", and the harder sub-facts ride along untrained.

The hard case is **list-shaped content** — the natural sentence form is "the N X are A, B, C…", or "X has components A, B, C…", or the content contains enumeration markers (three, four, five, several, multiple). When you see this pattern, **stop and choose a format**. Defaulting to one multi-cloze card produces an enumeration that will desync.

### Three options for list-shaped content

**Option A — Split into N separate cards.** Each card cues from that item's own mechanism, presentation, or clinical context, not from the parent list. Best when each item has independent meaning worth carding.

```
GOOD (split):
Card: Sustained high luminal pressure damages the endothelium and accelerates atherogenesis; this risk factor is {{c1::hypertension}}.
Card: Glycation of LDL and endothelial dysfunction in poorly controlled {{c1::T2DM}} drive premature atherosclerosis.
```

**Option B — One card with hint syntax.** Best when the list is short (≤4), items are conceptually paired, and the student needs the gestalt "what are the X" recall. Hints anchor each cloze to its retrieval angle without giving the answer away.

```
GOOD (hint syntax):
Modifiable risk factors for atherosclerosis: {{c1::hypertension::BP}}, {{c2::dyslipidaemia::↑LDL ↓HDL}}, {{c3::smoking::lifestyle}}, {{c4::T2DM::glucose}}.
```

**Option C — One card without hints, accepting desync.** Reserved for tightly-coupled lists — mnemonics, fixed sequences, ordered cascades — where the items only make sense as a set and splitting would lose the sequence concept.

```
GOOD (accept desync):
Vessels most affected by atherosclerosis, in order: {{c1::lower abdominal aorta}} > {{c2::coronary}} > {{c3::popliteal}} > {{c4::internal carotid}} > {{c5::circle of Willis}}.
```

### Choosing between them

| Content shape | Option |
|---|---|
| Items each have an independent mechanism | A — split |
| Short list (≤4), conceptually paired, gestalt matters | B — hint syntax |
| Long list (≥5) of independent items | A — split |
| Fixed sequence, mnemonic, or ordered cascade | C — accept desync |

### The honest tradeoff

Hint syntax on a long list will still score lower on atomicity than the split-into-N alternative. That's the rubric measuring per-card retrieval force, which is independent of "do I know the set." Some content genuinely needs the gestalt recall (ANZCOR algorithm steps, FRAX risk factors, Beighton score items) and the right call is hint syntax with the cost paid willingly. Recognise the tradeoff; do not pretend it isn't there.

### Coupled-but-separable facts

Atomicity also fails on cards that *look* atomic but aren't:

```
BAD (two facts disguised as one):
Peripheral vascular disease results from stenosis of the {{c1::popliteal}} artery.
```

The disease name and the vessel are independent facts. Knowing one doesn't tell you the other. Two cards: one cues PVD from clinical presentation (calf claudication, absent pulses); one cues the vessel from the disease.

## Cloze Format Rules

### Cloze deletions
- Cloze format only — no Q&A
- 1-3 clozes per card, each testing one discrete fact
- Split multi-component facts into separate clozes rather than clozing the whole phrase:
  - CORRECT: `prevents {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}`
  - INCORRECT: `prevents {{c1::anterior shear of L5 on S1}}`
- Use hints when the answer is a list item or hard to recall in isolation: `{{c1::answer::hint}}`

### Extra field
- Always include an Extra field in italics, separated from the card by a line break
- Add the clinical "so what" or the mechanism — do not repeat the front
- Include relevant clinical correlates, exam pearls, or Australian guideline context
- Keep concise — 2-4 sentences maximum

## Card Writing Rules

1. **Cue forces processing** — every card must pass the cue test. If the cue is a label, redesign.
2. **Detect list-shaped content before drafting** — if the source contains "the N X are…" or enumeration markers, pick Option A/B/C from the Atomicity section before writing.
3. **Substring check before finalising** — does the cloze answer appear as a literal substring of any word in the visible cue (including shared word stems)? If yes, the card is a give-away. Redesign before submitting.
4. **One testable fact per card** — never cram multiple concepts into one card; coupled-but-separable facts split.
5. **Specify the answer shape, not the answer** — give enough context that only one answer fits, without restating its label.
6. **Function over anatomy** — do not include origins/insertions unless explicitly requested.
7. **No duplicates** — if the student says they already have a card on a topic, put that content in Extra only.
8. **Concise** — no filler words, no preamble.
9. **Cloze the yield** — cloze the high-yield fact, not background context.

## Anti-patterns

Avoid these failure modes. The first five are cue failures (D1); the last three are atomicity failures (D2). Both kill cards.

- **Restating the answer's label.** "The first-line treatment for X is {{c1::Y}}." The cue tells you the answer is the first-line treatment for X; if you know X, you know Y.
- **Echoing the source phrasing.** If the original sentence said "RA is treated with methotrexate", clozing methotrexate just trains pattern-recognition of that sentence.
- **Syntactic give-aways.** "An {{c1::aneurysm}}…" — the indefinite article "an" cues a vowel-initial answer. Use "a/an" or restructure.
- **Substring overlap between cue and cloze.** The cloze answer appears as a literal substring of a word in the visible cue, or shares an obvious root. The reader can recover the answer by morphological inspection alone, no clinical retrieval needed.
  - BAD: `Vasospastic angina is caused by coronary {{c1::spasm}}.` — "spasm" sits inside "Vasospastic".
  - BAD: `Arteriolosclerosis affects the {{c1::media}} of small vessels.` — "media" sits inside the layer adjective in the parent topic.
  - BAD: `Tachyarrhythmias from rapid AV node conduction are treated with AV {{c1::nodal}} blockers.` — "nodal" sits inside "node".
  - Fix: reword the cue so the shared stem doesn't sit beside the blank. "Vasospastic angina is caused by transient {{c1::coronary artery spasm}}" still leaks; the cleanest fix is to cue from presentation or precipitant instead ("Cocaine-induced chest pain with ST elevation but clean coronaries on angiography suggests {{c1::vasospastic angina}}").
- **Definitional clozes.** "{{c1::Apoptosis}} is programmed cell death." That's a dictionary, not a flashcard.
- **Setting up the answer with a near-synonym.** "Inflammation of the joint is called {{c1::arthritis}}." The cue is the definition; the cloze is the term. Pure keyword match.
- **List-as-cloze without hints.** "Modifiable risk factors are {{c1::HTN}}, {{c2::dyslipidaemia}}, {{c3::smoking}}, {{c4::T2DM}}." Each cloze is an independent fact bundled onto one card; the easy ones will keep the card off the review queue while the hard ones never mature. Use Option A (split), B (hints), or C (accept desync) deliberately.
- **Cascade or sequence clozed at multiple points.** "Endothelial dysfunction → lipid leak → {{c1::oxidation}} → macrophage uptake → {{c2::foam cells}} → {{c3::SMC migration}}." Three independent retrievals on one card; will desync immediately. Either split by step or cue a single step from its predecessor.
- **Coupled-but-separable facts bundled.** "{{c1::Angina}} results from stenosis of the {{c2::coronary}} arteries." The disease name and the vessel are separable retrievals. Split into two cards, each cued from a different angle (presentation → disease; disease → vessel).

## When a card will deliberately flag

Some content is inherently label-association: the answer is a name and the cue is the entity's defining feature, the etymology unavoidably contains the answer, or the card teaches a name ↔ feature mapping that has no processing-cue equivalent. Mnemonics, eponym → mechanism associations, and etymologically self-revealing terms (vasospastic → spasm) fall here.

For these, write the card honestly and accept that the scorer will return **FLAG-inherent** rather than PASS. This is not a generator failure — it's the scorer correctly recognising that no rewrite preserves what the card teaches.

### The test before accepting a flag

Before writing a card that you expect will FLAG-inherent, apply the same test the scorer uses:

> **Could this be rewritten with a processing cue (mechanism, presentation, contrast, cause, consequence) while preserving the same retrieval target and pedagogical value?**

- **YES** → write the better version. A flag here would be laziness disguised as inherence.
- **NO** → write the label-association card; accept the flag.

### What this licenses

- "The mnemonic for vessels affected by atherosclerosis is {{c1::A CoPy Cat named Willis}}" — no processing cue exists for a mnemonic.
- "Marfan syndrome → {{c1::cystic medial degeneration}} of the aortic root" — eponym ↔ tissue lesion mapping; reversing the cue tests a different fact.

### What this does NOT license

- Defaulting to label cues when a processing cue would work. "The first-line treatment for anaphylaxis is {{c1::adrenaline}}" is **lazy**, not inherent — the mechanism cue exists and preserves the same fact.
- Definitional clozes that could be reframed as scenario-cued recall. "Angina is chest pain due to myocardial {{c1::ischaemia}}" is fixable by clinical scenario; do not call it inherent.
- Bundling enumerations under "it's all label-association anyway" — atomicity rules still apply.

When unsure, write the processing-cue version and let the scorer flag it if needed. It is better to over-correct than to import lazy cards under the inherent rule.

## Style

- No bold, headers, or bullet points inside card text
- Australian clinical context where relevant (RACGP, ETG, PBS, TGA, ACSQHC)
- Pitch at understanding, not memorisation — explain reasoning in Extra
- Direct tone, no affirmations or filler

## Card quantity

- Default: one card per concept unless the student requests more
- Fewer high-yield cards over more low-yield cards
- If a topic is low-yield, say so and offer 1 card maximum

## Examples

### Good — mechanism cue
```
{{c1::Adrenaline}} reverses the bronchospasm, vasodilation, and capillary leak of anaphylaxis.

*Extra: Acts on α1 (vasoconstriction), β1 (inotropy), and β2 (bronchodilation, mast cell stabilisation). IM into anterolateral thigh; repeat every 5 min as needed. ANZCOR guideline.*
```

### Good — presentation cue
```
A patient develops stridor, hypotension, and urticaria minutes after a bee sting; the immediate drug is {{c1::adrenaline}}.

*Extra: IM, anterolateral thigh, 0.01 mg/kg up to 0.5 mg. The diagnostic features here are airway + circulatory compromise with a temporal trigger — that combination is anaphylaxis until proven otherwise.*
```

### Good — function cue with split clozes
```
The iliolumbar ligament is the primary restraint against {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}.

*Extra: The lumbosacral junction is tilted ~30° from horizontal, so body weight creates a constant anterior shear vector at L5/S1. The iliolumbar ligament is the main passive restraint against spondylolisthesis at this level.*
```

### Good — mechanism cue with feedforward concept
```
Transversus abdominis fires {{c1::before}} limb movement ({{c2::feedforward}} activation), independent of {{c3::direction of movement}}.

*Extra: In chronic LBP this anticipatory activation is delayed or absent — a motor control deficit, not a strength deficit. Rehab targets low-load activation before progressive loading.*
```

### Good — hint syntax for list (Option B)
```
Modifiable risk factors for chronic LBP: {{c1::smoking::addiction}}, {{c2::obesity::BMI}}, {{c3::sedentary lifestyle::activity}}, {{c4::poor sleep::recovery}}.

*Extra: Psychosocial yellow flags (catastrophising, fear-avoidance, job dissatisfaction) are stronger predictors of chronicity than any of these but live on a separate card.*
```

### Good — split cards for independent items (Option A)

When each item in a list has its own mechanism worth carding, split rather than enumerate:

```
Card 1: Sustained high luminal pressure damages the endothelium and accelerates atherogenesis; this risk factor is {{c1::hypertension}}.

*Extra: Endothelial shear stress at branch points is the proximate mechanism; lifelong BP control prevents the cumulative dose.*

Card 2: Glycation of LDL and persistent endothelial dysfunction in poorly controlled {{c1::type 2 diabetes}} drive accelerated atherosclerosis.

*Extra: Glycated LDL is more readily oxidised and taken up by macrophage scavenger receptors — the foam cell pathway runs hotter.*
```

Same source content as the hint-syntax example; different structural choice because each item carries its own mechanism worth a card.

### Bad — label cue (give-away)
```
The first-line treatment for anaphylaxis is {{c1::adrenaline}}.

*Extra: Important drug.*
```
Problems: the cue restates the answer's label, so retrieval is keyword-matching; Extra adds nothing.

### Bad — anatomy without function (label cue)
```
The iliolumbar ligament runs from {{c1::L5 transverse process}} to {{c2::iliac crest}}.

*Extra: Connects the spine to the pelvis.*
```
Problems: tests anatomy not function; the cue ("iliolumbar ligament runs from…to") just asks you to recall the ligament's endpoints, which is low-yield without the functional context.

### Bad — definitional cloze
```
{{c1::Anaphylaxis}} is a severe IgE-mediated hypersensitivity reaction with airway and circulatory compromise.

*Extra: Treat with adrenaline.*
```
Problems: this is a definition, not a retrieval prompt. The cue is the definition, the cloze is the term — reverse it (cue with the clinical scenario, cloze the diagnosis or the drug) to get retrieval force.

### Bad — list-as-cloze without hints
```
The four modifiable risk factors for atherosclerosis are {{c1::hypertension}}, {{c2::dyslipidaemia}}, {{c3::smoking}}, and {{c4::type 2 diabetes}}.

*Extra: All four are addressable in primary prevention.*
```
Problems: four independent facts on one card. The first time you remember "hypertension" but blank on "T2DM", you'll still press "Good" because three of four felt fluent — and T2DM never matures. Use hints (Option B), split (Option A), or accept desync deliberately (Option C). Defaulting to this format is the failure.

## Australian clinical context

Where relevant, align card content with RACGP guidelines, ETG (Therapeutic Guidelines), and other reputable Australian sources (ACSQHC, NPS MedicineWise, TGA, ANZCOR). Note guideline source in Extra when the recommendation is guideline-specific.

## Common triggers

- "Give me a card on X"
- "Make a card for that"
- "Card on X please"
- "That's yield — card?"
- Any request following clinical content explanation
