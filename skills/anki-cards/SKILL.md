---
name: anki-cards
description: Use this skill whenever the user wants to create Anki cloze deletion cards from medical or clinical content. Triggers include requests like make a card, give me a card, anki card, cloze, that's yield card, or any request to convert clinical content into flashcard format. Designed for Year 3 Australian medical students using a cloze-deletion based Anki workflow. Do NOT use for Q and A format cards or non-medical content.
---

# Anki Cloze Card Creation — Australian Medical Student

> Mirrors the Ankisstant add-on's writing prompt (`tools/card_creator.py` → `CARD_GEN_SYSTEM`), which is the source of truth. Edit there first, then here.

## The default card: one fact, the shortest cue that pins it

The target style is the AnKing deck: short, direct, one fact per card.

- Each card tests **one fact**. The visible text is the shortest cue that makes exactly
  one answer correct — nothing more.
- **Hard limit: the front is at most 25 words** (cloze answers count; HTML and hints
  don't). Aim for about 15. A front that needs more than 25 words is carrying two
  facts — split it.
- **Direct cues are good.** State the fact plainly and cloze the answer:
  > `<b>Clomiphene</b> {{c1::increases::increases/decreases}} pituitary release of FSH and LH.`
  > `Unopposed <b>oestrogen</b> is the usual cause of {{c1::endometrial hyperplasia}}.`
  > `<b>Kawasaki disease</b> can cause coronary artery {{c1::aneurysms}}.`
- **Hints sparingly** — only where the student couldn't otherwise tell what the blank
  wants: `{{c1::increased::increased/decreased}}`, `{{c1::echocardiogram::which test?}}`.
  If the cue already makes the kind of answer obvious, no hint. Most cards have none.

## Story cards are rationed

A **story** card is cued by a clinical vignette ("A 24-year-old … presents with …") or a
mechanism chain ("X blocks Y, so Z rises, which …").

- **At most one in five cards** may be a story card (fewer than five cards → none,
  unless the topic itself is a clinical scenario).
- Use one only where the fact can't be tested without the setting — e.g. a diagnosis
  that only a combination of findings points to.
- A story card still obeys the 25-word limit. Mark it `"style": "story"`; every other
  card is `"style": "fact"`.

## One fact in the front

Atomicity is about the whole front, not just how many clozes you used. A one-cloze card
is still non-atomic if the visible text smuggles a second fact in — an em-dash tail, a
parenthetical aside, a "because…" clause.

> BAD: `In newly diagnosed <b>heart failure</b>, {{c1::transthoracic echocardiography}} is the initial test — it quantifies ventricular function and detects valve lesions that may be a surgically correctable cause.`
> GOOD: `The <u>initial</u> test in newly diagnosed <b>heart failure</b> is {{c1::transthoracic echocardiography}}.`

> BAD: `<b>Clomiphene</b> blocks hypothalamic oestrogen negative feedback, so GnRH pulses rise and the pituitary releases more {{c1::FSH}} — recruiting {{c2::multiple}} follicles at once.`
> GOOD (two cards): `<b>Clomiphene</b> blocks oestrogen negative feedback at the {{c1::hypothalamus}}.` · `<b>Clomiphene</b> raises the risk of {{c1::multiple}} pregnancy.`

**Deletion test** (every card, clause by clause): take one clause out and re-read. If
the cue still forces the same answer, delete that clause.

## Give-aways (banned — the answer can be read off the card)

- **Substring:** the cloze answer, or a shared word-stem, appears in the visible text.
  `Vasospastic angina is caused by coronary {{c1::spasm}}.` — "spasm" sits inside "Vasospastic".
- **Definition ↔ term:** `{{c1::Apoptosis}} is programmed cell death.` /
  `Inflammation of the joint is called {{c1::arthritis}}.` Test something the term
  does or causes instead.
- **Synonym set-up:** the cue restates the answer in other words, or a second cloze names the first.
- **Grammar:** `an {{c1::…}}` cues a vowel — write "a(n)" or restructure.

## List-shaped content — STOP and choose a format

When the content is "the N X are A, B, C…" (or uses three / four / several):

- **A — Split into N cards**, one per item. Default for lists of ≥5 or items with
  independent meaning.
- **B — One card with hint syntax** `{{c1::answer::hint}}` — short lists (≤4) where the
  gestalt "what are the X" recall matters.
- **C — One card without hints, accepting desync** — only fixed sequences / mnemonics.

Never default to a multi-cloze enumeration without choosing A/B/C.

## Cloze numbering (commonly done wrong)

- Separate facts each hidden on their own sibling card → `c1`, `c2`, `c3`.
- Deletions revealed together as one card → the same number.
- Split a multi-component fact into separate clozes rather than clozing a whole phrase:
  `prevents {{c1::anterior shear}} of {{c2::L5}} on {{c3::S1}}`, not `{{c1::anterior shear of L5 on S1}}`.

## Format & style

- Cloze only — no Q&A. Usually one cloze; at most 2–3 when they're parts of one fact.
  Short answers (1–4 words).
- `<b>Bold</b>` the topic anchor and qualifiers ("first-line", "most common");
  `<u>underline</u>` only words already doing the retrieval work; `<i>italicise</i>`
  genus/species. Inline `<b>`/`<u>`/`<i>` only in the front — no bullets, lists or `<br>`.
- Ruthless concision — no filler ("important to know that…"), no framing the answer
  doesn't need.
- **Extra field is optional and short.** An empty string is the right answer for most
  cards. At most ~20 words — one or two bolded points (`<b>Pearl:</b> IM thigh, repeat
  every 5 min`). Never a paragraph or a second lesson.
- No duplicates — if the student already has a card on something, don't write it again.

## Output

In chat, give each card as the front, then the Extra (if any) in italics on its own line:

```
<b>Kawasaki disease</b> can cause coronary artery {{c1::aneurysms}}.

*Extra: <b>Screen:</b> echocardiogram at diagnosis*
```

When a caller asks for JSON (the Ankisstant add-on does), return ONLY a JSON array of
`{"front": ..., "extra": ..., "style": "fact" | "story"}` objects — no prose, no fences.

## Card quantity

Scale the set to the number asked for — more cards means more single facts, never longer
cards. Never pad with low-yield filler; if a topic can't support the count, make fewer.
Default (no number given): one card per concept.

## Common triggers

- "Give me a card on X" · "Make a card for that" · "Card on X please" · "That's yield — card?"
