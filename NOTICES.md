# Third-party notices

Ankisstant bundles a small amount of code adapted from other open-source
Anki add-ons. Their original licenses apply to the corresponding files.

## Card Management (AJT)

- File: `tools/qbank/grade_cards.py`
- Source: https://ankiweb.net/shared/info/1021636467
- Upstream repo: https://github.com/Ajatt-Tools/CardManagement
- Copyright: Ren Tatsumoto <tatsu at autistici.org>
- License: GNU AGPL v3 or later — https://www.gnu.org/licenses/agpl-3.0.html

The functions `grade_cards`, `_adjust_intervals`, `_last_rep_day`, and
`_days_since_last_rep` are ported (with minor edits — type hints relaxed,
the undo-entry decorator inlined, the scheduler-version check moved to
the caller) from `grade_now.py` in the Card Management add-on. Because
AGPL v3 is copyleft, distributing this add-on means distributing the
corresponding source — which is satisfied by the add-on being shipped
as plain Python (no compilation step).
