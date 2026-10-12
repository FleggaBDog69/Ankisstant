# The ONE output page.
#
# Every tool in this add-on used to end on its own result screen: AI Create's
# card rows, AI Browse's list, AI Lecture's HTML report, Mass AI Create's group
# list, Update by Tag's row log, and the work-through's splitter. Six surfaces
# for one question — "here's what I found and what I wrote; what do you want to
# keep?" — each with its own ticking, its own editing and its own commit.
#
# There is now one, and it is the work-through's: found cards above, written
# cards below, one Apply, one Ctrl+Z. That screen is the one that already got
# this right, so it is generalised rather than rebuilt (tools/kg/bulk_search.py,
# BulkSearchDialog). This module is what the other doors call to reach it, plus
# the strip that runs along the top of it.
#
# The strip is a STATUS DISPLAY, not a wizard. It never stops the fast path; it
# says which of the eight stages this run used, which it skipped, and where it
# is now. Skipped stages are shown and LABELLED, never hidden — seeing that a
# solo AI Create run skipped Read, Approve and Find is how the shape of the
# pipeline stays legible across doors that use wildly different amounts of it.

from __future__ import annotations

from aqt.qt import (
    QColor, QFont, QFontMetrics, QHBoxLayout, QPainter, QPalette, QPen,
    QPoint, QRect, QSize, QSizePolicy, Qt, QWidget,
)

from . import pipeline


# State words. NEVER colour alone: grey reads as "rejected" when it means "not
# run yet", and this add-on has already been bitten by exactly that once. Every
# box says what it is in text, and the colour only reinforces it.
_WORD = {
    pipeline.PENDING: "not yet",
    pipeline.RUNNING: "running",
    pipeline.DONE:    "done",
    pipeline.SKIPPED: "skipped",
    pipeline.FAILED:  "failed",
}

_TIP = {
    pipeline.PENDING: "Hasn't run yet.",
    pipeline.RUNNING: "Running now.",
    pipeline.DONE:    "Finished.",
    pipeline.SKIPPED: "This run didn't need this stage.",
    pipeline.FAILED:  "This stage failed — see the status line.",
}

_STAGE_TIP = {
    pipeline.TAKE_IN: "What you fed in — a lecture, some gaps, a topic, or "
                      "notes you already have.",
    pipeline.READ:    "The AI reads the material and decomposes it into "
                      "learning points.",
    pipeline.APPROVE: "You edit the plan before anything runs.",
    pipeline.FIND:    "Your deck is searched for cards that already test each "
                      "point.",
    pipeline.WRITE:   "Cards are written for the points your deck didn't "
                      "cover.",
    pipeline.CHECK:   "The written cards are graded before you see them.",
    pipeline.REVIEW:  "You tick what to keep.",
    pipeline.COMMIT:  "Tag, unsuspend and add — one checkpoint, one undo.",
}

_CIRCLED = "①②③④⑤⑥⑦⑧"


# ── the strip, painted ───────────────────────────────────────────────────────
#
# It was eight bordered labels in a row: legible, but it read as a toolbar of
# buttons rather than as one run moving through eight stages. A pipeline has a
# direction and this now shows it — nodes on a track, the track between two
# finished stages filled in, the one you are on ringed.
#
# The rules it keeps from the label version, because both were load-bearing:
#
#   * EVERY stage is drawn, including the ones this run skipped. Seeing that a
#     solo AI Create skipped Read, Approve and Find is how the shape of the
#     pipeline stays legible across doors that use very different amounts of it.
#   * NEVER state by colour alone. Each node carries a glyph (✓ ▪ – !) AND a
#     word underneath. Grey reads as "rejected" when it means "not run yet",
#     and this add-on has been bitten by exactly that before.
#   * Palette-relative colours, so it reads on both Anki themes.

