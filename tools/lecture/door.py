# AI Lecture's door into the work-through.
#
# A lecture used to run its whole life on its own page: read, search, an HTML
# results report, and its own Tag + Unsuspend. Writing cards for what the deck
# didn't have was bolted on afterwards by handing the finished search to the
# work-through. Two result screens for one question.
#
# Now the lecture is a LEARNING-POINT CREATOR and nothing more. The AI Lecture
# tab is the intake form (file or pasted objectives, the supplementary
# resource, the coverage dial); pressing its button opens the work-through on
# the lecture, and every stage after that happens there:
#
#   2 READ     read_material() — the same extraction, cache, OCR and vision
#   3 APPROVE  the points, grouped into the lecture's SECTIONS, on the plan
#              table — and the same point-review dialog the gaps use
#   4 FIND     the same search and judge, plus the Lecture Notes pass
#   5 WRITE    cards for whatever the deck didn't have, grounded in the slides
#   7 REVIEW   the tree, with a side panel for slides and Lecture Notes
#   8 COMMIT   one Apply: tag, unsuspend, add — and staple the picked slides
#              and notes onto found AND written cards
#
# This module is everything only a lecture has. It is a mixin on
# BulkSearchDialog rather than a subclass or a second dialog, because the
# whole point is that there is one screen; every method is a no-op unless the
# dialog was opened with a lecture.
#
# Why SECTIONS: a forty-point lecture as one heading is a wall. The extractor
# already files every point under system › subsystem, which is the lecture's
# own structure, so those become the rows — each a work item with its own tag
# box (prefilled with the lecture's tag) and its own Find/Write ticks. The card
# budget is still the LECTURE's: sections are a grouping, not a re-plan, so a
# lecture finds exactly the cards it found before.

from __future__ import annotations

import dataclasses
import hashlib
import os
from dataclasses import dataclass, field

from ...core import log


# ── the run, as the intake form hands it over ────────────────────────────────

@dataclass
class LectureRun:
    """Everything the AI Lecture tab collected, snapshotted at the press.

    Snapshotted — not read live from the tab's widgets — so editing the form
    while the work-through is open can't change what this run was based on.
    """
    path: str = ""                 # the file, or "" for pasted objectives
    lo_text: str = ""              # pasted objectives, or ""
    cfg: dict = field(default_factory=dict)   # tool_config("lecture") + per-run keys
    companion: object = None       # ingest.CompanionText, or None
    tag: str = ""                  # the lecture's tag, from the intake form
    title: str = ""
    # Filled by the READ stage.
    lecture: object = None         # ingest.Lecture, or None on the LO path
    points: list = field(default_factory=list)

    @property
    def is_pdf(self) -> bool:
        return os.path.splitext(self.path or "")[1].lower() == ".pdf"

    @property
    def base_id(self) -> str:
        """Stable per lecture, so a section's id is the same run to run."""
        src = self.path or self.lo_text
        return "lec:" + hashlib.sha1(src.encode("utf-8", "replace")).hexdigest()[:10]

    def record(self) -> dict:
        """The one work item the run view opens on, before the read: the
        lecture itself. Replaced by its sections once it has been read."""
        return {"id": self.base_id, "kg_id": self.base_id,
                "title": self.title or "Lecture", "type": "", "tags": [],
                "fields": {"notes": self._intake_summary()}}

    def _intake_summary(self) -> str:
        bits = []
        if self.path:
            bits.append(os.path.basename(self.path))
        elif self.lo_text:
            n = len([ln for ln in self.lo_text.splitlines() if ln.strip()])
            bits.append(f"{n} pasted objective line(s)")
        if self.companion is not None and getattr(self.companion, "chunks", None):
            bits.append(f"+ {len(self.companion.chunks)} note(s) from your "
                        "supplementary resource")
        return " ".join(bits)


# ── sections: points → work items (pure, testable) ───────────────────────────

# A section's lecture excerpt rides on its record as the writer's context, so
# a card written for a gap is grounded in what the lecturer actually said
# rather than in the model's general knowledge. Capped: it goes into a prompt
# alongside every point in the section.
EXCERPT_CHARS = 3500


def _human(level: str) -> str:
    return " ".join(str(level or "").replace("_", " ").split())


def _section_key(pt) -> tuple:
    sub = (pt.subsystem or pt.topic or "").strip()
    return ((pt.system or "").strip().casefold(), sub.casefold())


def slide_span(refs) -> str:
    """[4, 5, 6, 9] -> "slides 4–6, 9"."""
    nums = sorted({int(r) for r in (refs or []) if isinstance(r, int) or str(r).isdigit()})
    if not nums:
        return ""
    runs, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    txt = ", ".join(f"{a}" if a == b else f"{a}–{b}" for a, b in runs)
    return ("slide " if len(nums) == 1 else "slides ") + txt


def build_sections(run: LectureRun, points: list, *, type_key: str = "",
                   excerpt_chars: int = EXCERPT_CHARS):
    """Group a lecture's points into its sections. Returns [(record, Plan)].

    Grouped on system › subsystem, in the order the lecture first reaches each
    one — which is the order it teaches them. Every point is stamped with its
    section's id (`kg_id`): the results tree and the commit both group on it.
    """
    from .. import pipeline

    order: list = []
    groups: dict = {}
    for pt in points or []:
        k = _section_key(pt)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(pt)

    systems = {k[0] for k in order if k[0]}
    many_systems = len(systems) > 1
    segs = {}
    if run.lecture is not None:
        segs = {s.number: (s.text or "") for s in getattr(run.lecture, "segments", [])}

    out = []
    for i, k in enumerate(order, 1):
        pts = groups[k]
        first = pts[0]
        name = _human(first.subsystem or first.topic) or _human(first.system) \
            or "General"
        if many_systems and first.system and _human(first.system) != name:
            name = f"{_human(first.system)} · {name}"
        refs = sorted({r for p in pts for r in (p.refs or [])})
        sid = f"{run.base_id}#{i}"
        for p in pts:
            p.kg_id = sid
        notes = []
        span = slide_span(refs)
        if span and segs:
            body = []
            used = 0
            for r in refs:
                t = " ".join((segs.get(r) or "").split())
                if not t:
                    continue
                chunk = f"[{r}] {t}"
                if used + len(chunk) > excerpt_chars:
                    chunk = chunk[:max(0, excerpt_chars - used)].rstrip() + "…"
                body.append(chunk)
                used += len(chunk)
                if used >= excerpt_chars:
                    break
            if body:
                notes.append(f"From the lecture ({span}):\n" + "\n".join(body))
        item = pipeline.WorkItem(
            id=sid, title=name, kind=pipeline.KIND_LECTURE, type_key=type_key,
            tags=[run.tag] if run.tag else [],
            fields={"notes": "\n\n".join(notes)},
            source={"path": run.path, "refs": refs},
        )
        plan = pipeline.Plan(item=item, concept=name, scope="broad",
                             cards=len(pts), breadth="wide", points=list(pts))
        out.append((item.to_kg_record(concept=name), plan))
    return out


