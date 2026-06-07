# Declarative one-call engine for KG types.
#
# A KG type's fields are data (config). This module turns those fields into a
# SINGLE AI request per flow (create | browse) and routes the one JSON-object
# reply back to (a) the KG store, (b) per-card outputs, (c) a hierarchical tag,
# and (d) ordered segments for each target Anki field. It generalises the two
# near-duplicate merged-call helpers (card_creator._merged_gen_instructions and
# browse._merged_terms_instructions) to N declarative fields, and works
# identically for every provider including manual/paste (one object reply == one
# paste).
#
# It is PURE: no Qt, no AI calls, no Anki writes. Callers dispatch via
# qt_utils.run_claude_json / manual_ai_complete and apply the routed result.
# That keeps it unit-testable off the main thread and off a live collection.

from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field
from typing import Any

# ── field-spec vocabulary ──────────────────────────────────────────────────────
# Each field on a KG type may declare these (all optional; sensible defaults are
# inferred so existing types Just Work):
#   source       capture | ai | capture+ai            (where the value comes from)
#   surfaces     [mq_capture | home | add_kg]          (which capture screens show it)
#   flows        [create | browse]                     (which AI calls request it)
#   cardinality  note | card                           (one per note, or one per card)
#   ai_prompt    str                                   (what the AI should produce)
#   ai_refs      [field_key, …]                        (other field values fed as context)
#   anki_target  {kind, role|field, notetype, position, mode}
#
# anki_target.kind ∈ role | field | tag | none
#   role  → resolved via the notetype profile (front/extra/image/one_by_one/missed_q)
#   field → a literal field name bound to anki_target.notetype
#   tag   → written to note.tags via the hierarchical scheme (not a field)
#   none  → kept in the KG store only

VALID_SOURCES = ("capture", "ai", "capture+ai")
VALID_FLOWS = ("create", "browse")
VALID_CARDINALITY = ("note", "card")
VALID_TARGET_KINDS = ("role", "field", "tag", "none")
ROLES = ("front", "extra", "image", "one_by_one", "missed_q")

# Known legacy field keys → the behaviour the hardcoded pipeline gave them, used
# only to INFER defaults when a field doesn't declare the new keys. This is what
# lets the makeover be backwards-compatible without rewriting every saved type.
_KNOWN_DEFAULTS: dict[str, dict] = {
    "concept":   {"source": "capture", "flows": ["create", "browse"],
                  "anki_target": {"kind": "role", "role": "missed_q", "position": 0, "mode": "append"}},
    "explanation": {"source": "ai", "flows": ["create"], "ai_refs": ["concept", "stem_html"],
                    "anki_target": {"kind": "role", "role": "missed_q", "position": 1, "mode": "append"}},
    "stem_html": {"source": "capture", "surfaces": ["mq_capture"],
                  "anki_target": {"kind": "role", "role": "missed_q", "position": 2, "mode": "append"}},
    "tags":      {"source": "ai", "flows": ["create", "browse"],
                  "anki_target": {"kind": "tag"}},
}


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v]
    return [v]


