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
             on_committed=None, unified: bool = True):
    """Open the shared run view on whatever this door has already produced.

    Four ways in, and the door picks by how much it has done already:

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
    """
    from .kg.bulk_search import BulkSearchDialog
    from aqt.utils import tooltip

    records = [r for r in (records or []) if isinstance(r, dict)]
    if not records:
        tooltip("Nothing to work through.")
        return None
    dlg = BulkSearchDialog(records, parent=parent, unified=unified,
                           plans=plans, matches=matches, coverage=coverage,
                           door=door, title=title, on_committed=on_committed)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
