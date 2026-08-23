# Mass AI Create — generate cards for a whole selection of knowledge gaps.
#
# The generation-side twin of bulk_search.py. Same three-beat shape, because the
# problem is the same one: a batch of gaps whose breadth varies wildly, which
# must be SIZED before it runs and REVIEWED after.
#
#   page 0  plan table   — one row per gap: what the AI read it as, how broad,
#                          how many cards, which tag. Editable before anything runs.
#   page 1  running      — per-gap status while the jobs generate.
#   page 2  batch review — every generated card, grouped by gap, approved and
#                          committed in ONE undoable step.
#
# WHY THE JOBS AREN'T OWNED BY THIS DIALOG.
# The run *feels* dialog-owned — its own progress bar, its own Stop, and closing
# mid-run offers to keep going — but the work itself is ordinary create_jobs
# records on disk. That runner already has restart recovery, per-job cancel and
# retry, and the uses_collection=False discipline that keeps a long generation
# from queueing behind Anki's backup. So the dialog POLLS a batch_id rather than
# holding threads: a crash or a quit mid-batch loses nothing, and reopening a
# batch afterwards is the same code path as watching one live.

from __future__ import annotations

import threading

from aqt import mw
from aqt.qt import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSpinBox, QStackedWidget, Qt,
    QTableWidget, QTableWidgetItem, QTimer, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ...core import anki_utils, api as core_api, log
from ...core.config import tool_config, save_tool_config, tool_model_for
from ...core.qt_utils import (
    attach_tag_completer, is_manual_provider, make_help_button,
    provider_configured, theme_dialog,
)
from .. import autotag
from .. import create_jobs
from . import gen_prompt
from . import store as kg_store
from . import triage as kg_triage
from .. import pipeline


NAME = "Mass AI Create"

# How often the running page re-reads the job store. Fast enough that a job
# flipping to "ready" feels immediate, slow enough that a 12-row rebuild every
# tick costs nothing.
POLL_MS = 750

HELP_HTML = """
<h3>Mass AI Create</h3>
<p>Generates cards for a whole selection of knowledge gaps in one run.</p>
<h4>1. Plan</h4>
<p><b>Read gaps with AI</b> sends every ticked gap to one call that decides, for
each, how broad it is and how many cards it's worth — then decomposes it into the
specific points the cards must cover. Everything it decides is editable: change
the <b>Breadth</b> dropdown or type over the <b>Cards</b> number.</p>
<p>Breadth moves two things at once: how much ground the cards cover, and how
deep each one cuts. <i>Wide</i> and <i>Deep</i> cover the same ground —
<i>Deep</i> goes past the headlines into mechanism.</p>
<p>The card number is a <b>ceiling, not a quota</b>. A gap with little in it
comes back with fewer cards, and that's correct.</p>
<h4>2. Generate</h4>
<p>One AI call per gap, two at a time (raise that in Settings &rarr; AI Create).
You can close this window — the run keeps going, and a crash or a restart won't
lose it.</p>
<h4>3. Review</h4>
<p>Every card, grouped by the gap it came from. Cards start approved; untick the
ones you don't want. <b>Add approved</b> writes them all in a single step, so one
Ctrl+Z takes the whole batch back out.</p>
<p><i>Skip what a deck or tag covers</i> searches a scope you name — a tag your
AI Search results were filed under, say — and drops any point already covered,
so Create only fills what's actually missing.</p>
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def _deck_names() -> list[str]:
    try:
        return sorted(d.name for d in mw.col.decks.all_names_and_ids())
    except Exception:
        return []


def _notetype_profiles(cfg: dict) -> list[dict]:
    profs = [p for p in (cfg.get("notetypes") or []) if isinstance(p, dict)]
    return [p for p in profs if (p.get("name") or "").strip()]


def _columns_cfg(cfg: dict) -> dict:
    """Which per-row override columns are shown. Additive key with per-key
    defaults, so an older config (or one written by another version) keeps every
    setting it already had and simply gains the defaults for what it lacks."""
    raw = cfg.get("mass_create_columns")
    out = {"tag": True, "deck": False}
    if isinstance(raw, dict):
        for k in out:
            if k in raw:
                out[k] = bool(raw[k])
    return out


def _norm_front(card: dict) -> str:
    """A card's front, flattened for near-duplicate comparison within a batch."""
    try:
        txt = anki_utils.strip_cloze(anki_utils.strip_html(card.get("front", "")))
    except Exception:
        txt = str(card.get("front", ""))
    return " ".join(txt.lower().split())[:60]


# ── the dialog ───────────────────────────────────────────────────────────────

def _is_group_scope(value: str) -> bool:
    """Whether `value` looks like a tag branch this dialog derived from a group
    (base::*::segment). Such a value may be left over from a previous batch in a
    different group, so it can be replaced; anything the user typed is theirs
    and is never touched."""
    v = (value or "").strip()
    return "::*::" in v


