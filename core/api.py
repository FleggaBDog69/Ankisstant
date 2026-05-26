# Single AI client used by every tool. No HTTP / subprocess elsewhere.
#
# Provider modes (configured globally as cfg["provider"]):
#   - "auto"     : prefer local Claude Code CLI when present, fall back to API
#   - "cli"      : Claude Code CLI subprocess (uses user's subscription)
#   - "anthropic": Anthropic Messages API (uses anthropic_api_key)
#   - "gemini"   : Google Gemini generateContent API (uses gemini_api_key)
#   - "openai"   : OpenAI chat completions API (uses openai_api_key)
#   - "ollama"   : local Ollama server /api/chat (keyless; uses ollama_url)
#
# Skills (skill_id) are an Anthropic-only beta — requests under gemini/openai
# transparently route through the Anthropic API when a key is available.
#
# ask_claude() returns the assistant's text reply, or None on failure (a tooltip
# is shown so the user notices without a stack trace). The name is kept for
# backwards-compatibility; it now dispatches across all providers.

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from aqt.utils import tooltip

from .config import (
    PROVIDER_MODELS, PROVIDER_OF, default_model_for, family_for, load_config,
)


# ── cooperative cancellation ───────────────────────────────────────────────────
#
# The progress runner (core/qt_utils.run_claude_*) sets a threading.Event on the
# worker thread before calling into ask_claude. The CLI path polls it and kills
# the subprocess when it trips. Stored thread-local so concurrent calls on
# different threads can't cancel each other.
_tls = threading.local()


def set_cancel_event(ev) -> None:
    _tls.cancel = ev


def _current_cancel_event():
    return getattr(_tls, "cancel", None)


API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OLLAMA_DEFAULT_URL = "http://localhost:11434"


def resolve_model(requested: str | None, family: str) -> str:
    """Pick a model ID valid for `family`. If the caller passed a model that
    belongs to a different family (e.g. a stored Claude ID while the active
    provider is Gemini), fall back to that family's configured default so we
    never send a cross-family ID to the wrong API."""
    requested = (requested or "").strip()
    if requested and requested in PROVIDER_MODELS.get(family, []):
        return requested
    if requested:
        # Custom/typed IDs: accept only if they look like they belong to the
        # family (prefix heuristic); otherwise fall back to the default.
        prefixes = {"anthropic": "claude", "gemini": "gemini", "openai": "gpt"}
        if requested.startswith(prefixes.get(family, "")):
            return requested
    return default_model_for(family)

# Common install locations for the claude binary. shutil.which often fails when
# Anki spawns subprocesses because the GUI app's PATH is minimal — these are
# checked as a fallback. Order matters: user-local first, then system.
# Windows entries use forward slashes; os.path.expanduser handles ~ on all OSes.
_IS_WIN = sys.platform.startswith("win")

CLI_FALLBACK_PATHS_POSIX = [
    "~/.claude/local/claude",
    "~/.claude/local/bin/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.volta/bin/claude",
    "~/.bun/bin/claude",
    "~/n/bin/claude",
]

CLI_FALLBACK_PATHS_WIN = [
    r"~\.claude\local\claude.exe",
    r"~\.claude\local\claude.cmd",
    r"~\AppData\Roaming\npm\claude.cmd",
    r"~\AppData\Roaming\npm\claude.exe",
    r"~\AppData\Local\Programs\claude\claude.exe",
    r"~\scoop\shims\claude.cmd",
    r"~\scoop\shims\claude.exe",
    r"C:\Program Files\nodejs\claude.cmd",
    r"C:\Program Files (x86)\nodejs\claude.cmd",
]

CLI_FALLBACK_PATHS = CLI_FALLBACK_PATHS_WIN if _IS_WIN else CLI_FALLBACK_PATHS_POSIX
CLI_EXEC_NAMES = ["claude.cmd", "claude.exe", "claude"] if _IS_WIN else ["claude"]


