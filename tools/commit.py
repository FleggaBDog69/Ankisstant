# Stage 8 — the one commit.
#
# Seven surfaces used to open their own mw.checkpoint and then do their own
# version of the same four things: tag some notes, unsuspend their cards,
# append something to a field, add some new notes. The MECHANISM was already
# shared (core/anki_utils.tag_notes / unsuspend_cards), so what diverged was
# policy — and the policies almost agreed. Almost is the problem: which tag
# went on first, whether the audit tag went on the found cards or all of them,
# whether the month tag existed at all on that path, and how many separate
# undo steps it took to reverse a run.
#
# There is now one implementation. A door builds a pipeline.CommitPlan — plain
# data, no widgets, buildable and testable off the main thread — and calls
# run(). One mw.checkpoint wraps the lot, so one Ctrl+Z takes back a whole
# pass on every path.
#
# ORDER IS THE CONTRACT, and it is why CommitPlan holds lists rather than
# dicts:
#   * the item's own tag goes on before the audit and month tags, so a note's
#     most specific tag is the one it gets first and the shared ones can never
#     displace it;
#   * field appends run in the order the door listed them, because AI Lecture
#     puts card notes on before slide images — a line you can read beats a
#     slide you have to squint at, and text must land above the pictures.
# A dict would silently lose both.

from __future__ import annotations

from aqt import mw

from ..core import anki_utils, log


class Result:
    """What a commit actually changed. Counted rather than assumed: 'tagged 40
    notes' when four of them had been deleted since the search is the kind of
    report that hides a real failure."""

    def __init__(self):
        self.tagged = 0            # notes that received at least one tag
        self.unsuspended = 0       # cards that went from suspended to not
        self.appended = 0          # field appends that landed
        self.added = 0             # new notes created
        self.closed = 0            # work items marked done
        self.errors: list = []

    def __repr__(self):
        return (f"Result(tagged={self.tagged}, unsuspended={self.unsuspended}, "
                f"appended={self.appended}, added={self.added}, "
                f"closed={self.closed}, errors={len(self.errors)})")


def cards_of(nids: list) -> list:
    """Every card id under these notes. Skips a note that has since been
    deleted rather than taking the run down with it."""
    cids: list = []
    for nid in nids or []:
        try:
            note = mw.col.get_note(int(nid))
        except Exception:
            continue
        try:
            cids.extend(note.card_ids() if hasattr(note, "card_ids")
                        else [c.id for c in note.cards()])
        except Exception:
            continue
    return cids


def run(plan, *, checkpoint: bool = True, close_items=None) -> Result:
    """Execute a pipeline.CommitPlan. Returns what it changed.

    `checkpoint=False` is for a caller that has ALREADY opened one and is
    committing several plans inside it — the point of the checkpoint is that
    it covers the whole pass, so nesting one per plan would defeat it.

    `close_items(ids) -> int` marks the work items done. Passed in rather than
    imported so this module stays free of the knowledge-gap store: a door with
    no store behind it (AI Lecture) has nothing to close.

    Never raises. A commit that half-worked must report what landed, not
    unwind into an exception on top of a collection it has already written to.
    """
    res = Result()
    if plan is None or plan.is_empty:
        return res
    if mw is None or mw.col is None:
        res.errors.append("no collection")
        return res

    if checkpoint and hasattr(mw, "checkpoint"):
        mw.checkpoint(plan.label or "Ankisstant")

    # 1 — tags, in the order the door listed them.
    touched: set = set()
    for tag, nids in (plan.tag_ops or []):
        tag = str(tag or "").strip()
        nids = [int(n) for n in (nids or [])]
        if not tag or not nids:
            continue
        try:
            anki_utils.tag_notes(nids, tag)
            touched.update(nids)
        except Exception as e:
            res.errors.append(f"tagging {tag}: {e}")
            log.error(f"commit: tagging '{tag}' failed: {e}")
    res.tagged = len(touched)

    # 2 — unsuspend. Counted by measuring, not by counting what was asked for:
    # a card that was already in rotation was never unsuspended by this run and
    # saying otherwise overstates what the pass did.
    if plan.unsuspend_nids:
        cids = cards_of(plan.unsuspend_nids)
        if cids:
            try:
                before = sum(1 for c in cids if mw.col.get_card(c).queue == -1)
                anki_utils.unsuspend_cards(cids)
                after = sum(1 for c in cids if mw.col.get_card(c).queue == -1)
                res.unsuspended = max(0, before - after)
            except Exception as e:
                res.errors.append(f"unsuspending: {e}")
                log.error(f"commit: unsuspend failed: {e}")

    # 3 — field appends, in list order. See the ordering note at the top.
    for nid, field, html in (plan.field_appends or []):
        try:
            if anki_utils.append_to_field(int(nid), str(field), str(html)):
                res.appended += 1
        except Exception as e:
            res.errors.append(f"appending to {nid}: {e}")

    # 4 — new notes. The door supplies them already built; adding them is the
    # only part that belongs here.
    if plan.new_cards:
        res.added = _add_notes(plan, res)

    # 5 — close the work items, last: a gap must never be marked done on the
    # strength of a commit that then failed.
    if plan.close_item_ids and close_items is not None:
        try:
            res.closed = int(close_items(list(plan.close_item_ids)) or 0)
        except Exception as e:
            res.errors.append(f"closing items: {e}")
            log.error(f"commit: closing work items failed: {e}")
    return res


