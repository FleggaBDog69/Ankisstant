# Knowledge Gaps — the unified queue page.
#
# Replaces the old "Analyse Knowledge Gaps" sidebar entry. Holds every KG
# coming from any source (manual, analyse, qbank, browse) in a single
# persistent list (see tools/kg/store.py).
#
# A KG's lifecycle from here:
#   - "Send to Browse with Claude" → preloads the Browse panel with the KG
#     title; on a successful tag/unsuspend pass the KG is marked done.
#   - "+ Create card from this KG" → pushes the KG onto MainWindow.gap_queue
#     (richer dict shape — carries stem_html + notes) and switches to
#     Create with Claude. When Create adds the card the KG is marked done.

from __future__ import annotations

import html as _html
import re

from aqt import mw
from aqt.qt import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFont, QFontMetrics, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QColor,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRect,
    QScrollArea,
    QSizePolicy,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QHeaderView,
    QTableWidget, QTableWidgetItem, QKeySequence,
    QShortcut, Qt,
    QTimer, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ..core import anki_utils, log
from ..core.config import (
    auto_tag_base, save_tool_config, tool_config,
)
from ..core.qt_utils import (
    FlowLayout, compact_button,
    attach_source_completer, attach_tag_completer, make_help_button,
    make_setup_banner,
    provider_configured, refresh_deck_browser, run_claude_json,
    set_ai_buttons_enabled, theme_dialog,
)
from . import autotag
from .kg import importers, store as kg_store
from . import pipeline


NAME = "Knowledge Gaps"


# ── helpers ──────────────────────────────────────────────────────────────────


def _queue_owner(widget):
    """The Ankisstant body holding gap_queue / browse_queue, or None.

    Imported lazily — ui.main_window imports the tools, so a module-level
    import here would be circular.
    """
    try:
        from ..ui.main_window import queue_owner
        return queue_owner(widget)
    except Exception as e:
        print(f"[ankisstant] couldn't resolve the Ankisstant queues: {e}")
        return None


def _gap_items(lst) -> list:
    """Every gap row in a list, in page order. Headers are skipped."""
    return [it for it in (lst.item(i) for i in range(lst.count()))
            if isinstance(it, _KGListItem)]


# A gap's tick box is its Closed state (see _KGListItem.refresh), so there is
# no separate tick set to load, save or push onto the rows any more. The old
# helpers read and wrote kg/ticks.py, which nothing calls now.


def open_anki_browse(title: str, *, as_tag: bool) -> None:
    """Open Anki's Browse window on this title — as a tag search or a card one.

    Tags can't hold spaces, so the tag form joins the words with the underscore
    Anki uses and wraps the lot in wildcards: "aortic stenosis" becomes
    tag:*aortic_stenosis*, which matches it at any depth of your tree. The card
    form is the bare title, unquoted — so Anki reads it as one term per word
    and ANDs them, which finds a card whose wording isn't the gap's wording.
    Quoting it would demand that exact phrase and find far less.

    Deliberately NOT the gap's computed auto-tag. That tag is what a card WILL
    be filed under once one exists; this button is for finding what is already
    there, which was very often filed by hand under something close but not
    identical."""
    title = " ".join(str(title or "").split())
    if not title:
        tooltip("That gap has no title to search for.")
        return
    # Unquoted, both of them. The tag form needs no quotes — the underscores
    # have already made it one word — and the card form is better off without
    # them, as a set of ANDed words rather than one exact phrase.
    if as_tag:
        query = "tag:*{}*".format(title.replace(" ", "_").replace('"', ""))
    else:
        query = title.replace('"', "")
    try:
        from aqt import dialogs
        browser = dialogs.open("Browser", mw)
        browser.activateWindow()
        browser.raise_()
        browser.search_for(query)
    except Exception as e:
        print(f"[ankisstant] couldn't open Anki's browser: {e}")
        showWarning(f"Couldn't open Anki's Browse screen:\n\n{e}")


SOURCE_LABELS = {
    "manual":  "Manual",
    "analyse": "Analyse",
    "qbank":   "QBank",
    "browse":  "Browse",
    "chat":    "AI chat",
}

# Two states, and only two: Open or Closed.
#
# The store still carries "in_progress" and a `dismissed` flag — older records
# have them, and bulk_search and mass_create still set in_progress when they
# take a gap. Neither is a state you can pick or see any more. in_progress
# READS as Open, because a gap handed to a tool is still a gap you haven't
# closed; dismissed reads as Closed, because "closed without a card" and
# "closed with one" were never worth a checkbox of their own.
STATUS_LABELS = {
    "open":        "Open",
    "in_progress": "Open",
    "done":        "Closed",
}


def _load_types() -> list[dict]:
    """Return the configured KG types. Always includes at least the three
    factory defaults if a user has wiped the list."""
    cfg = tool_config("knowledge_gaps")
    types = cfg.get("types") or []
    cleaned: list[dict] = []
    for t in types:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key", "") or "").strip().lower()
        name = str(t.get("name", "") or "").strip()
        if not key or not name:
            continue
        cleaned.append(_type_row(t, key, name))
    if not cleaned:
        # Single source of truth: fall back to the config DEFAULTS type list
        # (with its full field schema) rather than a second hardcoded copy.
        from ..core.config import DEFAULTS
        for t in DEFAULTS["tools"]["knowledge_gaps"]["types"]:
            cleaned.append(_type_row(t, str(t.get("key", "")),
                                     str(t.get("name", ""))))
    return cleaned


def _type_row(t: dict, key: str, name: str) -> dict:
    """One configured type, whole.

    It used to be rebuilt from four hand-picked keys, which silently dropped
    "fields" — so _schema_for_type looked the type up, found no fields, and
    every KG's detail pane said "This type has no fields configured" even
    though the notes field was right there in the config. Copy the record and
    normalise the keys that are read positionally; don't enumerate what to
    keep, or the next key added to a type is lost the same way."""
    out = dict(t)
    out["key"] = key
    out["name"] = name
    out["color"] = str(t.get("color", "") or "#6b7280")
    out["description"] = str(t.get("description", "") or "")
    fields = t.get("fields")
    out["fields"] = list(fields) if isinstance(fields, list) else []
    return out


def _type_meta(key: str) -> dict:
    """Lookup a type by key, falling back to a muted '(removed)' shim so
    KGs that reference a deleted type still render."""
    key = (key or "").strip().lower()
    for t in _load_types():
        if t["key"] == key:
            return t
    return {"key": key or "?", "name": f"({key or '?'})",
            "color": "#9ca3af", "description": "Type removed from settings."}


def _short_date(iso: str) -> str:
    return (iso or "")[:10]


def _format_levels(kg: dict) -> str:
    levels = [kg_store.field(kg, "system"),
              kg_store.field(kg, "subsystem"),
              kg_store.field(kg, "topic")]
    return " :: ".join(l for l in levels if l)


def wake_group(name: str) -> list[str]:
    """Bring a group back onto the page because work has just gone into it.

    Archiving and collapsing are both pure UI state for a batch you are
    finished with — neither touches a gap. That makes them a trap the moment
    you file something new into an old batch: the gap is saved, the group is
    still archived, the Groups page still hides it (unless "Show archived" is
    on) and its section on the Gaps page is still collapsed, so the gap reads
    as lost. A group with new work in it is by definition not finished, so
    putting a gap in one wakes it up rather than making you go looking.

    Returns the states it cleared ("archived", "collapsed") so the caller can
    say what happened. One read-modify-write of the tool slice, the house
    pattern — three saves is three chances to drop the other settings.
    """
    name = str(name or "").strip()
    if not name:
        return []
    cleared: list[str] = []
    try:
        cfg = tool_config("knowledge_gaps")
        for key, label in (("archived_groups", "archived"),
                           ("collapsed_groups", "collapsed")):
            cur = [str(g) for g in (cfg.get(key) or [])]
            if name in cur:
                cfg[key] = [g for g in cur if g != name]
                cleared.append(label)
        if cleared:
            save_tool_config("knowledge_gaps", cfg)
    except Exception as e:
        # A failed save costs the group's visibility, not the gap.
        print(f"[ankisstant] couldn't wake group {name!r}: {e}")
        return []
    return cleared


def wake_groups(names) -> list[str]:
    """wake_group over a batch, in ONE config write. Returns the groups woken.

    A markdown import or an inbox drain can land gaps in a dozen groups at
    once; looping wake_group would save the tool slice a dozen times, which is
    a dozen chances to drop the rest of the settings."""
    wanted = [n for n in dict.fromkeys(str(g or "").strip() for g in names) if n]
    if not wanted:
        return []
    woken: list[str] = []
    try:
        cfg = tool_config("knowledge_gaps")
        dirty = False
        for key in ("archived_groups", "collapsed_groups"):
            cur = [str(g) for g in (cfg.get(key) or [])]
            hit = [n for n in wanted if n in cur]
            if hit:
                cfg[key] = [g for g in cur if g not in set(hit)]
                woken.extend(hit)
                dirty = True
        if dirty:
            save_tool_config("knowledge_gaps", cfg)
    except Exception as e:
        print(f"[ankisstant] couldn't wake groups: {e}")
        return []
    return list(dict.fromkeys(woken))


def _woke_suffix(cleared: list[str]) -> str:
    """The tail of a tooltip explaining that a put-away group came back."""
    if not cleared:
        return ""
    return " — group un" + " and un".join(cleared) + "."


# ── Add KG modal ─────────────────────────────────────────────────────────────

class AddKGDialog(QDialog):
    """Quick modal for the ＋ Add KG button (home screen + sidebar).

    `group` prefills the Group box. It is a prefill and not a hidden setting on
    purpose: every door into this dialog knows something about which batch you
    are working in (a group picked on the Groups page, a marked header, a
    `group:` filter), and the old dialog threw all of it away — so filing a gap
    meant saving it ungrouped and then opening a second dialog to move it.
    """

    def __init__(self, parent=None, group: str | None = None):
        super().__init__(parent or mw)
        self.setWindowTitle("＋ Add Knowledge Gap")
        self.setMinimumWidth(520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        layout.addWidget(QLabel("<b>Title</b>"))
        self._title = QLineEdit()
        self._title.setPlaceholderText("e.g. mechanism of digoxin toxicity in hypokalaemia")
        layout.addWidget(self._title)

        # Type and Group share one label width so their boxes line up.
        type_lbl, group_lbl = QLabel("<b>Type</b>"), QLabel("<b>Group</b>")
        _w = max(type_lbl.sizeHint().width(), group_lbl.sizeHint().width())
        type_lbl.setFixedWidth(_w)
        group_lbl.setFixedWidth(_w)
        type_row = QHBoxLayout()
        type_row.addWidget(type_lbl)
        self._type = QComboBox()
        for t in _load_types():
            self._type.addItem(t["name"], t["key"])
            self._type.setItemData(self._type.count() - 1,
                                   t.get("description", ""), Qt.ItemDataRole.ToolTipRole)
        # Default to the configured default_type_on_add (falls back to first).
        cfg = tool_config("knowledge_gaps")
        default_type = (cfg.get("default_type_on_add") or "kg").lower()
        idx = self._type.findData(default_type)
        if idx >= 0:
            self._type.setCurrentIndex(idx)
        type_row.addWidget(self._type, 1)
        layout.addLayout(type_row)

        # Group — the batch this gap came in with. Editable, so a new batch can
        # be started here rather than in Manage groups first.
        group_row = QHBoxLayout()
        group_row.addWidget(group_lbl)
        self._group = QComboBox()
        self._group.setEditable(True)
        self._group.addItem("")                      # blank = ungrouped
        for g in kg_store.groups():
            self._group.addItem(g)
        self._group.setCurrentText(str(group or "").strip())
        self._group.setToolTip(
            "The batch this gap belongs to — an SRS, a week, a placement. "
            "Leave blank to file it later.")
        group_row.addWidget(self._group, 1)
        layout.addLayout(group_row)
        self._group_hint = QLabel("")
        self._group_hint.setStyleSheet("color: gray; font-size: 10px;")
        self._group_hint.setTextFormat(Qt.TextFormat.RichText)
        self._group_hint.setWordWrap(True)
        layout.addWidget(self._group_hint)
        # Connected only once the label it writes into exists.
        self._group.editTextChanged.connect(self._refresh_group_hint)
        self._refresh_group_hint()

        layout.addWidget(QLabel("<b>Tags</b> <span style='color:gray;font-size:10px'>"
                                "(space-separated; optional)</span>"))
        self._tags = QLineEdit()
        self._tags.setPlaceholderText("School::Year3::Cardio::Digoxin")
        attach_tag_completer(self._tags, multi=True)
        layout.addWidget(self._tags)

        layout.addWidget(QLabel("<b>Notes</b> <span style='color:gray;font-size:10px'>"
                                "(optional — what specifically don't you know?)</span>"))
        self._notes = QPlainTextEdit()
        self._notes.setMinimumHeight(80)
        layout.addWidget(self._notes)

        # Image attach — copied into Anki media on Save, surfaced to Create
        # (Extra field, or Missed Questions for MQ-type) and Browse (appended
        # to the same field on each tagged note).
        self._image_filenames: list[str] = []
        img_row = QHBoxLayout()
        img_btn = QPushButton("📷 Attach image…")
        img_btn.setAutoDefault(False)
        img_btn.clicked.connect(self._on_attach_image)
        img_row.addWidget(img_btn)
        self._img_label = QLabel("")
        self._img_label.setStyleSheet("color: gray; font-size: 11px;")
        img_row.addWidget(self._img_label, 1)
        img_clear = QPushButton("Clear")
        img_clear.setAutoDefault(False)
        img_clear.clicked.connect(self._on_clear_images)
        img_row.addWidget(img_clear)
        layout.addLayout(img_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._save)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

        theme_dialog(self)
        self._title.setFocus()

    def _refresh_group_hint(self, *_a) -> None:
        """Say what the group will tag, and whether it is one you put away.

        The second half matters more than the first: filing into an archived
        batch used to be silent, and this is the one place you can still see it
        before the gap is written."""
        name = self._group.currentText().strip()
        if not name:
            self._group_hint.setText("")
            return
        bits: list[str] = []
        try:
            branch = autotag.group_branch(name)
        except Exception:
            branch = ""
        if branch:
            bits.append("Tags <code>" + _html.escape(branch) + "</code>")
        known = set(kg_store.groups())
        if name not in known:
            bits.append("new group")
        else:
            try:
                cfg = tool_config("knowledge_gaps")
                put_away = [
                    label for key, label in (("archived_groups", "archived"),
                                             ("collapsed_groups", "collapsed"))
                    if name in [str(g) for g in (cfg.get(key) or [])]
                ]
            except Exception:
                put_away = []
            if put_away:
                bits.append(" and ".join(put_away)
                            + " — saving here brings it back")
        self._group_hint.setText(" · ".join(bits))

    def _on_attach_image(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach image(s)",
            "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;All files (*)",
        )
        if not paths:
            return
        added = 0
        for p in paths:
            try:
                fname = mw.col.media.add_file(p)
            except Exception as e:
                print(f"[ankisstant] media.add_file failed for {p}: {e}")
                continue
            if fname and fname not in self._image_filenames:
                self._image_filenames.append(fname)
                added += 1
        if added:
            self._refresh_img_label()

    def _on_clear_images(self) -> None:
        self._image_filenames.clear()
        self._refresh_img_label()

    def _refresh_img_label(self) -> None:
        n = len(self._image_filenames)
        if n == 0:
            self._img_label.setText("")
        else:
            self._img_label.setText(f"{n} image{'s' if n != 1 else ''} attached")

    def _save(self) -> None:
        title = self._title.text().strip()
        if not title:
            tooltip("Title is required.")
            return
        tags = [t for t in self._tags.text().split() if t.strip()]
        notes = self._notes.toPlainText().strip()
        kg_type = self._type.currentData() or "kg"
        group = self._group.currentText().strip()
        fields: dict = {}
        if self._image_filenames:
            fields["images"] = list(self._image_filenames)
        kg_store.add(
            title=title,
            source="manual",
            type=kg_type,
            status="open",
            group=group,
            tags=tags,
            notes=notes,
            fields=fields,
        )
        if group:
            tooltip(f"Added to “{group}”{_woke_suffix(wake_group(group))}")
        refresh_deck_browser()
        # Tell any open KG panel to reload.
        try:
            _refresh_open_panel()
        except Exception:
            pass
        self.accept()


def open_add_kg_dialog(group: str | None = None) -> None:
    dlg = AddKGDialog(group=group)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


# ── KG list item ─────────────────────────────────────────────────────────────

# ── group rows and two-line items ────────────────────────────────────────────
# The list used to be one flat line per gap: title, type prefix, status suffix,
# everything else buried in a tooltip. With a term's worth of PBL LOs in it
# there was no way to see what a gap SAID, and no way to act on "all the ones
# from this SRS" as a unit. Groups fix the second; the notes preview fixes the
# first.
#
# Both are done with a delegate over the existing QListWidget rather than a
# QTreeWidget rewrite: every selection, context-menu and refresh path already
# works against QListWidget, and a header is just an item that isn't selectable.

NO_GROUP = "(no group)"

_HEADER_ROLE = Qt.ItemDataRole.UserRole + 1   # group name, on header rows
_NOTES_ROLE = Qt.ItemDataRole.UserRole + 2    # notes preview, on gap rows


def _notes_preview(kg: dict, limit: int = 110) -> str:
    """The first line or so of a gap's notes, for the list's second row.

    MQ gaps keep their content in stem_html rather than notes, so they fall back
    to it — otherwise every captured missed question would show a blank second
    line, which is the majority of the list for a QBank-heavy week."""
    raw = kg_store.field(kg, "notes") or ""
    if not str(raw).strip() and (kg.get("type") or "").lower() == "mq":
        raw = kg_store.field(kg, "stem_html") or ""
    text = _html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))
    text = " ".join(text.split())
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class _KGGroupHeader(QListWidgetItem):
    """A non-selectable section row: `▸ SRS 4 · 12 gaps · 9 open`.

    Enabled but not selectable, so it can still be right-clicked and clicked to
    collapse while never entering `selectedItems()` — which is what keeps every
    existing bulk action working untouched. Selecting SEVERAL groups therefore
    can't use the list's own selection model; it is `marked`, a flag the panel
    sets on Cmd-click and reads back off the headers."""

    def __init__(self, name: str, total: int, open_n: int, collapsed: bool,
                 done_n: int = 0, archived: bool = False,
                 marked: bool = False, checked: str = "off"):
        super().__init__()
        self.group_name = name
        self.collapsed = collapsed
        self.marked = marked
        self.setData(_HEADER_ROLE, name)
        # Checkable as well as enabled, and still not selectable: the header's
        # tick means "close every gap under me", the same thing the box on a
        # row means. `checked` is "off" / "on" / "part", worked out from the
        # members rather than stored — a group is ticked exactly when its gaps
        # are Closed. Selecting a group for a batch is Cmd-click (see `marked`).
        self.setFlags(Qt.ItemFlag.ItemIsEnabled
                      | Qt.ItemFlag.ItemIsUserCheckable)
        self.setCheckState({
            "on": Qt.CheckState.Checked,
            "part": Qt.CheckState.PartiallyChecked,
        }.get(checked, Qt.CheckState.Unchecked))
        arrow = "▸" if collapsed else "▾"
        label = name or NO_GROUP
        bits = [f"{total} gap" + ("s" if total != 1 else "")]
        if open_n:
            bits.append(f"{open_n} open")
        # Finished is said in words, not implied by a colour: "0 open" reads as
        # missing data, "all done" reads as an outcome.
        if name and total and done_n >= total:
            bits.append("all done ✓")
        if archived:
            bits.append("archived")
        # Said in words as well as shown with a tick — a marker alone reads as
        # decoration.
        if marked:
            bits.append("selected")
        self.setText(f"{arrow}  {label}   ·   " + "   ·   ".join(bits))
        self.setToolTip(
            f"{label} — tick to CLOSE every gap in it (untick to reopen), "
            f"click to {'expand' if collapsed else 'collapse'}, Cmd-click to "
            "select the group for the gap buttons, right-click for group "
            "actions."
        )


class _KGItemDelegate(QStyledItemDelegate):
    """Draws group headers, and gaps that have a notes preview, as two lines.

    Every gap row is drawn here, one line or two. Single-line rows used to fall
    through to the default painter, which adds the theme's item padding that
    this one doesn't — so a gap with notes sat ~10px left of one without, and
    the list looked ragged. Titles are elided to the row, never scrolled to."""

    _PAD = 4

    def _sub_font(self, base: QFont) -> QFont:
        f = QFont(base)
        f.setPointSize(max(8, base.pointSize() - 2) if base.pointSize() > 0
                       else f.pointSize())
        return f

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        # Rows fit the list's width and elide; a width hint as long as the
        # title is what gave the list a sideways scrollbar.
        size.setWidth(1)
        if index.data(_HEADER_ROLE) is not None:
            size.setHeight(size.height() + self._PAD * 2)
        elif index.data(_NOTES_ROLE):
            sub = QFontMetrics(self._sub_font(option.font))
            size.setHeight(size.height() + sub.height())
        return size

    def paint(self, painter, option, index):
        group = index.data(_HEADER_ROLE)
        preview = index.data(_NOTES_ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        # Draw the row chrome (background, alternating colour, selection) with
        # no text, then place the text by hand.
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        fg = (opt.palette.highlightedText().color() if selected
              else opt.palette.text().color())
        custom = index.data(Qt.ItemDataRole.ForegroundRole)
        if custom is not None and not selected:
            try:
                fg = custom.color()
            except AttributeError:
                pass

        # Start the text where the style says text starts, not at a fixed
        # inset: with a tick box on every row, a fixed inset paints the title
        # straight through the indicator.
        text_left = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget).left()
        rect = QRect(text_left, opt.rect.top(),
                     max(0, opt.rect.right() - text_left - self._PAD),
                     opt.rect.height())
        painter.save()
        if group is not None:
            f = QFont(opt.font)
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(fg)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter
                                       | Qt.AlignmentFlag.AlignLeft), text)
            painter.restore()
            return

        title_font = QFont(opt.font)
        title_h = QFontMetrics(title_font).height()
        if not preview:
            painter.setFont(title_font)
            painter.setPen(fg)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter
                                       | Qt.AlignmentFlag.AlignLeft),
                             QFontMetrics(title_font).elidedText(
                                 text, Qt.TextElideMode.ElideRight, rect.width()))
            painter.restore()
            return
        sub_font = self._sub_font(opt.font)
        sub_h = QFontMetrics(sub_font).height()
        top = rect.top() + max(0, (rect.height() - title_h - sub_h) // 2)

        painter.setFont(title_font)
        painter.setPen(fg)
        painter.drawText(QRect(rect.left(), top, rect.width(), title_h),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft),
                         QFontMetrics(title_font).elidedText(
                             text, Qt.TextElideMode.ElideRight, rect.width()))

        sub_colour = QColor(fg)
        sub_colour.setAlpha(150)
        painter.setFont(sub_font)
        painter.setPen(sub_colour)
        painter.drawText(QRect(rect.left(), top + title_h, rect.width(), sub_h),
                         int(Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignLeft),
                         QFontMetrics(sub_font).elidedText(
                             str(preview), Qt.TextElideMode.ElideRight,
                             rect.width()))
        painter.restore()