class _StageNode(QWidget):
    """One stage: a node on the track, its label, and its state in words."""

    _R = 9                     # node radius
    _TRACK_Y = 13              # centre line, from the top

    def __init__(self, st, state: str, tail: str, is_current: bool,
                 first: bool, last: bool, left_done: bool, right_done: bool,
                 parent=None):
        super().__init__(parent)
        self._st, self._state, self._tail = st, state, tail
        self._current = is_current
        self._first, self._last = first, last
        self._left_done, self._right_done = left_done, right_done
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(46)
        f = self.font()
        f.setPointSizeF(max(8.0, f.pointSizeF() - 2))
        self._font = f
        fm = QFontMetrics(f)
        self._w = max(fm.horizontalAdvance(st.label),
                      fm.horizontalAdvance(tail), 2 * self._R) + 14
        self.setMinimumWidth(self._w)

    def sizeHint(self):
        return QSize(self._w, 46)

    # -- what the node itself says --------------------------------------------
    def _glyph(self) -> str:
        return {pipeline.DONE: "✓", pipeline.RUNNING: "▪",
                pipeline.SKIPPED: "–", pipeline.FAILED: "!"}.get(
                    self._state, str(self._st.ordinal))

    def _ink(self):
        pal = self.palette()
        if self._state == pipeline.FAILED:
            return QColor("#b45309")
        if self._state in (pipeline.PENDING, pipeline.SKIPPED):
            c = QColor(pal.color(QPalette.ColorRole.WindowText))
            c.setAlpha(110)
            return c
        return pal.color(QPalette.ColorRole.WindowText)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        mid = QColor(pal.color(QPalette.ColorRole.WindowText))
        mid.setAlpha(45)
        ink = self._ink()
        cx, cy, r = self.width() // 2, self._TRACK_Y, self._R

        # -- the track, half a segment either side ----------------------------
        # A segment is FILLED only when the stages at both of its ends are
        # behind you. A run that skipped a stage still shows a continuous line
        # through it: the run did pass through, it just had nothing to do there.
        def seg(x1, x2, solid):
            c = QColor(ink) if solid else mid
            if solid:
                c.setAlpha(120)
            pen = QPen(c, 2)
            if not solid:
                pen.setStyle(Qt.PenStyle.DotLine)
            p.setPen(pen)
            p.drawLine(x1, cy, x2, cy)

        behind = self._state in (pipeline.DONE, pipeline.SKIPPED)
        if not self._first:
            seg(0, cx - r - 3, behind and self._left_done)
        if not self._last:
            seg(cx + r + 3, self.width(), behind and self._right_done)

        # -- the node ----------------------------------------------------------
        p.setPen(QPen(ink, 1.5))
        if self._state == pipeline.DONE:
            fill = QColor(ink)
            fill.setAlpha(38)
            p.setBrush(fill)
        elif self._current or self._state == pipeline.RUNNING:
            # The ring is the "you are here" marker; the word underneath says
            # "running", so the ring is never the only thing carrying it.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(ink, 2.5))
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPoint(cx, cy), r, r)

        gf = QFont(self._font)
        gf.setBold(self._current or self._state == pipeline.RUNNING)
        gf.setPointSizeF(max(7.0, self._font.pointSizeF() - 0.5))
        p.setFont(gf)
        p.setPen(ink)
        p.drawText(QRect(cx - r, cy - r, 2 * r, 2 * r),
                   int(Qt.AlignmentFlag.AlignCenter), self._glyph())

        # -- the label and the state, in words ---------------------------------
        lf = QFont(self._font)
        lf.setBold(self._current or self._state == pipeline.RUNNING)
        p.setFont(lf)
        p.setPen(ink)
        p.drawText(QRect(0, cy + r + 1, self.width(), 13),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   QFontMetrics(lf).elidedText(self._st.label,
                                               Qt.TextElideMode.ElideRight,
                                               self.width()))
        sub = QColor(ink)
        sub.setAlpha(150)
        p.setPen(sub)
        p.setFont(self._font)
        p.drawText(QRect(0, cy + r + 14, self.width(), 13),
                   int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                   QFontMetrics(self._font).elidedText(
                       self._tail, Qt.TextElideMode.ElideRight, self.width()))
        p.end()


