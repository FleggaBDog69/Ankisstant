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


# ── where a group's tags live ────────────────────────────────────────────────
#
# A group tags in one of TWO ways, and the difference is the whole reason this
# is more than a string:
#
#   RELATIVE (the default)  the group is one segment inside the shared auto-tag
#                           base:  !!Fleg::KG::SRS::04::System::Sub::Topic
#   ABSOLUTE                the group owns a branch of its own, and the base and
#                           the type segment are not used at all:
#                           !!Curtanki::Year_3::SRS::02::System::Sub::Topic
#
# Absolute exists because a single global base is the wrong shape for a real
# collection. A study system that predates this add-on already has its own tree,
# and forcing every group under one prefix means the add-on's gaps can never
# join it. The base stays the default because it's the right answer for a group
# that has no home of its own yet.
#
# Config accepts two shapes per group, so nothing written before this survives
# as anything other than what it already meant:
#
#   "Cardio::SRS4"                                      relative segment
#   {"path": "...", "absolute": bool, "levels": bool}    the full form
#
# `levels` says HOW MUCH of the gap's system/subsystem/topic goes underneath the
# path. Not a yes/no, because both extremes are wrong for a real batch: the full
# three levels are one too many under something already as narrow as an SRS
# block or a PBL case (the block IS the system), while none at all dumps every
# gap in the batch onto one node with no leaf to tell them apart.

GROUP_LEVEL_MODES = {
    "full":  ("system", "subsystem", "topic"),
    "sub":   ("subsystem", "topic"),
    "topic": ("topic",),
    "none":  (),
}
GROUP_LEVEL_LABELS = {
    "full":  "System · Subsystem · Topic",
    "sub":   "Subsystem · Topic",
    "topic": "Topic only",
    "none":  "Nothing — the path is the whole tag",
}

_PLAN_DEFAULTS = {"path": "", "absolute": False, "levels": "full"}


def normalise_levels(value) -> str:
    """A levels setting -> one of GROUP_LEVEL_MODES.

    Accepts the bool this was first written as: True meant "append all three",
    False meant "append nothing", and both keep meaning exactly that."""
    if isinstance(value, bool):
        return "full" if value else "none"
    mode = str(value or "").strip().lower()
    return mode if mode in GROUP_LEVEL_MODES else "full"


def group_tag_overrides() -> dict:
    """The raw per-group override map, {label: str | dict}. Never raises — a
    broken or absent config means "no overrides", not a dead tag."""
    try:
        raw = tool_config("knowledge_gaps").get("group_tag_overrides") or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()}


def normalise_override(raw) -> dict | None:
    """One config entry -> {"path", "absolute", "levels"}, or None when it says
    nothing. Fail-open on every unexpected shape: an override that can't be read
    must fall back to the derived segment, never to a broken tag."""
    if isinstance(raw, str):
        path = clean_segment_path(raw)
        return dict(_PLAN_DEFAULTS, path=path) if path else None
    if isinstance(raw, dict):
        # "segment" is the key the first version of this used for a plain path.
        path = clean_segment_path(raw.get("path") or raw.get("segment") or "")
        if not path:
            return None
        return {"path": path,
                "absolute": bool(raw.get("absolute")),
                "levels": normalise_levels(raw.get("levels", "full"))}
    return None


def group_override(name: str) -> dict | None:
    """This group's saved override, normalised, or None if it uses the derived
    segment inside the base."""
    try:
        return normalise_override(
            group_tag_overrides().get(str(name or "").strip()))
    except Exception:
        return None


def group_tag_plan(name: str, *, path: str | None = None,
                   absolute: bool | None = None,
                   levels: bool | None = None) -> dict:
    """How this group tags, resolved: {"path", "absolute", "levels"}.

    THE single answer every tag-building caller goes through. The keyword
    arguments override the saved values one field at a time, which is what lets
    the edit dialog preview a path the user hasn't saved yet without touching
    config to do it."""
    plan = group_override(name) or dict(
        _PLAN_DEFAULTS, path=derive_group_segment(name))
    if path is not None:
        cleaned = clean_segment_path(path)
        plan = dict(plan, path=cleaned or derive_group_segment(name))
    if absolute is not None:
        plan = dict(plan, absolute=bool(absolute))
    if levels is not None:
        plan = dict(plan, levels=normalise_levels(levels))
    return plan


def trim_levels(levels: dict | None, mode: str) -> dict:
    """The {system, subsystem, topic} dict cut down to `mode`'s levels.

    Dropped levels are blanked rather than removed, so they collapse out of the
    template the same way an unclassified gap's empty level always has."""
    keep = GROUP_LEVEL_MODES.get(normalise_levels(mode), GROUP_LEVEL_MODES["full"])
    levels = levels or {}
    return {k: (levels.get(k, "") if k in keep else "")
            for k in ("system", "subsystem", "topic")}


