# The eight stages, as types.
#
# Every tool in this add-on — AI Lecture, AI Browse, AI Create, Mass AI Create,
# the work-through pass, Analyse Knowledge Gaps, Update by Tag — is some subset
# of the same eight steps:
#
#   1 TAKE IN   whatever you fed it            -> WorkItem
#   2 READ      the material, into points      -> Plan          (AI)
#   3 APPROVE   you edit the plan              -> Plan (edited)
#   4 FIND      what your deck already covers  -> Coverage      (AI)
#   5 WRITE     cards for what it doesn't      -> job records   (AI)
#   6 CHECK     grade what was written         -> Verdicts      (AI)
#   7 REVIEW    you tick what to keep          -> selection
#   8 COMMIT    tag, unsuspend, add            -> CommitPlan
#
# Most of these ALREADY EXIST as real functions with real types; what they
# lacked was a name, which is why the same operation drifted onto two different
# model dials depending on which button you pressed (tools/kg/bulk_search.py
# read gaps on `lecture_extract` while tools/kg/mass_create.py read the same
# gaps on `card_creation`). Naming the stage is the fix.
#
# Four of the six payload types are reused verbatim from where they already
# live, deliberately — this module does NOT reinvent them:
#
#   Stage 4  matcher.Coverage           tools/lecture/matcher.py
#   Stage 5  create_jobs job record     tools/kg/gen_prompt.py:build_job_record
#   Stage 6  quality_pass.Verdict       tools/quality_pass.py
#   Stage 2  extractor.LearningPoint    tools/lecture/extractor.py
#
# What's new here is the two ends (WorkItem, CommitPlan), the per-item wrapper
# generalised off KGPlan (Plan), and the execution context that carries the
# stage's model dial (RunContext).
#
# PURE. No Qt, no aqt, no mw.col, and — via `from __future__ import annotations`
# plus a TYPE_CHECKING-only import — no runtime dependency on the lecture
# package either. That's what lets it be unit-tested with no stubs at all, and
# it's the same rule tools/kg/engine.py follows.

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:                                    # pragma: no cover
    from .lecture.extractor import LearningPoint


# ── the stage vocabulary ─────────────────────────────────────────────────────
#
# `dial` is the AI matrix key the stage resolves its model from. One key per
# stage is the whole point — see the drift note at the top.
#
# `legacy_dials` is what that stage's model is read from TODAY, in priority
# order. dial_for() falls through to these, so a user whose meta.json has never
# heard of "read" keeps the exact model they're on right now. Additive only:
# nothing is rewritten, nothing is reset.

TAKE_IN, READ, APPROVE, FIND, WRITE, CHECK, REVIEW, COMMIT = (
    "take_in", "read", "approve", "find", "write", "check", "review", "commit")


@dataclass(frozen=True)
class Stage:
    key: str
    ordinal: int                 # 1-8, the number shown in the stage strip
    label: str                   # what the strip says
    dial: str = ""               # AI matrix key; "" = not an AI stage
    legacy_dials: tuple = ()     # fallbacks, highest priority first


STAGES: tuple = (
    Stage(TAKE_IN, 1, "Take in"),
    Stage(READ,    2, "Read",    "read",  ("lecture_extract", "card_creation")),
    Stage(APPROVE, 3, "Approve"),
    Stage(FIND,    4, "Find",    "find",  ("lecture_relevance", "search")),
    Stage(WRITE,   5, "Write",   "write", ("card_creation",)),
    Stage(CHECK,   6, "Check",   "check", ("quality_pass",)),
    Stage(REVIEW,  7, "Review"),
    Stage(COMMIT,  8, "Commit"),
)

_BY_KEY: dict = {s.key: s for s in STAGES}


def stage(key: str) -> Stage:
    """The Stage record for a key. Raises on an unknown key rather than
    returning a default — a typo'd stage name silently resolving to Take In
    would be invisible in the strip and wrong everywhere else."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(f"unknown pipeline stage: {key!r}") from None


def ai_stages() -> list:
    """The four stages that call a model. What Settings' AI matrix should list."""
    return [s for s in STAGES if s.dial]


# ── stage 1: the work item ───────────────────────────────────────────────────

