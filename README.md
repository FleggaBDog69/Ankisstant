# Ankisstant

**Four tools for Anki. Close the gap between what you miss and what you study.**

Miss a question in your QBank → Ankisstant finds the matching cards in your own deck, re-rates them so you see them tomorrow, and logs the gap. From there, browse by topic or create new cards — all without leaving Anki.

## Get started — pick your setup

Before anything else, pick how Ankisstant connects to AI. You need one of these:

| I have… | Guide | Cost |
|---|---|---|
| ChatGPT (Plus or free) or any web AI | [Paste import](docs/setup-paste.md) | Free |
| A Google account | [Gemini free tier](docs/setup-gemini.md) | Free* |
| Claude Pro or Max | [Claude CLI](docs/setup-claude-cli.md) | Covered by sub |
| Want offline / full privacy | [Ollama (local)](docs/setup-ollama.md) | Free, ~15 min setup |
| Pay-as-you-go | [Anthropic API](docs/setup-claude-cli.md#anthropic-api-pay-as-you-go) or [Gemini API](docs/setup-gemini.md#paid-tier) | Cents/session |

> **ChatGPT Plus ≠ API access.** ChatGPT Plus is a subscription to chatgpt.com — it does not include API credits. If you have ChatGPT, use the [paste import path](docs/setup-paste.md).
>
> **Gemini free tier\*:** Free tier prompts may be used by Google to improve its models. See the [Gemini guide](docs/setup-gemini.md) for details and the paid-tier option.
>
> **Confirmed-working providers:** **Gemini**, the **Claude API** (Anthropic), the **Claude CLI**, and **no-AI paste import** are the tested, supported paths. **OpenAI** and **Ollama** are implemented but not yet confirmed end-to-end — use them at your own risk and please report back.
>
> **Gemini model note:** on the free tier only **Gemini 2.5 Flash** has quota — *Gemini 2.0 Flash* and *Gemini 2.5 Pro* return `429 limit: 0` for free-tier keys. Ankisstant defaults to 2.5 Flash and will auto-correct an old saved default on upgrade.

Not sure where to start? [Gemini free tier](docs/setup-gemini.md) — five minutes, no payment.

## What it does

### QBank with Claude

Capture a missed question (text or screenshot) from any source — UWorld, Amboss, your med school's QBank. Claude finds the matching cards in your own deck and re-rates them as **Again** so the scheduler surfaces them tomorrow. The question stem is appended for context. Ten seconds, never leaving Anki.

A daily heatmap on the deck browser shows your capture habit at a glance.

> Inspired by [Dr Patrick Lee's](https://drpatricklee.substack.com/) approach to active recall and gap-targeted review.

### Knowledge Gaps

The hub. Gaps arrive from missed questions, manual notes, or the *Analyse LO* helper (paste a learning objective; Claude flags what your cards don't cover). From any gap, jump straight to Browse or Create.

### Browse with Claude

Describe a topic in plain English. Claude generates the Anki search terms, runs them against your deck, and lets you bulk-tag and unsuspend the hits. Search by note text or by tag.

### Create with Claude

When no card exists yet, draft cloze cards from a topic, pasted text, a URL, or an attached PDF / PowerPoint. Review every card before it's added.

Want cards in a specific format? See [customising card creation](docs/customising-cards.md).

## Install

1. Install from AnkiWeb (paste the addon code in *Tools → Add-ons → Get Add-ons*), or grab `ankisstant.ankiaddon` from a [GitHub release](https://github.com/FleggaBDog69/Ankisstant/releases) and use *Tools → Add-ons → Install from file…*
2. Restart Anki. A setup wizard walks you through picking your provider.

**Requires Anki 2.1.50+.** The v3 / FSRS scheduler is recommended so missed-Q re-grading schedules correctly.

## Privacy

- No telemetry. Period.
- Your collection stays local — only what you explicitly hand to a tool is sent to your AI provider (Browse sends search terms, not cards; Create sends the text or PDF you attach).
- API keys are stored in this profile's `meta.json` only, never in version-controlled files.

## Credits

- [Dr Patrick Lee](https://drpatricklee.substack.com/) — medical education and spaced repetition inspiration.
- [Review Heatmap](https://ankiweb.net/shared/info/1771074083) (addon 1771074083) — the original Anki heatmap addon for tracking your daily review habit. Ankisstant's heatmap tracks QBank captures specifically; Review Heatmap tracks your reviews. They complement each other.

## Licensing

GNU AGPL v3. Bundles ported code from the [*Card Management* addon](https://ankiweb.net/shared/info/874215009) by Ren Tatsumoto (AGPL v3). See `NOTICES.md`.

## Bug reports / feedback

Open an issue at <https://github.com/FleggaBDog69/Ankisstant> or message Flegga directly.
