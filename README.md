# Ankisstant

Four Claude-powered tools for Anki, bundled into one addon:

- **QBank with Claude** — capture questions you missed in your QBank, then
  let Claude find the matching Anki cards so you can re-rate them as Again.
  Includes a daily heatmap on the deck browser.
- **Browse with Claude** — type a topic, Claude generates Anki search terms,
  the addon runs each search and lets you bulk-tag / unsuspend the hits.
- **Knowledge Gaps** — one unified queue for everything you don't know yet.
  Items can come from manual notes, the Analyse LO sub-feature (paste an
  LO + pick a tag, Claude flags concepts not covered by your cards), or
  captured QBank misses. From any gap, send to Browse with Claude or
  straight to Create with Claude.
- **Create with Claude** — draft cloze cards from a topic, pasted text, a URL,
  or attached PDFs / PowerPoints. Review each card before adding.

Everything runs locally against your collection. Nothing is sent anywhere
except your chosen Claude provider.

## Requirements

- Anki 2.1.50 or newer (the v3 / FSRS scheduler is recommended for QBank's
  Again-grading; older schedulers fall back to a queue reset).
- A Claude provider — either:
  - **Claude Code CLI** (preferred — no key in your config), or
  - An **Anthropic API key**.

The first-launch welcome wizard helps you pick one.

## Install

1. Download `ankisstant.ankiaddon` from a release (or zip up the `ankisstant/`
   folder yourself — see `package.sh`).
2. In Anki: *Tools → Add-ons → Install from file…* → pick the `.ankiaddon`.
3. Restart Anki. On first profile load, the welcome wizard pops up.

## First-launch setup

The welcome dialog asks you to pick a provider:

- **Claude Code CLI** — click *Detect* (looks in `~/.claude/local/`,
  `/usr/local/bin/`, Homebrew, npm, and Windows equivalents) or paste the path
  yourself.
- **Anthropic API key** — paste your `sk-ant-…` key.

Use *Test connection* to verify before saving. You can also pick a default
deck, notetype, and audit-tag prefix — the wizard pre-fills sensible values.

If you skip the wizard, you can run it again from
*Tools → Ankisstant Settings → About → Re-run welcome wizard*.

## Day-to-day use

Open the suite from **Tools → Ankisstant…** (or `Ctrl+Shift+L`, or the
"Ankisstant" link in the top toolbar). Pick a tool tab.

Each tool has a `?` button next to its title with workflow-specific help.

## Settings

*Tools → Ankisstant Settings…* has one tab per tool plus a Global tab for
provider config and an About tab.

## Privacy

- No telemetry. Period.
- Your Anki collection isn't sent anywhere unless you explicitly ask Claude
  to do something with it (e.g. *Browse with Claude* sends search terms, not
  cards).
- Your API key is stored in this profile's `meta.json` only.
- *Create with Claude* attachments: PDFs are sent to Claude (CLI Read tool or
  Anthropic document block); PPTX text is extracted locally.

## Troubleshooting

- **"No Claude backend available"** — open *Tools → Ankisstant Settings* and
  either set the CLI path or paste an API key. The error dialog has an
  *Open setup…* button that goes straight to the welcome wizard.
- **"Notetype 'X' not found"** — open *Settings → Create with Claude* (or
  *QBank*) and pick a notetype that exists in your collection.
- **Add Cards screen lost my queue** — closing the Add window stops the
  auto-advance queue. Re-open *Create with Claude* and click *Open Add screen*
  again from the review dialog. Approved-but-unadded cards aren't persisted.
- **PowerPoint extracted no text** — Ankisstant reads slide text + speaker
  notes from the OOXML. Image-only slides won't work. Export to PDF instead.
- **Big PDFs are slow / out-of-context** — PDFs over 10 MB show a confirm
  prompt, 30 MB+ are refused. Split the PDF or extract relevant pages.

## Licensing

The addon ships under the GNU AGPL v3 because it bundles ported code from
the *Card Management* addon (©  Ren Tatsumoto, AGPL v3). See `NOTICES.md`.

## Bug reports / feedback

Right now: just message Flegga directly.