class ClaudeError(Exception):
    pass


class ClaudeCancelled(ClaudeError):
    """Raised when the user cancels an in-flight call via the progress dialog."""
    pass


# ── small text helpers ─────────────────────────────────────────────────────────

def strip_fences(text: str) -> str:
    """Drop ```lang ... ``` wrappers if the model added them."""
    if not text:
        return ""
    fence = re.search(r"```(?:json|JSON)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text.strip()


def _balanced_spans(text: str, open_ch: str, close_ch: str) -> list[str]:
    """Return every top-level balanced open_ch…close_ch span in `text`, ignoring
    brackets that appear inside JSON strings. Lets us pull a real JSON array/
    object out of prose that also contains stray brackets."""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
    return out


def extract_json(raw: str):
    """Best-effort parse of a JSON value from messy LLM output (fences, prose,
    trailing commas, stray brackets). Returns the parsed object, or raises
    ValueError if nothing parseable is found."""
    if not raw or not raw.strip():
        raise ValueError("the reply was empty")
    cleaned = strip_fences(raw).strip()
    # Candidates, longest balanced spans first so we prefer the full structure
    # over a nested fragment; the whole string is tried first for the clean case.
    candidates = [cleaned]
    spans: list[str] = []
    for o, c in (("[", "]"), ("{", "}")):
        spans.extend(_balanced_spans(cleaned, o, c))
    candidates.extend(sorted(spans, key=len, reverse=True))
    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([\]}])", r"\1", cand)):  # 2nd: drop trailing commas
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError("couldn't find valid JSON in the reply")


def _extract_text(body: dict) -> str:
    """Concatenate every text content block in the response — skills produce
    multi-block responses, so the naive content[0].text path is wrong."""
    blocks = body.get("content", []) or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


# ── CLI detection ─────────────────────────────────────────────────────────────

def _usable(path: str) -> bool:
    if not path:
        return False
    if not (os.path.isfile(path) or os.path.islink(path)):
        return False
    # Windows .cmd/.bat files aren't marked executable by the filesystem;
    # os.access(X_OK) lies on Windows anyway, so skip the access check there.
    if _IS_WIN:
        return True
    return os.access(path, os.X_OK)


def detect_cli_path(override: str = "") -> str:
    """Return a usable path to the claude binary, or '' if not found."""
    override = (override or "").strip()
    if override and _usable(override):
        return override
    for name in CLI_EXEC_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for p in CLI_FALLBACK_PATHS:
        expanded = os.path.expanduser(p)
        if _usable(expanded):
            return expanded
    return ""


# ── CLI provider ──────────────────────────────────────────────────────────────

