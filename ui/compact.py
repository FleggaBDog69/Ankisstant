"""Making a window-sized page fit a side panel.

Every tool page was laid out for a ~900px window. In the dock they're half that,
and the parts that overflow are always the same four things: fields with a hard
minimum width, combo boxes sized to their longest entry, long button and
checkbox wording, and generous margins repeated down a form.

`compactify()` is one pass over an already-built panel, run **only** when
Ankisstant is in the dock, and only once per panel. It is not a redesign of any
tool — nothing here changes what a page does, what it contains, or the order of
it. The popped-out window builds its panels without this pass, so the full-size
layout is still the layout.

Everything degrades quietly: a shortened caption that no longer matches its entry
here simply isn't shortened, which leaves today's behaviour.
"""

from __future__ import annotations

# Below this a field is too narrow to be worth having; above it, the minimum is
# a floor the layout can't negotiate below and the page runs off the right edge.
FIELD_MIN = 100

# Roughly six characters plus the chevron. A combo sized to
# "AnKingOverhaul (AnKing Step Deck / AnKingMed)" is on its own enough to push a
# whole row off the panel, and unlike a text field its minimum comes from its
# *contents*, so lowering minimumWidth alone does nothing.
COMBO_CHARS = 6

# Shorter wording for the dock only, keyed by the full caption. The long form
# goes to the tooltip, so nothing is actually lost — it moves.
#
# Keyed by text rather than by widget because the alternative is a compact
# branch in every tool file, which is how a UI ends up with two of everything.
# The cost is that re-wording a button upstream silently opts it out of this
# table; the failure is "not shortened", which is where we started.
_SHORT: dict[str, str] = {
    # AI Create
    "📎 Attach PDF / PPTX…":        "📎 PDF / PPTX…",
    "📷 Attach images for Extra…":  "📷 Images…",
    "🔍 Find image online…":        "🔍 Find online…",
    "Source-based (more reliable)": "Source-based",
    "Topic-based (verify carefully)": "Topic-based",
    "Number of cards:":             "Cards:",
    "Tags (comma-sep):":            "Tags:",
    "Focus (optional):":            "Focus:",
    "Extra images:":                "Images:",
    "Paste text below, enter a URL, and/or attach PDFs / PowerPoints.":
        "Paste text, enter a URL, and/or attach files.",
    # AI Browse
    "Open in browser":              "In browser",
    "Tag + Unsuspend":              "Tag + Unsusp.",
    "Apply audit tag":              "Audit tag",
    "Grade Again (MQ only)":        "Grade Again",
    "Tag to apply:":                "Tag:",
    "Topic (broad or narrow — the AI figures out the search terms):":
        "Topic (broad or narrow):",
    # AI Lecture
    "Read diagrams as images":      "Read diagrams",
    "Add facts beyond the lecture": "Add extra facts",
    "Re-read file":                 "Re-read",
    "Unsuspend the matched cards":  "Unsuspend matches",
    "Suggest slides to attach…":    "Suggest slides",
    "Choose File...":               "Choose…",
    "Choose File…":                 "Choose…",
    "No file chosen — .pdf, .pptx, .docx, .txt or .md":
        "No file chosen — pdf/pptx/docx/txt/md",
}

# Long enough that it will wrap rather than set the width of its row.
_WRAP_AT = 45


def compactify(root) -> None:
    """Fit `root` and everything under it into a narrow panel."""
    try:
        from aqt.qt import (QAbstractButton, QComboBox, QLabel, QLayout,
                            QWidget)
    except Exception:
        return

    try:
        widgets = [root, *root.findChildren(QWidget)]
    except Exception:
        return

    for w in widgets:
        try:
            _relax_width(w)
            if isinstance(w, QComboBox):
                _shrink_combo(w, QComboBox)
            if isinstance(w, (QAbstractButton, QLabel)):
                _shorten(w)
            if isinstance(w, QLabel):
                _wrap(w)
        except Exception:
            continue

    try:
        for layout in root.findChildren(QLayout):
            _tighten(layout)
        if root.layout() is not None:
            _tighten(root.layout())
    except Exception:
        pass


def _relax_width(w) -> None:
    """Lower a hard minimum width to something a narrow panel can honour.

    A minimum is a floor the layout cannot negotiate below, so instead of the
    field shrinking, the scroll area's contents get pushed wider than the
    viewport and everything to the right runs off the edge.

    Widgets where minimum == maximum are left alone: those came from
    `setFixedWidth`, are sized to their own text, and squeezing them clips
    rather than reflows.
    """
    mn = w.minimumWidth()
    if mn > FIELD_MIN and mn != w.maximumWidth():
        w.setMinimumWidth(FIELD_MIN)


def _shrink_combo(combo, QComboBox) -> None:
    """Stop a combo from demanding the width of its longest entry.

    The drop-down list keeps its own width, so the full text is still readable
    where you actually read it — when it's open.
    """
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(COMBO_CHARS)


_FULL = "_ank_full_text"


def _shorten(w) -> None:
    short = _SHORT.get(w.text())
    if not short:
        return
    if not w.toolTip():
        w.setToolTip(w.text())     # the long form, one hover away
    setattr(w, _FULL, w.text())
    w.setText(short)


def uncompactify(root) -> None:
    """Put the full wording back — this panel is going into a real window.

    Only the captions. The widths and margins are left where they are: they were
    minimums and paddings, and in a window twice as wide the layout has room to
    stretch past both without being told.

    A panel is carried across a pop-out rather than rebuilt (the tools cache
    them), so without this the window would keep the dock's abbreviations.
    """
    try:
        from aqt.qt import QAbstractButton, QLabel, QWidget
    except Exception:
        return
    try:
        for w in [root, *root.findChildren(QWidget)]:
            if not isinstance(w, (QAbstractButton, QLabel)):
                continue
            full = getattr(w, _FULL, None)
            # Only if it still says what we left it saying — a button whose text
            # has since been changed by its own code is not ours to overwrite.
            if full and w.text() == _SHORT.get(full):
                w.setText(full)
    except Exception:
        pass


def _wrap(label) -> None:
    """Let long prose wrap instead of setting the width of its row."""
    if label.wordWrap():
        return
    text = label.text() or ""
    if len(text) >= _WRAP_AT and not text.lstrip().startswith("<"):
        label.setWordWrap(True)


def _tighten(layout) -> None:
    m = layout.contentsMargins()
    layout.setContentsMargins(min(m.left(), 8), min(m.top(), 6),
                              min(m.right(), 8), min(m.bottom(), 6))
    if layout.spacing() > 6:
        layout.setSpacing(6)