def slides_for_points(points, imagery) -> list:
    """The slides a card answering these points could carry: what the points
    cite, intersected with the pages that actually have something to look at.
    refs says "this slide taught it"; imagery says "there's a picture on it" —
    the intersection keeps title and closing pages out."""
    imagery = set(imagery or ())
    out: list = []
    for p in points or []:
        for r in sorted(set(getattr(p, "refs", []) or [])):
            if r in imagery and r not in out:
                out.append(r)
    return out


def suggested(points, imagery) -> list:
    """The pre-ticked subset: only for points a figure genuinely helps."""
    from .panel import _point_wants_figure
    return slides_for_points([p for p in (points or []) if _point_wants_figure(p)],
                             imagery)


def missing_points_markdown(title: str, true_gaps: list, topic_gaps: list) -> str:
    """The missing points as a file for another card generator — the old AI
    Lecture export, with the slide numbers each point came from."""
    n = len(true_gaps) + len(topic_gaps)
    lines = [f"# Missing learning points — {title}", "",
             f"{n} learning point(s) from this lecture have no existing card in "
             "the deck. Generate a card for each one below.", ""]

    def block(p) -> None:
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
        lines += ["## Not in your deck at all", ""]
        for p in true_gaps:
            block(p)
    if topic_gaps:
        lines += ["## Deck has the topic, but no card tests this exact point",
                  "*(You may already own a related card — check before "
                  "generating.)*", ""]
        for p in topic_gaps:
            block(p)
    return "\n".join(lines).rstrip() + "\n"


# ── the mixin ────────────────────────────────────────────────────────────────