def _call_cli(cli_path: str, model: str, system: str, user: str,
              extra_args: list[str] | None = None, timeout: int = 180,
              attachments: list[str] | None = None,
              skill_invocation: str = "") -> str:
    if not cli_path:
        cli_path = detect_cli_path()
    if not cli_path:
        raise ClaudeError(
            "Claude Code CLI not found. Install Claude Code or set the path "
            "explicitly in Ankisstant Settings."
        )

    # Attachments need the Read tool; skill invocations are pure prompt
    # prefixes, no tool required (the CLI auto-loads skill bodies via
    # description matching from ~/.claude/skills/).
    tools_arg = "Read" if attachments else ""
    args = [
        cli_path, "-p",
        "--output-format", "text",
        "--tools", tools_arg,
        "--no-session-persistence",   # don't pollute resume history
        "--model", model,
    ]
    # Slash commands need to remain enabled when the user has configured
    # one (e.g. /malleus-anki). For pure-text completion we still disable
    # them so stray user input can't trigger unrelated commands.
    if not (skill_invocation or "").lstrip().startswith("/"):
        args.append("--disable-slash-commands")

    if attachments:
        # `--add-dir` is repeated per directory; the CLI rejects Read paths
        # outside its known dirs in sandboxed setups.
        added_dirs: set[str] = set()
        for p in attachments:
            d = os.path.dirname(os.path.abspath(p))
            if d and d not in added_dirs:
                args.extend(["--add-dir", d])
                added_dirs.add(d)
        files_preamble = (
            "Use the Read tool to open these files; their contents are the SOURCE "
            "for the task below:\n"
            + "\n".join(f"- {os.path.abspath(p)}" for p in attachments)
            + "\n\n"
        )
        user = files_preamble + user
        timeout = max(timeout, 300)  # Read on large PDFs takes longer

    if skill_invocation:
        # Prepend the user's invocation string verbatim — could be a slash
        # command (/malleus-anki) or English ("Use the malleus-anki skill"),
        # depending on what they typed in the profile.
        user = skill_invocation.strip() + "\n\n" + user

    if system:
        args.extend(["--system-prompt", system])
    if extra_args:
        args.extend(list(extra_args))

    # Popen (not subprocess.run) so a cancel event can kill the process
    # mid-flight. communicate() runs in a nested thread; the outer loop polls
    # both the cancel event and the timeout.
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as e:
        raise ClaudeError(f"Could not execute CLI: {e}")

    ev = _current_cancel_event()
    holder: dict = {}

    def _communicate():
        try:
            holder["out"], holder["err"] = proc.communicate(input=user)
        except Exception as e:
            holder["exc"] = e

    worker = threading.Thread(target=_communicate, daemon=True)
    worker.start()

    deadline = time.monotonic() + timeout
    while worker.is_alive():
        if ev is not None and ev.is_set():
            proc.kill()
            worker.join(timeout=2)
            raise ClaudeCancelled("Cancelled by user.")
        if time.monotonic() > deadline:
            proc.kill()
            worker.join(timeout=2)
            raise ClaudeError(f"Claude CLI timed out after {timeout}s.")
        worker.join(timeout=0.1)

    if "exc" in holder:
        raise ClaudeError(f"Claude CLI failed: {holder['exc']}")
    if proc.returncode != 0:
        err = (holder.get("err") or holder.get("out") or "").strip()
        raise ClaudeError(f"Claude CLI exited {proc.returncode}: {err[:800]}")

    return (holder.get("out") or "").strip()


# ── API provider ──────────────────────────────────────────────────────────────

_MIME_BY_EXT = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _call_api(api_key: str, model: str, system: str, user: str,
              max_tokens: int, temperature: float = 0.4,
              skill_id: str = "",
              attachments: list[str] | None = None) -> str:
    if not api_key:
        raise ClaudeError(
            "No Anthropic API key set. Open Ankisstant Settings to paste a key "
            "or switch to CLI mode."
        )
    headers = {
        "x-api-key":         api_key,
        "anthropic-version": API_VERSION,
        "content-type":      "application/json",
    }
    if attachments:
        content: list[dict] = []
        for path in attachments:
            ext = os.path.splitext(path)[1].lower()
            media_type = _MIME_BY_EXT.get(ext)
            if media_type is None:
                raise ClaudeError(
                    f"Can't send '{os.path.basename(path)}' to the API — only "
                    "PDF and image files are supported as document blocks."
                )
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError as e:
                raise ClaudeError(f"Couldn't read attachment {path}: {e}")
            block_type = "document" if media_type == "application/pdf" else "image"
            content.append({
                "type": block_type,
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(blob).decode("ascii"),
                },
            })
        content.append({"type": "text", "text": user})
        user_content: object = content
    else:
        user_content = user
    body: dict = {
        "model":       model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages":    [{"role": "user", "content": user_content}],
    }
    if system:
        body["system"] = system

    timeout = 120
    if skill_id:
        # Skills require the code-execution tool + two beta headers + a
        # container block. See docs: code-execution-2025-08-25 + skills-2025-10-02
        headers["anthropic-beta"] = "code-execution-2025-08-25,skills-2025-10-02"
        body["max_tokens"] = max(max_tokens, 4096)
        body["container"] = {
            "skills": [{"type": "custom", "skill_id": skill_id, "version": "latest"}]
        }
        body["tools"] = [{"type": "code_execution_20250825", "name": "code_execution"}]
        timeout = 180  # skills can read files / run code, so slower

    ev = _current_cancel_event()
    if ev is not None and ev.is_set():
        raise ClaudeCancelled("Cancelled by user.")
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if ev is not None and ev.is_set():
            raise ClaudeCancelled("Cancelled by user.")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise ClaudeError(f"Anthropic API {e.code}: {detail[:500]}")
    except urllib.error.URLError as e:
        raise ClaudeError(f"Network error: {e.reason}")
    return _extract_text(data)