# What kind of thing came in the door. Chooses the READ implementation (a
# knowledge gap is read by triage, a lecture by the extractor — same output
# type, deliberately different prompts) and which stages get skipped.
KIND_KG, KIND_LECTURE, KIND_NOTE, KIND_TOPIC = "kg", "lecture", "note", "topic"


@dataclass
class WorkItem:
    """One thing to work on, whatever door it came through.

    A SUPERSET, not a lowest common denominator: `fields` and `source` exist so
    that porting a door to this type can never drop what only that door carries
    (a lecture's segment numbers drive slide attachment; a Things import carries
    its uuid for write-back). Anything without a home goes in `source` and rides
    through untouched.
    """
    id: str
    title: str
    kind: str = KIND_KG
    # The KG *type* slug — "lo" | "mq" | "kg" | whatever the user configured.
    # DISTINCT from `kind`: kind is which door, type_key is which notetype
    # profile and which segment of the hierarchical tag. Conflating them would
    # quietly retag every gap.
    type_key: str = ""
    group: str = ""
    tags: list = field(default_factory=list)
    # Per-type content — notes, stem_html, explanation, images. Same blob the
    # KG store keeps under `fields{}`.
    fields: dict = field(default_factory=dict)
    # Door-specific baggage: {"path": ...} for a lecture, {"nid": ...} for a
    # note, {"things_uuid": ...} for an import. Never read generically.
    source: dict = field(default_factory=dict)

    # -- constructors, one per door --

    @classmethod
    def from_kg(cls, rec: dict) -> "WorkItem":
        """A knowledge-gap store record. Tolerates both the stored shape
        (content under fields{}) and the flattened hand-off shape."""
        rec = rec or {}
        kid = str(rec.get("id") or rec.get("kg_id") or "")
        fields_blob = dict(rec.get("fields") or {})
        # Promote the legacy top-level keys the store also accepts, so a record
        # that has been flattened once doesn't lose its content coming back.
        for legacy in ("notes", "stem_html", "explanation", "images",
                       "system", "subsystem", "topic", "platform", "lo", "lo_tag"):
            val = rec.get(legacy)
            if legacy not in fields_blob and val not in (None, "", []):
                fields_blob[legacy] = val
        return cls(
            id=kid,
            title=str(rec.get("title") or "").strip(),
            kind=KIND_KG,
            type_key=str(rec.get("type") or rec.get("kg_type") or "").lower(),
            group=str(rec.get("group") or "").strip(),
            tags=[str(t).strip() for t in (rec.get("tags") or []) if str(t).strip()],
            fields=fields_blob,
            source={},
        )

    @classmethod
    def from_lecture(cls, path: str, title: str = "", *,
                     companion: bool = False) -> "WorkItem":
        """A lecture file. The path stays in `source` because ingest re-reads it
        for slide rasterisation long after the read stage is done."""
        import os
        return cls(
            id=f"lec:{path}",
            title=(title or os.path.basename(path or "")).strip(),
            kind=KIND_LECTURE,
            source={"path": path, "companion": bool(companion)},
        )

    @classmethod
    def from_note(cls, nid: int, title: str = "") -> "WorkItem":
        """An existing note, for the rewrite path (Update by Tag). Skips READ
        and FIND entirely — the material is already a card."""
        return cls(id=f"nid:{nid}", title=str(title or f"note {nid}"),
                   kind=KIND_NOTE, source={"nid": int(nid)})

    @classmethod
    def from_topic(cls, topic: str) -> "WorkItem":
        """A typed topic — AI Create's and AI Browse's door. No id of its own,
        so it gets one derived from the text; two runs on the same topic are
        deliberately the same item."""
        t = str(topic or "").strip()
        return cls(id=f"topic:{t.lower()}", title=t, kind=KIND_TOPIC)

    # -- the shape the generation side reads --

    def to_kg_record(self, *, concept: str = "", points: list | None = None,
                     target_cards: int = 0) -> dict:
        """FLATTENED, not a raw store record.

        gen_prompt.build_generation_request and build_job_record read `notes`,
        `stem_html`, `concept` and `explanation` off the TOP level, while the
        store keeps them under fields{}. Handing over a raw store record wrote
        cards with none of the gap's own context — a real bug, fixed once in
        bulk_search.create_record_for and centralised here so the next door
        can't reintroduce it.

        `fields` rides along as well, for the declarative field values
        card_creator._gap_field_values resolves at commit time.
        """
        f = self.fields or {}
        pts = list(points or [])
        notes_bits = []
        own = str(f.get("notes") or "").strip()
        if own:
            notes_bits.append(own)
        # The uncovered points go in TWICE on purpose: structured, so a plan can
        # be rebuilt with no second AI call, and as prose on the end of the
        # notes, because prose is what the model actually reads.
        concepts = [str(p.get("concept") or "").strip() for p in pts
                    if isinstance(p, dict)]
        concepts = [c for c in concepts if c]
        if concepts:
            notes_bits.append("My deck has no card for these parts of this gap:\n"
                              + "\n".join(f"• {c}" for c in concepts))
        return {
            "id":           self.id,
            "kg_id":        self.id,
            "title":        self.title,
            "type":         self.type_key,
            "kg_type":      self.type_key,
            "group":        self.group,
            "tags":         list(self.tags),
            "fields":       dict(f),
            "stem_html":    str(f.get("stem_html") or ""),
            "explanation":  str(f.get("explanation") or ""),
            "images":       list(f.get("images") or []),
            "concept":      str(concept or "").strip() or self.title,
            "notes":        "\n\n".join(notes_bits),
            "points":       pts,
            # One card per uncovered point is the honest default: the point IS
            # the thing the deck is missing.
            "target_cards": int(target_cards) or max(1, len(pts)),
        }

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "WorkItem":
        d = dict(d or {})
        return cls(
            id=str(d.get("id") or ""), title=str(d.get("title") or ""),
            kind=str(d.get("kind") or KIND_KG),
            type_key=str(d.get("type_key") or ""),
            group=str(d.get("group") or ""),
            tags=list(d.get("tags") or []),
            fields=dict(d.get("fields") or {}),
            source=dict(d.get("source") or {}),
        )