def normalise_field(spec: dict) -> dict:
    """Return a copy of `spec` with the declarative keys filled in. Unspecified
    keys are inferred: `source` from `kind` (capture for input kinds), `flows`
    default [create], `cardinality` note, and known keys (concept/explanation/
    stem_html/tags) get their legacy behaviour. Tolerant — never raises."""
    s = dict(spec or {})
    key = str(s.get("key", "")).strip()
    known = _KNOWN_DEFAULTS.get(key, {})

    src = str(s.get("source") or known.get("source") or "capture").lower()
    if src not in VALID_SOURCES:
        src = "capture"
    s["source"] = src

    s["surfaces"] = [x for x in _as_list(s.get("surfaces") or known.get("surfaces")
                                         or ["mq_capture", "home", "add_kg"])
                     if x in ("mq_capture", "home", "add_kg")]

    flows = [x for x in _as_list(s.get("flows") or known.get("flows") or ["create"])
             if x in VALID_FLOWS]
    s["flows"] = flows or ["create"]

    card = str(s.get("cardinality") or known.get("cardinality") or "note").lower()
    s["cardinality"] = card if card in VALID_CARDINALITY else "note"

    s["ai_prompt"] = str(s.get("ai_prompt") or known.get("ai_prompt") or "").strip()
    s["ai_refs"] = [str(x) for x in _as_list(s.get("ai_refs") or known.get("ai_refs"))]

    tgt = dict(s.get("anki_target") or known.get("anki_target") or {})
    kind = str(tgt.get("kind") or "none").lower()
    if kind not in VALID_TARGET_KINDS:
        kind = "none"
    tgt["kind"] = kind
    tgt.setdefault("role", "")
    tgt.setdefault("field", "")
    tgt.setdefault("notetype", "")
    tgt["position"] = int(tgt.get("position", 0) or 0)
    mode = str(tgt.get("mode") or "append").lower()
    tgt["mode"] = mode if mode in ("append", "prepend", "replace") else "append"
    s["anki_target"] = tgt
    return s


def type_fields(type_meta: dict | None) -> list[dict]:
    """All normalised field specs for a KG type."""
    if not isinstance(type_meta, dict):
        return []
    return [normalise_field(f) for f in (type_meta.get("fields") or []) if isinstance(f, dict)]


def ai_fields(type_meta: dict | None, flow: str) -> list[dict]:
    """AI-produced fields participating in `flow` (source includes ai)."""
    return [f for f in type_fields(type_meta)
            if "ai" in f["source"] and flow in f["flows"]]


def capture_fields(type_meta: dict | None, surface: str) -> list[dict]:
    """Fields a capture screen should render: source includes capture and
    `surface` is in surfaces. `surface` ∈ mq_capture | home | add_kg. Preserves
    field order so the form reads as the user arranged it in Settings."""
    return [f for f in type_fields(type_meta)
            if "capture" in f["source"] and surface in f["surfaces"]]


# ── request assembly ───────────────────────────────────────────────────────────

# Reply-key aliases: a field key may also be returned under a legacy/alternate
# name (e.g. the long-standing wire key "mq_explanation" for the explanation
# field, which existing manual-paste GPTs still emit). route() accepts either.
_REPLY_ALIASES: dict[str, list[str]] = {
    "explanation": ["mq_explanation"],
}


@dataclass
class RequestPlan:
    """What the one call will ask for and how to route the reply back."""
    note_fields: list[str] = _dc_field(default_factory=list)   # per-note ai field keys
    card_fields: list[str] = _dc_field(default_factory=list)   # per-card ai field keys
    want_tag: bool = False
    want_cards: bool = False
    want_terms: bool = False
    want_grade: bool = False


def build_object_instructions(plan: RequestPlan, fields: list[dict],
                              grade_instructions: str = "") -> str:
    """Build the 'return one JSON OBJECT' instruction block from a plan. Mirrors
    _merged_gen_instructions/_merged_terms_instructions but driven by declarative
    fields. Returns '' when the plan asks for nothing object-shaped (reply then
    stays a bare array/text). `grade_instructions` is the quality-pass grade text
    (caller-supplied, since QP lives in card_creator)."""
    spec_by_key = {f["key"]: f for f in fields}
    keys: list[str] = []
    if plan.want_tag:
        keys.append('"tags": {"system": "...", "subsystem": "...", "topic": "..."}')
    for k in plan.note_fields:
        keys.append(f'"{k}": "..."')
    if plan.want_terms:
        keys.append('"terms": [ "search term", ... ]')
    if plan.want_grade:
        keys.append('"grades": [ ...one verdict object per card, same order... ]')
    if plan.want_cards:
        if plan.card_fields:
            extra_keys = [k for k in plan.card_fields if k not in ("front", "extra")]
            shape = ", ".join(f'"{k}": "..."' for k in (["front", "extra"] + extra_keys))
            keys.append(f'"cards": [ {{ {shape} }}, ... ]')
        else:
            keys.append('"cards": [ ...the card objects exactly as specified above... ]')
    if not keys:
        return ""
    out = ["\n\nReturn your ENTIRE answer as a single JSON OBJECT (not a bare array) "
           "with these keys:", "  {" + ", ".join(keys) + "}"]
    if plan.want_tag:
        out.append(
            "Tag rules: system = top-level body system/domain (Cardio, Neuro, Endo, "
            "GI, Resp, Renal, Heme, MSK, Derm, Repro, Psych, ID, Onc, Pharm, Stats, "
            "Genetics, Biochem, Immuno; single best fit). subsystem = more specific "
            "category. topic = most specific entity/drug/sign. PascalCase or "
            "snake_case; no spaces, '::' or slashes; '' for any unclear level.")
    for k in (plan.note_fields + plan.card_fields):
        prompt = (spec_by_key.get(k) or {}).get("ai_prompt", "")
        if prompt:
            out.append(f'The "{k}" value is {prompt}.')
    if plan.want_grade and grade_instructions:
        out.append(grade_instructions)
    if plan.want_cards:
        out.append('The "cards" array uses EXACTLY the card object shape described above.')
    return "\n".join(out)


