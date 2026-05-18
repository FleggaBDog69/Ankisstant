# Single Claude client used by every tool. No HTTP / subprocess elsewhere.
#
# Three provider modes (configured globally):
#   - "auto": prefer local Claude Code CLI when present, fall back to API
#   - "cli" : Claude Code CLI subprocess (uses user's subscription)
#   - "api" : Anthropic Messages API (uses anthropic_api_key)
#
# ask_claude() returns the assistant's text reply, or None on failure (a tooltip
# is shown so the user notices without a stack trace).

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from aqt.utils import tooltip

from .config import load_config


API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

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


# ── small text helpers ─────────────────────────────────────────────────────────

def strip_fences(text: str) -> str:
    """Drop ```lang ... ``` wrappers if the model added them."""
    if not text:
        return ""
    fence = re.search(r"```(?:json|JSON)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text.strip()


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

    try:
        proc = subprocess.run(
            args, input=user, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClaudeError(f"Could not execute CLI: {e}")
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"Claude CLI timed out after {timeout}s.")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ClaudeError(f"Claude CLI exited {proc.returncode}: {err[:800]}")

    return (proc.stdout or "").strip()


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

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise ClaudeError(f"Anthropic API {e.code}: {detail[:500]}")
    except urllib.error.URLError as e:
        raise ClaudeError(f"Network error: {e.reason}")
    return _extract_text(data)


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
    mode = (cfg.get("provider_mode") or "auto").lower()
    model = model or cfg.get("model_default") or "claude-sonnet-4-6"
    system_str = system or ""

    cli_path = ""
    if mode in ("auto", "cli"):
        cli_path = detect_cli_path(cfg.get("claude_cli_path", ""))
    use_cli = bool(cli_path) and mode in ("auto", "cli") and not skill_id
    # Skills need the API path — CLI doesn't carry the beta headers.
    use_api = bool(cfg.get("anthropic_api_key")) and mode in ("auto", "api")

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
        if use_api or skill_id or attachments:
            return _call_api(
                api_key=cfg.get("anthropic_api_key", ""),
                model=model,
                system=system_str,
                user=prompt,
                max_tokens=max_tokens,
                skill_id=skill_id,
                attachments=attachments,
            )
        raise ClaudeError(
            "No Claude backend available. Open Ankisstant Settings — paste an "
            "API key, or install the Claude Code CLI."
        )
    except ClaudeError as e:
        from . import log as _log
        _log.error(f"ask_claude failed: {e}")
        if show_errors:
            _surface_claude_error(str(e), use_cli=use_cli, use_api=use_api)
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
            box.setWindowTitle("Claude not set up")
            box.setText(
                "Ankisstant needs either the Claude Code CLI installed, or an "
                "Anthropic API key in settings, before it can talk to Claude."
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
    tooltip(f"Claude error: {msg}", period=4500)


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
    raw = ask_claude(prompt, system=system, max_tokens=max_tokens, model=model,
                     skill_id=skill_id, skill_invocation=skill_invocation,
                     show_errors=show_errors, attachments=attachments)
    if raw is None:
        return None
    cleaned = strip_fences(raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallbacks for when the model wraps JSON in prose.
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    if show_errors:
        tooltip(f"Claude returned unparseable JSON (see console).", period=4500)
    print(f"[ankisstant] couldn't parse JSON: {raw[:300]}")
    return None
