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


# ── Group segment ────────────────────────────────────────────────────────────
# A KG's group ("SRS 4") contributes ONE segment to its tag. This is the piece
# with a real judgement call in it, so the rule is written down and shown to the
# user live ("will tag as: SRS::04") rather than being discovered after 200 gaps
# have been tagged.

import re as _re

_TRAILING_NUM_RE = _re.compile(r"^(.*?)[\s_\-:]*(\d+)$")


def _clean_seg(text: str) -> str:
    """One tag segment, safe to sit between '::' separators."""
    return _re.sub(r"[\s/]+", "_", str(text or "")).strip("_")


def clean_segment_path(text: str) -> str:
    """Clean a typed '::'-separated segment path into something a tag can hold.
    Empty parts drop out, so 'Cardio :: :: SRS 4' -> 'Cardio::SRS_4'."""
    parts = [_clean_seg(p) for p in str(text or "").split("::")]
    return "::".join(p for p in parts if p)


def group_tag_overrides() -> dict:
    """The user's per-group segment overrides, {label: segment}. Never raises —
    a broken/absent config means "no overrides", not a dead tag."""
    try:
        raw = tool_config("knowledge_gaps").get("group_tag_overrides") or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v or "").strip() for k, v in raw.items()}


def group_segment_override(name: str) -> str:
    """The explicit segment set for this group, or '' if it uses the derived
    one. Cleaned the same way a derived segment is, so an override can't put a
    space or a stray slash into a tag."""
    raw = group_tag_overrides().get(str(name or "").strip(), "")
    return clean_segment_path(raw) if raw else ""


def group_segment(name: str) -> str:
    """The tag segment for a group: the user's override if they set one, else
    the derived rule. This is what every tag-building caller goes through."""
    return group_segment_override(name) or derive_group_segment(name)


def derive_group_segment(name: str) -> str:
    """Turn a free-text group label into its tag segment.

        SRS 4            -> SRS::04     trailing integer splits off, pads to 2
        SRS 12           -> SRS::12
        Week 3           -> Week::03
        Cardio revision  -> Cardio_revision   no trailing number, one segment
        SRS::04          -> SRS::04     already tag-shaped, passed through
        SRS 100          -> SRS::100    two-digit padding only

    The zero-padding is the entire reason this function exists: Anki's sidebar
    sorts tags as strings, so without it "SRS::12" sorts above "SRS::4" and a
    term's worth of groups reads in the wrong order. Padding to two digits gets
    the common case (1–99) right; beyond that the numbers are already the same
    width as each other.

    A label that already contains '::' is taken as deliberate and passed through
    untouched (bar whitespace cleanup) — the escape hatch for anyone whose
    scheme this guess doesn't fit."""
    label = (name or "").strip()
    if not label:
        return ""
    clean = _clean_seg
    if "::" in label:
        parts = [clean(p) for p in label.split("::")]
        return "::".join(p for p in parts if p)
    m = _TRAILING_NUM_RE.match(label)
    if not m:
        return clean(label)
    head, num = m.group(1).strip(), m.group(2)
    padded = num.zfill(2)
    if not head:
        return padded              # the label is just a number ("4" -> "04")
    return f"{clean(head)}::{padded}"


def group_tag_prefix(group: str) -> str:
    """The tag branch a group's cards live under, as an Anki search pattern.

    A group spans types — an SRS batch holds MQ, KG and LO gaps alike — and the
    type segment sits above the group in the tag, so there is no single literal
    prefix covering all of them. The '*' is doing real work here:

        !!Fleg::*::SRS::04

    Feed it to matcher.scope_query("tag", ...) to get a scope query. Returns ''
    when there's no base or no group, which every caller treats as no scope."""
    base = auto_tag_base()
    seg = group_segment(group)
    if not base or not seg:
        return ""
    return f"{base.rstrip(':')}::*::{seg}"


def tag_from_levels(levels: dict | None, kg_type_key: str | None = None,
                    type_meta: dict | None = None, group: str = "",
                    group_seg: str | None = None) -> str:
    """Format {system, subsystem, topic} levels into the shared hierarchical tag
    under the configured base + the KG type's segment + (optionally) the KG's
    group segment. Returns '' when unusable (no base, no levels, or it collapses
    to just the base). Pass either a type key OR the resolved type_meta dict.

    `group` is the KG's raw group LABEL ("SRS 4"), not a segment — the
    normalisation lives in group_segment() so every caller gets the same answer.
    Default '' renders exactly what this function rendered before groups
    existed.

    `group_seg` is the escape hatch: pass a segment to use it verbatim instead
    of resolving `group` through group_segment(). The Groups manager needs it to
    preview what a NEW name or override would produce before it saves either."""
    base = auto_tag_base()
    if not base or not levels:
        return ""
    if type_meta is not None:
        type_seg = type_meta.get("name") or type_meta.get("key") or ""
    else:
        type_seg, _enabled = kg_type_info((kg_type_key or default_type_key()))
    seg = group_segment(group) if group_seg is None else str(group_seg or "")
    tag = format_hierarchical_tag(base, levels, type_seg=type_seg,
                                  group_seg=seg)
    if not tag or tag == base:
        return ""
    return tag