def _read_attachment(path: str) -> tuple[str, bytes]:
    """Return (media_type, raw_bytes) for an attachment, or raise ClaudeError."""
    ext = os.path.splitext(path)[1].lower()
    media_type = _MIME_BY_EXT.get(ext)
    if media_type is None:
        raise ClaudeError(
            f"Can't send '{os.path.basename(path)}' — only PDF and image files "
            "are supported as attachments."
        )
    try:
        with open(path, "rb") as fh:
            return media_type, fh.read()
    except OSError as e:
        raise ClaudeError(f"Couldn't read attachment {path}: {e}")


def _http_post_json(url: str, headers: dict, body: dict, timeout: int,
                    provider_label: str) -> dict:
    """Shared POST helper: JSON in, JSON out, with cancel-event checks and
    consistent ClaudeError wrapping."""
    ev = _current_cancel_event()
    if ev is not None and ev.is_set():
        raise ClaudeCancelled("Cancelled by user.")
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if ev is not None and ev.is_set():
            raise ClaudeCancelled("Cancelled by user.")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        # A 429 with "limit: 0" / free-tier-token wording means the chosen model
        # has *no* free-tier quota at all (e.g. Gemini 2.0 Flash, 2.5 Pro) — a
        # raw dump of Google's JSON is useless to the user, so translate it into
        # the one action that fixes it.
        low = detail.lower()
        if e.code == 429 and ("limit: 0" in low or "free_tier" in low or "free tier" in low):
            raise ClaudeError(
                f"{provider_label} rejected this model: your key's free tier has "
                "no quota for it. Open Ankisstant Settings → AI and switch the "
                "model to Gemini 2.5 Flash (the only free-tier Gemini model), or "
                "add billing to your Google AI Studio account for Pro/2.0."
            )
        if e.code == 429:
            raise ClaudeError(
                f"{provider_label} rate limit hit (429). Wait a minute and retry, "
                "or switch to a lighter model in Settings → AI. [{}]".format(detail[:200])
            )
        raise ClaudeError(f"{provider_label} API {e.code}: {detail[:500]}")
    except urllib.error.URLError as e:
        raise ClaudeError(f"Network error: {e.reason}")
    return data