class MassCreateDialog(QDialog):
    """Plan → generate → review, for N knowledge gaps at once."""

    def __init__(self, kgs: list[dict], parent=None, batch_id: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Mass AI Create")
        self.setMinimumSize(980, 680)
        self.resize(1220, 800)
        # No WA_DeleteOnClose, and every callback checks _closed first: closing
        # mid-run hides this window and leaves the batch running, so a deleted
        # C++ object would take the poll timer down with it. Same contract as
        # bulk_search — see its __init__ for the full reasoning.
        self._closed = False
        self._backgrounded = False
        # Continuations for the Read → coverage → Generate chain. Held as state
        # rather than passed through the taskman callbacks, because those are
        # already three layers of lambda deep.
        self._after_read = None
        self._after_coverage = None
        self._auto_read_done = False

        self._kgs = [k for k in (kgs or []) if isinstance(k, dict)]
        self._by_id = {str(k.get("id") or ""): k for k in self._kgs}
        self._plans: list[kg_triage.KGPlan] = []
        self._batch_id = batch_id or ""
        self._busy = False
        self._cancel_ev: threading.Event | None = None
        self._groups: list[_KGGroup] = []
        self._committed = False

        self.cfg = tool_config("card_creator")
        self._cols = _columns_cfg(self.cfg)

        self._build()
        self._fill_plan_table()
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        if self._batch_id:
            # Reopened on an existing batch (from the Create panel, or after a
            # restart) — restore what it was enqueued with, then skip straight to
            # watching it. Without this the commit would use whatever the combos
            # happened to default to rather than the batch's own settings.
            self._restore_batch_settings()
            self._stack.setCurrentIndex(1)
            self._read_btn.setVisible(False)
            self._go_btn.setVisible(False)
            self._skip_btn.setVisible(True)
            self._skip_btn.setEnabled(False)
            self._set_busy(True, "Checking on that batch…", progress=True, stoppable=True)
            self._poll.start()
            self._on_poll()
        theme_dialog(self)

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        self._title = QLabel()
        self._title.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(make_help_button("Mass AI Create", HELP_HTML, self))
        root.addLayout(head)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_plan_page())
        self._stack.addWidget(self._build_running_page())
        self._stack.addWidget(self._build_review_page())
        root.addWidget(self._stack, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: rgba(127,127,127,0.95); font-size: 11px;")
        root.addWidget(self._status)

        row = QHBoxLayout()
        self._back_btn = QPushButton("◀ Back")
        self._back_btn.clicked.connect(self._on_back)
        self._back_btn.setVisible(False)
        row.addWidget(self._back_btn)
        row.addStretch(1)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setVisible(False)
        row.addWidget(self._stop_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.close)
        row.addWidget(self._close_btn)
        self._read_btn = QPushButton("🧠 Read gaps with AI")
        self._read_btn.setToolTip(
            "One call reads every ticked gap and fills in the breadth, the card "
            "count and the points each one decomposes into. You can edit all of it "
            "afterwards.")
        self._read_btn.clicked.connect(self._on_read)
        row.addWidget(self._read_btn)
        self._go_btn = QPushButton("✍️ Generate ▶")
        self._go_btn.clicked.connect(self._on_generate)
        row.addWidget(self._go_btn)
        self._skip_btn = QPushButton("Add all — skip the read-through")
        self._skip_btn.setVisible(False)
        self._skip_btn.clicked.connect(self._on_add_without_review)
        row.addWidget(self._skip_btn)
        self._add_btn = QPushButton("✅ Add approved")
        self._add_btn.setVisible(False)
        self._add_btn.clicked.connect(self._on_add_approved)
        row.addWidget(self._add_btn)
        root.addLayout(row)

        # A multi-step run can't go through the copy/paste provider: it would
        # need a separate manual paste per gap, plus one for the initial read.
        if is_manual_provider():
            self._read_btn.setEnabled(False)
            self._go_btn.setEnabled(False)
            self._set_status(
                "Mass AI Create needs a CLI or API provider — the copy/paste "
                "provider would need a separate paste for every gap.")
        elif not provider_configured():
            self._read_btn.setEnabled(False)
            self._go_btn.setEnabled(False)
            self._set_status("Set up an AI provider in Ankisstant Settings first.")

    def _build_plan_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)

        cols = ["Gap", "What the AI read it as", "Breadth", "Cards", "Pts", "Covered"]
        if self._cols["tag"]:
            cols.append("Tag")
        if self._cols["deck"]:
            cols.append("Deck")
        self._col_ix = {name: i for i, name in enumerate(cols)}

        self._table = QTableWidget(0, len(cols))
        self._table.setHorizontalHeaderLabels(cols)
        self._table.verticalHeader().setVisible(False)
        # The checkbox in column 0 IS the selection — a separate row highlight
        # would give two competing notions of "which gaps are we running".
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self._col_ix["What the AI read it as"],
                                QHeaderView.ResizeMode.Stretch)
        for name in ("Breadth", "Cards", "Pts", "Covered"):
            hh.setSectionResizeMode(self._col_ix[name], QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemChanged.connect(self._on_row_toggled)
        v.addWidget(self._table, 1)

        # ── batch settings strip ─────────────────────────────────────────────
        strip = QHBoxLayout()
        strip.addWidget(QLabel("Deck:"))
        self._deck = QComboBox()
        self._deck.setEditable(True)
        self._deck.addItems(_deck_names())
        self._deck.setCurrentText(self.cfg.get("default_deck") or "Default")
        self._deck.setMinimumWidth(220)
        strip.addWidget(self._deck)

        strip.addWidget(QLabel("Notetype:"))
        self._notetype = QComboBox()
        for p in _notetype_profiles(self.cfg):
            self._notetype.addItem(p["name"])
        want = (self.cfg.get("selected_notetype") or self.cfg.get("default_notetype") or "").strip()
        ix = self._notetype.findText(want)
        if ix >= 0:
            self._notetype.setCurrentIndex(ix)
        self._notetype.setMinimumWidth(200)
        strip.addWidget(self._notetype)

        strip.addWidget(QLabel("Focus:"))
        self._focus = QLineEdit()
        self._focus.setPlaceholderText("optional — applies to every gap")
        strip.addWidget(self._focus, 1)
        v.addLayout(strip)

        # ── coverage scope ───────────────────────────────────────────────────
        scope_cfg = self.cfg.get("mass_create_scope") or {}
        cov = QHBoxLayout()
        self._cov_cb = QCheckBox("Skip what a deck or tag already covers")
        self._cov_cb.setToolTip(
            "Searches only the deck or tag you name — not your whole collection — "
            "and drops any point it already covers.\n\n"
            "Inside a narrow scope, recall is low, so 'not covered' is "
            "over-reported. That errs safe: you may write a card you didn't "
            "strictly need and can untick it in review.")
        self._cov_cb.setChecked(bool(scope_cfg.get("enabled", False)))
        self._cov_cb.toggled.connect(self._sync_scope_enabled)
        cov.addWidget(self._cov_cb)

        self._cov_kind = QButtonGroup(self)
        self._cov_deck_rb = QRadioButton("Deck")
        self._cov_tag_rb = QRadioButton("Tag")
        self._cov_kind.addButton(self._cov_deck_rb)
        self._cov_kind.addButton(self._cov_tag_rb)
        (self._cov_deck_rb if scope_cfg.get("kind") == "deck" else self._cov_tag_rb).setChecked(True)
        self._cov_deck_rb.toggled.connect(self._sync_scope_completer)
        cov.addWidget(self._cov_deck_rb)
        cov.addWidget(self._cov_tag_rb)

        self._cov_value = QLineEdit(str(scope_cfg.get("value") or ""))
        self._cov_value.setPlaceholderText("deck or tag to check against")
        cov.addWidget(self._cov_value, 1)

        # When the whole selection shares a group, that group's tag branch is
        # almost always the scope you want: the usual sequence is Mass Search a
        # group, tag what it found, then Mass Create the residue. Prefilling it
        # turns the common case from "remember and retype the tag" into a
        # checkbox — and it only ever prefills, so a scope you typed yourself is
        # never overwritten.
        shared = self._shared_group()
        self._group_scope = ""
        saved_val = str(scope_cfg.get("value") or "").strip()
        if shared and (not saved_val or _is_group_scope(saved_val)):
            prefix = autotag.group_tag_prefix(shared)
            if prefix:
                self._group_scope = prefix
                self._cov_tag_rb.setChecked(True)
                self._cov_value.setText(prefix)
                self._cov_cb.setChecked(True)
                self._cov_cb.setText(
                    f"Skip what \u201c{shared}\u201d already covers")
                self._cov_cb.setToolTip(
                    f"Every gap here is in \u201c{shared}\u201d, so this is "
                    f"prefilled with that group's tag branch ({prefix}) — the "
                    "cards a Mass AI Search of this group would have filed.\n\n"
                    "Points that branch already covers are dropped rather than "
                    "written again. Untick to measure against your whole "
                    "collection instead, or edit the tag freely.")

        self._cov_btn = QPushButton("Check coverage")
        self._cov_btn.setToolTip(
            "Runs the check now, so you can see what it would skip before "
            "spending tokens on generation.")
        self._cov_btn.clicked.connect(self._on_check_coverage)
        cov.addWidget(self._cov_btn)
        v.addLayout(cov)
        self._sync_scope_enabled()
        self._sync_scope_completer()
        return page

    def _build_running_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        self._run_tree = QTreeWidget()
        self._run_tree.setColumnCount(3)
        self._run_tree.setHeaderLabels(["Gap", "Status", "Cards"])
        self._run_tree.setRootIsDecorated(False)
        self._run_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self._run_tree, 1)
        return page

    def _build_review_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self._review_head = QLabel("")
        self._review_head.setStyleSheet("font-weight: 600;")
        bar.addWidget(self._review_head)
        bar.addStretch(1)
        approve_all = QPushButton("Approve all")
        approve_all.clicked.connect(lambda: self._bulk_all("approved"))
        bar.addWidget(approve_all)
        reject_all = QPushButton("Reject all")
        reject_all.clicked.connect(lambda: self._bulk_all("rejected"))
        bar.addWidget(reject_all)
        v.addLayout(bar)

        self._review_scroll = QScrollArea()
        self._review_scroll.setWidgetResizable(True)
        inner = QWidget()
        self._review_layout = QVBoxLayout(inner)
        self._review_layout.setContentsMargins(0, 0, 0, 0)
        self._review_layout.addStretch(1)
        self._review_scroll.setWidget(inner)
        v.addWidget(self._review_scroll, 1)
        return page

    # ── plan table ───────────────────────────────────────────────────────────

    def _fill_plan_table(self) -> None:
        """Seed a row per gap before any AI has read them. A gap that arrived
        from Mass AI Search already carries its points and card target, so it's
        planned locally and never sent to the read call at all."""
        self._plans = []
        for kg in self._kgs:
            pre = kg_triage.plan_from_queued_gap(kg)
            if pre is not None:
                pre.note = pre.note or "Planned by Mass AI Search — points already checked against your deck."
                self._plans.append(pre)
                continue
            self._plans.append(kg_triage.KGPlan(
                kg_id=str(kg.get("id") or ""),
                title=str(kg.get("title") or "").strip(),
                kg_type=str(kg.get("type") or "kg").strip().lower() or "kg",
                concept="",
                cards=kg_triage.CREATE_BREADTHS[kg_triage.DEFAULT_BREADTH]["cards"],
                tags=[t for t in (kg.get("tags") or []) if t],
            ))
        self._render_plan_table()

    def _render_plan_table(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._plans))
        for row, p in enumerate(self._plans):
            item = QTableWidgetItem(p.title or "(untitled)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if p.included
                               else Qt.CheckState.Unchecked)
            self._table.setItem(row, 0, item)

            read = QTableWidgetItem(p.concept or "— not read yet —")
            if p.note:
                read.setToolTip(p.note)
            if p.confidence == "low":
                read.setText((p.concept or p.title) + "  ⚠")
            self._table.setItem(row, self._col_ix["What the AI read it as"], read)

            breadth = QComboBox()
            for key, meta in kg_triage.CREATE_BREADTHS.items():
                breadth.addItem(meta["label"], key)
                breadth.setItemData(breadth.count() - 1, meta["hint"],
                                    Qt.ItemDataRole.ToolTipRole)
            bi = breadth.findData(p.breadth)
            breadth.setCurrentIndex(bi if bi >= 0 else 0)
            breadth.currentIndexChanged.connect(lambda _i, r=row: self._on_breadth_changed(r))
            self._table.setCellWidget(row, self._col_ix["Breadth"], breadth)

            cards = QSpinBox()
            cards.setRange(kg_triage.MIN_CARDS, kg_triage.MAX_CARDS)
            cards.setValue(kg_triage.clamp_cards(p.cards))
            cards.valueChanged.connect(lambda _v, r=row: self._on_cards_changed(r))
            self._table.setCellWidget(row, self._col_ix["Cards"], cards)

            self._set_pts_cell(row, p)

            cov = QTableWidgetItem(getattr(p, "_covered_label", "") or "")
            cov.setFlags(cov.flags() & ~Qt.ItemFlag.ItemIsEditable)
            cov.setToolTip(getattr(p, "_covered_tip", "") or "")
            self._table.setItem(row, self._col_ix["Covered"], cov)

            if self._cols["tag"]:
                le = QLineEdit(getattr(p, "_row_tag", "") or "")
                le.setPlaceholderText("extra tag for this gap")
                attach_tag_completer(le, multi=False)
                self._table.setCellWidget(row, self._col_ix["Tag"], le)
            if self._cols["deck"]:
                dk = QComboBox()
                dk.setEditable(True)
                dk.addItem("")
                dk.addItems(_deck_names())
                dk.setCurrentText(getattr(p, "_row_deck", "") or "")
                self._table.setCellWidget(row, self._col_ix["Deck"], dk)
        self._table.blockSignals(False)
        self._refresh_title()

    def _set_pts_cell(self, row: int, p) -> None:
        """The Pts cell, and the warning when the budget can't cover the points.

        When the card budget is smaller than the decomposition, the surplus
        points are dropped at generation time (gen_prompt._points_block) — so
        say which ones here, rather than letting the user find out from the
        cards that didn't get written.
        """
        n_want = kg_triage.clamp_cards(p.cards)
        trimmed = bool(p.points) and len(p.points) > n_want
        label = f"{len(p.points)} → {n_want}" if trimmed else (
            str(len(p.points)) if p.points else "—")
        pts = QTableWidgetItem(label)
        pts.setFlags(pts.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if p.points:
            tip = "\n".join(
                ("• " if (i < n_want or not trimmed) else "✗ ") + pt.concept
                for i, pt in enumerate(p.points))
            if trimmed:
                tip = (f"Only {n_want} cards budgeted for {len(p.points)} points, "
                       "so the ✗ points won't be written.\nRaise the card count to "
                       "cover them.\n\n" + tip)
            pts.setToolTip(tip)
        self._table.setItem(row, self._col_ix["Pts"], pts)

    def _on_row_toggled(self, item) -> None:
        if item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self._plans):
            self._plans[row].included = item.checkState() == Qt.CheckState.Checked
        self._refresh_title()

    def _locks(self) -> dict:
        """The rows the user has hand-set, as {kg_id: {"breadth", "cards"}}.

        Reading with AI must not quietly undo a decision the user already made
        on screen — that was the single most annoying thing about the first
        version: set Dialysis to Core/4, press Read, watch it come back Deep/20.
        These go INTO the prompt (so the decomposition is sized to them) and are
        re-applied after parsing (so the model can't argue).
        """
        out = {}
        for p in self._plans:
            if getattr(p, "_user_set", False):
                out[p.kg_id] = {"breadth": p.breadth, "cards": p.cards}
        return out

    def _mark_user_set(self, row: int) -> None:
        if 0 <= row < len(self._plans):
            self._plans[row]._user_set = True

    def _on_breadth_changed(self, row: int) -> None:
        """Reset the card count to the new breadth's default.

        The two mean the same thing, so a hand-typed 14 left sitting on a row the
        user has just marked "Pinpoint" is the confusing outcome — the number
        would silently win over the word they actually chose.
        """
        if not (0 <= row < len(self._plans)):
            return
        combo = self._table.cellWidget(row, self._col_ix["Breadth"])
        spin = self._table.cellWidget(row, self._col_ix["Cards"])
        if combo is None or spin is None:
            return
        key = combo.currentData()
        meta = kg_triage.breadth_meta(key)
        self._plans[row].breadth = key
        self._plans[row].depth_band = meta["band"]
        spin.blockSignals(True)
        spin.setValue(kg_triage.clamp_cards(meta["cards"]))
        spin.blockSignals(False)
        self._plans[row].cards = spin.value()
        self._mark_user_set(row)
        self._set_pts_cell(row, self._plans[row])
        self._refresh_title()

    def _on_cards_changed(self, row: int) -> None:
        spin = self._table.cellWidget(row, self._col_ix["Cards"])
        if spin is not None and 0 <= row < len(self._plans):
            self._plans[row].cards = spin.value()
            self._set_pts_cell(row, self._plans[row])
        self._mark_user_set(row)
        self._refresh_title()

    def _sync_plans(self) -> None:
        """Fold every live widget value back into the plans, immediately before
        a run — so what is on screen is what runs."""
        for row, p in enumerate(self._plans):
            combo = self._table.cellWidget(row, self._col_ix["Breadth"])
            if combo is not None:
                p.breadth = combo.currentData() or p.breadth
                p.depth_band = kg_triage.breadth_meta(p.breadth)["band"]
            spin = self._table.cellWidget(row, self._col_ix["Cards"])
            if spin is not None:
                p.cards = spin.value()
            item = self._table.item(row, 0)
            if item is not None:
                p.included = item.checkState() == Qt.CheckState.Checked
            if self._cols["tag"]:
                le = self._table.cellWidget(row, self._col_ix["Tag"])
                if le is not None:
                    p._row_tag = le.text().strip()
            if self._cols["deck"]:
                dk = self._table.cellWidget(row, self._col_ix["Deck"])
                if dk is not None:
                    p._row_deck = dk.currentText().strip()

    def _active_plans(self) -> list:
        return [p for p in self._plans if p.included and p.ok]

    def _refresh_title(self) -> None:
        total = len(self._plans)
        active = [p for p in self._plans if p.included]
        runnable = [p for p in active if p.ok]
        budget = kg_triage.total_cards(runnable)
        over = budget > kg_triage.MAX_BATCH_CARDS
        colour = "#c0392b" if over else "inherit"
        bits = [f"<b>{len(active)} of {total}</b> gap(s)"]
        pts = sum(len(p.points) for p in runnable)
        if pts:
            bits.append(f"{pts} point(s)")
        bits.append(f"<span style='color:{colour}'>up to <b>{budget}</b> card(s)</span>")
        self._title.setText(" · ".join(bits))

    # ── coverage scope UI ────────────────────────────────────────────────────

    def _sync_scope_enabled(self) -> None:
        on = self._cov_cb.isChecked()
        for w in (self._cov_deck_rb, self._cov_tag_rb, self._cov_value, self._cov_btn):
            w.setEnabled(on)

    def _sync_scope_completer(self) -> None:
        if self._cov_deck_rb.isChecked():
            self._cov_value.setCompleter(QCompleter(_deck_names(), self._cov_value))
            self._cov_value.setPlaceholderText("deck to check against")
        else:
            attach_tag_completer(self._cov_value, multi=False)
            self._cov_value.setPlaceholderText("tag to check against")

    def _shared_group(self) -> str:
        """The group every selected gap is in, or "" when they disagree.

        Deliberately all-or-nothing: scoping a mixed batch to one gap's group
        would silently measure the others against a branch they were never
        filed under, and report them uncovered."""
        groups = {str(k.get("group") or "").strip() for k in self._kgs}
        if len(groups) != 1:
            return ""
        only = groups.pop()
        return only

    def _scope_settings(self) -> tuple[str, str]:
        if not self._cov_cb.isChecked():
            return "", ""
        kind = "deck" if self._cov_deck_rb.isChecked() else "tag"
        return kind, self._cov_value.text().strip()

    # ── status / busy ────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> bool:
        """False (and self-marks closed) once the widget is gone, so a worker
        callback arriving after the window was destroyed can bail quietly."""
        try:
            self._status.setText(msg)
            return True
        except RuntimeError:
            self._closed = True
            return False

    def _set_busy(self, busy: bool, msg: str = "", *,
                  progress: bool = False, stoppable: bool = False) -> None:
        self._busy = busy
        if msg:
            self._set_status(msg)
        try:
            self._progress.setVisible(busy and progress)
            if busy and progress:
                self._progress.setRange(0, 100)
                self._progress.setValue(0)
            self._stop_btn.setVisible(busy and stoppable)
            self._stop_btn.setEnabled(busy and stoppable)
            self._table.setEnabled(not busy)
            self._read_btn.setEnabled(not busy and not is_manual_provider())
            self._go_btn.setEnabled(not busy and not is_manual_provider())
            self._cov_btn.setEnabled(not busy and self._cov_cb.isChecked())
            # Close stays live throughout — being able to send a run to the
            # background is the whole point.
        except RuntimeError:
            self._closed = True

    def _resurface(self) -> None:
        """Bring a backgrounded dialog back when its run finishes."""
        if not self._backgrounded:
            return
        self._backgrounded = False
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except RuntimeError:
            self._closed = True

    # ── stage 1: read gaps with AI ───────────────────────────────────────────

    def _on_read(self, then=None) -> None:
        """Read the ticked gaps with AI.

        `then` is what to run once the reading has landed — used to chain
        Read → coverage check → Generate, so pressing Generate on an unread
        table just works instead of scolding the user for not pressing the
        button next to it first.
        """
        self._after_read = then
        self._sync_plans()
        # A gap already planned by Mass AI Search is left alone: its points were
        # measured against the deck, and re-reading them would cost tokens to
        # throw that measurement away.
        todo, pre_planned = [], 0
        for p in self._plans:
            if not p.included:
                continue
            kg = self._by_id.get(p.kg_id)
            if kg is None:
                continue
            if p.ok and kg_triage.plan_from_queued_gap(kg) is not None:
                pre_planned += 1
                continue
            todo.append(kg)
        if not todo:
            self._after_read = None
            if pre_planned and then is not None:
                then()          # already planned — the chain still continues
                return
            tooltip("Nothing left to read — every ticked gap is already planned."
                    if pre_planned else "Tick at least one gap first.")
            return

        # Stage READ, resolved through the one dial. This line used to read
        # tool_model_for("card_creation") — the same gaps that bulk_search read
        # on `lecture_extract`, read here on the generation dial, so which model
        # understood your shorthand depended on which button you pressed.
        model = pipeline.dial_for(pipeline.READ)
        locks = self._locks()
        note = f" ({pre_planned} already planned by Mass AI Search)" if pre_planned else ""
        self._set_busy(True, f"Reading {len(todo)} gap(s) with AI…{note}")

        def _report(msg, _pct=0):
            mw.taskman.run_on_main(lambda m=msg: self._set_status(m))

        def _bg():
            from ..lecture.panel import _Heartbeat
            with _Heartbeat(_report, f"Reading {len(todo)} gap(s) with AI…", 0):
                return kg_triage.triage_create(todo, model=model, locks=locks)

        mw.taskman.run_in_background(
            _bg, lambda fut: self._on_read_done(fut, todo), uses_collection=False)

    def _on_read_done(self, fut, todo: list[dict]) -> None:
        if self._closed:
            return
        self._set_busy(False)
        try:
            plans = fut.result()
        except core_api.ClaudeCancelled:
            self._set_status("Stopped.")
            return
        except Exception as e:
            log.error(f"mass create: reading gaps failed: {e}")
            self._set_status("Couldn't read those gaps.")
            showWarning(f"Couldn't read those knowledge gaps:\n\n{e}")
            return

        # Re-key onto the FULL list by kg_id: rows that weren't read (unticked,
        # or already planned by Search) must stay on the table rather than
        # vanishing from a batch the user can still see.
        fresh = {p.kg_id: p for p in plans}
        for i, old in enumerate(self._plans):
            new = fresh.get(old.kg_id)
            if new is None:
                continue
            new.included = old.included
            for attr in ("_row_tag", "_row_deck", "_user_set"):
                if hasattr(old, attr):
                    setattr(new, attr, getattr(old, attr))
            # Belt and braces on top of the prompt-side lock: whatever came back,
            # a row the user hand-set keeps the size they chose.
            if getattr(old, "_user_set", False):
                new.breadth = old.breadth
                new.depth_band = kg_triage.breadth_meta(old.breadth)["band"]
                new.cards = old.cards
            self._plans[i] = new
        self._render_plan_table()
        low = sum(1 for p in self._plans if p.confidence == "low" and p.included)
        held = sum(1 for p in self._plans if getattr(p, "_user_set", False) and p.included)
        msg = f"Read {len(plans)} gap(s)."
        if held:
            msg += f" {held} kept the breadth you set."
        if low:
            msg += f" {low} came back unsure — check the ⚠ rows before generating."
        self._set_status(msg)

        # Reading and coverage-checking are one thought, not two buttons: the
        # answer to "what should I write?" is incomplete until you know what the
        # deck already has. So when a scope is set, the check follows the read.
        then, self._after_read = self._after_read, None
        _kind, value = self._scope_settings()
        if self._cov_cb.isChecked() and value:
            self._on_check_coverage(then=then)
        elif then is not None:
            then()

    # ── stage 2: generate ────────────────────────────────────────────────────

    def _on_generate(self) -> None:
        if not anki_utils.require_col():
            return
        self._sync_plans()
        plans = self._active_plans()
        if not plans:
            unread = [p for p in self._plans if p.included and not p.ok]
            if unread:
                # Reading is a precondition, not a separate user decision — so
                # do it and carry on into the generate the user actually asked
                # for, rather than making them press the other button and come
                # back. Guarded so a failed read can't loop.
                if not self._auto_read_done:
                    self._auto_read_done = True
                    self._on_read(then=self._on_generate)
                    return
                showWarning("Those gaps couldn't be read, so there's nothing to "
                            "generate from. Try Read gaps with AI again.")
                return
            showWarning("Tick at least one gap first.")
            return

        budget = kg_triage.total_cards(plans)
        if budget > kg_triage.MAX_BATCH_CARDS and not askUser(
                f"That's {budget} cards across {len(plans)} gaps.\n\n"
                "This will take a while and cost a lot of tokens. Go ahead?",
                defaultno=True):
            return

        notetype_name = self._notetype.currentText().strip()
        if not notetype_name:
            showWarning("Pick a notetype first (Settings → AI Create adds profiles).")
            return
        deck_name = self._deck.currentText().strip() or "Default"

        # Must happen on the main thread BEFORE the jobs go off-thread, or every
        # job classifies against a cold vocab cache and invents its own tag branch.
        try:
            from .. import autotag
            autotag.refresh_vocab_cache()
        except Exception as e:
            log.warn(f"mass create: vocab cache refresh failed: {e}")

        base_tags = [t.strip() for t in (self.cfg.get("default_tags") or []) if str(t).strip()]
        audit = (self.cfg.get("audit_tag") or "").strip()
        if audit:
            base_tags.append(audit)
        tags_by_kg = {p.kg_id: getattr(p, "_row_tag", "") for p in plans}

        records = gen_prompt.build_batch(
            plans=plans, gaps_by_id=self._by_id, deck_name=deck_name,
            notetype_name=notetype_name, base_tags=base_tags,
            tags_by_kg=tags_by_kg, model=pipeline.dial_for(pipeline.WRITE),
            focus=self._focus.text().strip())
        if not records:
            showWarning("Couldn't build a request for any of those gaps — see the log.")
            return
        # Per-row deck overrides ride on the record itself (the column is off by
        # default; when it's on, a blank cell means "use the batch deck").
        if self._cols["deck"]:
            by_kg = {p.kg_id: getattr(p, "_row_deck", "") for p in plans}
            for rec in records:
                override = (by_kg.get(rec.get("kg_id")) or "").strip()
                if override:
                    rec["deck_name"] = override

        self._batch_id = records[0]["batch_id"]
        self._remember_batch(deck_name, notetype_name)
        create_jobs.enqueue_batch(records)
        self._mark_in_progress([r["kg_id"] for r in records])

        self._stack.setCurrentIndex(1)
        self._back_btn.setVisible(True)
        self._read_btn.setVisible(False)
        self._go_btn.setVisible(False)
        self._skip_btn.setVisible(True)
        self._skip_btn.setEnabled(False)
        self._set_busy(True, f"Generating cards for {len(records)} gap(s)…",
                       progress=True, stoppable=True)
        self._poll.start()
        self._on_poll()

    def _restore_batch_settings(self) -> None:
        """Put the deck/notetype this batch was enqueued with back on the combos.

        Falls back to the first job's own record if the config entry is gone —
        the record is the more durable of the two, since it's what the worker
        actually used.
        """
        info = (tool_config("card_creator").get("mass_batches") or {}).get(self._batch_id) or {}
        deck = info.get("deck") or ""
        notetype = info.get("notetype") or ""
        if not (deck and notetype):
            jobs = create_jobs.by_batch(self._batch_id)
            if jobs:
                deck = deck or jobs[0].get("deck_name") or ""
                notetype = notetype or jobs[0].get("notetype_name") or ""
        if deck:
            self._deck.setCurrentText(deck)
        if notetype:
            ix = self._notetype.findText(notetype)
            if ix >= 0:
                self._notetype.setCurrentIndex(ix)
            else:
                # The profile was renamed or deleted since. Keep the name visible
                # so the mismatch is obvious rather than silently committing to
                # whatever happens to be selected.
                self._notetype.addItem(notetype)
                self._notetype.setCurrentIndex(self._notetype.count() - 1)

    def _remember_batch(self, deck_name: str, notetype_name: str) -> None:
        """Persist enough to reopen this batch after a restart. Additive: only
        this key is touched, so nothing else in the config is disturbed."""
        try:
            cfg = tool_config("card_creator")
            batches = dict(cfg.get("mass_batches") or {})
            batches[self._batch_id] = {
                "deck": deck_name, "notetype": notetype_name,
                "kg_ids": [p.kg_id for p in self._active_plans()],
            }
            cfg["mass_batches"] = batches
            save_tool_config("card_creator", cfg)
        except Exception as e:
            log.warn(f"mass create: couldn't record batch: {e}")

    def _forget_batch(self) -> None:
        try:
            cfg = tool_config("card_creator")
            batches = dict(cfg.get("mass_batches") or {})
            if batches.pop(self._batch_id, None) is not None:
                cfg["mass_batches"] = batches
                save_tool_config("card_creator", cfg)
        except Exception as e:
            log.warn(f"mass create: couldn't clear batch: {e}")

    def _mark_in_progress(self, kg_ids) -> None:
        """A gap handed to a tool is no longer untouched. Only Open gaps move —
        generating from a Done one doesn't reopen it."""
        for kid in kg_ids:
            try:
                kg = kg_store.get(kid)
                if kg and kg.get("status") == "open":
                    kg_store.update(kid, status="in_progress")
            except Exception as e:
                log.warn(f"mass create: couldn't mark {kid} in progress: {e}")
        self._refresh_kg_panel()

    def _refresh_kg_panel(self) -> None:
        try:
            from .. import knowledge_gaps
            knowledge_gaps._refresh_open_panel()
        except Exception:
            pass

    def _on_stop(self) -> None:
        n = create_jobs.cancel_batch(self._batch_id)
        self._stop_btn.setEnabled(False)
        self._set_status(f"Stopping {n} job(s) — anything already finished is kept.")

    # ── polling ──────────────────────────────────────────────────────────────

    def _on_poll(self) -> None:
        if self._closed:
            return
        jobs = create_jobs.by_batch(self._batch_id)
        if not jobs:
            self._poll.stop()
            self._set_busy(False, "That batch is no longer in the job list.")
            return
        counts = create_jobs.batch_progress(self._batch_id)
        self._render_run_tree(jobs)
        try:
            pct = int(100 * counts["done"] / max(1, counts["total"]))
            self._progress.setValue(pct)
        except RuntimeError:
            self._closed = True
            return
        self._skip_btn.setEnabled(counts["ready"] > 0)
        if counts["finished"]:
            self._poll.stop()
            self._set_busy(False)
            self._resurface()
            msg = f"Generated {counts['cards']} card(s) across {counts['ready']} gap(s)."
            if counts["error"]:
                msg += f" {counts['error']} failed."
            self._set_status(msg)
            self._show_review(jobs)
        else:
            self._set_status(
                f"{counts['ready']} of {counts['total']} done · "
                f"{counts['running']} generating · {counts['queued']} queued"
                + (f" · {counts['error']} failed" if counts["error"] else ""))

    def _render_run_tree(self, jobs: list[dict]) -> None:
        labels = {
            create_jobs.QUEUED: "queued",
            create_jobs.RUNNING: "⏳ generating…",
            create_jobs.READY: "✅ ready",
            create_jobs.ERROR: "⚠️ failed",
            create_jobs.INTERRUPTED: "⛔ interrupted",
        }
        try:
            self._run_tree.clear()
            for j in jobs:
                n = len(j.get("cards") or [])
                it = QTreeWidgetItem([
                    j.get("title") or "(untitled)",
                    labels.get(j.get("status"), str(j.get("status"))),
                    str(n) if n else "",
                ])
                if j.get("status") == create_jobs.ERROR and j.get("error"):
                    it.setToolTip(1, str(j["error"]))
                self._run_tree.addTopLevelItem(it)
        except RuntimeError:
            self._closed = True

    # ── stage 3: review ──────────────────────────────────────────────────────

    def _show_review(self, jobs: list[dict]) -> None:
        from .. import card_creator as cc

        # Clear any previous render (a retry can bring us back here).
        for g in self._groups:
            g.setParent(None)
        self._groups = []

        profile = cc._resolved_profile(tool_config("card_creator"),
                                       self._notetype.currentText().strip())
        front_field = profile.get("front_field", "Text")

        # Dup-check in ONE pass rather than per card as the rows are built: it's
        # a find_notes per card on the main thread, and a 12-gap batch is easily
        # 150 of them.
        dup_msgs: dict[int, str] = {}
        seen: dict[str, str] = {}
        idx = 0
        for j in jobs:
            for card in (j.get("cards") or []):
                msg = cc.dup_check(card, front_field)
                # Within-batch near-duplicates are a hazard unique to generating
                # from related gaps: two gaps about the same entity will produce
                # the same card, and nothing culls a newly-written duplicate the
                # way search dedupes a matched one. Flag, never auto-reject.
                key = _norm_front(card)
                if key and key in seen:
                    extra = f"⚠ near-duplicate of a card under '{seen[key]}'"
                    msg = f"{msg}  ·  {extra}" if msg else extra
                elif key:
                    seen[key] = j.get("title") or "another gap"
                if msg:
                    dup_msgs[idx] = msg
                idx += 1

        idx = 0
        for j in jobs:
            group = _KGGroup(j, profile=profile, dup_msgs=dup_msgs, start_index=idx,
                             on_counts_changed=self._refresh_review_head,
                             on_retry=self._on_retry_job, parent=self)
            idx += len(j.get("cards") or [])
            self._review_layout.insertWidget(self._review_layout.count() - 1, group)
            self._groups.append(group)
        # Only the first group is expanded: building every _CardRow up front for
        # a big batch visibly stalls, and the rest are one click away.
        if self._groups:
            self._groups[0].set_expanded(True)

        self._stack.setCurrentIndex(2)
        self._back_btn.setVisible(False)
        self._skip_btn.setVisible(True)
        self._skip_btn.setEnabled(True)
        self._add_btn.setVisible(True)
        self._refresh_review_head()

    def _refresh_review_head(self) -> None:
        approved = sum(g.count_approved() for g in self._groups)
        rejected = sum(g.count_rejected() for g in self._groups)
        total = sum(g.count_total() for g in self._groups)
        gaps = sum(1 for g in self._groups if g.count_total())
        try:
            self._review_head.setText(
                f"{approved} approved · {rejected} rejected · {total} across {gaps} gap(s)")
            self._add_btn.setEnabled(approved > 0)
        except RuntimeError:
            self._closed = True

    def _bulk_all(self, state: str) -> None:
        for g in self._groups:
            g.set_all(state)
        self._refresh_review_head()

    def _on_retry_job(self, job_id: str) -> None:
        create_jobs.retry_job(job_id)
        self._stack.setCurrentIndex(1)
        self._set_busy(True, "Retrying…", progress=True, stoppable=True)
        self._poll.start()

    def _on_back(self) -> None:
        self._stack.setCurrentIndex(0)
        self._back_btn.setVisible(False)
        self._read_btn.setVisible(True)
        self._go_btn.setVisible(True)
        self._skip_btn.setVisible(False)

    # ── commit ───────────────────────────────────────────────────────────────

    def _on_add_approved(self) -> None:
        self._commit([(g, g.approved_cards()) for g in self._groups])

    def _on_add_without_review(self) -> None:
        """Add everything ready, without opening the read-through.

        Not blind: the quality pass already graded these cards and its failures
        are dropped, as are within-batch duplicate copies. The button skips the
        READING, not the checks.
        """
        jobs = [j for j in create_jobs.by_batch(self._batch_id)
                if j.get("status") == create_jobs.READY]
        if not jobs:
            tooltip("Nothing is ready yet.")
            return
        if not self._groups:
            self._show_review(jobs)

        batches, kept, culled = [], 0, 0
        for g in self._groups:
            good = g.passing_cards()
            culled += g.count_total() - len(good)
            kept += len(good)
            batches.append((g, good))
        if not kept:
            showWarning("Every generated card was dropped by the quality check.")
            return
        deck = self._deck.currentText().strip() or "Default"
        extra = f" ({culled} dropped by the quality check)" if culled else ""
        if not askUser(
                f"Add {kept} card(s) to '{deck}' across {len(batches)} gap(s) "
                f"without reviewing them?{extra}", defaultno=True):
            return
        self._commit(batches, confirm=False)

    def _commit(self, batches, *, confirm: bool = True) -> None:
        from .. import card_creator as cc
        if not anki_utils.require_col():
            return
        batches = [(g, cards) for g, cards in batches if cards]
        if not batches:
            tooltip("No approved cards.")
            return
        total = sum(len(c) for _g, c in batches)
        deck = self._deck.currentText().strip() or "Default"
        if confirm and not askUser(
                f"Create {total} card(s) across {len(batches)} gap(s) in deck '{deck}'?"):
            return

        profile = cc._resolved_profile(tool_config("card_creator"),
                                       self._notetype.currentText().strip())
        nt = cc.resolve_notetype(profile)
        if nt is None:
            return

        # ONE checkpoint around the whole batch — the point of committing here
        # rather than gap by gap is that a single Ctrl+Z unrolls the lot.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Mass AI Create")
        created_by_kg: dict[str, list] = {}
        for group, cards in batches:
            try:
                ids = cc.commit_cards(cards, ctx=group.commit_ctx(profile),
                                      deck_name=group.deck_name or deck, notetype=nt)
            except Exception as e:
                log.error(f"mass create: committing '{group.title}' failed: {e}")
                continue
            created_by_kg[group.kg_id] = ids
        mw.reset()

        n = sum(len(v) for v in created_by_kg.values())
        for kg_id, ids in created_by_kg.items():
            if not ids:
                continue
            try:
                kg_store.update(kg_id, status="done")
            except Exception as e:
                log.warn(f"mass create: couldn't mark {kg_id} done: {e}")
        self._refresh_kg_panel()
        try:
            from .. import knowledge_gaps
            knowledge_gaps.maybe_sync_to_things()
        except Exception:
            pass
        self._log_sessions(created_by_kg)

        create_jobs.remove_batch(self._batch_id)
        self._forget_batch()
        self._committed = True
        tooltip(f"Created {n} card(s) across {len(created_by_kg)} gap(s).")
        self.accept()

    def _log_sessions(self, created_by_kg: dict) -> None:
        """One session entry per gap — card_ids stay attributable to the gap they
        came from, with `source` carrying the batch grouping."""
        from .. import card_creator as cc
        for group in self._groups:
            ids = created_by_kg.get(group.kg_id)
            if not ids:
                continue
            try:
                cc._append_session(
                    mode="topic", source=f"mass-create:{self._batch_id}",
                    topic=group.title, cards_proposed=group.count_total(),
                    cards_created=len(ids), card_ids=ids)
            except Exception as e:
                log.warn(f"mass create: session log failed: {e}")

    # ── coverage check ───────────────────────────────────────────────────────

    def _on_check_coverage(self, then=None) -> None:
        """Search the named deck/tag for each point, and drop the ones it covers.

        Reuses the AI Lecture pipeline exactly as bulk search does — passing
        `points` short-circuits extraction, so everything from the deck search
        onward is identical to a lecture run. The only new part is the scope.
        """
        if not anki_utils.require_col():
            return
        self._after_coverage = then
        self._sync_plans()
        plans = self._active_plans()
        if not plans:
            self._after_coverage = None
            showWarning("Read the gaps first — there's nothing to check yet.")
            return
        kind, value = self._scope_settings()
        if not value:
            self._after_coverage = None
            showWarning("Type the deck or tag to check against first.")
            return

        from ..lecture import matcher
        from ..lecture.panel import run_pipeline
        cfg = dict(tool_config("lecture"))
        cfg["lo_text"] = ""                 # never the LO branch: points win
        cfg["search_scope"] = matcher.scope_query(kind, value)
        cfg["target_cards"] = kg_triage.total_cards(plans)
        cfg["depth"] = "thorough"
        points = kg_triage.points_of(plans)

        self._cancel_ev = threading.Event()
        cancel_ev = self._cancel_ev
        self._set_busy(True, "Checking coverage…", progress=True, stoppable=True)

        def _report(msg, pct):
            def _ui():
                if self._set_status(msg):
                    self._progress.setValue(int(pct))
            mw.taskman.run_on_main(_ui)

        def _bg():
            core_api.set_cancel_event(cancel_ev)
            try:
                return run_pipeline(
                    None, cfg, report=_report, cancel_ev=cancel_ev, points=points,
                    cap_for=kg_triage.cap_for(plans),
                    # We're asking "does anything cover this?", not "which is the
                    # best card" — a lean judge pool is enough, and inside a
                    # narrow scope the pools are small anyway.
                    cand_cap_for=lambda _p: 6)
            finally:
                core_api.set_cancel_event(None)

        mw.taskman.run_in_background(_bg, self._on_coverage_done, uses_collection=False)

    def _on_coverage_done(self, fut) -> None:
        if self._closed:
            return
        self._set_busy(False)
        then, self._after_coverage = self._after_coverage, None
        try:
            _lecture, _matches, coverage = fut.result()
        except core_api.ClaudeCancelled:
            self._set_status("Stopped.")
            return
        except Exception as e:
            log.error(f"mass create: coverage check failed: {e}")
            self._set_status("Coverage check failed.")
            showWarning(f"Couldn't check coverage:\n\n{e}")
            return

        # matcher.Coverage already splits the points into covered/gaps — that's
        # its whole job, and it's the same judgement Mass AI Search reports, so
        # the two tools can't disagree about what the deck holds.
        covered: dict[str, set] = {}
        for m in list(getattr(coverage, "covered", []) or []):
            kg_id = getattr(m.point, "kg_id", "")
            if kg_id:
                covered.setdefault(kg_id, set()).add(id(m.point))

        dropped_total, untouched = 0, 0
        for p in self._plans:
            if not p.included or not p.ok:
                continue
            hit = covered.get(p.kg_id) or set()
            keep = [pt for pt in p.points if id(pt) not in hit]
            n_before, n_keep = len(p.points), len(keep)
            dropped_total += n_before - n_keep
            p._covered_label = f"{n_before - n_keep}/{n_before}"
            p._covered_tip = (
                "Already covered:\n" +
                "\n".join(f"• {pt.concept}" for pt in p.points if id(pt) in hit)
            ) if hit else "Nothing in that scope covers this gap."
            if n_keep == 0:
                # Fully covered — visibly unticked rather than removed, so it's
                # clear WHY the gap isn't in the run and it can be put back.
                p.included = False
                p._covered_tip = "Fully covered in that scope — nothing left to write."
                continue
            if n_keep < n_before:
                # Scale the budget to what's left rather than writing the
                # original number of cards about a smaller set of points.
                p.cards = max(1, round(kg_triage.clamp_cards(p.cards) * n_keep / n_before))
                p.points = keep
            else:
                untouched += 1
        self._render_plan_table()
        self._set_status(
            f"Coverage check done — {dropped_total} point(s) already covered in that "
            f"scope and dropped. {untouched} gap(s) unchanged.")
        if then is not None:
            then()

    # ── closing ──────────────────────────────────────────────────────────────

    def closeEvent(self, ev) -> None:
        running = False
        if self._batch_id and not self._committed:
            c = create_jobs.batch_progress(self._batch_id)
            running = c["total"] > 0 and not c["finished"]
        if running:
            if askUser("Keep generating in the background?\n\n"
                       "The run continues either way — this just hides the window. "
                       "Reopen it from AI Create.", defaultno=False):
                self._backgrounded = True
                ev.ignore()
                self.hide()
                return
            create_jobs.cancel_batch(self._batch_id)
        self._closed = True
        try:
            self._poll.stop()
        except RuntimeError:
            pass
        self._save_scope_settings()
        super().closeEvent(ev)

    def reject(self) -> None:
        # Route Esc through close() so it can't bypass the background offer.
        self.close()

    def _save_scope_settings(self) -> None:
        try:
            kind, value = ("deck" if self._cov_deck_rb.isChecked() else "tag",
                           self._cov_value.text().strip())
            cfg = tool_config("card_creator")
            # A scope derived from this batch's group is per-batch state, not a
            # preference. Remembering it would prefill the NEXT batch — quite
            # possibly a different group — with this one's tag branch, which is
            # worse than not prefilling at all.
            if value and value == getattr(self, "_group_scope", ""):
                value = ""
            cfg["mass_create_scope"] = {
                "enabled": bool(self._cov_cb.isChecked()),
                "kind": kind, "value": value,
            }
            save_tool_config("card_creator", cfg)
        except Exception:
            pass


