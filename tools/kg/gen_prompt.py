# Panel-free card-generation requests — what Mass AI Create builds instead of
# driving the Create panel's form.
#
# The whole generation prompt currently lives inside CreatorPanel._on_generate:
# ~400 lines of widget reads, source ingestion (URL / PDF / PPTX / slide render),
# and prompt assembly, all interleaved. Mass create needs the assembly and none
# of the rest — it always generates from a TOPIC (the gap's concept plus its
# points), never from a source document.
#
# So this is a LEAN RE-ASSEMBLY, not a move. Every piece of prompt text is
# IMPORTED from card_creator, so there is exactly one definition of what a card
# request says and tuning it in Settings still tunes both paths. What's dropped
# is only the legs a KG can't have:
#
#   no URL fetch, no PDF/PPTX ingestion, no slide mode, no lecture-match,
#   no auto-image query
#
# Source grounding IS here, though it was once on that list. The reason it was
# excluded — "topic grounding keys off a typed topic and a per-batch checkbox
# that doesn't exist here" — turned out to be a description of the panel's
# plumbing rather than of the feature. The gap's concept is a typed topic by
# any other name, and the per-batch checkbox only ever reflected the global
# toggle plus the notetype override, both of which are readable from here. So
# a card written for a gap now cites the same guideline allow-list a card
# written in AI Create does, and the two stop disagreeing about what a real
# source is.
#
# That's what keeps this file short enough to read, and keeps the refactor of
# card_creator down to one optional kwarg.
#
# PURE: no Qt, no mw.col, no AI calls, NO NETWORK. Everything it touches is
# config or plain data, so it is safe to call from the dialog's main thread
# before enqueueing. That is why live guideline fetching isn't done here but
# handed to the worker as `grounding_live` — twelve gaps is twelve blocking
# HTTP round trips, and this runs while the user is looking at the window.

from __future__ import annotations

import re
import uuid

from ...core import log
from ...core.config import load_config, tool_config
from . import engine
from . import triage as kg_triage


def _constraints_block(constraints: list) -> str:
    """The student's own directives, restated as hard rules for the writer.

    Triage reads these ("do not focus on dx") and shapes the points around them,
    then the points are all the writer ever sees — so without this block the
    constraint is obeyed once and forgotten. Placed FIRST in the user message and
    stated as overriding, because it competes with a system prompt that is
    otherwise busily telling the model to cover an entity comprehensively.
    """
    lines = [str(c or "").strip() for c in (constraints or [])]
    lines = [c for c in lines if c]
    if not lines:
        return ""
    return (
        "THE STUDENT'S OWN INSTRUCTIONS FOR THIS TOPIC — these OVERRIDE every "
        "other instruction about what to cover, including any guidance to be "
        "comprehensive:\n"
        + "\n".join(f"- {c}" for c in lines)
        + "\nWrite no card that breaches these, even where the material would "
          "normally demand it.\n\n"
    )


def _points_block(points: list, n_cards: int = 0) -> str:
    """The gap's learning points, as the coverage list the model must satisfy.

    This is the heart of what mass create does differently from the panel. The
    panel asks for "N cards on TOPIC" and lets the model decide what they cover;
    here the points were already triaged (and edited by the user), so they're
    handed over as a requirement. One call per gap rather than one per point,
    deliberately: the model can only avoid writing the same card twice if it can
    see every point it's covering in one go.
    """
    concepts = [str(getattr(p, "concept", "") or "").strip() for p in (points or [])]
    concepts = [c for c in concepts if c]
    if not concepts:
        return ""

    # THE BUDGET MUST NEVER CONTRADICT THE COVERAGE LIST.
    # Asking for 4 cards while listing 14 points that each demand "at least one
    # card" is an impossible instruction, and the model resolves it by picking
    # arbitrarily — which is how a 4-card "dialysis modalities" gap came back as
    # four cards about haemodialysis and nothing at all about peritoneal.
    # So when the budget is smaller than the list, the list is cut to fit and the
    # rule becomes one card each, exactly.
    trimmed = False
    if n_cards and len(concepts) > n_cards:
        concepts = concepts[:n_cards]
        trimmed = True

    lines = "\n".join(f"{i}. {c}" for i, c in enumerate(concepts, 1))
    if trimmed:
        head = ("\nYour card budget is smaller than this gap decomposes into, so "
                "these are the points that made the cut, in priority order. Write "
                "EXACTLY ONE card for each — spending two cards on one point means "
                "another point gets none:\n")
    else:
        head = ("\nCover these specific points — write at least one card for each, "
                "and do NOT write two cards testing the same claim:\n")
    # ATTRIBUTION. Without this the reply is N cards with no statement of which
    # point each answers, and the review screen can only file them under the gap
    # as a lump. Position is not a substitute: one point legitimately splits into
    # two cards (a "symptoms" point becoming a genital card and a urinary card),
    # after which every later card is off by one. So the model is asked to say it
    # — one integer, from the list above — and anything unattributed still falls
    # back to the gap. An unknown extra key is ignored everywhere downstream
    # (resolve_card_validity only requires front/extra), so this cannot break a
    # reply from a model that omits it.
    tail = ("\nAdd a \"point\" key to every card object: the NUMBER from the list "
            "above that the card answers (e.g. \"point\": 2). If a card covers a "
            "point not on the list, use 0.\n")
    return head + lines + tail


