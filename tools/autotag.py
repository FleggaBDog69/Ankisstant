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

# A group's saved settings. `path` is its COURSE branch (a Curtanki case);
# `source` is its SOURCE path (a placement, a homework set) — the one thing a
# batch actually has to be told, since content classifies itself and the audit
# tags write themselves. They are independent: a group can have either, both or
# neither, and "neither" is the ordinary case.
_PLAN_DEFAULTS = {"path": "", "absolute": False, "levels": "full", "source": ""}


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
    """One config entry -> {"path", "absolute", "levels", "source"}, or None
    when it says nothing. Fail-open on every unexpected shape: an override that
    can't be read must fall back to the derived segment, never to a broken tag.

    A SOURCE on its own is a real override. That is the ordinary group now — a
    placement, a homework set, a day on the wards — and the reason this can no
    longer bail out the moment there is no path: doing so threw the source
    away and left the group looking unconfigured."""
    if isinstance(raw, str):
        path = clean_segment_path(raw)
        return dict(_PLAN_DEFAULTS, path=path) if path else None
    if isinstance(raw, dict):
        # "segment" is the key the first version of this used for a plain path.
        path = clean_segment_path(raw.get("path") or raw.get("segment") or "")
        source = clean_source_path(raw.get("source") or "")
        if not path and not source:
            return None
        return {"path": path,
                "absolute": bool(raw.get("absolute")),
                "levels": normalise_levels(raw.get("levels", "full")),
                "source": source}
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
                   levels: bool | None = None,
                   source: str | None = None) -> dict:
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
    if source is not None:
        plan = dict(plan, source=clean_source_path(source))
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


def group_source(name: str) -> str:
    """The source path this group's cards came from, or ''.

    The group is the right place to keep it: a batch has ONE provenance — the
    placement you were on, the homework set you were marked on — and asking per
    card is how a source axis stops being filled in."""
    ov = group_override(name)
    return (ov or {}).get("source", "") or ""


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

    A group with no course branch of its own but a SOURCE scopes to the source
    tag — the placement's branch is where its cards actually are:

        !!Fleg::Sources::Placements::GP

    Feed it to matcher.scope_query("tag", ...) to get a scope query. Returns ''
    when there's nothing to scope to, which every caller treats as no scope."""
    plan = group_tag_plan(group)
    if plan["absolute"] and plan["path"]:
        return plan["path"]
    if plan.get("source"):
        from ..core.config import source_tag
        tag = source_tag(plan["source"])
        if tag:
            return tag
    if not plan["path"] or not group_override(group):
        # A group with no saved override has a DERIVED segment, and nothing is
        # ever tagged with it: tags_for() applies the group's branch only when
        # the group owns one. Scoping to it returned a query that could only
        # ever match zero cards, which reads as "you have nothing on this"
        # rather than "this group has no branch".
        return ""
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
    #
    # An absolute group used to REPLACE the content tag: a gap in "SRS 5" got
    # !!Curtanki::Year_3::SRS::05::<topic> and nothing under !!Fleg::KG:: at
    # all. That is what the group feature was for — one contiguous course
    # branch — but it means the content axis has a hole in it exactly where the
    # course work is, so "everything I have on the thyroid" misses every card
    # made from a case.
    #
    # The two are no longer alternatives. This still returns the course path,
    # because that is what the group asked for and what the tag box should
    # show; the CONTENT tag comes back from content_tag_from_levels() and both
    # are applied. See tags_for().
    if plan["absolute"] and plan["path"]:
        if not any_kept:
            return plan["path"]
        # Rendered through the SAME template as everything else, with the path
        # standing in for the base and no type segment — so a customised
        # template still shapes the levels the way the user set it up.
        tag = format_hierarchical_tag(plan["path"], kept, type_seg="",
                                      group_seg="", content_seg="")
        return tag or plan["path"]

    # ── relative: one segment inside the shared base ─────────────────────────
    base = auto_tag_base()
    if not base or not levels:
        return ""
    # The second segment is the CONTENT axis ("KG"), the same for every card.
    # It used to be the gap's TYPE, which filed an LO gap under {base}::LO:: and
    # a missed question under {base}::MQ:: — provenance sitting in the middle of
    # the content hierarchy, so one fact lived in two branches depending on how
    # it happened to be captured. The type is provenance and now rides the
    # source axis (see source_tag_for).
    tag = format_hierarchical_tag(base, kept, group_seg=plan["path"])
    if not tag or tag == base:
        return ""
    return tag