class _GroupAssignDialog(QDialog):
    """Pick (or type) the group for a selection. Blank means ungroup.

    Shows the tag segment live, because the SRS 4 -> SRS::04 rule is a guess
    about what you meant and you should see it before 40 gaps carry it."""

    def __init__(self, existing: list[str], current: str = "", n: int = 1,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Group knowledge gaps")
        theme_dialog(self)
        root = QVBoxLayout(self)
        label = "1 gap" if n == 1 else f"{n} gaps"
        head = QLabel(f"<b>Put {label} in a group</b>")
        head.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(head)
        blurb = QLabel(
            "A group is the batch a gap came in with — an SRS, a week, a "
            "placement. It's independent of type and status. What it tags is "
            "set on the group itself (Manage groups…): a source for where the "
            "batch came from, and a course branch if it belongs to one."
        )
        blurb.setStyleSheet("color: gray; font-size: 11px;")
        blurb.setWordWrap(True)
        root.addWidget(blurb)

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.addItem("")                       # blank = ungrouped
        for g in existing:
            self.combo.addItem(g)
        self.combo.setCurrentText(current or "")
        self.combo.editTextChanged.connect(self._refresh_hint)
        root.addWidget(self.combo)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: gray; font-size: 11px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_hint()
        self.resize(420, self.sizeHint().height())

    def _refresh_hint(self, *_a) -> None:
        name = self.combo.currentText().strip()
        if not name:
            self._hint.setText("Leave blank to remove these gaps from their group.")
            return
        from ..core.config import source_tag as _source_tag
        esc = _html.escape
        bits = []
        branch = autotag.group_branch(name)
        if branch:
            bits.append(f"<code>{esc(branch)}</code>")
        src = _source_tag(autotag.group_source(name))
        if src:
            bits.append(f"<code>{esc(src)}</code>")
        # Their own content tag either way — saying so stops a group with
        # nothing set looking like a group that does nothing.
        self._hint.setText(
            ("Also tagged:  " + " &nbsp;+&nbsp; ".join(bits)
             + "<br>(each gap keeps its own content tag.)") if bits else
            ("“" + esc(name) + "” has no source or branch set yet — these gaps "
             "keep their own content tags. Set one in Manage groups…"))
        self._hint.setTextFormat(Qt.TextFormat.RichText)

    def value(self) -> str:
        return self.combo.currentText().strip()


class _GroupEditDialog(QDialog):
    """Rename a group, and say the two things about it that can't be worked out.

    It used to ask four. That was right when the group WAS the tag — one branch
    per batch, and every decision about where a card filed had to be made here.
    Under the three axes it isn't:

        content   !!Fleg::KG::MSK::Arthritis::Serology   classified per gap
        course    !!Curtanki::Year_3::SRS::02::KGs       yours, typed
        source    !!Fleg::Sources::Placements::GP        yours, typed
        audit     !!Fleg::Ankisstant::AI::…              written by the tool

    Content classifies itself and the audit tags write themselves, so the only
    questions left are the two nobody can answer for you: where this batch came
    from, and — for course work — which branch of your own tree it belongs to.
    Everything else was a dial asking you to re-decide something already decided.

    SOURCE lives on the group because provenance is a property of the BATCH, not
    of a card. "GP Day 1" has one source; asking per card is how a source axis
    stops being filled in.

    COURSE is optional and is blank by default. A group without one contributes
    no branch of its own — its cards are found through the content tag and the
    source tag, which is what those axes are for. Only work that belongs to a
    tree you already keep (a Curtanki case) needs to name it."""

    # A new course branch takes the gap's topic under it, which is what every
    # course group already set by hand. It is the one thing that keeps a case
    # branch browsable instead of collapsing forty gaps onto one node.
    _DEFAULT_LEVELS = "topic"

    def __init__(self, name: str, n_gaps: int = 0, n_cards: int = 0,
                 existing: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit group")
        theme_dialog(self)
        self._original = name
        self._existing = [g for g in (existing or []) if g != name]

        root = QVBoxLayout(self)
        head = QLabel(f"<b>{_html.escape(name)}</b>")
        head.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(head)
        sub = QLabel(
            f"{n_gaps} gap{'s' if n_gaps != 1 else ''} in this group"
            + (f" · {n_cards} card{'s' if n_cards != 1 else ''} already tagged"
               if n_cards else "")
        )
        sub.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(sub)

        saved = autotag.group_override(name)

        form = QGridLayout()
        form.setColumnStretch(1, 1)
        form.addWidget(QLabel("Name"), 0, 0)
        self._name = QLineEdit(name)
        self._name.setToolTip("What the group is called in the list and the chips.")
        form.addWidget(self._name, 0, 1)

        # THE question about a group, and the reason the others got shorter:
        # is this batch one subject, or just one afternoon? A lecture is one
        # subject and every card off it wants a shared head node. A GP placement
        # is not — the gaps have nothing in common but the day, and a branch
        # over them would say something untrue.
        self._as_group = QCheckBox("Tag every card in this group under one branch")
        self._as_group.setChecked(bool((saved or {}).get("tag_as_group", True)))
        self._as_group.setToolTip(
            "On: every card made from these gaps carries one shared tag, with "
            "the gap's own topic hanging under it —\n"
            "    …::Antiepileptics::Atypical\n"
            "    …::Antiepileptics::Typical\n\n"
            "so “everything from that lecture” is one click in the sidebar.\n\n"
            "Off: for a group that is a container rather than a subject — a "
            "placement, a week of ward work. The cards still get their content "
            "tag and their source tag; they just aren't claimed to be one topic."
        )
        form.addWidget(self._as_group, 1, 0, 1, 2)

        form.addWidget(QLabel("Source"), 2, 0)
        self._source = QLineEdit(saved["source"] if saved else "")
        self._source.setPlaceholderText("e.g. Placements::GP — optional")
        # Source PATHS, not whole tags: the box wants what goes after
        # "Sources::", so a completer offering finished tags would hand back a
        # value that prefixes itself.
        attach_source_completer(self._source)
        self._source.setToolTip(
            "Where this batch came from — a placement, a homework set, a "
            "question bank.\n\n"
            "Short and flat: it says WHERE, never what about. The body system "
            "is already in the content tag, and repeating it here is what put "
            "one fact in two branches.\n\n"
            "Leave it blank for course work — the course branch already says "
            "where it came from."
        )
        form.addWidget(self._source, 2, 1)

        self._seg_label = QLabel("Course")
        form.addWidget(self._seg_label, 3, 0)
        self._seg = QLineEdit(saved["path"] if saved else "")
        self._seg.setPlaceholderText(
            "e.g. !!Curtanki::Year_3::SRS::02::KGs — optional")
        # A full path IS the case you cannot type from memory — this completer
        # is reading the tags that actually exist in the collection.
        attach_tag_completer(self._seg, multi=False)
        self._seg.setToolTip(
            "The branch of your OWN tag tree this batch belongs to — a "
            "Curtanki case, a block.\n\n"
            "Typed in full and used verbatim: the auto-tag base is not put in "
            "front of it. That is how a group joins a tree you already had.\n\n"
            "Blank for anything that isn't course work. The gaps still get "
            "their content tag and their source tag either way."
        )
        form.addWidget(self._seg, 3, 1)

        # Only for a group with no course branch: WHERE inside the content tree
        # its head node sits. Blank is the ordinary answer — the group's name
        # becomes the segment — and this exists for the batch whose name reads
        # badly as a tag ("Nirmala Anti epileptics" -> "Pharm::Antiepileptics").
        self._gseg_label = QLabel("Group tag")
        form.addWidget(self._gseg_label, 4, 0)
        self._gseg = QLineEdit(
            "" if (saved or {}).get("absolute") else ((saved or {}).get("path") or ""))
        self._gseg.setToolTip(
            "The head node this batch shares, inside your auto-tag base.\n\n"
            "Leave it blank and the group's own name is used, cleaned up for a "
            "tag. Type one when the name reads badly as a tag, or when the "
            "batch belongs under something you already keep."
        )
        form.addWidget(self._gseg, 4, 1)

        self._levels_label = QLabel("Under it")
        form.addWidget(self._levels_label, 5, 0)
        self._levels = QComboBox()
        for key in ("full", "sub", "topic", "none"):
            self._levels.addItem(autotag.GROUP_LEVEL_LABELS[key], key)
        want = (autotag.normalise_levels(saved["levels"]) if saved
                else self._DEFAULT_LEVELS)
        ix = self._levels.findData(want)
        self._levels.setCurrentIndex(ix if ix >= 0 else 0)
        self._levels.setToolTip(
            "How much of the gap's classification goes under the course "
            "branch. It does NOT affect the content tag, which always carries "
            "all three levels.\n\n"
            "“Topic only” is the usual answer: a case branch is already as "
            "narrow as a system, so repeating the system under it makes a "
            "duplicate of your content tree — but dropping the topic too puts "
            "every gap in the case on one node with nothing to tell them apart."
        )
        form.addWidget(self._levels, 5, 1)
        root.addLayout(form)

        self._preview = QLabel("")
        self._preview.setStyleSheet("color: gray; font-size: 11px;")
        self._preview.setWordWrap(True)
        self._preview.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self._preview)

        self._warn = QLabel("")
        self._warn.setStyleSheet("color: #b45309; font-size: 11px;")
        self._warn.setWordWrap(True)
        root.addWidget(self._warn)

        self._name.textChanged.connect(self._refresh_preview)
        self._seg.textChanged.connect(self._refresh_preview)
        self._gseg.textChanged.connect(self._refresh_preview)
        self._source.textChanged.connect(self._refresh_preview)
        self._as_group.toggled.connect(self._refresh_preview)
        self._levels.currentIndexChanged.connect(self._refresh_preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_preview()
        self.resize(480, self.sizeHint().height())

    def _levels_mode(self) -> str:
        return str(self._levels.currentData() or "full")

    def _tail(self) -> str:
        """The example tail shown in the preview, so the shape of the finished
        tag is on screen rather than described."""
        return {"full": "::System::Subsystem::Topic",
                "sub": "::Subsystem::Topic",
                "topic": "::Topic",
                "none": ""}[self._levels_mode()]

    def _refresh_preview(self, *_a) -> None:
        from ..core.config import source_tag as _source_tag
        name = self._name.text().strip()
        typed = self._seg.text().strip()
        path = autotag.clean_segment_path(typed)
        typed_src = self._source.text().strip()
        src_tag = _source_tag(typed_src)
        base = (auto_tag_base() or "").rstrip(":")
        esc = _html.escape

        as_group = self._as_group.isChecked()
        has_course = bool(path)
        # The group's head node when it has no course branch of its own: what
        # was typed, else the name cleaned up into a tag segment.
        rel_seg = (autotag.clean_segment_path(self._gseg.text().strip())
                   or autotag.derive_group_segment(name))
        group_tag = ""
        if as_group:
            group_tag = (path + self._tail()) if has_course else (
                f"{base}::KG::{rel_seg}{self._tail()}" if (base and rel_seg) else "")

        # The depth dial shapes the group's branch, so it is on screen only when
        # there is one. Hidden rather than disabled: a greyed control reads as
        # "broken", and this one simply has nothing to apply to. Same for the
        # segment box, which a course branch makes irrelevant — the course
        # branch IS the head node then.
        self._levels_label.setVisible(bool(group_tag))
        self._levels.setVisible(bool(group_tag))
        self._gseg_label.setVisible(as_group and not has_course)
        self._gseg.setVisible(as_group and not has_course)
        # A course branch IS tagging the batch as one group, so it has nothing
        # to say when the box above is unticked. Hidden rather than left there
        # to be filled in and quietly ignored.
        self._seg_label.setVisible(as_group)
        self._seg.setVisible(as_group)
        self._gseg.setPlaceholderText(
            autotag.derive_group_segment(name) or "e.g. Pharm::Antiepileptics")

        # Every tag a card in this group earns, on one screen and in commit
        # order. The point of showing all of them — including the two nobody
        # sets here — is that the two you DO set stop looking like the whole
        # answer.
        lines = []
        content = f"{base}::KG::System::Subsystem::Topic" if base else ""
        if content:
            lines.append(f"<code>{esc(content)}</code> &nbsp;— "
                         "classified per gap, automatic")
        if group_tag:
            lines.append(f"<code>{esc(group_tag)}</code> &nbsp;— "
                         + ("this group's course branch" if has_course
                            else "this group's shared branch"))
        if src_tag:
            lines.append(f"<code>{esc(src_tag)}</code> &nbsp;— source")
        if not group_tag and not src_tag:
            lines.append("<i>no source and no shared branch — these gaps file "
                         "by content alone</i>")
        if not base and not has_course:
            lines = ["No auto-tag base is set, so nothing is tagged yet. "
                     "Give the group a course branch to tag without one."]
        self._preview.setText(
            ("Every card in this group is tagged:<br>&nbsp;&nbsp;"
             + "<br>&nbsp;&nbsp;".join(lines)) if lines else "")

        msgs = []
        if not name:
            msgs.append("A group needs a name.")
        elif name != self._original and name in self._existing:
            msgs.append(
                f"“{name}” already exists — saving merges this group into it.")
        if typed and not path:
            msgs.append("That branch has nothing usable in it.")
        if typed_src and not src_tag:
            msgs.append("That source has nothing usable in it."
                        if base else
                        "A source needs an auto-tag base to hang off.")
        self._warn.setText("  ".join(msgs))
        if self._ok is not None:
            self._ok.setEnabled(bool(name) and not (typed and not path))

    def values(self) -> tuple[str, dict | None]:
        """(name, override) — None when the group says nothing beyond its name.

        A typed COURSE branch is always absolute — it is a branch of a tree you
        already keep, and the auto-tag base is not put in front of it.

        Without one, a group that tags as a group gets a RELATIVE segment: a
        head node inside the content tree with the gap's own classification
        under it. That form was dropped from this dialog once, on the grounds
        that it put a batch where a body system goes. It is back because the
        alternative turned out to be worse: every group without a Curtanki case
        behind it produced cards with no shared tag at all, so "everything from
        that lecture" had no answer. The checkbox is what makes it honest —
        a batch says it is one subject, rather than being assumed to be one."""
        as_group = self._as_group.isChecked()
        # Both path boxes are hidden when the group doesn't tag as one, so
        # whatever they still hold is stale and must not be stored — a saved
        # branch nobody can see is how an unticked group comes back tagging.
        path = (autotag.clean_segment_path(self._seg.text().strip())
                if as_group else "")
        source = autotag.clean_source_path(self._source.text().strip())
        name = self._name.text().strip()
        if not path and as_group:
            # Blank means "derive it from the name", and it is stored blank so
            # renaming the group keeps moving the tag with it.
            path = autotag.clean_segment_path(self._gseg.text().strip())
        if not path and not source and as_group:
            return name, None          # nothing said beyond the default
        return name, {"path": path,
                      "absolute": bool(as_group and autotag.clean_segment_path(
                          self._seg.text().strip())),
                      "levels": self._levels_mode(),
                      "source": source,
                      "tag_as_group": as_group}


class _GroupManagerDialog(QDialog):
    """Every group that has ever existed, finished ones included.

    The list page shows what you are working on now; this is the other half —
    where a finished batch goes to be looked up, renamed, retagged or put away
    without deleting a single gap. Archiving is the only state it adds, and it
    is pure UI state: an archived group's gaps are exactly where they were."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel
        self.setWindowTitle("Groups")
        theme_dialog(self)
        root = QVBoxLayout(self)
        blurb = QLabel(
            "Every group, including finished and archived ones. Archiving hides "
            "a group from the list page — it never touches its gaps or its cards."
        )
        blurb.setStyleSheet("color: gray; font-size: 11px;")
        blurb.setWordWrap(True)
        root.addWidget(blurb)

        self._show_archived = QCheckBox("Show archived")
        self._show_archived.setChecked(True)
        self._show_archived.toggled.connect(self._reload)
        root.addWidget(self._show_archived)

        # One column per axis the group SETS. They used to share a column
        # called "Tag segment", which is how a source ended up saved as a
        # course branch: the two are different answers and they now look it.
        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["Group", "Course branch", "Source", "Gaps", "Open", "Closed", "State"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # One line per cell, cut from the LEFT. Tables word-wrap by default,
        # and Qt breaks a tag after its "!!" — so a one-line row showed "!!…",
        # the one part every tag shares. The end is the part that differs.
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideLeft)
        hh.setStretchLastSection(False)
        for c in (3, 4, 5, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._refresh_buttons)
        self._table.itemDoubleClicked.connect(lambda *_a: self._open_in_list())
        root.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._open_btn = QPushButton("Open in list")
        self._open_btn.setAutoDefault(False)
        self._open_btn.setToolTip(
            "Filter the Knowledge Gaps list to this group. Works for archived "
            "groups too — opening one shows it for as long as the filter is on."
        )
        self._open_btn.clicked.connect(self._open_in_list)
        self._edit_btn = QPushButton("Rename / edit tag…")
        self._edit_btn.setAutoDefault(False)
        self._edit_btn.clicked.connect(self._edit)
        self._retag_btn = QPushButton("Retag…")
        self._retag_btn.setAutoDefault(False)
        self._retag_btn.setToolTip(
            "Rebuild every tag in this group from its current name and segment.")
        self._retag_btn.clicked.connect(self._retag)
        self._archive_btn = QPushButton("Archive")
        self._archive_btn.setAutoDefault(False)
        self._archive_btn.clicked.connect(self._toggle_archive)
        self._delete_btn = QPushButton("Delete…")
        self._delete_btn.setAutoDefault(False)
        self._delete_btn.setToolTip(
            "Remove this group. You choose whether its gaps go with it — "
            "cards already in your collection are never touched.")
        self._delete_btn.clicked.connect(self._delete)
        for b in (self._open_btn, self._edit_btn, self._retag_btn,
                  self._archive_btn, self._delete_btn):
            row.addWidget(b)
        row.addStretch(1)
        sweep = QPushButton("Forget empty groups")
        sweep.setAutoDefault(False)
        sweep.setToolTip(
            "Clear out groups with no gaps left, and the saved tag settings of "
            "groups that no longer exist — which is what makes an old name "
            "come back with its old tag branch attached.")
        sweep.clicked.connect(self._delete_empty)
        row.addWidget(sweep)
        close = QPushButton("Close")
        close.setAutoDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

        self._rows: list[str] = []
        self._reload()
        self.resize(680, 420)

    # ── data ─────────────────────────────────────────────────────────────────

    def _reload(self, *_a) -> None:
        stats = self._panel._group_stats()
        archived = set(self._panel._archived_groups())
        # Groups that exist only in config — every gap gone, but an archive
        # flag or a tag override still saved. They are invisible everywhere
        # else, which is exactly why the one screen that manages groups has to
        # show them: otherwise there is no way to get rid of them.
        try:
            cfg = tool_config("knowledge_gaps")
            ghosts = (set(str(g) for g in (cfg.get("archived_groups") or []))
                      | set(str(g) for g in
                            (cfg.get("group_tag_overrides") or {}))) - set(stats)
        except Exception:
            ghosts = set()
        for g in ghosts:
            stats[g] = {"total": 0, "open": 0, "in_progress": 0, "done": 0,
                        "last": ""}
        names = list(stats.keys())
        show_arch = self._show_archived.isChecked()
        if not show_arch:
            names = [n for n in names if n not in archived]

        def bucket(n: str) -> int:
            st = stats[n]
            if n in archived:
                return 2
            return 1 if st["done"] >= st["total"] and st["total"] else 0

        names.sort(key=lambda n: (bucket(n), kg_store.group_sort_key(n)))
        self._rows = names
        self._table.setRowCount(len(names))
        for r, n in enumerate(names):
            st = stats[n]
            finished = bool(st["total"]) and st["done"] >= st["total"]
            state = "Archived" if n in archived else (
                "Finished" if finished else "Active")
            if n in archived and finished:
                state = "Archived · finished"
            if not st["total"]:
                # In words, not by being greyed out: an empty row that merely
                # looks faint reads as "disabled", not as "this has no gaps".
                state = "Empty — no gaps left"
            ov = autotag.group_override(n)
            plan = autotag.group_tag_plan(n)
            course = autotag.group_branch(n, plan)
            source = (ov or {}).get("source", "")
            cells = [n, course, source, str(st["total"]),
                     str(st["open"] + st["in_progress"]), str(st["done"]), state]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == 1:
                    if course:
                        depth = autotag.GROUP_LEVEL_LABELS.get(
                            plan["levels"], plan["levels"])
                        it.setToolTip(
                            ("Its own branch — tagged outside the auto-tag base."
                             if plan.get("absolute") else
                             "A shared head node inside the auto-tag base.")
                            + f"\nUnder it: {depth}")
                    else:
                        it.setText("—")
                        it.setToolTip(
                            "Not tagged as one group — its gaps file by "
                            "content and source only. Open the group to turn "
                            "the shared branch on.")
                if c == 2:
                    if source:
                        from ..core.config import source_tag as _st
                        it.setToolTip(f"Tagged {_st(source) or source}")
                    else:
                        it.setText("—")
                        it.setToolTip("No source set. Open the group to say "
                                      "where this batch came from.")
                if c == 0:
                    it.setToolTip(
                        f"Last touched: {st['last'][:10] or 'unknown'}")
                self._table.setItem(r, c, it)
        if names:
            self._table.selectRow(0)
        self._refresh_buttons()

    def _selected(self) -> str:
        r = self._table.currentRow()
        if r < 0 or r >= len(self._rows):
            return ""
        return self._rows[r]

    def _refresh_buttons(self) -> None:
        name = self._selected()
        on = bool(name)
        for b in (self._open_btn, self._edit_btn, self._retag_btn,
                  self._archive_btn, self._delete_btn):
            b.setEnabled(on)
        archived = name and name in set(self._panel._archived_groups())
        self._archive_btn.setText("Restore" if archived else "Archive")
        self._archive_btn.setToolTip(
            "Bring this group back onto the list page." if archived else
            "Hide this group from the list page. Its gaps and cards are untouched."
        )

    # ── actions ──────────────────────────────────────────────────────────────

    def _delete(self) -> None:
        """Delete a group. Two very different things go by that name.

        Removing the label and removing the work are separate decisions, and a
        single "Delete" that quietly did both would be the most destructive
        button on the page. So it asks which, states the count for each, and
        says plainly what it does NOT touch: the Anki notes. Cards already
        tagged under this group's branch stay tagged — deleting a label here
        must not reach into the collection."""
        name = self._selected()
        if not name:
            return
        ids = [k["id"] for k in kg_store.load_all()
               if str(k.get("group") or "").strip() == name and k.get("id")]
        seg = autotag.group_segment(name)

        box = QMessageBox(self)
        box.setWindowTitle("Delete group")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Delete the group “{name}”?")
        box.setInformativeText(
            (f"It holds {len(ids)} gap(s).\n\n" if ids else "It holds no gaps.\n\n")
            + "Cards already in your collection are NOT touched either way — "
            f"anything tagged under {seg} keeps that tag. Remove it in the "
            "Anki browser if you want it gone.")
        just = box.addButton("Delete the group only",
                             QMessageBox.ButtonRole.AcceptRole)
        both = None
        if ids:
            both = box.addButton(f"Delete the group and its {len(ids)} gap(s)",
                                 QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        just.setToolTip("The gaps stay exactly where they are, without a group.")
        if both is not None:
            both.setToolTip("The gaps are removed from Ankisstant. This can't "
                            "be undone.")
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel or clicked is None:
            return

        if both is not None and clicked is both:
            # Second confirm, because this is the one that destroys work and
            # the first dialog's job was choosing BETWEEN two actions — a
            # destructive choice shouldn't ride in on a decision about which.
            if not askUser(f"Really delete {len(ids)} gap(s) in “{name}”?\n\n"
                           "Their notes, resources and history go with them. "
                           "This can't be undone.", defaultno=True):
                return
            try:
                removed = kg_store.remove_many(ids)
            except Exception as e:
                showWarning(f"Couldn't delete those gaps: {e}")
                return
            what = f"Deleted “{name}” and {removed} gap(s)."
        else:
            try:
                for kid in ids:
                    kg_store.update(kid, group="")
            except Exception as e:
                showWarning(f"Couldn't ungroup those gaps: {e}")
                return
            what = (f"Deleted “{name}”. {len(ids)} gap(s) kept, now ungrouped."
                    if ids else f"Deleted “{name}”.")

        # The label is gone, so its UI state and tag override are dead weight —
        # and a stale override would silently apply again to a NEW group that
        # happened to reuse the name.
        self._forget_group(name)
        tooltip(what, period=5000)
        self._reload()
        self._panel.refresh_list()

    def _forget_group(self, name: str) -> None:
        try:
            cfg = tool_config("knowledge_gaps")
            for key in ("collapsed_groups", "archived_groups"):
                cfg[key] = [g for g in (cfg.get(key) or []) if str(g) != name]
            overrides = dict(cfg.get("group_tag_overrides") or {})
            overrides.pop(name, None)
            cfg["group_tag_overrides"] = overrides
            save_tool_config("knowledge_gaps", cfg)
        except Exception as e:
            print(f"[ankisstant] couldn't forget group {name!r}: {e}")

    def _delete_empty(self) -> None:
        """Sweep up labels with nothing left in them.

        A group only exists because a gap says it does, so an empty one is
        already invisible on the list page — but its tag override and archive
        state live on in config, and that is what makes a name you deleted
        months ago come back with its old tag branch attached."""
        stats = self._panel._group_stats()
        empty = sorted(n for n, st in stats.items() if not st["total"])
        cfg = tool_config("knowledge_gaps")
        orphaned = sorted(
            set(str(g) for g in (cfg.get("archived_groups") or []))
            | set(str(g) for g in (cfg.get("group_tag_overrides") or {}))
            - set(stats))
        dead = sorted(set(empty) | set(orphaned))
        if not dead:
            tooltip("No empty groups — every group still has gaps in it.",
                    period=4000)
            return
        shown = "\n".join(f"  • {n}" for n in dead[:15])
        more = f"\n  … and {len(dead) - 15} more" if len(dead) > 15 else ""
        if not askUser(f"Forget {len(dead)} empty group(s)?\n\n{shown}{more}\n\n"
                       "These have no gaps left. Their saved tag settings and "
                       "archive state are removed. No gaps and no cards are "
                       "touched.", defaultno=True):
            return
        for n in dead:
            self._forget_group(n)
        tooltip(f"Forgot {len(dead)} empty group(s).", period=4000)
        self._reload()
        self._panel.refresh_list()


    def _open_in_list(self) -> None:
        name = self._selected()
        if not name:
            return
        self.accept()
        self._panel.focus_group(name)

    def _edit(self) -> None:
        name = self._selected()
        if name and self._panel._rename_group(name):
            self._reload()

    def _retag(self) -> None:
        name = self._selected()
        if name:
            self._panel._retag_group(name)
            self._reload()

    def _toggle_archive(self) -> None:
        name = self._selected()
        if not name:
            return
        archived = name in set(self._panel._archived_groups())
        self._panel._set_group_archived(name, not archived)
        self._reload()


class _KGListItem(QListWidgetItem):
    """One gap, with the tick box that closes it.

    ONE CHECKBOX, ONE MEANING: ticked means done with. It used to be the
    SELECTION — what the buttons acted on — while the real done-mark was a
    second checkbox labelled "Closed" in the detail pane, and a gap finished by
    a work-through pass went grey without its box ever being ticked. Nobody
    reads a to-do list that way: a tick box beside an item is whether the item
    is done. Selecting a batch is what highlighting the rows is for."""

    def __init__(self, kg: dict):
        super().__init__()
        self.kg_id = kg.get("id", "")
        self.setFlags(self.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.setCheckState(Qt.CheckState.Unchecked)
        self.refresh(kg)

    def refresh(self, kg: dict) -> None:
        from aqt.qt import QBrush, QColor, QFont
        source_label = SOURCE_LABELS.get(kg.get("source", "manual"), "?")
        type_meta = _type_meta(kg.get("type", "kg"))
        status = kg.get("status", "open")
        status_label = STATUS_LABELS.get(status, "Open")
        title = kg.get("title", "(untitled)") or "(untitled)"
        # Prefix the title with a compact type tag for at-a-glance scanning.
        text = f"[{type_meta['name']}]  {title}"
        if len(text) > 80:
            text = text[:80].rstrip() + "…"
        self.setText(text)

        # Second line, drawn by _KGItemDelegate. Empty means the row stays a
        # single line, so nothing gets taller for gaps with nothing to show.
        try:
            show_notes = bool(tool_config("knowledge_gaps")
                              .get("show_notes_in_list", True))
        except Exception:
            show_notes = True
        self.setData(_NOTES_ROLE, _notes_preview(kg) if show_notes else "")

        # The box is the status, not a selection. Set before the grey so both
        # say the same thing about the same row.
        want = (Qt.CheckState.Checked if status == "done"
                else Qt.CheckState.Unchecked)
        if self.checkState() != want:
            self.setCheckState(want)

        # Grey done items so they stay visible but recede.
        if status == "done":
            brush = QBrush(QColor(150, 150, 150))
            self.setForeground(brush)
            f = QFont(self.font())
            f.setItalic(True)
            self.setFont(f)
        else:
            # Reset to default if a previously-done item flipped back to open.
            self.setData(Qt.ItemDataRole.ForegroundRole, None)
            self.setFont(QFont())

        tt_lines = [
            f"Type: {type_meta['name']}" + (f" — {type_meta['description']}"
                                            if type_meta.get('description') else ""),
            f"Source: {source_label}",
            f"Status: {status_label}",
        ]
        group = str(kg.get("group") or "").strip()
        if group:
            tt_lines.append(f"Group: {group}  →  {autotag.group_segment(group)}")
        levels = _format_levels(kg)
        if levels:
            tt_lines.append(f"Path: {levels}")
        tt_lines.append(f"Added: {_short_date(kg.get('created_at', ''))}")
        self.setToolTip("\n".join(tt_lines))


# ── LO analyser (inline section, only shown on type=lo KGs) ─────────────────

class _LOAnalyserSection(QGroupBox):
    """Inline Analyse-LO panel — appears on the KG detail pane for LO-type
    KGs. Reads the LO text + tag from the KG's own schema fields, runs the
    gap analysis, and appends accepted gaps into the KG's notes field."""

    def __init__(self, detail_pane):
        super().__init__("🔬 Analyse this LO")
        self._pane = detail_pane
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        hint = QLabel(
            "Pulls cards under this LO's tag and flags concepts the cards "
            "don't cover. Accepted gaps are appended to this LO's <b>Notes</b>."
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        btn_row = QHBoxLayout()
        self._count_btn = QPushButton("Count cards")
        self._count_btn.setAutoDefault(False)
        self._count_btn.clicked.connect(self._on_count)
        btn_row.addWidget(self._count_btn)
        self._analyse_btn = QPushButton("🔍 Analyse Gaps")
        self._analyse_btn.setAutoDefault(False)
        self._analyse_btn.clicked.connect(self._on_analyse)
        btn_row.addWidget(self._analyse_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        self._gaps_box = QGroupBox("Suggested gaps")
        gv = QVBoxLayout(self._gaps_box)
        gv.setContentsMargins(8, 6, 8, 6)
        gv.setSpacing(4)
        self._gaps_list = QListWidget()
        self._gaps_list.setAlternatingRowColors(True)
        self._gaps_list.setMaximumHeight(180)
        gv.addWidget(self._gaps_list, 1)
        ctrl_row = QHBoxLayout()
        sa = QPushButton("Select all")
        sa.setAutoDefault(False)
        sa.clicked.connect(self._select_all)
        sn = QPushButton("Select none")
        sn.setAutoDefault(False)
        sn.clicked.connect(self._select_none)
        ctrl_row.addWidget(sa)
        ctrl_row.addWidget(sn)
        ctrl_row.addStretch(1)
        self._append_btn = QPushButton("Append checked → Notes")
        self._append_btn.setAutoDefault(False)
        self._append_btn.clicked.connect(self._append_to_notes)
        ctrl_row.addWidget(self._append_btn)
        gv.addLayout(ctrl_row)
        self._gaps_box.setVisible(False)
        v.addWidget(self._gaps_box)

    def reset(self) -> None:
        """Wipe transient state — called when the selected KG changes."""
        self._gaps_list.clear()
        self._gaps_box.setVisible(False)
        self._status.setText("")

    # ── inputs ───────────────────────────────────────────────────────────────

    def _current_lo_and_tag(self) -> tuple[str, str]:
        widgets = self._pane._schema_widgets
        lo = ""
        tag = ""
        if "lo" in widgets:
            try:
                lo = widgets["lo"]["getter"]()
            except Exception:
                pass
        if "lo_tag" in widgets:
            try:
                tag = widgets["lo_tag"]["getter"]()
            except Exception:
                pass
        # Fall back to KG title if there's no `lo` field on the schema.
        if not lo and self._pane._current:
            lo = self._pane._current.get("title", "")
        return lo.strip(), tag.strip()

    # ── helpers (re-use gap_analyser's prompt + tag query) ───────────────────

    def _config(self) -> dict:
        return tool_config("gap_analyser")

    # ── actions ──────────────────────────────────────────────────────────────

    def _on_count(self) -> None:
        if not anki_utils.require_col():
            return
        _, tag = self._current_lo_and_tag()
        if not tag:
            tooltip("This LO has no tag set yet — fill the LO tag field above.")
            return
        from .gap_analyser import _build_tag_query
        cfg = self._config()
        notetype_filter = (cfg.get("notetype_filter") or "").strip()
        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        suffix = f" (notetype filter: {notetype_filter})" if notetype_filter else ""
        self._status.setText(f"{len(nids)} note(s) under '{tag}'{suffix}")

    def _on_analyse(self) -> None:
        if not anki_utils.require_col():
            return
        lo, tag = self._current_lo_and_tag()
        if not lo or not tag:
            tooltip("Set both the LO text and the LO tag on this KG first.")
            return
        from .gap_analyser import GAP_SYSTEM_TMPL, _build_tag_query, _front_preview
        cfg = self._config()
        notetype_filter = (cfg.get("notetype_filter") or "").strip()
        front_field = cfg.get("front_field", "Text")
        max_cards = int(cfg.get("max_cards", 80))
        try:
            nids = list(mw.col.find_notes(_build_tag_query(tag, notetype_filter)))
        except Exception as e:
            showWarning(f"Couldn't search tag: {e}")
            return
        if not nids:
            self._status.setText(f"No notes under tag '{tag}' — nothing to analyse.")
            return
        if len(nids) > max_cards:
            if not askUser(
                f"{len(nids)} cards under '{tag}' — that's more than the cap "
                f"({max_cards}). Only the first {max_cards} will be sent. Continue?"
            ):
                return
            nids = nids[:max_cards]

        fronts: list[str] = []
        for nid in nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            p = _front_preview(note, front_field)
            if p:
                fronts.append(p)
        if not fronts:
            showWarning("Couldn't read the front field on any card under that tag.")
            return

        max_n = int(cfg.get("max_gaps", 10))
        system = GAP_SYSTEM_TMPL.format(max_n=max_n)
        user_msg = (
            "Learning objective:\n" + lo + "\n\n"
            f"Cards currently tagged for this LO ({len(fronts)}):\n"
            + "\n".join(f"- {c}" for c in fronts)
        )
        self._status.setText(
            f"Asking AI what's missing from {len(fronts)} card(s) under '{tag}'…"
        )
        model = pipeline.dial_for(pipeline.READ)
        gaps = run_claude_json(
            self._analyse_btn, "Analysing…",
            prompt=user_msg, system=system, max_tokens=1024, model=model,
        )

        if not isinstance(gaps, list):
            self._status.setText("")
            return
        gaps = [g.strip() for g in gaps if isinstance(g, str) and g.strip()]
        if not gaps:
            self._gaps_box.setVisible(False)
            self._status.setText(
                f"No gaps found — your cards under '{tag}' appear to cover this LO well."
            )
            return

        self._gaps_list.clear()
        for g in gaps:
            item = QListWidgetItem(g)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self._gaps_list.addItem(item)
        self._gaps_box.setVisible(True)
        self._status.setText(
            f"AI found {len(gaps)} gap(s) across {len(fronts)} card(s)."
        )

    def _select_all(self) -> None:
        for i in range(self._gaps_list.count()):
            self._gaps_list.item(i).setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self._gaps_list.count()):
            self._gaps_list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _append_to_notes(self) -> None:
        if self._pane._current is None:
            return
        approved: list[str] = []
        for i in range(self._gaps_list.count()):
            it = self._gaps_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                t = it.text().strip()
                if t:
                    approved.append(t)
        if not approved:
            tooltip("Nothing checked.")
            return
        notes_widget = self._pane._schema_widgets.get("notes")
        if notes_widget is None:
            showWarning(
                "This LO type doesn't have a 'notes' field. Add one in "
                "Settings → Knowledge Gaps → edit the LO type."
            )
            return
        try:
            current = notes_widget["getter"]() or ""
        except Exception:
            current = ""
        bullets = "\n".join(f"- {g}" for g in approved)
        new_val = (current.rstrip() + "\n\n" + bullets).strip() if current.strip() else bullets
        try:
            notes_widget["setter"](new_val)
        except Exception as e:
            showWarning(f"Couldn't write to notes: {e}")
            return
        # Persist so the appended gaps survive a navigate-away.
        self._pane._save()
        self._gaps_list.clear()
        self._gaps_box.setVisible(False)
        self._status.setText(f"Appended {len(approved)} gap(s) to this LO's notes.")


# ── Detail pane ──────────────────────────────────────────────────────────────

class KGDetailPane(QWidget):
    """Right column — shows / edits / actions a single KG.

    `compact` is the Groups page's copy of it, sitting in the bottom half of a
    vertical split under the group it belongs to. Same pane, same actions, same
    saving — with the sections that don't fit a half-height panel dropped: tags
    (the group's own tag branch is what matters there, and it's shown above),
    resources, and the LO analyser, all of which stay on the Gaps page."""

    def __init__(self, parent_panel, parent=None, *, compact: bool = False,
                 selection_provider=None):
        super().__init__(parent)
        self._parent_panel = parent_panel
        self._current: dict | None = None
        self._compact = compact
        # Whose selection the send buttons act on. The Gaps page's pane means
        # the gap list; the Groups page's pane means the gap list inside the
        # group — passing it in is what stops one pane acting on the other
        # page's selection.
        self._selection_provider = selection_provider
        self._build()
        self._apply_compact()

    def _apply_compact(self) -> None:
        """Hide what a half-height pane has no room for. Called after every
        load() too, because load() shows the LO analyser for an LO gap."""
        if not self._compact:
            return
        for w in (self._tag_row_w, self._res_box, self._lo_analyser):
            try:
                w.setVisible(False)
            except Exception:
                pass

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 4, 4)
        root.setSpacing(6)

        # Header row
        self._title_input = QLineEdit()
        self._title_input.setPlaceholderText("Title — what you don't know")
        f = self._title_input.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self._title_input.setFont(f)
        # Auto-save on focus-out / Enter. Hitting Save is for the whole record;
        # the title is the field people edit and then click straight away from.
        self._title_input.editingFinished.connect(self._commit_edit)
        root.addWidget(self._title_input)

        meta_row = QHBoxLayout()
        # These two labels carry the selection count when there is one. The
        # controls under them act on the whole selection, and the ONLY thing
        # that used to say so was the send buttons further down — which say it
        # about themselves, not about these.
        self._type_lbl = QLabel("Type:")
        meta_row.addWidget(self._type_lbl)
        self._type = QComboBox()
        self._rebuild_type_combo()
        meta_row.addWidget(self._type)
        meta_row.addSpacing(8)
        # A WORD, NOT A SECOND TICK BOX. This used to be a "Closed" checkbox,
        # inches from the tick box on every row of the list — two checkboxes
        # for what a reader takes to be one question ("am I done with this?"),
        # and they meant different things. The row's box is the answer now (see
        # _KGListItem.refresh); this only reports it, so the pane can still be
        # read on its own.
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("font-size: 11px;")
        self._status_lbl.setToolTip(
            "Open or Closed. Tick the box beside the gap in the list to close "
            "it — a card written, or a card found that already covers it. "
            "Untick to put it back on the list."
        )
        meta_row.addWidget(self._status_lbl)
        meta_row.addSpacing(8)
        # Group. A button rather than a combo: it opens the same dialog the
        # list's right-click uses, so there is exactly one place the SRS 4 ->
        # SRS::04 rule is shown and exactly one code path that assigns.
        self._group_btn = QPushButton("Group: —")
        self._group_btn.setAutoDefault(False)
        self._group_btn.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        self._group_btn.setToolTip(
            "The batch this gap belongs to. Adds one level to its auto-tag."
        )
        self._group_btn.clicked.connect(self._on_group_clicked)
        meta_row.addWidget(self._group_btn)
        meta_row.addStretch(1)
        root.addLayout(meta_row)
        # Where it came from and when, on a line of its own: on the same row as
        # the type and group they pushed the pane wider than the window and
        # "added …" was cut off at the edge.
        origin_row = QHBoxLayout()
        self._source_lbl = QLabel("")
        self._source_lbl.setStyleSheet("color: gray; font-size: 11px;")
        origin_row.addWidget(self._source_lbl)
        origin_row.addSpacing(8)
        self._created_lbl = QLabel("")
        self._created_lbl.setStyleSheet("color: gray; font-size: 11px;")
        origin_row.addWidget(self._created_lbl)
        origin_row.addStretch(1)
        root.addLayout(origin_row)

        # Tags
        self._tag_row_w = QWidget()
        tag_row = QHBoxLayout(self._tag_row_w)
        tag_row.setContentsMargins(0, 0, 0, 0)
        tag_row.addWidget(QLabel("Tags:"))
        self._tags_input = QLineEdit()
        self._tags_input.setPlaceholderText("space-separated — School::Year3::Cardio")
        attach_tag_completer(self._tags_input, multi=True)
        self._tags_input.editingFinished.connect(self._commit_edit)
        tag_row.addWidget(self._tags_input, 1)
        root.addWidget(self._tag_row_w)

        # ── Dynamic schema-driven fields container ───────────────────────────
        # Built from the active type's `fields` list. Each entry yields a
        # widget keyed by its field key; values round-trip through kg["fields"].
        self._schema_container = QWidget()
        self._schema_layout = QVBoxLayout(self._schema_container)
        self._schema_layout.setContentsMargins(0, 0, 0, 0)
        self._schema_layout.setSpacing(6)
        self._schema_widgets: dict[str, dict] = {}  # key -> {widget, kind, getter, setter}
        root.addWidget(self._schema_container)
        # Rebuild whenever the type combo changes (preserves overlapping keys).
        self._type.currentIndexChanged.connect(self._on_type_changed)

        # Resources
        self._res_box = QGroupBox("Resources")
        rl = QVBoxLayout(self._res_box)
        rl.setContentsMargins(8, 6, 8, 6)
        rl.setSpacing(2)
        self._res_inner = QWidget()
        self._res_layout = QVBoxLayout(self._res_inner)
        self._res_layout.setContentsMargins(0, 0, 0, 0)
        self._res_layout.setSpacing(2)
        rl.addWidget(self._res_inner)
        add_res_btn = QPushButton("＋ Add resource")
        add_res_btn.setAutoDefault(False)
        add_res_btn.clicked.connect(self._add_resource_row)
        rl.addWidget(add_res_btn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._res_box)

        # LO analyser — only shown when the active KG's type is "lo".
        self._lo_analyser = _LOAnalyserSection(self)
        self._lo_analyser.setVisible(False)
        root.addWidget(self._lo_analyser)

        # Actions
        self._actions_w = QWidget()
        actions_row = FlowLayout(self._actions_w)
        # Labels are rewritten by refresh_send_labels() to name the selection
        # size, because these buttons act on every selected gap, not just the
        # one on screen.
        # One button, one verb.
        #
        # The queue IS search-and-create: Find & write looks for the gap in
        # your deck and writes cards for whatever nothing covered. Find-only or
        # write-only is a per-gap tick on the Find & write plan page, so there
        # is no separate AI Browse / AI Create door any more.
        self._work_btn = QPushButton("Send to queue")
        self._work_btn.setAutoDefault(False)
        self._work_btn.setToolTip(
            "Park this on the queue. It waits there — send as many as you "
            "like, from either page — until you press ▶ Run queue in the "
            "bottom right, which finds and writes cards for the lot in one "
            "pass, then empties.\n\nRight-click a gap to take it back off "
            "the queue.")
        self._work_btn.clicked.connect(self._queue_work_through)
        actions_row.addWidget(self._work_btn)

        # A QPushButton, NOT a QToolButton. The theme styles QPushButton — the
        # rounded fill every other button in this row has — and says nothing
        # about QToolButton, so the tool button rendered as a bare chevron
        # floating between two pills. Its own menu indicator is hidden because
        # the ▾ is already the label.
        self._work_menu_btn = QPushButton("▾")
        self._work_menu_btn.setAutoDefault(False)
        self._work_menu_btn.setStyleSheet(
            "QPushButton::menu-indicator { image: none; width: 0px; }")
        self._work_menu_btn.setToolTip(
            "Run Find & write on this now, without queueing it.")
        _wm = QMenu(self._work_menu_btn)
        self._work_now_act = _wm.addAction("Find && write now — skip the queue")
        self._work_now_act.setToolTip(
            "Open the unified pass on this selection straight away, without "
            "parking it first.")
        self._work_now_act.triggered.connect(self._work_through_now)
        self._work_menu_btn.setMenu(_wm)
        self._work_menu = _wm
        actions_row.addWidget(self._work_menu_btn)

        # Straight to Anki's own browser, with the gap's title as the search.
        # Neither of these asks an AI anything — they are the "just let me look"
        # door, and they were the one thing this pane made you leave it for.
        #
        # Named in words, and no emoji. 🏷 and 🔎 are outside the font this
        # theme renders with, so they came out as a blank block and a dark
        # blob — two buttons that looked broken and said nothing. A label that
        # can't render is worse than a longer one that can.
        self._find_tag_btn = QPushButton("Find tags")
        self._find_tag_btn.setAutoDefault(False)
        self._find_tag_btn.setToolTip(
            "Open Anki's own Browse screen, searching the tags for this "
            "gap's title.")
        self._find_tag_btn.clicked.connect(lambda: self._open_anki_browse(True))
        self._find_cards_btn = QPushButton("Find cards")
        self._find_cards_btn.setAutoDefault(False)
        self._find_cards_btn.setToolTip(
            "Open Anki's own Browse screen, searching the cards for this "
            "gap's title.")
        self._find_cards_btn.clicked.connect(lambda: self._open_anki_browse(False))

        self._own_btn = QPushButton("✎ Write my own card")
        self._own_btn.setAutoDefault(False)
        self._own_btn.setToolTip(
            "Write the card yourself. No AI in the path at all — it lands in "
            "the same deck, with the same tags, as a generated one would. "
            "Select several gaps to work through them one after another.")
        self._own_btn.clicked.connect(self._write_own_card)
        actions_row.addWidget(self._own_btn)
        self._save_btn = QPushButton("Save")
        self._save_btn.setAutoDefault(False)
        self._save_btn.clicked.connect(self._save)
        actions_row.addWidget(self._save_btn)
        root.addWidget(self._actions_w)
        # Second row: looking it up, and getting rid of it. Both rows WRAP
        # (FlowLayout): as fixed rows, a narrow window pushed Save and Delete
        # past the edge and gave the pane a sideways scrollbar.
        self._lookup_w = QWidget()
        lookup_row = FlowLayout(self._lookup_w)
        lookup_row.addWidget(self._find_tag_btn)
        lookup_row.addWidget(self._find_cards_btn)
        self._delete_btn = QPushButton("🗑 Delete")
        self._delete_btn.setAutoDefault(False)
        self._delete_btn.clicked.connect(self._delete)
        lookup_row.addWidget(self._delete_btn)
        root.addWidget(self._lookup_w)

        # Empty-state overlay
        self._empty_lbl = QLabel("Select a knowledge gap, or click ＋ Add KG to start.")
        self._empty_lbl.setStyleSheet("color: gray;")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._empty_lbl)

        root.addStretch(1)

        self._clear()

    def _rebuild_type_combo(self) -> None:
        """Refresh the dropdown items from current config (called on load and
        after settings save)."""
        prev = self._type.currentData() if self._type.count() else None
        self._type.blockSignals(True)
        self._type.clear()
        for t in _load_types():
            self._type.addItem(t["name"], t["key"])
            self._type.setItemData(self._type.count() - 1,
                                   t.get("description", ""),
                                   Qt.ItemDataRole.ToolTipRole)
        if prev is not None:
            idx = self._type.findData(prev)
            if idx >= 0:
                self._type.setCurrentIndex(idx)
        self._type.blockSignals(False)

    # ── state ────────────────────────────────────────────────────────────────

    def load(self, kg: dict | None) -> None:
        self._current = kg
        if kg is None:
            self._clear()
            return
        self._set_widgets_visible(True)
        self._empty_lbl.setVisible(False)

        self._title_input.setText(kg.get("title", ""))
        # Show the START of a long title. setText leaves the cursor at the end,
        # so a narrow pane opened on "…eligibility, age range and i".
        self._title_input.setCursorPosition(0)
        self._refresh_group_btn(kg)

        # Type combo — may have changed since panel was built. Block
        # signals during load so the auto-save handler doesn't fire on
        # programmatic selection changes.
        self._rebuild_type_combo()
        type_key = kg.get("type", "kg")
        self._type.blockSignals(True)
        try:
            idx = self._type.findData(type_key)
            if idx < 0:
                # Type has been deleted from settings — show a sentinel so the
                # selection isn't silently coerced to the first entry.
                meta = _type_meta(type_key)
                self._type.addItem(meta["name"], type_key)
                idx = self._type.count() - 1
                self._type.setItemData(idx, "Type removed from settings",
                                       Qt.ItemDataRole.ToolTipRole)
            self._type.setCurrentIndex(idx)
        finally:
            self._type.blockSignals(False)

        source = kg.get("source", "manual")
        source_label = SOURCE_LABELS.get(source, "?")
        self._source_lbl.setText(f"from <i>{source_label}</i>")
        self._source_lbl.setTextFormat(Qt.TextFormat.RichText)

        closed = kg.get("status", "open") == "done"
        self._status_lbl.setText(
            "<i>Closed</i>" if closed else "Open")
        self._status_lbl.setTextFormat(Qt.TextFormat.RichText)

        self._tags_input.setText(" ".join(kg.get("tags", [])))

        self._created_lbl.setText(f"added {_short_date(kg.get('created_at', ''))}")

        # Build schema-driven fields from the KG's current type.
        self._rebuild_schema_for_type(type_key, kg=kg)

        # Resources
        self._clear_resource_rows()
        for r in kg.get("resources", []) or []:
            self._add_resource_row(r.get("label", ""), r.get("url", ""))

        # LO analyser visibility — reset its transient state on KG change.
        self._lo_analyser.reset()
        self._lo_analyser.setVisible(type_key == "lo")
        self._apply_compact()

    def _clear(self) -> None:
        self._current = None
        self._set_widgets_visible(False)
        self._empty_lbl.setVisible(True)

    def _set_widgets_visible(self, visible: bool) -> None:
        for w in (self._title_input, self._type_lbl, self._type,
                  self._source_lbl, self._status_lbl, self._group_btn,
                  self._created_lbl, self._tag_row_w,
                  self._schema_container, self._res_box, self._work_btn,
                  self._work_menu_btn, self._find_tag_btn,
                  self._find_cards_btn, self._own_btn, self._delete_btn,
                  self._save_btn):
            w.setVisible(visible)
        # The LO analyser stays hidden when nothing is selected; load() re-
        # shows it only for type=lo KGs.
        if not visible:
            self._lo_analyser.setVisible(False)
        else:
            self._apply_compact()

    # ── schema-driven fields ─────────────────────────────────────────────────

    def _schema_for_type(self, type_key: str) -> list[dict]:
        for t in _load_types():
            if t["key"] == type_key:
                fields = t.get("fields") or []
                # Coerce + de-duplicate.
                seen: set[str] = set()
                out: list[dict] = []
                for spec in fields:
                    if not isinstance(spec, dict):
                        continue
                    key = str(spec.get("key", "")).strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "key":         key,
                        "label":       str(spec.get("label", key)),
                        "kind":        str(spec.get("kind", "text")).lower(),
                        "placeholder": str(spec.get("placeholder", "")),
                    })
                return out
        # Unknown type — no schema beyond a generic notes field.
        return [{"key": "notes", "label": "Notes", "kind": "longtext",
                 "placeholder": ""}]

    def _on_type_changed(self, _idx: int) -> None:
        if self._current is None:
            return
        # Persist current values to a stash keyed by field key so values
        # survive a type switch where keys overlap.
        prev_values = self._collect_schema_fields()
        new_type = self._type.currentData() or "kg"
        merged = dict(self._current.get("fields") or {})
        merged.update(prev_values)
        # Build with merged values so overlapping keys keep what was typed.
        pseudo_kg = {"fields": merged}
        self._rebuild_schema_for_type(new_type, kg=pseudo_kg)
        # Show/hide the inline LO analyser to match the new type.
        self._lo_analyser.reset()
        self._lo_analyser.setVisible(new_type == "lo")
        # Auto-persist the type change. Without this, switching the type
        # and navigating away silently reverts because the explicit Save
        # button is the only thing that wrote the new type to disk.
        ids = self._send_ids()
        cur_id = (self._current or {}).get("id")
        done = 0
        for kid in ids:
            try:
                # Only the gap on screen has its FIELDS merged — those are the
                # values in the widgets in front of you. The others get the
                # type and keep their own content.
                got = (kg_store.update(kid, type=new_type, fields=merged)
                       if kid == cur_id else kg_store.update(kid, type=new_type))
            except Exception as e:
                print(f"[ankisstant] auto-save type change failed: {e}")
                continue
            if got:
                done += 1
                if kid == cur_id:
                    self._current = got
        if done:
            self._parent_panel.refresh_list(
                preserve_selection=(self._current or {}).get("id"))
            if done > 1:
                tooltip(f"{done} gaps → {self._type.currentText()}")

    def _rebuild_schema_for_type(self, type_key: str, kg: dict | None = None) -> None:
        # Tear down existing widgets.
        while self._schema_layout.count():
            it = self._schema_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._schema_widgets = {}

        schema = self._schema_for_type(type_key)
        if not schema:
            empty = QLabel("<i>This type has no fields configured. Edit it in "
                           "Settings → Knowledge Gaps to add some.</i>")
            empty.setTextFormat(Qt.TextFormat.RichText)
            empty.setStyleSheet("color: gray; font-size: 11px;")
            empty.setWordWrap(True)
            self._schema_layout.addWidget(empty)
            return

        existing_values = (kg or {}).get("fields") or {}
        # Legacy fallback — if old top-level keys exist on the KG dict and the
        # store hasn't normalised yet, surface those too.
        if kg is not None and not existing_values:
            for legacy in ("notes", "stem_html", "system", "subsystem", "topic",
                           "platform", "lo", "lo_tag"):
                if kg.get(legacy):
                    existing_values[legacy] = kg[legacy]

        for spec in schema:
            row, getter, setter = self._build_field_widget(spec)
            label_text = spec["label"]
            kind = spec["kind"]
            label = QLabel(f"<b>{label_text}</b>")
            label.setTextFormat(Qt.TextFormat.RichText)
            self._schema_layout.addWidget(label)
            self._schema_layout.addWidget(row)
            self._schema_widgets[spec["key"]] = {
                "kind":   kind,
                "widget": row,
                "getter": getter,
                "setter": setter,
            }
            # Pre-fill from existing values.
            val = existing_values.get(spec["key"], "")
            if val:
                try:
                    setter(val)
                except Exception as e:
                    print(f"[ankisstant] set field {spec['key']!r}: {e}")

    def _build_field_widget(self, spec: dict):
        """Return (widget, getter, setter) for the given field spec."""
        kind = spec["kind"]
        ph   = spec.get("placeholder", "")
        if kind == "html":
            # Reuse the screenshot-aware editor from QBank capture.
            from .qbank.capture_dialog import _StemEdit
            w = _StemEdit()
            w.setAcceptRichText(True)
            w.setPlaceholderText(ph or "Paste text or a screenshot (Cmd/Ctrl+V)")
            w.setMinimumHeight(110)
            return w, (lambda: ("" if w.is_empty() else w.get_html())), \
                      (lambda v: w.setHtml(v) if v else w.clear())
        if kind == "longtext":
            w = QPlainTextEdit()
            w.setPlaceholderText(ph)
            w.setMinimumHeight(60)
            return w, (lambda: w.toPlainText().strip()), \
                      (lambda v: w.setPlainText(str(v) if v is not None else ""))
        if kind == "url":
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(4)
            le = QLineEdit()
            le.setPlaceholderText(ph or "https://…")
            open_btn = QPushButton("Open")
            open_btn.setAutoDefault(False)
            open_btn.clicked.connect(lambda _c=False, e=le: self._open_url(e.text()))
            h.addWidget(le, 1)
            h.addWidget(open_btn)
            return w, (lambda: le.text().strip()), \
                      (lambda v: le.setText(str(v) if v is not None else ""))
        if kind == "tag":
            w = QLineEdit()
            w.setPlaceholderText(ph or "School::Year3::…")
            attach_tag_completer(w, multi=False)
            return w, (lambda: w.text().strip()), \
                      (lambda v: w.setText(str(v) if v is not None else ""))
        # Default: text
        w = QLineEdit()
        w.setPlaceholderText(ph)
        return w, (lambda: w.text().strip()), \
                  (lambda v: w.setText(str(v) if v is not None else ""))

    def _collect_schema_fields(self) -> dict:
        out: dict = {}
        for key, info in self._schema_widgets.items():
            try:
                val = info["getter"]()
            except Exception:
                val = ""
            if val:
                out[key] = val
        # Preserve any existing fields that aren't part of the current
        # type's schema. Without this, saving an MQ KG that was captured
        # with stem_html / images / system / subsystem / topic would wipe
        # everything except whatever the (often-empty) schema exposes.
        if self._current:
            schema_keys = set(self._schema_widgets.keys())
            for k, v in (self._current.get("fields") or {}).items():
                if k not in schema_keys and k not in out and v:
                    out[k] = v
        return out

    # ── resources ────────────────────────────────────────────────────────────

    def _add_resource_row(self, label: str = "", url: str = "") -> None:
        # Ignore the QPushButton-emitted "checked" bool when triggered via click.
        if isinstance(label, bool):
            label, url = "", ""
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        lbl_input = QLineEdit(label or "")
        lbl_input.setPlaceholderText("Label (e.g. BNF chapter)")
        lbl_input.setMaximumWidth(220)
        url_input = QLineEdit(url or "")
        url_input.setPlaceholderText("https://…")
        open_btn = QPushButton("Open")
        open_btn.setAutoDefault(False)
        open_btn.clicked.connect(lambda _c=False, u=url_input: self._open_url(u.text()))
        rm_btn = QPushButton("×")
        compact_button(rm_btn)
        rm_btn.setAutoDefault(False)
        rm_btn.clicked.connect(lambda _c=False, r=row: self._remove_resource_row(r))
        rl.addWidget(lbl_input)
        rl.addWidget(url_input, 1)
        rl.addWidget(open_btn)
        rl.addWidget(rm_btn)
        row._lbl = lbl_input
        row._url = url_input
        self._res_layout.addWidget(row)

    def _remove_resource_row(self, row: QWidget) -> None:
        self._res_layout.removeWidget(row)
        row.deleteLater()

    def _clear_resource_rows(self) -> None:
        while self._res_layout.count():
            it = self._res_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _collect_resources(self) -> list[dict]:
        out = []
        for i in range(self._res_layout.count()):
            row = self._res_layout.itemAt(i).widget()
            if row is None:
                continue
            lbl = getattr(row, "_lbl", None)
            url = getattr(row, "_url", None)
            if lbl is None or url is None:
                continue
            l = lbl.text().strip()
            u = url.text().strip()
            if l or u:
                out.append({"label": l, "url": u})
        return out

    def _open_url(self, url: str) -> None:
        url = url.strip()
        if not url:
            tooltip("No URL.")
            return
        from aqt.qt import QDesktopServices, QUrl
        QDesktopServices.openUrl(QUrl(url))

    # ── persistence ──────────────────────────────────────────────────────────

    def _gather(self) -> dict:
        kg = dict(self._current or {})
        kg["title"]     = self._title_input.text().strip()
        kg["type"]      = self._type.currentData() or kg.get("type", "kg")
        # The pane does not EDIT the status any more — the list's tick box owns
        # it — so the stored value rides along untouched. It stays in the record
        # (and in _EDITABLE_KEYS) so a save can never blank a field it simply
        # isn't asking about, which is what dropping it here would do.
        kg["status"] = str((self._current or {}).get("status") or "open")
        kg["tags"]      = [t for t in self._tags_input.text().split() if t.strip()]
        kg["fields"]    = self._collect_schema_fields()
        kg["resources"] = self._collect_resources()
        # Strip legacy top-level keys so they don't shadow the new fields dict.
        for legacy in ("notes", "stem_html", "system", "subsystem", "topic",
                       "platform", "lo", "lo_tag"):
            kg.pop(legacy, None)
        return kg

    # Everything _gather() owns. Compared against the stored record to decide
    # whether an unsaved edit is sitting in the pane.
    _EDITABLE_KEYS = ("title", "type", "status", "tags",
                      "fields", "resources")

    def flush(self) -> bool:
        """Persist unsaved edits before the pane is pointed at another KG.

        Without this, retitling a gap and then clicking the next one in the list
        silently threw the edit away — load() overwrites every widget and
        nothing had written the old values back, so the title 'reverted' the
        moment you flicked away and back. Quiet on purpose: no tooltip, no list
        rebuild (rebuilding mid-selection-change would fight the selection).

        Returns True if something was actually written.
        """
        if not self._current:
            return False
        try:
            merged = self._gather()
        except Exception as e:
            print(f"[ankisstant] KG flush skipped — couldn't read the pane: {e}")
            return False
        # A blank title is a half-finished edit, not an instruction to wipe the
        # gap's name. Leave the stored one alone.
        if not merged.get("title"):
            return False
        if all(merged.get(k) == self._current.get(k) for k in self._EDITABLE_KEYS):
            return False
        try:
            updated = kg_store.update(self._current["id"], **{
                k: v for k, v in merged.items() if k != "id"
            })
        except Exception as e:
            print(f"[ankisstant] KG autosave failed: {e}")
            return False
        if not updated:
            return False
        self._current = updated
        return True

    def _commit_edit(self) -> None:
        """editingFinished on the title/tags boxes — save on focus-out or Enter,
        so the row in the list catches up straight away."""
        if self.flush():
            try:
                self._parent_panel.refresh_row(self._current.get("id"))
            except Exception as e:
                print(f"[ankisstant] KG row refresh failed: {e}")

    def _save(self) -> None:
        if not self._current:
            return
        merged = self._gather()
        if not merged.get("title"):
            tooltip("Title is required.")
            return
        updated = kg_store.update(self._current["id"], **{
            k: v for k, v in merged.items() if k != "id"
        })
        if updated:
            self._current = updated
            self._parent_panel.refresh_list(preserve_selection=updated["id"])
            tooltip("Saved.")

    def _delete(self) -> None:
        if not self._current:
            return
        ids = self._send_ids()
        if len(ids) > 1:
            if not askUser(f"Delete {len(ids)} knowledge gaps?\n\n"
                           "This can't be undone.", defaultno=True):
                return
            kg_store.remove_many(ids)
        else:
            if not askUser(
                f"Delete this knowledge gap?\n\n{self._current.get('title', '')}",
                defaultno=True,
            ):
                return
            kg_store.remove(self._current["id"])
        self._parent_panel.refresh_list()

    # ── actions ──────────────────────────────────────────────────────────────

    def refresh_send_labels(self, n: int) -> None:
        """Say out loud how many gaps each control will act on, so a
        multi-select can't silently act on one.

        It covers the whole pane now, not only the send buttons. Type, Status,
        Dismissed and Delete act on the selection too — they always looked as
        though they did, and for a long time they didn't."""
        many = n > 1
        if many:
            self._work_btn.setText(f"Send {n} gaps to queue")
            self._work_now_act.setText(
                f"Find && write {n} gaps now — skip the queue")
            self._own_btn.setText(f"✎ Write my own for {n} gaps")
        else:
            self._work_btn.setText("Send to queue")
            self._work_now_act.setText("Find && write now — skip the queue")
            self._own_btn.setText("✎ Write my own card")
        # The two Browse doors search ONE title, so they say so when the
        # selection is bigger than one rather than implying they'll do the lot.
        for b, what in ((getattr(self, "_find_tag_btn", None), "tags"),
                        (getattr(self, "_find_cards_btn", None), "cards")):
            if b is not None:
                b.setToolTip(
                    f"Open Anki's own Browse screen, searching the {what} for "
                    + ("the title of the gap shown above."
                       if many else "this gap's title."))
        lbl = getattr(self, "_type_lbl", None)
        if lbl is not None:
            lbl.setText(f"Type of {n}:" if many else "Type:")
            lbl.setToolTip(f"Applies to all {n} selected gaps." if many else "")
        btn = getattr(self, "_delete_btn", None)
        if btn is not None:
            btn.setText(f"🗑 Delete {n} gaps" if many else "🗑 Delete")

    def _send_ids(self) -> list[str]:
        """What the send buttons act on: the whole list selection, falling back
        to the loaded gap. Selecting five gaps and pressing the button used to
        send only the one the pane happened to be showing."""
        provider = self._selection_provider or self._parent_panel._selected_ids
        try:
            ids = list(provider() or [])
        except Exception:
            ids = []
        cur = (self._current or {}).get("id")
        if not ids and cur:
            ids = [cur]
        return ids

    def _queue_work_through(self) -> None:
        """Park the selection on the work-through queue and say so.

        Nothing opens. That is the point: you can send a gap, carry on reading,
        send three more from the other page, and deal with the lot in one pass
        when you're ready."""
        self.flush()
        ids = self._send_ids()
        if ids:
            self._parent_panel._queue_work_through(ids)

    def _work_through_now(self) -> None:
        self.flush()
        ids = self._send_ids()
        if ids:
            self._parent_panel._work_through_now(ids)

    def _open_anki_browse(self, as_tag: bool) -> None:
        """Anki's own Browse screen, searching this gap's title.

        One gap's title, even on a multi-select: a browser search is a thing you
        read, and OR-ing six titles into one query gives you a pile nobody can
        tell apart. The gap shown in the pane is the one you're looking at."""
        self.flush()
        kg = self._current
        if not kg:
            tooltip("Pick a gap first.")
            return
        open_anki_browse(str(kg.get("title") or ""), as_tag=as_tag)

    def _write_own_card(self) -> None:
        """No AI: type the card yourself, in Anki's own Add window.

        The editor is Anki's — cloze button, formatting, image paste, occlusion,
        every shortcut already in your hands. Ankisstant's contribution is the
        side-car beside it: the gap, what you wrote down about it, the tag the
        card should carry, and closing the gap once the card is in.

        Acts on the whole selection, like the two Send buttons beside it: pick
        six gaps and the side-car walks them, offering the next one each time a
        card goes in. It never advances on its own — two cards for one gap is
        an ordinary thing to want. See kg/write_own.py."""
        self.flush()
        gaps = [g for g in (kg_store.get(i) for i in self._send_ids()) if g]
        if not gaps:
            if self._current is None:
                return
            gaps = [kg_store.get(self._current.get("id")) or self._current]
        from .kg import write_own
        write_own.open_write_own(gaps, parent=self.window())

    def _on_group_clicked(self) -> None:
        self.flush()
        ids = self._send_ids()
        if ids:
            self._parent_panel._assign_group(ids)

    def _refresh_group_btn(self, kg: dict | None) -> None:
        name = str((kg or {}).get("group") or "").strip()
        if not name:
            self._group_btn.setText("Group: —")
            self._group_btn.setToolTip(
                "Not in a group. Click to put it in one."
            )
            return
        self._group_btn.setText(f"Group: {name}".replace("&", "&&"))
        self._group_btn.setToolTip(
            f"In “{name}” — tags as {autotag.group_segment(name)}. "
            "Click to move or remove it."
        )


