# Background card-generation jobs: persistent store + background runner.
#
# A "job" is one AI card-generation request fired from the Create panel. With
# background generation on, _on_generate enqueues a job instead of blocking; the
# runner generates (and grades) it off the main thread, and the finished cards
# land in this store as a "ready" job the user reviews whenever they like.
#
# Store layout mirrors tools/kg/store.py exactly (JSON list in user_files/),
# fail-open: read/write errors print and degrade, never crash Anki.
#
# Statuses:
#   queued       — accepted, waiting for a runner slot
#   running       — a background worker is generating it now
#   ready         — cards generated (+ graded); awaiting the user's review
#   error         — generation/grading failed (carries an `error` string)
#   interrupted   — was queued/running when Anki last quit; work was abandoned
#
# The runner half lives at the bottom of this file (anchored on mw so it
# survives the Ankisstant window closing). This module is import-safe with no
# Anki present for the pure store functions (the runner imports aqt lazily).

from __future__ import annotations

from ..core import jsonfile
import os
import shutil
import uuid
from datetime import datetime


# ── paths ─────────────────────────────────────────────────────────────────────

def _user_files() -> str:
    # This file is tools/create_jobs.py → dirname(here) is the addon root, which
    # holds user_files/. (tools/kg/store.py is one level deeper, hence its extra
    # dirname — do NOT copy that depth here.)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    uf = os.path.join(root, "user_files")
    os.makedirs(uf, exist_ok=True)
    return uf


def _store_path() -> str:
    return os.path.join(_user_files(), "create_jobs.json")