def build_generation_request(*, kg: dict, plan, profile: dict, cfg: dict,
                             focus: str = "", n_cards: int | None = None):
    """Assemble one gap's generation request.

    Returns a dict of everything the job record needs — the two prompts plus the
    merge/want flags that tell the worker how to route the reply. Mirrors
    CreatorPanel._on_generate's topic-mode branch (card_creator.py:2669-2827);
    see the module docstring for what's deliberately absent.
    """
    from .. import card_creator as cc

    n = int(n_cards if n_cards is not None else kg_triage.clamp_cards(plan.cards))
    topic_label = (getattr(plan, "concept", "") or "").strip() or str(kg.get("title") or "").strip()

    is_qa = str((profile or {}).get("card_format") or "").lower() == "qa"
    card_word = "Q&A" if is_qa else "cloze"

    # ── user message ────────────────────────────────────────────────────────
    user_msg = (
        _constraints_block(getattr(plan, "constraints", []))
        + f"Generate {n} {card_word} cards on the TOPIC: {topic_label}\n"
        + _points_block(getattr(plan, "points", []), n)
        + cc._topic_directive(topic_label, n) + "\n"
        # The band is the dial's depth half — see _breadth_directive(band=).
        + cc._breadth_directive(n, "topic", band=getattr(plan, "depth_band", None)) + "\n"
        + (cc._focus_directive(focus) + "\n" if focus else "")
    )

    # ── system prompt ───────────────────────────────────────────────────────
    base_system = cc.QA_GEN_SYSTEM if is_qa else cc.CARD_GEN_SYSTEM
    system_prompt = cc._augment_system(base_system, profile)
    if not is_qa:
        system_prompt += cc._gen_atomicity_note(cc._atomicity_max(cfg))

    # ── source grounding ────────────────────────────────────────────────────
    # The same gate AI Create uses: the global toggle, overridden per notetype.
    # Mode is always "topic" here — a gap is a subject, never a pasted document
    # — which is exactly the case grounding is for.
    #
    # Fail-open throughout. A guideline block that won't build is worth an
    # ungrounded card, not a gap that doesn't get written at all.
    grounding_live = None
    try:
        if cc._grounding_active(cfg, profile, "topic") and topic_label:
            # The gap's own tags are the specialty signal — the same list the
            # panel passes from its tag box.
            match_tags = [str(t) for t in (kg.get("tags") or []) if str(t).strip()]
            if cc._grounding_fetch_live(cfg):
                # Deferred, not skipped: the worker builds this one off-thread.
                grounding_live = {"topic": topic_label, "tags": match_tags}
            else:
                from ...grounding import guidelines as grounding
                block = grounding.build_guidelines_block(
                    topic_label, match_tags, fetch_content=False)
                if block:
                    system_prompt += "\n\n" + block
    except Exception as e:
        log.error(f"mass create: grounding injection skipped: {e}")

    skill_invocation, skill_id = cc._resolve_skill(profile)
    no_skill = not skill_id and not skill_invocation

    # ── reply-routing flags ─────────────────────────────────────────────────
    kg_type = str(kg.get("kg_type") or kg.get("type") or "").lower()
    type_meta = cc._kg_type_meta(kg_type)
    want_auto = bool(cfg.get("auto_tag", True))
    want_tag = bool(want_auto and type_meta is not None)
    cached_tag = (kg.get("auto_tag") or "").strip() if want_auto else ""
    merge_tag = want_tag and not cached_tag and no_skill
    want_explanation = cc._wants_mq_explanation(kg)
    merge_explanation = want_explanation and no_skill

    qpc = cfg.get("quality_pass") or {}
    merge_grade = (
        cc._quality_pass_active(cfg, profile)
        and qpc.get("grading_mode") == "same_call"
        and no_skill
    )

    gen_plan = None
    if no_skill:
        exclude = set() if want_explanation else {"explanation"}
        gen_plan, req_fields = engine.plan_for(
            type_meta, "create", want_cards=True, want_terms=False,
            want_tag=merge_tag, want_grade=merge_grade, exclude=exclude)
        grade_instr = ""
        if merge_grade:
            from .. import quality_pass as qp
            grade_instr = qp.merged_grade_instructions(cc._atomicity_max(cfg))
        obj = engine.build_object_instructions(gen_plan, req_fields,
                                               grade_instructions=grade_instr)
        if obj:
            system_prompt += obj
        if req_fields:
            refs = engine.build_refs_context(req_fields, kg)
            if refs:
                user_msg += "\n\nCONTEXT for the AI-written fields:\n" + refs

    tag_material = cc._tag_material(kg, topic_label, None, focus)
    if merge_tag:
        ctx = []
        if str(kg.get("title") or "").strip():
            ctx.append(f"Title: {str(kg['title']).strip()}")
        stem = re.sub(r"<[^>]+>", " ", str(kg.get("stem_html") or ""))
        stem = re.sub(r"\s+", " ", stem).strip()
        if stem:
            ctx.append(f"Question/stem: {stem[:600]}")
        if str(kg.get("notes") or "").strip():
            ctx.append(f"Notes: {str(kg['notes']).strip()[:300]}")
        if ctx:
            user_msg += "\n\nCLASSIFY THIS MATERIAL for the tag:\n" + "\n".join(ctx)

    return {
        "user_msg": user_msg,
        "system_prompt": system_prompt,
        "gen_plan": gen_plan,
        "note_field_keys": list(gen_plan.note_fields) if gen_plan is not None else [],
        "type_meta": type_meta,
        "card_word": card_word,
        "topic_label": topic_label,
        "n_cards": n,
        "skill_id": skill_id,
        "skill_invocation": skill_invocation,
        "want_tag": want_tag,
        "want_explanation": want_explanation,
        "merge_tag": merge_tag,
        "merge_explanation": merge_explanation,
        "merge_grade": merge_grade,
        "cached_tag": cached_tag,
        "tag_material": tag_material,
        "max_tokens": max_tokens_for(n),
        # None unless live fetching is on; see the grounding block above.
        "grounding_live": grounding_live,
    }


