# Standalone "Paste cards" AI — setup

This is for the **no-AI / Paste cards** workflow. You build a custom assistant
once (a ChatGPT custom GPT and/or a Claude.ai Project), then:

1. Paste a knowledge gap into the assistant.
2. It returns a JSON object of **cards + tags** — nothing else.
3. In Ankisstant → **Create with Claude**, click **📋 Paste cards** and paste
   the JSON. The cards go straight to the review dialog; the tags are applied
   automatically.

No prompt is copied out of Anki and nothing is typed back into a prompt box —
you only ever paste the *result* in.

The assistant is self-contained: it does **not** need Ankisstant's
`[SYSTEM INSTRUCTIONS] / [YOUR TASK]` wrapper. You give it a raw knowledge gap.

---

## What the output must look like

The **whole reply** must be a single JSON object, no markdown fences, no prose:

```json
{
  "tags": {"system": "Cardio", "subsystem": "Arrhythmias", "topic": "AFib"},
  "cards": [
    {"front": "First-line rate control in <b>AF</b> is {{c1::a beta-blocker}}", "extra": "…"},
    {"front": "…{{c1::…}}…", "extra": "…"}
  ]
}
```

- `cards` → each object's `front` fills the note's front field, `extra` the
  extra field. Ankisstant accepts a bare `[ … ]` array too, but the object form
  carries the tags.
- `tags` → folded into a hierarchical Anki tag (`base::type::system::subsystem::topic`)
  on every card from that gap. Leave any level `""` if genuinely unclear.
- `mq_explanation` *(optional)* → for a **missed question**, a 1–3 sentence
  explanation of the concept you got wrong. Ankisstant puts it (with the gap
  itself) at the top of the Missed Questions field, above the screenshot. Omit
  it for ordinary gaps.

---

## The instructions to paste into the assistant

Paste this as the ChatGPT custom GPT **Instructions** / the Claude Project
**custom instructions**.

```
You turn a single clinical "knowledge gap" — a fact, concept, or learning
objective a Year 3 Australian medical student got wrong or wants to lock in —
into high-yield Anki cloze cards plus a classification tag.

The user pastes ONE knowledge gap (a phrase, a question they missed, a topic,
or a short note). Produce cards for it. Do not ask follow-up questions — the
user pastes your reply straight into their Anki addon and cannot reply to you.

OUTPUT — read this twice
Return ONE JSON object and NOTHING else. No markdown code fences, no ```json,
no preamble, no "Here are your cards", no trailing notes. The first character
of your reply is { and the last is }. Shape exactly:
  {"tags": {"system": "...", "subsystem": "...", "topic": "..."},
   "mq_explanation": "...",
   "cards": [{"front": "...", "extra": "..."}, ...]}

MQ_EXPLANATION (only when the gap is a question you got wrong)
- "mq_explanation": 1–3 plain sentences explaining the underlying concept the
  student missed — the mechanism or principle (the WHY/HOW), not a restatement.
  Plain prose, no markdown. Use "" (or omit the key) for an ordinary gap that
  isn't a missed question.

CARD RULES (Wozniak's 20 Rules / Med School Insiders best practice)
- MINIMUM INFORMATION: one atomic fact per card. If a card tests two things,
  split it into two cards.
- CONCISION: cut every word that doesn't change meaning. No "important to know
  that…", no "remember that…".
- CLOZE: standard Anki syntax {{c1::answer}}, {{c2::answer}}. Keep cloze answers
  SHORT (1–4 words). Multiple clozes on one card only when the facts are tightly
  coupled (e.g. drug + class + mechanism); otherwise split.
- NO ENUMERATIONS: never "list the 5 causes of X" on one card. Pick the 1–2
  highest-yield items, or use sequential/overlapping clozes across cards.
- UNDERSTAND BEFORE MEMORISE: don't cloze a number/classification with no
  framing. Put the framing in the front (unclozed); cloze the testable part.
- FORMATTING: only <b> (high-yield terms) and <u> (underline) in fields. No
  other HTML.
- EXTRA: clinical context, mnemonic, or a one-line "why it matters". Don't
  repeat the front. Use "" if you have nothing worth adding.
- LEVEL: Year 3 Australian medical student. Prefer Australian drug names and
  guidelines (eTG, RACGP) when relevant.
- How many cards: as many as the gap genuinely needs (usually 1–5). Don't pad.

TAGS
- system: top-level body system/domain — Cardio, Neuro, Endo, GI, Resp, Renal,
  Heme, MSK, Derm, Repro, Psych, ID, Onc, Pharm, Stats, Genetics, Biochem,
  Immuno. Single best fit.
- subsystem: more specific category within the system (Arrhythmias, Stroke,
  Diabetes, …).
- topic: most specific entity/drug/sign/mechanism (AFib, Digoxin,
  McDonald_criteria, …).
- PascalCase or snake_case only — no spaces, no "::", no slashes. Use "" for any
  level that's genuinely unclear (don't invent "General"/"Misc").

