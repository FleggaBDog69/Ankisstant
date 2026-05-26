# Customising card creation

By default, Create uses a built-in prompt that produces AnKing-style cloze cards for an Australian Year 3 medical student. Here's how to change the format.

---

## Option 1 — System prompt override (all providers)

*Tools → Ankisstant Settings…* → **Card Creator** tab → **System prompt override**.

Paste any instructions here. They prepend the built-in prompt (or replace it, depending on the toggle). Examples:

- *"Always include a clinical vignette framing sentence."*
- *"Use Malleus-style Q&A format: question on front, single answer on back."*
- *"Produce 3 cards maximum per gap."*

The default prompt (for reference) is in `docs/standalone-ai-card-prompt.md`.

---

## Option 2 — Skills (Claude CLI provider only)

Skills let you define a reusable card-format persona that Claude Code loads on demand — no token cost per-call.

Two skills are pre-configured:
- **`anki-cards`** — default AnKing cloze format (Year 3 Australian)
- **`malleus-anki`** — Q&A format for [Malleus CM](https://www.ankihub.net/shared-decks/malleus) AnkiHub submissions (Australian guidelines, specific tag schema)

To apply a skill to a notetype profile:

1. *Tools → Ankisstant Settings…* → **Card Creator** → **Notetype profiles**
2. Select (or create) the profile for your notetype
3. Set **Skill invocation** to `/malleus-anki` or `Use the anki-cards skill` (or any skill name)

### Building a custom skill

Create a file at `~/.claude/skills/<your-skill-name>/SKILL.md`. Write your card format rules there — what fields to use, what style, what level of detail. Claude Code auto-loads the skill body when it matches the invocation text.

See `~/.claude/skills/anki-cards/SKILL.md` as an example.

---

## Option 3 — Custom GPT / Claude Project (paste import path)

If you're using the [paste import workflow](setup-paste.md), edit the instructions in your custom GPT or Claude Project. The prompt in that guide is the same one Ankisstant uses internally — modify it freely.

To target a specific notetype (e.g. Malleus), append that notetype's rules beneath the base prompt.

---

## Option 4 — Per-notetype profiles

*Settings → Card Creator → Notetype profiles* lets you configure a separate deck, notetype, and skill per card type. Useful if you switch between AnKing cloze cards and Malleus Q&A cards depending on the topic.

Each profile carries:
- Default deck
- Default notetype
- Skill invocation (for CLI) or skill ID (for Anthropic API beta)
- Any system prompt override specific to that notetype
