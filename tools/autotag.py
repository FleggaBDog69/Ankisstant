# Shared auto-tag system — ONE place for the hierarchical {base}::{type}::{system}
# ::{subsystem}::{topic} scheme used by BOTH AI Create and AI Browse. Each panel
# still calls it in its own flow (Create folds the tag into the card-gen reply
# when it can; Browse folds it into the search-terms reply), but the on/off
# toggle, the classification prompt, and the tag formatting all live here so the
# two panels can't drift apart again.
#
# Two ways to get tag levels:
#   1. FOLDED — the panel's existing engine.plan_for/route already returns
#      tag_levels in the same round-trip (no skill forcing a bare reply). Cheap.
#   2. SEPARATE — classify() does a small dedicated call. Used when a skill forces
#      a bare array (Create's skill notetypes) so folding isn't possible, and as a
#      general fallback. Qt-free, so the background worker can call it off-thread.
#
# Either way the levels go through tag_from_levels() for the final string.

from ..core import api as core_api
from ..core.config import (
    auto_tag_base, format_hierarchical_tag, kg_type_info, tool_config,
)


# Per-tool default: auto-tag is ON unless the user unticks the box.
def is_enabled(tool_cfg: dict | None) -> bool:
    """Whether the auto-tag checkbox is ticked for this tool. Defaults to True."""
    return bool((tool_cfg or {}).get("auto_tag", True))


def default_type_key() -> str:
    """The KG type whose segment is used when no specific gap/KG is loaded
    (free topic/source generation or a free Browse search)."""
    try:
        return str(tool_config("knowledge_gaps").get("default_type_on_add") or "kg").lower()
    except Exception:
        return "kg"


def type_meta_for(kg_type_key: str) -> dict | None:
    """The full KG type config dict for `kg_type_key` (for its name segment)."""
    try:
        types = tool_config("knowledge_gaps").get("types") or []
    except Exception:
        return None
    key = (kg_type_key or "").lower().strip()
    for t in types:
        if isinstance(t, dict) and str(t.get("key", "")).lower() == key:
            return t
    return None


# ── Existing-tag vocabulary ──────────────────────────────────────────────
# So auto-tag snaps to the user's OWN tag tree instead of inventing a fresh
# synonym every time (Cardio vs Cardiology, AFib vs Afib). We show the model the
# system/subsystem/topic branches already living under the auto-tag base and tell
# it to reuse one when it fits. mw.col reads must happen on the main thread, so
# the branches are cached: refresh_vocab_cache() (UI thread) recomputes, and
# existing_tag_vocab() (any thread) returns the cache — safe for the background
# generation/classify workers.
_VOCAB_CACHE: str = ""
_VOCAB_MAX = 60   # cap the branch list so the prompt stays small


def refresh_vocab_cache() -> str:
    """Recompute the existing-branch list from the collection. MAIN THREAD ONLY
    (reads mw.col.tags). Returns the cached string; '' when unavailable."""
    global _VOCAB_CACHE
    try:
        from aqt import mw
        base = auto_tag_base()
        if not base or mw.col is None:
            _VOCAB_CACHE = ""
            return ""
        prefix = base.rstrip(":") + "::"
        type_names = _known_type_segments()
        branches: set[str] = set()
        for tag in mw.col.tags.all():
            if not tag.startswith(prefix):
                continue
            rest = tag[len(prefix):]
            parts = [p for p in rest.split("::") if p]
            if parts and parts[0].lower() in type_names:
                parts = parts[1:]          # drop the type segment (MQ / KG / LO)
            if parts:
                branches.add("::".join(parts[:3]))
        if not branches:
            _VOCAB_CACHE = ""
            return ""
        ordered = sorted(branches, key=str.lower)[:_VOCAB_MAX]
        _VOCAB_CACHE = "\n".join(f"- {b}" for b in ordered)
    except Exception as e:
        print(f"[ankisstant] auto-tag vocab refresh failed: {e}")
        _VOCAB_CACHE = ""
    return _VOCAB_CACHE