def max_tokens_for(n: int) -> int:
    """Reply budget for a request of `n` cards.

    The panel has always hardcoded 4096 (card_creator._make_job_record), which is
    ample for its default of 3-10 cards and quietly fatal past ~30: the reply is
    truncated mid-JSON, nothing parses, and the job fails with a generic "the AI
    reply wasn't in the expected card format". Mass create's "deep" band asks for
    22 cards as a matter of routine, each with an Extra field and a folded grade
    verdict, so it would hit that constantly.

    ~500 tokens per card plus headroom for the folded tag/explanation/grades.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(4096, min(16000, 1500 + 500 * n))


def build_job_record(*, req: dict, kg: dict, plan, batch_id: str,
                     deck_name: str, notetype_name: str, tags_raw: list[str],
                     model: str | None) -> dict:
    """A create_jobs record for one gap, built without a live CreatorPanel.

    Deliberately the same shape CreatorPanel._make_job_record produces
    (card_creator.py:3302) so create_jobs._worker can't tell the difference —
    every source-mode key is present and empty rather than absent, because the
    worker reads them unconditionally.

    The three extra keys (batch_id, kg_id, mass_points) ride through untouched:
    create_jobs._normalise starts from dict(item), so the store carries unknown
    keys without needing to know about them.
    """
    job_id = uuid.uuid4().hex[:12]
    kg_type = str(kg.get("kg_type") or kg.get("type") or "").lower()
    is_mq = kg_type == "mq"
    return {
        "id": job_id,
        "batch_id": batch_id,
        "kg_id": str(kg.get("kg_id") or kg.get("id") or ""),
        # The points this job was asked to cover, so a restarted session can show
        # what a job was for without re-reading the gap.
        "mass_points": [str(getattr(p, "concept", "") or "")
                        for p in (getattr(plan, "points", []) or [])],
        "title": req["topic_label"] or str(kg.get("title") or "Generation"),
        "mode": "topic",
        "source_label": None,
        "topic_label": req["topic_label"],
        "focus": "",
        "deck_name": deck_name,
        "notetype_name": notetype_name,
        "profile_name": notetype_name,
        "tags_raw": list(tags_raw),
        "kg_image_filenames": list(kg.get("images") or []),
        "kg_type_for_image": kg_type,
        "kg_stem_html": str(kg.get("stem_html") or ""),
        "kg_notes": str(kg.get("notes") or ""),
        "kg_concept": str(kg.get("concept") or "").strip() if is_mq else "",
        "kg_explanation": str(kg.get("explanation") or "").strip() if is_mq else "",
        "kg_resources": list(kg.get("resources") or []),
        # No panel, no attachments, no slides — mass create is topic-only.
        "panel_image_filenames": [],
        "attachment_paths": [],
        "slide_pages": [],
        "slide_mode": False,
        "lecture_match": False,
        "page_texts": [],
        "card_word": req["card_word"],
        "user_msg": req["user_msg"],
        "system_prompt": req["system_prompt"],
        "model": model,
        "skill_id": req["skill_id"],
        "skill_invocation": req["skill_invocation"],
        "max_tokens": req["max_tokens"],
        "merge_tag": bool(req["merge_tag"]),
        "merge_explanation": bool(req["merge_explanation"]),
        "merge_grade": bool(req["merge_grade"]),
        "want_tag": bool(req["want_tag"]),
        "want_explanation": bool(req["want_explanation"]),
        "cached_tag": req["cached_tag"],
        # Set only when "fetch live" is on. create_jobs._worker builds the
        # guideline block from it on its own thread and appends it to the
        # system prompt there — the fetch is network I/O and must not happen
        # on the main thread, where this record is assembled.
        "grounding_live": req.get("grounding_live"),
        # want_auto drives a SEPARATE classification call in the worker, and is
        # only for skill flows that forced a bare reply. With no skill the tag is
        # folded into the object reply (merge_tag) and this must stay off or the
        # gap is classified twice.
        "want_auto": bool(req["want_tag"] and not req["merge_tag"]),
        "tag_material": req["tag_material"],
        "note_field_keys": list(req["note_field_keys"]),
        "type_meta": req["type_meta"],
        "gap": dict(kg),
        "provider_label": (load_config().get("provider") or "auto"),
    }


def build_batch(*, plans, gaps_by_id: dict, deck_name: str, notetype_name: str,
                base_tags: list[str], tags_by_kg: dict, model: str | None,
                focus: str = "", origin: str = "mass-create",
                batch_id: str = "") -> list[dict]:
    """Every included, runnable plan turned into a job record.

    Fail-soft per gap: one gap whose prompt can't be built (a broken KG type, a
    profile referencing a deleted notetype) is logged and skipped rather than
    taking the whole batch down — the same rule the rest of the add-on follows.
    """
    cfg = tool_config("card_creator")
    profile = _profile_for(cfg, notetype_name)
    # An explicit id joins these records to a batch already on screen — a
    # second write for one point, asked for after unticking the cards the
    # search found. A new batch would poll separately and render nowhere.
    batch_id = batch_id or uuid.uuid4().hex[:12]

    records: list[dict] = []
    for plan in plans:
        if not getattr(plan, "included", True) or not getattr(plan, "ok", False):
            continue
        kg = gaps_by_id.get(plan.kg_id)
        if not kg:
            log.warn(f"mass create: gap {plan.kg_id} vanished before enqueue")
            continue
        try:
            req = build_generation_request(kg=kg, plan=plan, profile=profile,
                                           cfg=cfg, focus=focus)
            tags = _merge_tags(base_tags, tags_by_kg.get(plan.kg_id), kg)
            rec = build_job_record(
                req=req, kg=kg, plan=plan, batch_id=batch_id,
                deck_name=deck_name, notetype_name=notetype_name,
                tags_raw=tags, model=model)
            # Which dialog owns this batch. AI Create shows every live batch so
            # one survives a restart, but a work-through batch belongs to a
            # screen that also holds the SEARCH results — offering it as an
            # ordinary "Mass AI Create" run is how the same gaps ended up being
            # generated a second time.
            rec["origin"] = origin
            records.append(rec)
        except Exception as e:
            log.error(f"mass create: couldn't build a request for "
                      f"'{str(kg.get('title') or '')[:40]}': {e}")
    return records


def _profile_for(cfg: dict, notetype_name: str) -> dict:
    from .. import card_creator as cc
    return cc._resolved_profile(cfg, (notetype_name or "").strip())


def _merge_tags(base_tags, row_tag, kg: dict) -> list[str]:
    """Batch tags + this row's override + the gap's own curated tags, deduped,
    order preserved. The auto-tag is NOT added here — it's resolved after
    generation (the model returns the tag levels), and create_jobs folds it in."""
    out: list[str] = []
    seen: set[str] = set()
    for t in list(base_tags or []) + [row_tag] + list(kg.get("tags") or []):
        t = str(t or "").strip()
        # Anki matches tags case-insensitively, so two spellings of one tag are
        # one tag with twice the clutter. First spelling wins.
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out