def build_refs_context(fields: list[dict], gap: dict) -> str:
    """Inline the values of every ai_ref'd field as context, deduped. `gap` is the
    KG dict (reads fields{} or legacy top-level via plain .get)."""
    wanted: list[str] = []
    for f in fields:
        for ref in f["ai_refs"]:
            if ref not in wanted:
                wanted.append(ref)
    bits: list[str] = []
    gfields = gap.get("fields") if isinstance(gap.get("fields"), dict) else {}
    for ref in wanted:
        val = (gfields.get(ref) if gfields else None)
        if val in (None, ""):
            val = gap.get(ref, "")
        val = re.sub(r"<[^>]+>", " ", str(val or ""))
        val = re.sub(r"\s+", " ", val).strip()
        if val:
            bits.append(f"{ref}: {val[:600]}")
    return "\n".join(bits)


def plan_for(type_meta: dict | None, flow: str, *, want_cards: bool,
             want_terms: bool, want_tag: bool, want_grade: bool = False,
             exclude: set | None = None) -> tuple[RequestPlan, list[dict]]:
    """Partition a type's ai-fields into per-note vs per-card for `flow`. Fields
    in `exclude` (already satisfied, e.g. a cached explanation) are dropped from
    the request but kept in the returned `fields` (so their prompt text is still
    available if needed). `fields` returned are only those actually requested."""
    exclude = exclude or set()
    all_fields = ai_fields(type_meta, flow)
    fields = [f for f in all_fields if f["key"] not in exclude]
    note_fields, card_fields = [], []
    for f in fields:
        if f["anki_target"]["kind"] == "tag":
            continue  # the tag rides want_tag, not a named key
        (card_fields if f["cardinality"] == "card" else note_fields).append(f["key"])
    plan = RequestPlan(note_fields=note_fields, card_fields=card_fields,
                       want_tag=want_tag, want_cards=want_cards, want_terms=want_terms,
                       want_grade=want_grade)
    return plan, fields


# ── reply routing ──────────────────────────────────────────────────────────────

@dataclass
class RoutedResult:
    field_values: dict = _dc_field(default_factory=dict)   # per-note ai field → value (for KG store)
    cards: list = _dc_field(default_factory=list)          # per-card objects
    tag_levels: dict | None = None                         # {system, subsystem, topic}
    terms: list = _dc_field(default_factory=list)          # search terms
    grades: Any = None                                     # per-card self-grade verdicts


def _reply_value(reply: dict, key: str):
    """Read a key from the reply, honouring legacy aliases (mq_explanation)."""
    if key in reply and reply.get(key) is not None:
        return reply.get(key)
    for alias in _REPLY_ALIASES.get(key, []):
        if alias in reply and reply.get(alias) is not None:
            return reply.get(alias)
    return None


