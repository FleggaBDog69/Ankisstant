# Gemini free tier setup

**You will need:** A Google account. 5 minutes. No payment required.

> **Privacy notice:** Google's free API tier may use your prompts to improve its models. If this is a concern, use [Ollama (fully offline)](setup-ollama.md) or upgrade to the Gemini paid tier (same steps, different model — data is not used for training on paid plans).

---

## Steps

**1. Get a free API key**

Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → **Create API key** → copy the key (starts with `AIza…`).

No billing setup required for the free tier.

**2. Open Ankisstant Settings**

In Anki: *Tools → Ankisstant Settings…* → **Provider** tab.

**3. Select Gemini**

Set Provider to **Gemini**. Paste your API key into the *Gemini API key* field.

**4. Pick a model**

- **gemini-2.0-flash** — recommended for free tier. Fast and capable for card creation.
- **gemini-2.5-flash** — better reasoning, still free tier eligible.
- **gemini-2.5-pro** — highest quality; free tier has lower rate limits.

**5. Test and save**

Click **Test connection**. You should see a green confirmation. Click **Save**.

---

## Free tier limits

Google's free tier has rate limits (requests per minute / per day). For typical Ankisstant use — a few card-creation sessions per day — you'll rarely hit them. If you do, wait a minute and retry, or upgrade to a paid plan.

---

## Paid tier

Same setup, but select a paid plan at [aistudio.google.com](https://aistudio.google.com). Prompts on paid plans are not used for model training. The API key setup is identical.