def sync_done_to_things(*, silent: bool = False, parent=None) -> int:
    """Tick off, in Things, every imported gap that is finished here.

    Called after anything that closes gaps, and from the KG page's own menu for
    the backlog. Reads Things first, so it only ever writes the ones actually
    still open over there — a sync run twice in a row does nothing the second
    time and says so.

    `silent` is for the automatic call: it reports what it changed but doesn't
    interrupt with a dialog when there was nothing to do, because the automatic
    path fires after every close and most closes have no Things link at all."""
    if not importers.available():
        return 0
    try:
        pending = importers.pending_writeback(kg_store.load_all())
    except Exception as e:
        log.warn(f"things: couldn't work out what to sync: {e}")
        return 0
    if not pending:
        if not silent:
            tooltip("Nothing to tick off — every imported gap that's done here "
                    "is already done in Things.", period=4000)
        return 0

    titles = [str(k.get("title") or "?") for k in pending]
    if not silent:
        shown = "\n".join(f"  • {t}" for t in titles[:12])
        more = f"\n  … and {len(titles) - 12} more" if len(titles) > 12 else ""
        if not askUser(
                f"Mark {len(pending)} to-do(s) completed in Things?\n\n"
                f"{shown}{more}\n\n"
                "These are gaps you've finished here that are still open over "
                "there. Nothing is deleted — you can un-tick any of them in "
                "Things."):
            return 0

    uuids = [importers.linked_uuid(k) for k in pending]
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        done, missing, err = importers.complete_in_things(uuids)
    finally:
        QApplication.restoreOverrideCursor()

    if err:
        # Never silent: a permission prompt the user dismissed looks exactly
        # like a feature that doesn't work, and the fix is two clicks away in
        # System Settings.
        showWarning(f"Couldn't tick those off in Things.\n\n{err}")
        return 0
    bits = [f"Ticked {done} to-do(s) off in Things"]
    if missing:
        bits.append(f"{missing} no longer exist there")
    if done or missing or not silent:
        tooltip(" · ".join(bits) + ".", period=4000)
    return done


