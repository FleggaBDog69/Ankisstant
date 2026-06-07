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


CLASSIFY_SYSTEM = (
    "You extract a hierarchical Anki tag path from clinical material.\n\n"
    "Given a topic, concept, or source text, return a JSON object: "
    '{"system": "...", "subsystem": "...", "topic": "..."}.\n\n'
    "RULES:\n"
    "- system: top-level body system or domain — e.g. Cardio, Neuro, Endo, GI, "
    "Resp, Renal, Heme, MSK, Derm, Repro, Psych, ID, Onc, Pharm, Stats, "
    "Genetics, Biochem, Immuno. Pick the single best fit.\n"
    "- subsystem: more specific anatomy / disease category within the system — "
    "e.g. Arrhythmias for Cardio, Stroke for Neuro, Diabetes for Endo.\n"
    "- topic: the most specific clinical entity, drug, sign, or mechanism — "
    "e.g. AFib, MCA_stroke, Digoxin, McDonald_criteria.\n"
    "- Use PascalCase or snake_case (no spaces, no '::', no slashes).\n"
    "- Avoid generic placeholders ('General', 'Misc', 'Other'). If a level is "
    "genuinely unclear, return an empty string for that level — the caller "
    "will skip it.\n\n"
    "Return ONLY the JSON object. No prose, no markdown fences."
)


def classify(material: str, *, model: str | None = None) -> dict | None:
    """Dedicated classification call: turn free material into {system, subsystem,
    topic} levels. Returns None on failure. Qt-free — safe off the main thread."""
    material = (material or "").strip()
    if not material:
        return None
    try:
        resp = core_api.ask_claude_json(
            prompt=f"Material to classify:\n{material[:1500]}",
            system=CLASSIFY_SYSTEM, max_tokens=256, model=model,
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
