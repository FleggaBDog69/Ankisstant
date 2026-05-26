# Ollama — local AI, fully offline

Run AI entirely on your own machine. No API key, no account, no data ever leaves your computer.

**You will need:**
- A Mac, Windows, or Linux machine
- 8 GB RAM minimum (16 GB recommended for better models)
- ~5 GB free disk space per model
- 15 minutes for first-time setup

---

## Steps

**1. Install Ollama**

Download from [ollama.com](https://ollama.com) and install. On macOS, Ollama runs as a menu bar app.

**2. Pull a model**

Open Terminal and run:

```
ollama pull llama3.1
```

This downloads the model (~4.7 GB). Pull once; it stays on disk.

**3. Verify it's running**

```
ollama list
```

You should see `llama3.1:latest` (or whichever model you pulled) in the output.

Ollama starts automatically when you log in. If it's not running, open the Ollama app.

**4. Configure Ankisstant**

*Tools → Ankisstant Settings…* → **Provider** tab → set Provider to **Ollama**.

- **Base URL:** `http://localhost:11434` (default — leave as-is unless you changed Ollama's port)
- **Model:** type the model name exactly as it appears in `ollama list`, e.g. `llama3.1:latest`

Click **Test connection** → **Save**.

---

## Recommended models

| Model | Disk / RAM | Notes |
|---|---|---|
| `llama3.1` | ~5 GB / 8 GB | Good default. Handles card creation well. |
| `qwen2.5` | ~4 GB / 8 GB | Strong for structured output (JSON). |
| `llama3.2` | ~2 GB / 6 GB | Smaller/faster, slightly weaker. |
| `llama3.1:70b` | ~40 GB / 40 GB | Best quality. Needs a powerful machine. |

For Ankisstant card creation, `qwen2.5` often produces cleaner JSON output than Llama models. Try both.

---

## Notes

- First request after a cold start takes a few seconds while the model loads into RAM — this is normal.
- Ollama keeps the model loaded for a few minutes after the last request, then unloads it to free RAM.
- You can run multiple models; just change the model name in settings.
- Internet connection not required once the model is pulled.