def commit_ctx_for(job: dict, profile: dict, tags: list):
    """A job's CommitCtx — the batch's profile, but this gap's own tags and KG
    content, so an MQ gap still composes its Missed Questions field correctly
    inside a mixed batch.

    Module-level because two surfaces now build cards from the same job records:
    this dialog's review blocks, and the unified pass, which renders the written
    cards straight into its results tree and so has no block to ask."""
    from .. import card_creator as cc
    return cc.CommitCtx(
        profile=profile,
        tags=list(tags),
        mode="topic",
        source_label=None,
        panel_image_paths=[],
        kg_image_filenames=list(job.get("kg_image_filenames") or []),
        kg_type_for_image=(job.get("kg_type_for_image") or "").lower(),
        kg_stem_html=str(job.get("kg_stem_html") or ""),
        kg_notes=str(job.get("kg_notes") or ""),
        kg_concept=str(job.get("kg_concept") or ""),
        kg_explanation=str(job.get("kg_explanation") or ""),
        type_meta=job.get("type_meta") if isinstance(job.get("type_meta"), dict) else None,
        # From the GAP, not the job: the worker persists AI-written field values
        # back onto the gap record (create_jobs._apply_result_main_thread), so
        # that is where a freshly-written explanation actually lives.
        kg_field_values=cc._gap_field_values(
            job.get("gap") if isinstance(job.get("gap"), dict) else None),
    )