# ── the source axis ──────────────────────────────────────────────────────────
#
# WHERE a card came from, kept strictly separate from what it is about. The
# content tag holds the body system once; a source tag that repeated it would
# put the same fact in two branches, which is the thing this scheme exists to
# stop. So a source path is short and says only where: "MQ", "CS::HW_Aug_26",
# "Lecture::cardiac_output", "Placements::Homeless_Healthcare".
#
# Derived where it can be, set by hand where it can't, and the hand-set value
# STICKS per tool — a placement runs for weeks, and re-typing the same source on
# every card is how a source axis stops being filled in.

# KG type key -> its source segment. A type not listed here contributes no
# source of its own: "kg" is the ordinary case (a gap you noticed), and
# "Sources::KG" would say nothing the content tag doesn't already say.
_TYPE_SOURCES = {"mq": "MQ", "cs": "CS", "lo": "LO"}


def clean_source_path(text: str) -> str:
    """A typed source down to the PATH — what goes after "{base}::Sources::".

    Strips the prefix if it is already there, so pasting a whole tag out of the
    sidebar or accepting a tag completer's suggestion stores "Placements::GP"
    rather than a value that prefixes itself on the way back out. Renders
    through source_tag() and takes the head off again, so there is exactly one
    definition of what the prefix is.
    """
    from ..core.config import auto_tag_base as _base, SOURCES_SEGMENT
    from ..core.config import source_tag as _source_tag
    tag = _source_tag(text)
    if not tag:
        return clean_segment_path(text)
    head = f"{_base()}::{SOURCES_SEGMENT}::"
    return tag[len(head):] if tag.startswith(head) else clean_segment_path(text)


def source_for_type(kg_type_key: str) -> str:
    return _TYPE_SOURCES.get(str(kg_type_key or "").strip().lower(), "")


def source_for_lecture(path: str) -> str:
    """A lecture file's source path: Lecture::<the file's own name>."""
    import os
    name = os.path.basename(str(path or ""))
    stem = os.path.splitext(name)[0]
    # ".pdf" -> splitext returns (".pdf", ""): a dotfile has no stem, and
    # "Lecture::.pdf" is not a source.
    if stem.startswith("."):
        stem = ""
    stem = _clean_seg(stem)
    return f"Lecture::{stem}" if stem else "Lecture"


def sticky_source(tool: str) -> str:
    """The source this tool last committed under. Sticks because a placement or
    a question bank run spans weeks, not one card."""
    try:
        return str(tool_config(tool).get("source_path") or "").strip()
    except Exception:
        return ""


def set_sticky_source(tool: str, path: str) -> None:
    # Imported here, not at module level: autotag is deliberately importable
    # with almost nothing stubbed, and three offline harnesses rely on that.
    from ..core import log
    from ..core.config import save_tool_config, tool_config as _tc
    try:
        cfg = dict(_tc(tool))
        cfg["source_path"] = str(path or "").strip()
        save_tool_config(tool, cfg)
    except Exception as e:
        log.warn(f"autotag: couldn't remember the source for {tool}: {e}")


def source_path_for(*, tool: str = "", kg_type_key: str = "",
                    lecture_path: str = "", group: str = "",
                    override: str | None = None) -> str:
    """The source path for a card, most specific first.

    An explicit `override` (what the user typed for this run) always wins. Then
    the file, then the GROUP's own source, then the gap type, then whatever this
    tool last used. "" means no source tag — which is a legitimate answer, not a
    hole to fill with a guess.

    The group sits above the type deliberately: "GP Day 1" is something you
    said about this batch, and a type segment is a default that applies to
    every batch. A thing you chose beats a thing that was assumed.
    """
    if override is not None:
        typed = str(override).strip()
        # An explicit EMPTY override is the user saying "no source", and it has
        # to beat every derived answer — including the group's. Clearing the
        # box is the ONLY way to say "not this one" for a single run, and a
        # value that came back anyway would make the box a lie. This check used
        # to sit below the derivations, where it could only override the sticky
        # value it was already below.
        return typed
    if lecture_path:
        return source_for_lecture(lecture_path)
    from_group = group_source(group) if group else ""
    if from_group:
        return from_group
    from_type = source_for_type(kg_type_key)
    if from_type:
        return from_type
    return sticky_source(tool) if tool else ""