class StageStrip(QWidget):
    """The eight stages, along the top of the run view.

    Reads a pipeline.RunRecord and nothing else, so it works identically for a
    one-card AI Create run and a sixty-point lecture. Deliberately cheap to
    rebuild: set_record() throws the nodes away and remakes them, because a run
    changes state a handful of times, not per frame.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._record = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(0)
        self._lay = lay
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.set_record(None)

    def set_record(self, record) -> None:
        self._record = record
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if record is None:
            self._lay.addStretch(1)
            return
        current = record.current
        stages = list(pipeline.STAGES)
        states = [record.state_of(st.key) for st in stages]
        behind = [s in (pipeline.DONE, pipeline.SKIPPED) for s in states]
        for i, st in enumerate(stages):
            node = _StageNode(
                st, states[i], self._tail(st, states[i], record),
                current is not None and current.key == st.key,
                first=(i == 0), last=(i == len(stages) - 1),
                left_done=behind[i - 1] if i else False,
                right_done=behind[i + 1] if i + 1 < len(stages) else False,
                parent=self)
            err = (record.errors or {}).get(st.key, "")
            node.setToolTip("\n\n".join(x for x in (
                f"{st.ordinal}. {st.label}",
                _STAGE_TIP.get(st.key, ""), _TIP.get(states[i], ""), err) if x))
            self._lay.addWidget(node, 1)

    @staticmethod
    def _tail(st, state: str, record) -> str:
        """What sits under the label: the count once there is one, else the
        state word. "14 points" says both that the stage ran and what came out
        of it, and the line is too short to carry both without truncating the
        useful half."""
        return (record.counts or {}).get(st.key, "") or _WORD.get(state, state)


# ── the one entry point ──────────────────────────────────────────────────────

def open_run(records: list, *, plans=None, matches=None, coverage=None,
             door: str = "", title: str = "", parent=None,
             on_committed=None, source: str = "", lecture=None):
    """Open the shared run view on whatever this door has already produced.

    Three ways in, and the door picks by how much it has done already:

      records only            -> the plan page, unread. (Knowledge Gaps)
      records + plans         -> the plan page, pre-planned; READ is skipped.
      records + plans
              + matches/cov   -> straight to the results page; READ, APPROVE
                                 and FIND are all skipped, because the door
                                 already did them. (AI Lecture)

    `records` are FLATTENED gap records — pipeline.WorkItem.to_kg_record() or
    bulk_search.create_record_for(). A door with no knowledge-gap store behind
    it (a lecture) supplies them directly and the view never touches the store.

    `on_committed(created, found_nids)` runs INSIDE the run's single
    checkpoint, for a door with a commit step of its own — AI Lecture attaches
    slide images there. `created` is {item_id: [(nid, card), ...]} so the door
    can act on the specific cards it just wrote. Anything it does is covered
    by the same Ctrl+Z.

    `lecture` (a lecture.door.LectureRun) opens the view on a lecture that has
    not been read yet: READ, APPROVE and FIND all run here, and `records` may
    be empty — the run supplies its own.
    """
    from .kg.bulk_search import BulkSearchDialog
    from aqt.utils import tooltip

    records = [r for r in (records or []) if isinstance(r, dict)]
    if lecture is not None and not records:
        records = [lecture.record()]
    if not records:
        tooltip("Nothing to run.")
        return None
    dlg = BulkSearchDialog(records, parent=parent,
                           plans=plans, matches=matches, coverage=coverage,
                           door=door, title=title, on_committed=on_committed,
                           source=source, lecture=lecture)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


# ── HTML: the run, written out ───────────────────────────────────────────────
#
# Qt is for ACTING — ticking, editing, rewriting, splitting. That is what the
# tree is and always was. This is for READING: what your deck covered, what it
# didn't, and what was written for the difference, as prose rather than as text
# crammed into tree row labels. It renders on the SAME page (a stacked widget
# behind the tree), not in a second window.

def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


_SUMMARY_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       font-size: 13px; line-height: 1.55; padding: 4px 14px 24px; }
h2 { font-size: 16px; margin: 18px 0 6px; }
h3 { font-size: 13px; margin: 14px 0 4px; font-weight: 600; }
.lede { opacity: .75; margin: 0 0 14px; }
.strip { font-size: 11px; opacity: .8; margin: 0 0 14px;
         border-bottom: 1px solid rgba(128,128,128,.35); padding-bottom: 8px; }
.strip span { display: inline-block; margin-right: 10px; white-space: nowrap; }
.strip .skipped { opacity: .5; }
ul { margin: 4px 0 10px; padding-left: 20px; }
li { margin: 2px 0; }
.pt { font-weight: 600; }
.card { border-left: 3px solid rgba(128,128,128,.4); padding: 2px 0 2px 10px;
        margin: 4px 0; }
.none { opacity: .65; font-style: italic; }
.tag { font-size: 11px; opacity: .7; }
"""


