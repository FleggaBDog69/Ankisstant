# QBank-specific Anki ops. The broadly-useful helpers (tag/unsuspend/find/strip)
# live in core.anki_utils — this module is just the bits unique to QBank's
# "append to missed-questions field + re-rate Again" flow.

from __future__ import annotations

import time

from aqt import mw

from ...core import log
from .grade_cards import grade_cards as _grade_cards


def mark_again(note_id: int) -> tuple[int, int]:
    """For each card of this note:
      - new + suspended → unsuspend (don't rate)
      - review/learning → rate as Again (v3 scheduler) or fall back to a
        queue-manipulation reset on the v2 scheduler.
    Returns (graded_count, unsuspended_count)."""
    if mw.col is None:
        return (0, 0)
    try:
        note = mw.col.get_note(note_id)
        cards = [mw.col.get_card(cid) for cid in note.card_ids()]

        to_unsuspend = []
        to_grade     = []
        for c in cards:
            if c.type == 0:  # new
                if c.queue == -1:
                    to_unsuspend.append(c)
            elif c.queue >= 0:  # review/learning, not suspended/buried
                to_grade.append(c)

        if to_unsuspend:
            try:
                mw.col.sched.unsuspend_cards([c.id for c in to_unsuspend])
            except Exception as e:
                log.warn(f"unsuspend_cards failed, falling back: {e}")
                for c in to_unsuspend:
                    c.queue = 0
                    mw.col.update_card(c)

        if to_grade:
            graded_properly = False
            if getattr(mw.col.sched, "version", 0) >= 3:
                try:
                    _grade_cards(mw.col, to_grade, 1)
                    graded_properly = True
                except Exception as e:
                    log.warn(f"grade_cards failed, falling back: {e}")
            if not graded_properly:
                now = int(time.time())
                for card in to_grade:
                    card.queue = 1
                    card.type  = 1
                    card.due   = now
                    card.ivl   = 0
                    mw.col.update_card(card)

        return (len(to_grade), len(to_unsuspend))
    except Exception as e:
        log.error(f"mark_again note {note_id} failed: {e}")
        return (0, 0)
