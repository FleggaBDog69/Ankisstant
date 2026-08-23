# AI Lecture — the panel and the pipeline that drives the engine modules.
#
# The premise, in one line: the good cards for this lecture ALREADY EXIST in the
# collection — find them and unsuspend them, rather than generating new ones of
# unknown quality. Generation is the fallback for genuine gaps, and it stays
# opt-in and one concept at a time (see the gaps list).
#
# Kept apart from ingest/extractor/matcher on purpose: those three do no Qt work
# and touch no threads, which is what lets them be tested in plain Python outside
# Anki. All the Qt and all the threading live here.

from __future__ import annotations

import dataclasses
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from aqt import mw
from aqt.qt import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, Qt,
    QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip
from aqt.webview import AnkiWebView

from ...core import anki_utils, api as core_api, log
from ...core.config import (
    month_tag, save_tool_config, tool_config, tool_enabled, tool_model_for,
)
from ...core.qt_utils import attach_tag_completer, is_manual_provider
from . import cache, extractor, ingest, manual, matcher, media
from .. import pipeline


NAME = "AI Lecture"

# Vision is OFF by default: text-first is the common path (most slides read from
# their text layer for free), and local OCR now recovers no-text pages without
# it. Vision is an opt-in for the narrow case it alone solves — slides whose
# meaning lives in LAYOUT (drug-to-class matching tables), where both the text
# layer and OCR return a meaningless bag of words. It costs ~4-5x and minutes.
DEFAULT_USE_VISION = False

# Coverage — the single user-facing size dial. Users don't think "do I want more
# topics or more cards per topic"; they think "how much do I want". So one dial
# sets BOTH the extractor's topic granularity (detail -> target_points) and the
# per-topic note count (depth). The raw numbers stay in Settings for hand-tuning.
COVERAGE_PRESETS = {
    "quick":         {"detail": "focused",  "depth": "tight"},
    "balanced":      {"detail": "standard", "depth": "standard"},
    "comprehensive": {"detail": "fine",     "depth": "thorough"},
}
DEFAULT_COVERAGE = "comprehensive"

# Share of the point budget the user's own notes / a transcript may claim. His
# steer was "slides should make up 95% of the LPs" — 0.12 with the floor of 2 in
# extractor.companion_cap lands a little above that on a small lecture and a
# little below on a big one, which is the right way round: a 40-point lecture
# does not need five points of commentary.
DEFAULT_COMPANION_SHARE = 0.12


def _coverage_from(target_points: int, depth: str) -> str:
    """Best-fit a saved (target_points, depth) back onto a Coverage preset, so the
    panel shows the right choice even when the values came from the Settings
    fields. Matches on depth first (the sharper signal), then topic count."""
    for name, spec in COVERAGE_PRESETS.items():
        if spec["depth"] == depth and \
                extractor.DETAIL_TARGETS.get(spec["detail"]) == target_points:
            return name
    # No exact pair — fall back on depth alone, the dial users feel most.
    for name, spec in COVERAGE_PRESETS.items():
        if spec["depth"] == depth:
            return name
    return DEFAULT_COVERAGE


# Note-state dots, in ANKI'S OWN colours so they need no legend: blue is the
# new-card count in every Anki screen, green is the review count. A note we're
# about to unsuspend enters as new (blue); one already in rotation is a review
# (green). Grey is reserved for candidates that were NOT chosen.
# Functions, not constants. As constants these would be evaluated once at Anki
# launch and would then hold whatever palette SynapsePro had at that instant —
# switching its colour theme would appear to do nothing until a restart.
def _c_new() -> str:
    """Suspended -> will be unsuspended -> enters as NEW."""
    from ...core import synapse
    return synapse.color("blue", "#4a90d9")


def _c_rot() -> str:
    """Already in rotation -> a review."""
    from ...core import synapse
    return synapse.color("green", "#3a9e6a")

# Ceiling on any single main-thread hop (see _on_main). Generous on purpose:
# the point of it is to catch a dead event loop, not to police slow queries.
_MAIN_THREAD_TIMEOUT = 300

# Slide-attach AUTO-SUGGEST (free, text-only). A pixel test can only tell that a
# slide has *something* on it, never that it's high-yield — the junkiest pages
# (Acknowledgement, closing) are the most colourful. So we suggest from the
# learning POINT instead: pre-tick a candidate slide when the point is the kind
# a figure genuinely helps — a mechanism, pathway, anatomy, table, or graph —
# and leave plain single-fact points unticked. Fletcher still nudges the edges.
import re as _re
_FIGURE_SIGNAL_RE = _re.compile(r"""(?xi)
    mechanism | pathway | cascade | \bcycle | synthes | metaboli | signal
  | receptor | \bchannel | transporter | \bpump\b | gradient | allosteric
  | \bion\b | uptake | secret | \baxis\b | feedback | reflex | \bloop\b
  | \bcircuit | \barc\b | \bstage | \bphase | \bstep\b | sequence | algorithm
  | anatom | structur | nucle | \btract\b | foramen | innervat | dermatome
  | ganglion | plexus | \blobe | cortex | \bcourse\b | \bbranch | \blayer
  | \bzone\b | \bgland | distribution
  | classif | subtype | categor | \bcompar | versus | \bvs\b | differen
  | spectrum | \btable\b | \bgrid\b | \bmatrix\b
  | \bgraph | \bcurve | kinetic | dose | titrat | concentration | \becg\b
  | waveform | \btrace\b | \bplot\b | \bmap\b | diagram | \bfigure\b
""")


def _point_wants_figure(point) -> bool:
    """Heuristic 'would a diagram/table help this point?' over its own words —
    concept + terms + topic. Drives the free auto-suggest of slides to attach."""
    parts = [getattr(point, "concept", "") or "",
             " ".join(getattr(point, "terms", []) or []),
             getattr(point, "topic", "") or ""]
    return bool(_FIGURE_SIGNAL_RE.search(" ".join(parts)))


# ── thread plumbing ──────────────────────────────────────────────────────────

class Cancelled(Exception):
    """The user pressed Stop."""


class _Heartbeat:
    """Ticks an elapsed-seconds status line while a long, blocking call runs.

    The extraction call holds the worker thread for minutes on a big lecture;
    a static "Reading the lecture with AI…" reads as a hang. This spins a small
    daemon thread that re-reports the same step with a running clock (and, when
    vision is on, a note that diagrams take longer) so the UI shows life. It
    never touches the collection — `report` marshals to the main thread itself.
    """

    def __init__(self, report, base_msg: str, pct: int, *, vision: bool = False):
        self._report = report
        self._base = base_msg
        self._pct = pct
        self._vision = vision
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        t0 = time.monotonic()

        def _tick():
            # First repaint is immediate; then every ~2.5s until the call ends.
            self._report(self._base, self._pct)
            while not self._stop.wait(2.5):
                el = int(time.monotonic() - t0)
                clock = f"{el}s" if el < 60 else f"{el // 60}m{el % 60:02d}s"
                hint = (" · reading diagrams, this can take a few minutes"
                        if self._vision else "")
                self._report(f"{self._base}  ({clock}{hint})", self._pct)

        self._thread = threading.Thread(target=_tick, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.2)
        return False


def _on_main(fn):
    """Run `fn()` on the main thread and block the calling worker until it
    returns, propagating its value or exception.

    The pipeline alternates between AI calls (which must NOT hold the main
    thread) and collection reads (which must ONLY happen on it), so neither
    "all on a worker" nor "all on main" is available. Blocking here is safe
    precisely because the main thread never waits on the worker — it just runs
    `fn` and moves on. The reverse would deadlock.
    """
    box: dict = {}
    done = threading.Event()

    def _run():
        try:
            box["value"] = fn()
        except Exception as e:      # noqa: BLE001 — re-raised on the worker below
            box["error"] = e
        finally:
            done.set()

    mw.taskman.run_on_main(_run)
    # Bounded, because an unbounded wait here has one catastrophic failure mode:
    # if the main loop stops (Anki quitting mid-run), the worker blocks forever
    # and takes the shutdown with it. A slow step is fine — the ceiling is far
    # above any real one (the whole 25-point deck sweep is ~24s) — so blowing it
    # means the main thread is gone, and a visible error beats a silent hang.
    if not done.wait(timeout=_MAIN_THREAD_TIMEOUT):
        raise RuntimeError("Timed out waiting on Anki's main thread.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ── the pipeline (worker thread) ─────────────────────────────────────────────

def search_all(points, *, index, notetype_filter, candidate_cap, on_main, report,
               check=lambda: None, cand_cap_for=None):
    """Both search legs per point, one at a time on the main thread so the UI
    keeps breathing. Shared by the automated worker and the file hand-off path so
    a manual run searches the deck identically to a CLI run.

    `cand_cap_for(point) -> int` narrows the pool for individual points. KG mode
    uses it: a gap allowed one card has no use for twenty candidates, and every
    candidate is relevance-judging work. None keeps the flat `candidate_cap`.
    """
    matches: list[matcher.PointMatch] = []
    for i, p in enumerate(points):
        check()
        limit = candidate_cap
        if cand_cap_for is not None:
            try:
                limit = max(1, min(candidate_cap, int(cand_cap_for(p))))
            except Exception:
                limit = candidate_cap

        def _search(p=p, limit=limit):
            m = matcher.search_point(p, index, notetype_filter=notetype_filter,
                                     limit=limit)
            matcher.annotate_suspended(m)      # mandatory before summarise()
            return m

        matches.append(on_main(_search))
        report(f"Searching your deck… {i + 1}/{len(points)} points",
               35 + int(20 * (i + 1) / max(len(points), 1)))
    return matches


def score_all(rel_batches, *, model, report, check, cancel_ev, workers=1):
    """Run the relevance batches and yield (batch, verdicts) for each.

    PARALLEL BY DEFAULT. The judge is the slowest leg of the whole pipeline —
    one AI call per ~20 candidates, tens of seconds each — and the batches are
    fully independent: each judges its own points against its own candidates,
    and nothing downstream cares what order verdicts come back in. Running them
    one after another was what made a 49-point bulk gap search take ~25 minutes.

    Each worker re-arms the cancel event, which is thread-local in core/api.py,
    or Stop wouldn't reach the subprocesses it spawns.

    Raises matcher.RelevanceUsageExhausted once usage runs out — every remaining
    call would fail identically, so the caller stops and lets the fail-open fill
    downstream handle whatever wasn't judged.
    """
    total = len(rel_batches)
    if total == 0:
        return

    def _one(b):
        # Thread-local in core/api.py: a worker that never sets it cannot be
        # cancelled, and Stop would look ignored until the call timed out.
        core_api.set_cancel_event(cancel_ev)
        try:
            verdicts = matcher.score_batch(b, model=model)
            if any(not v.filtered for v in verdicts):
                # Failed open, usually a CLI timeout. One more go — a second
                # call on the same batch usually lands. Whatever is STILL
                # unfiltered is left for the fail-open flag downstream, which
                # names the points so the gap isn't silent.
                if cancel_ev.is_set():
                    return verdicts
                verdicts = matcher.score_batch(b, model=model)
            return verdicts
        finally:
            core_api.set_cancel_event(None)

    if workers <= 1 or total == 1:
        for i, b in enumerate(rel_batches):
            check()
            report(f"Checking which notes actually test each point… "
                   f"{i + 1}/{total}", 55 + int(35 * (i + 1) / total))
            yield b, _one(b)
        return

    done = 0
    lock = threading.Lock()
    report(f"Checking which notes actually test each point… 0/{total} "
           f"({workers} at a time)", 55)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="ankisstant-relevance") as pool:
        futures = {pool.submit(_one, b): b for b in rel_batches}
        try:
            for fut in as_completed(futures):
                b = futures[fut]
                verdicts = fut.result()      # RelevanceUsageExhausted lands here
                with lock:
                    done += 1
                    n = done
                report(f"Checking which notes actually test each point… "
                       f"{n}/{total} ({workers} at a time)",
                       55 + int(35 * n / total))
                yield b, verdicts
                if cancel_ev.is_set():
                    break
        finally:
            # Nothing that hasn't started should start: on Stop or on usage
            # exhaustion every remaining call is either unwanted or doomed.
            for fut in futures:
                fut.cancel()


def finish_matches(matches, *, target_cards, depth=matcher.DEFAULT_DEPTH,
                   on_main, report, ai_dedupe_model=None, cap_for=None):
    """Everything after relevance scoring: fail-open fill for unscored points,
    local + (optional) semantic dedupe, budget allocation, the diagnostic trail,
    and the coverage summary. Shared by both paths.

    A point left unscored (relevance ran out of usage, or — in the file path — a
    verdict never came back) fails OPEN: its candidates are kept unranked so the
    run still produces cards, and it's counted in `degraded` so the summary can
    say the result is only partly filtered. `ai_dedupe_model=None` skips the
    semantic dedupe entirely (the file path has no provider to call).

    `cap_for` is passed straight to allocate: KG mode uses it to give each gap a
    per-point share scaled to how broad that gap is (see tools/kg/triage.py);
    None keeps the flat `depth` preset every other path uses."""
    degraded_pts = []
    for m in matches:
        if m.nids and not m.scored:
            m.kept, m.scored = list(m.nids), True
            # reason is set on the relevance path; the file hand-off path leaves
            # points unscored with no reason — call that "not judged".
            reason = getattr(m, "degrade_reason", "") or "not judged"
            degraded_pts.append((m.point, reason))
    degraded = len(degraded_pts)
    # A point with no candidates never reaches the judge, but it IS scored: the
    # deck genuinely has nothing, and is_gap must say so.
    for m in matches:
        if not m.nids:
            m.kept, m.scored = [], True
    if degraded:
        _named = "; ".join(f"{(p.topic or p.concept or '?')[:60]} [{r}]"
                           for p, r in degraded_pts)
        log.warn(f"lecture: relevance incomplete — {degraded} point(s) left "
                 f"unfiltered: {_named}")

    # Dedupe BEFORE allocate so the budget is never spent on the AnKing/Malleus
    # twins of one fact. Reads note text, so it goes through the main thread.
    report("Removing duplicate notes…", 92)
    on_main(lambda: matcher.dedupe(matches))
    report("Choosing the best notes…", 95)
    matcher.allocate(matches, target_cards, depth=depth, cap_for=cap_for)
    # A second, SEMANTIC dedupe over just the SELECTED cards — catches same-fact
    # twins worded too differently for the local token-overlap pass to fuse.
    # Skipped when usage is exhausted (the call would only fail) or when there is
    # no provider at all (the file hand-off path).
    if ai_dedupe_model and not degraded:
        report("Merging near-duplicate notes…", 97)
        dd_cards, dd_owner = on_main(lambda: matcher.selected_card_text(matches))
        matcher.ai_dedupe_selected(dd_cards, dd_owner, model=ai_dedupe_model)
    # Diagnostic trail (debug_logging only, file-only): status of every candidate
    # of every point — what makes "the SSRI MOA card is missing" answerable.
    if log.enabled():
        _cand = sorted({nid for m in matches for nid in m.nids})
        _snip = on_main(lambda: {n: matcher.note_text(n, 110) for n in _cand})
        for _i, _m in enumerate(matches, 1):
            _sel = set(_m.selected)
            _rank = {nid: r + 1 for r, nid in enumerate(_m.kept)}
            log.trace(
                f"PICK {_i}: topic='{_m.point.topic}' | {_m.point.concept[:80]} "
                f"| found={len(_m.nids)} kept={len(_m.kept)} weight={_m.weight} "
                f"selected={_m.selected}")
            if _m.tag_hits:
                log.trace(f"    tags: {_m.tag_hits}")
            if _m.per_term:
                log.trace("    terms: " + ", ".join(f"'{t}'->{n}" for t, n in _m.per_term))
            for nid in _m.nids:
                _st = "SELECTED" if nid in _sel else (
                    f"kept#{_rank[nid]}" if nid in _rank else "DROPPED")
                _w, _p, _t = _m.rank_debug.get(nid, (0, 0, 0))
                _fp = f"w{_w} p{_p}{' tag' if _t else ''}"
                _cov = sorted(_m.term_cover.get(nid, ()))
                _tc = ("t" + ",".join(str(i) for i in _cov)) if _cov else "t-"
                _sc = _m.scores.get(nid)
                _js = _m.judge_slot.get(nid)
                _v = (f"s{_sc}" if _sc is not None else "s-") + \
                     (f"/j{_js}" if _js is not None else "")
                log.trace(f"    {nid} {_st:9} [{_fp:>9}] {_tc:8} {_v:6} | {_snip.get(nid, '')}")
    coverage = on_main(lambda: matcher.summarise(matches, target_cards))
    if degraded:
        _lines = "; ".join(f"“{(p.topic or p.concept or '?')[:60]}” ({r})"
                           for p, r in degraded_pts)
        coverage.degraded_note = (
            f"{degraded} learning point(s) show raw, unranked search hits instead "
            f"of filtered notes — the relevance step didn't finish for them: "
            f"{_lines}. Re-run for the full-quality result.")
    return coverage


