# Claude CLI setup (recommended)

Use your existing Claude Pro or Max subscription — no API key sits in your config, and you get the best Claude models.

**You will need:** A [Claude Pro or Max](https://claude.ai) subscription ($20–100/month). Node.js installed (most developers already have it).

---

## Steps

**1. Install Claude Code CLI**

Open Terminal:

```
npm install -g @anthropic-ai/claude-code
```

Or follow the [official install guide](https://claude.ai/code) if you prefer a different method.

**2. Log in**

```
claude
```

This opens a browser login. Sign in with your Claude account. You only do this once.

**3. Configure Ankisstant**

*Tools → Ankisstant Settings…* → **Provider** tab.

Set Provider to **Auto** (detects CLI automatically) or **Claude CLI** explicitly.

Click **Detect** next to the CLI path — Ankisstant checks the standard install locations and fills the path in.

**4. Test and save**

Click **Test connection** → **Save**.

---

## Manual path override

If *Detect* doesn't find the CLI, find it yourself:

```
which claude
```

Paste the output path into the *Claude CLI path* field in settings.

---

## Anthropic API (pay-as-you-go)

If you don't have a Claude subscription but want direct API access:

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. *Tools → Ankisstant Settings…* → Provider → **Anthropic**
3. Paste your `sk-ant-…` key
4. Pick a model — `claude-haiku-4-5` is cheapest (fractions of a cent per session)
5. Test → Save

Typical cost for Ankisstant use: a few cents per day at Haiku pricing.
