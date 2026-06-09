# Bundled notetypes for Ankisstant.
#
# Ships a self-contained Malleus Q&A notetype (Front / Back / Source) and seeds a
# matching AI-Create profile so question/answer card generation works out of the
# box. Both helpers are idempotent and fail-open — safe to call on every profile
# load.

from __future__ import annotations

from aqt import mw

from ..core.config import load_config, save_config
from ..core import log

QA_NOTETYPE_NAME = "Ankisstant — Malleus Q&A"

_QA_FRONT = "{{Front}}"
_QA_BACK = (
    "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}\n\n"
    "{{#Source}}<div style=\"margin-top:14px;font-size:12px;color:#888\">"
    "{{Source}}</div>{{/Source}}"
)
_QA_CSS = (
    ".card { font-family: -apple-system, 'Segoe UI', sans-serif; font-size: 20px;"
    " color: #2c2c2c; background: #fff; text-align: center; }\n"
    "#answer { margin: 14px 0; }\n"
    "b { color: #1a2440; }"
)


def ensure_qa_notetype() -> str | None:
    """Create the Malleus Q&A notetype if it doesn't already exist. Returns the
    notetype name, or None if the collection isn't ready."""
    try:
        if mw.col is None:
            return None
        mm = mw.col.models
        existing = mm.by_name(QA_NOTETYPE_NAME)
        if existing is not None:
            return QA_NOTETYPE_NAME
        m = mm.new(QA_NOTETYPE_NAME)
        for fname in ("Front", "Back", "Source"):
            mm.add_field(m, mm.new_field(fname))
        t = mm.new_template("Q&A")
        t["qfmt"] = _QA_FRONT
        t["afmt"] = _QA_BACK
        mm.add_template(m, t)
        m["css"] = _QA_CSS
        mm.add(m)
        log.info(f"created bundled notetype: {QA_NOTETYPE_NAME}")
        return QA_NOTETYPE_NAME
    except Exception as e:
        log.error(f"ensure_qa_notetype failed: {e}")
        return None


def ensure_qa_profile() -> None:
    """Seed an AI-Create notetype profile for the Malleus Q&A notetype (prompt
    mode + Q&A format + quality pass forced off) if one isn't present."""
    try:
        cfg = load_config()
        cc = cfg.setdefault("tools", {}).setdefault("card_creator", {})
        profiles = cc.setdefault("notetypes", [])
        if any(isinstance(p, dict) and p.get("name") == QA_NOTETYPE_NAME
               for p in profiles):
            return
        profiles.append({
            "name": QA_NOTETYPE_NAME,
            "front_field": "Front",
            "extra_field": "Back",       # the answer lands here
            "image_field": "Back",
            "sources_field": "Source",
            "one_by_one_field": "",
            "card_format": "qa",
            # Prompt mode: QA_GEN_SYSTEM carries the Malleus Q&A rules. We do NOT
            # bind the malleus-anki skill here — that skill emits FRONT:/BACK:
            # prose, not the {front, extra} JSON the Create engine parses.
            "card_creation_mode": "prompt",
            "card_creation_skill_invocation": "",
            "card_creation_skill_id": "",
            "quality_pass_override": "off",   # cloze rubric doesn't fit Q&A
            "extra_instructions": (
                "Malleus Clinical Medicine submission style: clinically relevant, "
                "Australian guidelines and spelling, one acceptable source in the "
                "Source field with a URL. Repeat nothing verbatim from sources."
            ),
        })
        save_config(cfg)
        log.info(f"seeded AI-Create profile: {QA_NOTETYPE_NAME}")
    except Exception as e:
        log.error(f"ensure_qa_profile failed: {e}")


def ensure_bundled() -> None:
    """Install the bundled notetype + its Create profile. Idempotent."""
    if ensure_qa_notetype():
        ensure_qa_profile()