_things_sync_pending = False


def maybe_sync_to_things() -> None:
    """The automatic half, debounced onto the next event loop turn.

    Debounced because closing a gap happens inside loops — a twelve-gap
    work-through calls this twelve times, and each call is an osascript launch
    and a database snapshot. Coalescing them means one write for the whole
    operation, which is also the only way the user sees ONE tooltip saying what
    changed rather than twelve.

    Never raises: it runs on the tail of operations whose real work already
    succeeded, and a failed write-back must not be reported as a failed
    commit."""
    global _things_sync_pending
    try:
        if _things_sync_pending:
            return
        if not tool_config("knowledge_gaps").get("things_writeback", True):
            return
        if not importers.available():
            return
        _things_sync_pending = True
        QTimer.singleShot(0, _run_pending_things_sync)
    except Exception as e:
        log.warn(f"things: write-back skipped: {e}")


def _run_pending_things_sync() -> None:
    global _things_sync_pending
    _things_sync_pending = False
    try:
        sync_done_to_things(silent=True)
    except Exception as e:
        log.warn(f"things: write-back failed: {e}")


# ── Bulk import modal ──────────────────────────────────────────────────────────

# The instruction that matters here is the one telling the model NOT to write.
# "A concise, cleaned-up gap title" is an invitation to improve, and improving a
# gap means widening it: "Clomiphine hyperstimulation of ovaries" comes back as
# "Ovulation induction", which is a topic, not a gap. A gap's whole value is
# that it names the one thing the student didn't know — so the only edits
# allowed are ones that leave that thing exactly where it was.
_BULK_PARSE_SYSTEM = (
    "You are given a medical student's own list of knowledge gaps, as a "
    "numbered list of raw lines. Each line names ONE specific thing they "
    "didn't know. Your job is spelling and legibility ONLY.\n"
    "\n"
    "For EACH line return one JSON object:\n"
    '  {"title": "...", "system": "...", "subsystem": "...", "topic": "..."}\n'
    "Return ONLY a JSON array, one object per input line, in the same order. "
    "Never merge, split, drop, reorder or add lines.\n"
    "\n"
    "TITLE — you may ONLY:\n"
    "  • fix a misspelling            ('Mirizzi Syndorme' -> 'Mirizzi syndrome')\n"
    "  • expand an abbreviation the line already uses ('TRT SEs' -> "
    "'TRT (testosterone replacement) side effects')\n"
    "  • fix capitalisation and spacing\n"
    "\n"
    "You may NOT:\n"
    "  • make it broader, more general, or more 'proper'\n"
    "  • replace a specific entity with its category — 'Lymphoedema tardum' is "
    "NOT 'Types of lymphoedema'; '4-2-1 child fluids rule' is NOT 'Paediatric "
    "fluid management'\n"
    "  • turn a phrase into a question, or a question into a phrase\n"
    "  • add words that were not implied by the line\n"
    "  • drop a qualifier — 'after age 35', 'in pregnancy', 'first-line' are "
    "the whole point of the gap\n"
    "\n"
    "If a line is already clear, return it BYTE-FOR-BYTE unchanged. Returning "
    "the line untouched is the correct answer far more often than not, and is "
    "always better than a tidier line that means something wider.\n"
    "\n"
    "system / subsystem / topic: your best guess at where this sits in "
    "medicine, for filing. Empty string if unsure — a wrong guess is worse "
    "than none. These never change the title."
)


_PDF_EXTRACT_SYSTEM = (
    "You read a PDF a medical student attached — it may be a list of learning "
    "objectives (LOs), a set of knowledge gaps, lecture notes, or exam topics. "
    "Extract each DISTINCT learnable item as its own entry. Return ONLY a JSON "
    "array; one object per item, each with: \"title\" (a concise, self-contained "
    "gap/LO statement) and best-effort medical \"system\", \"subsystem\" and "
    "\"topic\" strings (empty string if unsure). Do not merge unrelated items, "
    "do not invent items that aren't in the document, and don't add commentary."
)