def source_tag_for(**kw) -> str:
    """source_path_for, rendered as a tag. "" when there is no source."""
    from ..core.config import source_tag
    return source_tag(source_path_for(**kw))


def known_source_paths() -> list:
    """Source paths already in the collection, for the completer — so the
    second placement of the year files under the first one's branch instead of
    inventing a near-miss spelling."""
    from ..core.config import auto_tag_base, SOURCES_SEGMENT
    base = auto_tag_base()
    if not base:
        return []
    head = f"{base}::{SOURCES_SEGMENT}::"
    out: set = set()
    try:
        from aqt import mw
        for t in mw.col.tags.all():
            if t.startswith(head):
                tail = t[len(head):].strip(":")
                if tail:
                    out.add(tail)
                    # The parents too: "CS::HW_Aug_26" implies "CS" is a source
                    # you use, even if nothing is tagged with it bare.
                    parts = tail.split("::")
                    for i in range(1, len(parts)):
                        out.add("::".join(parts[:i]))
    except Exception:
        return []
    return sorted(out)


def content_tag_from_levels(levels: dict | None, *, group: str = "",
                            group_plan: dict | None = None,
                            kg_type_key: str | None = None,
                            type_meta: dict | None = None) -> str:
    """The CONTENT tag — {base}::KG::{system}::{subsystem}::{topic} — always
    relative to the base, whatever the group is doing.

    Separate from tag_from_levels because an absolute group deliberately
    returns its own branch there. Both are wanted; neither replaces the other.

    `kg_type_key` / `type_meta` are ACCEPTED AND IGNORED. They used to pick the
    second segment, which is exactly the mistake this function exists to undo —
    the type is provenance and rides the source axis now (source_for_type).
    They stay in the signature so the ~six call sites that pass one don't all
    need editing to say the same thing, and so passing one is visibly a no-op
    rather than a silent difference.
    """
    base = auto_tag_base()
    if not base or not levels:
        return ""
    # The group's `levels` mode is DELIBERATELY not applied here. It decides how
    # much of the hierarchy to repeat under the group's own branch — "topic"
    # keeps that branch shallow instead of making it a deep duplicate of the
    # content tree. The content tag is that tree, so it always carries the full
    # levels; trimming it here filed a "topic"-mode gap as
    # !!Fleg::KG::Graves_disease, with no field segment at all.
    if not any(str(v).strip() for v in (levels or {}).values()):
        return ""
    tag = format_hierarchical_tag(base, levels)
    return "" if (not tag or tag == base) else tag


def tags_for(levels: dict | None, *, group: str = "", tool: str = "",
             kg_type_key: str = "", lecture_path: str = "",
             source_override: str | None = None,
             group_plan: dict | None = None) -> list:
    """EVERY tag one card earns from the scheme, on all three axes.

    Order is the commit order and it is load-bearing (see
    tools/commit.tag_ops_for): most specific first.

      1. content   {base}::KG::{system}::{subsystem}::{topic}
      2. course    the group's own branch, when it has an absolute one
      3. source    {base}::Sources::{path}, when there is a source —
                   the group's own source unless this run overrode it

    Deduped case-insensitively, because Anki matches tags that way and two
    spellings of one tag are one tag with twice the clutter.
    """
    out: list = []
    seen: set = set()

    def add(t):
        t = str(t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)

    add(content_tag_from_levels(levels, group=group, group_plan=group_plan))
    course = tag_from_levels(levels, group=group, group_plan=group_plan)
    plan = (group_plan if group_plan is not None else group_tag_plan(group))
    if plan.get("absolute") and plan.get("path"):
        add(course)
    add(source_tag_for(tool=tool, kg_type_key=kg_type_key,
                       lecture_path=lecture_path, group=group,
                       override=source_override))
    return out
