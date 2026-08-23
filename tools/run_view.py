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
    QFrame, QHBoxLayout, QLabel, QSizePolicy, Qt, QWidget,
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


class StageStrip(QWidget):
    """The eight stages, along the top of the run view.

    Reads a pipeline.RunRecord and nothing else, so it works identically for a
    one-card AI Create run and a sixty-point lecture. Deliberately cheap to
    rebuild: set_record() throws the boxes away and remakes them, because a run
    changes state a handful of times, not per frame.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._record = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 4)
        lay.setSpacing(4)
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
        for st in pipeline.STAGES:
            state = record.state_of(st.key)
            self._lay.addWidget(self._box(st, state, record,
                                          current is not None
                                          and current.key == st.key))
        self._lay.addStretch(1)

    def _box(self, st, state: str, record, is_current: bool) -> QWidget:
        count = (record.counts or {}).get(st.key, "")
        word = _WORD.get(state, state)
        num = _CIRCLED[st.ordinal - 1] if st.ordinal <= len(_CIRCLED) else str(st.ordinal)
        # The count replaces the state word once there IS one: "14 points" says
        # both that the stage ran and what came out of it, and the box is too
        # small to carry both without truncating the useful half.
        tail = count or word
        lbl = QLabel(f"{num} {st.label} · {tail}")
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        err = (record.errors or {}).get(st.key, "")
        lbl.setToolTip("\n\n".join(x for x in (
            _STAGE_TIP.get(st.key, ""), _TIP.get(state, ""), err) if x))
        muted = state in (pipeline.PENDING, pipeline.SKIPPED)
        weight = "bold" if is_current or state == pipeline.RUNNING else "normal"
        # Palette-relative so it reads on both Anki themes; the words carry the
        # meaning either way.
        colour = ("palette(mid)" if muted else
                  "palette(bright-text)" if state == pipeline.FAILED
                  else "palette(text)")
        lbl.setStyleSheet(
            f"QLabel {{ color: {colour}; font-size: 11px; font-weight: {weight};"
            "  padding: 2px 6px; border: 1px solid palette(mid);"
            "  border-radius: 3px; }")
        return lbl


def separator() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


# ── the one entry point ──────────────────────────────────────────────────────

def open_run(records: list, *, plans=None, matches=None, coverage=None,
             door: str = "", title: str = "", parent=None,
             on_committed=None, unified: bool = True, adopt_batch: str = "",
             source: str = ""):
    """Open the shared run view on whatever this door has already produced.

    Four ways in, and the door picks by how much it has done already:

      records only            -> the plan page, unread. (Knowledge Gaps)
      records + plans         -> the plan page, pre-planned; READ is skipped.
      records + plans
              + matches/cov   -> straight to the results page; READ, APPROVE
                                 and FIND are all skipped, because the door
                                 already did them. (AI Lecture)
      records + plans
              + adopt_batch   -> the write-only page, showing a create_jobs
                                 batch this window did not start. (AI Create)

    `records` are FLATTENED gap records — pipeline.WorkItem.to_kg_record() or
    bulk_search.create_record_for(). A door with no knowledge-gap store behind
    it (a lecture) supplies them directly and the view never touches the store.

    `on_committed(created, found_nids)` runs INSIDE the run's single
    checkpoint, for a door with a commit step of its own — AI Lecture attaches
    slide images there. `created` is {item_id: [(nid, card), ...]} so the door
    can act on the specific cards it just wrote. Anything it does is covered
    by the same Ctrl+Z.
    """
    from .kg.bulk_search import BulkSearchDialog
    from aqt.utils import tooltip

    records = [r for r in (records or []) if isinstance(r, dict)]
    if not records:
        tooltip("Nothing to work through.")
        return None
    dlg = BulkSearchDialog(records, parent=parent, unified=unified,
                           plans=plans, matches=matches, coverage=coverage,
                           door=door, title=title, on_committed=on_committed,
                           adopt_batch=adopt_batch, source=source)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


def open_written(job: dict, *, parent=None, door: str = "create"):
    """A finished create_jobs generation, on the shared run view.

    The door for AI Create: it enqueues its job exactly as it always has, and
    when the cards land they appear here rather than in a review dialog of
    their own. One work item, one plan built from the points the job was asked
    to cover (or none, for a plain "N cards on a topic"), and the batch adopted
    rather than started.

    Returns the dialog, or None when the job can't be shown here.
    """
    if not isinstance(job, dict):
        return None
    batch = str(job.get("batch_id") or job.get("id") or "")
    if not batch:
        return None
    item = pipeline.WorkItem(
        id=str(job.get("kg_id") or f"create:{batch}"),
        title=str(job.get("title") or job.get("topic_label") or "Generation"),
        kind=pipeline.KIND_TOPIC,
        type_key=str(job.get("kg_type_for_image") or ""),
        tags=list(job.get("tags_raw") or []),
        fields={"notes": str(job.get("kg_notes") or ""),
                "stem_html": str(job.get("kg_stem_html") or ""),
                "explanation": str(job.get("kg_explanation") or ""),
                "images": list(job.get("kg_image_filenames") or [])},
    )
    # The points the job was asked to cover, so each card files under the point
    # it answers rather than in one undifferentiated heap. A plain topic
    # generation has none, and that is fine — the cards sit under the item.
    concepts = [str(c or "").strip() for c in (job.get("mass_points") or [])]
    plan = pipeline.Plan(
        item=item,
        concept=str(job.get("topic_label") or item.title),
        cards=int(job.get("n_cards") or 0) or len(concepts),
        points=[_bare_point(c) for c in concepts if c],
    )
    from . import autotag
    return open_run([item.to_kg_record(concept=plan.concept)],
                    plans=[plan], door=door, adopt_batch=batch,
                    title=f"AI Create — {item.title}", parent=parent,
                    source=autotag.source_path_for(
                        tool="card_creator",
                        kg_type_key=str(job.get("kg_type_for_image") or "")))


def _bare_point(concept: str):
    """A LearningPoint carrying only a concept. The write-only tree needs
    somewhere to hang each card; it never searches, so terms and levels would
    go unused."""
    from .lecture.extractor import LearningPoint
    return LearningPoint(concept=concept)


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