def _add_notes(plan, res: Result) -> int:
    """Add the plan's new cards through card_creator's committer, which is the
    only thing that knows how a card dict maps onto a notetype's fields."""
    try:
        from . import card_creator as cc
    except Exception as e:
        res.errors.append(f"card creator unavailable: {e}")
        return 0
    ctx = getattr(plan, "commit_ctx", None)
    nt = getattr(plan, "notetype", None)
    if ctx is None or nt is None:
        res.errors.append("no notetype/context for the new cards")
        return 0
    try:
        ids = cc.commit_cards(list(plan.new_cards), ctx=ctx,
                              deck_name=plan.deck_name or "Default",
                              notetype=nt)
        plan.created_ids = list(ids)
        return len(ids)
    except Exception as e:
        res.errors.append(f"adding cards: {e}")
        log.error(f"commit: adding {len(plan.new_cards)} card(s) failed: {e}")
        return 0


def refresh() -> None:
    """Repaint Anki after a commit. Separate from run() so a caller committing
    several plans resets once at the end rather than after each."""
    try:
        mw.col.reset()
        mw.reset()
    except Exception:
        pass


# ── the shared policy ────────────────────────────────────────────────────────

def tag_ops_for(*, per_item: list, audit: str = "", month: str = "",
                everything: list | None = None) -> list:
    """The tag order every door agreed on, made explicit.

    `per_item` is [(tag, nids), ...] — the specific tag for each item's own
    notes. The audit tag and the month tag then go on, after it and in that
    order.

    `everything` is which notes the SHARED tags cover, and it is a separate
    argument rather than derived from per_item for one reason: a gap whose tag
    box is empty contributes no per-item entry at all, but its notes were still
    part of the run and must still carry the audit trail. Deriving the set from
    the tagged notes silently dropped the audit tag from exactly the gaps the
    confirm dialog warns you about. Defaults to the per-item notes when the
    caller has nothing else to say.

    This is the policy that was reimplemented five times with small
    differences; stating it once is most of what stage 8 was for.
    """
    ops = [(t, list(n)) for t, n in (per_item or []) if t and n]
    if everything is None:
        everything = [n for _t, nids in (per_item or []) for n in (nids or [])]
    shared_nids: list = []
    seen: set = set()
    for n in everything or []:
        if n not in seen:
            seen.add(n)
            shared_nids.append(n)
    if shared_nids:
        for shared in (audit, month):
            shared = str(shared or "").strip()
            if shared:
                ops.append((shared, list(shared_nids)))
    return ops
