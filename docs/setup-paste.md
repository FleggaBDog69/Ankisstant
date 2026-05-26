# Paste import — no AI subscription needed

Use any free web chatbot (ChatGPT, Claude.ai, Gemini) to create cards. No API key required.

**You will need:** A free chatbot account. 10 minutes for one-time setup. Nothing else.

---

## One-time setup: build your card assistant

You'll create a dedicated assistant that outputs cards in the exact format Ankisstant expects. Do this once.

### ChatGPT — custom GPT (recommended)

1. [chatgpt.com](https://chatgpt.com) → *Explore GPTs* → **+ Create** → **Configure** tab
2. Paste the prompt block below into **Instructions**
3. Under *Capabilities* — turn off Web Search, Canvas, Code Interpreter, Image Generation
4. Save as **Only me**

> No ChatGPT Plus? Use a *Project* instead: *Projects* → *+ New project* → *Add instructions* → paste the same block.

### Claude.ai — Project

[claude.ai](https://claude.ai) → *Projects* → **+ Create project** → **Set custom instructions** → paste the block below. Free tier is fine.

### Any other chatbot

Paste the block as a system prompt or at the top of each session.

---

## The prompt to paste

Copy this entire block:

```
You turn a single clinical "knowledge gap" into high-yield Anki cloze cards
for a Year 3 Australian medical student.

The user pastes ONE gap (a phrase, missed question, topic, or short note).
Do not ask follow-up questions — your reply goes straight into their Anki
addon and they cannot reply to you.

OUTPUT: ONE JSON object and NOTHING else. No markdown fences, no preamble,
no trailing notes. First character is { and last is }. Shape exactly:
{"tags": {"system": "...", "subsystem": "...", "topic": "..."},
 "cards": [{"front": "...", "extra": "..."}, ...]}

CARD RULES
- One atomic fact per card. Split if testing two things.
- Cloze syntax: {{c1::answer}}. Keep cloze answers 1–4 words.
- No enumerations ("list the 5 causes of X"). Pick top 1–2 or split.
- extra: clinical context or mnemonic. Use "" if nothing worth adding.
- Only <b> and <u> formatting in fields.
- Australian drug names and guidelines (eTG, RACGP) when relevant.
- As many cards as the gap genuinely needs (usually 1–5). Don't pad.

TAGS
- system: top-level body system (Cardio, Neuro, Endo, GI, Resp, Renal, …)
- subsystem: more specific (Arrhythmias, Stroke, Diabetes, …)
- topic: most specific entity/drug/sign (AFib, Digoxin, …)
- PascalCase or snake_case. Use "" for any level that's genuinely unclear.
```

---

## Day-to-day (30 seconds per gap)

1. Paste a knowledge gap into your assistant — e.g. *"AF rate control: beta blockers vs CCBs"*
2. Copy the JSON it returns
3. Ankisstant → **Create with Claude** → **📋 Paste cards** → paste → review → **Add all**

That's it.

---

## What the output should look like

```json
{
  "tags": {"system": "Cardio", "subsystem": "Arrhythmias", "topic": "AFib"},
  "cards": [
    {"front": "First-line rate control in <b>AF</b> (no heart failure) is {{c1::beta-blocker or non-DHP CCB}}", "extra": "Metoprolol or diltiazem. Avoid verapamil if systolic dysfunction."},
    {"front": "Lenient rate control target in chronic AF is {{c1::< 110 bpm}}", "extra": "RACE II: strict < 80 bpm has no added benefit."}
  ]
}
```

A bare array `[{...}]` also works but won't carry tags.

---

## Troubleshooting

**Output has extra text / code fences** — remind the assistant: *"Return only the JSON object. No preamble, no code fences."* For ChatGPT custom GPTs, ensure Web Search and Code Interpreter are off.

**Want a different card format?** See [customising card creation](customising-cards.md).