def _coerce_parsed_list(parsed) -> list | None:
    """Normalise Claude's bulk-parse output into a list of objects.

    Accepts a bare JSON array, or a dict wrapper like {"gaps": [...]} /
    {"items": [...]} (models sometimes wrap the array). Returns None if there's
    nothing list-shaped to use."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                return v
    return None


class _BulkImportDialog(QDialog):
    """Bring many gaps in at once — pasted, from a PDF, or from Things 3.

    All three sources land in the same preview table before anything is
    written, because every one of them can be wrong in a way you only see when
    you look at it: a markdown paste can mis-read a nested list, a PDF can
    invent an item, and the AI clean-up pass can quietly widen a gap. The table
    IS the review step; Import writes exactly what it shows.
    """

    C_TITLE, C_GROUP, C_NOTES = range(3)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.imported_count = 0
        # The parsed candidates currently on show. Rebuilt from the text box on
        # demand; edited in place by the table.
        self._rows: list[dict] = []
        self.setWindowTitle("Bulk import knowledge gaps")
        self.setMinimumSize(820, 620)
        self._build()
        theme_dialog(self)

    # ── build ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        intro = QLabel(
            "Paste your gaps — <b>one per line</b>, or as <b>markdown</b> where "
            "<code>#&nbsp;Headings</code> become groups and indented text under "
            "an item becomes that gap's notes. Or pull them straight out of "
            "<b>Things</b>, or a <b>PDF</b> of LOs."
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            "# SRS 12 Jaundice\n"
            "- [ ] Mirizzi syndrome\n"
            "    GB stone -> biliary obstruction\n"
            "- [ ] Patterns of hepatic disease\n"
            "\n"
            "# SRS 11 SOB\n"
            "- Clubbing causes"
        )
        self._text.setMaximumHeight(150)
        self._text.textChanged.connect(self._on_text_changed)
        root.addWidget(self._text)

        # ── source row ──────────────────────────────────────────────────────
        src = QHBoxLayout()
        src.addWidget(QLabel("Type:"))
        self._type = QComboBox()
        for t in _load_types():
            self._type.addItem(t["name"], t["key"])
        default_key = (tool_config("knowledge_gaps").get("default_type_on_add")
                       or "kg")
        self._type.setCurrentIndex(max(0, self._type.findData(default_key)))
        src.addWidget(self._type)
        src.addSpacing(12)

        self._things_btn = QPushButton("⇪  Import from Things…")
        self._things_btn.setAutoDefault(False)
        if importers.available():
            self._things_btn.setToolTip(
                "Read every Things to-do carrying a tag (default “KG”). Each "
                "one's PROJECT becomes its group, and its notes and checklist "
                "come with it. Nothing in Things is changed or completed.")
            self._things_btn.clicked.connect(self._on_load_things)
        else:
            self._things_btn.setEnabled(False)
            self._things_btn.setToolTip("Things 3 isn't installed on this Mac.")
        src.addWidget(self._things_btn)

        self._pdf_btn = QPushButton("📄  Load from PDF…")
        self._pdf_btn.setAutoDefault(False)
        self._pdf_btn.setEnabled(provider_configured())
        if not provider_configured():
            self._pdf_btn.setToolTip("Set up an AI provider in Settings first.")
        self._pdf_btn.clicked.connect(self._on_load_pdf)
        src.addWidget(self._pdf_btn)
        src.addStretch(1)
        root.addLayout(src)

        # ── options ─────────────────────────────────────────────────────────
        opt = QHBoxLayout()
        self._use_groups = QCheckBox("Keep groups")
        self._use_groups.setChecked(True)
        self._use_groups.setToolTip(
            "A markdown heading, or a Things project, becomes the gap's group "
            "— the unit the queue and the tag tree both work on. Untick to import everything ungrouped.")
        self._use_groups.stateChanged.connect(lambda _s: self._fill_table())
        opt.addWidget(self._use_groups)

        opt.addSpacing(10)
        opt.addWidget(QLabel("Put all in group:"))
        self._group_override = QLineEdit()
        self._group_override.setPlaceholderText("(leave blank to keep the ones above)")
        self._group_override.textChanged.connect(lambda _t: self._fill_table())
        # Takes the rest of the row: capped at 220px, its own hint was cut off.
        opt.addWidget(self._group_override, 1)
        root.addLayout(opt)

        self._use_claude = QCheckBox(
            "Use AI to fix spelling and infer System / Subsystem / Topic")
        self._use_claude.setEnabled(provider_configured())
        self._use_claude.setToolTip(
            "Spelling and legibility only — it is told, at length, not to make "
            "a gap broader or more 'proper'. Run it and check the table: the "
            "titles should look like yours, only spelled right.")
        if not provider_configured():
            self._use_claude.setToolTip("Set up an AI provider in Settings first.")
        root.addWidget(self._use_claude)

        # ── the preview ─────────────────────────────────────────────────────
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Gap", "Group", "Notes"])
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        th = self._table.horizontalHeader()
        th.setSectionResizeMode(self.C_TITLE, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(self.C_GROUP, QHeaderView.ResizeMode.Interactive)
        th.setSectionResizeMode(self.C_NOTES, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(self.C_GROUP, 150)
        self._table.itemChanged.connect(self._on_cell_changed)
        root.addWidget(self._table, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._import_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self._import_btn.setText("Import")
        self._import_btn.setAutoDefault(False)
        bb.accepted.connect(self._on_import)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._refresh_status()

    # ── sources ─────────────────────────────────────────────────────────────

    def _on_text_changed(self) -> None:
        """Re-parse the text box, leaving loaded rows alone.

        Rows are tagged with where they came from, so typing a line replaces
        only what the text box itself contributed. Without that, one keystroke
        after a Things import silently threw the whole import away — and the
        rows carry uuids and notes the text box never held, so it isn't
        recoverable by retyping."""
        pasted = importers.parse_markdown(self._text.toPlainText())
        for rec in pasted:
            rec["_src"] = "text"
        self._rows = [r for r in self._rows
                      if r.get("_src") != "text"] + pasted
        self._fill_table()

    def _on_load_things(self) -> None:
        tag, ok = QInputDialog.getText(
            self, "Import from Things",
            "Import every Things to-do tagged:", text=importers.DEFAULT_TAG)
        if not ok:
            return
        tag = tag.strip() or importers.DEFAULT_TAG
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            items = importers.fetch(tag)
        finally:
            QApplication.restoreOverrideCursor()
        if not items:
            names = importers.list_tags()
            showWarning(
                f"No open to-dos are tagged “{tag}” in Things.\n\n"
                + ("Tags on this Mac: " + ", ".join(names[:25])
                   if names else "Couldn't read your Things database."))
            return
        for rec in items:
            rec["_src"] = "things"
        # Replace a previous Things load rather than stacking a second copy of
        # the same to-dos on top of it.
        self._rows = [r for r in self._rows
                      if r.get("_src") != "things"] + items
        self._fill_table()
        tooltip(f"Loaded {len(items)} gap(s) from Things — review, then Import.",
                period=3500)

    def _on_load_pdf(self) -> None:
        if not provider_configured():
            showWarning("Set up an AI provider in Settings first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a PDF of LOs / knowledge gaps", "",
            "PDF (*.pdf);;All files (*)")
        if not path:
            return
        parsed = _coerce_parsed_list(run_claude_json(
            self._pdf_btn, "Reading PDF…",
            prompt="Extract the knowledge gaps / learning objectives from the "
                   "attached PDF.",
            system=_PDF_EXTRACT_SYSTEM, max_tokens=8000, attachments=[path]))
        if not parsed:
            tooltip("Couldn't pull any items from that PDF.", period=4000)
            return
        rows = []
        for obj in parsed:
            if not isinstance(obj, dict):
                continue
            title = importers.clean_title(str(obj.get("title") or ""))
            if not title:
                continue
            rows.append({
                "title": title,
                "notes": str(obj.get("notes") or "").strip(),
                "group": str(obj.get("group") or "").strip(),
                "things_uuid": "", "done": False,
                "levels": {k: str(obj.get(k) or "").strip()
                           for k in ("system", "subsystem", "topic")
                           if str(obj.get(k) or "").strip()},
            })
        if not rows:
            tooltip("Couldn't pull any items from that PDF.", period=4000)
            return
        for rec in rows:
            rec["_src"] = "pdf"
        self._rows = self._rows + rows
        self._fill_table()
        tooltip(f"Loaded {len(rows)} item(s) from PDF — review, then Import.",
                period=3500)

    # ── the preview table ───────────────────────────────────────────────────

    def _effective_group(self, rec: dict) -> str:
        override = self._group_override.text().strip()
        if override:
            return override
        if not self._use_groups.isChecked():
            return ""
        return str(rec.get("group") or "").strip()

    def _fill_table(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._rows))
        for i, rec in enumerate(self._rows):
            title = QTableWidgetItem(str(rec.get("title") or ""))
            title.setToolTip("Edit this before importing if the AI changed it "
                             "more than you wanted.")
            self._table.setItem(i, self.C_TITLE, title)
            self._table.setItem(i, self.C_GROUP,
                                QTableWidgetItem(self._effective_group(rec)))
            notes = str(rec.get("notes") or "")
            cell = QTableWidgetItem(" ⏎ ".join(notes.splitlines()))
            cell.setToolTip(notes or "no notes")
            self._table.setItem(i, self.C_NOTES, cell)
        self._table.blockSignals(False)
        self._refresh_status()

    def _on_cell_changed(self, item) -> None:
        """The table is editable and its edits are what import."""
        row = item.row()
        if not (0 <= row < len(self._rows)):
            return
        col = item.column()
        if col == self.C_TITLE:
            self._rows[row]["title"] = item.text().strip()
        elif col == self.C_GROUP:
            self._rows[row]["group"] = item.text().strip()
        elif col == self.C_NOTES:
            self._rows[row]["notes"] = item.text().replace(" ⏎ ", "\n").strip()
        self._refresh_status()

    def _refresh_status(self) -> None:
        rows = [r for r in self._rows if str(r.get("title") or "").strip()]
        if not rows:
            self._status.setText("Nothing to import yet.")
            self._import_btn.setEnabled(False)
            return
        try:
            fresh, dupes = importers.dedupe_against_store(
                [{**r, "group": self._effective_group(r)} for r in rows],
                kg_store.load_all())
        except Exception as e:
            log.warn(f"bulk import: dedupe failed: {e}")
            fresh, dupes = rows, []
        groups = {self._effective_group(r) for r in rows}
        groups.discard("")
        bits = [f"{len(fresh)} gap(s) to import"]
        if groups:
            bits.append(f"{len(groups)} group(s): "
                        + ", ".join(sorted(groups)[:6])
                        + ("…" if len(groups) > 6 else ""))
        if dupes:
            bits.append(f"{len(dupes)} already in your list — skipped")
        n_notes = sum(1 for r in rows if str(r.get("notes") or "").strip())
        if n_notes:
            bits.append(f"{n_notes} with notes")
        self._status.setText("  ·  ".join(bits))
        self._import_btn.setEnabled(bool(fresh))

    # ── import ──────────────────────────────────────────────────────────────

    def _run_ai_cleanup(self) -> None:
        """Spelling only — and the table shows exactly what it did.

        Applied per index against the ORIGINAL row, so a reply of the wrong
        length can only fail to improve a line, never shift every title onto
        the wrong gap."""
        lines = [str(r.get("title") or "").strip() for r in self._rows]
        lines = [ln for ln in lines if ln]
        if not lines:
            return
        numbered = "\n".join(f"{i + 1}. {ln}" for i, ln in enumerate(lines))
        budget = min(8000, max(2000, len(lines) * 120))
        parsed = _coerce_parsed_list(run_claude_json(
            self._import_btn, "Checking spelling…",
            prompt=numbered, system=_BULK_PARSE_SYSTEM, max_tokens=budget))
        if parsed is None:
            tooltip("AI couldn't structure the list — importing your titles "
                    "as they are.", period=4000)
            return
        changed = 0
        for i, rec in enumerate(self._rows):
            obj = parsed[i] if i < len(parsed) else None
            if not isinstance(obj, dict):
                continue
            new = str(obj.get("title") or "").strip()
            if new and new != rec.get("title"):
                rec["title"] = new
                changed += 1
            levels = {k: str(obj.get(k) or "").strip()
                      for k in ("system", "subsystem", "topic")
                      if str(obj.get(k) or "").strip()}
            if levels:
                rec.setdefault("levels", {}).update(levels)
        self._fill_table()
        if changed:
            tooltip(f"AI changed {changed} of {len(self._rows)} title(s) — "
                    "check them in the table before importing.", period=5000)

    def _on_import(self) -> None:
        if not self._rows:
            tooltip("Nothing to import — paste some lines first.")
            return
        if self._use_claude.isChecked() and provider_configured():
            self._run_ai_cleanup()

        staged = [{**r, "group": self._effective_group(r)}
                  for r in self._rows if str(r.get("title") or "").strip()]
        fresh, dupes = importers.dedupe_against_store(staged,
                                                      kg_store.load_all())
        if not fresh:
            tooltip("Every one of those is already in your gap list.",
                    period=4000)
            return
        records = importers.to_store_records(
            fresh, type_key=self._type.currentData() or "kg",
            use_groups=True)
        # System/Subsystem/Topic ride in the type's own fields, alongside notes.
        for rec, src in zip(records, fresh):
            for k, v in (src.get("levels") or {}).items():
                rec["fields"].setdefault(k, v)
        kg_store.add_many(records)
        # A markdown import writes its own groups; any of them you had put away
        # comes back, same as adding one gap does — see wake_group.
        woken = wake_groups(r.get("group") for r in records)
        self.imported_count = len(records)
        if woken:
            tooltip("Brought back: " + ", ".join(woken), period=4000)
        if dupes:
            tooltip(f"Skipped {len(dupes)} already in your list.", period=4000)
        self.accept()


# ── Main panel ───────────────────────────────────────────────────────────────

# Open covers in-progress too. A gap that has been sent to Browse or Create is
# still a gap you haven't closed, and giving it a chip of its own split the
# working list in two — the row already says "sent, in progress" in words.
_STATUS_FILTERS = [
    ("open", "Open"),
    ("done", "Closed"),
]


def group_state(st: dict) -> str:
    """A group's state in one word, derived from its gaps: Closed once none of
    them are still open, Open while any is. Said in words wherever it is shown —
    a row that merely goes grey reads as disabled, not as finished."""
    if not st.get("total"):
        return "Empty — no gaps"
    return "Closed" if (st["open"] + st["in_progress"]) == 0 else "Open"


class _KGGroupRow(QListWidgetItem):
    """One group in the Groups view — name on top, counts and state beneath,
    drawn as two lines by _KGItemDelegate."""

    def __init__(self, name: str, st: dict, *, archived: bool = False):
        super().__init__()
        self.group_name = name
        self.state = group_state(st)
        open_n = st["open"] + st["in_progress"]
        bits = [f"{st['total']} gap" + ("s" if st["total"] != 1 else "")]
        if open_n:
            bits.append(f"{open_n} open")
        if st["done"]:
            bits.append(f"{st['done']} done")
        bits.append(self.state)
        if archived:
            bits.append("archived")
        self.setText(name or NO_GROUP)
        self.setData(_NOTES_ROLE, "   ·   ".join(bits))


class _GroupsView(QWidget):
    """The Groups page: every group down the left, the one you picked on the
    right — what it is tagged as, whether it is open or closed, and every gap
    in it.

    It replaces a horizontal row of group chips, which stopped being readable at
    about eight groups and could only ever filter the gap list. Picking several
    groups here is the list's own selection (Shift/Cmd-click), not the marking
    gesture the gap page needs for its unselectable headers, and every batch
    tool runs on the union of what you picked."""

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel
        self._stats: dict[str, dict] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Expanding)

        # ── left: the groups ──
        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 6, 0)
        lcol.setSpacing(4)
        self._count = QLabel("")
        self._count.setStyleSheet("color: gray; font-size: 11px;")
        lcol.addWidget(self._count)
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setItemDelegate(_KGItemDelegate(self._list))
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_menu)
        self._list.itemSelectionChanged.connect(self._on_select)
        lcol.addWidget(self._list, 1)
        hint = QLabel("Shift/Cmd-click to pick several groups and run them "
                      "together.")
        hint.setStyleSheet("color: gray; font-size: 10px;")
        hint.setWordWrap(True)
        lcol.addWidget(hint)
        self._show_archived = QCheckBox("Show archived")
        self._show_archived.setStyleSheet("font-size: 11px; color: gray;")
        self._show_archived.setToolTip(
            "Archived groups are put away, not deleted — their gaps and cards "
            "are untouched.")
        self._show_archived.toggled.connect(lambda _v: self.refresh())
        lcol.addWidget(self._show_archived)
        brow = QHBoxLayout()
        manage = QPushButton("⚙︎ Manage groups…")
        manage.setAutoDefault(False)
        manage.setStyleSheet("font-size: 11px;")
        manage.setToolTip(
            "Every group, finished and archived ones included: rename them, "
            "change the tag they file under, or put a finished batch away.")
        manage.clicked.connect(panel._open_group_manager)
        brow.addWidget(manage)
        brow.addStretch(1)
        refresh = QPushButton("⟳")
        compact_button(refresh)
        refresh.setToolTip("Refresh")
        refresh.setAutoDefault(False)
        refresh.clicked.connect(lambda: self.refresh())
        brow.addWidget(refresh)
        lcol.addLayout(brow)
        splitter.addWidget(left)

        # ── right: the group on top, the gap you clicked underneath ──
        #
        # One page that does the lot: pick a group, see what it is, pick a gap
        # inside it and edit or send it without ever leaving for the Gaps page.
        right = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        rcol = QVBoxLayout(top)
        rcol.setContentsMargins(6, 0, 0, 0)
        rcol.setSpacing(6)
        self._title = QLabel("")
        self._title.setTextFormat(Qt.TextFormat.RichText)
        self._title.setWordWrap(True)
        rcol.addWidget(self._title)
        self._state = QLabel("")
        self._state.setTextFormat(Qt.TextFormat.RichText)
        self._state.setWordWrap(True)
        rcol.addWidget(self._state)
        self._meta = QLabel("")
        self._meta.setTextFormat(Qt.TextFormat.RichText)
        self._meta.setWordWrap(True)
        self._meta.setStyleSheet("font-size: 11px;")
        self._meta.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        rcol.addWidget(self._meta)

        # Everything below the heading only means something once a group is
        # picked. Before that it was a row of greyed-out buttons over an empty
        # box — a form for nothing — so it is hidden instead (see _show).
        self._group_body = QWidget()
        bcol = QVBoxLayout(self._group_body)
        bcol.setContentsMargins(0, 0, 0, 0)
        bcol.setSpacing(6)
        rcol.addWidget(self._group_body, 1)
        # The same as the gap pane: one button sends the group to the queue,
        # and the ▾ runs it straight away instead.
        self._buttons: dict[str, QPushButton] = {}
        run_row = QHBoxLayout()
        run_row.setSpacing(4)
        b = QPushButton("Send to queue")
        b.setAutoDefault(False)
        b.setToolTip(
            "Park this group's open gaps on the queue. They wait there — add "
            "as many groups as you like — until you press ▶ Run queue in the "
            "bottom right, which finds and writes cards for the lot in one "
            "pass, then empties.\n\nRight-click a gap to take it back off "
            "the queue.")
        self._buttons["work"] = b
        run_row.addWidget(b)

        # QPushButton for the same reason as the gap pane's — see there.
        self._work_menu_btn = QPushButton("▾")
        self._work_menu_btn.setAutoDefault(False)
        self._work_menu_btn.setStyleSheet(
            "QPushButton::menu-indicator { image: none; width: 0px; }")
        self._work_menu_btn.setToolTip(
            "Run Find & write on this group now, without queueing it.")
        _gm = QMenu(self._work_menu_btn)
        _gm.addAction("Find && write now — skip the queue",
                      lambda: self._run(panel._work_through_now))
        self._work_menu_btn.setMenu(_gm)
        run_row.addWidget(self._work_menu_btn)
        run_row.addStretch(1)
        bcol.addLayout(run_row)
        self._buttons["work"].clicked.connect(
            lambda: self._run(panel._queue_work_through))

        edit_row = QHBoxLayout()
        edit_row.setSpacing(4)
        for key, text, tip in (
            ("add", "＋ Add KG",
             "Add a gap straight into this group (⌘N) — no saving it loose "
             "and moving it afterwards."),
            ("rename", "Rename / edit tag…",
             "Rename the group, or change the tag branch it files under. Cards "
             "already tagged the old way move with it."),
            ("retag", "Retag…",
             "Bring this group's tags up to date: rebuild each gap's content "
             "tag, and put the group's shared branch and source onto cards "
             "that don't carry them yet. Shows you what changes before "
             "writing — this is also how you fix a batch made before you set "
             "the group up."),
            ("regroup", "Regroup…", "Move these gaps into another group."),
            ("done", "Close all", "Close every gap in this group."),
            ("archive", "Archive", "Put a finished batch away. Its gaps and "
                                   "cards are untouched."),
        ):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.setStyleSheet("font-size: 11px;")
            b.setToolTip(tip)
            self._buttons[key] = b
            edit_row.addWidget(b)
        edit_row.addStretch(1)
        bcol.addLayout(edit_row)
        self._buttons["add"].clicked.connect(self._on_add)
        self._buttons["rename"].clicked.connect(self._on_rename)
        self._buttons["retag"].clicked.connect(self._on_retag)
        self._buttons["regroup"].clicked.connect(
            lambda: self._run(panel._assign_group))
        self._buttons["done"].clicked.connect(self._on_done)
        self._buttons["archive"].clicked.connect(self._on_archive)

        self._gaps_lbl = QLabel("")
        self._gaps_lbl.setStyleSheet("color: gray; font-size: 11px;")
        bcol.addWidget(self._gaps_lbl)
        self._gaps = QListWidget()
        self._gaps.setAlternatingRowColors(True)
        self._gaps.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._gaps.setItemDelegate(_KGItemDelegate(self._gaps))
        self._gaps.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._gaps.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._gaps.customContextMenuRequested.connect(self._on_gap_menu)
        self._gaps.itemDoubleClicked.connect(self._on_gap_opened)
        self._gaps.setToolTip(
            "The tick box closes a gap. Highlight the rows you want the pane "
            "below to act on — click, Shift-click a run, Cmd-click to add one. "
            "Double-click a gap to open it on the Gaps page. Right-click for "
            "the same actions the gap list has.")
        bcol.addWidget(self._gaps, 1)
        self._gaps.itemSelectionChanged.connect(self._on_gap_select)
        # Same rule as the Gaps page: the box is the Closed state, the
        # highlight is the batch. Both pages write through
        # panel.set_gaps_closed, so a gap closed here is closed there.
        self._gaps.itemSelectionChanged.connect(self._after_gap_ticks)
        self._gaps.itemChanged.connect(self._on_gap_ticked)
        gtick_row = QHBoxLayout()
        gtick_row.setSpacing(4)
        for _text, _on in (("Select all", True), ("Clear selection", False)):
            _tb = QPushButton(_text)
            _tb.setAutoDefault(False)
            _tb.setStyleSheet("font-size: 10px; padding: 1px 8px;")
            _tb.clicked.connect(lambda _c=False, v=_on: self._tick_all_gaps(v))
            gtick_row.addWidget(_tb)
        self._gaps_tick_lbl = QLabel("")
        self._gaps_tick_lbl.setStyleSheet("color: gray; font-size: 10px;")
        gtick_row.addWidget(self._gaps_tick_lbl)
        gtick_row.addStretch(1)
        bcol.addLayout(gtick_row)
        # With no group picked the body is hidden and this takes the space, so
        # the heading stays at the top instead of spreading down the pane.
        self._group_filler = QWidget()
        rcol.addWidget(self._group_filler, 1)
        right.addWidget(top)

        # The gap pane: the same one the Gaps page uses, minus what won't fit
        # (see KGDetailPane.compact). Its send buttons act on THIS list, not the
        # Gaps page's selection — hence the provider.
        self._detail = KGDetailPane(
            panel, compact=True, selection_provider=self.gap_ids)
        det_wrap = QScrollArea()
        det_wrap.setWidgetResizable(True)
        det_wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        det_wrap.setWidget(self._detail)
        right.addWidget(det_wrap)
        self._det_wrap = det_wrap
        right.setSizes([420, 320])
        splitter.addWidget(right)
        splitter.setSizes([260, 640])
        root.addWidget(splitter, 1)
        self._show([])

    # ── the list ──────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Rebuild from the store, keeping whatever was selected."""
        panel = self._panel
        keep = set(self.selected_names())
        self._keep_gap = (self._detail._current or {}).get("id") \
            if getattr(self, "_detail", None) else None
        self._stats = panel._group_stats()
        archived = set(panel._archived_groups())
        ungrouped = kg_store.in_group("")
        if ungrouped:
            # Loose gaps are a group in every sense that matters here — they
            # have a state, they can be worked through, and leaving them off
            # this page would make it lie about how many gaps you have.
            self._stats[""] = {
                "total": len(ungrouped),
                "open": sum(1 for k in ungrouped
                            if k.get("status") == "open"),
                "in_progress": sum(1 for k in ungrouped
                                   if k.get("status") == "in_progress"),
                "done": sum(1 for k in ungrouped
                            if k.get("status") == "done"),
                "last": "",
            }
        names = list(self._stats)
        if not self._show_archived.isChecked():
            names = [n for n in names if n not in archived]

        def bucket(n: str) -> int:
            if n in archived:
                return 2
            return 1 if group_state(self._stats[n]) != "Open" else 0

        names.sort(key=lambda n: (bucket(n), kg_store.group_sort_key(n)))
        self._list.blockSignals(True)
        self._list.clear()
        for n in names:
            self._list.addItem(
                _KGGroupRow(n, self._stats[n], archived=n in archived))
        self._list.blockSignals(False)
        open_n = sum(1 for n in names if group_state(self._stats[n]) == "Open")
        self._count.setText(
            f"{len(names)} group(s) · {open_n} open · "
            f"{len(names) - open_n} closed or empty")
        if keep:
            for i in range(self._list.count()):
                it = self._list.item(i)
                if it.group_name in keep:
                    it.setSelected(True)
        self._on_select()

    def selected_names(self) -> list[str]:
        return [it.group_name for it in self._list.selectedItems()
                if isinstance(it, _KGGroupRow)]

    def _ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for n in self.selected_names():
            for kid in self._panel._group_ids(n):
                if kid not in seen:
                    seen.add(kid)
                    ids.append(kid)
        return ids

    def _run(self, fn) -> None:
        """Run one of the three batch tools over the picked group(s).

        Done gaps are left out — see KGPanel._live_ids. A group you have worked
        through is meant to shrink, not to be re-read every time you press the
        button again."""
        ids = self._ids()
        if not ids:
            tooltip("Pick a group first.")
            return
        live = self._panel._live_ids(ids)
        if not live:
            tooltip(f"All {len(ids)} gap(s) here are Closed — nothing to run.")
            return
        if len(live) != len(ids):
            tooltip(f"Running {len(live)} gap(s) — "
                    f"{len(ids) - len(live)} already Closed.")
        fn(live)

    # ── the right-hand pane ───────────────────────────────────────────────────

    def _on_select(self) -> None:
        self._show(self.selected_names())

    def _show(self, names: list[str]) -> None:
        for b in self._buttons.values():
            b.setEnabled(bool(names))
        # The ▾ isn't in self._buttons (it's built beside them, not with them),
        # so it has to be told separately — otherwise it stays live over an
        # empty pick and offers to send nothing anywhere.
        self._work_menu_btn.setEnabled(bool(names))
        self._group_body.setVisible(bool(names))
        self._det_wrap.setVisible(bool(names))
        self._group_filler.setVisible(not names)
        if not names:
            self._title.setText("<h3 style='margin:0'>No group picked</h3>")
            self._state.setText("")
            self._meta.setText(
                "Pick a group on the left to see its tag, its state and the "
                "gaps in it.")
            self._gaps_lbl.setText("")
            self._gaps.clear()
            self._after_gap_ticks()
            self._buttons["work"].setText("Send to queue")
            for k in ("rename", "retag", "archive"):
                self._buttons[k].setEnabled(False)
            return

        stats = {n: self._stats.get(n) or {"total": 0, "open": 0,
                                           "in_progress": 0, "done": 0}
                 for n in names}
        total = sum(s["total"] for s in stats.values())
        open_n = sum(s["open"] + s["in_progress"] for s in stats.values())
        done_n = sum(s["done"] for s in stats.values())
        one = names[0] if len(names) == 1 else None
        archived = set(self._panel._archived_groups())

        if one is not None:
            self._title.setText(
                f"<h3 style='margin:0'>{_html.escape(one or NO_GROUP)}</h3>")
        else:
            self._title.setText(
                f"<h3 style='margin:0'>{len(names)} groups</h3>"
                + _html.escape(", ".join(n or NO_GROUP for n in names)))
        state = "Open" if open_n else ("Closed" if total else "Empty")
        detail = (f"{open_n} of {total} gap(s) still open"
                  if open_n else
                  (f"all {total} gap(s) done" if total else "no gaps in it"))
        arch_names = [n for n in names if n in archived]
        arch = (f" · archived ({len(arch_names)})" if len(names) > 1 and arch_names
                else (" · archived" if arch_names else ""))
        self._state.setText(f"<b>{state}</b> — {detail}{arch}")

        rows: list[str] = []
        if one is not None and one:
            try:
                prefix = autotag.group_tag_prefix(one)
            except Exception:
                prefix = ""
            rows.append("Tag branch: <code>"
                        + _html.escape(prefix or "—") + "</code>")
            try:
                ov = autotag.group_override(one) or {}
            except Exception:
                ov = {}
            try:
                gplan = autotag.group_tag_plan(one)
            except Exception:
                gplan = {}
            if autotag.group_branch(one, gplan or None):
                depth = autotag.GROUP_LEVEL_LABELS.get(gplan.get("levels"),
                                                       gplan.get("levels"))
                rows.append(("Its own branch — outside the auto-tag base. "
                             if gplan.get("absolute") else
                             "A shared head node inside the auto-tag base. ")
                            + f"Under it: {_html.escape(str(depth))}")
            elif gplan:
                rows.append("Not tagged as one group — these file by content "
                            "and source only.")
            if ov.get("source"):
                rows.append("Source: " + _html.escape(str(ov["source"])))
            last = (self._stats.get(one) or {}).get("last") or ""
            if last:
                rows.append("Last touched: " + _html.escape(last[:16].replace("T", " ")))
        elif one == "":
            rows.append("Not in a group — these file by content and source "
                        "only.")
        else:
            rows.append(f"{total} gap(s) across {len(names)} groups: "
                        f"{open_n} open, {done_n} done.")
            rows.append("Rename and Retag are about one group's own identity, "
                        "so they're off while several are picked.")
        self._meta.setText("<br>".join(rows))

        single = one is not None and bool(one)
        # Enabled for exactly one pick, the ungrouped section included — a new
        # loose gap is a perfectly good answer there.
        self._buttons["add"].setEnabled(one is not None)
        self._buttons["rename"].setEnabled(single)
        self._buttons["retag"].setEnabled(single)
        self._buttons["archive"].setEnabled(any(n for n in names))
        self._buttons["archive"].setText(
            "Restore" if single and one in archived else "Archive")
        # The buttons name the LIVE count, so the page never offers to work
        # through 12 gaps and then quietly run 4. Nothing to run = disabled,
        # and the button says why rather than just going grey.
        if open_n:
            self._buttons["work"].setEnabled(True)
            self._work_menu_btn.setEnabled(True)
            self._buttons["work"].setText(f"Send {open_n} gap(s) to queue")
        else:
            self._buttons["work"].setEnabled(False)
            self._work_menu_btn.setEnabled(False)
            self._buttons["work"].setText("Send to queue — all Closed")

        # Rebuilt with signals off, then _on_gap_select is called ONCE at the
        # end: clearing a list emits a selection change per row, and each one
        # would flush and reload the pane underneath mid-rebuild.
        self._gaps.blockSignals(True)
        self._gaps.clear()
        # Re-read before the rows are built: a tick made on the Gaps page has
        # to show on the first refresh, not the second.
        shown = 0
        for n in names:
            for kg in sorted(kg_store.in_group(n),
                             key=lambda k: ({"open": 0, "in_progress": 0,
                                             "done": 1}.get(
                                                 str(k.get("status") or "open"), 0),
                                            str(k.get("title") or "").lower())):
                li = _KGListItem(kg)
                self._gaps.addItem(li)
                shown += 1
        self._gaps_lbl.setText(
            f"Gaps in {'this group' if one is not None else 'these groups'} "
            f"({shown}) — click one to edit it below, double-click to open it "
            "on the Gaps page")
        keep_gap = getattr(self, "_keep_gap", None)
        self._keep_gap = None
        if keep_gap:
            for i in range(self._gaps.count()):
                it = self._gaps.item(i)
                if isinstance(it, _KGListItem) and it.kg_id == keep_gap:
                    self._gaps.setCurrentItem(it)
                    break
        self._gaps.blockSignals(False)
        self._on_gap_select()
        self._after_gap_ticks()

    # ── actions ───────────────────────────────────────────────────────────────

    def _on_menu(self, pos) -> None:
        it = self._list.itemAt(pos)
        if isinstance(it, _KGGroupRow) and not it.isSelected():
            self._list.setCurrentItem(it)
        names = self.selected_names()
        if not names:
            return
        self._panel._group_menu_for(names, self._list).exec(
            self._list.mapToGlobal(pos))

    def _on_gap_menu(self, pos) -> None:
        it = self._gaps.itemAt(pos)
        if isinstance(it, _KGListItem) and not it.isSelected():
            self._gaps.setCurrentItem(it)
        ids = self.gap_ids()
        if not ids:
            return
        menu = self._panel._gap_actions_menu(ids)
        menu.addSeparator()
        menu.addAction("Open on the Gaps page",
                       lambda: self._panel.jump_to_gap(ids[0]))
        menu.exec(self._gaps.mapToGlobal(pos))

    def _on_gap_select(self) -> None:
        """Load the clicked gap into the pane underneath, saving whatever the
        pane was holding first — load() blows the widgets away, so an unsaved
        title would go with it."""
        prev = (self._detail._current or {}).get("id")
        try:
            if self._detail.flush():
                self._panel.refresh_row(prev)
        except Exception as e:
            print(f"[ankisstant] groups pane flush failed: {e}")
        items = self._gaps.selectedItems()
        it = self._gaps.currentItem()
        if not isinstance(it, _KGListItem) or it not in items:
            it = next((x for x in items if isinstance(x, _KGListItem)), None)
        if it is None:
            self._detail.load(None)
            return
        self._detail.refresh_send_labels(len(self.gap_ids()))
        # Extending a selection over the gap already loaded must not reload it —
        # that would revert edits still in the boxes.
        if prev and prev == it.kg_id:
            return
        self._detail.load(kg_store.get(it.kg_id))

    def _on_gap_opened(self, item) -> None:
        if isinstance(item, _KGListItem) and item.kg_id:
            self._panel.jump_to_gap(item.kg_id)

    # ── ticking ───────────────────────────────────────────────────────────────

    def gap_ids(self) -> list[str]:
        """What this page's pane and menus act on: the HIGHLIGHTED gaps. Same
        rule as the Gaps page, so the two lists behave identically."""
        return [x.kg_id for x in self._gaps.selectedItems()
                if isinstance(x, _KGListItem) and x.kg_id]

    def _on_gap_ticked(self, item) -> None:
        """The box on a row closes that gap — see _KGListItem. Deferred for the
        same reason the Gaps page defers: the write rebuilds this list, and
        rebuilding it inside its own itemChanged tears down the row mid-signal."""
        if not isinstance(item, _KGListItem) or not item.kg_id:
            return
        kid = item.kg_id
        closed = item.checkState() == Qt.CheckState.Checked
        QTimer.singleShot(0, lambda: self._close_gap(kid, closed))

    def _close_gap(self, kid: str, closed: bool) -> None:
        # No refresh of our own: set_gaps_closed ends in panel.refresh_list(),
        # which rebuilds this page when it is the one on show.
        self._panel.set_gaps_closed([kid], closed)

    def _tick_all_gaps(self, on: bool) -> None:
        """Highlight every gap this group shows, or drop the highlight. The
        batch-building gesture, now that the box means Closed."""
        self._gaps.clearSelection()
        if on:
            for it in _gap_items(self._gaps):
                it.setSelected(True)
        self._after_gap_ticks()

    def _after_gap_ticks(self) -> None:
        n = len(self.gap_ids())
        self._gaps_tick_lbl.setText(
            "" if not n else f"{n} selected" if n > 1 else "1 selected")
        try:
            self._detail.refresh_send_labels(n)
        except Exception as e:
            print(f"[ankisstant] couldn't relabel the groups pane: {e}")

    def _on_rename(self) -> None:
        """Rename is genuinely one-at-a-time — you are typing the new name.

        It says so instead of doing nothing: both buttons stayed enabled on a
        multi-select and then returned silently, which reads as a broken
        button rather than as a restriction."""
        names = [n for n in self.selected_names() if n]
        if not names:
            return
        if len(names) > 1:
            tooltip("Rename takes one group at a time — you're naming it. "
                    f"{len(names)} are picked.")
            return
        self._panel._rename_group(names[0])
        self.refresh()

    def _on_retag(self) -> None:
        names = [n for n in self.selected_names() if n]
        if not names:
            return
        self._panel._retag_groups(names)
        self.refresh()

    def _on_add(self) -> None:
        """Add a gap into the one group picked here.

        Off while several are picked: a new gap belongs to one batch, and
        silently choosing the first of five would be a worse answer than
        asking you to narrow it down."""
        names = self.selected_names()
        if len(names) != 1:
            tooltip("Pick one group to add into.")
            return
        self._panel._on_add_kg(group=names[0])
        self.refresh()

    def _on_done(self) -> None:
        ids = self._ids()
        if not ids:
            return
        self._panel._bulk_set_status(ids, "done")
        self.refresh()

    def _on_archive(self) -> None:
        names = [n for n in self.selected_names() if n]
        if not names:
            return
        archived = set(self._panel._archived_groups())
        if len(names) == 1 and names[0] in archived:
            self._panel._set_group_archived(names[0], False)
        else:
            self._panel._archive_groups([n for n in names
                                         if n not in archived])
        self.refresh()


class KnowledgeGapsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter = "open"
        self._build()
        self.refresh_list()
        # Groups first: it's the page you work from, and the gap list is a
        # click away.
        self._set_view("groups")

    def _build_corner(self, row) -> None:
        """The rest of Ankisstant, small, in the top-right corner.

        There is no sidebar any more: this page IS Ankisstant, and the other
        pages are places you visit and come back from (each has a ← Gaps link).
        AI Browse and AI Create are gone as pages — the queue does both."""
        css = ("QPushButton { padding: 3px 10px; font-size: 11px; "
               "border: 1px solid rgba(127,127,127,0.3); border-radius: 5px; "
               "background: transparent; }"
               "QPushButton:hover { background: rgba(127,127,127,0.12); }")
        try:
            from ..ui.main_window import _tool_visible
        except Exception:
            def _tool_visible(_k):
                return True
        row.addSpacing(10)
        for key, text, tip in (
            ("lecture", "AI Lecture",
             "Find & write from a lecture, a document, screenshots, pasted "
             "text or a web page — sections instead of gaps, with the slides "
             "attached to the cards."),
            ("qbank", "AI QBank", "Question banks and captured misses."),
        ):
            if not _tool_visible(key):
                continue
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.setToolTip(tip)
            b.setStyleSheet(css)
            b.clicked.connect(lambda _c=False, k=key: self._open_page(k))
            row.addWidget(b)
        gear = QPushButton("Settings")
        gear.setAutoDefault(False)
        gear.setToolTip("Ankisstant settings")
        gear.setStyleSheet(css)
        gear.clicked.connect(self._open_settings)
        row.addWidget(gear)
        try:
            from ..core import synapse as _syn
            dockable = _syn.synapse_available()
        except Exception:
            dockable = False
        if dockable:
            dock = QPushButton("⧉")
            dock.setAutoDefault(False)
            dock.setToolTip("Move between SynapsePro's side panel and a window "
                            "of its own.")
            dock.setStyleSheet(css)
            dock.clicked.connect(self._toggle_dock)
            row.addWidget(dock)

    def _open_page(self, key: str) -> None:
        body = _queue_owner(self)
        if body is not None and hasattr(body, "show_page"):
            body.show_page(key)

    def _open_settings(self) -> None:
        body = _queue_owner(self)
        if body is not None and hasattr(body, "_open_settings"):
            body._open_settings()
            return
        from ..ui.settings import open_settings
        open_settings()

    def _toggle_dock(self) -> None:
        body = _queue_owner(self)
        if body is not None and hasattr(body, "_request_popout"):
            body._request_popout()

    def showEvent(self, ev):
        super().showEvent(ev)
        # The queue can be added to from the Groups page, or emptied from the
        # dashboard, while this page is off screen.
        self.refresh_work_badge()
        # Tag completer on the detail pane's tag field is stale after a
        # session of adding new tags; rebuild on every show.
        try:
            attach_tag_completer(self._detail._tags_input, multi=True)
        except Exception as e:
            print(f"[ankisstant] KG tag completer refresh failed: {e}")

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        self._setup_banner = make_setup_banner(self)
        root.addWidget(self._setup_banner)
        self.refresh_setup_banner()

        # Title row
        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>Knowledge Gaps</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        # View switch. Groups used to be a horizontal chip row, which stopped
        # being readable at about eight of them and could only ever filter this
        # list — they get a page of their own instead.
        self._view = "gaps"
        self._view_buttons: dict[str, QPushButton] = {}
        # Deliberately bigger than the filter chips below them: these switch the
        # whole page, the chips only narrow one list, and a switch that reads
        # like a filter gets missed. The checked one is filled AND bold — the
        # state is never carried by colour alone.
        from ..core import synapse as _syn
        _sel_bg = _syn.color("blue", "palette(highlight)")
        _sel_fg = _syn.color("surface", "palette(highlighted-text)")
        _hover = _syn.color("hover_subtle", "rgba(127,127,127,0.12)")
        _view_css = (
            "QPushButton { padding: 6px 20px; font-size: 13px; "
            "border: 1px solid rgba(127,127,127,0.35); "
            "border-radius: 6px; background: transparent; }"
            f"QPushButton:checked {{ background: {_sel_bg}; color: {_sel_fg}; "
            "font-weight: bold; }"
            f"QPushButton:hover:!checked {{ background: {_hover}; }}")
        for _key, _text, _tip in (
            ("gaps", "📋 Gaps", "Every gap, in group sections."),
            ("groups", "🗂 Groups",
             "Your groups: what each one is tagged as, whether it's open or "
             "closed, and every gap in it."),
        ):
            _b = QPushButton(_text)
            _b.setCheckable(True)
            _b.setAutoDefault(False)
            _b.setMinimumHeight(30)
            _b.setToolTip(_tip)
            _b.setStyleSheet(_view_css)
            _b.clicked.connect(lambda _c=False, k=_key: self._set_view(k))
            self._view_buttons[_key] = _b
            title_row.addWidget(_b)
        self._view_buttons["gaps"].setChecked(True)
        title_row.addWidget(make_help_button(
            "Knowledge Gaps — help",
            "<h3>What it does</h3>"
            "<p>One unified queue of things you don't know — from any source: "
            "manual notes, captured QBank misses, or things Browse couldn't find.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Add a KG via <b>＋ Add KG</b> (here or from the home screen).</li>"
            "<li><b>Highlight</b> the gaps you want — click, Shift-click a "
            "run, Cmd-click to add one, or press <i>Select all</i>. Cmd-click a "
            "group header to act on the whole group. The <b>tick box</b> is not "
            "the selection: ticking a gap CLOSES it, which is also what a "
            "finished Find & write pass does to it.</li>"
            "<li>Press <b>Send to queue</b> to park them. Send as many as you "
            "like, from either page; they wait there, and they survive "
            "quitting Anki.</li>"
            "<li>Press <b>▶ Run queue</b> (bottom right) to Find &amp; write "
            "the whole queue in one pass — your deck is searched for each gap "
            "AND cards are written for whatever nothing covers. Want only one "
            "half? Untick Find or Write for that gap on the plan page. "
            "Right-click ▶ Run queue to clear the queue instead.</li>"
            "<li><b>AI Lecture</b> (top right) takes a lecture, a document, "
            "screenshots, pasted text or a web page and runs it through the "
            "same Find &amp; write screen.</li>"
            "<li>The <b>🏷</b> and <b>🔎</b> buttons open Anki's own Browse "
            "screen on the gap's title — as a tag search and a card search.</li>"
            "<li>For <b>LO</b>-type KGs, the detail pane shows an inline "
            "<i>Analyse this LO</i> section that pulls cards under the LO's tag "
            "and flags concepts they don't cover — accepted gaps append into "
            "the LO's Notes.</li>"
            "<li>When a card is added (or matching cards re-graded), the KG "
            "is closed automatically.</li>"
            "</ol>",
            self,
        ))
        self._build_corner(title_row)
        root.addLayout(title_row)

        intro = QLabel(
            "Every gap from every source. Pick the ones you want, press Send "
            "to queue to park them, then press ▶ Run queue in the bottom "
            "right."
        )
        intro.setStyleSheet("color: gray; font-size: 11px;")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Filter chips — two rows: status filters on top, type filters below.
        self._chip_buttons: dict[str, QPushButton] = {}
        status_row = QHBoxLayout()
        status_row.setSpacing(4)
        for entry in _STATUS_FILTERS:
            self._add_chip(status_row, entry)
        status_row.addStretch(1)
        # Both filter rows live in one wrapper so the Groups page can hide them
        # in a single call: they filter the gap list, and the gap list is not on
        # screen there.
        self._filters_wrap = QWidget()
        fcol = QVBoxLayout(self._filters_wrap)
        fcol.setContentsMargins(0, 0, 0, 0)
        fcol.setSpacing(4)
        fcol.addLayout(status_row)
        # Types row is rebuilt on every settings save (see refresh_chips).
        self._type_chip_row_holder = QHBoxLayout()
        fcol.addLayout(self._type_chip_row_holder)
        root.addWidget(self._filters_wrap)
        self._rebuild_type_chip_row()
        # Groups are derived from the gaps themselves rather than settings, so
        # the Groups page is rebuilt whenever the live set changes — see
        # refresh_list and _on_groups_changed.
        self._known_groups: list[str] = []
        # Cmd-clicked group headers. Deliberately NOT the list's selection model
        # (headers are unselectable, which is what keeps the gap-level bulk
        # actions honest) — the panel owns this and the headers render it.
        self._marked_groups: set[str] = set()
        self._last_marked: str | None = None
        # The Closed gaps, by id — the tri-state for the group headers, which
        # have to answer for gaps that are collapsed out of sight. Rebuilt from
        # the store on every refresh_list; the STATE lives in kg_queue.json,
        # not here. A gap's own row reads its own status (_KGListItem.refresh).
        self._done_ids: set[str] = set()
        # Set while tick state is being written programmatically, so the
        # itemChanged handler doesn't recurse through its own writes.
        self._tick_guard = False
        # A click that landed on a header's tick box must not also collapse the
        # group. Qt toggles the check state before it emits itemClicked, so the
        # tick handler can veto the click that is about to arrive.
        self._suppress_next_click = False
        # Visible gap ids per group, filled by refresh_list. A header tick ticks
        # these — read from here rather than from the rows, so ticking a
        # COLLAPSED group still ticks the gaps inside it.
        self._group_members: dict[str, list[str]] = {}
        self._archived_set: set[str] = set(self._archived_groups())
        if self._filter in self._chip_buttons:
            self._chip_buttons[self._filter].setChecked(True)

        # Splitter: list / detail
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Left side
        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 6, 0)
        lcol.setSpacing(4)
        # With the chip row gone, nothing else says the list is showing one
        # group — so the list says it itself, with the way out beside it.
        self._group_filter_row = QWidget()
        gfr = QHBoxLayout(self._group_filter_row)
        gfr.setContentsMargins(0, 0, 0, 0)
        gfr.setSpacing(6)
        self._group_filter_lbl = QLabel("")
        self._group_filter_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._group_filter_lbl.setStyleSheet("font-size: 11px;")
        self._group_filter_lbl.setWordWrap(True)
        gfr.addWidget(self._group_filter_lbl, 1)
        gf_clear = QPushButton("Show all")
        gf_clear.setAutoDefault(False)
        gf_clear.setStyleSheet("font-size: 11px; padding: 1px 8px;")
        gf_clear.clicked.connect(lambda: self.set_filter("all"))
        gfr.addWidget(gf_clear)
        self._group_filter_row.setVisible(False)
        lcol.addWidget(self._group_filter_row)
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("color: gray; font-size: 11px;")
        lcol.addWidget(self._count_lbl)
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        self._list.itemSelectionChanged.connect(self._on_select)
        self._list.setItemDelegate(_KGItemDelegate(self._list))
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemChanged.connect(self._on_item_checked)
        lcol.addWidget(self._list, 1)
        tick_row = QHBoxLayout()
        tick_row.setSpacing(4)
        for _text, _on in (("Select all", True), ("Clear selection", False)):
            _b = QPushButton(_text)
            _b.setAutoDefault(False)
            _b.setStyleSheet("font-size: 10px; padding: 1px 8px;")
            _b.clicked.connect(lambda _c=False, v=_on: self._select_all(v))
            tick_row.addWidget(_b)
        self._tick_lbl = QLabel("")
        self._tick_lbl.setStyleSheet("color: gray; font-size: 10px;")
        tick_row.addWidget(self._tick_lbl)
        tick_row.addStretch(1)
        lcol.addLayout(tick_row)
        sel_hint = QLabel(
            "The tick box closes a gap — tick it when you're done with it. "
            "Tick a group header to close everything in it. The buttons act on "
            "the HIGHLIGHTED rows: click, Shift-click a run, Cmd-click to add "
            "one, or press Select all. Cmd-click a group header to act on the "
            "whole group, collapsed or not."
        )
        sel_hint.setStyleSheet("color: gray; font-size: 10px;")
        sel_hint.setWordWrap(True)
        lcol.addWidget(sel_hint)
        # Wraps (FlowLayout): as one fixed row these five set the list's
        # minimum width, which in a narrow window starved the gap pane.
        btn_row_w = QWidget()
        btn_row = FlowLayout(btn_row_w)
        add_btn = QPushButton("＋ Add KG")
        add_btn.setAutoDefault(False)
        add_btn.setToolTip(
            "Add one gap (⌘N). Prefilled with the group you are working in — "
            "the one you have picked, marked, or filtered to.")
        # Lambda, not the bound method: clicked() passes `checked`, which would
        # land in `group` and quietly kill the prefill.
        add_btn.clicked.connect(lambda: self._on_add_kg())
        import_btn = QPushButton("⇪ Bulk import")
        import_btn.setAutoDefault(False)
        import_btn.setToolTip(
            "Add many gaps at once — pasted one per line, as markdown with "
            "groups and notes, from a PDF, or straight from Things.")
        import_btn.clicked.connect(self._on_bulk_import)
        # A direct route for the source used most often, so the common case
        # isn't a dialog you have to open and then press a second button in.
        self._things_btn = None
        if importers.available():
            self._things_btn = QPushButton("⇪ From Things")
            self._things_btn.setAutoDefault(False)
            self._things_btn.setToolTip(
                "Pull in every Things to-do tagged “KG”. Each one's project "
                "becomes its group; its notes and checklist come with it. "
                "Already-imported gaps are skipped, and nothing in Things is "
                "changed.")
            self._things_btn.clicked.connect(self._on_import_things)
        refresh_btn = QPushButton("⟳")
        compact_button(refresh_btn)
        refresh_btn.setToolTip("Refresh")
        refresh_btn.setAutoDefault(False)
        refresh_btn.clicked.connect(lambda: self.refresh_list())
        btn_row.addWidget(add_btn)
        btn_row.addWidget(import_btn)
        if self._things_btn is not None:
            btn_row.addWidget(self._things_btn)
        # Capture from SynapsePro's AI chat. This is the robust half of that
        # feature — it reads one private global, where the button injected into
        # the chat header needs four — so it stays even if a SynapsePro update
        # renames a seam and the injected button quietly stops appearing.
        # Built only when SynapsePro is actually there; without it the button
        # would be permanently dead with nothing explaining why.
        self._chat_btn = None
        try:
            from ..core import synapse_chat
            if synapse_chat.chat_available():
                self._chat_btn = QPushButton("＋ KG from AI chat")
                self._chat_btn.setAutoDefault(False)
                self._chat_btn.setToolTip(
                    "Condense the latest answer in SynapsePro's AI chat into "
                    "knowledge gaps and add them here."
                )
                self._chat_btn.clicked.connect(self._on_kg_from_chat)
                btn_row.addWidget(self._chat_btn)
        except Exception as e:
            print(f"[ankisstant] chat KG button unavailable: {e}")
        btn_row.addWidget(refresh_btn)
        lcol.addWidget(btn_row_w)

        # Clear-completed row — sits below add/analyse so it's out of the way.
        clear_row_w = QWidget()
        clear_row = FlowLayout(clear_row_w)
        self._clear_btn = QPushButton("Clear completed (0)")
        self._clear_btn.setAutoDefault(False)
        self._clear_btn.setStyleSheet("font-size: 11px;")
        self._clear_btn.setToolTip(
            "Remove every Closed KG. Open ones stay."
        )
        self._clear_btn.clicked.connect(self._on_clear_completed)
        clear_row.addWidget(self._clear_btn)
        if importers.available():
            sync_btn = QPushButton("↻ Sync done → Things")
            sync_btn.setAutoDefault(False)
            sync_btn.setStyleSheet("font-size: 11px;")
            sync_btn.setToolTip(
                "Tick off, in Things, every imported gap you've finished here. "
                "Shows you the list first. Runs by itself whenever a gap is "
                "completed — this is for the backlog, or if you turned that off.")
            sync_btn.clicked.connect(lambda: sync_done_to_things(parent=self))
            clear_row.addWidget(sync_btn)
        lcol.addWidget(clear_row_w)
        splitter.addWidget(left)

        # Right side
        right_wrap = QScrollArea()
        right_wrap.setWidgetResizable(True)
        right_wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        self._detail = KGDetailPane(self)
        right_wrap.setWidget(self._detail)
        splitter.addWidget(right_wrap)

        # Close to half each. The list used to be held wide by its fixed
        # button row; with that row wrapping, [280, 620] left room for three
        # gaps and stacked the buttons five deep in a narrow window.
        splitter.setSizes([460, 540])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        # Two pages: the gaps, and the groups.
        self._stack = QStackedWidget()
        self._stack.addWidget(splitter)
        self._groups_view = _GroupsView(self)
        self._stack.addWidget(self._groups_view)
        root.addWidget(self._stack, 1)

        # The queue, bottom right of the whole page — OUTSIDE the stack, so it
        # is in the same corner whichever page you are on. It is the one
        # control here that isn't about the gap in front of you: it is
        # everything you've parked, and pressing it works through the lot.
        queue_row = QHBoxLayout()
        queue_row.addStretch(1)
        self._work_queue_btn = QPushButton("▶ Run queue")
        self._work_queue_btn.setAutoDefault(False)
        self._work_queue_btn.setMinimumHeight(30)
        self._work_queue_btn.setStyleSheet(
            "QPushButton { padding: 5px 16px; font-size: 12px; "
            "border: 1px solid rgba(127,127,127,0.35); border-radius: 6px; }")
        self._work_queue_btn.clicked.connect(self._run_work_queue)
        self._work_queue_btn.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._work_queue_btn.customContextMenuRequested.connect(
            self._work_queue_menu)
        queue_row.addWidget(self._work_queue_btn)
        root.addLayout(queue_row)
        self.refresh_work_badge()

        # ⌘N / Ctrl+N adds a gap, from either page. Scoped to this widget and
        # its children rather than the window, so it can't fire while another
        # Ankisstant tool is on screen. StandardKey.New rather than a literal
        # "Ctrl+N" so it is ⌘N on his Mac and Ctrl+N everywhere else.
        self._add_shortcut = QShortcut(
            QKeySequence(QKeySequence.StandardKey.New), self)
        self._add_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._add_shortcut.activated.connect(lambda: self._on_add_kg())

    # ── the two pages ────────────────────────────────────────────────────────

    def _set_view(self, key: str) -> None:
        self._view = key if key in self._view_buttons else "gaps"
        for k, b in self._view_buttons.items():
            b.setChecked(k == self._view)
        self._filters_wrap.setVisible(self._view == "gaps")
        self._stack.setCurrentIndex(1 if self._view == "groups" else 0)
        if self._view == "groups":
            self._groups_view.refresh()

    def jump_to_gap(self, kg_id: str) -> None:
        """Show one gap on the Gaps page, whatever page you were on and
        whatever filter is set. A gap opened from the Groups page is usually
        done or dismissed, so the filter is widened rather than leaving you on
        an empty list wondering where it went."""
        self._set_view("gaps")
        # A gap inside a collapsed section has no row to select, so its section
        # is opened before we go looking for it.
        grp = str((kg_store.get(kg_id) or {}).get("group") or "").strip()
        if grp in set(self._collapsed_groups()):
            self._set_group_collapsed(grp, False)
        self.refresh_list(preserve_selection=kg_id)
        it = self._row_for(kg_id)
        if it is None:
            self.set_filter("all")
            self.refresh_list(preserve_selection=kg_id)
            it = self._row_for(kg_id)
        if it is not None:
            self._list.setCurrentItem(it)
            self._list.scrollToItem(it)

    def _row_for(self, kg_id: str):
        for i in range(self._list.count()):
            it = self._list.item(i)
            if isinstance(it, _KGListItem) and it.kg_id == kg_id:
                return it
        return None

    def _refresh_group_filter_banner(self) -> None:
        on = self._filter.startswith("group:")
        if on:
            name = self._filter.split(":", 1)[1]
            self._group_filter_lbl.setText(
                "Showing one group: <b>"
                + _html.escape(name or NO_GROUP) + "</b>")
        self._group_filter_row.setVisible(on)

    # ── chip rows ────────────────────────────────────────────────────────────

    def _add_chip(self, row, entry) -> None:
        # entry = (key, label)  OR (key, label, color)
        if len(entry) == 3:
            key, label, color = entry
        else:
            key, label = entry
            color = None
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setAutoExclusive(True)
        btn.setMinimumHeight(24)
        if color:
            btn.setStyleSheet(
                f"QPushButton {{ padding: 2px 10px; font-size: 11px; "
                f"border: 1px solid {color}; border-radius: 12px; "
                f"background: transparent; color: {color}; }}"
                f"QPushButton:checked {{ background: {color}; color: white; }}"
                f"QPushButton:hover:!checked {{ background: rgba(127,127,127,0.12); }}"
            )
        else:
            # The chips that aren't a KG *type* (All, Open, …) have no colour of
            # their own, so they take the app accent. Under SynapsePro that's its
            # accent rather than Anki's highlight; the type chips above keep the
            # user's chosen colours untouched — those are data, not chrome.
            from ..core import synapse
            sel_bg = synapse.color("blue", "palette(highlight)")
            sel_fg = synapse.color("surface", "palette(highlighted-text)")
            hover = synapse.color("hover_subtle", "rgba(127,127,127,0.12)")
            btn.setStyleSheet(
                "QPushButton { padding: 2px 10px; font-size: 11px; "
                "border: 1px solid rgba(127,127,127,0.3); border-radius: 12px; "
                "background: transparent; }"
                f"QPushButton:checked {{ background: {sel_bg}; color: {sel_fg}; }}"
                f"QPushButton:hover:!checked {{ background: {hover}; }}"
            )
        btn.clicked.connect(lambda _c=False, k=key: self._set_filter(k))
        row.addWidget(btn)
        self._chip_buttons[key] = btn

    def _rebuild_type_chip_row(self) -> None:
        # Clear out any existing type chips first.
        for key in list(self._chip_buttons.keys()):
            if key.startswith("type:"):
                btn = self._chip_buttons.pop(key)
                btn.setParent(None)
                btn.deleteLater()
        # Strip the prior row's items.
        while self._type_chip_row_holder.count():
            item = self._type_chip_row_holder.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # Build fresh row.
        row = QHBoxLayout()
        row.setSpacing(4)
        prefix_label = QLabel("Types:")
        prefix_label.setStyleSheet("color: gray; font-size: 11px;")
        # Wrap in a widget so we can put it inside the holder layout.
        from aqt.qt import QWidget as _QW
        wrapper = _QW()
        wrapper.setLayout(row)
        row.addWidget(prefix_label)
        for t in _load_types():
            self._add_chip(row, (f"type:{t['key']}", t["name"], t["color"]))
        row.addStretch(1)
        self._type_chip_row_holder.addWidget(wrapper)

    # ── filters / list ───────────────────────────────────────────────────────

    def refresh_setup_banner(self) -> None:
        try:
            ok = provider_configured()
            self._setup_banner.setVisible(not ok)
            analyse_btn = getattr(
                getattr(self._detail, "_lo_analyser", None), "_analyse_btn", None
            )
            set_ai_buttons_enabled([analyse_btn], ok)
        except Exception:
            pass

    def set_filter(self, key: str) -> None:
        """Programmatic filter switch (used by QBank's Review button)."""
        # Translate legacy/short keys: source names → type keys, where it
        # makes sense.
        translated = {
            "qbank":   "type:mq",
            "analyse": "type:lo",
            "manual":  "type:kg",
            "browse":  "all",
        }.get(key, key)
        if translated not in self._chip_buttons:
            return
        self._chip_buttons[translated].setChecked(True)
        self._set_filter(translated)

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self.refresh_list()

    def _matches_filter(self, kg: dict) -> bool:
        f = self._filter
        # Archived groups are off the list page entirely, whatever else is
        # selected — the one exception being an explicit filter on that group,
        # which is how the Groups manager's "Open in list" looks inside one.
        grp = str(kg.get("group") or "").strip()
        if grp and grp in self._archived_set and f != f"group:{grp}":
            return False
        # Closed is status == done, and Open is everything else — in_progress
        # included, because a gap handed to a tool is still one you haven't
        # closed, and sending it somewhere must not make it vanish from under
        # you. Dismissed doesn't enter into it any more: it is just Closed.
        status = kg.get("status", "open")
        if f == "open":
            return status != "done"
        if f == "done":
            return status == "done"
        if f.startswith("type:"):
            return kg.get("type", "kg") == f.split(":", 1)[1]
        if f.startswith("group:"):
            return str(kg.get("group") or "").strip() == f.split(":", 1)[1]
        return True

    def refresh_list(self, preserve_selection: str | None = None) -> None:
        # Anything unsaved in the detail pane goes to disk before we re-read it,
        # or the rebuild would show (and then reload) the pre-edit record.
        self._detail.flush()
        # Read once per rebuild, not once per gap: _matches_filter runs over the
        # whole store and tool_config is a dict walk each time.
        self._archived_set = set(self._archived_groups())
        items = kg_store.load_all()
        # Active items above done; newest first within each bucket.
        # Python's sort is stable so we can apply secondary then primary.
        status_order = {"open": 0, "in_progress": 0, "done": 1}
        items.sort(key=lambda k: k.get("created_at", ""), reverse=True)
        items.sort(key=lambda k: status_order.get(k.get("status", "open"), 4))

        visible = [k for k in items if self._matches_filter(k)]
        self._count_lbl.setText(
            f"{len(visible)} shown · {len(items)} total"
        )
        done_count = sum(1 for k in items if k.get("status") == "done")
        self._clear_btn.setText(f"Clear completed ({done_count})")
        self._clear_btn.setEnabled(done_count > 0)

        prev_id = preserve_selection
        # The WHOLE selection, not just the current row. Every bulk action ends
        # in a rebuild, and a rebuild that keeps one id collapsed eleven
        # selected gaps down to one — so the second half of "mark these Done,
        # then dismiss them" quietly acted on a single gap, and the pane's
        # labels went back to saying "Status:" as though nothing were selected.
        prev_ids = [i.kg_id for i in self._list.selectedItems()
                    if isinstance(i, _KGListItem) and i.kg_id]
        if prev_id is None:
            sel = self._list.currentItem()
            if isinstance(sel, _KGListItem):
                prev_id = sel.kg_id

        # Keep the group chips in step with what actually exists. Rebuilt only
        # when the set changes, and deferred a tick: refresh_list can be called
        # FROM a chip's own clicked handler, and tearing that button down inside
        # its own signal is a crash rather than a bug.
        live_groups = [g for g in kg_store.groups(items)
                       if g not in self._archived_set
                       or self._filter == f"group:{g}"]
        if live_groups != self._known_groups:
            self._known_groups = live_groups
            QTimer.singleShot(0, self._on_groups_changed)
        self._refresh_group_filter_banner()
        if self._view == "groups":
            self._groups_view.refresh()

        collapsed = set(self._collapsed_groups())
        self._list.blockSignals(True)
        self._list.clear()
        target_row = -1
        row_of: dict[str, int] = {}
        sections = self._sections(visible)
        # A group that is no longer on the page can't stay selected — otherwise
        # a filter change leaves an invisible group in the next batch.
        self._marked_groups &= {name for name, _ in sections}
        if self._last_marked not in self._marked_groups:
            self._last_marked = None
        self._group_members = {
            name: [str(k.get("id") or "") for k in members if k.get("id")]
            for name, members in sections
        }
        # Which gaps are Closed, for the group headers' tick boxes. From the
        # WHOLE store, not the page: a collapsed group still has to know, and
        # the header is the only thing that can say so while it is shut.
        self._done_ids = {str(k.get("id") or "") for k in items
                          if k.get("status") == "done"}
        for name, members in sections:
            is_collapsed = name in collapsed
            open_n = sum(1 for k in members if k.get("status") == "open")
            done_n = sum(1 for k in members if k.get("status") == "done")
            self._list.addItem(
                _KGGroupHeader(name, len(members), open_n, is_collapsed,
                               done_n=done_n,
                               archived=name in self._archived_set,
                               marked=name in self._marked_groups,
                               checked={
                                   Qt.CheckState.Checked: "on",
                                   Qt.CheckState.PartiallyChecked: "part",
                               }.get(self._header_state(name), "off")))
            if is_collapsed:
                continue
            for kg in members:
                # The row sets its own box from the gap's status (see
                # _KGListItem.refresh) — nothing to restore here.
                li = _KGListItem(kg)
                self._list.addItem(li)
                row_of[str(kg.get("id") or "")] = self._list.count() - 1
                if prev_id and kg.get("id") == prev_id:
                    target_row = self._list.count() - 1
        self._list.blockSignals(False)

        if target_row >= 0:
            self._list.setCurrentRow(target_row)
            # _on_select deliberately skips reloading the gap already in the
            # pane (so it can't clobber a live edit). A list rebuild means the
            # record may have changed underneath it, so reload it here.
            li = self._list.item(target_row)
            if isinstance(li, _KGListItem):
                self._detail.load(kg_store.get(li.kg_id))
        else:
            # Row 0 is a group header now, and headers aren't selectable — so
            # fall to the first actual gap rather than to a row that can't take
            # a selection (which would leave the detail pane blank).
            first = next((i for i in range(self._list.count())
                          if isinstance(self._list.item(i), _KGListItem)), -1)
            if first >= 0:
                self._list.setCurrentRow(first)
            else:
                self._detail.load(None)

        # Put the rest of the selection back. Anything the filter has just
        # hidden (the gaps you marked Done, on a page that hides Done) simply
        # isn't there to reselect, which is the right answer — the count in the
        # pane's labels then tells you so rather than over-promising.
        want = set(prev_ids) | ({prev_id} if prev_id else set())
        cur = self._list.currentRow()
        extra = sorted({row_of[k] for k in want if k in row_of} - {cur})
        if extra:
            self._list.blockSignals(True)
            for r in extra:
                it = self._list.item(r)
                if it is not None:
                    it.setSelected(True)
            self._list.blockSignals(False)
        # The pane's labels name the selection ("Status of 11:"). Left unsaid,
        # they went on claiming eleven after a rebuild had reduced it.
        self._after_selection_changed()
        # A gap deleted, imported or closed changes what is queued too.
        self.refresh_work_badge()

    # ── groups ───────────────────────────────────────────────────────────────

    def _sections(self, visible: list[dict]) -> list[tuple[str, list[dict]]]:
        """Split the visible gaps into (group name, members), groups first in
        natural order and the ungrouped ones last.

        Ungrouped last on purpose: they're the inbox — things not yet filed —
        and burying the filed batches under a long unsorted pile is exactly the
        problem groups exist to solve. Within a section the incoming order is
        preserved, so the status/date sort from refresh_list still holds."""
        buckets: dict[str, list[dict]] = {}
        for kg in visible:
            buckets.setdefault(str(kg.get("group") or "").strip(), []).append(kg)
        named = sorted((g for g in buckets if g), key=kg_store.group_sort_key)
        out = [(g, buckets[g]) for g in named]
        if "" in buckets:
            out.append(("", buckets[""]))
        return out

    def _collapsed_groups(self) -> list[str]:
        try:
            raw = tool_config("knowledge_gaps").get("collapsed_groups") or []
            return [str(g) for g in raw]
        except Exception:
            return []

    def _set_group_collapsed(self, name: str, collapsed: bool) -> None:
        """Persist one group's collapse state.

        Read-modify-write of the whole tool slice, the house pattern here — it
        must never drop the other knowledge_gaps settings on the floor."""
        try:
            cfg = tool_config("knowledge_gaps")
            current = [str(g) for g in (cfg.get("collapsed_groups") or [])]
            if collapsed and name not in current:
                current.append(name)
            elif not collapsed and name in current:
                current = [g for g in current if g != name]
            cfg["collapsed_groups"] = current
            save_tool_config("knowledge_gaps", cfg)
        except Exception as e:
            # A failed save costs the collapse state, not the click.
            print(f"[ankisstant] couldn't save collapse state: {e}")

    def _on_item_clicked(self, item) -> None:
        # The click that toggled a tick box is not also a click on the row.
        # Without this, ticking a group folded it shut in the same gesture.
        if self._suppress_next_click:
            self._suppress_next_click = False
            return
        if not isinstance(item, _KGGroupHeader):
            return
        mods = QApplication.keyboardModifiers()
        # Qt reports Cmd as ControlModifier on macOS, so this is Cmd-click here
        # and Ctrl-click elsewhere — the same "add to the selection" gesture the
        # list already uses for gaps.
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._toggle_group_mark(item.group_name)
            return
        if mods & Qt.KeyboardModifier.ShiftModifier and self._last_marked is not None:
            self._extend_group_marks(item.group_name)
            return
        # A plain click is the reset — it drops any group selection and then
        # does exactly what it always did.
        if self._marked_groups:
            self._marked_groups.clear()
            self._last_marked = None
        self._on_collapse_clicked(item)

    def _on_collapse_clicked(self, header) -> None:
        self._set_group_collapsed(header.group_name, not header.collapsed)
        self.refresh_list()

    # ── closing, and choosing what to act on ─────────────────────────────────

    def _on_item_checked(self, item) -> None:
        """A tick box changed: close, or reopen, what it belongs to.

        Deferred a tick before it writes. The write ends in refresh_list(),
        which clears the list — and clearing a list inside a signal emitted BY
        one of its own items tears down the object mid-signal. The same reason
        _on_groups_changed is deferred a few lines up."""
        if self._tick_guard:
            return
        if isinstance(item, _KGGroupHeader):
            # Part-ticked is a state a header REPORTS, never one you click
            # into: clicking a part-closed group closes the rest of it.
            closed = item.checkState() != Qt.CheckState.Unchecked
            self._suppress_next_click = True
            ids = list(self._group_members.get(item.group_name, []))
        elif isinstance(item, _KGListItem) and item.kg_id:
            closed = item.checkState() == Qt.CheckState.Checked
            ids = [item.kg_id]
        else:
            return
        if not ids:
            return
        # A header's box closes a whole group, and it sits one slip away from
        # the collapse arrow. Asked only when it would close a batch big enough
        # to be annoying to put back — never when reopening, which loses
        # nothing.
        if closed and isinstance(item, _KGGroupHeader) and len(ids) > 8:
            n_open = sum(1 for i in ids if i not in self._done_ids)
            if n_open > 8 and not askUser(
                    f"Close all {n_open} open gap(s) in "
                    f"“{item.group_name or NO_GROUP}”?\n\n"
                    "Untick the header to put them all back."):
                self.refresh_list()
                return
        QTimer.singleShot(0, lambda: self.set_gaps_closed(ids, closed))

    def _select_all(self, on: bool) -> None:
        """Highlight every gap row on the page, or drop the highlight.

        This is how a batch is built now that the tick box means Closed. Rows
        only: a header cannot be highlighted, and a COLLAPSED group has no rows
        at all — Cmd-clicking its header is what covers that (see
        _toggle_group_mark, and _selected_ids which reads both)."""
        self._list.clearSelection()
        if on:
            for li in _gap_items(self._list):
                li.setSelected(True)
        self._after_selection_changed()

    def _header_state(self, name: str):
        """A group is ticked when all its gaps are Closed, part-ticked when
        some are.

        Read from the gaps rather than stored, so it can never disagree with
        the rows underneath it — including when they are collapsed out of
        sight, which is why it reads the id set and not the items."""
        ids = self._group_members.get(name, [])
        if not ids:
            return Qt.CheckState.Unchecked
        done = getattr(self, "_done_ids", set())
        n = sum(1 for i in ids if i in done)
        if not n:
            return Qt.CheckState.Unchecked
        return (Qt.CheckState.Checked if n == len(ids)
                else Qt.CheckState.PartiallyChecked)

    def _after_selection_changed(self) -> None:
        """Say how many gaps are selected, and re-label the pane to match."""
        n = len(self._selected_ids())
        lbl = getattr(self, "_tick_lbl", None)
        if lbl is not None:
            lbl.setText("" if not n else f"{n} selected" if n > 1
                        else "1 selected")
        try:
            self._detail.refresh_send_labels(n)
        except Exception as e:
            print(f"[ankisstant] couldn't relabel the pane: {e}")

    # ── selecting several groups ───────────────────────────────────────────────

    def _header_items(self) -> list:
        """The group headers currently on the page, in page order."""
        return [it for it in (self._list.item(i)
                              for i in range(self._list.count()))
                if isinstance(it, _KGGroupHeader)]

    def _toggle_group_mark(self, name: str) -> None:
        if name in self._marked_groups:
            self._marked_groups.discard(name)
            if self._last_marked == name:
                self._last_marked = None
        else:
            self._marked_groups.add(name)
            self._last_marked = name
        self.refresh_list()

    def _extend_group_marks(self, name: str) -> None:
        """Shift-click: mark every group between the last one marked and this
        one, in the order they sit on the page."""
        order = [h.group_name for h in self._header_items()]
        if self._last_marked not in order or name not in order:
            self._toggle_group_mark(name)
            return
        a, b = sorted((order.index(self._last_marked), order.index(name)))
        self._marked_groups.update(order[a:b + 1])
        self._last_marked = name
        self.refresh_list()

    def _clear_group_marks(self) -> None:
        self._marked_groups.clear()
        self._last_marked = None
        self.refresh_list()

    def _on_groups_changed(self) -> None:
        """The set of live groups changed. Deferred a tick by every caller:
        refresh_list can run from inside a widget's own signal handler, and
        tearing that widget down there is a crash rather than a bug."""
        if not self._known_groups and self._filter.startswith("group:"):
            # The group being filtered to has just stopped existing (emptied, or
            # archived from the manager). Fall back to All rather than showing
            # an empty list with no way to tell why.
            self._set_filter("all")
            if "all" in self._chip_buttons:
                self._chip_buttons["all"].setChecked(True)
        self._refresh_group_filter_banner()
        if self._view == "groups":
            self._groups_view.refresh()

    def _group_ids(self, name: str) -> list[str]:
        """Every KG id in a group — the whole group, not just the rows the
        current filter happens to be showing. A group action reading "12 gaps"
        off the header must act on those 12."""
        return [str(kg.get("id") or "")
                for kg in kg_store.in_group(name) if kg.get("id")]

    def _live_ids(self, ids: list[str]) -> list[str]:
        """The gaps in a group selection that a RUN should actually touch.

        Sending a group to work-through means "do what's left of it", not "redo
        the ones I already closed" — a Done gap has had its cards found or
        written, and re-reading it spends a Read call and a Find pass to
        rediscover that. So the run and send actions act on the not-Done gaps.

        Edit actions (regroup, retag, archive, mark done) still act on the WHOLE
        group: those mean the group's identity, not its remaining work. That
        asymmetry is the point of having this as a separate helper rather than
        filtering inside _group_ids."""
        out: list[str] = []
        for i in ids:
            kg = kg_store.get(i) or {}
            if str(kg.get("status") or "open") != "done":
                out.append(i)
        return out

    def _assign_group(self, ids: list[str]) -> None:
        if not ids:
            return
        current = ""
        if len(ids) == 1:
            current = str((kg_store.get(ids[0]) or {}).get("group") or "")
        else:
            # Prefill only when the whole selection already agrees; offering one
            # gap's group as the default for twelve would silently retag eleven.
            groups = {str((kg_store.get(i) or {}).get("group") or "") for i in ids}
            current = groups.pop() if len(groups) == 1 else ""
        dlg = _GroupAssignDialog(kg_store.groups(), current=current,
                                 n=len(ids), parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.value()
        changed = kg_store.set_group(ids, name)
        if not changed:
            tooltip("No change — they were already there.")
            return
        if name:
            # Moving work INTO a batch you had put away wakes it, for the same
            # reason adding one does — see wake_group.
            tooltip(f"{changed} gap(s) → {name}  "
                    f"({autotag.group_segment(name)})"
                    f"{_woke_suffix(wake_group(name))}")
        else:
            tooltip(f"{changed} gap(s) ungrouped.")
        self.refresh_list()

    def _planned_retag(self, members: list[dict], group_name: str,
                       plan: dict) -> tuple[list[tuple[dict, str, str]], int]:
        """What retagging these gaps under `plan` would change, without doing it.

        Split out from _retag_group because renaming a group has to preview the
        SAME thing for a name/path/mode that isn't saved yet — hence the
        explicit plan rather than resolving it from the group. Passing the whole
        plan (not just a segment) is what stops a rename from silently dragging
        an own-branch group back under the auto-tag base."""
        changes: list[tuple[dict, str, str]] = []
        skipped = 0
        for kg in members:
            levels = {k: kg_store.field(kg, k) for k in ("system", "subsystem", "topic")}
            if not any(str(v).strip() for v in levels.values()):
                skipped += 1        # never classified — nothing to build a tag from
                continue
            new = autotag.content_tag_from_levels(
                levels, kg_type_key=(kg.get("type") or "kg"),
                group=group_name, group_plan=plan)
            old = str(kg_store.field(kg, "auto_tag") or "").strip()
            if new and new != old:
                changes.append((kg, old, new))
        return changes, skipped

    def _retag_preview_text(self, changes: list[tuple[dict, str, str]],
                            skipped: int, head: str) -> str:
        note_counts = self._note_counts([o for _kg, o, _n in changes if o])
        lines = []
        for _kg, old, new in changes[:12]:
            n = note_counts.get(old, 0)
            moved = f"   [{n} card{'s' if n != 1 else ''}]" if n else ""
            lines.append(f"• {(old or '(no tag yet)')}\n      →  {new}{moved}")
        more = len(changes) - len(lines)
        total_notes = sum(note_counts.get(o, 0) for _k, o, _n in changes)
        return (
            head
            + "\n".join(lines)
            + (f"\n… and {more} more" if more > 0 else "")
            + (f"\n\n{skipped} gap(s) skipped — not classified yet." if skipped else "")
            + (f"\n\n{total_notes} existing card(s) carry the old tags and will be "
               "moved to the new ones." if total_notes else
               "\n\nNo existing cards carry these tags yet.")
        )

    def _apply_retag(self, changes: list[tuple[dict, str, str]]) -> int:
        """Write the new tags onto the KGs and move any cards already under the
        old ones. Returns the number of cards moved."""
        for kg, old, new in changes:
            fields = dict(kg.get("fields") or {})
            fields["auto_tag"] = new
            tags = [t for t in (kg.get("tags") or []) if t]
            if old and old in tags:
                tags = [new if t == old else t for t in tags]
            kg_store.update(kg["id"], fields=fields, tags=tags)
        return self._rename_note_tags(
            [(old, new) for _kg, old, new in changes if old])

    def _planned_backfill(self, members: list[dict],
                          name: str) -> tuple[list, int]:
        """Which cards in this group are MISSING a tag the scheme says they
        earn, and which tags. Returns ([(tag, [nid, ...]), ...], n_cards).

        This is the half of retagging that did not exist. _planned_retag only
        ever recomputed the CONTENT tag and renamed cards from the old one to
        the new one — so a card that never carried the group's branch, or the
        batch's source, could not be given one by any screen in the add-on.
        Every card written before the written half of a run started applying
        the full scheme is in exactly that state, and the only repair on offer
        was to make the cards again.

        The cards are found through each gap's own content tag, which is the
        one tag they are known to carry.
        """
        if mw is None or mw.col is None:
            return [], 0
        by_tag: dict[str, set] = {}
        seen_nids: set = set()
        plan = autotag.group_tag_plan(name)
        # One collection search per distinct tag, not per gap: a group's shared
        # branch and source are the SAME tag for all forty of its gaps, and
        # looking each up forty times is most of the wait.
        have_cache: dict[str, set] = {}

        def _notes_under(tag: str) -> set:
            if tag not in have_cache:
                try:
                    have_cache[tag] = set(
                        mw.col.find_notes(f'"tag:{tag}" OR "tag:{tag}::*"'))
                except Exception as e:
                    print(f"[ankisstant] backfill: finding {tag}: {e}")
                    have_cache[tag] = set()
            return have_cache[tag]
        for kg in members:
            content = str(kg_store.field(kg, "auto_tag") or "").strip()
            if not content:
                continue          # no card of its own to find
            levels = {k: kg_store.field(kg, k)
                      for k in ("system", "subsystem", "topic")}
            try:
                wanted = autotag.tags_for(
                    levels, group=name, group_plan=plan, tool="knowledge_gaps",
                    kg_type_key=str(kg.get("type") or "kg").lower())
            except Exception as e:
                print(f"[ankisstant] backfill: tags for {kg.get('id')}: {e}")
                continue
            wanted = [t for t in wanted if t and t != content]
            if not wanted:
                continue
            nids = _notes_under(content)
            if not nids:
                continue          # nothing was ever made from this gap
            for tag in wanted:
                missing = [n for n in nids if n not in _notes_under(tag)]
                if missing:
                    by_tag.setdefault(tag, set()).update(missing)
                    seen_nids.update(missing)
        return ([(t, sorted(n)) for t, n in by_tag.items()], len(seen_nids))

    def _apply_backfill(self, ops: list) -> int:
        """Put the missing tags on. One bulk write per tag, never per note —
        looping tag writes over notes is what turns a repair into an
        out-of-memory crash."""
        from ..core import anki_utils
        n = 0
        for tag, nids in ops:
            try:
                n += anki_utils.tag_notes([int(x) for x in nids], tag)
            except Exception as e:
                print(f"[ankisstant] backfill: tagging {tag}: {e}")
        return n

    def _retag_group(self, name: str) -> None:
        self._retag_groups([name])

    def _retag_groups(self, names: list[str]) -> None:
        """Bring these groups' tags up to date, previewing first — both halves:

          * every gap's CONTENT tag, recomputed, with cards already under the
            old one moved across;
          * every tag the scheme says the group's cards earn but they do not
            carry — its shared branch, its source — added to them.

        The second half is the one that was missing, and it is the only way to
        repair cards written before the written half of a run applied the full
        scheme. Preview-first is not politeness: this writes to cards already in
        the collection, and you should see the list before it happens.

        SEVERAL groups at once, in one preview and one checkpoint. Retag was
        wired to a button that stayed enabled on a multi-select and then did
        nothing at all — no dialog, no message — which is the worst possible
        behaviour for the one action that repairs missing tags. Doing them in
        turn would have meant N confirms and N undo steps for what is one
        decision.
        """
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            return
        # (name, changes, skipped, ops, n_cards) for every group with work to do.
        jobs: list[tuple] = []
        empty, skipped_total = 0, 0
        for name in names:
            members = [kg for kg in kg_store.in_group(name) if kg.get("id")]
            if not members:
                empty += 1
                continue
            changes, skipped = self._planned_retag(
                members, name, autotag.group_tag_plan(name))
            ops, n_cards = self._planned_backfill(members, name)
            skipped_total += skipped
            if changes or ops:
                jobs.append((name, changes, skipped, ops, n_cards))

        if not jobs:
            if empty == len(names):
                tooltip("That group is empty." if len(names) == 1
                        else f"All {empty} of those groups are empty.")
                return
            where = (f"“{names[0]}”" if len(names) == 1
                     else f"any of those {len(names)} groups")
            showWarning(
                f"Nothing to retag in {where}.\n\n"
                + (f"{skipped_total} gap(s) have no system/subsystem/topic yet, "
                   "so there's no tag to build — they get one the first time "
                   "they go through Browse or Create."
                   if skipped_total else "Every tag is already up to date."))
            return

        if len(jobs) == 1:
            name, changes, skipped, ops, n_cards = jobs[0]
            head = (f"Retag {len(changes)} gap(s) in “{name}”?\n\n" if changes
                    else f"Update the tags on “{name}”?\n\n")
            body = (self._retag_preview_text(changes, skipped, head)
                    if changes else head)
            if ops:
                body += ("\n\nAdding to cards that don't carry them yet:\n"
                         + "\n".join(
                             f"• {t}   [{len(n)} card{'s' if len(n) != 1 else ''}]"
                             for t, n in sorted(ops)[:10])
                         + (f"\n… and {len(ops) - 10} more"
                            if len(ops) > 10 else "")
                         + f"\n\n{n_cards} card(s) affected.")
        else:
            # A per-group summary rather than every tag: several groups is
            # already dozens of lines, and the question being answered here is
            # "which groups are about to change, and by how much".
            n_gaps = sum(len(j[1]) for j in jobs)
            n_cards_all = sum(j[4] for j in jobs)
            lines = []
            for name, changes, _sk, ops, n_cards in jobs:
                bits = []
                if changes:
                    bits.append(f"{len(changes)} gap(s) retagged")
                if ops:
                    bits.append(f"{len(ops)} tag(s) added to {n_cards} card(s)")
                lines.append(f"• {name} — " + ", ".join(bits))
            body = (f"Retag {len(jobs)} group(s)?\n\n"
                    + "\n".join(lines)
                    + (f"\n\n{len(names) - len(jobs)} of the groups you picked "
                       "need no changes." if len(jobs) != len(names) else "")
                    + (f"\n\n{skipped_total} gap(s) skipped — not classified "
                       "yet." if skipped_total else "")
                    + f"\n\n{n_gaps} gap(s) and {n_cards_all} card(s) affected in "
                      "total. One Ctrl+Z takes all of it back out.")

        if not askUser(body, defaultno=True,
                       title="Retag group" if len(jobs) == 1 else "Retag groups"):
            return

        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Ankisstant: retag group"
                          if len(jobs) == 1 else "Ankisstant: retag groups")
        n_changes = moved = added = affected = 0
        for _name, changes, _sk, ops, n_cards in jobs:
            n_changes += len(changes)
            moved += self._apply_retag(changes) if changes else 0
            if ops:
                added += self._apply_backfill(ops)
                affected += n_cards
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass
        self.refresh_list()
        bits = []
        if len(jobs) > 1:
            bits.append(f"{len(jobs)} groups")
        if n_changes:
            bits.append(f"retagged {n_changes} gap(s)")
        if moved:
            bits.append(f"moved {moved} card(s)")
        if added:
            bits.append(f"added tags to {affected} card(s)")
        tooltip("Done — " + ", ".join(bits) + "." if bits else "Nothing to do.",
                period=3500)

    # ── renaming, archiving, and the groups manager ──────────────────────────

    def _group_stats(self) -> dict[str, dict]:
        """Per-group counts, in one pass over the store. Includes finished and
        archived groups — this is what the manager lists, so it must not filter
        anything the list page happens to be hiding."""
        out: dict[str, dict] = {}
        for kg in kg_store.load_all():
            name = str(kg.get("group") or "").strip()
            if not name:
                continue
            st = out.setdefault(name, {"total": 0, "open": 0, "in_progress": 0,
                                       "done": 0, "last": ""})
            st["total"] += 1
            status = str(kg.get("status") or "open")
            if status in ("open", "in_progress", "done"):
                st[status] += 1
            ts = str(kg.get("updated_at") or kg.get("created_at") or "")
            if ts > st["last"]:
                st["last"] = ts
        return out

    def _archived_groups(self) -> list[str]:
        try:
            raw = tool_config("knowledge_gaps").get("archived_groups") or []
            return [str(g) for g in raw]
        except Exception:
            return []

    _KEEP_OVERRIDE = object()      # "don't touch it", as distinct from "clear it"

    def _write_group_meta(self, old_name: str, new_name: str,
                          override=_KEEP_OVERRIDE) -> None:
        """Move a group's UI state across a rename, and set/clear its tag
        override — in ONE read-modify-write, because three separate saves of the
        same config slice is three chances to drop the others on the floor.

        `override` is a dict to set, anything falsy to clear, and the
        _KEEP_OVERRIDE sentinel to leave whatever is saved alone. Three states,
        because "no override" and "don't change the override" are different
        answers and conflating them is how a rename loses a group's branch."""
        try:
            cfg = tool_config("knowledge_gaps")
            for key in ("collapsed_groups", "archived_groups"):
                cur = [str(g) for g in (cfg.get(key) or [])]
                if old_name != new_name and old_name in cur:
                    cur = [new_name if g == old_name else g for g in cur]
                    # A merge can leave the name in twice.
                    cur = list(dict.fromkeys(cur))
                cfg[key] = cur
            overrides = dict(cfg.get("group_tag_overrides") or {})
            carried = overrides.get(old_name)
            if old_name != new_name:
                overrides.pop(old_name, None)
                if override is self._KEEP_OVERRIDE and carried is not None:
                    overrides[new_name] = carried
            if override is not self._KEEP_OVERRIDE:
                if override:
                    overrides[new_name] = override
                else:
                    overrides.pop(new_name, None)
            cfg["group_tag_overrides"] = overrides
            save_tool_config("knowledge_gaps", cfg)
        except Exception as e:
            print(f"[ankisstant] couldn't save group metadata: {e}")

    def _set_group_archived(self, name: str, archived: bool) -> None:
        if not name:
            return
        try:
            cfg = tool_config("knowledge_gaps")
            cur = [str(g) for g in (cfg.get("archived_groups") or [])]
            if archived and name not in cur:
                cur.append(name)
            elif not archived and name in cur:
                cur = [g for g in cur if g != name]
            cfg["archived_groups"] = cur
            save_tool_config("knowledge_gaps", cfg)
        except Exception as e:
            print(f"[ankisstant] couldn't save archive state: {e}")
            return
        tooltip(f"“{name}” {'archived' if archived else 'restored'}.")
        self.refresh_list()

    def _rename_group(self, name: str) -> bool:
        """Rename a group and/or change the tag segment it files under, moving
        any cards already tagged the old way. Returns True if anything changed.

        Both halves go through one confirm: the dialog shows the new tag branch,
        and the confirm below shows every tag that moves and how many cards ride
        along with it."""
        if not name:
            showWarning("Ungrouped gaps aren't a group — put them in one first.")
            return False
        members = [kg for kg in kg_store.in_group(name) if kg.get("id")]
        counts = self._note_counts(
            [str(kg_store.field(kg, "auto_tag") or "").strip() for kg in members
             if str(kg_store.field(kg, "auto_tag") or "").strip()])
        dlg = _GroupEditDialog(name, n_gaps=len(members),
                               n_cards=sum(counts.values()),
                               existing=kg_store.groups(), parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        new_name, override = dlg.values()
        if not new_name:
            return False
        old_override = autotag.group_override(name)
        new_override = autotag.normalise_override(override)
        renaming = new_name != name
        if not renaming and new_override == old_override:
            tooltip("No change.")
            return False
        if renaming and new_name in kg_store.groups():
            if not askUser(
                f"“{new_name}” already exists.\n\n"
                f"Merge {len(members)} gap(s) from “{name}” into it?",
                defaultno=True, title="Merge groups"):
                return False

        # The plan for the values on screen, which are not saved yet — so the
        # preview below is of what OK would actually write.
        plan = new_override or autotag.group_tag_plan(
            new_name, path=autotag.derive_group_segment(new_name),
            absolute=False)
        changes, skipped = self._planned_retag(members, new_name, plan)
        if changes:
            what = (f"Rename “{name}” to “{new_name}”"
                    if renaming else f"Retag “{name}”")
            body = self._retag_preview_text(
                changes, skipped,
                f"{what} and update {len(changes)} tag(s)?\n\n")
            if not askUser(body, defaultno=True, title="Edit group"):
                return False

        # Order matters: the KGs carry the new name before the tags are written,
        # so anything that re-derives a tag mid-flight derives the new one.
        if renaming:
            # The snapshots in `changes` go stale only in their "group" key, and
            # _apply_retag writes fields/tags by id — so they stay usable.
            kg_store.set_group([str(kg.get("id")) for kg in members], new_name)
        self._write_group_meta(name, new_name, new_override)
        moved = self._apply_retag(changes) if changes else 0
        self.refresh_list()
        bits = []
        if renaming:
            bits.append(f"“{name}” → “{new_name}”")
        if changes:
            bits.append(f"{len(changes)} tag(s) rebuilt")
        if moved:
            bits.append(f"{moved} card(s) moved")
        tooltip("; ".join(bits) or "Saved.", period=3500)
        return True

    def focus_group(self, name: str) -> None:
        """Filter the list to one group. Used by the manager's Open button, and
        it deliberately works for archived groups — an explicit group filter is
        how you look inside one without un-archiving it."""
        key = f"group:{name}"
        self._set_view("gaps")
        self._set_filter(key)
        self._refresh_group_filter_banner()

    def _open_group_manager(self) -> None:
        _GroupManagerDialog(self, parent=self.window()).exec()
        self.refresh_list()

    def _note_counts(self, tags: list[str]) -> dict[str, int]:
        """How many notes sit under each tag (including its subtree). Best
        effort — the preview is more useful slightly wrong than absent."""
        out: dict[str, int] = {}
        try:
            if mw is None or mw.col is None:
                return out
            # Deduped: a group of 40 gaps can share a handful of tags, and each
            # distinct tag is a collection search.
            for t in dict.fromkeys(t for t in tags if t):
                q = f'"tag:{t}" OR "tag:{t}::*"'
                out[t] = len(mw.col.find_notes(q))
        except Exception as e:
            print(f"[ankisstant] tag count failed: {e}")
        return out

    def _rename_note_tags(self, pairs: list[tuple[str, str]]) -> int:
        """Move existing notes from each old tag to its new one.

        col.tags.rename moves a tag AND its subtree in one backend operation,
        which is the only safe way to do this — looping note-by-note over tag
        writes is what turns a retag into an out-of-memory crash."""
        if not pairs:
            return 0
        try:
            if mw is None or mw.col is None:
                return 0
            moved = 0
            for old, new in pairs:
                if not old or old == new:
                    continue
                try:
                    res = mw.col.tags.rename(old, new)
                    # Modern Anki returns OpChangesWithCount; older builds
                    # returned a bare int. Both are handled rather than pinned
                    # to whichever this profile happens to run.
                    moved += int(getattr(res, "count", res) or 0)
                except Exception as e:
                    print(f"[ankisstant] tag rename {old} -> {new} failed: {e}")
            if moved:
                refresh_deck_browser()
            return moved
        except Exception as e:
            print(f"[ankisstant] tag rename failed: {e}")
            return 0

    def refresh_row(self, kg_id: str) -> None:
        """Re-render one list row from the store. Used after an autosave, where
        a full refresh_list() would fight the selection that's mid-change."""
        if not kg_id:
            return
        kg = kg_store.get(kg_id)
        if not kg:
            return
        for i in range(self._list.count()):
            li = self._list.item(i)
            if isinstance(li, _KGListItem) and li.kg_id == kg_id:
                # The row sets its tick box from the status, and that emits
                # itemChanged — which is the signal that CLOSES a gap. Guarded,
                # or re-rendering a row would read as ticking its box.
                self._tick_guard = True
                try:
                    li.refresh(kg)
                finally:
                    self._tick_guard = False
                return

    def _on_select(self) -> None:
        # Save whatever the pane is holding BEFORE it gets pointed elsewhere —
        # load() blows the widgets away, so an unsaved title would be lost here.
        prev_id = (self._detail._current or {}).get("id")
        if self._detail.flush():
            self.refresh_row(prev_id)

        items = self._list.selectedItems()
        if not items:
            self._detail.load(None)
            self._after_selection_changed()
            return
        # The gap you just CLICKED, not selectedItems()[0] — that's list order,
        # so extending a multi-select downwards left the pane pinned to the
        # topmost row while you were looking at the bottom one. Every field on
        # screen then belonged to a different gap, which reads as "my notes
        # aren't showing" rather than as the wrong record.
        li = self._list.currentItem()
        if not isinstance(li, _KGListItem) or li not in items:
            li = next((x for x in items if isinstance(x, _KGListItem)), None)
        if li is None:
            self._after_selection_changed()
            return
        self._after_selection_changed()
        # Extending a selection over the gap that's already loaded must not
        # reload it — that would revert edits still in the boxes.
        if prev_id and prev_id == li.kg_id:
            return
        kg = kg_store.get(li.kg_id)
        self._detail.load(kg)

    # ── bulk actions ───────────────────────────────────────────────────────────

    def _selected_ids(self) -> list[str]:
        """What every bulk action acts on: the HIGHLIGHTED gaps on this page,
        plus every gap in a MARKED group.

        The tick box is the Closed state now, so choosing a batch is the
        ordinary list selection — click, Shift-click a run, Cmd-click to add
        one, or Select all. It used to be the ticks, which put a to-do tick and
        a selection tick in the same box.

        Marked groups are in because a COLLAPSED group has no rows to
        highlight, and because "these three groups" is a real batch: Cmd-click
        the headers (see _toggle_group_mark) and the gap buttons act on all of
        their gaps.

        Group members lead the list, then the highlighted rows, deduped — order
        is only what the confirm dialogs show, but it should be stable."""
        out, seen = [], set()
        for name in self._marked_groups:
            for i in self._group_members.get(name, []):
                if i and i not in seen:
                    seen.add(i)
                    out.append(i)
        for li in self._list.selectedItems():
            if isinstance(li, _KGListItem) and li.kg_id and li.kg_id not in seen:
                seen.add(li.kg_id)
                out.append(li.kg_id)
        return out

    def _on_list_context_menu(self, pos) -> None:
        # A right-click on a group header is about the GROUP, not the selection
        # (headers can't be selected, so the selection is whatever was clicked
        # last and acting on it here would be indefensible).
        clicked = self._list.itemAt(pos)
        if isinstance(clicked, _KGGroupHeader):
            self._on_group_context_menu(clicked, pos)
            return
        ids = self._selected_ids()
        if not ids:
            return
        self._gap_actions_menu(ids).exec(self._list.mapToGlobal(pos))

    def _gap_actions_menu(self, ids: list[str]) -> QMenu:
        """The bulk menu for a set of gaps, built rather than shown, so the
        Groups page's gap list offers exactly the same actions — one menu, two
        places it can be raised from."""
        menu = QMenu(self._list)
        n = len(ids)
        label = "1 gap" if n == 1 else f"{n} gaps"
        # The unified pass. First in the menu because it is the one that ends
        # with the gap actually closed: search and create were always two halves
        # of one question ("is this in my deck, and if not, write it").
        work = menu.addAction(f"Send {label} to queue",
                              lambda: self._queue_work_through(ids))
        work.setToolTip(
            "Park these on the queue. They wait there until you press ▶ Run "
            "queue, which does the lot in one pass: your deck is searched for "
            "every point, cards are written for the points nothing covered, "
            "and one button adds both in one undoable step."
        )
        now = menu.addAction(f"Find && write {label} now…",
                             lambda: self._work_through_now(ids))
        now.setToolTip("Open Find & write on these straight away, without "
                       "parking them first.")
        # Only when there is something to take off — an always-there entry that
        # is usually a no-op reads as a second queue button.
        on_queue = self._queued_set()
        queued = [i for i in ids if i in on_queue]
        if queued:
            off = menu.addAction(
                f"Take {'it' if len(queued) == 1 else f'{len(queued)} of them'}"
                " off the queue",
                lambda: self._unqueue_work_through(queued))
            off.setToolTip(
                "Un-send: these stop waiting for ▶ Run queue. The "
                "gaps are not changed — they stay exactly as they are in the "
                "list.")
        menu.addSeparator()
        grp = menu.addAction(f"Group {label}…",
                             lambda: self._assign_group(ids))
        grp.setToolTip(
            "Put these in a named batch — an SRS, a week, a revision block. "
            "The group adds one level to their auto-tag, so the whole batch "
            "ends up as one branch of your tag tree."
        )
        menu.addSeparator()
        # Anki's own Browse, on the gap you right-clicked. One title, not six —
        # see open_anki_browse.
        first = kg_store.get(ids[0]) or {}
        first_title = str(first.get("title") or "")
        if first_title:
            menu.addAction(
                "🏷 Find in Anki Browse — tags",
                lambda t=first_title: open_anki_browse(t, as_tag=True))
            menu.addAction(
                "🔎 Find in Anki Browse — cards",
                lambda t=first_title: open_anki_browse(t, as_tag=False))
        menu.addSeparator()
        menu.addAction(f"Close {label}",
                       lambda: self._bulk_set_status(ids, "done"))
        menu.addAction(f"Reopen {label}",
                       lambda: self._bulk_set_status(ids, "open"))
        menu.addSeparator()
        reread = menu.addAction(f"↻ Read {label} again next run",
                                lambda: self._forget_readings(ids))
        reread.setToolTip(
            "A gap is read once and the reading is kept, so re-running a batch "
            "only pays for what changed. Use this when the AI read a gap wrong "
            "— it throws that reading away so the next run reads it fresh."
        )
        menu.addSeparator()
        menu.addAction(f"Delete {label}…",
                       lambda: self._bulk_delete(ids))
        return menu

    def _on_group_context_menu(self, header, pos) -> None:
        """Actions on a whole group. This is the point of groups — the batch is
        the unit of work, so the tools that run on a batch are one click from
        its header rather than requiring you to select twelve rows first.

        Right-clicking a header that is part of a Cmd-click selection of several
        groups acts on all of them instead, the same rule as right-clicking one
        row of a multi-selection of gaps."""
        if (header.group_name in self._marked_groups
                and len(self._marked_groups) > 1):
            names = [h.group_name for h in self._header_items()
                     if h.group_name in self._marked_groups]
        else:
            names = [header.group_name]
        menu = self._group_menu_for(names, self._list)
        # Collapse belongs to the header you clicked rather than to the batch,
        # so it goes above the shared actions instead of into them.
        act = menu.addAction("Collapse" if not header.collapsed else "Expand",
                             lambda: self._on_collapse_clicked(header))
        first = menu.actions()[0]
        menu.insertAction(first, act)
        menu.insertSeparator(first)
        menu.exec(self._list.mapToGlobal(pos))

    def _group_menu_for(self, names: list[str], parent=None) -> QMenu:
        """The actions for one group or several.

        One builder for both, because the only real difference is that Rename
        and Retag are about a single group's own identity — everything else
        means the same thing over one group or six. The Gaps page raises this
        off a section header, the Groups page off its list."""
        names = list(names)
        ids: list[str] = []
        seen: set[str] = set()
        for g in names:
            for kid in self._group_ids(g):
                if kid not in seen:
                    seen.add(kid)
                    ids.append(kid)
        one = names[0] if len(names) == 1 else None
        # && — a group called "Obs & Gynae" is a menu label too, and a single
        # & would be eaten as a shortcut marker.
        label = ((one or NO_GROUP).replace("&", "&&") if one is not None
                 else f"{len(names)} groups")
        n = len(ids)
        menu = QMenu(parent or self)
        if one is None:
            head = menu.addAction(", ".join(g or NO_GROUP for g in names).replace("&", "&&"))
            head.setEnabled(False)
            menu.addSeparator()
        else:
            # First, and above the early return below, because an empty group
            # is exactly the one that needs a gap put in it.
            menu.addAction(f"＋ Add KG into “{label}”",
                           lambda: self._on_add_kg(group=one))
            menu.addSeparator()
        if not ids:
            empty = menu.addAction("No gaps in it")
            empty.setEnabled(False)
            if len(self._marked_groups) > 1:
                menu.addAction("Clear group selection", self._clear_group_marks)
            menu.addSeparator()
            menu.addAction("Manage groups…", self._open_group_manager)
            return menu
        # Runs act on what's LEFT of the group; edits act on all of it. See
        # _live_ids. The count in the label is the live one, so the menu never
        # promises to work through 12 gaps and then read 4.
        live = self._live_ids(ids)
        m = len(live)
        done_note = f" — {n - m} already Closed" if m != n else ""
        if not live:
            skipped = menu.addAction(f"All {n} gap(s) are Closed — nothing to run")
            skipped.setEnabled(False)
        else:
            work = menu.addAction(f"Send {label} ({m}){done_note} to queue",
                                  lambda: self._queue_work_through(live))
            work.setToolTip(
                "Park these on the queue. They wait there until you press "
                "▶ Run queue, which does the lot in one pass: your deck is "
                "searched for every point, "
                "cards are written for the points nothing covered, and one "
                "button adds both in one undoable step."
                + ("\n\nGaps already Closed are left out." if done_note else "")
            )
            menu.addAction(f"Find && write {label} ({m}){done_note} now…",
                           lambda: self._work_through_now(live))
            on_queue = self._queued_set()
            queued = [i for i in ids if i in on_queue]
            if queued:
                menu.addAction(f"Take {len(queued)} off the queue",
                               lambda: self._unqueue_work_through(queued))
        menu.addSeparator()
        menu.addAction(f"Regroup {label} ({n})…",
                       lambda: self._assign_group(ids))
        if one:
            menu.addAction(f"Rename / edit tag: {label}…",
                           lambda: self._rename_group(one))
        retag = menu.addAction(f"Retag {label}…",
                               lambda: self._retag_groups(list(names)))
        retag.setToolTip(
            "Rebuild every tag in "
            + ("this group" if one else "these groups")
            + " so it carries the group segment, and add the shared branch and "
            "source to cards written before the scheme did. Shows you what "
            "changes before writing anything."
            + ("" if one else "\n\nAll of them in one preview and one undo step.")
        )
        menu.addSeparator()
        menu.addAction(f"Close {label}",
                       lambda: self._bulk_set_status(ids, "done"))
        if one:
            archived = one in self._archived_set
            act = menu.addAction(
                f"Restore {label}" if archived else f"Archive {label}",
                lambda: self._set_group_archived(one, not archived))
            act.setToolTip(
                "Bring it back onto this page." if archived else
                "Put a finished batch away. The gaps and their cards are "
                "untouched — it stays in Manage groups."
            )
        else:
            live = [g for g in names if g and g not in self._archived_set]
            if live:
                menu.addAction(f"Archive {len(live)} group(s)",
                               lambda: self._archive_groups(live))
        menu.addSeparator()
        menu.addAction(f"↻ Read {label} again next run",
                       lambda: self._forget_readings(ids))
        menu.addSeparator()
        if len(self._marked_groups) > 1:
            menu.addAction("Clear group selection", self._clear_group_marks)
        menu.addAction("Manage groups…", self._open_group_manager)
        return menu

    def _archive_groups(self, names: list[str]) -> None:
        """Archive several groups in ONE config write. Looping
        _set_group_archived would save and rebuild the page once per group, and
        fire a tooltip per group over the top of the last."""
        names = [g for g in names if g]
        if not names:
            return
        try:
            cfg = tool_config("knowledge_gaps")
            cur = [str(g) for g in (cfg.get("archived_groups") or [])]
            for name in names:
                if name not in cur:
                    cur.append(name)
            cfg["archived_groups"] = cur
            save_tool_config("knowledge_gaps", cfg)
        except Exception as e:
            print(f"[ankisstant] couldn't save archive state: {e}")
            return
        self._marked_groups.difference_update(names)
        tooltip(f"{len(names)} group(s) archived.")
        self.refresh_list()

    def _bulk_set_status(self, ids: list[str], status: str) -> None:
        self.set_gaps_closed(ids, status == "done", quiet=False)

    def set_gaps_closed(self, ids: list[str], closed: bool, *,
                        quiet: bool = True) -> int:
        """Open or close these gaps. THE one path that writes the status.

        Every door goes through here — the tick box on a row, the tick box on a
        group header, the bulk menu — so closing a gap does the same things
        whichever way you did it: the store, the Things sync, the list, and the
        pane if it is showing one of them. Two half-implementations of this is
        how the row's tick and the pane's "Closed" box came to disagree.

        `quiet` suppresses the tooltip for a single gap you just ticked (you
        watched it happen); a bulk change still says what it did."""
        wanted = "done" if closed else "open"
        done = 0
        for kid in ids:
            try:
                # dismissed is always cleared — it was only ever a shade of
                # Closed, and nothing can set it any more.
                if kg_store.update(kid, status=wanted, dismissed=False):
                    done += 1
            except Exception as e:
                print(f"[ankisstant] couldn't set the status: {e}")
        if not done:
            return 0
        if closed:
            maybe_sync_to_things()
        if not quiet or done > 1:
            tooltip(f"{done} gap(s) → " + ("Closed" if closed else "Open") + ".")
        cur = None
        try:
            cur = (self._detail._current or {}).get("id")
        except Exception:
            cur = None
        self.refresh_list(preserve_selection=cur if cur in set(ids) else None)
        return done

    def _forget_readings(self, ids: list[str]) -> None:
        """Throw away the AI's cached reading of these gaps.

        The read is cached per gap so a re-run only pays for what changed; the
        cost of that is a bad reading sticking around, since the gap's wording
        is what the cache is keyed on. This is the way out."""
        try:
            from .kg import triage as kg_triage
            n = kg_triage.forget(ids)
        except Exception as e:
            print(f"[ankisstant] couldn't clear cached readings: {e}")
            showWarning(f"Couldn't clear the cached reading:\n\n{e}")
            return
        tooltip(f"{n or 0} cached reading(s) cleared — "
                f"{len(ids)} gap(s) will be read fresh next run.")

    def _bulk_delete(self, ids: list[str]) -> None:
        if not ids:
            return
        if not askUser(
            f"Delete {len(ids)} knowledge gap(s)?\n\nThis is permanent.",
            defaultno=True,
        ):
            return
        for kid in ids:
            kg_store.remove(kid)
        tooltip(f"Deleted {len(ids)} gap(s).")
        self.refresh_list()

    # ── the work-through queue ───────────────────────────────────────────────

    def _queue_work_through(self, ids: list[str]) -> None:
        """Park these gaps on the queue. Nothing opens.

        The whole point of the queue is that sending is cheap: you tick a gap,
        it goes, you carry on reading. Popping a dialog on every send would
        make building a batch across four groups a fight with four dialogs."""
        from .kg import work_queue
        live = [i for i in ids
                if (kg_store.get(i) or {}).get("status") != "done"]
        skipped = len(ids) - len(live)
        if not live:
            tooltip("Those gaps are all Closed already — nothing to queue."
                    if skipped else "Those gaps are no longer in the list.")
            return
        added, dupes = work_queue.add(live)
        self.refresh_work_badge()
        bits = []
        if added:
            bits.append(f"Queued {added} gap(s)")
        if dupes:
            bits.append(f"{dupes} already waiting")
        if skipped:
            bits.append(f"{skipped} skipped as Closed")
        total = work_queue.count()
        tooltip("   ·   ".join(bits)
                + f" — {total} on the queue. Press ▶ Run queue when you're "
                  "ready.")

    def _unqueue_work_through(self, ids: list[str]) -> None:
        """Take these off the queue. The gaps themselves are untouched — this
        is un-sending, not closing."""
        from .kg import work_queue
        n = work_queue.remove(ids)
        self.refresh_work_badge()
        if not n:
            tooltip("None of those were on the queue.")
            return
        left = work_queue.count()
        tooltip(f"Took {n} gap(s) off the queue"
                + (f" — {left} still waiting." if left else " — queue empty."))

    def _queued_set(self) -> set[str]:
        """What is on the queue right now, as a set, for menus that need to ask
        about several gaps at once without re-reading the file per gap."""
        try:
            from .kg import work_queue
            return set(work_queue.ids())
        except Exception as e:
            print(f"[ankisstant] couldn't read the queue: {e}")
            return set()

    def refresh_work_badge(self) -> None:
        """The badge is the only thing on the page that says how many are
        waiting, so it is refreshed after anything that could change that."""
        btn = getattr(self, "_work_queue_btn", None)
        if btn is None:
            return
        try:
            from .kg import work_queue
            n = work_queue.count()
        except Exception as e:
            print(f"[ankisstant] work queue count failed: {e}")
            return
        btn.setText(f"▶ Run queue  ({n})" if n else "▶ Run queue")
        btn.setEnabled(bool(n))
        btn.setToolTip(
            "Nothing waiting yet — pick gaps and press Send to queue to "
            "park them here." if not n else
            f"Find & write all {n} queued gap(s) now, in one pass.\n\n"
            "The queue empties as that window opens — this is the sitting. "
            "Gaps you don't run stay Open in the list; re-tick them to queue "
            "them again.\n\nRight-click for the queue's housekeeping."
        )

    def _run_work_queue(self) -> None:
        """Run everything on the queue, in one pass.

        There is no queue window. A screen listing the queued gaps so you could
        pick some of them would be the work-through screen with a worse table —
        and picking is the thing the queue exists to save you. You parked them
        because you meant to work through them; this works through them.

        The queue empties when the pass OPENS — all of it, not just what got
        closed. The queue is the list of gaps you meant to deal with in this
        sitting; one you opened the pass on and didn't finish has still had its
        turn, and leaving it to pile up is how a queue stops meaning anything.
        Nothing is lost: the gap stays Open in the list, and re-queueing it is
        one tick away, deliberately.

        The draining itself lives in _work_through_now (see _drain_queue) so
        that a pass started from a row, a group or "Work through now" tidies up
        exactly the same way this one does."""
        from .kg import work_queue
        ids = work_queue.ids()
        if not ids:
            tooltip("Nothing queued yet — pick gaps and press Send to queue "
                    "to park them here.")
            return
        self._work_through_now(ids)

    def _work_queue_menu(self, pos) -> None:
        """Right-click the badge: the queue's housekeeping, without a screen
        of its own to hold it."""
        from .kg import work_queue
        n = work_queue.count()
        menu = QMenu(self._work_queue_btn)
        run = menu.addAction(f"Find && write all {n} now…",
                             self._run_work_queue)
        run.setEnabled(bool(n))
        menu.addSeparator()
        # The queue's contents, one entry each, so taking one gap back off
        # doesn't mean hunting for its row. This is the only place the queue
        # is ever listed — a menu you click once, not a screen to manage.
        off = menu.addMenu(f"Take one off the queue  ({n})")
        off.setEnabled(bool(n))
        LIMIT = 30
        for kid in work_queue.ids()[:LIMIT]:
            kg = kg_store.get(kid) or {}
            title = " ".join(str(kg.get("title") or "(untitled)").split())
            if len(title) > 60:
                title = title[:60].rstrip() + "…"
            off.addAction(title,
                          lambda _c=False, i=kid: self._unqueue_work_through([i]))
        if n > LIMIT:
            more = off.addAction(f"…and {n - LIMIT} more")
            more.setEnabled(False)
        menu.addSeparator()
        cd = menu.addAction("Clear the closed ones",
                            lambda: self._clear_work_queue(done_only=True))
        cd.setEnabled(bool(n))
        ca = menu.addAction("Clear the queue",
                            lambda: self._clear_work_queue(done_only=False))
        ca.setEnabled(bool(n))
        menu.exec(self._work_queue_btn.mapToGlobal(pos))

    def _clear_work_queue(self, *, done_only: bool) -> None:
        from .kg import work_queue
        if done_only:
            n = work_queue.clear_done()
            tooltip("Nothing closed to clear." if not n
                    else f"Cleared {n} closed gap(s) off the queue.")
        else:
            n = work_queue.count()
            if n and not askUser(
                    f"Take all {n} gap(s) off the queue?\n\n"
                    "The gaps themselves are untouched.", parent=self):
                return
            work_queue.clear()
            tooltip(f"Queue cleared — {n} gap(s) taken off it.")
        self.refresh_work_badge()

    def _work_through_now(self, ids: list[str]) -> None:
        """Search AND create, on ONE screen.

        The same dialog as Bulk search, in unified mode: the deck is searched
        for every gap, the points it can't cover are written as cards into the
        bottom of that same results screen while you read the found ones, and a
        single button commits both under one checkpoint. Bulk search and Mass AI
        Create stay on the menu underneath it — this adds a path, it doesn't
        replace either.

        Returns the dialog so the queue can tell when the pass is over. The
        window is modeless: this function returning means it has opened, not
        that anything has happened in it."""
        try:
            from .kg.bulk_search import open_unified
        except Exception as e:
            print(f"[ankisstant] unified pass unavailable: {e}")
            showWarning(f"Couldn't open Find & write:\n\n{e}")
            return None
        kgs = [kg for kg in (kg_store.get(i) for i in ids) if kg]
        if not kgs:
            tooltip("Those gaps are no longer in the list.")
            return None
        dlg = open_unified(kgs, parent=self.window())
        self._drain_queue(dlg)
        return dlg

    def _drain_queue(self, dlg) -> None:
        """Empty the work queue the moment a pass OPENS — every pass, any door.

        THE QUEUE IS A SITTING, NOT A BACKLOG. Everything parked on it when the
        pass opens has had its turn: the gaps that get worked, and the ones
        looked at and decided against. A gap passed over stays OPEN in the
        list; what it stops being is queued. Leaving those behind is how a
        queue of four becomes a queue of sixteen with no way to tell which of
        them you had already seen.

        AT OPEN, NOT AT CLOSE. This hung off dialog.finished, which reads well
        and never fired once: BulkSearchDialog overrides closeEvent without
        calling up to QDialog, so the signal that empties this queue could not
        be emitted by any route. That is fixed too — but the queue should not
        depend on it. Draining on the way in is better on its own merits:

          · the badge clears when you press the button, which is when you
            expect to see it clear, not minutes later when the window shuts;
          · a gap ticked and queued DURING the pass belongs to the next
            sitting, and it survives, because it is added after this;
          · it cannot be skipped by a crash, a force-quit, or a window left
            open all afternoon — which is exactly what happened.

        Nothing is lost by draining early: the queue holds ids, the gaps are
        untouched, and re-queueing one is a tick away. Deliberately.
        """
        if dlg is None:
            return
        try:
            from .kg import work_queue
            # clear(), not remove(ids()): ids() prunes entries whose gap has
            # since been deleted, so removing exactly what it lists would leave
            # those rows on disk for good. Draining is draining. The count is
            # read first, from the live list, so the number reported is gaps
            # rather than rows.
            n = work_queue.count()
            work_queue.clear()
        except Exception as e:
            print(f"[ankisstant] couldn't empty the work queue: {e}")
            return
        if not n:
            return
        try:
            self.refresh_work_badge()
            self.refresh_list()
        except RuntimeError:
            # The panel went away. The queue is on disk and has been emptied;
            # there is nothing left to redraw.
            return
        tooltip(f"Work queue cleared ({n} gap(s)) — this is the sitting. Any "
                "you don't run stay Open in the list.")

    def _mark_in_progress(self, ids: list[str]) -> None:
        """A gap that's been handed to a tool is no longer untouched. Only Open
        gaps move — sending a Done one somewhere doesn't reopen it."""
        for kid in ids:
            try:
                kg = kg_store.get(kid)
                if kg and kg.get("status") == "open":
                    kg_store.update(kid, status="in_progress")
            except Exception as e:
                print(f"[ankisstant] couldn't mark {kid} in progress: {e}")

    # ── add / analyse ────────────────────────────────────────────────────────

    def _current_group_context(self) -> str | None:
        """The group you are plainly working in, or None if nothing says.

        Read off whichever page you are on, in order of how explicit the
        gesture was: a group picked on the Groups page, then a marked header,
        then a `group:` filter, then the group of the gap you have selected.
        It only ever PREFILLS the Add dialog's Group box, so guessing wide is
        safe — you can see it and change it before saving. "" is a real answer
        (the ungrouped section); None means don't prefill."""
        if self._view == "groups":
            picked = self._groups_view.selected_names()
            return picked[0] if len(picked) == 1 else None
        if len(self._marked_groups) == 1:
            return next(iter(self._marked_groups))
        if self._filter.startswith("group:"):
            return self._filter.split(":", 1)[1]
        sel = [i.kg_id for i in self._list.selectedItems()
               if isinstance(i, _KGListItem) and i.kg_id]
        if len(sel) == 1:
            grp = str((kg_store.get(sel[0]) or {}).get("group") or "").strip()
            if grp:
                return grp
        return None

    def _on_add_kg(self, group: str | None = None) -> None:
        if group is None:
            group = self._current_group_context()
        dlg = AddKGDialog(self, group=group)
        if dlg.exec():
            self.refresh_list()
            if self._view == "groups":
                self._groups_view.refresh()

    def _on_kg_from_chat(self) -> None:
        """Condense SynapsePro's latest chat answer into gaps.

        Asynchronous and non-modal — the panel stays usable while the AI
        thinks, and ``capture_last_turn`` disables the button for the duration
        so a second click can't queue a duplicate request. The list is
        refreshed by the capture itself via ``_refresh_open_panel``.
        """
        from ..core import synapse_chat
        synapse_chat.capture_last_turn(self._chat_btn)

    def _on_bulk_import(self) -> None:
        dlg = _BulkImportDialog(self)
        if not dlg.exec():
            return
        n = dlg.imported_count
        if n:
            tooltip(f"Imported {n} knowledge gap(s).")
            self.refresh_list()

    def _on_import_things(self) -> None:
        """One press: read Things, show what's new, import it.

        Still a confirm, not a silent write — it can add fifty gaps and create
        a dozen groups, and the tag tree is built off those groups."""
        tag = importers.DEFAULT_TAG
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            items = importers.fetch(tag)
        finally:
            QApplication.restoreOverrideCursor()
        if not items:
            names = importers.list_tags()
            showWarning(
                f"No open to-dos are tagged “{tag}” in Things.\n\n"
                + ("Tags on this Mac: " + ", ".join(names[:25]) + "\n\n"
                   "Use Bulk import to pick a different tag."
                   if names else "Couldn't read your Things database."))
            return
        fresh, dupes = importers.dedupe_against_store(items,
                                                      kg_store.load_all())
        if not fresh:
            tooltip(f"All {len(items)} tagged to-do(s) are already in your "
                    "gap list.", period=4000)
            return
        groups = sorted({str(r.get("group") or "").strip() for r in fresh}
                        - {""})
        n_notes = sum(1 for r in fresh if str(r.get("notes") or "").strip())
        detail = [f"{len(fresh)} new gap(s) from Things."]
        if groups:
            detail.append("Groups: " + ", ".join(groups))
        if n_notes:
            detail.append(f"{n_notes} bring their notes across.")
        if dupes:
            detail.append(f"{len(dupes)} already imported — skipped.")
        detail.append("\nThey stay OPEN in Things — each one is ticked off "
                      "there when you finish it here.")
        if not askUser("\n".join(detail) + "\n\nImport them?"):
            return
        records = importers.to_store_records(
            fresh,
            type_key=(tool_config("knowledge_gaps").get("default_type_on_add")
                      or "kg"))
        kg_store.add_many(records)
        wake_groups(r.get("group") for r in records)
        tooltip(f"Imported {len(records)} gap(s) from Things.", period=4000)
        self.refresh_list()

    def _on_clear_completed(self) -> None:
        items = kg_store.load_all()
        done_ids = [k["id"] for k in items
                    if k.get("status") == "done" and k.get("id")]
        if not done_ids:
            tooltip("Nothing to clear — no completed items.")
            return
        if not askUser(
            f"Remove {len(done_ids)} completed knowledge gap(s) from the list?\n\n"
            "This deletes them permanently — open items are untouched.",
            defaultno=True,
        ):
            return
        for kid in done_ids:
            kg_store.remove(kid)
        tooltip(f"Cleared {len(done_ids)} completed gap(s).")
        self.refresh_list()

# ── Tool contract ────────────────────────────────────────────────────────────

_panel: KnowledgeGapsPanel | None = None
_scroll: QScrollArea | None = None


def init(main_window) -> None:
    return None


def get_panel():
    global _panel, _scroll
    if _panel is None:
        _panel = KnowledgeGapsPanel()
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        _scroll.setWidget(_panel)
    else:
        _panel.refresh_setup_banner()
        # Pick up any newly-added or renamed types from Settings.
        try:
            _panel._rebuild_type_chip_row()
        except Exception:
            pass
        _panel.refresh_list()
    return _scroll


def _refresh_open_panel() -> None:
    """If the KG panel is currently visible, reload it. Called by AddKGDialog."""
    if _panel is not None:
        try:
            _panel.refresh_list()
        except Exception:
            pass