def group_segment_override(name: str) -> str:
    """The explicit path set for this group, or '' if it uses the derived one.
    Cleaned the same way a derived segment is, so an override can't put a space
    or a stray slash into a tag."""
    ov = group_override(name)
    return ov["path"] if ov else ""


def group_is_absolute(name: str) -> bool:
    """True when this group owns its tag branch outright — the auto-tag base and
    the type segment are not used for it."""
    ov = group_override(name)
    return bool(ov and ov["absolute"])


def group_segment(name: str) -> str:
    """The tag path for a group: the user's override if they set one, else the
    derived rule. For an absolute group this is the whole branch, not a
    segment — see group_tag_plan for which it is."""
    return group_tag_plan(name)["path"]


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

    For a RELATIVE group, a batch spans types — an SRS batch holds MQ, KG and LO
    gaps alike — and the type segment sits above the group in the tag, so there
    is no single literal prefix covering all of them. The '*' is doing real work:

        !!Fleg::*::SRS::04

    An ABSOLUTE group has no type segment above it, so its prefix is the branch
    itself and needs no wildcard at all:

        !!Curtanki::Year_3::SRS::02

    Feed it to matcher.scope_query("tag", ...) to get a scope query. Returns ''
    when there's nothing to scope to, which every caller treats as no scope."""
    plan = group_tag_plan(group)
    if not plan["path"]:
        return ""
    if plan["absolute"]:
        return plan["path"]
    base = auto_tag_base()
    if not base:
        return ""
    return f"{base.rstrip(':')}::*::{plan['path']}"


def tag_from_levels(levels: dict | None, kg_type_key: str | None = None,
                    type_meta: dict | None = None, group: str = "",
                    group_seg: str | None = None,
                    group_plan: dict | None = None) -> str:
    """Format {system, subsystem, topic} levels into this KG's hierarchical tag.

    Two shapes, decided by the group (see group_tag_plan):

      RELATIVE  {base}::{type}::{group}::{system}::{subsystem}::{topic}
      ABSOLUTE  {group path}::{system}::{subsystem}::{topic}   — the base and
                the type segment are not used, because the group owns its own
                branch.

    How many of the three trailing levels appear is the group's `levels` mode
    (see GROUP_LEVEL_MODES); "none" means the path is the whole tag. It applies
    to both shapes, and defaults to "full" — which is what every caller rendered
    before the mode existed.

    Returns '' when unusable (nothing to build from, or it collapses to just the
    base). Pass either a type key OR the resolved type_meta dict.

    `group` is the KG's raw group LABEL ("SRS 4"), not a path — the
    normalisation lives in group_tag_plan() so every caller gets the same
    answer. Default '' renders exactly what this rendered before groups existed.

    `group_plan` and `group_seg` are the preview escape hatches: pass either to
    use it verbatim instead of resolving `group`. The Groups manager needs them
    to show what a name/path/mode the user hasn't saved yet would produce.
    `group_seg` is the older, relative-only form and is kept for callers that
    only ever meant "this segment, inside the base"."""
    if group_plan is not None:
        plan = dict(_PLAN_DEFAULTS, **{k: v for k, v in group_plan.items()
                                       if k in _PLAN_DEFAULTS})
        plan["path"] = clean_segment_path(plan["path"])
        plan["levels"] = normalise_levels(plan["levels"])
    elif group_seg is not None:
        plan = dict(_PLAN_DEFAULTS, path=clean_segment_path(group_seg))
    else:
        plan = group_tag_plan(group)

    kept = trim_levels(levels, plan["levels"])
    any_kept = any(str(v).strip() for v in kept.values())

    # ── absolute: the group's branch, standing on its own ────────────────────
    if plan["absolute"] and plan["path"]:
        if not any_kept:
            return plan["path"]
        # Rendered through the SAME template as everything else, with the path
        # standing in for the base and no type segment — so a customised
        # template still shapes the levels the way the user set it up.
        tag = format_hierarchical_tag(plan["path"], kept, type_seg="",
                                      group_seg="")
        return tag or plan["path"]

    # ── relative: one segment inside the shared base ─────────────────────────
    base = auto_tag_base()
    if not base or not levels:
        return ""
    if type_meta is not None:
        type_seg = type_meta.get("name") or type_meta.get("key") or ""
    else:
        type_seg, _enabled = kg_type_info((kg_type_key or default_type_key()))
    tag = format_hierarchical_tag(base, kept, type_seg=type_seg,
                                  group_seg=plan["path"])
    if not tag or tag == base:
        return ""
    return tag
