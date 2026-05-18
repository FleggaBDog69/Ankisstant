# Ported from the "Card Management" Anki add-on by Ren Tatsumoto.
# Source: https://ankiweb.net/shared/info/1021636467
# Upstream: https://github.com/Ajatt-Tools/CardManagement
# Original copyright: Ren Tatsumoto <tatsu at autistici.org>
# License: GNU AGPL, version 3 or later — http://www.gnu.org/licenses/agpl.html
#
# Only the minimum needed for an "Again"-grade pass is reproduced here:
# interval adjustment for prematurely-answered cards, plus the answerCard
# loop, wrapped in a custom undo entry.

from __future__ import annotations

from collections.abc import Sequence


def _last_rep_day(card) -> int:
    return card.due - card.ivl


def _days_since_last_rep(col, card) -> int:
    return col.sched.today - _last_rep_day(card)


def _adjust_intervals(col, cards: Sequence) -> None:
    """When answering a card before it's due, its real interval is smaller
    than the stored one. Shrink such cards' intervals to reality so the
    next scheduling step doesn't over-credit them."""
    for card in cards:
        passed = _days_since_last_rep(col, card)
        if card.ivl > passed >= 0:
            card.ivl = passed
    col.update_cards(list(cards))


def grade_cards(col, cards: Sequence, ease: int) -> None:
    """Grade each card with the given ease (1=Again, 2=Hard, 3=Good, 4=Easy)
    via the FSRS/v3 scheduler. Requires the v3 scheduler. Wrapped in a
    custom undo entry so the operation appears in Anki's undo stack."""
    pos = col.add_custom_undo_entry("Ankisstant: grade cards")
    try:
        _adjust_intervals(col, cards)
        for card in cards:
            card.start_timer()
            col.sched.answerCard(card, ease)
    finally:
        col.merge_undo_entries(pos)
