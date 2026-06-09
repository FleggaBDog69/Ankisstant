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

import json
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


def copy_attachments(job_id: str, paths) -> list[str]:
    """Copy source attachments into the job's folder so the job is self-contained
    across restarts. Returns the copied paths (falls back to the original path on
    any copy error)."""
    if not paths:
        return []
    out: list[str] = []
    try:
        d = attachments_dir(job_id)
    except OSError:
        return list(paths)
    for p in paths:
        try:
            dst = os.path.join(d, os.path.basename(p))
            shutil.copy2(p, dst)
            out.append(dst)
        except OSError:
            out.append(p)
    return out


# ── load / save ───────────────────────────────────────────────────────────────

QUEUED, RUNNING, READY, ERROR, INTERRUPTED = (
    "queued", "running", "ready", "error", "interrupted")
VALID_STATUSES = {QUEUED, RUNNING, READY, ERROR, INTERRUPTED}
ACTIVE_STATUSES = {QUEUED, RUNNING}


def load_all() -> list[dict]:
    path = _store_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return [it for it in data if isinstance(it, dict)] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[ankisstant] create_jobs load failed: {e}")
        return []


def save_all(items: list[dict]) -> None:
    try:
        with open(_store_path(), "w") as f:
            json.dump(items, f, indent=2)
    except OSError as e:
        print(f"[ankisstant] create_jobs save failed: {e}")


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
    # List-shaped fields the UI/worker iterate over.
    for key in ("tags_raw", "kg_image_filenames", "panel_image_filenames",
                "attachment_paths", "slide_pages"):
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


def active_count() -> int:
    """queued + running — i.e. generations currently in flight."""
    return sum(1 for it in load_all() if it.get("status") in ACTIVE_STATUSES)


def by_status(*statuses: str) -> list[dict]:
    want = set(statuses)
    return [it for it in load_all() if it.get("status") in want]


def find_active_duplicate(*, mode: str, topic_label, source_label,
                          notetype_name: str, focus: str) -> dict | None:
    """An in-flight (queued/running) job with the same request signature, used to
    warn the user before they accidentally fire the same generation twice."""
    sig = (mode, topic_label or "", source_label or "", notetype_name or "", focus or "")
    for it in load_all():
        if it.get("status") not in ACTIVE_STATUSES:
            continue
        if (it.get("mode"), it.get("topic_label") or "", it.get("source_label") or "",
                it.get("notetype_name") or "", it.get("focus") or "") == sig:
            return it
    return None


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
            mw.taskman.run_in_background(
                lambda r=rec, e=ev: _worker(r, e),
                lambda fut, jid=job_id: self._done(jid, fut),
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
        _toast_outcome(outcome, n_cards)
        _refresh_ui()
        self.pump()

    # ── cancel ──────────────────────────────────────────────────────────────
    def cancel(self, job_id: str) -> None:
        ev = self._inflight.get(job_id)
        if ev is not None:           # running → signal the worker, drop on return
            self._cancelled.add(job_id)
            ev.set()
        else:                        # still queued → never started
            remove(job_id)
        _refresh_ui()


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
    try:
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
        # Auto-tag in skill flows: a skill forces a bare-array reply with no
        # folded tag levels, so classify the material with a small dedicated
        # call (Qt-free, safe off the main thread). Same shared classifier AI
        # Browse uses, so background generations tag identically to the modal.
        if rec.get("want_auto") and not merged_tag_levels:
            from . import autotag
            merged_tag_levels = autotag.classify(
                rec.get("tag_material", ""), model=rec.get("model"))
        cfg = tool_config("card_creator")
        profile = cc._resolved_profile(cfg, rec.get("profile_name", ""))
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
        if auto_tag and auto_tag not in tags_raw:
            tags_raw.append(auto_tag)
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

def _toast_outcome(outcome: str, n_cards: int) -> None:
    if outcome == "cancelled":
        return  # user-initiated; no toast
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


def _refresh_ui() -> None:
    """Update the nav badge and the panel lists if they're alive. Safe to call
    even when the Ankisstant window is closed (it just no-ops)."""
    try:
        from ..ui import main_window as mwmod
        if getattr(mwmod, "_current", None) is not None:
            mwmod._current.refresh_queue_badge()
    except Exception:
        pass
    try:
        from . import card_creator as cc
        panel = getattr(cc, "_panel", None)
        if panel is not None and hasattr(panel, "refresh_ready_lists"):
            panel.refresh_ready_lists()
    except Exception:
        pass