# ── stage 2: the plan ────────────────────────────────────────────────────────

# Hard clamps on a plan's card target, whatever a model or a stale cache says.
# Mirrors triage.MIN_CARDS / MAX_CARDS / SCOPES[DEFAULT_SCOPE]["cards"] — see
# Plan.per_point_cap for why they are copied rather than imported.
#
# DEFAULT_CARDS is the fallback for a target that isn't a number at all, and it
# is the FOCUSED scope's figure rather than the floor: garbage in a cache should
# land on the ordinary case, not starve the plan down to a single card.
MIN_CARDS, MAX_CARDS, DEFAULT_CARDS = 1, 40, 4


@dataclass
class Plan:
    """One work item, read and decomposed — triage.KGPlan generalised.

    Every KGPlan field is kept. The four identity fields (kg_id, title,
    kg_type, group) and `tags` move onto `item` and are re-exposed as read-only
    properties, so the ~40 existing `plan.kg_id` / `plan.title` / `plan.tags`
    call sites keep working untouched while the migration runs. Nothing in the
    add-on assigns to those four — only to the body fields below — which is
    what makes read-only safe (checked, not assumed).

    A lecture produces ONE Plan holding all its points, which is exactly what
    a lecture never had: somewhere to hang its card budget, its constraints and
    its tag set. That absence is why lecture points could never reach the
    write stage.
    """
    item: WorkItem
    concept: str = ""                # the cleaned reading of the item
    scope: str = "focused"           # search-side breadth (triage.SCOPES)
    cards: int = 0                   # per-item card target, user-editable
    breadth: str = "core"            # generation-side breadth (CREATE_BREADTHS)
    depth_band: str = "essentials"   # the half of breadth that reaches the prompt
    points: list = field(default_factory=list)      # list[LearningPoint]
    confidence: str = "high"
    note: str = ""                   # why it's low-confidence / what's ambiguous
    # Directives the student wrote AT the AI ("do not focus on dx") rather than
    # facts to learn. Verbatim — they are passed straight to the generation
    # prompt, and a constraint that only reaches READ is one the model that
    # actually writes the cards never sees.
    constraints: list = field(default_factory=list)
    included: bool = True            # unticked rows are carried, never run

    # -- identity, delegated to the item --

    @property
    def kg_id(self) -> str:
        return self.item.id

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def kg_type(self) -> str:
        return self.item.type_key

    @property
    def group(self) -> str:
        return self.item.group

    @property
    def tags(self) -> list:
        return self.item.tags

    @property
    def kind(self) -> str:
        return self.item.kind

    # -- the two properties every caller of KGPlan uses --

    @property
    def ok(self) -> bool:
        """Runnable: something was understood and it produced searchable points."""
        return bool(self.points)

    @property
    def per_point_cap(self) -> int:
        """The card target spread across this item's points, for allocate(cap_for).

        Ceiling division, floored at 1: a 5-card plan over 2 points gives 3 each
        (the allocator's round-robin and the total budget stop it actually
        spending 6), and a 1-card plan still gets its one card.

        The clamp is triage.clamp_cards' rule, inlined rather than imported so
        this module stays dependency-free at import time. It must stay in step
        with MIN_CARDS/MAX_CARDS there — a pinned test compares the two.
        """
        n = max(1, len(self.points))
        try:
            cards = max(MIN_CARDS, min(MAX_CARDS, int(self.cards)))
        except (TypeError, ValueError):
            cards = DEFAULT_CARDS
        return max(1, -(-cards // n))


# ── stage 8: the commit ──────────────────────────────────────────────────────

@dataclass
class CommitPlan:
    """Everything one run wants to change in the collection, as data.

    Built by whichever door ran, executed by ONE commit() inside ONE
    mw.checkpoint — which is what makes a single Ctrl+Z take a whole pass back
    out, on every path, instead of the seven separate checkpoints the add-on
    has today.

    `tag_ops` and `field_appends` are LISTS, not dicts, because their order is
    load-bearing: the per-item tag goes on before the audit and month tags, and
    card notes go on before slide images so the text lands above the images in
    the field. A dict would silently lose that.
    """
    label: str                                   # the mw.checkpoint name
    tag_ops: list = field(default_factory=list)          # [(tag, [nid, ...]), ...]
    unsuspend_nids: list = field(default_factory=list)   # unsuspend all their cards
    new_cards: list = field(default_factory=list)        # approved written cards
    deck_name: str = ""
    notetype_name: str = ""
    field_appends: list = field(default_factory=list)    # [(nid, field, html), ...]
    # Gaps to mark done in the KG store once the write lands. Kept here rather
    # than done by the caller so "the cards committed" and "the gap closed" can
    # never come apart.
    close_item_ids: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.tag_ops or self.unsuspend_nids or self.new_cards
                    or self.field_appends or self.close_item_ids)

    def touched_nids(self) -> list:
        """Every note this plan writes to, deduped, order preserved — what the
        confirm dialog counts and what the audit tag goes on."""
        out, seen = [], set()
        for _tag, nids in self.tag_ops:
            for n in nids:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        for n in self.unsuspend_nids:
            if n not in seen:
                seen.add(n)
                out.append(n)
        for n, _f, _h in self.field_appends:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out


# ── the execution context ────────────────────────────────────────────────────

@dataclass
class RunContext:
    """What a stage needs to run, other than its input.

    The one field that earns its keep is `needs_main_thread`. core/api.py:841
    dispatches the manual/paste provider to qt_utils.manual_ai_complete, whose
    own docstring says "Must run on the main (UI) thread" — but there is no
    guard, and both READ (bulk_search runs triage via run_in_background) and
    FIND (the relevance judge, on run_pipeline's worker) are off-thread. So a
    paste user can currently Write but cannot Read or Find at all.

    Declaring it here means the runner picks the dispatch, once, instead of
    every stage remembering. Pure: this class decides nothing and calls nothing,
    it only reports.
    """
    cfg: dict = field(default_factory=dict)
    provider: str = "auto"
    cancel_ev: Any = None
    on_main: Callable | None = None
    # Per-run model overrides, stage key -> model id. Empty = resolve from the
    # AI matrix. This is how a re-run of ONE stage can be pushed to a bigger
    # model without touching the user's saved settings.
    dials: dict = field(default_factory=dict)

    @property
    def needs_main_thread(self) -> bool:
        return (self.provider or "").strip().lower() == "manual"

    def model_for(self, stage_key: str):
        if stage_key in self.dials:
            return self.dials[stage_key]
        return dial_for(stage_key, cfg=self.cfg or None)

    def cancelled(self) -> bool:
        ev = self.cancel_ev
        return bool(ev is not None and ev.is_set())


def dial_for(stage_key: str, *, family: str | None = None,
             cfg: dict | None = None):
    """The model for a stage, resolved ADDITIVELY.

    Order: the stage's own matrix key, then the legacy keys it replaces, then
    the family default. A user whose meta.json has never heard of "read" keeps
    the exact model `lecture_extract` is on right now — nothing is rewritten and
    nothing is reset, which is the standing rule for this add-on's config.

    Returns None for a non-AI stage.
    """
    st = stage(stage_key)
    if not st.dial:
        return None
    from ..core.config import (load_config, active_family, tool_model,
                               default_model_for)
    cfg = cfg if cfg is not None else load_config()
    family = family or active_family(cfg)
    matrix = cfg.get("ai_matrix") or {}
    for key in (st.dial,) + tuple(st.legacy_dials):
        m = tool_model(matrix.get(key) or {}, "model", family)
        if m:
            return m
    return default_model_for(family, cfg)


def skill_for(stage_key: str, *, family: str | None = None,
              cfg: dict | None = None) -> str:
    """The stage's configured skill invocation, same additive fall-through.
    "" when the stage has no skill or none is set."""
    st = stage(stage_key)
    if not st.dial:
        return ""
    from ..core.config import load_config, active_family, tool_model
    cfg = cfg if cfg is not None else load_config()
    family = family or active_family(cfg)
    matrix = cfg.get("ai_matrix") or {}
    for key in (st.dial,) + tuple(st.legacy_dials):
        s = tool_model(matrix.get(key) or {}, "skill", family)
        if s:
            return s
    return ""


# ── the run record ───────────────────────────────────────────────────────────

# Stage states, as WORDS. Never colour alone in the strip: grey reads as
# "rejected" when it means "not run yet", which is exactly the confusion this
# add-on has already been bitten by once.
PENDING, RUNNING, DONE, SKIPPED, FAILED = (
    "pending", "running", "done", "skipped", "failed")


@dataclass
class RunRecord:
    """One run, stage by stage — what the strip renders and what makes a run
    resumable BY STAGE rather than from the top.

    Persisting the stage outputs is what turns "re-run Find" from a rebuild of
    the whole pass into re-running one function, which is the single biggest
    reduction in what a run costs. Modelled on create_jobs' record: a plain
    JSON-safe dict that carries unknown keys through untouched, so a later
    stage can add a field without a migration.
    """
    run_id: str
    door: str = ""                              # which entry point opened it
    items: list = field(default_factory=list)   # list[WorkItem]
    status: dict = field(default_factory=dict)  # stage key -> one of the states
    counts: dict = field(default_factory=dict)  # stage key -> what to show ("14 points")
    errors: dict = field(default_factory=dict)  # stage key -> friendly message

    def __post_init__(self):
        for s in STAGES:
            self.status.setdefault(s.key, PENDING)

    def mark(self, stage_key: str, state: str, count: str = "",
             error: str = "") -> "RunRecord":
        stage(stage_key)                        # validate the key
        self.status[stage_key] = state
        if count:
            self.counts[stage_key] = count
        if error:
            self.errors[stage_key] = error
        return self

    def state_of(self, stage_key: str) -> str:
        return self.status.get(stage_key, PENDING)

    @property
    def current(self):
        """The stage the strip should highlight: the running one, else the
        first that hasn't finished, else None when the run is over."""
        for s in STAGES:
            if self.status.get(s.key) == RUNNING:
                return s
        for s in STAGES:
            if self.status.get(s.key) in (PENDING,):
                return s
        return None

    def to_json(self) -> dict:
        return {
            "run_id": self.run_id, "door": self.door,
            "items": [i.to_json() for i in self.items],
            "status": dict(self.status), "counts": dict(self.counts),
            "errors": dict(self.errors),
        }

    @classmethod
    def from_json(cls, d: dict) -> "RunRecord":
        d = dict(d or {})
        return cls(
            run_id=str(d.get("run_id") or ""), door=str(d.get("door") or ""),
            items=[WorkItem.from_json(i) for i in (d.get("items") or [])],
            status=dict(d.get("status") or {}), counts=dict(d.get("counts") or {}),
            errors=dict(d.get("errors") or {}),
        )


# ── adapters, for the migration ──────────────────────────────────────────────
#
# Stage A introduces the types without moving a single call site. These two
# functions are how both shapes coexist while each system is ported one at a
# time — the old path stays live until the new one is proven, which is the only
# way a re-plumb of seven surfaces stays verifiable.
#
# They are lossless in both directions. That's pinned by a test, because the
# whole safety argument for "contracts first" rests on it.

def plan_from_kgplan(kgplan, *, record: dict | None = None) -> Plan:
    """triage.KGPlan -> Plan. `record` is the gap's store record when the caller
    has it — a KGPlan carries identity but not content, so without it the
    item's fields{} (notes, stem_html, images) come back empty."""
    item = (WorkItem.from_kg(record) if record else
            WorkItem(id=getattr(kgplan, "kg_id", ""),
                     title=getattr(kgplan, "title", ""),
                     kind=KIND_KG,
                     type_key=getattr(kgplan, "kg_type", ""),
                     group=getattr(kgplan, "group", ""),
                     tags=list(getattr(kgplan, "tags", []) or [])))
    # A supplied record is the source of truth for content, but the PLAN is the
    # source of truth for identity: the user may have edited the tag or the
    # group in the plan table since the record was read.
    item.id = getattr(kgplan, "kg_id", "") or item.id
    item.title = getattr(kgplan, "title", "") or item.title
    item.type_key = getattr(kgplan, "kg_type", "") or item.type_key
    item.group = getattr(kgplan, "group", "") or item.group
    if getattr(kgplan, "tags", None):
        item.tags = list(kgplan.tags)
    return Plan(
        item=item,
        concept=getattr(kgplan, "concept", ""),
        scope=getattr(kgplan, "scope", "focused"),
        cards=getattr(kgplan, "cards", 0),
        breadth=getattr(kgplan, "breadth", "core"),
        depth_band=getattr(kgplan, "depth_band", "essentials"),
        points=list(getattr(kgplan, "points", []) or []),
        confidence=getattr(kgplan, "confidence", "high"),
        note=getattr(kgplan, "note", ""),
        constraints=list(getattr(kgplan, "constraints", []) or []),
        included=bool(getattr(kgplan, "included", True)),
    )


def kgplan_from_plan(plan: Plan):
    """Plan -> triage.KGPlan, for the call sites not ported yet. Imported
    lazily so this module stays free of the lecture package at import time."""
    from .kg.triage import KGPlan
    return KGPlan(
        kg_id=plan.kg_id, title=plan.title, kg_type=plan.kg_type,
        group=plan.group, concept=plan.concept, scope=plan.scope,
        cards=plan.cards, breadth=plan.breadth, depth_band=plan.depth_band,
        points=list(plan.points), confidence=plan.confidence, note=plan.note,
        constraints=list(plan.constraints), tags=list(plan.tags),
        included=plan.included,
    )


def plan_from_lecture(item: WorkItem, points: list, *, cards: int = 0,
                      constraints: list | None = None,
                      tags: list | None = None,
                      concept: str = "") -> Plan:
    """A lecture's extraction, wrapped in the per-item type it never had.

    The extractor returns a bare list[LearningPoint]. That list has nowhere to
    hang a card budget, the user's constraints, or the run's tag — which is
    precisely why lecture points could reach the FIND stage but never the WRITE
    stage: gen_prompt.build_batch reads all three off the plan. A lecture with
    a Plan can be generated for exactly like a knowledge gap.

    ONE Plan per lecture, not one per point. A lecture is a single work item
    that happens to decompose into dozens of points, the same way a broad gap
    ("Graves + Tx") decomposes into several — the wrapper is the item, and the
    points are what it decomposed into. Splitting it per point would lose the
    shared budget, which is the one thing the allocator needs.

    `cards` of 0 means "size it from the points", matching
    matcher.auto_target_cards' reasoning rather than a fixed default.
    """
    pts = list(points or [])
    if tags:
        item.tags = [t for t in tags if t]
    return Plan(
        item=item,
        concept=concept or item.title,
        # A lecture is broad by definition — it's a whole teaching session.
        scope="broad",
        cards=int(cards) if cards else 0,
        breadth="wide",
        depth_band="essentials",
        points=pts,
        constraints=list(constraints or []),
    )