def route(reply: Any, plan: RequestPlan) -> RoutedResult:
    """Split a one-call reply into its parts. Tolerant of a bare array (cards
    only) or a partial object (missing keys just skip). Replaces
    card_creator.unpack_generation_reply / browse's inline parse, generalised to
    N note fields and alias-aware (mq_explanation → explanation)."""
    res = RoutedResult()
    if isinstance(reply, dict):
        if plan.want_tag and isinstance(reply.get("tags"), dict):
            res.tag_levels = reply.get("tags")
        for k in plan.note_fields:
            v = _reply_value(reply, k)
            if v is not None:
                res.field_values[k] = v
        if plan.want_terms:
            res.terms = _coerce_str_list(reply.get("terms"))
        if plan.want_grade:
            res.grades = reply.get("grades")
        cards = reply.get("cards") if plan.want_cards else None
        res.cards = cards if isinstance(cards, list) else []
    elif isinstance(reply, list):
        if plan.want_cards:
            res.cards = reply
        elif plan.want_terms:
            res.terms = _coerce_str_list(reply)
    return res


def _coerce_str_list(v) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


# ── Anki target resolution + segment composition ───────────────────────────────

def resolve_target(anki_target: dict, profile: dict | None,
                   qbank_cfg: dict | None = None) -> tuple[str, str] | None:
    """Resolve an anki_target to (kind, anki_field_name). Returns:
        ("tag", "")            for a tag target
        ("field", "<name>")    for a role or literal field
        None                   for kind=none or an unresolvable role/field.
    Roles resolve via the notetype profile; missed_q via qbank.missed_q_field."""
    kind = anki_target.get("kind", "none")
    if kind == "tag":
        return ("tag", "")
    if kind == "field":
        name = (anki_target.get("field") or "").strip()
        return ("field", name) if name else None
    if kind == "role":
        role = anki_target.get("role", "")
        prof = profile or {}
        role_map = {
            "front":      prof.get("front_field", "Text"),
            "extra":      prof.get("extra_field", "Extra"),
            "image":      prof.get("image_field", prof.get("extra_field", "Extra")),
            "one_by_one": prof.get("one_by_one_field", "One by one"),
            "missed_q":   (qbank_cfg or {}).get("missed_q_field", "Missed Questions"),
        }
        name = (role_map.get(role) or "").strip()
        return ("field", name) if name else None
    return None


@dataclass
class _Segment:
    position: int
    mode: str
    value: str


def compose_field_segments(target_field: str, segments: list[_Segment]) -> str:
    """Compose ordered segments into one field value. `replace` wins (last one),
    otherwise segments join by position with <br> between, prepend before append."""
    if not segments:
        return ""
    replace = [s for s in segments if s.mode == "replace"]
    if replace:
        return replace[-1].value
    ordered = sorted(segments, key=lambda s: s.position)
    parts = [s.value for s in ordered if str(s.value).strip()]
    return "<br>".join(parts)


def note_field_plan(type_meta: dict | None, profile: dict | None,
                    qbank_cfg: dict | None,
                    value_for: "callable",
                    only_kinds: tuple | None = None) -> dict[str, str]:
    """Build {anki_field_name: composed_value} for fields whose target is a
    role/literal field. `value_for(field_key)` supplies the resolved value for a
    field (from capture or the routed AI result). Tag targets are excluded (apply
    those separately via the hierarchical scheme). `only_kinds` restricts to
    target kinds (e.g. ("field",) to apply ONLY user-defined literal-field
    targets, leaving built-in role composition to the existing pipeline)."""
    buckets: dict[str, list[_Segment]] = {}
    for f in type_fields(type_meta):
        if only_kinds is not None and f["anki_target"].get("kind") not in only_kinds:
            continue
        resolved = resolve_target(f["anki_target"], profile, qbank_cfg)
        if not resolved or resolved[0] != "field" or not resolved[1]:
            continue
        val = value_for(f["key"])
        if val in (None, ""):
            continue
        seg = _Segment(position=f["anki_target"]["position"],
                       mode=f["anki_target"]["mode"], value=str(val))
        buckets.setdefault(resolved[1], []).append(seg)
    return {fname: compose_field_segments(fname, segs) for fname, segs in buckets.items()}
