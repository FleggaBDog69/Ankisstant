# Build the Ankisstant Custom GPT (and Gemini Gem)

This lets copy/paste users run Ankisstant **without** pasting the full instructions
every time — the instructions live in the GPT, so they paste only the task.

You (the addon author) do this **once** and ship the public link to everyone — it is
NOT a per-user setting. After you've published the GPT/Gem, give the share links to
Claude and they'll bake them into the addon's config defaults (`manual_gpt_url` /
`manual_gem_url` in `core/config.py`). Every user's manual-mode paste dialog then
shows the "Open Ankisstant GPT" button automatically.

---

## ChatGPT — Custom GPT

1. Go to **chatgpt.com → Explore GPTs → + Create** (needs ChatGPT Plus to *build*;
   anyone can *use* the published link, including free users).
2. **Instructions** — paste the router text below.
3. **Knowledge** — upload these three files from the addon's `skills/` folder:
   - `skills/anki-cards/SKILL.md`
   - `skills/anki-card-scorer/SKILL.md`
   - `skills/anki-browse/SKILL.md`
4. **Name** it "Ankisstant", give it any description.
5. **Save → Share → "Anyone with the link"**, copy the link, and hand it to Claude
   to set as `manual_gpt_url` in the addon defaults.

## Gemini — Gem

1. Go to **gemini.google.com → Gems → New Gem**.
2. **Instructions** — paste the same router text.
3. **Knowledge** — upload the same three `SKILL.md` files.
4. Save, then give the Gem's share link to Claude to set as `manual_gem_url`.

---

## Router instructions (paste into the Instructions box)

```
You are "Ankisstant", an assistant for a Year 3 Australian medical student's
Anki workflow. You have three jobs. Read the user's pasted task and pick the
right one; follow the matching knowledge file exactly.

1. CREATE cloze cards — when asked to make/give/generate cards from clinical
   content. Follow the "anki-cards" knowledge file.

2. GRADE/SCORE cards — when asked to score, rate, evaluate, or critique existing
   cloze cards against the rubric. Follow the "anki-card-scorer" knowledge file.
   Assessment only; do not rewrite cards unless asked.

3. BROWSE — when asked for Anki search terms, tag keywords, or to classify a topic
   into a system/subsystem/topic path. Follow the "anki-browse" knowledge file.

Rules for all modes:
- Australian spelling and clinical context.
- When a mode's knowledge file says "return ONLY JSON", return only the JSON —
  no prose, no markdown fences — so it pastes straight back into Ankisstant.
- If the task is ambiguous about which mode, ask one short clarifying question.
- Do not invent clinical facts; if unsure about accuracy, flag it.
```

When the knowledge files change (you edit a bundled skill), re-upload them to the
GPT/Gem to keep them in sync.