def attachments_dir(job_id: str) -> str:
    """Per-job folder for copied PDF/PPTX attachments, so a queued job survives a
    restart even if the user's original files move."""
    d = os.path.join(_user_files(), "job_attachments", job_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── load / save ───────────────────────────────────────────────────────────────

QUEUED, RUNNING, READY, ERROR, INTERRUPTED = (
    "queued", "running", "ready", "error", "interrupted")
VALID_STATUSES = {QUEUED, RUNNING, READY, ERROR, INTERRUPTED}
ACTIVE_STATUSES = {QUEUED, RUNNING}


def load_all() -> list[dict]:
    data = jsonfile.read(_store_path(), [])
    return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []


def save_all(items: list[dict]) -> None:
    jsonfile.write(_store_path(), items, indent=2)


# ── normalisation ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalise(item: dict) -> dict:
    """Fill defaults / coerce types so callers can pass partial dicts. Keeps the
    record JSON-safe and guarantees the fields the UI/runner read exist."""
    out = dict(item)
    out.setdefault("id", uuid.uuid4().hex[:12])
    st = str(out.get("status") or QUEUED)
    out["status"] = st if st in VALID_STATUSES else QUEUED
    out["title"] = str(out.get("title", "")).strip()
    out.setdefault("error", "")
    out["slide_mode"] = bool(out.get("slide_mode", False))
    out["lecture_match"] = bool(out.get("lecture_match", False))
    out.setdefault("card_word", "cloze")
    # List-shaped fields the UI/worker iterate over.
    for key in ("tags_raw", "kg_image_filenames", "panel_image_filenames",
                "attachment_paths", "slide_pages", "page_texts"):
        v = out.get(key)
        out[key] = list(v) if isinstance(v, (list, tuple)) else []
    out.setdefault("created_at", _now())
    out["updated_at"] = _now()
    return out


# ── CRUD ────────────────────────────────────────────────────────────────────────

def get(job_id: str) -> dict | None:
    for it in load_all():
        if it.get("id") == job_id:
            return it
    return None


def add(**fields) -> dict:
    item = _normalise(fields)
    items = load_all()
    items.append(item)
    save_all(items)
    return item


def update(job_id: str, **fields) -> dict | None:
    items = load_all()
    for i, it in enumerate(items):
        if it.get("id") != job_id:
            continue
        merged = dict(it)
        merged.update(fields)
        items[i] = _normalise(merged)
        save_all(items)
        return items[i]
    return None


def remove(job_id: str) -> None:
    save_all([it for it in load_all() if it.get("id") != job_id])
    # Best-effort cleanup of any copied attachments for this job.
    try:
        d = os.path.join(_user_files(), "job_attachments", job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# ── counts / queries ───────────────────────────────────────────────────────────

def ready_count() -> int:
    return sum(1 for it in load_all() if it.get("status") == READY)


# ── startup cleanup ─────────────────────────────────────────────────────────────

_startup_done = False


def mark_interrupted_on_startup() -> int:
    """Once per session: any job still queued/running from a previous session had
    its in-flight work abandoned when Anki quit — mark it interrupted so it shows
    as recoverable (Retry) rather than silently stuck. Returns how many changed."""
    global _startup_done
    if _startup_done:
        return 0
    _startup_done = True
    items = load_all()
    n = 0
    changed = False
    for it in items:
        if it.get("status") in ACTIVE_STATUSES:
            it["status"] = INTERRUPTED
            it["updated_at"] = _now()
            n += 1
            changed = True
    if changed:
        save_all(items)
    return n


# ══ background runner ═══════════════════════════════════════════════════════════
#
# Anchored on `mw` so it outlives the Ankisstant window. The worker runs OFF the
# main thread: it may touch only plain data + core_api (no Qt, no mw.col). All
# Anki/Qt work (auto-tag resolve, KG persist, toast, badge, panel refresh) is
# done in the main-thread done-callback. The manual/paste provider is interactive
# and NEVER enters the worker (its paste dialog needs the main thread).


class _Cancelled(Exception):
    pass


def max_parallel_jobs() -> int:
    try:
        from ..core.config import tool_config
        return max(1, int(tool_config("card_creator").get("max_parallel_jobs", 2)))
    except Exception:
        return 2


class _Runner:
    def __init__(self):
        self._running = 0
        self._inflight: dict = {}      # job_id -> threading.Event
        self._cancelled: set = set()   # job_ids cancelled while running

    # ── enqueue / pump ──────────────────────────────────────────────────────
    def enqueue(self, record: dict) -> dict:
        job = add(**{**record, "status": QUEUED})
        self.pump()
        return job

    def pump(self) -> None:
        import threading
        from aqt import mw
        cap = max_parallel_jobs()
        while self._running < cap:
            nxt = next((it for it in load_all() if it.get("status") == QUEUED), None)
            if nxt is None:
                return
            job_id = nxt["id"]
            update(job_id, status=RUNNING)
            ev = threading.Event()
            self._inflight[job_id] = ev
            self._running += 1
            rec = get(job_id) or nxt
            # uses_collection=False is not optional: the default is taskman's
            # single-worker _collection_executor, which Anki's periodic backup
            # (and every other collection op) queues behind — so a "background"
            # generation would hang the app for its whole multi-minute run, and
            # max_parallel_jobs would silently serialise to 1.
            mw.taskman.run_in_background(
                lambda r=rec, e=ev: _worker(r, e),
                lambda fut, jid=job_id: self._done(jid, fut),
                uses_collection=False,
            )

    # ── completion (main thread) ────────────────────────────────────────────
    def _done(self, job_id: str, fut) -> None:
        self._running = max(0, self._running - 1)
        self._inflight.pop(job_id, None)
        cancelled = job_id in self._cancelled
        self._cancelled.discard(job_id)
        outcome = "ready"
        n_cards = 0
        try:
            if cancelled:
                remove(job_id)
                outcome = "cancelled"
            else:
                result = fut.result()
                _apply_result_main_thread(job_id, result)
                n_cards = len(result.get("cards") or [])
        except _Cancelled:
            remove(job_id)
            outcome = "cancelled"
        except Exception as e:
            print(f"[ankisstant] job {job_id} failed: {e}")
            update(job_id, status=ERROR, error=str(e))
            outcome = "error"
        _toast_outcome(outcome, n_cards, (get(job_id) or {}).get("origin", ""))
        self.pump()

    # ── cancel ──────────────────────────────────────────────────────────────
    def cancel(self, job_id: str) -> None:
        ev = self._inflight.get(job_id)
        if ev is not None:           # running → signal the worker, drop on return
            self._cancelled.add(job_id)
            ev.set()
        else:                        # still queued → never started
            remove(job_id)


def _runner():
    """The process-wide runner, lazily created and stashed on mw so it survives
    the Ankisstant window opening/closing."""
    from aqt import mw
    r = getattr(mw, "_ankisstant_job_runner", None)
    if r is None:
        r = _Runner()
        mw._ankisstant_job_runner = r
    return r


# ── public runner API ──────────────────────────────────────────────────────────

def enqueue(record: dict) -> dict:
    return _runner().enqueue(record)


def cancel(job_id: str) -> None:
    _runner().cancel(job_id)


# ── batches (Mass AI Create) ───────────────────────────────────────────────────
#
# A "batch" is nothing new to the runner: it's N ordinary jobs that happen to
# share a batch_id, which rides through _normalise untouched because that starts
# from dict(item). So parallelism, cancellation, restart recovery and retry are
# all the existing machinery, and the mass-create dialog just needs to address
# its own jobs as a group.

def by_batch(batch_id: str) -> list[dict]:
    """Every job in a batch, in creation order. The dialog polls this rather
    than subscribing to the runner: it works whether or not the dialog is alive,
    which is what makes reopening a batch after a restart the same code path as
    watching one live."""
    if not batch_id:
        return []
    return [it for it in load_all() if it.get("batch_id") == batch_id]


def enqueue_batch(records: list[dict]) -> list[dict]:
    """Add every record, then pump ONCE.

    Deliberately not a loop of enqueue(): that pumps per record, and each pump
    walks the whole store looking for the next queued job. For a 12-gap batch
    that's 12 full passes before any work starts, on the main thread, while the
    user waits on a dialog that hasn't switched pages yet.
    """
    jobs = [add(**{**rec, "status": QUEUED}) for rec in (records or [])]
    if jobs:
        _runner().pump()
    return jobs


def cancel_batch(batch_id: str) -> int:
    """Stop everything still queued or running in this batch. Jobs that already
    finished are LEFT ALONE — a Stop half way through a run should keep what it
    has already paid for, and those cards are what the review page shows."""
    n = 0
    for job in by_batch(batch_id):
        if job.get("status") in (QUEUED, RUNNING):
            _runner().cancel(job["id"])
            n += 1
    return n


def remove_batch(batch_id: str) -> None:
    """Drop a whole batch's records — used after its cards have been committed,
    so a finished batch doesn't linger in the Create panel's counts."""
    for job in by_batch(batch_id):
        remove(job["id"])


def drop_work_through_leftovers() -> int:
    """Startup only: remove every Find & write batch left from a past session.

    A Find & write run's written cards exist only for that window. Once it
    has closed they are either in your collection or thrown away, so anything
    still on disk at startup is a leftover — from a crash, a quit with the
    window hidden, or a build that used to park them in AI Create. Returns how
    many job records were dropped."""
    bids = {it.get("batch_id") for it in load_all()
            if it.get("origin") == "work-through" and it.get("batch_id")}
    n = 0
    for bid in bids:
        n += len(by_batch(bid))
        remove_batch(bid)
    return n


def batch_progress(batch_id: str) -> dict:
    """Counts by state, for the running page's progress bar and status line.

    `done` is the bar's numerator: an errored job is finished as far as the run
    is concerned (it won't progress further without a retry), so a batch with
    one permanent failure still reaches 100% instead of hanging at 11/12.
    """
    jobs = by_batch(batch_id)
    counts = {"total": len(jobs), "queued": 0, "running": 0,
              "ready": 0, "error": 0, "interrupted": 0, "cards": 0}
    for j in jobs:
        st = j.get("status")
        if st == QUEUED:
            counts["queued"] += 1
        elif st == RUNNING:
            counts["running"] += 1
        elif st == READY:
            counts["ready"] += 1
            counts["cards"] += len(j.get("cards") or [])
        elif st == ERROR:
            counts["error"] += 1
        elif st == INTERRUPTED:
            counts["interrupted"] += 1
    counts["done"] = counts["ready"] + counts["error"] + counts["interrupted"]
    counts["finished"] = counts["total"] > 0 and counts["done"] >= counts["total"]
    return counts


def retry_job(job_id: str) -> None:
    """Re-queue an interrupted/errored job. The full AI request is persisted, so
    no rebuild is needed."""
    rec = get(job_id)
    if rec is None or rec.get("status") not in (INTERRUPTED, ERROR):
        return
    update(job_id, status=QUEUED, error="")
    _runner().pump()


# ── worker (OFF main thread — no Qt, no mw.col) ─────────────────────────────────

def _worker(rec: dict, cancel_ev) -> dict:
    from ..core import api as core_api
    from . import card_creator as cc
    from ..core.config import tool_config
    core_api.set_cancel_event(cancel_ev)
    # Live guideline grounding, if the job asked for it. Done HERE and not where
    # the prompt was assembled because it is blocking HTTP, once per source, and
    # the assembly runs on the main thread with the user watching the window. A
    # failure leaves the card ungrounded rather than unwritten.
    gl = rec.get("grounding_live")
    if isinstance(gl, dict):
        try:
            from ..grounding import guidelines as _grounding
            block = _grounding.build_guidelines_block(
                str(gl.get("topic") or ""),
                [str(t) for t in (gl.get("tags") or [])],
                fetch_content=True)
            if block:
                # A copy: `rec` is the caller's job record, and a worker that
                # rewrote it would leave the fetched page text sitting in the
                # job store for every later read of that job.
                rec = dict(rec)
                rec["system_prompt"] = (rec.get("system_prompt") or "") + "\n\n" + block
        except Exception as e:
            print(f"[ankisstant] live grounding skipped: {e}")
    try:
        if rec.get("slide_mode"):
            # Slide-by-slide: one AI call per rendered page (basename-sorted so
            # page order survives a restart). Each card is tagged with its exact
            # source slide by the helper; nothing is folded, so there's no engine
            # route — tag/grade happen in the shared tail below.
            slide_pages = sorted(
                (p for p in (rec.get("slide_pages") or []) if p and os.path.exists(p)),
                key=lambda p: os.path.basename(p))
            cards = cc.generate_cards_per_slide(
                slide_pages, system=rec.get("system_prompt", ""),
                card_word=rec.get("card_word", "cloze"), model=rec.get("model"),
                skill_id=rec.get("skill_id", ""),
                skill_invocation=rec.get("skill_invocation", ""),
                cancel_check=cancel_ev.is_set)
            if cancel_ev.is_set():
                raise _Cancelled()
            merged_tag_levels = None
            same_call_grades = None
            mq_explanation = ""
            ai_field_values = {}
            if not cc.resolve_card_validity(cards):
                raise RuntimeError("no cards were generated from that slide deck.")
        else:
            reply = core_api.ask_claude_json(
                prompt=rec.get("user_msg", ""), system=rec.get("system_prompt", ""),
                max_tokens=int(rec.get("max_tokens", 4096) or 4096),
                model=rec.get("model"),
                skill_id=rec.get("skill_id", ""),
                skill_invocation=rec.get("skill_invocation", ""),
                attachments=rec.get("attachment_paths") or None,
                show_errors=False,
            )
            if cancel_ev.is_set():
                raise _Cancelled()
            if reply is None:
                raise RuntimeError("the AI returned nothing (see console).")
            # Route the one-call reply with the engine, using the plan the modal
            # serialised into the record (note_field_keys + the merge flags). This is
            # the SAME routing the interactive path uses, so background and modal
            # generations land identically.
            from .kg import engine
            plan = engine.RequestPlan(
                note_fields=list(rec.get("note_field_keys") or []),
                want_tag=bool(rec.get("merge_tag")),
                want_grade=bool(rec.get("merge_grade")),
                want_cards=True)
            routed = engine.route(reply, plan)
            cards = routed.cards
            merged_tag_levels = routed.tag_levels
            same_call_grades = routed.grades
            mq_explanation = str(routed.field_values.get("explanation") or "")
            ai_field_values = routed.field_values
            if not cc.resolve_card_validity(cards):
                raise RuntimeError("the AI reply wasn't in the expected card format.")
            # Text-first lecture: attach each card to its source slide by matching
            # the card back to the page text (deterministic, no AI). slide_pages
            # are restored when the review dialog opens, so _sync_slide_images
            # then pulls in the matched slide's image.
            if rec.get("lecture_match") and rec.get("page_texts"):
                cc.match_cards_to_slides(cards, rec.get("page_texts") or [])
        # Auto-tag in skill flows: a skill forces a bare-array reply with no
        # folded tag levels, so classify the material with a small dedicated
        # call (Qt-free, safe off the main thread). Same shared classifier AI
        # Browse uses, so background generations tag identically to the modal.
        if rec.get("want_auto") and not merged_tag_levels:
            from . import autotag
            merged_tag_levels = autotag.classify(
                rec.get("tag_material", ""), model=rec.get("model"))
        # FRONT EMPHASIS repair, before grading and before the review screen —
        # a card the model left unbolded gets its anchor back from the point it
        # was written for. Deterministic and local; see card_creator's
        # ensure_front_emphasis for why prompting alone doesn't finish the job.
        cfg = tool_config("card_creator")
        profile = cc._resolved_profile(cfg, rec.get("profile_name", ""))
        # LENGTH NET, before emphasis and grading: over-long fronts and the
        # story cards past the 1-in-5 allowance are rewritten in one call, so
        # what reaches the review screen is already AnKing-length. Cloze only
        # (tighten_cards no-ops on a Q&A profile); fail-open.
        if str(rec.get("card_word") or "cloze").lower() != "q&a":
            cards, tightened = cc.tighten_cards(cards, profile, rec.get("model"))
            if cancel_ev.is_set():
                raise _Cancelled()
            if tightened:
                # Folded grades were written against the cards as they were;
                # positions have moved (a split adds a card), so grade afresh.
                same_call_grades = None
        cc.ensure_front_emphasis_for_job(cards, rec)
        verdicts = cc.grade_cards_sync(
            cards, profile, cfg, same_call_grades=same_call_grades)
        return {
            "cards": cards,
            "merged_tag_levels": merged_tag_levels,
            "mq_explanation": mq_explanation,
            "ai_field_values": ai_field_values,
            "verdicts": [_verdict_to_dict(v) for v in verdicts] if verdicts else None,
        }
    finally:
        core_api.set_cancel_event(None)


def _verdict_to_dict(v):
    """Serialise a qp.Verdict (or None) to a plain dict for JSON storage."""
    if v is None:
        return None
    try:
        from dataclasses import asdict
        return asdict(v)
    except Exception:
        return None


# ── result handling (main thread) ───────────────────────────────────────────────

def _apply_result_main_thread(job_id: str, result: dict) -> None:
    """Fold the worker's plain-data result into a `ready` record, doing the bits
    that need Anki/mw.col here on the main thread (auto-tag, KG persist)."""
    from . import card_creator as cc
    rec = get(job_id)
    if rec is None:
        return
    cards = result.get("cards") or []
    tags_raw = list(rec.get("tags_raw") or [])

    # Resolve the auto-tag the same way _finalize_review does: cached tag, or
    # the levels — which the worker produced either by folding them into the
    # card reply, or (skill flows) via a separate classification call.
    try:
        gap = rec.get("gap") if isinstance(rec.get("gap"), dict) else None
        type_meta = rec.get("type_meta") if isinstance(rec.get("type_meta"), dict) else None
        auto_tag = (rec.get("cached_tag") or "").strip()
        levels = result.get("merged_tag_levels")
        if not auto_tag and levels:
            if gap is not None and type_meta is not None:
                auto_tag = cc._apply_tag_levels(gap, type_meta, levels) or ""
            elif gap is None:
                # Topic/source auto-tag: no gap to cache on — pure format under
                # the resolved (or default) KG type segment.
                auto_tag = cc._topic_tag_from_levels(levels, type_meta) or ""
        # Case-insensitively, because Anki matches tags that way. The tag box on
        # the work-through screen derives its topic segment from the point's own
        # slug ("…::KGs::smegma") while the post-generation auto-tag derives it
        # from the levels the model returned ("…::KGs::Smegma"). Both are the
        # same tag to Anki, so appending the second only puts two spellings of
        # one tag on every note.
        #
        # A near-match is the same problem and case-folding does not catch it.
        # The box is filled from the levels the READ produced and the auto-tag
        # from the levels the WRITE produced — two guesses at one classification,
        # which is how a card ends up under both
        # "…KG::OBGYN::AbortionCare" and "…KG::ObstetricsGynaecology::Termination".
        # One card in two content branches is the exact thing this scheme exists
        # to stop, so when the door has already put a content tag on, that one
        # stands: it is the one on screen and the one the student could edit.
        try:
            from ..core.config import auto_tag_base, content_segment
            _b, _s = (auto_tag_base() or "").rstrip(":"), content_segment()
            head = f"{_b}::{_s}::".lower() if _b and _s else ""
        except Exception:
            head = ""
        door_tag = next((str(t) for t in tags_raw
                         if head and str(t).lower().startswith(head)), "")
        if auto_tag and door_tag and door_tag.lower() != auto_tag.lower():
            print(f"[ankisstant] job {job_id}: content tag already set to "
                  f"{door_tag!r}; dropping the second opinion {auto_tag!r}")
            auto_tag = door_tag
            # …and the gap remembers the tag that actually went on, so a later
            # "Retag group" can find these cards by it.
            if gap is not None:
                try:
                    cc.cache_auto_tag(gap, door_tag)
                except Exception as e:
                    print(f"[ankisstant] job {job_id} tag cache skipped: {e}")
        if auto_tag and auto_tag.lower() not in {t.lower() for t in tags_raw}:
            tags_raw.append(auto_tag)
        # …and the rest of the scheme: the group's shared branch, the batch's
        # source, the month. Every door that WRITES cards funnels through here,
        # so this is the one place that had to know — and the one place that
        # didn't. Deduped case-insensitively against what the door already set.
        if gap is not None:
            have = {t.lower() for t in tags_raw}
            for t in cc.scheme_tags_for_gap(gap, levels, content_tag=auto_tag):
                if t.lower() not in have:
                    have.add(t.lower())
                    tags_raw.append(t)
    except Exception as e:
        print(f"[ankisstant] job {job_id} auto-tag skipped: {e}")

    # Persist every per-note AI field value produced in the same call (the MQ
    # explanation plus any custom AI fields) to the KG store, and lead the Missed
    # Questions field with the explanation.
    kg_explanation = rec.get("kg_explanation", "")
    try:
        gap = rec.get("gap") if isinstance(rec.get("gap"), dict) else None
        ai_vals = result.get("ai_field_values") or {}
        if gap is not None and ai_vals:
            cc._persist_ai_field_values(gap, ai_vals)
        mqx = (result.get("mq_explanation") or "").strip()
        if mqx and rec.get("kg_type_for_image") == "mq":
            kg_explanation = mqx
    except Exception as e:
        print(f"[ankisstant] job {job_id} ai-field persist skipped: {e}")

    update(job_id, status=READY, cards=cards, tags_raw=tags_raw,
           verdicts=result.get("verdicts"), kg_explanation=kg_explanation)


# ── notify (main thread): outcome toast + badge + panel refresh ─────────────────

def _toast_outcome(outcome: str, n_cards: int, origin: str = "") -> None:
    if outcome == "cancelled":
        return  # user-initiated; no toast
    if origin == "work-through":
        # That run has its own screen open, showing each card land under the
        # point it was written for. Telling the user to "open AI Create" points
        # them at a second, redundant copy of the same batch.
        return
    try:
        from aqt.utils import tooltip
        if outcome == "error":
            tooltip("Ankisstant: a background generation failed — see AI Create.",
                    period=4000)
        else:
            noun = "card" if n_cards == 1 else "cards"
            tooltip(f"Ankisstant: {n_cards} {noun} ready to review — open AI Create.",
                    period=3500)
    except Exception:
        pass