def _known_type_segments() -> set[str]:
    """Lower-cased KG type display-names (MQ/KG/LO/…) — stripped from a tag so the
    vocabulary is just the system/subsystem/topic levels, not the type segment."""
    out: set[str] = set()
    try:
        for t in tool_config("knowledge_gaps").get("types") or []:
            if isinstance(t, dict):
                for v in (t.get("name"), t.get("key")):
                    if v:
                        out.add(str(v).lower())
    except Exception:
        pass
    return out


def existing_tag_vocab() -> str:
    """The cached existing-branch list (see refresh_vocab_cache). Any thread."""
    return _VOCAB_CACHE


def tag_rules_text(vocab: str | None = None) -> str:
    """The shared 'how to pick system/subsystem/topic' instruction block, used by
    BOTH the dedicated classifier and the folded generation prompt so they can't
    drift. When `vocab` (existing branches) is non-empty the model is told to
    reuse an existing branch before inventing a new one. Pass '' to force no
    vocab; pass None to use the current cache."""
    if vocab is None:
        vocab = existing_tag_vocab()
    rules = (
        "Build THREE nested levels, broad to specific.\n"
        "system = the broad clinical specialty or body system — NEVER a disease "
        "(Cardio, Resp, GI, Renal, Neuro, Endo, Heme, MSK, Rheum, Derm, Repro, "
        "Psych, ID, Onc, Ophtho, ENT, Uro, OBGYN, Pharm, Micro, Anatomy, Path, "
        "Stats, Genetics, Biochem, Immuno). Pick the single best fit and ALWAYS "
        "fill it — a disease/sign/drug is never the system.\n"
        "subsystem = the disease category or organ within that system.\n"
        "topic = the most specific entity/drug/sign.\n"
        "Worked example: open-angle glaucoma -> system=Ophtho, subsystem=Glaucoma, "
        "topic=OpenAngleGlaucoma (NOT system=Glaucoma).\n"
        "Use PascalCase; no spaces, '::' or slashes; leave a level '' only when "
        "genuinely unclear (the system level should almost always be known)."
    )
    if vocab.strip():
        rules += (
            "\nReuse one of these EXISTING branches when it fits — match its exact "
            "spelling/casing — and only invent a new level when none match:\n"
            + vocab.strip()
        )
    return rules


def classify(material: str, *, model: str | None = None) -> dict | None:
    """Dedicated classification call: turn free material into {system, subsystem,
    topic} levels. Returns None on failure. Qt-free — safe off the main thread
    (reads the cached vocab, not mw.col)."""
    material = (material or "").strip()
    if not material:
        return None
    system = (
        "You extract a hierarchical Anki tag path from clinical material.\n\n"
        "Given a topic, concept, or source text, return a JSON object: "
        '{"system": "...", "subsystem": "...", "topic": "..."}.\n\n'
        + tag_rules_text()
        + "\n\nAvoid generic placeholders ('General', 'Misc', 'Other').\n"
        "Return ONLY the JSON object. No prose, no markdown fences."
    )
    try:
        resp = core_api.ask_claude_json(
            prompt=f"Material to classify:\n{material[:1500]}",
            system=system, max_tokens=256, model=model,
            show_errors=False,
        )
    except Exception as e:
        print(f"[ankisstant] auto-tag classify failed: {e}")
        return None
    return resp if isinstance(resp, dict) else None


def tag_from_levels(levels: dict | None, kg_type_key: str | None = None,
                    type_meta: dict | None = None) -> str:
    """Format {system, subsystem, topic} levels into the shared hierarchical tag
    under the configured base + the KG type's segment. Returns '' when unusable
    (no base, no levels, or it collapses to just the base). Pass either a type
    key OR the resolved type_meta dict."""
    base = auto_tag_base()
    if not base or not levels:
        return ""
    if type_meta is not None:
        type_seg = type_meta.get("name") or type_meta.get("key") or ""
    else:
        type_seg, _enabled = kg_type_info((kg_type_key or default_type_key()))
    tag = format_hierarchical_tag(base, levels, type_seg=type_seg)
    if not tag or tag == base:
        return ""
    return tag