def _call_gemini(api_key: str, model: str, system: str, user: str,
                 max_tokens: int, temperature: float = 0.4,
                 attachments: list[str] | None = None) -> str:
    if not api_key:
        raise ClaudeError(
            "No Gemini API key set. Open Ankisstant Settings → AI to paste a key "
            "(free keys: aistudio.google.com/apikey) or pick another provider."
        )
    parts: list[dict] = []
    for path in attachments or []:
        media_type, blob = _read_attachment(path)
        parts.append({"inline_data": {
            "mime_type": media_type,
            "data": base64.b64encode(blob).decode("ascii"),
        }})
    parts.append({"text": user})
    gen_cfg: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
    # Gemini 2.5 Flash/Flash-Lite are "thinking" models: by default they spend
    # output tokens on internal reasoning, which counts against maxOutputTokens
    # and can leave zero budget for the actual reply. These tools want terse
    # answers, so disable thinking on Flash variants. (2.5 Pro can't disable
    # thinking — leave it untouched.)
    if "flash" in model.lower():
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    body: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
    }
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    url = GEMINI_URL.format(model=model)
    headers = {"content-type": "application/json", "x-goog-api-key": api_key}
    data = _http_post_json(url, headers, body, timeout=180, provider_label="Gemini")
    cands = data.get("candidates") or []
    if not cands:
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        raise ClaudeError(f"Gemini returned no candidates{f' ({fb})' if fb else ''}.")
    blocks = (cands[0].get("content") or {}).get("parts") or []
    text = "\n".join(b.get("text", "") for b in blocks if b.get("text")).strip()
    if not text:
        reason = cands[0].get("finishReason") or "unknown"
        hint = (" — the model hit its token limit while thinking; raise "
                "maxOutputTokens or use a non-thinking model"
                if reason == "MAX_TOKENS" else "")
        raise ClaudeError(f"Gemini returned empty text (finishReason: {reason}){hint}.")
    return text


def _call_openai(api_key: str, model: str, system: str, user: str,
                 max_tokens: int, temperature: float = 0.4,
                 attachments: list[str] | None = None) -> str:
    if not api_key:
        raise ClaudeError(
            "No OpenAI API key set. Open Ankisstant Settings → AI to paste a key "
            "(platform.openai.com/api-keys) or pick another provider."
        )
    if attachments:
        content: list[dict] = []
        for path in attachments:
            media_type, blob = _read_attachment(path)
            if media_type == "application/pdf":
                raise ClaudeError(
                    "OpenAI chat completions can't take PDF attachments — switch "
                    "to Anthropic or Gemini for PDF-based tasks."
                )
            b64 = base64.b64encode(blob).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })
        content.append({"type": "text", "text": user})
        user_content: object = content
    else:
        user_content = user
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user_content})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
    data = _http_post_json(OPENAI_URL, headers, body, timeout=180, provider_label="OpenAI")
    choices = data.get("choices") or []
    if not choices:
        raise ClaudeError("OpenAI returned no choices.")
    return (choices[0].get("message") or {}).get("content", "").strip()


def _call_ollama(base_url: str, model: str, system: str, user: str,
                 max_tokens: int, temperature: float = 0.4,
                 attachments: list[str] | None = None) -> str:
    """Call a local Ollama server's /api/chat endpoint. Keyless. Images are
    passed as base64 in the message's `images` array; PDFs aren't supported by
    Ollama's vision models, so they're rejected with a clear message."""
    base_url = (base_url or OLLAMA_DEFAULT_URL).strip().rstrip("/")
    images: list[str] = []
    for path in attachments or []:
        media_type, blob = _read_attachment(path)
        if media_type == "application/pdf":
            raise ClaudeError(
                "Ollama can't take PDF attachments — switch to Anthropic or "
                "Gemini for PDF-based tasks."
            )
        images.append(base64.b64encode(blob).decode("ascii"))
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    user_msg: dict = {"role": "user", "content": user}
    if images:
        user_msg["images"] = images
    messages.append(user_msg)
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    url = f"{base_url}/api/chat"
    headers = {"content-type": "application/json"}
    # Local model generation can be slow on CPU — give it a generous timeout.
    try:
        data = _http_post_json(url, headers, body, timeout=300, provider_label="Ollama")
    except ClaudeError as e:
        # Connection-refused is the most common Ollama failure (server not
        # running) — make that actionable rather than a raw socket error.
        if "refused" in str(e).lower() or "urlopen" in str(e).lower():
            raise ClaudeError(
                f"Couldn't reach Ollama at {base_url}. Is the Ollama app/server "
                f"running? (Start it, or run `ollama serve`.) [{e}]"
            )
        raise
    text = ((data.get("message") or {}).get("content") or "").strip()
    if not text:
        raise ClaudeError(
            f"Ollama returned no text. Is the model '{model}' pulled? "
            f"(Run `ollama pull {model}`.)"
        )
    return text


