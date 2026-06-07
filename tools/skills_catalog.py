# Skill catalog — the ONE place that knows about Ankisstant's bundled skills and
# how to install them to the user's Claude Code skills folder. Qt-free and
# Anki-free so the UI, the background worker, and the dispatch layer can all import
# it. Bundled skill folders live at <addon>/skills/<name>/SKILL.md; installing a
# skill copies that folder to ~/.claude/skills/<name>/ so the Claude CLI auto-loads
# it, while the SKILL.md body is also available here as portable instructions for
# providers that have no skill mechanism (inline fallback).

from __future__ import annotations

import os
import re
import shutil

# <addon>/skills (this file is <addon>/tools/skills_catalog.py)
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLED_DIR = os.path.join(_ADDON_DIR, "skills")


def _claude_skills_dir() -> str:
    """The user's Claude Code skills folder (~/.claude/skills)."""
    return os.path.join(os.path.expanduser("~"), ".claude", "skills")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Frontmatter is the leading --- … --- YAML
    block; we only need name/description, so parse those two keys leniently."""
    fm: dict = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        block, body = m.group(1), m.group(2)
        for key in ("name", "description"):
            km = re.search(rf"^{key}\s*:\s*(.+)$", block, re.MULTILINE)
            if km:
                fm[key] = km.group(1).strip()
    return fm, body


def _read_skill_md(path: str) -> tuple[dict, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return _split_frontmatter(fh.read())
    except Exception:
        return {}, ""


# Display labels & the AI tool each bundled skill maps to. The folder name (and the
# SKILL.md `name:`) is the canonical id used for CLI invocation and install.
_LABELS = {
    "anki-cards":       {"label": "Create — cloze cards",   "tool": "card_creation"},
    "anki-card-scorer": {"label": "Grade — quality scorer", "tool": "quality_pass"},
    "anki-browse":      {"label": "Browse — search & tags", "tool": "search"},
}


def catalog() -> list[dict]:
    """Every skill bundled with the addon, with its metadata and install status.
    Each entry: {name, label, description, tool, bundled_path, installed,
    cli_invocation}."""
    out: list[dict] = []
    if not os.path.isdir(_BUNDLED_DIR):
        return out
    for name in sorted(os.listdir(_BUNDLED_DIR)):
        skill_md = os.path.join(_BUNDLED_DIR, name, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        fm, _body = _read_skill_md(skill_md)
        meta = _LABELS.get(name, {})
        out.append({
            "name": name,
            "label": meta.get("label", name),
            "description": fm.get("description", ""),
            "tool": meta.get("tool", ""),
            "bundled_path": os.path.join(_BUNDLED_DIR, name),
            "installed": is_installed(name),
            "cli_invocation": f"Use the {name} skill",
        })
    return out


def catalog_entry(name: str) -> dict | None:
    for entry in catalog():
        if entry["name"] == name:
            return entry
    return None


def is_installed(name: str) -> bool:
    """True if the skill is present in the user's Claude skills folder."""
    return os.path.isfile(os.path.join(_claude_skills_dir(), name, "SKILL.md"))


def install_skill(name: str) -> tuple[bool, str]:
    """Copy the bundled skill folder to ~/.claude/skills/<name>/. Returns
    (ok, message). Overwrites an existing install (refresh-safe)."""
    src = os.path.join(_BUNDLED_DIR, name)
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        return False, f"No bundled skill named {name!r}."
    dst = os.path.join(_claude_skills_dir(), name)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return True, f"Installed {name} to {dst}."
    except Exception as e:
        return False, f"Couldn't install {name}: {e}"


def uninstall_skill(name: str) -> tuple[bool, str]:
    """Remove the skill from ~/.claude/skills/. Leaves the bundled copy intact."""
    dst = os.path.join(_claude_skills_dir(), name)
    if not os.path.isdir(dst):
        return True, f"{name} was not installed."
    try:
        shutil.rmtree(dst)
        return True, f"Removed {name}."
    except Exception as e:
        return False, f"Couldn't remove {name}: {e}"


def claude_project_instructions() -> str:
    """The bundled provider-neutral Ankisstant instructions for Claude.ai users to
    paste into their own Claude Project (free web Claude has no shareable assistant
    link). Returns '' if the file is missing."""
    path = os.path.join(_ADDON_DIR, "assistants", "claude-project-instructions.md")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def portable_instructions(name: str) -> str:
    """The SKILL.md body (frontmatter stripped) for `name` — used to inline a
    skill's guidance on providers that have no native skill mechanism. Reads the
    installed copy if present, else the bundled copy. Returns '' if unknown."""
    for base in (_claude_skills_dir(), _BUNDLED_DIR):
        path = os.path.join(base, name, "SKILL.md")
        if os.path.isfile(path):
            _fm, body = _read_skill_md(path)
            return body.strip()
    return ""