def run_pipeline(path: str, cfg: dict, *, report, cancel_ev: threading.Event,
                 points: list | None = None, cap_for=None, cand_cap_for=None,
                 companion=None):
    """Ingest → extract → match → relevance → allocate → summarise.

    Runs entirely on a worker thread. Returns (lecture, matches, coverage).
    Every collection touch goes through _on_main; every AI call stays here.

    `points` short-circuits the whole read step. KG mode supplies them: the gaps
    were triaged (and edited by the user) BEFORE the run, so there is nothing
    left to extract — everything from the deck search onward is identical to a
    lecture. `cap_for` rides with them to give each gap its own card share, and
    `cand_cap_for` shrinks the candidate pool for the narrow ones so the judge
    isn't reading twenty cards for a gap that wants one.

    `companion` is the user's own notes / a transcript (ingest.CompanionText),
    snapshotted by the panel before the run starts. Supplemental in both of its
    roles: it may contribute a hard-capped handful of learning points, and it
    supplies material for the card-note pass. None (or empty) is the default and
    changes nothing about the run.
    """
    def _check():
        if cancel_ev.is_set():
            raise Cancelled()

    # `points` is rebound below when we extract, so remember whether the CALLER
    # supplied them — that's what distinguishes a lecture run from KG mode.
    supplied_points = points

    target_points = int(cfg.get("target_points") or extractor.DEFAULT_TARGET_POINTS)
    target_cards = int(cfg.get("target_cards") or matcher.DEFAULT_TARGET_CARDS)
    depth = str(cfg.get("depth") or matcher.DEFAULT_DEPTH)
    # Candidate pool follows depth automatically (leaner depth -> leaner, faster
    # judging) unless Settings carries an explicit override. Logged because a
    # silently-small pool is invisible in the results and costs good cards.
    candidate_cap = matcher.resolve_candidate_cap(cfg)
    log.info(f"lecture: depth '{depth}' — judging up to {candidate_cap} candidate(s) per point")
    use_vision = bool(cfg.get("use_vision", False))
    use_ocr = bool(cfg.get("use_local_ocr", True))
    high_yield = bool(cfg.get("high_yield_only", True))
    # Off = the lecture is the source of truth (no backfilling facts it omits).
    # In the extraction cache key, so flipping it forces a fresh read.
    enrich = bool(cfg.get("enrich", False))
    notetype_filter = (cfg.get("notetype_filter") or "").strip()
    # Optional deck/tag restriction on every deck search (Mass AI Create's
    # "skip what a deck or tag already covers"). "" is the default and
    # searches the whole collection, exactly as before.
    search_scope = (cfg.get("search_scope") or "").strip()
    force_reextract = bool(cfg.get("force_reextract", False))
    lo_text = (cfg.get("lo_text") or "").strip()
    model_x = pipeline.dial_for(pipeline.READ)

    # Companion notes. The cap is what keeps them supplemental — the teaching
    # material still supplies the overwhelming majority of the points.
    comp_cap = 0
    comp_digest = ""
    if companion and getattr(companion, "chunks", None):
        comp_cap = extractor.companion_cap(
            target_points, cfg.get("companion_lp_share", DEFAULT_COMPANION_SHARE))
        comp_digest = cache.companion_digest(companion)

    lecture = None

    if points is not None:
        # KG MODE — the points arrived pre-read. tools/kg/triage.py already did
        # the one AI call that turns rough knowledge gaps into searchable points
        # (and the user has since reviewed and edited them), so there is no
        # ingest, no OCR, no extraction and no cache lookup to do here.
        report(f"{len(points)} point(s) from your knowledge gaps…", 30)
    elif lo_text:
        # LO MODE — the user handed over the objectives directly, so skip ingest,
        # OCR, imagery and the whole-document extraction read. One cheap AI call
        # turns the pasted list into the same LearningPoints the file path would
        # have produced, then the pipeline below is identical.
        ck = cache.cache_key_text(lo_text, model=model_x, enrich=enrich,
                                  companion=comp_digest)
        if not force_reextract:
            points = cache.load(ck)
            if points is not None:
                report("Loaded a cached read of your objectives…", 40)
        if points is None:
            base = "Reading your learning objectives…"
            with _Heartbeat(report, base, 12):
                try:
                    points = extractor.extract_from_objectives(
                        lo_text, model=model_x, enrich=enrich,
                        companion=companion, companion_cap_points=comp_cap)
                except extractor.ExtractionError as e:
                    raise ValueError(e.friendly)
            if not points:
                raise ValueError(
                    "No testable learning points came back from those objectives.")
            points = extractor.enforce_companion_cap(points, comp_cap)
            cache.store(ck, points)
    else:
        # FILE MODE — the original path.
        # 1 — ingest. No AI, no collection: every supported format reads its text.
        report("Reading the file…", 0)
        lecture = ingest.load(path)
        _check()

        # 1b — local OCR for pages with NO text layer (scans, one-big-image
        # slides). Free and local; runs ahead of vision and shrinks it.
        # Text-bearing pages are never touched. A missing engine is a silent
        # no-op — vision still catches whatever stays empty (if the user has it on).
        if use_ocr and lecture.rasterisable and lecture.vision_segments:
            report("Reading scanned pages with OCR…", 3)
            try:
                n = ingest.ocr_fill(lecture)
                if n:
                    report(f"OCR recovered {n} page(s)…", 4)
            except Exception as e:
                log.error(f"lecture: OCR step failed, continuing: {e}")
        _check()

        # 2 — imagery. Which segments carry meaning in their LAYOUT rather than
        # their words. Pixels can say "there's something to look at" but never
        # "this is high-yield" — the junkiest slides are the most colourful ones —
        # so this is only ever an input to extraction, never a filter on its own.
        imagery: set[int] = set()
        if use_vision and lecture.rasterisable:
            report("Looking for diagrams and tables…", 5)
            try:
                imagery = ingest.detect_imagery(path)
            except Exception as e:
                log.error(f"lecture: imagery detection failed, text only: {e}")
        _check()

        # 3 — extract. Normally ONE call for the whole document; batching is only
        # a truncation defence for very large files and costs global dedupe.
        batches = extractor.build_batches(lecture)
        if not batches:
            raise ValueError("Nothing readable in that file.")
        per_batch = extractor.share_of_target(target_points, len(batches))

        # 3a — extraction cache. The AI read is the slow, expensive step and it's
        # identical across runs of the same file+settings. When you're iterating
        # on matching/relevance you'd otherwise re-pay ~240s and tokens for the
        # exact same points every run. Hit the cache and skip straight to
        # matching. The "Re-extract" toggle forces a fresh read.
        ck = cache.cache_key(
            path, target_points=target_points, use_vision=use_vision,
            model=model_x, high_yield=high_yield, enrich=enrich,
            companion=comp_digest,
        )
        if not force_reextract:
            points = cache.load(ck)
            if points is not None:
                report("Loaded a cached read — skipping the AI extraction…", 40)

        if points is None:
            results = []
            last_err: extractor.ExtractionError | None = None
            for b in batches:
                _check()
                base = (
                    "Reading the lecture with AI…"
                    if len(batches) == 1 else
                    f"Extracting learning points… batch {b.index + 1}/{len(batches)}"
                )
                # The AI call blocks this worker for minutes on a big lecture.
                # Without a heartbeat the status line freezes and the user can't
                # tell live from hung — so tick elapsed seconds until it returns.
                with _Heartbeat(report, base, 12, vision=use_vision):
                    try:
                        res = extractor.extract_batch(
                            b, lecture, model=model_x, use_vision=use_vision,
                            target_points=per_batch, imagery=imagery,
                            high_yield=high_yield, enrich=enrich,
                            companion=companion, companion_cap_points=comp_cap,
                        )
                    except extractor.ExtractionError as e:
                        # A hard provider failure (out of usage, timeout).
                        # Remember it so that if the whole document ends up empty
                        # we can tell the user WHY, but keep going: in a
                        # multi-batch run the others may work.
                        last_err = e
                        log.error(f"lecture: extraction failed [{e.code}]: {e.friendly}")
                        res = []
                results.append(res)
            points = extractor.enforce_companion_cap(
                extractor.merge(results), comp_cap)
            if not points:
                if last_err is not None:
                    raise ValueError(last_err.friendly)
                raise ValueError(
                    "No learning points came back — the AI returned nothing "
                    "usable for this file. It may be unreadable, or empty of "
                    "testable content."
                )
            # Semantic twin pass. merge() has already fused rewordings by word
            # overlap; this catches the pair that says one thing in two
            # vocabularies, which no local threshold can separate from two
            # genuinely different facts.
            #
            # Placed BEFORE the cache write on purpose: the collapse is paid for
            # once per extraction, and a cached run inherits the result for free
            # rather than re-judging the same points every time.
            # A READ refinement — it decides which points exist — so it belongs
            # on the read dial, not the judge's. Same model on both families
            # today; this stops them drifting apart later.
            twin_model = pipeline.dial_for(pipeline.READ)
            if bool(cfg.get("ai_twin_pass", True)) and twin_model:
                _check()
                report("Checking for points that say the same thing…", 32)
                try:
                    extractor.ai_collapse_twins(points, model=twin_model)
                except (Cancelled, core_api.ClaudeCancelled):
                    raise
                except Exception as e:
                    # Fail open, like every other optional pass here: worse
                    # points beat no lecture.
                    log.warn(f"lecture: twin pass skipped: {e}")

            # Only cache a real, non-empty extraction we just paid for.
            cache.store(ck, points)

    # Diagnostic trail (file logging only, via log.trace): the exact learning
    # points + terms — logged on BOTH paths so a cached run is just as
    # diagnosable, in case matching later fails and we want the extraction record.
    for _i, _p in enumerate(points, 1):
        log.trace(f"LP {_i}: topic='{_p.topic}' sys='{_p.system}' | {_p.concept} | terms={_p.terms}")
    _check()

    # 4 — match. Both legs per point, on the main thread one point at a time so
    # the UI keeps breathing rather than freezing for the whole sweep.
    report(f"{len(points)} learning points — searching your deck…", 35)
    index = _on_main(lambda: matcher.TagIndex.build(search_scope))
    matches = search_all(points, index=index, notetype_filter=notetype_filter,
                         candidate_cap=candidate_cap, on_main=_on_main,
                         report=report, check=_check, cand_cap_for=cand_cap_for)

    # 5 — relevance. Card text is pulled on the main thread, then scored off it:
    # score_batch never touches mw.col, which is what makes that split legal.
    items = _on_main(lambda: [
        (m.point, [(nid, matcher.note_text(nid)) for nid in m.nids])
        for m in matches if m.nids
    ])
    by_point = {id(m.point): m for m in matches}
    rel_batches = matcher.build_relevance_batches(items)
    model_r = tool_model_for("lecture_relevance")
    workers = max(1, int(cfg.get("relevance_workers") or 1))
    try:
        for b, verdicts in score_all(rel_batches, model=model_r, report=report,
                                     check=_check, cancel_ev=cancel_ev,
                                     workers=workers):
            _check()
            for (point, _cards), v in zip(b, verdicts):
                m = by_point.get(id(point))
                if m is not None:
                    m.kept = list(v.kept)
                    m.scores = dict(v.scores)
                    m.judge_slot = dict(v.slots)
                    # Only "scored" if actually judged — an unfiltered fail-open
                    # is left False so finish_matches counts and names it, with
                    # the reason (timeout / network / parse error) it carries.
                    m.scored = v.filtered
                    m.degrade_reason = "" if v.filtered else (v.reason or "unknown")
    except matcher.RelevanceUsageExhausted:
        # Usage ran out mid-pass; every remaining call would fail identically, so
        # stop here. The points not yet scored are handled by the fail-open fill
        # below, and `degraded` (their count) tells the user the result is only
        # partly filtered — the silent version of this is what made a broken run
        # look successful.
        pass

    # 6 — fail-open fill, dedupe, budget, diagnostics, coverage. Shared with the
    # file hand-off path (see finish_matches); a clean CLI run passes its own
    # relevance model so the semantic dedupe still fires.
    # Size the cap from what the lecture actually produced, now that we know.
    # Done here rather than at the top because the point count is the input and
    # it doesn't exist until extraction has run.
    #
    # NOT in KG mode. When `points` was supplied, the budget is the sum of the
    # per-gap card targets the triage pass worked out (and the user edited in
    # the plan table) — a deliberate figure, not a default to improve on.
    if supplied_points is None and bool(cfg.get("auto_target_cards", True)):
        sized = matcher.auto_target_cards(len(points or []))
        if sized != target_cards:
            log.info(f"lecture: {len(points or [])} point(s) -> max notes "
                     f"{sized} (was {target_cards}, sized from the lecture)")
        target_cards = sized

    coverage = finish_matches(matches, target_cards=target_cards, depth=depth,
                              on_main=_on_main, report=report, ai_dedupe_model=model_r,
                              cap_for=cap_for)

    # 7 — card notes. Last, because it works on the FINAL selected set: which
    # cards survived the budget is exactly what decides whose source lines are
    # worth reading. Entirely optional and off by default, so a normal run never
    # reaches this and costs exactly what it always did.
    if bool(cfg.get("attach_notes", False)) and model_r:
        _check()
        report("Looking for anything worth adding to these cards…", 98)
        segs: dict[int, str] = {}
        if lecture is not None:
            segs = {s.number: s.text for s in lecture.segments}
        cn_cards, cn_owner = _on_main(lambda: matcher.selected_card_text(matches))
        try:
            notes, unplaced = matcher.pick_card_notes(
                cn_cards, cn_owner, segs, companion=companion,
                max_per_card=int(cfg.get("card_notes_max_per_note") or 2),
                model=model_r,
                workers=max(1, int(cfg.get("relevance_workers") or 1)),
            )
        except core_api.ClaudeCancelled:
            raise
        except Exception as e:
            # Fails CLOSED, deliberately: attaching nothing is always safe, and
            # this step is a garnish on a run that has already succeeded.
            log.error(f"lecture: card-note pass failed, attaching nothing: {e}")
            notes, unplaced = [], set()
        coverage.card_notes = notes
        coverage.unplaced_notes = sorted(unplaced)

    return lecture, matches, coverage


# ── panel ────────────────────────────────────────────────────────────────────

class LecturePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = tool_config("lecture")
        self._path = ""
        self._matches: list[matcher.PointMatch] = []
        self._coverage = None
        self._cancel_ev: threading.Event | None = None
        self._running = False
        # Built lazily and kept for the session's re-searches. The run builds its
        # own; this only pays the tag walk again if a re-search is the first
        # thing that needs one.
        self._tag_index = None
        # id(PointMatch) of every candidate picker the user has opened, so a
        # re-render doesn't collapse what they were working in.
        self._open_points: set[int] = set()
        # Session id -> PointMatch, rebuilt each render so the HTML bridge can
        # name a point in a pycmd string. Last known scroll position of the web
        # view, kept current by a throttled scroll ping so a re-render can
        # restore it (the widget scrollbar is gone).
        self._match_by_id: dict[int, matcher.PointMatch] = {}
        self._scroll_y: int = 0
        # File hand-off (no-provider) flow, driven across button clicks:
        #   None → await_points → await_verdicts → None
        self._fm_stage: str | None = None
        self._fm_lecture = None
        self._fm_matches: list[matcher.PointMatch] = []
        self._fm_scorable: list = []
        self._fm_cfg: dict = {}
        self._build()

    # -- construction --
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # Everything you set BEFORE a run — source + options — lives in one box
        # that collapses once results come back, handing the whole panel over to
        # the cards. "New lecture" (in the button row) brings it back. The tag
        # row is deliberately kept OUTSIDE this box so it stays visible over the
        # results.
        self._setup_box = QWidget()
        setup = QVBoxLayout(self._setup_box)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.setSpacing(10)
        root.addWidget(self._setup_box)

        intro = QLabel(
            "<b>AI Lecture</b> — point it at a lecture and it finds the cards you "
            "<i>already own</i> for it, then unsuspends them. It doesn't write new "
            "cards: yours are already vetted. Anything your deck genuinely misses "
            "is listed as a gap for you to decide about."
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        setup.addWidget(intro)

        # Source — a file to extract from, OR a learning-objective list pasted
        # straight in. The paste path skips the whole extraction read (the slow,
        # expensive step), because an LO list already IS the distilled thing
        # extraction works to recover.
        self._sec("PRIMARY RESOURCE", setup)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Source:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("A file — slides, handout or transcript", "file")
        self._mode_combo.addItem("Paste learning objectives (skips extraction)", "lo")
        self._mode_combo.setToolTip(
            "Paste your own learning objectives to skip the slow, costly "
            "extraction read entirely — they're already the atomic, "
            "mechanism-shaped points the tool is trying to find cards for."
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, 1)
        setup.addLayout(mode_row)

        # File row (mode = file)
        self._file_widget = QWidget()
        file_row = QHBoxLayout(self._file_widget)
        file_row.setContentsMargins(0, 0, 0, 0)
        self._file_btn = QPushButton("Choose File…")
        self._file_btn.setFixedWidth(120)
        self._file_btn.clicked.connect(self._pick_file)
        file_row.addWidget(self._file_btn)
        self._file_lbl = QLabel(
            "No file chosen — .pdf, .pptx, .docx, .txt, .md, .rtf, .vtt or .srt")
        self._file_lbl.setStyleSheet("color: gray;")
        self._file_lbl.setWordWrap(True)
        file_row.addWidget(self._file_lbl, 1)
        setup.addWidget(self._file_widget)

        # Paste box (mode = lo) — hidden until the LO source is picked.
        self._lo_widget = QWidget()
        lo_col = QVBoxLayout(self._lo_widget)
        lo_col.setContentsMargins(0, 0, 0, 0)
        # Load objectives straight out of a document (a PBL sheet, a unit guide)
        # so an LO PDF doesn't have to be hand-pasted; the course header, page
        # numbers and footer are stripped and each objective folded to one line.
        lo_load_row = QHBoxLayout()
        self._lo_file_btn = QPushButton("Load from file…")
        self._lo_file_btn.setFixedWidth(140)
        self._lo_file_btn.setToolTip(
            "Pull the objectives out of a .pdf, .docx, .txt or .md — the course "
            "header, section headings, page numbers and footer are removed "
            "automatically. You can still edit them below before running.")
        self._lo_file_btn.clicked.connect(self._load_lo_file)
        lo_load_row.addWidget(self._lo_file_btn)
        lo_load_row.addWidget(QLabel("or paste below:"))
        lo_load_row.addStretch(1)
        lo_col.addLayout(lo_load_row)
        self._lo_text = QPlainTextEdit()
        self._lo_text.setPlaceholderText(
            "Paste your learning objectives — one per line, numbered or "
            "bulleted.\n\ne.g.\n1. Describe the mechanism of action of SSRIs\n"
            "2. Outline the management of an acute TIA")
        self._lo_text.setMinimumHeight(120)
        self._lo_text.textChanged.connect(self._on_lo_changed)
        lo_col.addWidget(self._lo_text)
        self._lo_hint = QLabel("")
        self._lo_hint.setStyleSheet("color: gray; font-size: 11px;")
        lo_col.addWidget(self._lo_hint)
        self._lo_widget.setVisible(False)
        setup.addWidget(self._lo_widget)

        # Companion text — your own notes, or a transcript. Strictly
        # supplemental and OFF by default: the slides are the spine of a run,
        # and this is the optional second document beside them. One slot rather
        # than a "notes" feature and a "transcript" feature, because
        # mechanically they are the same thing.
        self._companion_cb = QCheckBox("Add a supplementary resource")
        self._companion_cb.setToolTip(
            "Off by default. A SECOND document alongside the primary one — your "
            "jots, your full notes, or a transcript.\n\n"
            "It does two things, both supplemental: it can add a few learning "
            "points for things it covers that the primary resource never taught "
            "(capped — the primary still supplies the overwhelming majority), "
            "and it gives 'Add notes to cards' something to place onto the "
            "cards it finds.\n\n"
            "Anything YOU wrote goes onto a card word for word. A transcript is "
            "tidied first — the filler comes out, the facts don't.\n\n"
            "If a video IS the assigned material, it isn't supplementary — put "
            "its transcript in the primary slot above instead."
        )
        self._companion_cb.toggled.connect(self._on_companion_toggled)
        setup.addWidget(self._companion_cb)

        self._companion_widget = QWidget()
        comp_col = QVBoxLayout(self._companion_widget)
        comp_col.setContentsMargins(18, 0, 0, 0)
        comp_row = QHBoxLayout()
        comp_row.setContentsMargins(0, 0, 0, 0)
        self._companion_btn = QPushButton("Choose…")
        self._companion_btn.setFixedWidth(120)
        self._companion_btn.clicked.connect(self._pick_companion)
        comp_row.addWidget(self._companion_btn)
        self._companion_lbl = QLabel("No file — .txt, .md, .docx, .vtt or .srt")
        self._companion_lbl.setStyleSheet("color: gray;")
        self._companion_lbl.setWordWrap(True)
        comp_row.addWidget(self._companion_lbl, 1)
        comp_col.addLayout(comp_row)
        self._companion_text = QPlainTextEdit()
        self._companion_text.setPlaceholderText(
            "…and / or paste your jots here — one per line.\n\n"
            "e.g.\n- jugular foramen = CN 9, 10, 11 — Vernet. exam favourite\n"
            "- he said the pupil is the giveaway in uncal herniation")
        self._companion_text.setMinimumHeight(80)
        self._companion_text.textChanged.connect(self._update_companion_hint)
        comp_col.addWidget(self._companion_text)
        self._companion_hint = QLabel("")
        self._companion_hint.setStyleSheet("color: gray; font-size: 11px;")
        self._companion_hint.setWordWrap(True)
        comp_col.addWidget(self._companion_hint)
        self._companion_widget.setVisible(False)
        setup.addWidget(self._companion_widget)

        setup.addWidget(self._hsep())

        # Options — ONE size dial (Coverage) plus a hard cap. Coverage sets both
        # how finely the lecture splits into topics AND how many notes each topic
        # gets: users think "how much do I want", not two separate axes. The
        # underlying target_points/depth/candidate numbers live in Settings for
        # anyone who wants to hand-tune. See COVERAGE_PRESETS.
        self._sec("OPTIONS", setup)
        opt = QHBoxLayout()
        opt.addWidget(QLabel("Coverage:"))
        self._coverage_combo = QComboBox()
        for _lbl, _val in (("Quick", "quick"), ("Balanced", "balanced"),
                           ("Comprehensive", "comprehensive")):
            self._coverage_combo.addItem(_lbl, _val)
        self._coverage_combo.setToolTip(
            "How much to pull for the whole lecture — one dial for the size of "
            "the result.\n\n"
            "Quick — the big topics only, one or two notes each: a lean revision "
            "shortlist.\n"
            "Balanced — a topic per distinct idea, a few notes each.\n"
            "Comprehensive — every enumerable member split out (ACA / MCA / PCA "
            "stroke syndromes each their own line) with a full cluster of notes "
            "each: a proper study set.\n\n"
            "Max notes (right) is still a hard cap on the total."
        )
        opt.addWidget(self._coverage_combo)
        opt.addSpacing(18)
        opt.addWidget(QLabel("Max notes:"))
        self._cards_spin = QSpinBox()
        self._cards_spin.setRange(5, 500)
        self._cards_spin.setToolTip(
            "A hard CAP on the total notes for the whole lecture — not a target. "
            "Coverage decides how many notes are actually wanted; this only steps "
            "in to stop a big lecture running away.\n\nNothing is ever padded to "
            "reach it."
        )
        opt.addWidget(self._cards_spin)
        self._auto_cards_cb = QCheckBox("from lecture")
        self._auto_cards_cb.setToolTip(
            "Size the cap from how many learning points the lecture actually "
            "yields, instead of a fixed number.\n\nA fixed cap is wrong in both "
            "directions — it binds on a dense pharmacology deck and is far too "
            "loose on a six-point tutorial. Still only a cap: nothing is ever "
            "padded to reach it.\n\nUntick to go back to the number on the left, "
            "which is kept either way."
        )
        self._auto_cards_cb.toggled.connect(
            lambda on: self._cards_spin.setEnabled(not on))
        opt.addWidget(self._auto_cards_cb)
        opt.addStretch(1)
        setup.addLayout(opt)

        opt2 = QHBoxLayout()
        self._vision_cb = QCheckBox("Read diagrams as images")
        self._vision_cb.setToolTip(
            "Off by default. Text and local OCR already read almost every "
            "slide.\n\nTick this only for lectures whose meaning lives in the "
            "LAYOUT — a drug-to-class matching table is just a bag of words "
            "without it. It costs roughly 4-5x and adds a few minutes (measured: "
            "~$0.15/60s text-only vs ~$0.62/320s with vision on a 44-page deck)."
        )
        opt2.addWidget(self._vision_cb)
        opt2.addSpacing(18)
        self._enrich_cb = QCheckBox("Add facts beyond the lecture")
        self._enrich_cb.setToolTip(
            "Off by default — the lecture is the source of truth, and the tool "
            "finds cards only for what the material actually teaches.\n\nTick "
            "this to let it ALSO add closely-related standard facts the lecture "
            "implies but doesn't state (e.g. the other members of a class it only "
            "partly lists). Broader, but some points won't trace to the slides."
        )
        opt2.addWidget(self._enrich_cb)
        opt2.addSpacing(18)
        opt2.addWidget(QLabel("Notetype:"))
        self._nt_combo = QComboBox()
        self._nt_combo.setMinimumWidth(180)
        self._nt_combo.setToolTip("Restrict matches to one notetype, or search them all.")
        opt2.addWidget(self._nt_combo)
        opt2.addSpacing(18)
        # Demoted: the cache now self-invalidates when the extraction changes
        # (cache version bump), so a manual re-read is rarely needed — kept as a
        # small, unobtrusive toggle rather than a headline option.
        self._reextract_cb = QCheckBox("Re-read file")
        self._reextract_cb.setToolTip(
            "The AI read of a lecture is cached on disk, so running the same file "
            "again is instant and free. The cache clears itself whenever the "
            "extraction logic changes.\n\nTick this only to force a fresh read of "
            "a file whose CONTENTS changed but whose name didn't."
        )
        opt2.addWidget(self._reextract_cb)
        opt2.addStretch(1)
        setup.addLayout(opt2)

        # Give the native controls enough vertical room that a bumped Anki UI
        # font can't clip their tops (combos/buttons render a few px taller than
        # their text). A minimum, not a fixed size — they still grow with the font.
        for _ctl in (self._mode_combo, self._file_btn, self._lo_file_btn,
                     self._companion_btn, self._coverage_combo, self._cards_spin,
                     self._auto_cards_cb,
                     self._nt_combo):
            _ctl.setMinimumHeight(30)

        # Tag row — kept OUTSIDE the setup box so it stays put when the box
        # collapses; you often want to eyeball/adjust the tag over the results.
        tag_row = QHBoxLayout()
        lbl = QLabel("Tag as:")
        lbl.setFixedWidth(60)
        tag_row.addWidget(lbl)
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText("lecture::<name> — filled in from the file name")
        # Autocomplete against the collection's existing tags, like AI Browse:
        # so you can land matches under a branch you already use rather than
        # fat-fingering a near-duplicate of it.
        attach_tag_completer(self._tag_input)
        tag_row.addWidget(self._tag_input, 1)
        root.addLayout(tag_row)

        root.addWidget(self._hsep())

        # Progress + results
        self._sec("RESULTS", root)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        root.addWidget(self._progress)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: gray; font-size: 11px;")
        self._status_lbl.setWordWrap(True)
        root.addWidget(self._status_lbl)

        # Divert-all stepper state. The stepper itself is a FLOATING dialog (built
        # lazily in _ensure_stepper), not an inline bar: AI Lecture is a tab in the
        # same window as Browse/Create, so sending a gap there switches the tab
        # away from this panel — an inline bar would vanish. A floating dialog
        # stays visible over the Browse/Create tab so the user can step through.
        self._divert_queue: list = []
        self._divert_idx = 0
        self._divert_dest = ""
        self._stepper = None
        self._qbar_lbl = None
        self._qbar_next = None

        # Slide-attach preview state. Imagery detection + thumbnail rendering is
        # done ONCE per file (cached here, in the background when the checkbox is
        # ticked); the per-note plan (which slides a card gets) is then cheap to
        # recompute each render from point.refs ∩ imagery. Attach is OPT-IN:
        # `_slide_pick` holds the (nid, slide) pairs the user ticked; nothing is
        # attached until it's picked.
        self._imagery_slides: set = set()      # imagery-bearing slide numbers
        self._slide_thumbs: dict = {}          # slide no -> data-URI thumbnail
        self._slide_pick: set = set()          # {(nid, slide_no)} ticked by user
        self._thumbs_for_path = ""             # file the caches belong to
        self._computing_thumbs = False
        self._picks_seeded_for = ""            # file the auto-suggest ran for

        # Companion text (your notes / a transcript). `_companion` is the parsed
        # CompanionText for the run in flight — the panel widgets are the INPUT,
        # this is the snapshot the pipeline actually used, so the results screen
        # can still show a chunk's text after you've edited the box.
        self._companion_path = ""
        self._companion = None
        self._companion_file_chunks = None     # cached count; None = not read yet
        self._companion_truncated = False      # file hit the char budget; say so

        # Card-note picks, mirroring _slide_pick. `_notes_seeded` guards the
        # one-time pre-tick so a re-render can't resurrect a line the user
        # unticked; `_arming_note` is the companion-note index waiting for the
        # user to say which card it goes on (None = nothing armed).
        self._note_pick: set = set()           # {(nid, CardNote.key)}
        self._notes_seeded = False
        self._arming_note = None

        # The results surface is ONE rendered HTML document, not a stack of Qt
        # widgets — he found the hand-built widget rows unreadable and noted his
        # own HTML output looks far better. Every interaction routes back through
        # Anki's pycmd bridge to the same handlers (see _on_bridge).
        self._web = AnkiWebView(parent=self, title="ankisstant_lecture")
        self._web.setMinimumHeight(220)
        self._web.set_bridge_command(self._on_bridge, self)
        root.addWidget(self._web, 1)
        self._render_empty()

        btn_row = QHBoxLayout()
        # Shown only while results are up (setup collapsed) — the way back.
        self._new_btn = QPushButton("◀  New lecture")
        self._new_btn.setVisible(False)
        self._new_btn.setToolTip("Clear these results and set up another lecture.")
        self._new_btn.clicked.connect(self._new_lecture)
        btn_row.addWidget(self._new_btn)
        self._unsuspend_cb = QCheckBox("Unsuspend the matched cards")
        self._unsuspend_cb.setChecked(True)
        self._unsuspend_cb.setToolTip(
            "The actual payoff — these cards are sitting suspended until needed, "
            "and this is the lecture that needs them."
        )
        btn_row.addWidget(self._unsuspend_cb)
        self._attach_cb = QCheckBox("Suggest slides to attach…")
        self._attach_cb.setChecked(False)
        self._attach_cb.setToolTip(
            "Show each card's candidate source slide(s) — the diagram/table pages "
            "the point was drawn from — and PRE-TICK the high-yield ones (points "
            "about a mechanism, pathway, anatomy, table or graph). They go into "
            "the note's 'Lecture Notes' field.\n\n"
            "PDF only, and only slides that actually carry imagery (a plain bullet "
            "slide is skipped). The tested content (Text/Extra) is never touched, "
            "and running the same lecture twice won't stack duplicate images.\n\n"
            "Claude's picks are just a starting point — '＋ add' or '✓' any chip to "
            "adjust before you commit. Only ticked slides are attached."
        )
        self._attach_cb.toggled.connect(self._on_attach_toggled)
        btn_row.addWidget(self._attach_cb)
        self._notes_cb = QCheckBox("Add notes to cards…")
        self._notes_cb.setChecked(False)
        self._notes_cb.setToolTip(
            "Append a line of context to a card's 'Lecture Notes' field — a "
            "number, a caveat, a mnemonic, a pearl, or something the lecturer "
            "leaned on.\n\n"
            "The bar is deliberately high: a line has to add something the card "
            "doesn't already say AND have a reason to be on THAT card. Anything "
            "that merely restates the answer, or that you'd find in any textbook, "
            "is rejected. Most cards will get nothing — that's working as "
            "intended, not a miss.\n\n"
            "If you handed over your own notes above, those get placed here too, "
            "word for word. The tested content (Text/Extra) is never touched, and "
            "re-running the same lecture won't stack duplicates.\n\n"
            "Everything proposed is pre-ticked — untick any chip to drop it."
        )
        self._notes_cb.toggled.connect(self._on_notes_toggled)
        btn_row.addWidget(self._notes_cb)
        btn_row.addStretch(1)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self._stop_btn)
        self._run_btn = QPushButton("Find Cards")
        self._run_btn.setMinimumHeight(36)
        self._run_btn.setMinimumWidth(130)
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._start)
        btn_row.addWidget(self._run_btn)
        self._confirm_btn = QPushButton("Tag + Unsuspend")
        self._confirm_btn.setMinimumHeight(36)
        self._confirm_btn.setMinimumWidth(160)
        self._confirm_btn.setVisible(False)
        self._confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self._confirm_btn)
        root.addLayout(btn_row)

        self._load_cfg()
        self._refresh_notetypes()

    # -- small ui helpers --
    def _sec(self, text: str, layout):
        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 10px; font-weight: 700; color: gray; letter-spacing: 0.6px;")
        layout.addWidget(lbl)

    def _hsep(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet("color: palette(mid);")
        return f

    def _load_cfg(self):
        self.cfg = tool_config("lecture")
        # Coverage is one preset over BOTH target_points and depth: prefer a saved
        # "coverage", else best-fit whatever those two are (incl. advanced Settings
        # values) back onto a preset so the panel shows the right choice.
        _cov = self.cfg.get("coverage") or _coverage_from(
            int(self.cfg.get("target_points") or extractor.DEFAULT_TARGET_POINTS),
            self.cfg.get("depth") or matcher.DEFAULT_DEPTH)
        _ci = self._coverage_combo.findData(_cov)
        self._coverage_combo.setCurrentIndex(_ci if _ci >= 0 else self._coverage_combo.count() - 1)
        self._cards_spin.setValue(int(self.cfg.get("target_cards") or 60))
        # Default ON. The cap is documented as a soft ceiling that is never
        # padded to, so sizing it from the lecture can only relax a constraint —
        # and the typed value is left untouched underneath for when it's off.
        self._auto_cards_cb.setChecked(bool(self.cfg.get("auto_target_cards", True)))
        self._cards_spin.setEnabled(not self._auto_cards_cb.isChecked())
        self._vision_cb.setChecked(bool(self.cfg.get("use_vision", DEFAULT_USE_VISION)))
        self._enrich_cb.setChecked(bool(self.cfg.get("enrich", False)))
        self._attach_cb.setChecked(bool(self.cfg.get("attach_slides", False)))
        # Signals blocked: these two toggles PERSIST on change, and this is the
        # load path — letting them fire would write the config back while reading
        # it, on every showEvent. Their side effects are applied by hand below.
        _notes = bool(self.cfg.get("attach_notes", False))
        _comp = bool(self.cfg.get("companion_enabled", False))
        for _cb, _val in ((self._notes_cb, _notes), (self._companion_cb, _comp)):
            _blocked = _cb.blockSignals(True)
            _cb.setChecked(_val)
            _cb.blockSignals(_blocked)
        self._companion_widget.setVisible(_comp)
        if _comp:
            self._update_companion_hint()

    def _refresh_notetypes(self):
        cur = self._nt_combo.currentText()
        self._nt_combo.clear()
        self._nt_combo.addItem("All notetypes")
        try:
            for nt in sorted(mw.col.models.all_names_and_ids(), key=lambda n: n.name):
                self._nt_combo.addItem(nt.name)
        except Exception:
            pass
        idx = self._nt_combo.findText(cur)
        if idx >= 0:
            self._nt_combo.setCurrentIndex(idx)

    def showEvent(self, ev):
        super().showEvent(ev)
        try:
            self._load_cfg()
            self._refresh_notetypes()
        except Exception as e:
            log.error(f"lecture: panel refresh failed: {e}")

    def _clear_results(self):
        self._render_empty()

    def _set_setup_visible(self, on: bool):
        """Collapse the source+options box once results are up so the cards get
        the whole panel; expand it again for a new lecture. The web view keeps a
        taller floor while collapsed so the results fill the freed space."""
        self._setup_box.setVisible(on)
        self._new_btn.setVisible(not on)
        self._web.setMinimumHeight(220 if on else 420)

    def _new_lecture(self):
        """Back to a fresh setup. Results are cleared; the chosen file, options
        and tag are left as-is so tweak-and-rerun is one click (pick a new file
        to fully start over)."""
        if self._running:
            return
        self._fm_reset()
        self._coverage = None
        # Card-note picks belong to a result set, not to the panel — carrying
        # them into the next run would tick lines against cards that run never
        # chose. The companion INPUT (file + paste box) is deliberately kept,
        # like the file and the options, so tweak-and-rerun stays one click.
        self._note_pick.clear()
        self._notes_seeded = False
        self._arming_note = None
        self._confirm_btn.setVisible(False)
        self._clear_results()
        self._set_setup_visible(True)

    def _render_empty(self):
        self._web.stdHtml(
            "<div class='empty'>Pick a primary resource or paste learning "
            "objectives, then Find Cards.</div>",
            head=_results_head())

    # -- source mode --
    def _current_mode(self) -> str:
        return self._mode_combo.currentData() or "file"

    def _on_mode_changed(self):
        if self._fm_stage and not self._running:
            self._fm_reset()          # switching source cancels an in-progress hand-off
        lo = self._current_mode() == "lo"
        self._file_widget.setVisible(not lo)
        self._lo_widget.setVisible(lo)
        # Vision reads slide images — meaningless when there's no file. The cache
        # toggle still applies (LO extraction is cached too), so it stays live.
        self._vision_cb.setEnabled(not lo)
        self._confirm_btn.setVisible(False)
        self._update_run_enabled()

    def _on_lo_changed(self):
        n = len(extractor.split_objectives(self._lo_text.toPlainText()))
        self._lo_hint.setText(
            f"{n} objective(s) detected" if n else "")
        self._update_run_enabled()

    def _load_lo_file(self):
        """Read an LO document into the paste box: ingest its text, strip the
        course chrome, drop it in for the user to eyeball and edit. Reuses the
        LO (paste) engine — no vision, no document extraction."""
        start = self.cfg.get("last_file_dir") or ""
        exts = " ".join(f"*{s}" for s in sorted(ingest.SUPPORTED_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(
            self, "Load learning objectives", start,
            f"Documents ({exts});;All files (*)")
        if not path:
            return
        try:
            lecture = ingest.load(path)
        except ingest.UnsupportedLecture as e:
            showWarning(str(e))
            return
        except Exception as e:
            log.error(f"lecture: LO file load failed: {e}")
            showWarning(f"Couldn't read that file:\n\n{e}")
            return
        raw = "\n".join(s.text for s in lecture.segments if (s.text or "").strip())
        objectives = extractor.isolate_objectives(raw)
        if not extractor.split_objectives(objectives):
            showWarning(
                "No learning objectives found in that file. If it's a scanned "
                "PDF with no text layer, paste the objectives in by hand instead.")
            return
        self._lo_text.setPlainText(objectives)   # fires _on_lo_changed
        if not self._tag_input.text().strip():
            root = (self.cfg.get("tag_root") or "lecture").strip()
            leaf = anki_utils.slugify_tag_leaf(ingest._title_from_path(path))
            self._tag_input.setText(anki_utils.build_tag(root, leaf))
        self.cfg["last_file_dir"] = os.path.dirname(path)
        save_tool_config("lecture", self.cfg)

    def _update_run_enabled(self):
        if self._running:
            self._run_btn.setEnabled(False)
            return
        if self._current_mode() == "lo":
            ready = bool(self._lo_text.toPlainText().strip())
        else:
            ready = bool(self._path)
        self._run_btn.setEnabled(ready)

    # -- file --
    def _pick_file(self):
        start = self.cfg.get("last_file_dir") or ""
        exts = " ".join(f"*{s}" for s in sorted(ingest.SUPPORTED_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the primary resource", start,
            f"Lectures and transcripts ({exts});;All files (*)")
        if not path:
            return
        self._path = path
        if self._fm_stage and not self._running:
            self._fm_reset()          # a new file restarts the hand-off from step 1
        self._file_lbl.setText(os.path.basename(path))
        self._file_lbl.setStyleSheet("")
        self._update_run_enabled()
        self._confirm_btn.setVisible(False)
        if not self._tag_input.text().strip():
            root = (self.cfg.get("tag_root") or "lecture").strip()
            leaf = anki_utils.slugify_tag_leaf(ingest._title_from_path(path))
            self._tag_input.setText(anki_utils.build_tag(root, leaf))
        self.cfg["last_file_dir"] = os.path.dirname(path)
        save_tool_config("lecture", self.cfg)

    # -- companion text (your notes / a transcript) --
    def _on_companion_toggled(self, on: bool):
        self._companion_widget.setVisible(bool(on))
        self.cfg["companion_enabled"] = bool(on)
        save_tool_config("lecture", self.cfg)
        if on:
            self._update_companion_hint()

    def _pick_companion(self):
        start = (self.cfg.get("companion_last_dir")
                 or self.cfg.get("last_file_dir") or "")
        exts = " ".join(f"*{s}" for s in sorted(ingest.COMPANION_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a supplementary resource", start,
            f"Notes and transcripts ({exts});;All files (*)")
        if not path:
            return
        self._companion_path = path
        self._companion_file_chunks = None     # new file, re-count on next hint
        self._companion_truncated = False
        self._companion_lbl.setText(os.path.basename(path))
        self._companion_lbl.setStyleSheet("")
        self.cfg["companion_last_dir"] = os.path.dirname(path)
        save_tool_config("lecture", self.cfg)
        self._update_companion_hint()

    def _update_companion_hint(self):
        """Live count of what the box would hand over.

        The file is only re-read when one is chosen (reading it on every
        keystroke would be silly), so the pasted half is counted directly with
        ingest.chunk_notes and the file half is remembered from the pick.
        """
        if not self._companion_cb.isChecked():
            return
        pasted = self._companion_text.toPlainText()
        n_paste = len(ingest.chunk_notes(pasted)) if pasted.strip() else 0
        n_file = 0
        if self._companion_path:
            if self._companion_file_chunks is None:
                comp = ingest.load_companion(path=self._companion_path)
                self._companion_file_chunks = len(comp.chunks)
                self._companion_truncated = comp.truncated
            n_file = self._companion_file_chunks
        total = n_file + n_paste
        if not total:
            self._companion_hint.setText("")
            return
        bits = []
        if n_file:
            bits.append(f"{n_file} from the file")
        if n_paste:
            bits.append(f"{n_paste} pasted")
        # Truncation has to be said on screen, not just in the log. A 62k-char
        # transcript loses most of itself to the char budget, and a silent
        # two-thirds loss is indistinguishable from the feature working.
        cut = ("  ⚠ The file is too long — only the first part was kept. "
               "Trim it, or hand over your own notes instead."
               if getattr(self, "_companion_truncated", False) else "")
        self._companion_hint.setText(
            f"{total} note(s) to place — {', '.join(bits)}. "
            "Yours are never reworded; anything that can't be matched to a card "
            "is listed at the end of the results so you can place it yourself."
            + cut
        )

    # -- run --
    def _start(self):
        # File hand-off continuation: while we're partway through the two-step
        # file flow, the primary button advances it rather than starting over.
        if self._fm_stage == "await_points":
            self._fm_load_points()
            return
        if self._fm_stage == "await_verdicts":
            self._fm_load_verdicts()
            return

        mode = self._current_mode()
        lo_text = self._lo_text.toPlainText().strip()
        if not anki_utils.require_col() or self._running:
            return
        if mode == "file" and not self._path:
            return
        if mode == "lo" and not lo_text:
            return

        # Persist the knobs — these are the user's, and they are the whole point
        # of exposing them. tool_config/save_tool_config only ever writes the
        # keys given; nothing else in meta.json is touched.
        _cov = self._coverage_combo.currentData()
        _spec = COVERAGE_PRESETS.get(_cov, COVERAGE_PRESETS[DEFAULT_COVERAGE])
        self.cfg["coverage"] = _cov
        # Coverage is the source of truth; it maps to the concrete target_points +
        # depth the pipeline and the advanced Settings fields both read.
        self.cfg["target_points"] = extractor.DETAIL_TARGETS.get(
            _spec["detail"], extractor.DEFAULT_TARGET_POINTS)
        self.cfg["depth"] = _spec["depth"]
        self.cfg["target_cards"] = self._cards_spin.value()
        self.cfg["auto_target_cards"] = self._auto_cards_cb.isChecked()
        self.cfg["use_vision"] = self._vision_cb.isChecked()
        self.cfg["enrich"] = self._enrich_cb.isChecked()
        self.cfg["attach_slides"] = self._attach_cb.isChecked()
        self.cfg["companion_enabled"] = self._companion_cb.isChecked()
        save_tool_config("lecture", self.cfg)

        # Snapshot the companion text ONCE, here, rather than reading the widgets
        # from the worker thread. Everything downstream (extraction, the card-note
        # pass, the results screen) works off this object, so editing the box
        # mid-run can't change what the run was based on.
        if self._companion_cb.isChecked():
            self._companion = ingest.load_companion(
                path=self._companion_path,
                pasted=self._companion_text.toPlainText(),
            )
        else:
            self._companion = None

        cfg = dict(self.cfg)
        nt = self._nt_combo.currentText()
        cfg["notetype_filter"] = "" if nt == "All notetypes" else nt
        # Per-run only — deliberately NOT persisted, so "ignore cache" is a
        # one-shot after a prompt change rather than a setting that quietly
        # disables the cache forever.
        cfg["force_reextract"] = self._reextract_cb.isChecked()
        # The source: a file path, or pasted LO text. run_pipeline branches on
        # which one is present.
        cfg["source_mode"] = mode
        cfg["lo_text"] = lo_text if mode == "lo" else ""

        # No AI provider set up (copy/paste selected): run the two-step FILE
        # hand-off instead of blocking. The tool exports a prompt file, the user
        # runs it in any browser AI and saves the reply, and loads it back. No
        # vision — a file can't carry the slide images — so this is text-only.
        if is_manual_provider():
            if mode != "file":
                showWarning(
                    "The file hand-off needs a lecture FILE. Switch to the file "
                    "tab and choose one, or set up a CLI / API provider in "
                    "Ankisstant Settings to use pasted objectives.")
                return
            self._fm_start(cfg)
            return

        self._clear_results()
        self._matches, self._coverage = [], None
        # Card-note picks belong to the result set that produced them. A second
        # run without pressing "New lecture" must not inherit ticks made against
        # cards this run may not even choose — and must re-seed, or the fresh
        # proposals would all render greyed.
        self._note_pick.clear()
        self._notes_seeded = False
        self._arming_note = None
        # Tags may have changed since the last run (this tool itself adds them),
        # so a fresh run gets a fresh index rather than a stale session cache.
        self._tag_index = None
        self._open_points = set()
        self._running = True
        self._cancel_ev = threading.Event()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._run_btn.setEnabled(False)
        self._file_btn.setEnabled(False)
        self._confirm_btn.setVisible(False)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setStyleSheet("color: gray; font-size: 11px;")
        self._status_lbl.setText("Starting…")

        path, cancel_ev = self._path, self._cancel_ev
        companion = self._companion

        def _report(msg, pct):
            def _ui():
                self._status_lbl.setText(msg)
                self._progress.setValue(int(pct))
            mw.taskman.run_on_main(_ui)

        def _bg():
            core_api.set_cancel_event(cancel_ev)
            try:
                return run_pipeline(path, cfg, report=_report, cancel_ev=cancel_ev,
                                    companion=companion)
            finally:
                core_api.set_cancel_event(None)

        # uses_collection=False is not optional: the default puts this on
        # taskman's single collection executor, behind which Anki's periodic
        # backup queues and the app appears to hang for the whole run.
        mw.taskman.run_in_background(_bg, self._on_pipeline_done, uses_collection=False)

    def _cancel(self):
        if self._cancel_ev:
            self._cancel_ev.set()
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText("Stopping after the current step…")

    def _on_pipeline_done(self, fut):
        self._running = False
        self._stop_btn.setVisible(False)
        self._progress.setVisible(False)
        self._file_btn.setEnabled(True)
        self._update_run_enabled()
        try:
            result = fut.result()
        except Cancelled:
            self._status_lbl.setText("Stopped.")
            return
        except core_api.ClaudeCancelled:
            self._status_lbl.setText("Stopped.")
            return
        except ingest.UnsupportedLecture as e:
            self._status_lbl.setText("")
            showWarning(str(e))
            return
        except Exception as e:
            # Full traceback to the log, not just the message — a bare
            # "list index out of range" is un-diagnosable, and "check the
            # console" is only useful if the console actually has the trace.
            import traceback
            log.error(f"lecture: run failed: {e}\n{traceback.format_exc()}")
            self._status_lbl.setText("")
            showWarning(f"AI Lecture failed:\n\n{e}")
            return

        _lecture, matches, coverage = result
        self._matches, self._coverage = matches, coverage
        self._render(coverage)

    # -- file hand-off (no-provider) flow --
    # Two file round-trips through any browser AI, for users with no CLI/API key.
    # The tool does all the LOCAL work (ingest, deck search, allocation); the AI
    # does the two thinking steps via a .md the user feeds it and a .json it
    # returns. No vision — a file can't carry the slide images.
    def _fm_stem(self) -> str:
        base = os.path.splitext(os.path.basename(self._path or "lecture"))[0]
        return _re.sub(r"[^\w.-]+", "_", base) or "lecture"

    def _fm_dir(self) -> str:
        return (os.path.dirname(self._path) if self._path else "") \
            or (self.cfg.get("last_file_dir") or "") or os.path.expanduser("~")

    def _fm_reset(self):
        self._fm_stage = None
        self._fm_lecture = None
        self._fm_matches = []
        self._fm_scorable = []
        self._fm_cfg = {}
        self._run_btn.setText("Find Cards")
        self._update_run_enabled()

    def _fm_busy(self, on: bool, msg: str = ""):
        self._running = on
        self._run_btn.setEnabled(not on)
        self._file_btn.setEnabled(not on)
        self._progress.setVisible(on)
        if on:
            self._progress.setRange(0, 0)      # indeterminate; steps are quick
            self._status_lbl.setStyleSheet("color: gray; font-size: 11px;")
        if msg:
            self._status_lbl.setText(msg)

    def _fm_report(self, msg, pct):
        mw.taskman.run_on_main(lambda: self._status_lbl.setText(msg))

    def _fm_save(self, text: str, default_name: str) -> str:
        start = os.path.join(self._fm_dir(), default_name)
        out, _ = QFileDialog.getSaveFileName(
            self, "Save the file to give your AI", start, "Text (*.md *.txt)")
        if not out:
            return ""
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            showWarning(f"Couldn't write the file:\n\n{e}")
            return ""
        return out

    def _fm_load(self, title: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self, title, self._fm_dir(),
            "AI reply (*.json *.txt *.md);;All files (*)")
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            showWarning(f"Couldn't read that file:\n\n{e}")
            return None

    # Step 1 — ingest locally, write the extraction prompt, hand it over.
    def _fm_start(self, cfg: dict):
        self._fm_reset()
        self._clear_results()
        self._matches, self._coverage = [], None
        self._fm_cfg = cfg
        path = self._path
        tp = int(cfg.get("target_points") or extractor.DEFAULT_TARGET_POINTS)
        hy = bool(cfg.get("high_yield_only", True))
        use_ocr = bool(cfg.get("use_local_ocr", True))
        self._fm_busy(True, "Reading the file…")

        def _bg():
            lecture = ingest.load(path)
            # Some pages have no text layer; vision is off in this mode, so local
            # OCR (free) is the only thing that can recover them.
            if use_ocr and lecture.rasterisable and lecture.vision_segments:
                try:
                    ingest.ocr_fill(lecture)
                except Exception as e:
                    log.error(f"lecture/manual: OCR failed, continuing: {e}")
            md = manual.export_extract_request(lecture, target_points=tp, high_yield=hy)
            return lecture, md

        def _done(fut):
            self._fm_busy(False)
            try:
                lecture, md = fut.result()
            except ingest.UnsupportedLecture as e:
                self._status_lbl.setText("")
                showWarning(str(e))
                self._fm_reset()
                return
            except Exception as e:
                import traceback
                log.error(f"lecture/manual: ingest failed: {e}\n{traceback.format_exc()}")
                self._status_lbl.setText("")
                showWarning(f"Couldn't read that file:\n\n{e}")
                self._fm_reset()
                return
            self._fm_lecture = lecture
            out = self._fm_save(md, f"{self._fm_stem()}_step1_extract.md")
            if not out:
                self._status_lbl.setText("File hand-off cancelled.")
                self._fm_reset()
                return
            self._fm_stage = "await_points"
            self._run_btn.setText("Load points file →")
            self._run_btn.setEnabled(True)
            self._status_lbl.setText("Step 1 of 2 saved — run it in your AI, then load the reply.")
            showInfo(
                f"Step 1 of 2 saved to:\n{out}\n\n"
                "1. Open that file and paste all of it into ChatGPT, Claude, or Gemini.\n"
                "2. Save the AI's reply as a file (points.json, or any .txt).\n"
                "3. Back here, click “Load points file →”.",
                parent=self, title="AI Lecture — file hand-off")

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    # Step 2 — load the AI's points, search the deck, write the relevance prompt.
    def _fm_load_points(self):
        text = self._fm_load("Load the AI's points file")
        if text is None:
            return
        points = manual.parse_extract_reply(text, self._fm_lecture)
        if not points:
            showWarning(
                "No learning points were found in that file. Make sure you saved "
                "the AI's full reply (the JSON), then click “Load points file →” "
                "again.")
            return
        cfg = self._fm_cfg
        ntf = (cfg.get("notetype_filter") or "").strip()
        cap = matcher.resolve_candidate_cap(cfg)
        self._fm_busy(True, f"{len(points)} points — searching your deck…")

        def _bg():
            index = _on_main(matcher.TagIndex.build)
            matches = search_all(points, index=index, notetype_filter=ntf,
                                 candidate_cap=cap, on_main=_on_main, report=self._fm_report)
            items = _on_main(lambda: [
                (m.point, [(nid, matcher.note_text(nid)) for nid in m.nids])
                for m in matches if m.nids])
            md, scorable = manual.export_relevance_request(items)
            return matches, scorable, md

        def _done(fut):
            self._fm_busy(False)
            try:
                matches, scorable, md = fut.result()
            except Exception as e:
                import traceback
                log.error(f"lecture/manual: search failed: {e}\n{traceback.format_exc()}")
                self._status_lbl.setText("")
                showWarning(f"Deck search failed:\n\n{e}")
                return
            self._fm_matches = matches
            self._fm_scorable = scorable
            if not scorable:
                # Nothing to judge — every point is a gap or found no candidates.
                # Skip step 2 and finish straight away.
                self._fm_finalise([])
                return
            out = self._fm_save(md, f"{self._fm_stem()}_step2_relevance.md")
            if not out:
                self._status_lbl.setText("File hand-off cancelled.")
                self._fm_reset()
                return
            self._fm_stage = "await_verdicts"
            self._run_btn.setText("Load relevance file →")
            self._run_btn.setEnabled(True)
            self._status_lbl.setText("Step 2 of 2 saved — run it in your AI, then load the reply.")
            showInfo(
                f"Step 2 of 2 saved to:\n{out}\n\n"
                "1. Paste all of it into the same AI.\n"
                "2. Save its reply as a file (relevance.json, or any .txt).\n"
                "3. Back here, click “Load relevance file →” to see your cards.",
                parent=self, title="AI Lecture — file hand-off")

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    # Step 3 — load verdicts, apply, run the shared finish, show the results.
    def _fm_load_verdicts(self):
        text = self._fm_load("Load the AI's relevance file")
        if text is None:
            return
        verdicts = manual.parse_relevance_reply(text, self._fm_scorable)
        self._fm_finalise(verdicts)

    def _fm_finalise(self, verdicts):
        # Apply verdicts on the main thread (cheap, no collection touch) — same
        # assignment the automated relevance loop makes.
        by_point = {id(m.point): m for m in self._fm_matches}
        for (point, _cards), v in zip(self._fm_scorable, verdicts):
            m = by_point.get(id(point))
            if m is not None:
                m.kept = list(v.kept)
                m.scores = dict(v.scores)
                m.judge_slot = dict(v.slots)
                m.scored = True
        matches = self._fm_matches
        target_cards = int(self._fm_cfg.get("target_cards") or matcher.DEFAULT_TARGET_CARDS)
        depth = str(self._fm_cfg.get("depth") or matcher.DEFAULT_DEPTH)
        self._fm_busy(True, "Choosing the best notes…")

        def _bg():
            # No provider, so ai_dedupe_model=None (skips the semantic dedupe).
            return finish_matches(matches, target_cards=target_cards, depth=depth,
                                  on_main=_on_main, report=self._fm_report, ai_dedupe_model=None)

        def _done(fut):
            self._fm_busy(False)
            try:
                coverage = fut.result()
            except Exception as e:
                import traceback
                log.error(f"lecture/manual: finish failed: {e}\n{traceback.format_exc()}")
                self._status_lbl.setText("")
                showWarning(f"Couldn't finish:\n\n{e}")
                return
            self._matches, self._coverage = matches, coverage
            self._fm_reset()          # flow complete; button back to "Find Cards"
            self._render(coverage)

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    # -- results --
    def _render(self, coverage):
        self._coverage = coverage
        # Pre-tick the proposed lines before the first paint, so they show up
        # added rather than greyed. Guarded to run once per result set — after
        # that the pick set belongs to the user.
        self._seed_note_picks()
        self._status_lbl.setText(coverage.summary())
        self._status_lbl.setStyleSheet("font-weight: 600; font-size: 11px;")
        self._web.stdHtml(self._results_html(coverage),
                          head=_results_head())
        self._confirm_btn.setVisible(bool(coverage.selected_cards))
        self._confirm_btn.setEnabled(
            bool(coverage.new_cards or coverage.selected_cards))
        # If attach is on, build the slide thumbnails for this file once (bg);
        # the re-render on completion fills the previews in. No-op otherwise.
        self._maybe_build_previews()
        # Results are in — hand the panel over to the cards.
        self._set_setup_visible(False)

    # -- html builders --
    # The results are ONE html document. Each point is a disclosure block whose
    # header pycmd's a flip; each note row is a checkbox that pycmd's a toggle;
    # the re-search box and Browse link pycmd their own actions. Nothing here
    # decides anything — the handlers on the far side of the bridge are the same
    # ones the old widgets called, so the engine can't tell the surface changed.

    def _results_html(self, coverage) -> str:
        all_m = list(coverage.covered) + list(coverage.gaps)
        self._match_by_id = {i: m for i, m in enumerate(all_m)}
        idx = {id(m): i for i, m in enumerate(all_m)}

        parts: list[str] = []
        if getattr(coverage, "degraded_note", ""):
            parts.append(self._note_html(coverage.degraded_note, "err"))
        if coverage.budget_capped:
            parts.append(self._note_html(
                f"Your note budget ({coverage.budget}) is what limited this, not "
                f"your deck — raise it to see more.", "warn"))
        elif coverage.selected_cards < coverage.budget:
            # Under-spending is the NORMAL outcome, not a bug: each point takes
            # only as many notes as it has distinct things to say.
            parts.append(self._note_html(
                f"{coverage.selected_cards} of your {coverage.budget}-note budget "
                f"— each point took only as many notes as it had separate facts. "
                f"The unused slots would have been repeats.", "mut"))
        if coverage.covered_elsewhere:
            parts.append(self._note_html(
                f"{len(coverage.covered_elsewhere)} point(s) share a card with a "
                f"neighbouring point — covered there, so shown collapsed here "
                f"rather than as an empty gap.", "mut"))
        if coverage.uncovered_by_budget:
            parts.append(self._note_html(
                f"{len(coverage.uncovered_by_budget)} point(s) have notes but lost "
                f"the budget lottery. They're covered, not gaps.", "mut"))

        for m in coverage.covered:
            parts.append(self._point_html(m, idx[id(m)], is_gap=False))

        # One toolbar for ALL the gaps at once (topic + true), just above them.
        # Operates on the missed set only — the per-point buttons handle filled
        # points. Re-search-all runs a broad pass over every gap in turn; the two
        # "send all" buttons queue the gaps through Browse/Create one at a time.
        _tg = getattr(coverage, "topic_gaps", None) or []
        _rg = getattr(coverage, "true_gaps", None)
        if _rg is None:
            _rg = coverage.gaps
        n_gaps = len(_tg) + len(_rg)
        if n_gaps:
            parts.append(self._gaptools_html(n_gaps))

        # "No direct card" — the deck DOES carry these topics (tagged cards
        # exist, listed in each picker), there's just no card testing the exact
        # point. Kept apart from true gaps so "gap" never implies "your deck is
        # empty here" when it holds 25 cards on the topic.
        if getattr(coverage, "topic_gaps", None):
            parts.append(
                "<div class='gaphd topic'>No direct card — "
                f"{len(coverage.topic_gaps)} point(s) your deck covers the topic "
                "for, but no card tests the exact point"
                "<div class='sub'>Your cards for these topics are in each list "
                "below — tick any to include. Nothing is auto-added: they're the "
                "tangential matches the relevance filter deliberately drops.</div>"
                "</div>")
            for m in coverage.topic_gaps:
                parts.append(self._point_html(m, idx[id(m)], is_gap=True))

        true_gaps = getattr(coverage, "true_gaps", None)
        if true_gaps is None:      # older Coverage without the split
            true_gaps = coverage.gaps
        if true_gaps:
            fill = ("use AI Create if you want one" if tool_enabled("card_creator")
                    else "export them to make cards elsewhere")
            parts.append(
                "<div class='gaphd'>Gaps — "
                f"{len(true_gaps)} point(s) your deck doesn't cover"
                "<div class='sub'>Listed, not filled. Auto-generating here would "
                f"rebuild the thing this tool replaces — {fill}.</div></div>")
            for m in true_gaps:
                parts.append(self._point_html(m, idx[id(m)], is_gap=True))

        # Your own notes that couldn't be matched to a card. Last, because it's a
        # loose end rather than a result — but always shown, because the one thing
        # this feature must never do is quietly lose something you wrote down.
        parts.append(self._unplaced_html())

        body = "".join(parts) or "<div class='empty'>No points.</div>"
        # Restore scroll (the widget scrollbar is gone) and keep _scroll_y current
        # with a throttled ping, so the NEXT re-render lands in the same place.
        body += (
            "<script>window.scrollTo(0,%d);var _st;"
            "window.addEventListener('scroll',function(){if(_st)return;"
            "_st=setTimeout(function(){_st=null;"
            "pycmd('lec:scroll:'+Math.round(window.scrollY));},120);});"
            "</script>" % int(self._scroll_y))
        return body

    def _point_html(self, m, mid: int, is_gap: bool) -> str:
        chosen = list(m.final)
        chosen_set = set(chosen)
        rest = [n for n in m.candidates if n not in chosen_set][:self._OVERRIDE_LIMIT]
        # A point whose cards a sibling holds isn't a miss — its content IS on
        # screen, one block away — so it stays CLOSED. A gap or a real
        # budget-squeeze IS a miss the user may want to fix, so it opens: that's
        # exactly when they need the candidates and the re-search, and making them
        # hunt for a collapsed row would hide a fixable miss behind a dead end.
        sib = getattr(m, "covered_by", None) if not is_gap else None
        covered_elsewhere = sib is not None and not m.final
        open_ = (id(m) in self._open_points or is_gap
                 or (not m.final and not covered_elsewhere))

        if is_gap and getattr(m, "topic_present", False):
            # Deck has the topic (tagged cards in the picker), just no card for
            # this exact point. Named in words, not colour alone, and with the
            # count so the row says how many are there to take.
            n_topic = len(getattr(m, "topic_cards", []) or [])
            if n_topic:
                badge = (f"no direct card — {n_topic} card(s) you own on this "
                         "topic, none tests this exact point")
            else:
                badge = ("no direct card — your deck covers this topic, "
                         "none tests this exact point")
            cls = "pt topicgap"
        elif is_gap:
            badge = "gap — nothing in your deck tests this"
            cls = "pt gap"
        elif covered_elsewhere:
            # State named in WORDS, not by colour alone — a bare grey row reads as
            # "rejected" when the truth is the opposite.
            name = sib.point.concept
            if len(name) > 46:
                name = name[:45].rstrip() + "…"
            badge = f"covered by “{name}” — its card is above"
            cls = "pt covered"
        else:
            new_n, rot_n = len(m.new_nids), len(m.in_rotation_nids)
            bits = []
            if new_n:
                bits.append(f"{new_n} to unsuspend")
            if rot_n:
                bits.append(f"{rot_n} already studying")
            if not m.final:
                bits = ["no notes shown — budget went elsewhere"]
            te = getattr(m, "top_elsewhere", None)
            if m.final and te is not None:
                # The point got a card, but its STRONGEST match went to a sibling.
                # Say so in words so the weaker fallback on show isn't taken for the
                # best the deck has — the strong card is unsuspended, one block away.
                name = te.point.concept
                if len(name) > 46:
                    name = name[:45].rstrip() + "…"
                bits.append(f"strongest match shown under “{name}”")
            badge = " · ".join(bits)
            cls = "pt" if m.final else "pt empty"

        topic_cards = getattr(m, "topic_cards", []) or []
        topic_set = set(topic_cards)
        if topic_set:
            # Surface the provably-on-topic cards FIRST among the unchosen, so the
            # ones worth taking aren't buried under term-leg near-misses.
            rest = ([n for n in rest if n in topic_set]
                    + [n for n in rest if n not in topic_set])
        rows = [self._row_html(m, mid, nid, True, nid in topic_set) for nid in chosen]
        rows += [self._row_html(m, mid, nid, False, nid in topic_set) for nid in rest]
        inner = (f"<div class='lp'>{_esc(m.point.concept)}</div>"
                 + "".join(rows))
        # A topic_present gap owns cards filed under this exact topic that no card
        # tests the point with. Offer them as one click rather than making him
        # tick each; taking them settles the point as covered (is_gap yields to a
        # manual pick). Only cards not already taken are counted.
        untaken = [n for n in topic_cards if n not in chosen_set]
        if untaken:
            inner += (f"<button class='takebtn' "
                      f"onclick=\"pycmd('lec:taketopic:{mid}')\">"
                      f"Take {len(untaken)} card(s) you own on this topic</button>")
        # The action row is in EVERY point's body, not just gaps — a filled point
        # the user opens up gets Re-search / send-to-Browse / send-to-Create too.
        # (It's inside the collapsed <div class='lst'>, so it only shows when the
        # dropdown is open.) Replaces the old free-text term box.
        inner += self._actions_html(m, mid)

        arrow = "▾" if open_ else "▸"
        disp = "block" if open_ else "none"
        # Where the point came from, in words. A point the slides never taught is
        # a different kind of claim from one they did — if it's wrong, it's your
        # note that was wrong, and you can only know that if it's labelled.
        origin = ("<span class='tmark'>· from your notes</span>"
                  if getattr(m.point, "src", "") == "notes" else "")
        return (
            f"<div class='{cls}'>"
            f"<div class='hd' onclick=\"pycmd('lec:flip:{mid}')\">"
            f"<span class='arw'>{arrow}</span>"
            f"<span class='cpt'>{_esc(m.point.concept)}</span>{origin}"
            f"<span class='bdg'>{_esc(badge)}</span>"
            f"<span class='br' onclick=\"event.stopPropagation();"
            f"pycmd('lec:browse:{mid}')\">⌕ Browse</span>"
            f"</div>"
            f"<div class='lst' style='display:{disp}'>{inner}</div>"
            f"</div>")

    def _row_html(self, m, mid: int, nid: int, chosen: bool,
                  topic: bool = False) -> str:
        """One note row. Chosen and unchosen are styled IDENTICALLY — the tick is
        the only difference — and the whole list is merged so every candidate
        appears exactly once. Clicking the text expands it to more of the note.

        `topic` marks an unchosen card the tag leg proved is filed under this
        point's own topic (see PointMatch.topic_cards) — labelled so he can tell
        the on-topic cards worth taking from the term-leg near-misses below."""
        susp, total = m.susp.get(nid, (0, 0))
        try:
            short = matcher.note_text(nid, max_len=110)
            full = matcher.note_text(nid, max_len=420)
        except Exception:
            short = full = f"note {nid}"
        dot = "gray" if not chosen else (_c_new() if susp else _c_rot())
        cnt = f"{susp}/{total}" if susp else f"{total}"

        # No numeric score badge. Post-floor every visible card is a 2 or 3, so
        # "score 2/3 vs 3/3" on every row is decoration you'd never act on — the
        # score did its real work upstream (keep/drop). The ONE score-derived
        # signal worth reading survives: the tangential caveat, and only on the
        # cards that genuinely earn it. Fletcher: scores "meaningless… maybe just
        # one of them."
        flag = ("<span class='flag'>· closest match, may not directly answer</span>"
                if chosen and self._is_tangential(m, nid) else "")
        tmark = ("<span class='tmark'>· your card on this topic</span>"
                 if topic and not chosen else "")
        ck = "checked" if chosen else ""
        # More-of-the-note lives in a hidden span the text-click swaps in, so an
        # explanation that used to get cut off is one click from being read.
        short_span = f"<span class='s'>{_esc(short)}</span>"
        expand = (f"<span class='f'>{_esc(full)}</span>"
                  if len(full) > len(short) else "")
        # Slide-attach preview sits UNDER a chosen row: the candidate slide(s)
        # this note could get, each add-able — opt-in, nothing attached until picked.
        preview = (self._slide_preview_html(mid, nid, self._row_slides(m, nid))
                   if chosen else "")
        # Note chips sit alongside, above the slide thumbnails: a line of text is
        # quicker to judge than a picture, and when both are offered the text is
        # usually the one worth reading.
        notes = self._note_chips_html(mid, nid) if chosen else ""
        # While a stray note is armed, every chosen row offers itself as a home
        # for it — a two-click place with no new widgets and no drag targets.
        put = (f"<button class='sx putbtn' onclick=\"pycmd('lec:noteput:{nid}')\">"
               f"＋ here</button>"
               if chosen and self._arming_note is not None else "")
        return (
            f"<div class='row'>"
            f"<input type='checkbox' {ck} "
            f"onchange=\"pycmd('lec:toggle:{mid}:{nid}:'+(this.checked?1:0))\">"
            f"<span class='dot' style='color:{dot}'>●</span>"
            f"<span class='txt' onclick=\"this.classList.toggle('open')\">"
            f"{short_span}{expand}</span>"
            f"<span class='cnt'>{cnt}</span>{flag}{tmark}{put}"
            f"</div>{notes}{preview}")

    def _actions_html(self, m, mid: int) -> str:
        """The per-point action row, in every point's expanded body. Re-search is
        a broader, more lenient second pass (AI-suggested terms, wider net, keeps
        'closest match' cards); the two send buttons divert the point to AI Browse
        / AI Create — replacing the old editable-terms box (the user asked to send
        the point to Browse instead of retyping terms). The two send buttons only
        appear when their target tool is enabled in Settings → Tools; with both
        off, AI Lecture still stands alone (Re-search + the results/commit)."""
        parts = [
            "<div class='acts'>",
            f"<button class='actbtn' onclick=\"pycmd('lec:research:{mid}')\" "
            "title=\"Search again — wider net, keeps closest matches the first "
            "pass dropped\">↻ Re-search</button>",
        ]
        if tool_enabled("browse"):
            parts.append(
                f"<button class='actbtn' onclick=\"pycmd('lec:aibrowse:{mid}')\" "
                "title=\"Open this point in AI Browse to work it by hand\">"
                "→ AI Browse</button>")
        if tool_enabled("card_creator"):
            parts.append(
                f"<button class='actbtn' onclick=\"pycmd('lec:create:{mid}')\" "
                "title=\"Open this point in AI Create to make a card\">"
                "→ AI Create</button>")
        parts.append("</div>")
        return "".join(parts)

    def _gaptools_html(self, n: int) -> str:
        """The all-gaps toolbar: act on every unaddressed point at once. The
        Browse/Create batch buttons are shown only when that tool is enabled;
        Re-search and Export missing never depend on another tool."""
        parts = [
            "<div class='gaptools'>",
            f"<span class='glbl'>{n} unaddressed point(s):</span>",
            "<button class='actbtn' onclick=\"pycmd('lec:researchall')\">"
            "↻ Re-search all</button>",
        ]
        if tool_enabled("browse"):
            parts.append(
                "<button class='actbtn' onclick=\"pycmd('lec:divbrowseall')\">"
                "→ All to AI Browse</button>")
        if tool_enabled("card_creator"):
            parts.append(
                "<button class='actbtn' onclick=\"pycmd('lec:divcreateall')\">"
                "→ All to AI Create</button>")
        parts.append(
            "<button class='actbtn' onclick=\"pycmd('lec:exportgaps')\">"
            "⇩ Export missing…</button>")
        parts.append("</div>")
        return "".join(parts)

    def _note_html(self, text: str, kind: str) -> str:
        return f"<div class='msg {kind}'>{text}</div>"

    # -- the bridge: every pycmd from the html lands here --
    def _on_bridge(self, cmd: str):
        if not isinstance(cmd, str) or not cmd.startswith("lec:"):
            return
        _, action, rest = (cmd.split(":", 2) + ["", ""])[:3]
        if action == "scroll":
            try:
                self._scroll_y = int(rest)
            except Exception:
                pass
            return
        # Global gap actions carry no point id — handle before the id lookup.
        if action == "researchall":
            self._research_all()
            return
        if action == "divbrowseall":
            self._divert_all("browse")
            return
        if action == "divcreateall":
            self._divert_all("create")
            return
        if action == "exportgaps":
            self._export_gaps()
            return
        # The unplaced-note tray and its target buttons carry a note index or a
        # nid, not a point id — handle them before the point lookup below.
        if action == "notearm":
            try:
                self._arm_note(int(rest))
            except Exception:
                pass
            return
        if action == "noteput":
            try:
                self._put_note(int(rest))
            except Exception:
                pass
            return
        head = rest.split(":")
        try:
            m = self._match_by_id.get(int(head[0]))
        except Exception:
            m = None
        if m is None:
            return
        if action == "flip":
            self._open_points.discard(id(m)) if id(m) in self._open_points \
                else self._open_points.add(id(m))
            self._render(self._coverage)
        elif action == "toggle" and len(head) >= 3:
            try:
                self._toggle_override(m, int(head[1]), head[2] == "1")
            except Exception:
                pass
        elif action == "research":
            self._do_research(m)
        elif action == "create":
            self._send_point_to(m, "create")
        elif action == "aibrowse":
            self._send_point_to(m, "browse")
        elif action == "browse":
            self._open_browse(m)     # header ⌕ — Anki's native Browser
        elif action == "taketopic":
            self._take_topic(m)
        elif action == "slidepick" and len(head) >= 3:
            try:
                self._toggle_slide_pick(int(head[1]), int(head[2]))
            except Exception:
                pass
        elif action == "notepick" and len(head) >= 3:
            try:
                self._toggle_note_pick(int(head[1]), head[2])
            except Exception:
                pass

    def _open_browse(self, m):
        """#8: open Anki's own Browser on the EXACT search this point used, so he
        can see the full recall set the tool worked from and pick by hand."""
        terms = [t for t in m.point.terms if t.strip()]
        if not terms:
            tooltip("No search terms for this point.")
            return
        nf = (self.cfg.get("notetype_filter") or "").strip()
        query = " OR ".join(matcher._scoped(t, nf) for t in terms)
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            browser.form.searchEdit.lineEdit().setText(query)
            browser.onSearchActivated()
        except Exception as e:
            log.error(f"lecture: open Browser failed: {e}")
            tooltip("Couldn't open the Browser.")

    # -- manual override --
    # The judge is a filter, not an oracle. When the right card is visibly
    # sitting in the candidate list and got dropped (or squeezed out by the
    # budget), the user has to be able to overrule — otherwise the only recourse
    # is re-running the whole lecture and hoping. Collapsed by default so 25
    # points don't become a wall of checkboxes.
    _OVERRIDE_LIMIT = 14

    def _is_tangential(self, m, nid: int) -> bool:
        """A chosen note that doesn't really answer its point: it matched ZERO of
        the point's whole search terms AND the judge rated it only tangential (or
        the judge never ran). This is the "card doesn't directly answer the LP"
        case — usually a topic-neighbour dragged up for a low-yield point that
        has no true card. We FLAG it rather than hide it, so nothing vanishes."""
        whole = m.rank_debug.get(nid, (0, 0, 0))[0]
        if whole > 0:
            return False
        score = m.scores.get(nid)
        return score is None or score <= 1

    def _set_override(self, m, nid: int, on: bool):
        """Mutate one card's take/release state on the match. No re-render — the
        caller does that once, so a bulk take doesn't rebuild the html per card."""
        if on:
            if nid in m.manual_drop:
                m.manual_drop.remove(nid)
            if nid not in m.final and nid not in m.manual:
                m.manual.append(nid)
        else:
            if nid in m.manual:
                m.manual.remove(nid)
            if nid in m.final and nid not in m.manual_drop:
                m.manual_drop.append(nid)

    def _resummarise(self, m):
        """Re-derive the coverage from the mutated matches and re-render, keeping
        `m` open. is_gap flips when a gap point gains an override, so a point can
        move between the gap and covered lists — re-summarise rather than patch
        counters, or the headline disagrees with what's ticked on screen. Full
        re-render (not a counter patch) because a take/release also has to
        add/remove the row in the point's list above; the picker stays open and
        the scroll position is restored in the rebuilt html (via _scroll_y)."""
        cov = self._coverage
        if not cov:
            return
        fresh = matcher.summarise(cov.covered + cov.gaps, cov.budget).carry_run_state(cov)
        self._coverage = fresh
        self._open_points.add(id(m))
        self._render(fresh)

    def _toggle_override(self, m, nid: int, on: bool):
        """Take or release one card by hand, then refresh so the headline never
        disagrees with what's ticked on screen."""
        self._set_override(m, nid, on)
        self._resummarise(m)

    def _take_topic(self, m):
        """Take every one of a topic_present gap's own-topic cards at once (the
        'Take N cards' button). Same effect as ticking each by hand, but one
        re-render instead of N — and taking them settles the point as covered."""
        for nid in getattr(m, "topic_cards", []) or []:
            self._set_override(m, nid, True)
        self._resummarise(m)

    # -- re-search --
    # "Gap" means the first guess at the vocabulary failed, which is not the same
    # as the deck not having the card. The terms were written blind from the
    # slide's wording; by the time the gap is on screen there's better information
    # — which terms found nothing, which found the wrong thing — and that is what
    # this hands back to the model. There is no editable term box any more (the
    # user found it fiddly and would rather send the point to Browse): re-search
    # ALWAYS asks the AI for better terms, and does so DELIBERATELY BROADLY — a
    # wider candidate net (RESEARCH_CANDIDATE_LIMIT) and a lenient keep floor of 1
    # (RESEARCH_MIN_SCORE) so a "closest match" the first pass dropped is offered,
    # while a genuine 0 is still thrown out. `then` chains re-search-all.
    def _do_research(self, m, then=None):
        if self._running:
            tooltip("A lecture run is already going — wait for it to finish.")
            return

        # Card text for the prompt must be pulled here, on the main thread — the
        # worker below must never touch mw.col directly.
        snippets = {}
        for nid in m.nids[:14]:
            try:
                snippets[nid] = matcher.note_text(nid, max_len=140)
            except Exception:
                pass
        prompt = matcher.build_research_prompt(m, snippets)

        self._running = True
        tooltip("Re-searching…")
        cfg = self.cfg
        notetype_filter = (cfg.get("notetype_filter") or "").strip()
        model_r = tool_model_for("lecture_relevance")

        def _bg():
            terms = matcher.research_terms(prompt, model=model_r) or [
                t for t in m.point.terms if t.strip()]
            point = dataclasses.replace(m.point, terms=list(terms))

            def _search():
                if self._tag_index is None:
                    self._tag_index = matcher.TagIndex.build()
                # Wider net than a main-run search: keep more of the ranked pool
                # so the broader pass has something to be lenient about.
                fresh = matcher.search_point(
                    point, self._tag_index, notetype_filter=notetype_filter,
                    limit=matcher.RESEARCH_CANDIDATE_LIMIT)
                matcher.annotate_suspended(fresh)
                return fresh

            fresh = _on_main(_search)
            # Judge the new candidates exactly as the main run would — skipping
            # this would let a re-search inject unfiltered hits alongside cards
            # that were filtered, which is the one thing the coverage report must
            # never mix — but with the lenient floor so score-1 survives here.
            if fresh.nids:
                cards = _on_main(lambda: [
                    (nid, matcher.note_text(nid)) for nid in fresh.nids])
                verdicts = matcher.score_batch(
                    [(point, cards)], model=model_r,
                    min_score=matcher.RESEARCH_MIN_SCORE)
                if verdicts:
                    fresh.kept = list(verdicts[0].kept)
                    fresh.scores = dict(verdicts[0].scores)
                    fresh.judge_slot = dict(verdicts[0].slots)
                else:
                    fresh.kept = list(fresh.nids)
            else:
                fresh.kept = []
            fresh.scored = True
            return terms, fresh

        def _done(fut):
            self._running = False
            try:
                terms, fresh = fut.result()
            except matcher.RelevanceUsageExhausted as e:
                showWarning(str(e))
                return   # stop a chain: every remaining call would fail the same
            except core_api.ClaudeCancelled:
                return
            except Exception as e:
                import traceback
                log.error(f"lecture: re-search failed: {e}\n{traceback.format_exc()}")
                showWarning(f"Re-search failed:\n\n{e}")
                if then:
                    then()   # a one-off flake shouldn't stall re-search-all
                return
            self._apply_research(m, terms, fresh)
            if then:
                then()

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _research_all(self):
        """Re-search every gap in turn, one AI pass each, chained so only one runs
        at a time (the engine is single-threaded per run). Covered points are left
        alone — their per-point button handles them."""
        if self._running:
            tooltip("A lecture run is already going — wait for it to finish.")
            return
        cov = self._coverage
        gaps = list(getattr(cov, "gaps", []) or []) if cov else []
        if not gaps:
            tooltip("No gaps to re-search.")
            return
        queue = list(gaps)
        total = len(queue)

        def step():
            if not queue:
                tooltip(f"Re-searched {total} gap(s).", period=3000)
                return
            m = queue.pop(0)
            self._status_lbl.setText(
                f"Re-searching gap {total - len(queue)}/{total}…")
            self._do_research(m, then=step)

        step()

    def _apply_research(self, m, terms, fresh):
        """Fold a re-search result into the existing match, then re-run the
        whole allocation.

        The point's new cards have to compete for the budget with everyone
        else's — a re-searched point that helped itself to slots outside the
        allocator would be taking them from points that never got a vote.
        Re-allocating is also what lets the new cards inherit a proper weight.
        """
        found = len(fresh.kept)
        m.point = fresh.point            # carries the new terms, so the box persists
        m.nids = fresh.nids
        m.kept = fresh.kept
        m.per_term = fresh.per_term
        m.tag_hits = fresh.tag_hits
        m.rank_debug = fresh.rank_debug
        m.term_cover = fresh.term_cover
        m.scores = fresh.scores
        m.judge_slot = fresh.judge_slot
        m.susp.update(fresh.susp)
        m.scored = True
        # Overrides are the user's own decisions about specific notes and
        # survive a re-search untouched; they're note ids, still valid whether
        # or not the new search happens to return them.
        log.info(f"lecture: re-search '{m.point.topic}' with {terms} "
                 f"-> {len(fresh.nids)} candidate(s), {found} kept")

        cov = self._coverage
        if not cov:
            return
        all_matches = cov.covered + cov.gaps
        matcher.allocate(all_matches, cov.budget)
        fresh_cov = matcher.summarise(all_matches, cov.budget).carry_run_state(cov)
        self._coverage = fresh_cov
        self._render(fresh_cov)
        tooltip(f"{found} note(s) found." if found else
                "Still nothing — send it to Browse or Create instead.")

    # -- divert to Browse / Create --
    def _send_point_to(self, m, tool: str) -> bool:
        """Divert one point to AI Browse ('browse') or AI Create ('create'): open
        the Ankisstant window on that tool, switch to it, and prefill it with the
        point's concept. Soft bridge — the target tool may be absent (standalone
        build) — so it degrades to copying the concept to the clipboard.

        Reaches the panel through the tool module's `_panel` global (get_panel()
        returns a QScrollArea wrapper, not the panel), and sets the topic LAST,
        after the tool is shown, so nothing on the show path clears it."""
        key = "browse" if tool == "browse" else "card_creator"
        label = "AI Browse" if tool == "browse" else "AI Create"
        if not tool_enabled(key):
            # The button should be hidden, but a stale render could still fire it.
            tooltip(f"{label} is turned off in Settings → Tools.")
            return False
        concept = m.point.concept
        try:
            from ...ui import main_window as mwnd
            if tool == "browse":
                from .. import browse as mod
                mod.get_panel()                    # build/cache the panel
                mwnd.open_main_window()
                win = getattr(mwnd, "_current", None)
                if win is not None and hasattr(win, "show_browse_tool"):
                    win.show_browse_tool()
                p = getattr(mod, "_panel", None)
                if p is not None:
                    p.topic.setText(concept)
                label = "AI Browse"
            else:
                from .. import card_creator as mod
                mod.get_panel()
                mwnd.open_main_window()
                win = getattr(mwnd, "_current", None)
                if win is not None and hasattr(win, "show_create_tool"):
                    win.show_create_tool()
                p = getattr(mod, "_panel", None)
                if p is not None:
                    # Topic mode first — the topic box is hidden unless rb_topic is
                    # checked, so setting text alone would drop it into a hidden field.
                    p.rb_topic.setChecked(True)
                    p._sync_mode()
                    p.topic.setText(concept)
                label = "AI Create"
            tooltip(f"Sent to {label} — review it before running.", period=3000)
            return True
        except Exception as e:
            log.error(f"lecture: hand-off to {tool} failed: {e}")
            try:
                mw.app.clipboard().setText(concept)
                tooltip("That tool isn't available — concept copied to clipboard.",
                        period=4000)
            except Exception:
                showWarning(f"Couldn't hand this off:\n\n{e}")
            return False

    def _divert_all(self, dest: str):
        """Queue every gap through Browse/Create one at a time — each tool works a
        single topic, so the floating stepper sends the first and lets the user
        step to the next when they're done with it."""
        key = "browse" if dest == "browse" else "card_creator"
        if not tool_enabled(key):
            label = "AI Browse" if dest == "browse" else "AI Create"
            tooltip(f"{label} is turned off in Settings → Tools.")
            return
        cov = self._coverage
        gaps = list(getattr(cov, "gaps", []) or []) if cov else []
        if not gaps:
            tooltip("No gaps to send.")
            return
        self._divert_queue = gaps
        self._divert_idx = 0
        self._divert_dest = dest
        self._divert_send_current()

    def _ensure_stepper(self):
        """The floating divert-all stepper — a small modeless dialog parented to
        Anki's main window (NOT this panel), so it stays up when the tab switches
        to Browse/Create."""
        if self._stepper is not None:
            return
        from aqt.qt import QDialog
        dlg = QDialog(mw)
        dlg.setWindowTitle("AI Lecture — sending gaps")
        try:
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.Tool
                               | Qt.WindowType.WindowStaysOnTopHint)
        except Exception:
            pass
        lay = QVBoxLayout(dlg)
        self._qbar_lbl = QLabel("")
        self._qbar_lbl.setWordWrap(True)
        self._qbar_lbl.setMinimumWidth(320)
        lay.addWidget(self._qbar_lbl)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Done")
        done.clicked.connect(self._divert_finish)
        row.addWidget(done)
        self._qbar_next = QPushButton("Next gap →")
        self._qbar_next.setDefault(True)
        self._qbar_next.clicked.connect(self._divert_next)
        row.addWidget(self._qbar_next)
        lay.addLayout(row)
        dlg.finished.connect(lambda _=0: self._divert_finish())
        self._stepper = dlg

    def _divert_send_current(self):
        q, i = self._divert_queue, self._divert_idx
        if i >= len(q):
            self._divert_finish()
            return
        m = q[i]
        dest_label = "AI Browse" if self._divert_dest == "browse" else "AI Create"
        self._send_point_to(m, self._divert_dest)
        self._ensure_stepper()
        last = i == len(q) - 1
        concept = m.point.concept
        if len(concept) > 90:
            concept = concept[:89].rstrip() + "…"
        self._qbar_lbl.setText(
            f"Sending gaps → {dest_label}   ·   {i + 1} of {len(q)}\n\n{concept}")
        self._qbar_next.setText("Finish" if last else "Next gap →")
        self._stepper.show()
        self._stepper.raise_()

    def _divert_next(self):
        self._divert_idx += 1
        if self._divert_idx >= len(self._divert_queue):
            self._divert_finish()
        else:
            self._divert_send_current()

    def _divert_finish(self):
        n = len(self._divert_queue)
        stepped = self._divert_idx + 1 if n else 0
        if self._stepper is not None:
            self._stepper.hide()
        self._divert_queue = []
        self._divert_idx = 0
        if n:
            tooltip(f"Done — stepped through {min(stepped, n)} of {n} gap(s).",
                    period=2500)

    # -- this run, in the shared pipeline shape --
    def _work_item(self):
        """This lecture as a pipeline.WorkItem — the TAKE IN stage's output."""
        title = os.path.splitext(os.path.basename(self._path or ""))[0]
        if not title:
            title = ("your objectives" if (self.cfg.get("lo_text") or "").strip()
                     else "lecture")
        return pipeline.WorkItem.from_lecture(self._path or "", title)

    def _plan_for(self, matches, *, concept: str = ""):
        """`matches` as a pipeline.Plan.

        A lecture's extraction is a bare list of points, which is why it could
        reach the FIND stage but never the WRITE stage — gen_prompt reads the
        card budget, the constraints and the tag off a PLAN, and a list has
        nowhere to keep them. This is that missing wrapper, built from what the
        run already holds.

        Pass `coverage.gaps` to plan what the deck DOESN'T cover (the write
        case) or covered+gaps for the whole lecture.
        """
        pts = [m.point for m in (matches or []) if getattr(m, "point", None)]
        tag = self._tag_input.text().strip()
        return pipeline.plan_from_lecture(
            self._work_item(), pts,
            # 0 means "size it from the points" — the same reasoning
            # matcher.auto_target_cards applies, rather than a fixed default.
            cards=(0 if self._auto_cards_cb.isChecked()
                   else self._cards_spin.value()),
            tags=[tag] if tag else [],
            concept=concept,
        )

    # -- export the gaps for a third-party generator --
    def _export_gaps(self):
        """Write the MISSING learning points to a markdown file the user can hand
        to a third-party card generator.

        Exported set = `coverage.gaps` (is_gap: scored, no card the deck kept for
        the point, and no manual pick). That is exactly the points the user walks
        away with no deck card for and the deck can't supply one — the honest
        "make a card" list. The INVERSE is left out on purpose: a point that still
        has a card in play — selected, squeezed out by the budget (deck has it,
        just raise the budget), or shared with a sibling (covered elsewhere) — all
        sit in `covered`, never `gaps`, so exporting them would ask a generator to
        duplicate a card the deck already owns. A gap the user overrode by hand is
        settled as covered (is_gap yields to a manual pick) and drops out too."""
        cov = self._coverage
        gaps = list(getattr(cov, "gaps", []) or []) if cov else []
        if not gaps:
            tooltip("No missing points to export — every point has a deck card.")
            return
        text = self._build_gap_export(cov)
        out = self._fm_save(text, f"{self._fm_stem()}_missing_points.md")
        if not out:
            return
        self.cfg["last_file_dir"] = os.path.dirname(out)
        showInfo(
            f"Exported {len(gaps)} missing learning point(s) to:\n\n{out}\n\n"
            "Feed this to any card generator, then import the cards you get back.")

    def _build_gap_export(self, cov) -> str:
        """Markdown for the missing-points file. True gaps (deck has nothing on
        the topic) and topic gaps (deck has the topic tagged but no card tests
        this exact point) are split, mirroring the on-screen coverage report, so
        the reader knows which ones risk duplicating a card already in the deck."""
        true_gaps = list(getattr(cov, "true_gaps", None) or [])
        topic_gaps = list(getattr(cov, "topic_gaps", None) or [])
        if not true_gaps and not topic_gaps:      # older Coverage without the split
            true_gaps = list(cov.gaps)

        stem = self._fm_stem()
        n = len(true_gaps) + len(topic_gaps)
        lines = [
            f"# Missing learning points — {stem}",
            "",
            f"{n} learning point(s) from this lecture have no existing card in the "
            "deck. Generate a card for each one below.",
            "",
        ]

        def block(m) -> None:
            p = m.point
            lines.append(f"### {p.concept}")
            tag = " > ".join(x for x in (p.system, p.subsystem, p.topic) if x)
            if tag:
                lines.append(f"- Topic: {tag}")
            terms = [t for t in (p.terms or []) if t.strip()]
            if terms:
                lines.append(f"- Search terms tried: {', '.join(terms)}")
            if p.refs:
                lines.append(f"- Slide(s): {', '.join(str(r) for r in p.refs)}")
            lines.append("")

        if true_gaps:
            lines.append("## Not in your deck at all")
            lines.append("")
            for m in true_gaps:
                block(m)
        if topic_gaps:
            lines.append("## Deck has the topic, but no card tests this exact point")
            lines.append(
                "*(You may already own a related card — check before generating.)*")
            lines.append("")
            for m in topic_gaps:
                block(m)

        return "\n".join(lines).rstrip() + "\n"

    # -- commit --
    def _on_confirm(self):
        if not anki_utils.require_col() or not self._coverage:
            return
        tag = self._tag_input.text().strip()
        if not tag:
            showWarning("Type a tag before confirming.")
            return

        nids: list[int] = []
        seen: set[int] = set()
        # Gaps are included too: a gap point the user overrode has a card in
        # `final`, and committing everything except his overrides would silently
        # discard the one decision he made by hand.
        for m in self._coverage.covered + self._coverage.gaps:
            for nid in m.final:
                if nid not in seen:
                    seen.add(nid)
                    nids.append(nid)
        if not nids:
            tooltip("Nothing to commit.")
            return

        do_unsuspend = self._unsuspend_cb.isChecked()
        do_attach = self._attach_cb.isChecked()
        do_notes = self._notes_cb.isChecked()
        n_picked = len({nid for (nid, _s) in self._slide_pick})
        # Count both the lines and the notes they land on. "a line of notes to N
        # note(s)" read as "every card gets a line", which is the opposite of
        # what the bar is for — say how many of the committed notes are actually
        # touched, so a pass that found three pearls doesn't look like a pass
        # that annotated everything.
        committing = set(nids)
        n_noted = len({nid for (nid, _k) in self._note_pick} & committing)
        n_lines = sum(1 for (nid, _k) in self._note_pick if nid in committing)
        new_n = self._coverage.new_cards

        # Say exactly what will be written, counted from the actual picks. With
        # nothing ticked it must still say the notes are untouched — the promise
        # that this tool doesn't edit cards is the reason it gets trusted with a
        # 103k-card collection.
        additions = []
        if do_notes and n_noted:
            additions.append(f"{n_lines} line(s) of text to {n_noted} of them")
        if do_attach and n_picked:
            additions.append(f"the slide(s) you picked to {n_picked} of them")
        if additions:
            written = (" and ".join(additions)
                       + ", in the 'Lecture Notes' field; the tested content "
                         "(Text/Extra) is never modified.")
            body = f"\n\nThis will add {written}"
        else:
            body = "\n\nCard content is never modified."

        if not askUser(
            f"Tag {len(nids)} note(s) as '{tag}'"
            + (f" and unsuspend {new_n} card(s)?" if do_unsuspend and new_n else "?")
            + body
        ):
            return

        cids: list[int] = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            cids.extend(note.card_ids() if hasattr(note, "card_ids")
                        else [c.id for c in note.cards()])

        # One checkpoint around everything, so one undo reverses the whole run.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("AI Lecture: tag + unsuspend")
        anki_utils.tag_notes(nids, tag)
        audit = (self.cfg.get("audit_tag") or "").strip()
        if audit and audit != tag:
            anki_utils.tag_notes(nids, audit)
        mtag = month_tag()
        if mtag and mtag != tag:
            anki_utils.tag_notes(nids, mtag)

        unsuspended = 0
        if do_unsuspend and cids:
            before = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
            anki_utils.unsuspend_cards(cids)
            after = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
            unsuspended = before - after

        # Notes before slides so the text lands above the images in the field.
        noted = self._attach_notes(nids) if do_notes else 0
        attached = self._attach_slides(nids) if do_attach else 0

        self._confirm_btn.setEnabled(False)
        self._status_lbl.setText(
            f"Done — {len(nids)} note(s) tagged '{tag}'"
            + (f", {unsuspended} card(s) unsuspended" if do_unsuspend else "")
            + (f", notes added to {noted}" if do_notes and noted else "")
            + (f", slides added to {attached} note(s)" if do_attach and attached else "")
            + "."
        )
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass

    # -- slide-attach preview --
    def _on_attach_toggled(self, on: bool):
        """Ticking turns on the per-note preview and seeds Claude's suggested
        picks; unticking clears the picks and the seed flag (so re-ticking
        suggests fresh). Thumbnails are built lazily by the _render hook."""
        if not on:
            self._slide_pick.clear()
            self._picks_seeded_for = ""
        elif self._thumbs_for_path == self._path:
            # cache already built this session → suggest right away
            self._seed_suggested_picks()
        if self._coverage:
            self._render(self._coverage)

    def _imagery_slides_cached(self) -> set:
        """Imagery-bearing slide numbers for the current file — from the preview
        cache when it's for this file, else detected now."""
        if self._thumbs_for_path == self._path and self._imagery_slides:
            return self._imagery_slides
        try:
            return ingest.detect_imagery(self._path)
        except Exception as e:
            log.warn(f"lecture: imagery detection failed: {e}")
            return set()

    def _maybe_build_previews(self):
        """Build slide thumbnails once per file, in the background, when attach is
        on. Called from _render; guarded so it fires once and never re-enters."""
        if (not self._attach_cb.isChecked() or not self._path
                or self._computing_thumbs
                or self._thumbs_for_path == self._path
                or os.path.splitext(self._path)[1].lower() != ".pdf"
                or not self._coverage):
            return
        self._computing_thumbs = True
        path = self._path
        # Only slides the points actually cite can ever be shown — render just those.
        cited = sorted({r for m in (self._coverage.covered + self._coverage.gaps)
                        for r in (getattr(m.point, "refs", []) or [])})

        def _bg():
            imagery = ingest.detect_imagery(path, cited) if cited else set()
            paths = ingest.render_segment_paths(path, sorted(imagery))
            thumbs = {}
            for n, p in paths.items():
                uri = _on_main(lambda p=p: self._slide_thumb_datauri(p))
                if uri:
                    thumbs[n] = uri
            return imagery, thumbs

        def _done(fut):
            self._computing_thumbs = False
            try:
                imagery, thumbs = fut.result()
            except Exception as e:
                log.warn(f"lecture: slide preview build failed: {e}")
                self._thumbs_for_path = path   # don't spin retrying a broken file
                return
            self._imagery_slides = imagery
            self._slide_thumbs = thumbs
            self._thumbs_for_path = path
            self._seed_suggested_picks()   # free auto-suggest, once per file
            if self._coverage:
                self._render(self._coverage)

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _seed_suggested_picks(self):
        """Pre-tick the slides Claude suggests — those whose learning point is the
        kind a figure genuinely helps (see `_point_wants_figure`). Runs ONCE per
        file; any nudging the user does afterward is preserved (the seed flag
        stops it re-seeding on later renders)."""
        if (self._thumbs_for_path != self._path or not self._coverage
                or self._picks_seeded_for == self._path):
            return
        self._picks_seeded_for = self._path
        imagery = self._imagery_slides
        for m in self._coverage.covered + self._coverage.gaps:
            if not _point_wants_figure(m.point):
                continue
            refs = [r for r in sorted(set(getattr(m.point, "refs", []) or []))
                    if r in imagery]
            if not refs:
                continue
            for nid in m.final:            # gaps have no chosen notes → skipped
                for r in refs:
                    self._slide_pick.add((nid, r))

    def _slide_thumb_datauri(self, path: str, max_dim: int = 260) -> str:
        """A small base64 JPEG data-URI for a rendered slide — the results web
        view can't load arbitrary local files, so previews are inlined. Returns
        '' on any failure, and the row falls back to a plain 'slide N' label."""
        try:
            from aqt.qt import QImage, QByteArray, QBuffer, QIODevice
            img = QImage(path)
            if img.isNull():
                return ""
            if max(img.width(), img.height()) > max_dim:
                img = img.scaled(
                    max_dim, max_dim,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "JPG", 78)
            buf.close()
            import base64
            b64 = base64.b64encode(bytes(ba.data())).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            log.warn(f"lecture: thumb render failed for {path}: {e}")
            return ""

    def _row_slides(self, m, nid: int) -> list[int]:
        """Slide numbers a chosen note would receive (point.refs ∩ imagery), for
        the inline preview. Empty unless attach is on with the cache built."""
        if not self._attach_cb.isChecked() or self._thumbs_for_path != self._path:
            return []
        refs = getattr(m.point, "refs", None) or []
        return [r for r in sorted(set(refs)) if r in self._imagery_slides]

    def _slide_preview_html(self, mid: int, nid: int, slides: list[int]) -> str:
        """The 'attach a slide?' strip under a chosen note: a thumbnail per
        candidate slide, greyed until ticked. Nothing attaches unless picked."""
        if not slides:
            return ""
        n_pick = sum(1 for s in slides if (nid, s) in self._slide_pick)
        chips = []
        for s in slides:
            picked = (nid, s) in self._slide_pick
            thumb = self._slide_thumbs.get(s, "")
            img = (f"<img class='sthumb' src=\"{thumb}\">" if thumb else "")
            cls = "schip picked" if picked else "schip"
            act = "✓ added" if picked else "＋ add"
            chips.append(
                f"<span class='{cls}'>{img}"
                f"<span class='slab'>slide {s}</span>"
                f"<button class='sx' onclick=\"pycmd('lec:slidepick:{mid}:{nid}:{s}')\">"
                f"{act}</button></span>")
        cap = (f"attaching {n_pick} ✓ · adjust" if n_pick
               else "suggested: none · add?")
        return ("<div class='sprev'><span class='scap'>" + cap + "</span>"
                + "".join(chips) + "</div>")

    # -- card notes --
    def _notes_for(self, nid: int) -> list:
        """Proposed lines for one card, in the order the pass returned them."""
        cov = self._coverage
        if not cov or not getattr(cov, "card_notes", None):
            return []
        return [n for n in cov.card_notes if n.nid == nid]

    def _note_chips_html(self, mid: int, nid: int) -> str:
        """The 'add this line?' strip under a chosen note.

        Everything the pass proposed is PRE-TICKED — it already applied a
        deliberately hard bar, so making the user re-approve each line would be
        asking them to redo the judgement they turned the feature on to avoid.
        Untick to drop. Nothing here is a picture: a slide chip and a note chip
        can both appear under one card, and they're different offers.
        """
        notes = self._notes_for(nid)
        if not notes:
            return ""
        chips = []
        n_pick = 0
        for n in notes:
            picked = (nid, n.key) in self._note_pick
            n_pick += 1 if picked else 0
            cls = "nchip picked" if picked else "nchip"
            act = "✓ added" if picked else "＋ add"
            # The source is named in words, not by colour: "note 3" vs "slide 14"
            # is the difference between your words and the lecture's, and that
            # has to be readable, not inferred from a tint.
            origin = n.src
            if n.kind == "note":
                origin = "from the transcript" if getattr(n, "tidied", False) \
                    else "your note"
            why = f" — {n.why}" if n.why else ""
            chips.append(
                f"<span class='{cls}' title=\"{_esc_attr(origin + why)}\">"
                f"<span class='nsrc'>{_esc(origin)}</span>"
                f"<span class='ntxt'>{_esc(n.text)}</span>"
                f"<button class='sx' onclick=\"pycmd('lec:notepick:{mid}:{nid}:{n.key}')\">"
                f"{act}</button></span>")
        cap = (f"adding {n_pick} ✓ · adjust" if n_pick
               else "none selected · add?")
        return ("<div class='nprev'><span class='scap'>" + cap + "</span>"
                + "".join(chips) + "</div>")

    def _toggle_note_pick(self, nid: int, key: str):
        pair = (nid, key)
        self._note_pick.discard(pair) if pair in self._note_pick \
            else self._note_pick.add(pair)
        if self._coverage:
            self._render(self._coverage)

    def _seed_note_picks(self):
        """Pre-tick every proposed line, once per result set.

        Runs once (guarded by `_notes_seeded`) so a later re-render can't undo an
        untick: after the first pass the pick set is the user's, not ours.
        """
        cov = self._coverage
        if self._notes_seeded or not cov or not getattr(cov, "card_notes", None):
            return
        self._notes_seeded = True
        for n in cov.card_notes:
            self._note_pick.add((n.nid, n.key))

    def _on_notes_toggled(self, on: bool):
        """The 'Add notes to cards…' switch.

        Off clears the picks, so unticking the box always means "attach nothing"
        — never a set of stale picks lying in wait for the next commit. It does
        NOT re-run the pass: the proposals are already computed for this result
        set, and re-ticking simply offers them again.
        """
        if not on:
            self._note_pick.clear()
            # Clear the seed guard too, so re-ticking offers the proposals again
            # rather than showing them all greyed with no way back.
            self._notes_seeded = False
        else:
            self._seed_note_picks()
        self.cfg["attach_notes"] = bool(on)
        save_tool_config("lecture", self.cfg)
        if self._coverage:
            self._render(self._coverage)

    def _unplaced_html(self) -> str:
        """The tray of notes that couldn't be placed on any card.

        Nothing the user wrote is ever dropped silently — that's the whole
        contract of handing your own notes over. Clicking one ARMS it; every
        chosen row then grows a '＋ here' button until it's placed or cancelled.
        """
        cov = self._coverage
        if not self._notes_cb.isChecked() or not cov:
            return ""
        idxs = getattr(cov, "unplaced_notes", None) or []
        chunks = {c.index: c.text for c in
                  (getattr(self._companion, "chunks", None) or [])}
        rows = []
        for i in idxs:
            text = chunks.get(i)
            if not text:
                continue
            armed = (self._arming_note == i)
            cls = "nchip picked" if armed else "nchip"
            act = "choose a card ▾" if armed else "＋ place…"
            rows.append(
                f"<span class='{cls}'>"
                f"<span class='ntxt'>{_esc(text)}</span>"
                f"<button class='sx' onclick=\"pycmd('lec:notearm:{i}')\">"
                f"{act}</button></span>")
        if not rows:
            return ""
        hint = ("Pick a card above — every chosen note now has a '＋ here' button."
                if self._arming_note else
                "These didn't clearly belong to any card the search found. "
                "Place one yourself, or leave it.")
        return ("<div class='gaphd'>Your notes with nowhere to go</div>"
                f"<div class='nprev'><span class='scap'>{hint}</span>"
                + "".join(rows) + "</div>")

    def _arm_note(self, idx: int):
        """Toggle 'I want to place note N'. A second click cancels."""
        self._arming_note = None if self._arming_note == idx else idx
        if self._coverage:
            self._render(self._coverage)

    def _put_note(self, nid: int):
        """Place the armed note onto this card, ticked and ready to commit."""
        idx = self._arming_note
        self._arming_note = None
        chunk = next((c for c in (getattr(self._companion, "chunks", None) or [])
                      if c.index == idx), None)
        cov = self._coverage
        if chunk is None or not cov:
            return
        note = matcher.CardNote(
            nid=nid, src=f"note {idx}", text=chunk.text,
            why="you placed this", kind="note", note_index=idx,
        )
        cov.card_notes = list(getattr(cov, "card_notes", None) or []) + [note]
        cov.unplaced_notes = [i for i in (getattr(cov, "unplaced_notes", None) or [])
                              if i != idx]
        self._note_pick.add((nid, note.key))
        self._render(cov)

    def _toggle_slide_pick(self, nid: int, slide: int):
        key = (nid, slide)
        self._slide_pick.discard(key) if key in self._slide_pick \
            else self._slide_pick.add(key)
        if self._coverage:
            self._render(self._coverage)

    def _attach_slides(self, note_nids: list[int]) -> int:
        """Append source-slide images to each committed note's slide field.

        WHICH slides: for every point that selected the note, its cited slides
        (`point.refs`) INTERSECTED with the lecture's imagery-bearing pages. refs
        supplies "this slide taught the point"; imagery supplies "there's actually
        something to look at" — the intersection keeps decorative title/closing
        pages out while catching dedicated diagram/table slides, which are the
        high-yield ones. PDF-only, since only PDFs rasterise.

        Non-destructive and re-run-safe: writes only into the resolved slide field
        (AnKing 'Lecture Notes', or a configured one) — never the tested Text/Extra
        — and skips any image already present in that field, so a second run over
        the same lecture doesn't stack duplicate <img> tags. Runs inside the same
        checkpoint as tag + unsuspend, so one undo reverses everything together.
        Returns the number of notes that gained at least one new image.
        """
        if not self._path or not self._coverage:
            return 0

        # Imagery-bearing pages — from the preview cache if it's for this file,
        # else computed now (commit without ever building the preview).
        imagery = self._imagery_slides_cached()
        if not imagery:
            return 0

        # nid -> ordered slide numbers (refs ∩ imagery), unioned across every
        # point that put this note into its final set. Only committed notes count,
        # and any (nid, slide) the user STRUCK in the preview is skipped — the
        # commit attaches exactly what the preview showed as kept.
        want = set(note_nids)
        slides_by_nid: dict[int, list[int]] = {}
        for m in self._coverage.covered + self._coverage.gaps:
            refs = [r for r in sorted(set(getattr(m.point, "refs", []) or []))
                    if r in imagery]
            if not refs:
                continue
            for nid in m.final:
                if nid not in want:
                    continue
                cur = slides_by_nid.setdefault(nid, [])
                for r in refs:
                    if r not in cur and (nid, r) in self._slide_pick:
                        cur.append(r)
        slides_by_nid = {k: v for k, v in slides_by_nid.items() if v}
        if not slides_by_nid:
            return 0

        # Render + import each needed slide ONCE, sharing the stored media
        # filename across every note that cites it.
        wanted_slides = sorted({r for rs in slides_by_nid.values() for r in rs})
        paths = ingest.render_segment_paths(self._path, wanted_slides)
        fname_by_slide: dict[int, str] = {}
        for n, p in paths.items():
            fn = media.import_image_to_media(p)
            if fn:
                fname_by_slide[n] = fn
        if not fname_by_slide:
            return 0

        cfg_field = (self.cfg.get("slide_field") or "").strip()
        touched = 0
        for nid, slides in slides_by_nid.items():
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            field = self._slide_field_for(note, cfg_field)
            if not field:
                continue  # no Lecture Notes / configured field — don't pollute Extra
            existing = note[field] or ""
            frags: list[str] = []
            for n in slides:
                fn = fname_by_slide.get(n)
                if not fn or fn in existing:   # re-run guard: already attached
                    continue
                frags.append(f'<img src="{_esc_attr(fn)}">')
            if not frags:
                continue
            note[field] = (existing + "<br>" if existing else "") + "<br>".join(frags)
            try:
                mw.col.update_note(note)
                touched += 1
            except Exception:
                try:
                    note.flush()   # legacy fallback for older Anki
                    touched += 1
                except Exception as e:
                    log.warn(f"lecture: could not attach slides to note {nid}: {e}")
        return touched

    def _attach_notes(self, note_nids: list[int]) -> int:
        """Append the ticked lines to each committed note's Lecture Notes field.

        Only what the user actually ticked (`_note_pick`) is written — the pass
        proposes and pre-ticks, but the pick set is the authority, so unticking a
        chip is the whole veto.

        Text goes in BEFORE any images (see _attach_slides, called after this):
        a line you can read beats a slide you have to squint at, and when the
        line already carries what the slide was for, the picture is just weight.

        Same guarantees as the slide half: writes only into the resolved field,
        never the tested Text/Extra; skips a line already present so a second run
        over the same lecture can't stack duplicates; runs inside the shared
        checkpoint so one undo reverses the lot. Returns the number of notes that
        gained at least one new line.
        """
        cov = self._coverage
        if not cov or not getattr(cov, "card_notes", None):
            return 0

        want = set(note_nids)
        by_nid: dict[int, list] = {}
        for n in cov.card_notes:
            if n.nid in want and (n.nid, n.key) in self._note_pick:
                by_nid.setdefault(n.nid, []).append(n)
        if not by_nid:
            return 0

        cfg_field = (self.cfg.get("slide_field") or "").strip()
        touched = 0
        for nid, notes in by_nid.items():
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            field = self._slide_field_for(note, cfg_field)
            if not field:
                continue  # no Lecture Notes / configured field — never touch Extra
            existing = note[field] or ""
            frags: list[str] = []
            for n in notes:
                body = _esc(n.text)
                # Re-run guard, on the TEXT rather than a marker: the same line
                # from the same lecture renders identically, so its presence is
                # the check. Matching on the escaped form is what makes an
                # apostrophe or an ampersand compare equal to what's stored.
                if body and body not in existing and not any(body == f for f in frags):
                    frags.append(body)
            if not frags:
                continue
            note[field] = (existing + "<br>" if existing else "") + "<br>".join(frags)
            try:
                mw.col.update_note(note)
                touched += 1
            except Exception:
                try:
                    note.flush()   # legacy fallback for older Anki
                    touched += 1
                except Exception as e:
                    log.warn(f"lecture: could not attach notes to note {nid}: {e}")
        return touched

    @staticmethod
    def _slide_field_for(note, configured: str) -> str | None:
        """Field that slide images and card notes go into: the configured field if
        present on this note, else AnKing's 'Lecture Notes'. Returns None when
        neither exists — unlike card_creator's resolver we deliberately DON'T fall
        back to Extra, which on a cloze note is a tested reference field where
        neither slides nor notes belong."""
        keys = set(note.keys())
        if configured and configured in keys:
            return configured
        if "Lecture Notes" in keys:
            return "Lecture Notes"
        return None


def _esc(s: str, limit: int | None = None) -> str:
    """Truncate FIRST, then escape. The other order can cut an entity in half
    ('&amp;' -> '&am') and the renderer shows the wreckage literally."""
    s = s or ""
    if limit is not None and len(s) > limit:
        s = s[:limit] + "…"
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(s: str) -> str:
    """As _esc, but also escape the double-quote — this goes inside value="…"."""
    return _esc(s).replace('"', "&quot;")


# The whole results surface's look lives here, so it's one place to tune rather
# than a style string on every widget. Reads the viewer's own palette via
# Anki's CSS variables where it can, so it sits right in light and dark. Bigger
# type than the old widgets (his ask), disclosure rows, and chosen/unchosen
# note rows styled IDENTICALLY — the tick is the only difference.
_RESULTS_CSS = """
body { font-size: 13px; line-height: 1.4; margin: 0; padding: 4px 2px 40px; }
.empty { color: var(--ank-muted, #888); font-size: 12px; padding: 14px 6px; }
.msg { font-size: 12px; padding: 5px 8px; margin: 3px 2px; border-radius: 4px; }
.msg.err { color: #fff; background: var(--ank-error, #c0392b); }
.msg.warn { color: var(--ank-warn, #7a5b00); background: rgba(184,134,11,.16); }
.msg.mut { color: var(--ank-muted, #888); }
.gaphd { font-size: 13px; font-weight: 600; color: var(--ank-error, #c05050); padding: 14px 4px 2px; }
/* "No direct card" heading — amber, not the red of a true gap: the deck HAS the
   topic, so it must not read as "your deck is empty here". */
.gaphd.topic { color: var(--ank-warn, #b8860b); }
.gaphd .sub { font-size: 11px; font-weight: 400; color: var(--ank-muted, #888); padding-top: 2px; }

.pt { margin: 2px 0; border-radius: 5px; }
.pt .hd {
  display: flex; align-items: baseline; gap: 7px; cursor: pointer;
  padding: 6px 6px; border-radius: 5px;
}
.pt .hd:hover { background: rgba(128,128,128,.10); }
.pt .arw { color: var(--ank-muted, #888); width: 11px; flex: none; font-size: 11px; }
.pt .cpt { font-weight: 600; flex: 1 1 auto; }
.pt.empty .cpt, .pt.gap .cpt { color: var(--ank-warn, #b8860b); }
.pt.gap .cpt { color: var(--ank-error, #c05050); }
/* Covered-by-a-sibling: a calm, non-alarming state. NOT the yellow of a squeeze
   or the red of a gap — the point is fine, its card is just shown next door. */
.pt.covered .cpt { color: inherit; opacity: .72; }
.pt.covered .bdg { color: var(--ank-ok, #3c8f5a); white-space: normal; }
/* "No direct card": deck carries the topic but no card tests this exact point.
   Amber like the budget/empty state — a caveat, not the red "your deck lacks
   this" of a true gap. Long badge wraps rather than truncating. */
.pt.topicgap .cpt { color: var(--ank-warn, #b8860b); }
.pt.topicgap .bdg { color: var(--ank-warn, #b8860b); white-space: normal; }
.pt .bdg { color: var(--ank-muted, #888); font-size: 11px; flex: none; white-space: nowrap; }
.pt .br {
  color: var(--ank-accent, #4a90d9); font-size: 11px; flex: none; cursor: pointer;
  white-space: nowrap; opacity: .75; padding-left: 4px;
}
.pt .br:hover { opacity: 1; text-decoration: underline; }

.lst { padding: 2px 4px 8px 22px; }
.lst .lp { color: var(--ank-muted, #888); font-size: 11px; padding: 1px 0 6px; }

.row { display: flex; align-items: baseline; gap: 7px; padding: 3px 2px; }
.row input { margin: 0; flex: none; position: relative; top: 1px; }
.row .dot { flex: none; font-size: 11px; }
.row .txt { flex: 1 1 auto; cursor: pointer; }
.row .txt .f { display: none; }
.row .txt.open .s { display: none; }
.row .txt.open .f { display: inline; }
.row .cnt { color: var(--ank-muted, #888); font-size: 11px; flex: none; }
.row .flag { color: var(--ank-warn, #b8860b); font-size: 11px; }
.row .why { color: var(--ank-muted, #888); font-size: 11px; }

.acts { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 2px 2px; }
.actbtn {
  font-size: 12px; padding: 3px 10px; border-radius: 4px; cursor: pointer;
  border: 1px solid rgba(128,128,128,.4); background: rgba(128,128,128,.10);
  color: inherit;
}
.actbtn:hover { background: rgba(128,128,128,.22); }
.gaptools {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  padding: 10px 6px; margin: 8px 0 2px; border-radius: 5px;
  background: rgba(128,128,128,.07);
}
.gaptools .glbl { font-size: 11px; color: var(--ank-muted, #888); margin-right: 4px; }
.row .tmark { color: var(--ank-warn, #b8860b); font-size: 11px; }
.sprev {
  display: flex; flex-wrap: wrap; align-items: flex-start; gap: 8px;
  padding: 4px 0 8px 26px;
}
.sprev .scap {
  font-size: 10px; color: var(--ank-muted, #888); text-transform: uppercase;
  letter-spacing: .5px; align-self: center;
}
.schip {
  display: inline-flex; flex-direction: column; align-items: center; gap: 2px;
  padding: 4px; border: 1px solid rgba(128,128,128,.35); border-radius: 5px;
  background: rgba(128,128,128,.06);
}
.schip .sthumb {
  max-width: 130px; max-height: 90px; border-radius: 3px; display: block;
}
.schip .slab { font-size: 10px; color: var(--ank-muted, #888); }
.schip .sx {
  font-size: 11px; padding: 1px 8px; border-radius: 3px; cursor: pointer;
  border: 1px solid rgba(128,128,128,.4); background: transparent; color: inherit;
}
.schip .sx:hover { background: rgba(60,143,90,.18); }
/* Unpicked = candidate: greyed until you add it. Picked = full colour, green. */
.schip .sthumb { filter: grayscale(1); opacity: .55; }
.schip.picked {
  border-color: rgba(60,143,90,.55); background: rgba(60,143,90,.10);
}
.schip.picked .sthumb { filter: none; opacity: 1; }
.schip.picked .slab { color: var(--ank-ok, #3c8f5a); }
.schip.picked .sx {
  border-color: rgba(60,143,90,.55); color: var(--ank-ok, #3c8f5a);
}
.schip.picked .sx:hover { background: rgba(192,80,80,.18); }
/* Card notes. Same picked/unpicked language as the slide chips, but laid out as
   rows of text rather than a thumbnail strip — a line of prose needs width, and
   these have to be READ before they're judged, not glanced at. */
.nprev {
  display: flex; flex-direction: column; align-items: stretch; gap: 5px;
  padding: 4px 0 8px 26px;
}
.nprev .scap {
  font-size: 10px; color: var(--ank-muted, #888); text-transform: uppercase;
  letter-spacing: .5px;
}
.nchip {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 5px 8px; border: 1px solid rgba(128,128,128,.35); border-radius: 5px;
  background: rgba(128,128,128,.06); opacity: .6;
}
.nchip .nsrc {
  font-size: 10px; color: var(--ank-muted, #888); white-space: nowrap;
  text-transform: uppercase; letter-spacing: .4px; padding-top: 2px;
}
.nchip .ntxt { flex: 1; font-size: 12px; line-height: 1.45; }
.nchip .sx {
  font-size: 11px; padding: 1px 8px; border-radius: 3px; cursor: pointer;
  white-space: nowrap; border: 1px solid rgba(128,128,128,.4);
  background: transparent; color: inherit;
}
.nchip .sx:hover { background: rgba(60,143,90,.18); }
.nchip.picked {
  opacity: 1; border-color: rgba(60,143,90,.55); background: rgba(60,143,90,.10);
}
.nchip.picked .nsrc { color: var(--ank-ok, #3c8f5a); }
.nchip.picked .sx {
  border-color: rgba(60,143,90,.55); color: var(--ank-ok, #3c8f5a);
}
.nchip.picked .sx:hover { background: rgba(192,80,80,.18); }
.row .putbtn {
  font-size: 11px; padding: 1px 8px; margin-left: 8px; border-radius: 3px;
  cursor: pointer; color: var(--ank-accent, #4a90d9);
  border: 1px solid rgba(74,144,217,.5); background: rgba(74,144,217,.10);
}
.takebtn {
  font-size: 12px; padding: 3px 10px; border-radius: 4px; cursor: pointer;
  margin: 6px 6px 0 0; color: var(--ank-warn, #b8860b);
  border: 1px solid rgba(184,134,11,.5); background: rgba(184,134,11,.10);
}
.takebtn:hover { background: rgba(184,134,11,.20); }
"""


def _results_head() -> str:
    """The <style> block for the results webview.

    The bridge's `:root{--ank-*}` declarations go FIRST, then the stylesheet
    that reads them. Order doesn't actually matter — CSS custom properties
    resolve at use time — but it keeps the generated part visible at the top
    when you inspect the page.

    Without SynapsePro `css_vars()` returns "", none of the `--ank-*` names are
    ever defined, and every `var(--ank-NAME, <literal>)` falls back to the colour
    this file has always used. That's what makes "standalone is unchanged"
    checkable by reading the diff rather than by opening every screen twice.

    Worth knowing: this surface had **no dark-mode rules at all** before the
    bridge, so with SynapsePro on it now tracks the theme for the first time.
    """
    from ...core import synapse
    return synapse.css_vars() + f"<style>{_RESULTS_CSS}</style>"


# ── tool entry points ────────────────────────────────────────────────────────

_panel: LecturePanel | None = None


def init(main_window) -> None:
    return


def get_panel() -> QWidget:
    global _panel
    if _panel is None:
        _panel = LecturePanel()
    return _panel