# ── Public entry point ───────────────────────────────────────────────────────

def ask_claude(
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1000,
    model: str | None = None,
    skill_id: str = "",
    skill_invocation: str = "",
    show_errors: bool = True,
    attachments: list[tuple[bytes, str]] | None = None,
) -> str | None:
    """Single shared entry point. Returns the text reply, or None on failure.
    A tooltip is shown on failure so the user notices (unless show_errors=False).

    `attachments` is a list of absolute file paths. On the CLI path the Read
    tool is enabled and the paths are referenced in the prompt; on the API path
    the files are base64-encoded as document/image blocks.

    `skill_id` invokes an Anthropic API custom skill server-side (API path only).
    `skill_invocation` is a free-text prompt prefix to prepend on the CLI path —
    e.g. '/malleus-anki' (slash command) or 'Use the malleus-anki skill'
    (English). The skill body itself lives in ~/.claude/skills/<name>/SKILL.md
    and is loaded by Claude Code on demand via description matching."""
    cfg = load_config()
    provider = (cfg.get("provider") or "auto").lower()
    family = family_for(provider)
    # Skills are an Anthropic-only feature (code-execution beta). If a skill is
    # requested under a non-Anthropic provider, route through the Anthropic API
    # instead (when a key exists), since the other providers can't honour it.
    if skill_id and family != "anthropic":
        provider, family = "anthropic", "anthropic"
    model = resolve_model(model, family)
    system_str = system or ""

    cli_path = ""
    if provider in ("auto", "cli"):
        cli_path = detect_cli_path(cfg.get("claude_cli_path", ""))
    use_cli = bool(cli_path) and provider in ("auto", "cli") and not skill_id
    # Skills need the Anthropic API path — CLI doesn't carry the beta headers.
    use_api = bool(cfg.get("anthropic_api_key")) and provider in ("auto", "anthropic")
    any_provider_configured = use_cli or use_api or (
        provider == "gemini" and bool(cfg.get("gemini_api_key"))
    ) or (
        provider == "openai" and bool(cfg.get("openai_api_key"))
    ) or (
        # Ollama is keyless — having selected it is enough; reachability is
        # checked at call time with a friendly error if the server is down.
        provider == "ollama"
    ) or (
        # Manual needs no provider at all — the user supplies the reply.
        provider == "manual"
    )

    # Manual provider: no model is called. Show the assembled prompt for the
    # user to run in their own external AI and return their pasted reply. This
    # runs on the main thread (run_claude_* routes manual here synchronously).
    if provider == "manual":
        from .qt_utils import manual_ai_complete
        return manual_ai_complete(prompt, system_str, attachments=attachments)

    try:
        if use_cli and not skill_id:
            return _call_cli(
                cli_path=cli_path,
                model=model,
                system=system_str,
                user=prompt,
                extra_args=cfg.get("claude_cli_extra_args") or [],
                attachments=attachments,
                skill_invocation=skill_invocation,
            )
        if provider == "gemini" and not skill_id:
            return _call_gemini(
                api_key=cfg.get("gemini_api_key", ""),
                model=model, system=system_str, user=prompt,
                max_tokens=max_tokens, attachments=attachments,
            )
        if provider == "openai" and not skill_id:
            return _call_openai(
                api_key=cfg.get("openai_api_key", ""),
                model=model, system=system_str, user=prompt,
                max_tokens=max_tokens, attachments=attachments,
            )
        if provider == "ollama" and not skill_id:
            return _call_ollama(
                base_url=cfg.get("ollama_url", "") or OLLAMA_DEFAULT_URL,
                model=model, system=system_str, user=prompt,
                max_tokens=max_tokens, attachments=attachments,
            )
        if use_api or skill_id or attachments:
            return _call_api(
                api_key=cfg.get("anthropic_api_key", ""),
                model=resolve_model(model, "anthropic"),
                system=system_str,
                user=prompt,
                max_tokens=max_tokens,
                skill_id=skill_id,
                attachments=attachments,
            )
        raise ClaudeError(
            "No AI provider available. Open Ankisstant Settings → AI — pick a "
            "provider and paste its API key, or install the Claude Code CLI."
        )
    except ClaudeCancelled:
        # User-initiated — no error UI, just a quiet None.
        return None
    except ClaudeError as e:
        from . import log as _log
        _log.error(f"ask_claude failed: {e}")
        if show_errors:
            _surface_claude_error(str(e), use_cli=use_cli, use_api=any_provider_configured)
        return None
    except Exception as e:
        # Catch-all safety net. ask_claude runs on a background thread; an
        # exception type we didn't anticipate (JSON decode, attribute error,
        # an SDK quirk on any provider) would otherwise surface uncaught in the
        # taskman done-callback on the main thread and can hard-crash Anki on
        # macOS. Degrade to a logged error + tooltip instead — same as a
        # ClaudeError. This is provider-agnostic by design: it protects the
        # Gemini path and every other path equally.
        from . import log as _log
        _log.error(f"ask_claude crashed unexpectedly: {e!r}")
        if show_errors:
            _surface_claude_error(
                f"Unexpected error talking to the AI provider: {e}. "
                "See the console for the full traceback.",
                use_cli=use_cli, use_api=any_provider_configured,
            )
        return None
    except Exception as e:
        from . import log as _log
        _log.error(f"ask_claude unexpected error: {e}")
        if show_errors:
            tooltip(f"Claude error: {e}", period=4500)
        return None