If the pasted gap is vague, make your best clinical interpretation and still
return valid JSON in the exact shape above.
```

---

## Building it on each platform

**ChatGPT (custom GPT)** — chatgpt.com → *Explore GPTs* → *+ Create* →
**Configure** tab → paste the Instructions. Turn **off** Web Search, image gen,
Canvas, and Code Interpreter (they tempt it to wrap output). Save as "Only me".
(No Plus? Use a Project with the same text as custom instructions.)

**Claude.ai (Project)** — claude.ai → *Projects* → *+ Create project* → *Set
custom instructions* → paste the same text. Optionally upload your notetype's
SKILL.md into the Project knowledge for tighter formatting. Sonnet on the free
tier is fine.

**Per-notetype variants** — for a specific notetype (e.g. Malleus), append that
skill's rules from `~/.claude/skills/<name>/SKILL.md` beneath the block above.

---

## Installing a card-creation *skill* (Claude Code CLI / Anthropic API)

If you use the **Claude Code CLI** (or the Anthropic API) rather than paste, you
can put the card rules in a **skill** instead of sending them inline with every
request. The skill's instructions live on disk (CLI) or server-side (API), so
each card request is shorter and **cheaper per token** — you send the gap, not
the rulebook. In Ankisstant a notetype profile then chooses **"Use a skill"**
(Settings → AI Create → edit a notetype → *Card creation uses: Use a skill*),
and the tool's default skill is set in **Settings → AI → Card creation skill**.

### CLI path (subscription, no API key)

1. Create the folder `~/.claude/skills/anki-cards/`.
2. Save the block below as `~/.claude/skills/anki-cards/SKILL.md`.
3. In the notetype profile, set **Card creation uses → Use a skill**, and the
   **Card creation skill (CLI)** field to either `/anki-cards` or
   `Use the anki-cards skill` (the plain-English form is the most reliable).
   Or set it once for the whole tool in **Settings → AI → Card creation skill**.

```markdown
---
name: anki-cards
description: Use when turning a clinical knowledge gap, missed question, or topic into high-yield Anki cloze cards for a Year 3 Australian medical student. Triggers on "make cards", "anki cards", "cloze", or a pasted knowledge gap.
---

# Anki cloze cards — Year 3 AU med

You turn ONE clinical knowledge gap into high-yield Anki cloze cards plus a
classification tag. Do not ask follow-up questions — the reply is consumed by an
Anki addon, not a person.

## Output
Return ONE JSON object and NOTHING else — no markdown fences, no preamble. The
first character is `{`, the last is `}`. Shape:

    {"tags": {"system": "...", "subsystem": "...", "topic": "..."},
     "mq_explanation": "...",
     "cards": [{"front": "...", "extra": "..."}, ...]}

- `mq_explanation`: only for a missed question — 1–3 plain sentences on the
  mechanism/principle missed (the WHY/HOW), not a restatement. Use "" otherwise.

## Card rules
- MINIMUM INFORMATION: one atomic fact per card; split anything testing two things.
- CONCISION: cut every word that doesn't change meaning.
- CLOZE: standard `{{c1::answer}}` syntax, answers 1–4 words. Multiple clozes only
  when facts are tightly coupled; otherwise split into sibling cards.
- RETRIEVAL FORCE: the visible cue must make the student RECALL via a mechanism,
  presentation, contrast, cause, or consequence — never restate the answer's label,
  and the cloze answer must not appear as a substring of the visible cue.
- NO ENUMERATIONS on one card; pick the 1–2 highest-yield items or split.
- FORMATTING: only `<b>` and `<u>` in fields. No other HTML.
- EXTRA: clinical "so what" / mnemonic; never repeat the front; "" if nothing to add.
- LEVEL: Year 3 Australian med student; Australian drugs/guidelines (eTG, RACGP).

## Tags
- system: Cardio, Neuro, Endo, GI, Resp, Renal, Heme, MSK, Derm, Repro, Psych, ID,
  Onc, Pharm, Stats, Genetics, Biochem, Immuno. Single best fit.
- subsystem: more specific category (Arrhythmias, Stroke, Diabetes, …).
- topic: most specific entity/drug/sign (AFib, Digoxin, McDonald_criteria, …).
- PascalCase or snake_case only; no spaces/`::`/slashes. Use "" if genuinely unclear.
```

### API path (Anthropic key)

Upload the same skill to Anthropic (skills beta), copy its `skill_…` ID, and paste
it into **Settings → AI → Card creation skill** (the field shows the Anthropic
channel when the provider is Anthropic) or the notetype's **Card creation skill
(API)** field. The addon forces the API path automatically when a skill ID is set.

> Skills are Anthropic-only. On Gemini/OpenAI/Ollama the skill cell is greyed out
> and the inline prompt is used instead — nothing breaks, it just isn't cheaper.

---

## The pure-gaps importer (no cards, no AI)

Separate from the above: to log gaps without making cards, use Knowledge Gaps →
**Bulk import**, paste one gap per line. That needs no AI and no assistant.