# ── one gap's cards, in the review page ──────────────────────────────────────

class _KGGroup(QFrame):
    """A collapsible block of one gap's generated cards.

    Rows are built LAZILY on first expand: a 12-gap batch is easily 150 _CardRow
    frames, and constructing them all up front stalls the UI for seconds on a
    screen the user is about to scroll past.
    """

    def __init__(self, job: dict, *, profile: dict, dup_msgs: dict,
                 start_index: int, on_counts_changed, on_retry, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.job = job
        self.job_id = job.get("id") or ""
        self.kg_id = job.get("kg_id") or ""
        self.title = job.get("title") or "(untitled)"
        self.deck_name = (job.get("deck_name") or "").strip()
        self.profile = profile
        self.cards = list(job.get("cards") or [])
        self.verdicts = list(job.get("verdicts") or [])
        self._dup_msgs = dup_msgs
        self._start = start_index
        self._on_counts_changed = on_counts_changed
        self._on_retry = on_retry
        self.rows: list = []
        self._built = False
        # What un-built rows count as. Cards start approved (matching _CardRow's
        # own default); a group-level Approve/Reject moves this so the counts
        # stay honest without the rows having to exist yet.
        self._default_state = "approved"

        v = QVBoxLayout(self)
        head = QHBoxLayout()
        self._toggle = QToolButton()
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setCheckable(True)
        self._toggle.setAutoRaise(True)
        self._toggle.toggled.connect(self.set_expanded)
        head.addWidget(self._toggle)

        self._head_lbl = QLabel()
        self._head_lbl.setStyleSheet("font-weight: 600;")
        head.addWidget(self._head_lbl)
        head.addStretch(1)

        failed = job.get("status") in (create_jobs.ERROR, create_jobs.INTERRUPTED)
        if failed:
            retry = QPushButton("Retry")
            retry.clicked.connect(lambda: self._on_retry(self.job_id))
            head.addWidget(retry)
        else:
            ok_btn = QPushButton("Approve gap")
            ok_btn.clicked.connect(lambda: self._set_all_and_notify("approved"))
            head.addWidget(ok_btn)
            no_btn = QPushButton("Reject gap")
            no_btn.clicked.connect(lambda: self._set_all_and_notify("rejected"))
            head.addWidget(no_btn)
        v.addLayout(head)

        if failed:
            err = QLabel("⚠️ " + str(
                job.get("error")
                or ("Stopped before this one was written."
                    if job.get("status") == create_jobs.INTERRUPTED
                    else "Generation failed.")))
            err.setWordWrap(True)
            err.setStyleSheet("color: #c0392b;")
            v.addWidget(err)
            self._toggle.setEnabled(False)

        # The tag box carries what the job was enqueued with, including the
        # auto-tag the model resolved — editable right up to commit.
        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tags:"))
        self.tag_edit = QLineEdit(", ".join(job.get("tags_raw") or []))
        attach_tag_completer(self.tag_edit, multi=True)
        tag_row.addWidget(self.tag_edit, 1)
        v.addLayout(tag_row)

        self.inner = QWidget()
        self.inner_layout = QVBoxLayout(self.inner)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner.setVisible(False)
        v.addWidget(self.inner)
        self._refresh_head()

    # ── rows ─────────────────────────────────────────────────────────────────

    def set_expanded(self, on: bool) -> None:
        if on and not self._built:
            self._build_rows()
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if on
                                  else Qt.ArrowType.RightArrow)
        if self._toggle.isChecked() != on:
            self._toggle.setChecked(on)
        self.inner.setVisible(on)

    def _build_rows(self) -> None:
        from .. import card_creator as cc
        self._built = True
        for i, card in enumerate(self.cards):
            row = cc._CardRow(
                i, card, self._dup_msgs.get(self._start + i),
                self._on_row_state, self._on_regen, self._on_split, self._on_obo,
                slide_pages=[], parent=self.inner)
            # Carry any group-level Approve/Reject made while these rows were
            # still unbuilt, or expanding would silently undo it.
            if self._default_state != row.state:
                row.state = self._default_state
                row._refresh_header()
            self.inner_layout.addWidget(row)
            self.rows.append(row)
        self._refresh_head()

    def _on_row_state(self, *_a) -> None:
        self._refresh_head()
        self._on_counts_changed()

    def _on_regen(self, old_card, hint: str = ""):
        from .. import card_creator as cc
        return cc._regenerate_one(old_card, hint, self.profile, "",
                                  pipeline.dial_for(pipeline.WRITE))

    def _on_obo(self, old_card):
        from .. import card_creator as cc
        return cc.one_by_one_rewrite(old_card, self.profile)

    def _on_split(self, row):
        from .. import card_creator as cc
        parts = cc.split_card(row.card, self.profile)
        if not parts:
            return
        at = self.inner_layout.indexOf(row)
        row.setParent(None)
        if row in self.rows:
            self.rows.remove(row)
        for k, card in enumerate(parts):
            new = cc._CardRow(len(self.rows) + k, card, None,
                              self._on_row_state, self._on_regen, self._on_split,
                              self._on_obo, slide_pages=[], parent=self.inner)
            self.inner_layout.insertWidget(at + k, new)
            self.rows.append(new)
        self._refresh_head()
        self._on_counts_changed()

    # ── state ────────────────────────────────────────────────────────────────

    def _states(self) -> list[str]:
        if self._built:
            return [r.state for r in self.rows]
        # Not expanded yet, so there are no rows to ask — every card is at this
        # group's default. Tracking that default (rather than forcing the rows to
        # exist) is what lets "Reject all" stay instant on a 150-card batch.
        return [self._default_state] * len(self.cards)

    def count_total(self) -> int:
        return len(self.rows) if self._built else len(self.cards)

    def count_approved(self) -> int:
        from .. import card_creator as cc
        return sum(1 for s in self._states() if s == cc._CardRow.APPROVED)

    def count_rejected(self) -> int:
        from .. import card_creator as cc
        return sum(1 for s in self._states() if s == cc._CardRow.REJECTED)

    def set_all(self, state: str) -> None:
        self._default_state = state
        for r in self.rows:
            r.state = state
            r._refresh_header()
        self._refresh_head()

    def _set_all_and_notify(self, state: str) -> None:
        self.set_all(state)
        self._on_counts_changed()

    def approved_cards(self) -> list[dict]:
        from .. import card_creator as cc
        if not self._built:
            return list(self.cards) if self._default_state == cc._CardRow.APPROVED else []
        return [r.card for r in self.rows if r.state == cc._CardRow.APPROVED]

    def passing_cards(self) -> list[dict]:
        """Approved cards minus anything the quality pass failed — what the
        skip-the-read-through path adds."""
        cards = self.approved_cards()
        if not self.verdicts:
            return cards
        # Verdicts are stored as plain dicts (create_jobs._verdict_to_dict), one
        # per card in order. Only FAIL is dropped — FLAG means "worth a look",
        # which is exactly the judgement this button is declining to make.
        bad = set()
        for i, v in enumerate(self.verdicts):
            if not isinstance(v, dict):
                continue
            if str(v.get("verdict") or "").upper() == "FAIL":
                bad.add(i)
        if not bad:
            return cards
        keep = [c for i, c in enumerate(self.cards) if i not in bad]
        keep_ids = {id(c) for c in keep}
        return [c for c in cards if id(c) in keep_ids]

    def commit_ctx(self, profile: dict):
        """This gap's CommitCtx, built from the tag box on this block."""
        tags = [t.strip() for t in self.tag_edit.text().split(",") if t.strip()]
        return commit_ctx_for(self.job, profile, tags)

    def _refresh_head(self) -> None:
        n = self.count_total()
        st = self.job.get("status")
        if st in (create_jobs.ERROR, create_jobs.INTERRUPTED):
            self._head_lbl.setText(
                f"{self.title} — "
                + ("stopped" if st == create_jobs.INTERRUPTED else "failed"))
            return
        self._head_lbl.setText(
            f"{self.title}    {n} card(s) · {self.count_approved()} approved")


# ── entry point ──────────────────────────────────────────────────────────────

def open_mass_create(kgs: list[dict], parent=None, batch_id: str = "") -> None:
    """Open the dialog on a selection of KG records (full records from
    tools/kg/store.py). Non-modal, like bulk search."""
    kgs = [k for k in (kgs or []) if isinstance(k, dict)
           and str(k.get("title") or "").strip()]
    if not kgs and not batch_id:
        tooltip("No knowledge gaps to generate from.")
        return
    dlg = MassCreateDialog(kgs, parent=parent, batch_id=batch_id)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    # Read straight away rather than presenting a table of "— not read yet —".
    # Until the gaps are read there is nothing on this screen worth deciding
    # about: no concept, no points, and a breadth that is just the default.
    # singleShot(0) so the window is painted before the busy state lands on it.
    if not batch_id and any(not p.ok for p in dlg._plans):
        dlg._auto_read_done = True
        QTimer.singleShot(0, lambda: (not dlg._closed) and dlg._on_read())