class LectureDoorMixin:
    """The lecture's stages, on BulkSearchDialog. See the module comment."""

    # -- setup -----------------------------------------------------------------

    def _lec_init(self, lecture) -> None:
        self._lecture = lecture if isinstance(lecture, LectureRun) else None
        # File hand-off (no-provider) state: None → await_points → None, and
        # None → await_verdicts → None for the search half.
        self._lec_fm_stage = None
        self._lec_fm_matches: list = []
        self._lec_fm_scorable: list = []
        # Slides. Built once per run, in the background, after the search:
        # which cited pages carry imagery, and a rendered PNG for each.
        self._lec_imagery: set = set()
        self._lec_slide_png: dict = {}       # slide no -> rendered file path
        self._lec_slides_state = "none"      # none | building | ready | n/a
        self._lec_pix: dict = {}             # slide no -> scaled QPixmap
        # Picks. Found cards: {(nid, slide)} and {(nid, CardNote.key)}. A
        # written card carries its own picks as card["lecture_slides"], because
        # it has no nid until it is added.
        self._lec_slide_pick: set = set()
        self._lec_note_pick: set = set()
        self._lec_slides_seeded = False
        self._lec_tag_index = None
        self._lec_researching = False

    def _lec_setup_ui(self) -> None:
        """Retitle the controls this door uses differently, once built."""
        if not self._lecture:
            return
        from aqt.qt import QTimer
        from .. import pipeline
        from ...core.qt_utils import is_manual_provider, provider_configured
        from ..kg.bulk_search import C_CARDS, C_GAP, C_NOTES, C_READ, C_SCOPE
        self._read_btn.setText("📖  Read the lecture with AI")
        self._read_btn.setToolTip(
            "Read the lecture into learning points, grouped into its sections. "
            "Cached — reading the same file again is instant and free.")
        self._back_btn.setText("◀  Back to the sections")
        self._found_head.setText("What your deck already has for this lecture")
        self._tree.setHeaderLabels(["Section  ·  point  ·  card",
                                    "Tag  ·  slides & notes"])
        hdr = self._table.horizontalHeaderItem(C_GAP)
        if hdr is not None:
            hdr.setText("Section")
        for c, label in ((C_NOTES, "Slides"), (C_READ, "Learning points")):
            h = self._table.horizontalHeaderItem(c)
            if h is not None:
                h.setText(label)
                h.setToolTip("")
        # The lecture's card budget is the lecture's, not the section's — the
        # Coverage dial on the AI Lecture tab sets it. Per-section scope and
        # card counts would be dials that do nothing.
        self._table.setColumnHidden(C_SCOPE, True)
        self._table.setColumnHidden(C_CARDS, True)
        self._table_hint.setText(
            "Each row is a section of the lecture, in teaching order. Untick "
            "<b>Find</b> on a section you know your deck doesn't have, or "
            "<b>Write</b> on one you don't want new cards for. Hover a row for "
            "every point; <b>Review points…</b> edits them and their search "
            "terms before anything touches your deck.")
        self._record.mark(pipeline.TAKE_IN, pipeline.DONE,
                          "1 lecture" if self._lecture.path else "objectives")
        self._sync_strip()
        if provider_configured() or is_manual_provider():
            # The press on the AI Lecture tab WAS the go-ahead; asking for a
            # second click to start reading would just be a speed bump.
            QTimer.singleShot(0, self._on_read)

    # -- READ --------------------------------------------------------------

    def _lec_read(self) -> None:
        from aqt import mw
        from .. import pipeline
        from ...core import api as core_api
        from ...core.qt_utils import is_manual_provider
        run = self._lecture
        if self._busy:
            return
        if is_manual_provider():
            if self._lec_fm_stage == "await_points":
                self._lec_fm_load_points()
            else:
                self._lec_fm_start()
            return
        import threading
        from .panel import Cancelled, _Heartbeat, read_material
        self._cancel_ev = threading.Event()
        cancel_ev = self._cancel_ev
        self._stage(pipeline.READ, pipeline.RUNNING)
        self._set_busy(True, "Reading the lecture…", progress=True,
                       stoppable=True)

        def _report(msg, pct=0):
            def _ui():
                if self._set_status(msg):
                    self._progress.setValue(int(pct or 0))
            mw.taskman.run_on_main(_ui)

        def _check():
            if cancel_ev.is_set():
                raise Cancelled()

        cfg = dict(run.cfg)

        def _bg():
            core_api.set_cancel_event(cancel_ev)
            try:
                with _Heartbeat(_report, "Reading the lecture…", 5,
                                vision=bool(cfg.get("use_vision"))):
                    return read_material(run.path, cfg, report=_report,
                                         check=_check, companion=run.companion)
            finally:
                core_api.set_cancel_event(None)

        mw.taskman.run_in_background(_bg, self._lec_read_done,
                                     uses_collection=False)

    def _lec_read_done(self, fut) -> None:
        if self._closed:
            return
        from aqt.utils import showWarning
        from .. import pipeline
        from ...core import api as core_api
        from . import ingest
        from .panel import Cancelled
        self._set_busy(False)
        self._resurface()
        try:
            lecture, points = fut.result()
        except (Cancelled, core_api.ClaudeCancelled):
            self._stage(pipeline.READ, pipeline.PENDING)
            self._set_status("Stopped.")
            return
        except ingest.UnsupportedLecture as e:
            self._stage(pipeline.READ, pipeline.FAILED, error=str(e))
            self._set_status("")
            showWarning(str(e), parent=self)
            return
        except Exception as e:
            import traceback
            log.error(f"lecture door: read failed: {e}\n{traceback.format_exc()}")
            self._stage(pipeline.READ, pipeline.FAILED, error=str(e))
            self._set_status("")
            showWarning(f"Couldn't read the lecture:\n\n{e}", parent=self)
            return
        self._lec_take_points(lecture, points)

    def _lec_take_points(self, lecture, points) -> None:
        """The read landed: split it into sections and show them."""
        from .. import autotag, pipeline
        run = self._lecture
        run.lecture, run.points = lecture, list(points or [])
        try:
            type_key = autotag.default_type_key()
        except Exception:
            type_key = ""
        pairs = build_sections(run, run.points, type_key=type_key)
        self._kgs = [r for r, _p in pairs]
        self._plans = [p for _r, p in pairs]
        self._stage(pipeline.READ, pipeline.DONE,
                    f"{len(run.points)} point(s)")
        self._fill_plan_table()
        self._render_plan_table()
        self._set_status(
            f"Read {len(run.points)} learning point(s) in {len(self._plans)} "
            "section(s). Check them over, then search your deck.")
        if self._lp_gate_cb.isChecked():
            self._open_lp_gate()

    def _lec_fix_table(self) -> None:
        """The plan table, as a lecture reads: the slides each section came
        from, and its points written out — the learning points ARE the
        product of this door, so they belong on the page, not in a hover."""
        from aqt.qt import QTableWidgetItem, Qt
        from ..kg.bulk_search import C_NOTES, C_READ
        self._table.blockSignals(True)
        try:
            for row, p in enumerate(self._plans):
                refs = sorted({r for pt in p.points for r in (pt.refs or [])})
                span = slide_span(refs) or ("your objectives"
                                            if not self._lecture.path else "—")
                c = QTableWidgetItem(span)
                c.setFlags(c.flags() & ~Qt.ItemFlag.ItemIsEditable)
                c.setToolTip(span)
                self._table.setItem(row, C_NOTES, c)
                lines = []
                for pt in p.points:
                    mark = " (from your notes)" if getattr(pt, "src", "") == "notes" else ""
                    lines.append(f"• {pt.concept}{mark}")
                txt = "\n".join(lines) or "— no points —"
                r = QTableWidgetItem(txt)
                r.setFlags(r.flags() & ~Qt.ItemFlag.ItemIsEditable)
                r.setToolTip(txt)
                self._table.setItem(row, C_READ, r)
        finally:
            self._table.blockSignals(False)
        self._fit_rows()

    def _lec_title(self) -> str:
        run = self._lecture
        if not self._plans or not run.points:
            return (f"<b>{run.title or 'Lecture'}</b> — reading it into "
                    "learning points…")
        active = self._active_plans()
        pts = sum(len(p.points) for p in active)
        return (f"<b>{run.title or 'Lecture'}</b> · {len(active)} of "
                f"{len(self._plans)} section(s) · {pts} learning point(s)")

    # -- file hand-off (no AI provider) ---------------------------------------
    #
    # Two round trips through any browser AI, exactly as AI Lecture always
    # offered: the tool does the local work (ingest, deck search, allocation)
    # and the AI does the two thinking steps via a file you paste in and a file
    # you save back. No vision — a text file can't carry slide images.

    def _lec_fm_dir(self) -> str:
        run = self._lecture
        return (os.path.dirname(run.path) if run.path else "") \
            or (run.cfg.get("last_file_dir") or "") or os.path.expanduser("~")

    def _lec_fm_stem(self) -> str:
        import re
        base = os.path.splitext(os.path.basename(self._lecture.path or "lecture"))[0]
        return re.sub(r"[^\w.-]+", "_", base) or "lecture"

    def _lec_fm_save(self, text: str, name: str) -> str:
        from aqt.qt import QFileDialog
        from aqt.utils import showWarning
        out, _ = QFileDialog.getSaveFileName(
            self, "Save the file to give your AI",
            os.path.join(self._lec_fm_dir(), name), "Text (*.md *.txt)")
        if not out:
            return ""
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            showWarning(f"Couldn't write the file:\n\n{e}", parent=self)
            return ""
        return out

    def _lec_fm_load(self, title: str):
        from aqt.qt import QFileDialog
        from aqt.utils import showWarning
        path, _ = QFileDialog.getOpenFileName(
            self, title, self._lec_fm_dir(),
            "AI reply (*.json *.txt *.md);;All files (*)")
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            showWarning(f"Couldn't read that file:\n\n{e}", parent=self)
            return None

    def _lec_fm_start(self) -> None:
        from aqt import mw
        from aqt.utils import showInfo, showWarning
        from . import extractor, ingest, manual
        run = self._lecture
        if not run.path:
            showWarning("The file hand-off needs a lecture FILE. Pasted "
                        "objectives need a CLI or API provider — set one up in "
                        "Ankisstant Settings.", parent=self)
            return
        cfg = run.cfg
        tp = int(cfg.get("target_points") or extractor.DEFAULT_TARGET_POINTS)
        hy = bool(cfg.get("high_yield_only", True))
        use_ocr = bool(cfg.get("use_local_ocr", True))
        self._set_busy(True, "Reading the file…")

        def _bg():
            lecture = ingest.load(run.path)
            if use_ocr and lecture.rasterisable and lecture.vision_segments:
                try:
                    ingest.ocr_fill(lecture)
                except Exception as e:
                    log.error(f"lecture door/manual: OCR failed, continuing: {e}")
            return lecture, manual.export_extract_request(
                lecture, target_points=tp, high_yield=hy)

        def _done(fut):
            self._set_busy(False)
            try:
                lecture, md = fut.result()
            except Exception as e:
                self._set_status("")
                showWarning(f"Couldn't read that file:\n\n{e}", parent=self)
                return
            run.lecture = lecture
            out = self._lec_fm_save(md, f"{self._lec_fm_stem()}_step1_extract.md")
            if not out:
                self._set_status("File hand-off cancelled.")
                return
            self._lec_fm_stage = "await_points"
            self._read_btn.setText("Load the AI's points file →")
            self._set_status("Step 1 of 2 saved — run it in your AI, then "
                             "load the reply.")
            showInfo(f"Step 1 of 2 saved to:\n{out}\n\n"
                     "1. Paste all of it into ChatGPT, Claude or Gemini.\n"
                     "2. Save the AI's reply as a file (points.json, or any "
                     ".txt).\n"
                     "3. Back here, click “Load the AI's points file →”.",
                     parent=self, title="AI Lecture — file hand-off")

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _lec_fm_load_points(self) -> None:
        from aqt.utils import showWarning
        from . import manual
        text = self._lec_fm_load("Load the AI's points file")
        if text is None:
            return
        points = manual.parse_extract_reply(text, self._lecture.lecture)
        if not points:
            showWarning("No learning points were found in that file. Make sure "
                        "you saved the AI's full reply (the JSON).", parent=self)
            return
        self._lec_fm_stage = None
        self._read_btn.setText("📖  Read the lecture with AI")
        self._lec_take_points(self._lecture.lecture, points)

    def _lec_fm_find(self, points) -> None:
        """Search half of the hand-off: search the deck locally, then write the
        relevance file for the AI to judge."""
        from aqt import mw
        from aqt.utils import showInfo, showWarning
        from . import manual, matcher
        from .panel import _on_main, search_all
        if self._lec_fm_stage == "await_verdicts":
            self._lec_fm_load_verdicts()
            return
        cfg = self._lecture.cfg
        ntf = (cfg.get("notetype_filter") or "").strip()
        cap = matcher.resolve_candidate_cap(cfg)
        self._set_busy(True, f"{len(points)} points — searching your deck…")

        def _rep(msg, _pct=0):
            mw.taskman.run_on_main(lambda m=msg: self._set_status(m))

        def _bg():
            index = _on_main(matcher.TagIndex.build)
            matches = search_all(points, index=index, notetype_filter=ntf,
                                 candidate_cap=cap, on_main=_on_main, report=_rep)
            items = _on_main(lambda: [
                (m.point, [(nid, matcher.note_text(nid)) for nid in m.nids])
                for m in matches if m.nids])
            md, scorable = manual.export_relevance_request(items)
            return matches, scorable, md

        def _done(fut):
            self._set_busy(False)
            try:
                matches, scorable, md = fut.result()
            except Exception as e:
                showWarning(f"Deck search failed:\n\n{e}", parent=self)
                return
            self._lec_fm_matches, self._lec_fm_scorable = matches, scorable
            if not scorable:
                self._lec_fm_finalise([])
                return
            out = self._lec_fm_save(md, f"{self._lec_fm_stem()}_step2_relevance.md")
            if not out:
                self._set_status("File hand-off cancelled.")
                return
            self._lec_fm_stage = "await_verdicts"
            self._search_btn.setText("Load the AI's relevance file →")
            self._set_status("Step 2 of 2 saved — run it in your AI, then "
                             "load the reply.")
            showInfo(f"Step 2 of 2 saved to:\n{out}\n\n"
                     "1. Paste all of it into the same AI.\n"
                     "2. Save its reply as a file (relevance.json, or any .txt).\n"
                     "3. Back here, click “Load the AI's relevance file →”.",
                     parent=self, title="AI Lecture — file hand-off")

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _lec_fm_load_verdicts(self) -> None:
        from . import manual
        text = self._lec_fm_load("Load the AI's relevance file")
        if text is None:
            return
        self._lec_fm_finalise(manual.parse_relevance_reply(
            text, self._lec_fm_scorable))

    def _lec_fm_finalise(self, verdicts) -> None:
        from aqt import mw
        from aqt.utils import showWarning
        from . import matcher
        from .panel import _on_main, finish_matches
        by_point = {id(m.point): m for m in self._lec_fm_matches}
        for (point, _cards), v in zip(self._lec_fm_scorable, verdicts):
            m = by_point.get(id(point))
            if m is not None:
                m.kept, m.scores = list(v.kept), dict(v.scores)
                m.judge_slot, m.scored = dict(v.slots), True
        matches = self._lec_fm_matches
        cfg = self._lecture.cfg
        target = int(cfg.get("target_cards") or matcher.DEFAULT_TARGET_CARDS)
        if cfg.get("auto_target_cards", True):
            target = matcher.auto_target_cards(len(matches))
        depth = str(cfg.get("depth") or matcher.DEFAULT_DEPTH)
        self._set_busy(True, "Choosing the best notes…")

        def _rep(msg, _pct=0):
            mw.taskman.run_on_main(lambda m=msg: self._set_status(m))

        def _bg():
            return finish_matches(matches, target_cards=target, depth=depth,
                                  on_main=_on_main, report=_rep,
                                  ai_dedupe_model=None)

        def _done(fut):
            self._set_busy(False)
            try:
                coverage = fut.result()
            except Exception as e:
                showWarning(f"Couldn't finish:\n\n{e}", parent=self)
                return
            self._lec_fm_stage = None
            self._search_btn.setText("Search my deck  ▶")
            from .. import pipeline
            self._matches, self._coverage = matches, coverage
            self._stage(pipeline.FIND, pipeline.DONE,
                        f"{len(coverage.gaps)} gap(s)")
            self._render_results()

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    # -- FIND --------------------------------------------------------------

    def _lec_search_args(self, plans) -> tuple:
        """(cfg, extra run_pipeline kwargs) for this lecture's search.

        The lecture's OWN settings — coverage depth, notetype filter, the card
        budget sized from the points — not the work-through's per-gap shares:
        a lecture searched here finds exactly the cards it found on its own
        page. The notes pass rides along when it was asked for."""
        run = self._lecture
        cfg = dict(run.cfg)
        cfg["lo_text"] = ""
        cfg["size_from_points"] = True
        return cfg, {"lecture": run.lecture, "companion": run.companion}

    # -- REVIEW: the tree, decorated -----------------------------------------

    def _lec_suggest_tag(self, plan) -> str:
        return self._lecture.tag or (plan.tags[0] if plan.tags else "")

    def _lec_leaf_flag(self, m, nid: int, chosen: bool) -> str:
        """A chosen card that doesn't really answer its point: it matched none
        of the point's whole search terms AND the judge rated it tangential (or
        never ran). Flagged in words, never hidden."""
        if not chosen:
            return ""
        whole = (getattr(m, "rank_debug", {}) or {}).get(nid, (0, 0, 0))[0]
        if whole > 0:
            return ""
        score = (getattr(m, "scores", {}) or {}).get(nid)
        return "may not answer this point" if (score is None or score <= 1) else ""

    def _lec_after_results(self) -> None:
        """The search has landed and the tree is drawn."""
        cov = self._coverage
        if cov is not None and getattr(cov, "card_notes", None) \
                and not self._lec_note_pick:
            # The notes pass already applied a deliberately hard bar, so what
            # it proposes starts ticked. Unticking is the veto.
            for n in cov.card_notes:
                self._lec_note_pick.add((n.nid, n.key))
        self._lec_more_btn.setVisible(True)
        self._lec_refresh_badges()
        self._lec_build_slides()
        self._pane_show(self._tree.currentItem())

    def _lec_matches_by_kg(self) -> dict:
        by: dict = {}
        if self._coverage is None:
            return by
        for m in list(self._coverage.covered) + list(self._coverage.gaps):
            by.setdefault(getattr(m.point, "kg_id", ""), []).append(m)
        return by

    def _lec_match_for(self, node):
        """The PointMatch behind a point row (same index the tree was drawn
        with: the section's matches in covered-then-gaps order)."""
        if node is None or node.data(0, self.ROLE_KIND) != "point":
            return None
        idx = node.data(0, self.ROLE_KEY)
        group = self._lec_matches_by_kg().get(str(node.data(0, self.ROLE_KG) or ""), [])
        if isinstance(idx, int) and 0 <= idx < len(group):
            return group[idx]
        return None

    def _lec_point_for_concept(self, concept: str):
        want = " ".join(str(concept or "").split()).casefold()
        for p in self._plans or []:
            for pt in p.points:
                if " ".join(pt.concept.split()).casefold() == want:
                    return pt
        return None

    def _lec_points_of_leaf(self, leaf) -> list:
        """The learning points a card row answers.

        A found card can sit under several points (a summary card often does),
        and its slide candidates are the union of theirs — so every row with
        the same nid is consulted, not only this one."""
        kind = leaf.data(0, self.ROLE_KIND)
        if kind == "note":
            nid = int(leaf.data(0, self.ROLE_KEY))
            pts = []
            for node in self._lec_point_nodes():
                for i in range(node.childCount()):
                    ch = node.child(i)
                    if ch.data(0, self.ROLE_KIND) == "note" \
                            and int(ch.data(0, self.ROLE_KEY)) == nid:
                        m = self._lec_match_for(node)
                        if m is not None and m.point not in pts:
                            pts.append(m.point)
            return pts
        if kind == "newcard":
            parent = leaf.parent()
            if parent is not None and parent.data(0, self.ROLE_KIND) == "point":
                pt = self._lec_point_for_concept(parent.data(0, self.ROLE_CONCEPT))
                return [pt] if pt is not None else []
            card = leaf.data(0, self.ROLE_CARD) or {}
            pt = self._lec_point_of_written(str(leaf.data(0, self.ROLE_KG) or ""),
                                            card)
            return [pt] if pt is not None else []
        return []

    def _lec_point_of_written(self, kg_id: str, card: dict):
        """The point a written card says it answers (the prompt asks for a
        point number, which indexes its job's mass_points)."""
        try:
            n = int((card or {}).get("point") or 0)
        except (TypeError, ValueError):
            n = 0
        concepts = self._point_concepts(self._job_for_kg(kg_id))
        if 1 <= n <= len(concepts):
            return self._lec_point_for_concept(concepts[n - 1])
        return None

    def _lec_point_nodes(self) -> list:
        out = []
        for gi in range(self._tree.topLevelItemCount()):
            g = self._tree.topLevelItem(gi)
            for pi in range(g.childCount()):
                p = g.child(pi)
                if p.data(0, self.ROLE_KIND) == "point":
                    out.append(p)
        return out

    def _lec_written_slides(self, leaf, card: dict) -> list:
        """A written card's slides: what it was ticked for in the panel, else
        the same suggestion a found card gets."""
        if isinstance(card, dict) and "lecture_slides" in card:
            return [int(x) for x in (card.get("lecture_slides") or [])]
        if not self._lec_suggest_on():
            return []
        return suggested(self._lec_points_of_leaf(leaf), self._lec_imagery)

    def _lec_suggest_on(self) -> bool:
        return bool(self._lecture.cfg.get("attach_slides", True))

    def _lec_refresh_badges(self) -> None:
        """Column 1 of every card row says, in words, what it will gain."""
        if not self._lecture:
            return
        self._tree.blockSignals(True)
        try:
            for node in self._lec_point_nodes():
                self._lec_badge_children(node)
            for gi in range(self._tree.topLevelItemCount()):
                g = self._tree.topLevelItem(gi)
                for pi in range(g.childCount()):
                    ch = g.child(pi)
                    if ch.data(0, self.ROLE_KIND) == "written":
                        self._lec_badge_children(ch)
        finally:
            self._tree.blockSignals(False)

    def _lec_badge_children(self, node) -> None:
        for i in range(node.childCount()):
            leaf = node.child(i)
            kind = leaf.data(0, self.ROLE_KIND)
            bits = []
            if kind == "note":
                nid = int(leaf.data(0, self.ROLE_KEY))
                ns = sum(1 for (n, _s) in self._lec_slide_pick if n == nid)
                nn = sum(1 for (n, _k) in self._lec_note_pick if n == nid)
                if ns:
                    bits.append(f"+{ns} slide{'s' if ns != 1 else ''}")
                if nn:
                    bits.append(f"+{nn} note{'s' if nn != 1 else ''}")
            elif kind == "newcard":
                card = leaf.data(0, self.ROLE_CARD) or {}
                ns = len(self._lec_written_slides(leaf, card))
                if ns:
                    bits.append(f"+{ns} slide{'s' if ns != 1 else ''}")
            else:
                continue
            leaf.setText(1, "  ·  ".join(bits))

    # -- slides: build once, in the background -------------------------------

    def _lec_build_slides(self) -> None:
        from aqt import mw
        from . import ingest
        run = self._lecture
        if self._lec_slides_state in ("building", "ready", "n/a"):
            return
        if not run.path or not run.is_pdf:
            self._lec_slides_state = "n/a"
            return
        cited = sorted({r for p in self._plans for pt in p.points
                        for r in (pt.refs or [])})
        if not cited:
            self._lec_slides_state = "n/a"
            return
        self._lec_slides_state = "building"
        path = run.path

        def _bg():
            imagery = ingest.detect_imagery(path, cited)
            return imagery, ingest.render_segment_paths(path, sorted(imagery))

        def _done(fut):
            if self._closed:
                return
            try:
                imagery, pngs = fut.result()
            except Exception as e:
                log.warn(f"lecture door: slide previews failed: {e}")
                self._lec_slides_state = "n/a"
                self._pane_show(self._tree.currentItem())
                return
            self._lec_imagery = set(imagery)
            self._lec_slide_png = dict(pngs)
            self._lec_slides_state = "ready"
            self._lec_seed_slide_picks()
            self._lec_refresh_badges()
            self._pane_show(self._tree.currentItem())

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _lec_seed_slide_picks(self) -> None:
        """Pre-tick the suggested slides on the cards the search chose — once,
        so a later redraw can't undo an untick."""
        if self._lec_slides_seeded or not self._lec_suggest_on():
            return
        self._lec_slides_seeded = True
        for node in self._lec_point_nodes():
            m = self._lec_match_for(node)
            if m is None:
                continue
            refs = suggested([m.point], self._lec_imagery)
            if not refs:
                continue
            for i in range(node.childCount()):
                leaf = node.child(i)
                if leaf.data(0, self.ROLE_KIND) != "note":
                    continue
                if leaf.checkState(0) != self._checked():
                    continue
                nid = int(leaf.data(0, self.ROLE_KEY))
                for r in refs:
                    self._lec_slide_pick.add((nid, r))

    @staticmethod
    def _checked():
        from aqt.qt import Qt
        return Qt.CheckState.Checked

    def _lec_pixmap(self, n: int, width: int = 250):
        from aqt.qt import QPixmap, Qt
        key = (n, width)
        if key in self._lec_pix:
            return self._lec_pix[key]
        pm = QPixmap(self._lec_slide_png.get(n, ""))
        if not pm.isNull():
            pm = pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        self._lec_pix[key] = pm
        return pm

    # -- the side panel --------------------------------------------------------

    def _lec_pane_extras(self, item, kind) -> None:
        """The lecture's sections of the card panel (kg/card_pane.py), under
        the card itself: the slides it can carry, and its Lecture Notes."""
        if kind == "note":
            self._lec_pane_found(item)
        elif kind == "newcard":
            self._lec_pane_written(item)
        elif kind == "point":
            self._lec_pane_point(item)
        self._lec_pane_unplaced(item if kind == "note" else None)

    def _lec_slides_status(self) -> str:
        st = self._lec_slides_state
        if st == "building":
            return "Preparing slide previews…"
        if st == "n/a":
            return ("Slides can only be attached from a PDF lecture."
                    if not self._lecture.is_pdf else
                    "None of the slides these points cite carry a diagram, "
                    "table or picture.")
        return ""

    def _lec_slide_row(self, n: int, checked: bool, on_toggle):
        from aqt.qt import QCheckBox, QLabel, QVBoxLayout, QWidget
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 2, 0, 6)
        cb = QCheckBox(f"Attach slide {n}")
        cb.setChecked(checked)
        cb.toggled.connect(on_toggle)
        lay.addWidget(cb)
        pm = self._lec_pixmap(n)
        if not pm.isNull():
            img = QLabel()
            img.setPixmap(pm)
            lay.addWidget(img)
        return w

    def _lec_pane_found(self, leaf) -> None:
        body = self._pane_body
        nid = int(leaf.data(0, self.ROLE_KEY))
        pts = self._lec_points_of_leaf(leaf)
        body.addWidget(self._pane_heading("Slides to attach"))
        status = self._lec_slides_status()
        cands = slides_for_points(pts, self._lec_imagery) \
            if self._lec_slides_state == "ready" else []
        if status:
            body.addWidget(self._pane_label(status, grey=True))
        elif not cands:
            body.addWidget(self._pane_label(
                "The slides this card's point came from are text only — "
                "nothing worth a picture.", grey=True))
        for n in cands:
            def _t(on, n=n, nid=nid):
                (self._lec_slide_pick.add if on else self._lec_slide_pick.discard)((nid, n))
                self._lec_refresh_badges()
            body.addWidget(self._lec_slide_row(
                n, (nid, n) in self._lec_slide_pick, _t))
        # Notes
        notes = [n for n in (getattr(self._coverage, "card_notes", None) or [])
                 if n.nid == nid]
        body.addWidget(self._pane_heading("Lecture Notes to add"))
        if not self._lecture.cfg.get("attach_notes"):
            body.addWidget(self._pane_label(
                "“Add notes to cards” was off on the AI Lecture tab, so no "
                "lines were proposed this run.", grey=True))
        elif not notes:
            body.addWidget(self._pane_label(
                "Nothing proposed for this card — the bar is deliberately "
                "high, so most cards get nothing.", grey=True))
        from aqt.qt import QCheckBox
        for n in notes:
            origin = n.src
            if n.kind == "note":
                origin = ("from the transcript" if getattr(n, "tidied", False)
                          else "your note")
            cb = QCheckBox(f"{n.text}   — {origin}")
            cb.setChecked((nid, n.key) in self._lec_note_pick)
            if n.why:
                cb.setToolTip(n.why)

            def _t(on, key=n.key, nid=nid):
                (self._lec_note_pick.add if on else self._lec_note_pick.discard)((nid, key))
                self._lec_refresh_badges()
            cb.toggled.connect(_t)
            body.addWidget(cb)

    def _lec_pane_written(self, leaf) -> None:
        body = self._pane_body
        card = leaf.data(0, self.ROLE_CARD) or {}
        pts = self._lec_points_of_leaf(leaf)
        body.addWidget(self._pane_heading("Slides to attach"))
        status = self._lec_slides_status()
        cands = slides_for_points(pts, self._lec_imagery) \
            if self._lec_slides_state == "ready" else []
        if status:
            body.addWidget(self._pane_label(status, grey=True))
        elif not pts:
            body.addWidget(self._pane_label(
                "The AI didn't say which point this card answers, so no slide "
                "is offered rather than a guess.", grey=True))
        elif not cands:
            body.addWidget(self._pane_label(
                "Its point's slides are text only.", grey=True))
        current = set(self._lec_written_slides(leaf, card))
        for n in cands:
            def _t(on, n=n, leaf=leaf):
                c = dict(leaf.data(0, self.ROLE_CARD) or {})
                have = list(self._lec_written_slides(leaf, c))
                if on and n not in have:
                    have.append(n)
                elif not on and n in have:
                    have.remove(n)
                c["lecture_slides"] = sorted(have)
                self._tree.blockSignals(True)
                leaf.setData(0, self.ROLE_CARD, c)
                self._tree.blockSignals(False)
                self._lec_refresh_badges()
            body.addWidget(self._lec_slide_row(n, n in current, _t))
        body.addWidget(self._pane_label(
            "Lecture Notes lines are proposed for cards you already own; a "
            "written card was written from the slides already.", grey=True))

    def _lec_pane_point(self, node) -> None:
        body = self._pane_body
        m = self._lec_match_for(node)
        pt = m.point if m is not None else self._lec_point_for_concept(
            node.data(0, self.ROLE_CONCEPT))
        if pt is None:
            return
        bits = [slide_span(pt.refs)]
        if getattr(pt, "src", "") == "notes":
            bits.append("from your supplementary notes")
        body.addWidget(self._pane_label(" · ".join(b for b in bits if b), grey=True))
        if self._lec_slides_state == "ready":
            for n in slides_for_points([pt], self._lec_imagery):
                pm = self._lec_pixmap(n)
                body.addWidget(self._pane_label(f"Slide {n}", grey=True))
                if not pm.isNull():
                    from aqt.qt import QLabel
                    img = QLabel()
                    img.setPixmap(pm)
                    body.addWidget(img)
        body.addWidget(self._pane_label(
            "Right-click the point to search again, open it in the Anki "
            "browser, or write a card for it.", grey=True))

    def _lec_pane_unplaced(self, leaf) -> None:
        """Your own notes the pass couldn't place. Nothing you wrote is ever
        dropped silently: each can be put on whichever found card is selected."""
        from aqt.qt import QPushButton
        cov = self._coverage
        idxs = list(getattr(cov, "unplaced_notes", None) or []) if cov else []
        comp = self._lecture.companion
        chunks = {c.index: c.text for c in (getattr(comp, "chunks", None) or [])}
        rows = [(i, chunks[i]) for i in idxs if chunks.get(i)]
        if not rows:
            return
        body = self._pane_body
        body.addWidget(self._pane_heading("Your notes with nowhere to go"))
        body.addWidget(self._pane_label(
            "Select a card you already own, then put a note on it — or leave "
            "it.", grey=True))
        nid = int(leaf.data(0, self.ROLE_KEY)) if leaf is not None else None
        for i, text in rows:
            body.addWidget(self._pane_label(text))
            b = QPushButton("Put on the selected card")
            b.setAutoDefault(False)
            b.setEnabled(nid is not None)
            b.clicked.connect(lambda _c=False, i=i, nid=nid: self._lec_put_note(i, nid))
            body.addWidget(b)

    def _lec_put_note(self, idx: int, nid) -> None:
        from . import matcher
        cov = self._coverage
        comp = self._lecture.companion
        chunk = next((c for c in (getattr(comp, "chunks", None) or [])
                      if c.index == idx), None)
        if chunk is None or cov is None or nid is None:
            return
        note = matcher.CardNote(nid=int(nid), src=f"note {idx}", text=chunk.text,
                                why="you placed this", kind="note",
                                note_index=idx)
        cov.card_notes = list(getattr(cov, "card_notes", None) or []) + [note]
        cov.unplaced_notes = [i for i in (cov.unplaced_notes or []) if i != idx]
        self._lec_note_pick.add((int(nid), note.key))
        self._lec_refresh_badges()
        self._pane_show(self._tree.currentItem())

    # -- right-click and the Lecture ▾ menu -------------------------------------

    def _lec_menu(self, menu, item, kind) -> None:
        if not self._lecture or kind != "point":
            return
        m = self._lec_match_for(item)
        if m is None:
            return
        menu.addSeparator()
        menu.addAction("↻  Search again with better terms",
                       lambda: self._lec_research(item))
        topic = [n for n in (getattr(m, "topic_cards", None) or [])]
        if topic and not m.final:
            menu.addAction(f"Tick this topic's own {len(topic)} card(s)",
                           lambda: self._lec_take_topic(item))
        menu.addAction("⌕  Open this point's search in the Anki browser",
                       lambda: self._lec_open_browser(m))

    def _lec_more_menu(self) -> None:
        from aqt.qt import QMenu
        menu = QMenu(self)
        uncovered = self._lec_uncovered_nodes()
        a = menu.addAction(f"↻  Search again for all {len(uncovered)} uncovered "
                           "point(s)", self._lec_research_all)
        a.setEnabled(bool(uncovered) and not self._lec_researching)
        menu.addSeparator()
        menu.addAction("Save the missing points with slide numbers (.md)…",
                       self._lec_export_missing)
        menu.exec(self._lec_more_btn.mapToGlobal(
            self._lec_more_btn.rect().bottomLeft()))

    def _lec_uncovered_nodes(self) -> list:
        """Point rows with no found card ticked under them."""
        out = []
        for node in self._lec_point_nodes():
            if not any(node.child(i).data(0, self.ROLE_KIND) == "note"
                       and node.child(i).checkState(0) == self._checked()
                       for i in range(node.childCount())):
                out.append(node)
        return out

    def _lec_open_browser(self, m) -> None:
        from aqt import mw
        from aqt.utils import tooltip
        from . import matcher
        terms = [t for t in m.point.terms if t.strip()]
        if not terms:
            tooltip("No search terms for this point.")
            return
        nf = (self._lecture.cfg.get("notetype_filter") or "").strip()
        query = " OR ".join(matcher._scoped(t, nf) for t in terms)
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            browser.form.searchEdit.lineEdit().setText(query)
            browser.onSearchActivated()
        except Exception as e:
            log.error(f"lecture door: open Browser failed: {e}")
            tooltip("Couldn't open the Browser.")

    def _lec_take_topic(self, node) -> None:
        m = self._lec_match_for(node)
        if m is None:
            return
        want = set(getattr(m, "topic_cards", None) or [])
        have = set()
        self._tree.blockSignals(True)
        try:
            for i in range(node.childCount()):
                leaf = node.child(i)
                if leaf.data(0, self.ROLE_KIND) != "note":
                    continue
                nid = int(leaf.data(0, self.ROLE_KEY))
                have.add(nid)
                if nid in want:
                    leaf.setCheckState(0, self._checked())
            for nid in want - have:
                self._make_found_leaf(node, m, nid, chosen=True)
        finally:
            self._tree.blockSignals(False)
        node.setExpanded(True)
        self._update_selection_count()
        self._lec_refresh_badges()

    # -- re-search ---------------------------------------------------------------
    #
    # "Gap" means the first guess at the vocabulary failed, which is not the
    # same as the deck not having the card. Re-search asks the AI for better
    # terms, told which ones found nothing, and casts a deliberately WIDER net
    # with a lenient keep floor — a "closest match" the first pass dropped is
    # offered, a genuine 0 is still thrown out. The new cards go under the
    # point in place; the closest are ticked, the rest offered.

    RESEARCH_TICK_MAX = 3

    def _lec_research(self, node, then=None) -> None:
        from aqt import mw
        from aqt.utils import showWarning, tooltip
        from .. import pipeline
        from ...core import api as core_api
        from . import matcher
        from .panel import _on_main
        if self._lec_researching or self._busy:
            tooltip("Something is already running — wait for it to finish.")
            return
        m = self._lec_match_for(node)
        if m is None:
            if then:
                then()
            return
        snippets = {}
        for nid in m.nids[:14]:
            try:
                snippets[nid] = matcher.note_text(nid, max_len=140)
            except Exception:
                pass
        prompt = matcher.build_research_prompt(m, snippets)
        nf = (self._lecture.cfg.get("notetype_filter") or "").strip()
        model_r = pipeline.dial_for(pipeline.FIND)
        self._lec_researching = True
        self._set_status(f"Searching again: {m.point.concept[:80]}…")

        def _bg():
            terms = matcher.research_terms(prompt, model=model_r) or [
                t for t in m.point.terms if t.strip()]
            point = dataclasses.replace(m.point, terms=list(terms))

            def _search():
                if self._lec_tag_index is None:
                    self._lec_tag_index = matcher.TagIndex.build()
                fresh = matcher.search_point(
                    point, self._lec_tag_index, notetype_filter=nf,
                    limit=matcher.RESEARCH_CANDIDATE_LIMIT)
                matcher.annotate_suspended(fresh)
                return fresh

            fresh = _on_main(_search)
            if fresh.nids:
                cards = _on_main(lambda: [(nid, matcher.note_text(nid))
                                          for nid in fresh.nids])
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
            self._lec_researching = False
            if self._closed:
                return
            try:
                terms, fresh = fut.result()
            except matcher.RelevanceUsageExhausted as e:
                showWarning(str(e), parent=self)
                return       # stop a chain: every remaining call would fail too
            except core_api.ClaudeCancelled:
                return
            except Exception as e:
                import traceback
                log.error(f"lecture door: re-search failed: {e}\n"
                          f"{traceback.format_exc()}")
                self._set_status(f"Re-search failed: {e}")
                if then:
                    then()
                return
            self._lec_apply_research(node, m, terms, fresh)
            if then:
                then()

        mw.taskman.run_in_background(_bg, _done, uses_collection=False)

    def _lec_apply_research(self, node, m, terms, fresh) -> None:
        from aqt.utils import tooltip
        m.point.terms = list(terms)
        m.nids, m.kept = fresh.nids, fresh.kept
        m.per_term, m.tag_hits = fresh.per_term, fresh.tag_hits
        m.rank_debug, m.term_cover = fresh.rank_debug, fresh.term_cover
        m.scores, m.judge_slot = fresh.scores, fresh.judge_slot
        m.susp.update(fresh.susp)
        m.scored = True
        best = [n for n in fresh.kept if (fresh.scores.get(n) or 0) >= 2]
        best = best[:self.RESEARCH_TICK_MAX]
        m.manual = list(dict.fromkeys(list(m.manual) + best))
        self._tree.blockSignals(True)
        try:
            present = {}
            for i in range(node.childCount()):
                leaf = node.child(i)
                if leaf.data(0, self.ROLE_KIND) == "note":
                    present[int(leaf.data(0, self.ROLE_KEY))] = leaf
                elif leaf.data(0, self.ROLE_KIND) == "info":
                    node.removeChild(leaf)
                    break
            from ..kg.bulk_search import EXTRA_CANDIDATES_PER_POINT
            offer = list(fresh.kept)[:EXTRA_CANDIDATES_PER_POINT + len(best)]
            for nid in offer:
                if nid in present:
                    if nid in best:
                        present[nid].setCheckState(0, self._checked())
                    continue
                self._make_found_leaf(node, m, nid, chosen=nid in best)
        finally:
            self._tree.blockSignals(False)
        node.setExpanded(True)
        self._update_selection_count()
        self._lec_refresh_badges()
        found = len(fresh.kept)
        msg = (f"{found} card(s) found for “{m.point.concept[:60]}”"
               + (f", {len(best)} ticked." if best else " — none close enough to "
                  "tick; look them over.")) if found else \
            "Still nothing — right-click it → Write a card for this point."
        self._set_status(msg)
        tooltip(msg, period=3000)

    def _lec_research_all(self) -> None:
        queue = list(self._lec_uncovered_nodes())
        total = len(queue)
        if not total:
            return

        def step():
            if not queue or self._closed:
                self._set_status(f"Searched again for {total} point(s).")
                return
            node = queue.pop(0)
            self._set_status(f"Searching again: {total - len(queue)} of {total}…")
            self._lec_research(node, then=step)

        step()

    # -- export ------------------------------------------------------------------

    def _lec_export_missing(self) -> None:
        from aqt.utils import showInfo, tooltip
        nodes = self._lec_uncovered_nodes()
        ms = [self._lec_match_for(n) for n in nodes]
        ms = [m for m in ms if m is not None]
        if not ms:
            tooltip("No missing points — every point has a card ticked.")
            return
        true_gaps = [m.point for m in ms if not getattr(m, "topic_present", False)]
        topic_gaps = [m.point for m in ms if getattr(m, "topic_present", False)]
        text = missing_points_markdown(self._lecture.title or self._lec_fm_stem(),
                                       true_gaps, topic_gaps)
        out = self._lec_fm_save(text, f"{self._lec_fm_stem()}_missing_points.md")
        if out:
            showInfo(f"Saved {len(ms)} missing learning point(s) to:\n\n{out}\n\n"
                     "Feed it to any card generator, then import what you get "
                     "back.", parent=self)

    # -- COMMIT ------------------------------------------------------------------

    def _lec_commit_counts(self, found_nids) -> tuple:
        """(slides on found cards, notes lines, cards touched, written cards
        with slides) — what Apply is about to add, for the confirm."""
        want = set(found_nids or [])
        s = {(n, x) for (n, x) in self._lec_slide_pick if n in want}
        k = {(n, x) for (n, x) in self._lec_note_pick if n in want}
        touched = {n for n, _ in s} | {n for n, _ in k}
        written = 0
        for leaf, card in self._written_items():
            if leaf.checkState(0) == self._checked() \
                    and self._lec_written_slides(leaf, card):
                written += 1
        return len(s), len(k), len(touched), written

    def _lec_confirm_lines(self, found_nids) -> list:
        if not self._lecture:
            return []
        ns, nk, nt, nw = self._lec_commit_counts(found_nids)
        bits = []
        if nk:
            bits.append(f"{nk} line(s) of Lecture Notes")
        if ns:
            bits.append(f"{ns} slide image(s)")
        out = []
        if bits:
            out.append(f"• Add {' and '.join(bits)} to {nt} card(s) you "
                       "already own.")
        if nw:
            out.append(f"• Staple their source slides onto {nw} written "
                       "card(s).")
        if out:
            out.append("  (Into the ‘Lecture Notes’ field only — the tested "
                       "Text/Extra is never touched.)")
        return out

    def _lec_on_committed(self, pairs: dict, found_nids: list) -> None:
        """The lecture's own commit step, inside the run's checkpoint: the
        picked notes and slides onto the found cards, and each written card's
        slides onto it. Fails closed — attaching nothing is always safe."""
        if not self._lecture:
            return
        try:
            noted = self._lec_attach_notes(found_nids)
            slides_by_nid: dict = {}
            want = set(found_nids or [])
            for (nid, n) in sorted(self._lec_slide_pick):
                if nid in want:
                    slides_by_nid.setdefault(nid, []).append(n)
            for kg_id, rows in (pairs or {}).items():
                for nid, card in rows:
                    if isinstance(card, dict) and "lecture_slides" in card:
                        refs = list(card.get("lecture_slides") or [])
                    elif self._lec_suggest_on():
                        pt = self._lec_point_of_written(kg_id, card)
                        refs = suggested([pt] if pt else [], self._lec_imagery)
                    else:
                        refs = []
                    if refs:
                        slides_by_nid.setdefault(int(nid), [])
                        for r in refs:
                            if r not in slides_by_nid[int(nid)]:
                                slides_by_nid[int(nid)].append(r)
            slid = self._lec_attach_slides(slides_by_nid)
            if noted or slid:
                log.info(f"lecture door: notes on {noted} card(s), slides on "
                         f"{slid} card(s)")
        except Exception as e:
            log.error(f"lecture door: attaching slides/notes failed: {e}")

    def _lec_field(self, note):
        from .panel import LecturePanel
        return LecturePanel._slide_field_for(
            note, (self._lecture.cfg.get("slide_field") or "").strip())

    def _lec_attach_notes(self, found_nids) -> int:
        from aqt import mw
        from .panel import _esc
        cov = self._coverage
        if not cov or not getattr(cov, "card_notes", None):
            return 0
        want = set(found_nids or [])
        by_nid: dict = {}
        for n in cov.card_notes:
            if n.nid in want and (n.nid, n.key) in self._lec_note_pick:
                by_nid.setdefault(n.nid, []).append(n)
        touched = 0
        for nid, notes in by_nid.items():
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            fld = self._lec_field(note)
            if not fld:
                continue
            existing = note[fld] or ""
            frags = []
            for n in notes:
                body = _esc(n.text)
                # Re-run guard on the text itself: the same line renders the
                # same way, so a second run can't stack duplicates.
                if body and body not in existing and body not in frags:
                    frags.append(body)
            if not frags:
                continue
            note[fld] = (existing + "<br>" if existing else "") + "<br>".join(frags)
            try:
                mw.col.update_note(note)
                touched += 1
            except Exception as e:
                log.warn(f"lecture door: notes onto {nid} failed: {e}")
        return touched

    def _lec_attach_slides(self, slides_by_nid: dict) -> int:
        from aqt import mw
        from . import ingest, media
        from .panel import _esc_attr
        run = self._lecture
        slides_by_nid = {k: v for k, v in (slides_by_nid or {}).items() if v}
        if not slides_by_nid or not run.path or not run.is_pdf:
            return 0
        wanted = sorted({r for rs in slides_by_nid.values() for r in rs})
        paths = {n: p for n, p in self._lec_slide_png.items() if n in wanted}
        missing = [n for n in wanted if n not in paths]
        if missing:
            paths.update(ingest.render_segment_paths(run.path, missing))
        fname = {}
        for n, p in paths.items():
            fn = media.import_image_to_media(p)
            if fn:
                fname[n] = fn
        touched = 0
        for nid, slides in slides_by_nid.items():
            try:
                note = mw.col.get_note(int(nid))
            except Exception:
                continue
            fld = self._lec_field(note)
            if not fld:
                continue       # no Lecture Notes field — never pollute Extra
            existing = note[fld] or ""
            frags = [f'<img src="{_esc_attr(fname[n])}">' for n in slides
                     if n in fname and fname[n] not in existing]
            if not frags:
                continue
            note[fld] = (existing + "<br>" if existing else "") + "<br>".join(frags)
            try:
                mw.col.update_note(note)
                touched += 1
            except Exception as e:
                log.warn(f"lecture door: slides onto {nid} failed: {e}")
        return touched