def _surface_claude_error(msg: str, *, use_cli: bool, use_api: bool) -> None:
    """If neither provider is configured, prompt the user to open the welcome
    wizard. Otherwise show the error as a tooltip so it doesn't block input."""
    if not use_cli and not use_api:
        try:
            from aqt.utils import showWarning
            from aqt.qt import QMessageBox
            from ..ui.welcome import open_welcome
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("AI provider not set up")
            box.setText(
                "Ankisstant needs an AI provider configured first — install the "
                "Claude Code CLI, or open Settings → AI and paste an API key for "
                "Anthropic, Gemini, or OpenAI."
            )
            box.setInformativeText(msg)
            setup_btn = box.addButton("Open setup…", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is setup_btn:
                open_welcome()
            return
        except Exception:
            pass
    tooltip(f"AI error: {msg}", period=4500)


def ask_claude_json(
    prompt: str,
    system: str | None = None,
    max_tokens: int = 1000,
    model: str | None = None,
    skill_id: str = "",
    skill_invocation: str = "",
    show_errors: bool = True,
    attachments: list[tuple[bytes, str]] | None = None,
):
    """Convenience: ask_claude + parse JSON. Returns None on failure.
    Tries strict json.loads first, then a fenced extract, then a regex fallback
    that finds the first {...} or [...] block."""
    # Manual provider: collect AND validate the pasted reply inside the dialog,
    # so a bad/partial paste can be fixed without redoing the whole round-trip.
    if (load_config().get("provider") or "auto").lower() == "manual":
        from .qt_utils import manual_ai_complete
        return manual_ai_complete(prompt, system or "",
                                  attachments=attachments, parse=extract_json)

    raw = ask_claude(prompt, system=system, max_tokens=max_tokens, model=model,
                     skill_id=skill_id, skill_invocation=skill_invocation,
                     show_errors=show_errors, attachments=attachments)
    if raw is None:
        return None
    try:
        return extract_json(raw)
    except ValueError:
        if show_errors:
            tooltip("AI returned unparseable JSON (see console).", period=4500)
        print(f"[ankisstant] couldn't parse JSON: {raw[:300]}")
        return None