def summary_html(record, plans, coverage, *, ticked=None, written=None,
                 approved=None) -> str:
    """One run, as a document.

    Everything here is derived from what is on screen NOW — the ticks, not what
    the search originally concluded — because the summary's whole job is to be
    the thing you read before pressing Apply.
    """
    ticked = ticked or {}
    written = list(written or [])
    approved = list(approved or [])
    plans = list(plans or [])

    out = ["<style>" + _SUMMARY_CSS + "</style>"]

    # The strip again, in words. Same eight, skipped ones included.
    if record is not None:
        bits = []
        for st in pipeline.STAGES:
            state = record.state_of(st.key)
            tail = (record.counts or {}).get(st.key, "") or _WORD.get(state, state)
            cls = " class='skipped'" if state in (pipeline.PENDING,
                                                  pipeline.SKIPPED) else ""
            bits.append(f"<span{cls}>{st.ordinal}. {_esc(st.label)} — "
                        f"{_esc(tail)}</span>")
        out.append("<div class='strip'>" + "".join(bits) + "</div>")

    n_found = sum(len(r.get("nids") or []) for r in ticked.values())
    out.append("<h2>What this run found</h2>")
    out.append(
        f"<p class='lede'>{len(plans)} item(s) · {n_found} card(s) you already "
        f"own, ticked · {len(approved)} of {len(written)} written card(s) "
        "approved.</p>")

    by_item = {}
    if coverage is not None:
        for m in (list(coverage.covered) + list(coverage.gaps)):
            by_item.setdefault(getattr(m.point, "kg_id", ""), []).append(m)

    for plan in plans:
        out.append(f"<h2>{_esc(plan.title)}</h2>")
        if plan.concept and plan.concept != plan.title:
            out.append(f"<p class='lede'>{_esc(plan.concept)}</p>")
        if plan.tags:
            out.append("<p class='tag'>tags: "
                       + _esc(", ".join(plan.tags)) + "</p>")

        group = by_item.get(plan.kg_id) or []
        if group:
            covered = [m for m in group if m.final]
            gaps = [m for m in group if not m.final]
            out.append(f"<h3>Your deck covers {len(covered)} of "
                       f"{len(group)} point(s)</h3>")
            if covered:
                out.append("<ul>")
                for m in covered:
                    out.append(f"<li><span class='pt'>{_esc(m.point.concept)}"
                               f"</span> — {len(m.final)} card(s)</li>")
                out.append("</ul>")
            if gaps:
                out.append("<h3>Nothing in your deck tests these</h3><ul>")
                for m in gaps:
                    out.append(f"<li><span class='pt'>{_esc(m.point.concept)}"
                               "</span></li>")
                out.append("</ul>")
        elif plan.points:
            out.append("<h3>Points (your deck was not searched for these)</h3>"
                       "<ul>")
            for pt in plan.points:
                out.append(f"<li>{_esc(getattr(pt, 'concept', ''))}</li>")
            out.append("</ul>")

    if written:
        out.append(f"<h2>Cards written ({len(approved)} of {len(written)} "
                   "approved)</h2>")
        for c in written:
            front = _esc(str((c or {}).get("front") or ""))[:400]
            extra = _esc(str((c or {}).get("extra") or ""))[:300]
            mark = "✓" if c in approved else "○ not approved"
            out.append(f"<div class='card'><b>{mark}</b> {front}"
                       + (f"<br><span class='lede'>{extra}</span>" if extra else "")
                       + "</div>")
    elif record is not None and record.state_of(pipeline.WRITE) == pipeline.SKIPPED:
        out.append("<h2>Nothing was written</h2><p class='none'>"
                   + _esc((record.counts or {}).get(pipeline.WRITE, "")
                          or "this run had nothing left to write for.")
                   + "</p>")
    return "".join(out)
