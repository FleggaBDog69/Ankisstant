# Bulk knowledge-gap search — the popup.
#
# Launched from the AI Browse queue (and the Knowledge Gaps page), this works a
# whole batch of gaps in one pass instead of one at a time. It owns the UI; all
# the thinking is borrowed:
#
#   tools/kg/triage.py       reads the batch — scope, card target, decomposition
#   tools/lecture/matcher.py finds and judges the cards (tag leg + term leg +
#                            relevance judge + dedupe + budget allocation)
#   tools/lecture/panel.py   run_pipeline(), entered with points= so it skips
#                            ingest/extraction entirely
#
# A DIALOG, not a tab, on purpose: bulk work is occasional and dense, and the
# Browse panel is already a busy single-topic surface. It opens over Browse,
# does its job, and goes away.
#
# UNIFIED MODE IS ONE RESULT SCREEN, NOT A WIZARD.
# The results page splits in two: what your deck already had on top, and — for
# every point it turned out NOT to have — the cards written for it underneath,
# generated automatically as soon as the search lands. Both halves are reviewed
# side by side and committed by ONE button under ONE checkpoint, so a single
# Ctrl+Z takes the entire pass back out. Generation runs while you read the
# found cards, which is the real reason the two halves share a screen: in a
# wizard that time is spent staring at a progress bar.
#
# WHY THE RESULTS ARE GROUPED BY GAP, AND TAGGED PER GAP.
# A lecture commits everything under one tag because everything came from one
# lecture. A batch of gaps did not — they're unrelated, and one shared tag would
# throw away the classification triage already did. So every gap gets its OWN
# editable tag (prefilled with the hierarchical base::Type::System::Subsystem::
# Topic tag, the same scheme AI Browse and AI Create use) and its own subtree in
# the results, showing which of its points the deck covers and which it doesn't.

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar,
    QPushButton, QSpinBox, QStackedWidget, Qt, QTableWidget, QTableWidgetItem,
    QTimer, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showWarning, tooltip

from ...core import anki_utils, api as core_api, log
from ...core.config import month_tag, tool_config
from ...core.qt_utils import (
    attach_tag_completer, is_manual_provider, provider_configured, theme_dialog,
)
from .. import autotag
from . import store as kg_store, triage as kg_triage


# How many judge-approved-but-not-chosen cards to offer per point, under the
# ones the budget actually picked. The point is override room, not a second
# results list: the allocator's choice is what's ticked, and this is the short
# tail he can promote from if it got one wrong.
EXTRA_CANDIDATES_PER_POINT = 6

# Plan-table columns, named because the table is index-addressed in a dozen
# places and inserting one by hand is how the scope combo ends up in the card
# spinner's column.
C_GAP, C_NOTES, C_READ, C_SCOPE, C_CARDS, C_PTS = range(6)


def create_record_for(kg: dict, concept: str, points: list[dict]) -> dict:
    """One gap, flattened into the record shape the generation side reads.

    FLATTENED, not dict(kg): gen_prompt.build_generation_request and
    build_job_record read `notes`, `stem_html`, `concept` and `explanation` off
    the TOP level, while the store keeps them under fields{}. Handing over a raw
    store record silently wrote cards with none of the gap's own context.
    `fields` rides along as well, for the declarative field values
    card_creator._gap_field_values resolves at commit time.

    `points` are the ones the deck was just measured against and found wanting,
    already serialised. They go in twice on purpose: structured, for
    triage.plan_from_queued_gap to rebuild a plan with no AI call, and as prose
    on the end of the notes, because that is what the model actually reads.
    """
    kg_id = str(kg.get("kg_id") or kg.get("id") or "")
    kg_type = str(kg.get("type") or kg.get("kg_type") or "").lower()
    notes_bits = []
    own = kg_store.field(kg, "notes")
    if own:
        notes_bits.append(own)
    concepts = [str(p.get("concept") or "").strip() for p in points]
    concepts = [c for c in concepts if c]
    if concepts:
        notes_bits.append("My deck has no card for these parts of this gap:\n"
                          + "\n".join(f"\u2022 {c}" for c in concepts))
    return {
        "id":           kg_id,
        "kg_id":        kg_id,
        "title":        str(kg.get("title") or ""),
        "type":         kg_type,
        "kg_type":      kg_type,
        "group":        str(kg.get("group") or "").strip(),
        "tags":         [t for t in (kg.get("tags") or []) if t],
        "fields":       dict(kg.get("fields") or {}),
        "stem_html":    kg_store.field(kg, "stem_html"),
        "explanation":  kg_store.field(kg, "explanation"),
        "images":       list((kg.get("fields") or {}).get("images") or []),
        "concept":      str(concept or "").strip() or str(kg.get("title") or ""),
        "notes":        "\n\n".join(notes_bits),
        "points":       list(points),
        # One card per uncovered point is the honest default: the point IS the
        # thing the deck is missing.
        "target_cards": max(1, len(points)),
    }


def _parse_terms(text: str) -> list[str]:
    """A typed 'a, b; c' into search terms, deduped, order preserved.

    Semicolons count as separators too — they are what you reach for when a
    term itself contains a comma, and treating them as literal text would put a
    stray ';' into a deck search."""
    out: list[str] = []
    for raw in str(text or "").replace(";", ",").split(","):
        term = " ".join(raw.split())
        if term and term.lower() not in {t.lower() for t in out}:
            out.append(term)
    return out


def _card_back(card: dict) -> str:
    """The back of a generated card.

    Every producer (generation, split, one-by-one, regenerate) writes "extra",
    and commit_cards reads "extra". "back" only turns up on hand-edited dicts,
    so it is a fallback, never the primary."""
    return str((card or {}).get("extra") or (card or {}).get("back") or "")


def _short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class BulkSearchDialog(QDialog):
    """Read a batch of gaps → search the deck for all of them → commit per gap."""

    # Roles on the tree items, so the commit walk can tell the three row kinds
    # apart without reparsing their labels.
    ROLE_KIND = Qt.ItemDataRole.UserRole          # "gap" | "point" | "note"
    ROLE_KEY = Qt.ItemDataRole.UserRole + 1       # kg_id | point index | nid
    ROLE_CARD = Qt.ItemDataRole.UserRole + 2      # the written card dict itself
    ROLE_CONCEPT = Qt.ItemDataRole.UserRole + 3   # a point row's concept text
    ROLE_KG = Qt.ItemDataRole.UserRole + 4        # owning gap, carried by leaves

    def __init__(self, kgs: list[dict], parent=None, *, unified: bool = False):
        super().__init__(parent or mw)
        # UNIFIED MODE is the same dialog with the hand-off automated: instead of
        # stopping at "these gaps aren't covered, send them to Create yourself",
        # committing rolls straight on into Mass AI Create carrying the points
        # your deck turned out not to have. One pass, one decision per gap.
        self._unified = bool(unified)
        self.setWindowTitle("Work through knowledge gaps" if unified
                            else "Bulk search — knowledge gaps")
        self.setMinimumSize(940, 660)
        self.resize(1180, 760)
        # NOT WA_DeleteOnClose: closing while a search runs HIDES this window and
        # leaves the run going, and a deleted dialog would take the completion
        # callback down with it. It's deleted explicitly once it really closes.

        self._kgs = [k for k in kgs if isinstance(k, dict)]
        self._plans: list = []          # triage.KGPlan, row-aligned with the table
        self._run_plans: list = []      # the snapshot the current results came from
        self._coverage = None
        self._matches: list = []
        self._cancel_ev = None
        self._busy = False
        # Set when the user closes the window mid-run and chose to let it finish:
        # the dialog is hidden, not destroyed, and re-shows itself with results.
        self._backgrounded = False
        # Set once the window has really closed. A stopped run's callbacks still
        # fire afterwards, and touching a deleted Qt widget from one is a crash.
        self._closed = False
        self._tag_edits: dict[str, QLineEdit] = {}   # kg_id -> its tag box

        # ── the writing half of the one screen ───────────────────────────────
        # Unified mode no longer hands the uncovered points to another window:
        # it generates cards for them into the bottom of this results page, and
        # commits them together with the found cards. The JOBS are ordinary
        # create_jobs records on disk (same as Mass AI Create), so this dialog
        # polls a batch_id rather than owning threads — a crash mid-write loses
        # nothing, and the batch can be reopened from AI Create.
        self._deck = None                    # built on the plan page, unified only
        self._notetype = None
        self._write_cb = None
        self._gen_batch_id = ""
        self._gen_jobs: dict = {}            # kg_id -> the create_jobs record
        self._gen_profile: dict = {}         # the notetype profile cards commit under
        self._gen_active = False             # jobs still running
        self._gen_rendered = False           # the written cards are on screen
        self._applied = False
        self._poll = QTimer(self)
        self._poll.setInterval(750)
        self._poll.timeout.connect(self._on_gen_poll)

        self._build()
        self._fill_plan_table()
        if provider_configured() and not is_manual_provider():
            self._set_status(
                "Nothing has been sent to the AI yet. Edit any gap's notes "
                "first — they're the biggest thing the read goes on — then "
                "press Read gaps with AI.")
        theme_dialog(self)

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        self._title = QLabel()
        self._title.setTextFormat(Qt.TextFormat.RichText)
        self._title.setWordWrap(True)
        root.addWidget(self._title)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_plan_page())     # 0
        self._stack.addWidget(self._build_results_page())  # 1
        root.addWidget(self._stack, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        root.addWidget(self._progress)
        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        btn_row = QHBoxLayout()
        self._back_btn = QPushButton("◀  Back to gaps")
        self._back_btn.setVisible(False)
        self._back_btn.clicked.connect(self._on_back)
        btn_row.addWidget(self._back_btn)
        btn_row.addStretch(1)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.setToolTip(
            "Closing during a search offers to leave it running — this window "
            "hides and comes back on its own when the results are in.")
        self._close_btn.clicked.connect(self.close)
        btn_row.addWidget(self._close_btn)

        self._read_btn = QPushButton("🔎  Read gaps with AI")
        self._read_btn.setMinimumHeight(32)
        self._read_btn.setToolTip(
            "One AI call reads every ticked gap: cleans up the shorthand, judges "
            "how broad it is, splits it into searchable points and proposes how "
            "many cards it should pull.\n\nYou review all of that before anything "
            "touches your deck."
        )
        self._read_btn.clicked.connect(self._on_read)
        btn_row.addWidget(self._read_btn)

        self._search_btn = QPushButton("Search my deck  ▶")
        self._search_btn.setMinimumHeight(32)
        self._search_btn.setEnabled(False)
        self._search_btn.clicked.connect(self._on_search)
        btn_row.addWidget(self._search_btn)

        self._commit_btn = QPushButton("Tag + Unsuspend")
        self._commit_btn.setMinimumHeight(32)
        self._commit_btn.setVisible(False)
        self._commit_btn.clicked.connect(self._on_commit)
        btn_row.addWidget(self._commit_btn)
        root.addLayout(btn_row)

        if not provider_configured():
            self._read_btn.setEnabled(False)
            self._status.setText("Set up an AI provider in Ankisstant Settings first.")
        elif is_manual_provider():
            self._read_btn.setEnabled(False)
            self._status.setText(
                "Bulk search needs a CLI or API provider — the copy/paste "
                "provider would need a separate paste for every step.")

    def _build_plan_page(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Gap", "Your notes", "What the AI read it as", "Scope", "Cards", "Pts"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setWordWrap(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(C_GAP, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_NOTES, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_READ, QHeaderView.ResizeMode.Stretch)
        for c in (C_SCOPE, C_CARDS, C_PTS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(C_GAP, 220)
        self._table.itemChanged.connect(self._on_table_item_changed)
        col.addWidget(self._table, 1)

        self._table_hint = QLabel(
            "Untick any gap to skip it. <b>Your notes are editable here</b> and "
            "are saved back to the gap — they're the main thing the AI reads, so "
            "this is the place to fix a gap it's about to misunderstand. After "
            "reading, edit the reading or the card count on any row too. Rows "
            "marked ⚠ are ones the AI wasn't sure it understood."
        )
        self._table_hint.setTextFormat(Qt.TextFormat.RichText)
        self._table_hint.setStyleSheet("color: gray; font-size: 11px;")
        self._table_hint.setWordWrap(True)
        col.addWidget(self._table_hint)

        # The learning-point gate. Off by default because the plan table is
        # already a review step; on when the batch is worth the extra minute —
        # a rogue point costs a wasted search AND, in unified mode, a wasted
        # card, so it is the one thing worth checking before the run starts.
        gate_row = QHBoxLayout()
        self._lp_gate_cb = QCheckBox("Review the learning points before searching")
        self._lp_gate_cb.setStyleSheet("font-size: 11px;")
        self._lp_gate_cb.setToolTip(
            "After the read, show every point the AI split these gaps into so "
            "you can edit or drop them. Per-run — it isn't remembered."
        )
        gate_row.addWidget(self._lp_gate_cb)
        gate_row.addStretch(1)
        # The same screen, on demand. Without this the points — the only thing
        # that actually steers the search — were unreachable unless you had
        # ticked the box BEFORE the read, which is before you know whether the
        # read needs correcting.
        self._lp_btn = QPushButton("Review points…")
        self._lp_btn.setAutoDefault(False)
        self._lp_btn.setEnabled(False)
        self._lp_btn.setToolTip(
            "Edit the points and their SEARCH TERMS — the terms are what is "
            "actually looked up in your deck.")
        self._lp_btn.clicked.connect(self._open_lp_gate)
        gate_row.addWidget(self._lp_btn)
        col.addLayout(gate_row)
        if self._unified:
            col.addLayout(self._build_write_strip())
        return w

    def _build_write_strip(self):
        """Where the cards written for the uncovered points go.

        On the PLAN page, not down beside the results, because generation starts
        by itself the moment the search lands — by the time you can see the
        written cards it is too late to be asked which deck they belong in."""
        from .mass_create import _deck_names, _notetype_profiles
        cc_cfg = tool_config("card_creator")

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        self._write_cb = QCheckBox("Write cards for whatever the search doesn't find")
        self._write_cb.setChecked(True)
        self._write_cb.setStyleSheet("font-size: 11px;")
        self._write_cb.setToolTip(
            "Generation starts on its own as soon as the search results are up, "
            "so you can read what your deck already had while the rest is being "
            "written. The cards land in the bottom half of the results screen "
            "for review — nothing reaches your collection until you press "
            "Apply.\n\n"
            "Untick to just search: the uncovered gaps are then offered to AI "
            "Create the old way, in its own window.")
        outer.addWidget(self._write_cb)

        strip = QHBoxLayout()
        strip.setContentsMargins(20, 0, 0, 0)
        lbl = QLabel("into deck")
        lbl.setStyleSheet("color: gray; font-size: 11px;")
        strip.addWidget(lbl)
        self._deck = QComboBox()
        self._deck.setEditable(True)
        self._deck.addItems(_deck_names())
        self._deck.setCurrentText(cc_cfg.get("default_deck") or "Default")
        self._deck.setMinimumWidth(200)
        strip.addWidget(self._deck, 1)
        lbl2 = QLabel("as")
        lbl2.setStyleSheet("color: gray; font-size: 11px;")
        strip.addWidget(lbl2)
        self._notetype = QComboBox()
        for prof in _notetype_profiles(cc_cfg):
            self._notetype.addItem(prof["name"])
        want = (cc_cfg.get("selected_notetype")
                or cc_cfg.get("default_notetype") or "").strip()
        ix = self._notetype.findText(want)
        if ix >= 0:
            self._notetype.setCurrentIndex(ix)
        self._notetype.setMinimumWidth(180)
        strip.addWidget(self._notetype, 1)
        strip.addStretch(1)
        outer.addLayout(strip)

        def _sync(on: bool) -> None:
            for wd in (self._deck, self._notetype, lbl, lbl2):
                wd.setEnabled(on)
        self._write_cb.toggled.connect(_sync)
        return outer

    def _build_results_page(self) -> QWidget:
        """ONE tree, holding both halves of the answer.

        The cards your deck already had and the cards written for what it didn't
        sit in the SAME tree, under the same gap — because the question you are
        actually answering is per gap: "is this covered, and if the deck's
        attempt doesn't cover it, is the new card the right replacement?" With
        the written cards in a separate panel you have to hold one list in your
        head while reading the other. Here the found cards for a gap and the
        cards written for that same gap are adjacent rows.

        The written rows appear as soon as generation starts — as a placeholder
        that says what is being written — so the tree never silently reshuffles
        under you when the jobs land."""
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)

        col.addWidget(self._build_found_pane(), 1)

        send_row = QHBoxLayout()
        self._send_create_btn = QPushButton("\u2192  Send gaps to AI Create\u2026")
        self._send_create_btn.setAutoDefault(False)
        self._send_create_btn.setToolTip(
            "Pick which gaps go to AI Create. Each one carries the specific "
            "points your deck had nothing for, so a partly-covered gap asks for "
            "cards on the residue rather than the whole topic again. They land "
            "in AI Create's queue \u2014 nothing is generated yet.")
        self._send_create_btn.clicked.connect(self._on_send_to_create)
        send_row.addWidget(self._send_create_btn)
        send_row.addStretch(1)
        col.addLayout(send_row)
        return w

    def _build_found_pane(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)

        self._found_head = QLabel("What your deck already has for these gaps")
        self._found_head.setStyleSheet("font-weight: 600;")
        self._found_head.setWordWrap(True)
        col.addWidget(self._found_head)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Gap  ·  point  ·  card", "Tag"])
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(False)
        # The card text is the thing being read and judged; the tag box is one
        # short string per GAP row and was taking a quarter of the width for it.
        # Column 0 takes everything left over, and the tag column is fixed narrow
        # (drag it wider if a tag is genuinely long — the box scrolls anyway).
        th = self._tree.header()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        th.setStretchLastSection(False)
        self._tree.setColumnWidth(1, 210)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        col.addWidget(self._tree, 1)

        ctl = QHBoxLayout()
        for label, fn in (("Expand all", lambda: self._tree.expandAll()),
                          ("Collapse all", lambda: self._tree.collapseAll())):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            ctl.addWidget(b)
        ctl.addSpacing(14)
        self._unsuspend_cb = QCheckBox("Unsuspend the ticked cards")
        self._unsuspend_cb.setChecked(True)
        self._unsuspend_cb.setToolTip(
            "The payoff — these cards are sitting suspended until something needs "
            "them, and these gaps are what needs them.")
        ctl.addWidget(self._unsuspend_cb)
        ctl.addSpacing(14)
        # Only meaningful once cards have been written; hidden until then so a
        # plain Bulk search sees exactly the controls it always had.
        self._written_lbl = QLabel("Written cards:")
        self._written_lbl.setStyleSheet("font-size: 11px;")
        ctl.addWidget(self._written_lbl)
        self._gen_approve_all = QPushButton("Approve all")
        self._gen_approve_all.setAutoDefault(False)
        self._gen_approve_all.clicked.connect(lambda: self._written_bulk(True))
        ctl.addWidget(self._gen_approve_all)
        self._gen_reject_all = QPushButton("Reject all")
        self._gen_reject_all.setAutoDefault(False)
        self._gen_reject_all.clicked.connect(lambda: self._written_bulk(False))
        ctl.addWidget(self._gen_reject_all)
        # NB: not `for w in ...` — `w` is this pane's QWidget, and rebinding it
        # drops Python's only reference to an unparented widget, so Qt deletes it
        # (taking `col` with it) mid-build.
        for _wgt in (self._written_lbl, self._gen_approve_all,
                     self._gen_reject_all):
            _wgt.setVisible(False)
        ctl.addStretch(1)
        self._sel_lbl = QLabel("")
        self._sel_lbl.setStyleSheet("color: gray; font-size: 11px;")
        ctl.addWidget(self._sel_lbl)
        col.addLayout(ctl)

        self._gen_note = QLabel("")
        self._gen_note.setStyleSheet("color: gray; font-size: 11px;")
        self._gen_note.setWordWrap(True)
        self._gen_note.setVisible(False)
        col.addWidget(self._gen_note)

        self._gap_hint = QLabel("")
        self._gap_hint.setStyleSheet("color: gray; font-size: 11px;")
        self._gap_hint.setWordWrap(True)
        col.addWidget(self._gap_hint)
        return w

    def _written_bulk(self, approve: bool) -> None:
        """Tick or untick every written card at once."""
        state = Qt.CheckState.Checked if approve else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        for item, _card in self._written_items():
            item.setCheckState(0, state)
        self._tree.blockSignals(False)
        self._update_selection_count()

    # ── stage 1: the plan table ──────────────────────────────────────────────

    def _fill_plan_table(self) -> None:
        """Before the AI read, the table is just the gaps with their tick boxes;
        the scope/cards/points columns fill in once they've been read."""
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._kgs))
        for row, kg in enumerate(self._kgs):
            title = (kg.get("title") or "(untitled)").strip() or "(untitled)"
            cell = QTableWidgetItem(title)
            cell.setFlags((cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                          & ~Qt.ItemFlag.ItemIsEditable)
            cell.setCheckState(Qt.CheckState.Checked)
            notes = kg_store.field(kg, "notes")
            cell.setToolTip(f"{title}\n\n{notes[:400]}" if notes else title)
            self._table.setItem(row, C_GAP, cell)
            self._table.setItem(row, C_NOTES, self._notes_cell(kg))
            hint = QTableWidgetItem("— not read yet —")
            hint.setFlags(hint.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, C_READ, hint)
            for c in (C_SCOPE, C_CARDS, C_PTS):
                blank = QTableWidgetItem("")
                blank.setFlags(blank.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, c, blank)
        self._table.blockSignals(False)
        self._table.resizeRowsToContents()
        self._refresh_title()

    def _notes_cell(self, kg: dict) -> QTableWidgetItem:
        """This gap's notes, editable in place.

        Notes are the single biggest input to how the AI reads a gap — a
        two-word title plus "the vascular ones keep tripping me up" is a
        completely different search from the title alone. They were readable
        only as a hover tooltip, on the one screen where you are deciding what
        to run, which is exactly where you notice they're wrong."""
        cell = QTableWidgetItem(kg_store.field(kg, "notes") or "")
        cell.setToolTip("Editable. Saved back onto the gap before the AI reads "
                        "it, so what you type here shapes the run.")
        return cell

    def _refresh_title(self) -> None:
        n = len(self._kgs)
        if self._plans:
            active = self._active_plans()
            pts = sum(len(p.points) for p in active)
            budget = kg_triage.total_cards(active)
            self._title.setText(
                f"<b>{len(active)} of {n} gap(s)</b> · {pts} searchable point(s) "
                f"· up to {budget} card(s)")
        else:
            self._title.setText(
                f"<b>{n} knowledge gap(s)</b> — check the notes below, then "
                "<b>Read gaps with AI</b> to split them into searchable points.")

    def _checked_rows(self) -> list[int]:
        out = []
        for row in range(self._table.rowCount()):
            it = self._table.item(row, C_GAP)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(row)
        return out

    def _on_table_item_changed(self, item) -> None:
        if item is None:
            return
        if item.column() == C_NOTES:
            self._table.resizeRowToContents(item.row())
            return
        if item.column() != C_GAP:
            return
        row = item.row()
        if 0 <= row < len(self._plans):
            self._plans[row].included = item.checkState() == Qt.CheckState.Checked
        self._refresh_buttons()
        self._refresh_title()

    def _sync_notes(self) -> int:
        """Write edited notes back onto the gaps. Returns how many changed.

        Runs BEFORE the read and before the search, not at the end: the whole
        point is that the AI sees what you just typed. Writing to the store as
        well as to the in-memory record means the edit survives the run, so you
        fix a badly-worded gap once rather than every time you look at it."""
        changed = 0
        for row, kg in enumerate(self._kgs):
            cell = self._table.item(row, C_NOTES)
            if cell is None:
                continue
            typed = cell.text().strip()
            if typed == (kg_store.field(kg, "notes") or "").strip():
                continue
            fields = dict(kg.get("fields") or {})
            if typed:
                fields["notes"] = typed
            else:
                fields.pop("notes", None)
            kg["fields"] = fields          # the in-memory copy triage reads
            kg.pop("notes", None)          # never let a legacy top-level key win
            try:
                updated = kg_store.update(str(kg.get("id") or ""), fields=fields)
                if updated:
                    self._kgs[row] = updated
            except Exception as e:
                log.warn(f"bulk search: couldn't save notes for "
                         f"{kg.get('id')}: {e}")
            changed += 1
        if changed:
            try:
                from .. import knowledge_gaps
                knowledge_gaps._refresh_open_panel()
            except Exception:
                pass
        return changed

    def _active_plans(self) -> list:
        return [p for p in self._plans if p.ok and p.included]

    def _refresh_buttons(self) -> None:
        can_read = (bool(self._checked_rows()) and not self._busy
                    and provider_configured() and not is_manual_provider())
        self._read_btn.setEnabled(can_read)
        has_points = any(p.points for p in self._active_plans())
        self._search_btn.setEnabled(bool(self._active_plans()) and not self._busy)
        self._lp_btn.setEnabled(has_points and not self._busy)

    # ── stage 1: the read ────────────────────────────────────────────────────

    def _on_read(self) -> None:
        rows = self._checked_rows()
        if not rows:
            tooltip("Tick at least one gap first.")
            return
        # Notes first: they are the biggest input to how each gap is read, and
        # an edit sitting in a cell that hasn't been committed yet would be
        # invisible to the call that's about to run.
        saved = self._sync_notes()
        kgs = [self._kgs[r] for r in rows]
        self._set_busy(True, (f"Saved notes on {saved} gap(s). " if saved else "")
                       + f"Reading {len(kgs)} gap(s) with AI…")
        from ...core.config import tool_model_for
        model = tool_model_for("lecture_extract")

        # A batch of gaps is one big reasoning call — measured at several minutes
        # for 17 gaps. Without a running clock a static label reads as a hang.
        from ..lecture.panel import _Heartbeat

        def _report(msg, _pct=0):
            mw.taskman.run_on_main(lambda m=msg: self._set_status(m))

        def _bg():
            with _Heartbeat(_report, f"Reading {len(kgs)} gap(s) with AI…", 0):
                return kg_triage.triage(kgs, model=model)

        mw.taskman.run_in_background(_bg, self._on_read_done, uses_collection=False)

    def _on_read_done(self, fut) -> None:
        if self._closed:
            return
        self._set_busy(False)
        self._resurface()
        try:
            plans = fut.result()
        except core_api.ClaudeCancelled:
            self._status.setText("Stopped.")
            return
        except Exception as e:
            import traceback
            log.error(f"bulk search: triage failed: {e}\n{traceback.format_exc()}")
            self._status.setText("")
            friendly = getattr(e, "friendly", None) or str(e)
            showWarning(f"Couldn't read those gaps:\n\n{friendly}")
            return

        # Re-key onto the full gap list: only the ticked rows were read, and the
        # unticked ones must stay on the table (unread, unticked) rather than
        # vanishing from a batch the user can still see.
        by_id = {p.kg_id: p for p in plans}
        self._plans = []
        for kg in self._kgs:
            kid = str(kg.get("id") or "")
            p = by_id.get(kid)
            if p is None:
                p = kg_triage.KGPlan(
                    kg_id=kid, title=(kg.get("title") or "").strip(),
                    kg_type=str(kg.get("type") or "kg").lower(),
                    concept="", included=False)
            self._plans.append(p)
        self._render_plan_table()
        if self._lp_gate_cb.isChecked():
            self._open_lp_gate()

    def _open_lp_gate(self) -> None:
        """Show every point the read produced, editable, before anything runs.

        Dropping a point here drops it from the search AND from anything that
        falls through to generation, because both read the same plan. This is
        also the ONLY place the search terms can be changed — the plan table's
        reading does not feed the search."""
        plans = [p for p in self._plans if p.ok and p.included]
        if not plans:
            tooltip("Read the gaps first — there are no points yet.")
            return
        dlg = _PointApprovalDialog(plans, parent=self)
        if not dlg.exec():
            self._set_status("Left the points as they were.")
            return
        kept, termless = dlg.apply()
        dropped = 0
        for p in self._plans:
            if p.ok and p.included and not p.points:
                p.included = False
                dropped += 1
        self._render_plan_table()
        self._refresh_title()
        self._set_status(
            f"{kept} point(s) kept"
            + (f"; {dropped} gap(s) left with none and were unticked" if dropped
               else "")
            + (f"; {termless} point(s) have no search terms and can only be "
               "matched by tag" if termless else "")
            + ".")

    def _render_plan_table(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._plans))
        for row, p in enumerate(self._plans):
            unread = not p.concept and not p.points
            shaky = (p.confidence == "low" or not p.ok) and not unread
            title = ("⚠ " if shaky else "") + (p.title or "(untitled)")
            cell = QTableWidgetItem(title)
            cell.setFlags((cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                          & ~Qt.ItemFlag.ItemIsEditable)
            cell.setCheckState(Qt.CheckState.Checked if p.included and p.ok
                               else Qt.CheckState.Unchecked)
            tip = [p.title or ""]
            if p.note:
                tip.append(p.note)
            if p.points:
                tip.append("Points:\n" + "\n".join(
                    f"  · {pt.concept}   {pt.terms}" for pt in p.points))
            cell.setToolTip("\n\n".join(t for t in tip if t))
            self._table.setItem(row, C_GAP, cell)
            # Rebuilt from what's ON SCREEN, not from the store: the read
            # happens after _sync_notes, so the cell is already authoritative
            # and re-reading the record would undo an edit made mid-run.
            keep = self._table.item(row, C_NOTES)
            self._table.setItem(row, C_NOTES, QTableWidgetItem(
                keep.text() if keep is not None else
                (kg_store.field(self._kgs[row], "notes")
                 if row < len(self._kgs) else "")))

            reading = QTableWidgetItem(
                "— not read —" if unread else p.concept)
            if unread:
                reading.setFlags(reading.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                reading.setToolTip(
                    "How the AI understood this gap.\n\n"
                    "Editing it does NOT change the search. The search runs on "
                    "the learning points below it and, specifically, on their "
                    "SEARCH TERMS — use “Review points…” to change those.\n\n"
                    "What this text does affect: it becomes the topic any cards "
                    "written for this gap are generated from.")
            self._table.setItem(row, C_READ, reading)

            if unread:
                for c in (C_SCOPE, C_CARDS, C_PTS):
                    blank = QTableWidgetItem("")
                    blank.setFlags(blank.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self._table.setItem(row, c, blank)
                self._table.removeCellWidget(row, C_SCOPE)
                self._table.removeCellWidget(row, C_CARDS)
                continue

            scope = QComboBox()
            for key, meta in kg_triage.SCOPES.items():
                scope.addItem(meta["label"], key)
                scope.setItemData(scope.count() - 1, meta["hint"],
                                  Qt.ItemDataRole.ToolTipRole)
            si = scope.findData(p.scope)
            scope.setCurrentIndex(si if si >= 0 else 0)
            scope.currentIndexChanged.connect(lambda _i, r=row: self._on_scope_changed(r))
            self._table.setCellWidget(row, C_SCOPE, scope)

            cards = QSpinBox()
            cards.setRange(kg_triage.MIN_CARDS, kg_triage.MAX_CARDS)
            cards.setValue(kg_triage.clamp_cards(p.cards))
            cards.setToolTip("How many existing cards this gap should pull. "
                             "Changing the scope resets it to that scope's default.")
            cards.valueChanged.connect(lambda _v, r=row: self._on_cards_changed(r))
            self._table.setCellWidget(row, C_CARDS, cards)

            pts = QTableWidgetItem(str(len(p.points)))
            pts.setFlags(pts.flags() & ~Qt.ItemFlag.ItemIsEditable)
            pts.setToolTip(
                "How many searchable points this gap was split into. A broad gap "
                "splits into several — that's what actually finds more cards, "
                "since one point only matches cards about one thing.")
            self._table.setItem(row, C_PTS, pts)
        self._table.blockSignals(False)
        self._table.resizeRowsToContents()

        n_ok = sum(1 for p in self._plans if p.ok)
        n_shaky = sum(1 for p in self._plans if p.ok and p.confidence == "low")
        msg = f"Read {n_ok} gap(s)."
        if n_shaky:
            msg += (f" {n_shaky} marked ⚠ — check the reading on those before "
                    "you run them.")
        self._status.setText(msg)
        self._refresh_buttons()
        self._refresh_title()

    def _on_scope_changed(self, row: int) -> None:
        """Re-scoping resets the card target to that scope's default: the two
        mean the same thing, so a hand-typed 14 left sitting on a row just marked
        'narrow' would be the confusing outcome."""
        if not (0 <= row < len(self._plans)):
            return
        combo = self._table.cellWidget(row, C_SCOPE)
        spin = self._table.cellWidget(row, C_CARDS)
        if combo is None or spin is None:
            return
        scope = combo.currentData() or kg_triage.DEFAULT_SCOPE
        self._plans[row].scope = scope
        spin.setValue(kg_triage.clamp_cards(kg_triage.scope_meta(scope)["cards"]))

    def _on_cards_changed(self, row: int) -> None:
        if 0 <= row < len(self._plans):
            spin = self._table.cellWidget(row, C_CARDS)
            if spin is not None:
                self._plans[row].cards = spin.value()
        self._refresh_title()

    def _sync_plans(self) -> None:
        """Fold the table's edits back in, so what's on screen is what runs."""
        for row, p in enumerate(self._plans):
            it0 = self._table.item(row, C_GAP)
            if it0 is not None:
                p.included = it0.checkState() == Qt.CheckState.Checked
            it1 = self._table.item(row, C_READ)
            if it1 is not None and p.ok:
                txt = it1.text().strip()
                if txt and txt != "— not read —":
                    p.concept = txt
            combo = self._table.cellWidget(row, C_SCOPE)
            if combo is not None:
                p.scope = combo.currentData() or p.scope
            spin = self._table.cellWidget(row, C_CARDS)
            if spin is not None:
                p.cards = spin.value()

    # ── stage 2: the search ──────────────────────────────────────────────────

    def _on_search(self) -> None:
        if not anki_utils.require_col() or self._busy:
            return
        self._sync_notes()
        self._sync_plans()
        plans = self._active_plans()
        if not plans:
            tooltip("No gaps ticked — nothing to search.")
            return
        points = kg_triage.points_of(plans)
        if not points:
            showWarning("None of those gaps produced anything searchable.")
            return
        self._run_plans = list(plans)

        import threading
        from ..lecture.panel import run_pipeline

        cfg = dict(tool_config("lecture"))
        cfg["lo_text"] = ""                       # never the LO branch: points win
        cfg["target_cards"] = kg_triage.total_cards(plans)
        # Depth no longer decides the per-point cap here (cap_for does), but it
        # still sizes the candidate POOL the judge reads. Use the deepest preset
        # so a broad gap has enough candidates to draw a real cluster from.
        cfg["depth"] = "thorough"
        cap_for = kg_triage.cap_for(plans)
        # ...but a narrow gap's point still gets a SMALL pool: the judge reads
        # every candidate, and that read is the slow leg of the whole run.
        cand_cap_for = kg_triage.cand_cap_for(plans)

        self._cancel_ev = threading.Event()
        cancel_ev = self._cancel_ev
        self._set_busy(True, "Starting…", progress=True, stoppable=True)

        def _report(msg, pct):
            def _ui():
                if self._set_status(msg):
                    self._progress.setValue(int(pct))
            mw.taskman.run_on_main(_ui)

        def _bg():
            core_api.set_cancel_event(cancel_ev)
            try:
                return run_pipeline(None, cfg, report=_report, cancel_ev=cancel_ev,
                                    points=points, cap_for=cap_for,
                                    cand_cap_for=cand_cap_for)
            finally:
                core_api.set_cancel_event(None)

        # uses_collection=False is not optional: the default executor is the one
        # Anki's periodic backup queues behind, and the app would look hung for
        # the whole run.
        mw.taskman.run_in_background(_bg, self._on_search_done, uses_collection=False)

    def _on_stop(self) -> None:
        # One Stop button, two things it can be stopping: the deck search, or
        # the writing that follows it. Only one of them ever runs at a time.
        if self._gen_active:
            from .. import create_jobs
            n = create_jobs.cancel_batch(self._gen_batch_id)
            self._stop_btn.setEnabled(False)
            self._gen_note.setText(
                f"Stopping {n} job(s) — anything already written is kept.")
            return
        if self._cancel_ev is not None:
            self._cancel_ev.set()
        self._stop_btn.setEnabled(False)
        self._status.setText("Stopping after the current step…")

    def _on_search_done(self, fut) -> None:
        if self._closed:
            return
        from ..lecture.panel import Cancelled
        self._set_busy(False)
        self._resurface()
        try:
            _lecture, matches, coverage = fut.result()
        except (Cancelled, core_api.ClaudeCancelled):
            self._status.setText("Stopped.")
            return
        except Exception as e:
            import traceback
            log.error(f"bulk search: run failed: {e}\n{traceback.format_exc()}")
            self._status.setText("")
            showWarning(f"Bulk search failed:\n\n{e}")
            return
        self._matches, self._coverage = matches, coverage
        self._render_results()

    # ── stage 3: results, grouped by gap ─────────────────────────────────────

    def _render_results(self) -> None:
        from ..lecture import matcher

        by_kg: dict[str, list] = {}
        for m in list(self._coverage.covered) + list(self._coverage.gaps):
            by_kg.setdefault(getattr(m.point, "kg_id", ""), []).append(m)

        self._tree.blockSignals(True)
        self._tree.clear()
        self._tag_edits = {}
        total_notes = 0

        for plan in self._run_plans:
            group = by_kg.get(plan.kg_id) or []
            if not group:
                continue
            covered_pts = sum(1 for m in group if m.final)
            gap_pts = [m for m in group if not m.final]

            top = QTreeWidgetItem(self._tree)
            top.setData(0, self.ROLE_KIND, "gap")
            top.setData(0, self.ROLE_KEY, plan.kg_id)
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsAutoTristate
                         | Qt.ItemFlag.ItemIsUserCheckable)
            # An auto-tristate parent needs a starting state or Qt draws no
            # checkbox at all; it takes over from the children once they exist.
            top.setCheckState(0, Qt.CheckState.Unchecked)
            n_cards = sum(len(m.final) for m in group)
            total_notes += n_cards
            # What the SEARCH found. What actually commits is driven by the
            # ticks and reported live in the summary line below the tree.
            state = ("GAP — nothing found" if covered_pts == 0 else
                     f"{covered_pts}/{len(group)} points found"
                     + ("" if covered_pts == len(group) else " — partial"))
            top.setText(0, f"{plan.title}      {state}  ·  {n_cards} card(s)")
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            notes = ""
            try:
                notes = kg_store.field(kg_store.get(plan.kg_id) or {}, "notes")
            except Exception:
                notes = ""
            top.setToolTip(0, "\n\n".join(x for x in (
                plan.concept,
                (f"Your notes:\n{notes}" if notes else ""),
                f"Scope: {plan.scope} · target {plan.cards} card(s)") if x))

            # Each gap carries its OWN tag — unrelated gaps must not share one.
            tag_edit = QLineEdit()
            tag_edit.setPlaceholderText("no tag — this gap's cards won't be tagged")
            tag_edit.setText(self._suggest_tag(plan, group))
            tag_edit.setToolTip(
                "Applied to every ticked card under this gap. Prefilled from the "
                "gap's own system/subsystem/topic, in the same scheme AI Browse "
                "and AI Create use. Edit or clear it freely.")
            attach_tag_completer(tag_edit, multi=False)
            self._tree.setItemWidget(top, 1, tag_edit)
            self._tag_edits[plan.kg_id] = tag_edit

            for pi, m in enumerate(group):
                node = QTreeWidgetItem(top)
                node.setData(0, self.ROLE_KIND, "point")
                node.setData(0, self.ROLE_KEY, pi)
                # The join key for written cards: the model attributes a card to
                # a point NUMBER, that number indexes the job's mass_points, and
                # mass_points are these concepts verbatim. Matching on the text
                # keeps it exact rather than relying on two lists staying aligned.
                node.setData(0, self.ROLE_CONCEPT, m.point.concept)
                node.setData(0, self.ROLE_KG, plan.kg_id)
                node.setFlags(node.flags() | Qt.ItemFlag.ItemIsAutoTristate
                              | Qt.ItemFlag.ItemIsUserCheckable)
                node.setCheckState(0, Qt.CheckState.Unchecked)

                chosen = list(m.final)
                extra = [n for n in m.candidates if n not in set(chosen)]
                # A gap point has no chosen cards; offer its own-topic cards (the
                # tag leg proved they're on topic) rather than showing it empty.
                if not chosen and m.topic_present:
                    extra = list(m.topic_cards) + [
                        n for n in extra if n not in set(m.topic_cards)]
                extra = extra[:EXTRA_CANDIDATES_PER_POINT]

                # Three different reasons a point can show nothing, and calling
                # them all "gap" would be a lie: the deck genuinely lacks it, the
                # deck has the TOPIC but no card testing this claim, or the deck
                # has cards and the budget simply went elsewhere. Only the first
                # two are gaps; the third is a card he can just tick.
                label = _short(m.point.concept, 96)
                if chosen:
                    pass
                elif not m.is_gap:
                    label += "   — cards found, but this gap's card budget went elsewhere"
                elif m.topic_present:
                    label = ("⚠  " + label
                             + "   — no card tests this; your own topic cards below")
                else:
                    label = "⚠  " + label + "   — your deck has nothing for this"
                node.setText(0, label)
                node.setToolTip(0, f"{m.point.concept}\n\nterms: {m.point.terms}")

                for nid in chosen + extra:
                    leaf = QTreeWidgetItem(node)
                    leaf.setData(0, self.ROLE_KIND, "note")
                    leaf.setData(0, self.ROLE_KEY, int(nid))
                    leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    leaf.setCheckState(0, Qt.CheckState.Checked if nid in chosen
                                       else Qt.CheckState.Unchecked)
                    susp, tot = m.susp.get(nid, (0, 0))
                    # Words, not colour: a suspended card is the one there's
                    # something to DO about, and that has to read as text.
                    mark = ("⏸ suspended  " if susp else "in rotation  ") \
                        if tot else ""
                    # Show as much of the card as the row can hold, and keep the
                    # rest in the tooltip: deciding whether a card belongs to a
                    # gap is unreadable off a 150-character stub.
                    full = matcher.note_text(nid, 700)
                    leaf.setText(0, f"{mark}{_short(full, 260)}")
                    leaf.setToolTip(0, f"{full}\n\nDouble-click to open this "
                                       "note in the Anki browser.")
                if not chosen and not extra:
                    empty = QTreeWidgetItem(node)
                    empty.setData(0, self.ROLE_KIND, "info")
                    empty.setText(0, "nothing in your deck matched this point's "
                                     "search terms")
                    empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

                node.setExpanded(bool(chosen))
            top.setExpanded(True)

        self._tree.blockSignals(False)

        full, partial, empty = self._outcome_split()
        self._gap_hint.setText(
            f"{len(full)} gap(s) fully covered · {len(partial)} partly · "
            f"{len(empty)} not covered at all.  Only fully-covered gaps are "
            "marked done — the rest stay open, and those are the ones worth "
            "making cards for."
        )
        self._stack.setCurrentIndex(1)
        self._back_btn.setVisible(True)
        self._read_btn.setVisible(False)
        self._search_btn.setVisible(False)
        self._commit_btn.setVisible(True)
        self._title.setText(
            f"<b>{len(self._run_plans)} gap(s) searched</b> · {total_notes} card(s) found")
        self._update_selection_count()
        # Start writing straight away, on this same screen. Deferred one tick so
        # the found cards paint first: they are readable for the whole time the
        # rest is being generated, which is the point of not making this a page.
        if (self._unified and self._write_cb is not None
                and self._write_cb.isChecked() and not self._gen_batch_id):
            QTimer.singleShot(0, self._start_generation)

    def _suggest_tag(self, plan, group) -> str:
        """This gap's hierarchical tag. Built from the point levels the triage
        pass assigned — preferring a point that actually found cards, since a
        gap point's levels are the least corroborated thing in the group."""
        ordered = [m for m in group if m.final] + [m for m in group if not m.final]
        for m in ordered:
            try:
                tag = autotag.tag_from_levels(m.point.tag_levels,
                                              kg_type_key=plan.kg_type,
                                              group=plan.group)
            except Exception as e:
                log.warn(f"bulk search: tag_from_levels failed: {e}")
                tag = ""
            if tag:
                return tag
        # No usable levels — fall back to the gap's own curated tag, if any.
        return (plan.tags[0] if plan.tags else "")

    # ── results interaction ──────────────────────────────────────────────────

    def _on_tree_item_changed(self, item, column) -> None:
        if column != 0:
            return
        if self._gen_rendered:
            self._refresh_gen_head()      # calls _update_selection_count itself
            return
        self._update_selection_count()

    def _on_tree_double_clicked(self, item, column) -> None:
        if item is None:
            return
        if item.data(0, self.ROLE_KIND) == "newcard":
            self._edit_written(item)
            return
        if item.data(0, self.ROLE_KIND) != "note":
            return
        nid = item.data(0, self.ROLE_KEY)
        try:
            from aqt import dialogs
            browser = dialogs.open("Browser", mw)
            if hasattr(browser, "search_for_terms"):
                browser.search_for_terms(f"nid:{nid}")
            else:
                browser.form.searchEdit.lineEdit().setText(f"nid:{nid}")
                browser.onSearchActivated()
        except Exception as e:
            log.warn(f"bulk search: couldn't open browser for nid:{nid}: {e}")

    def _edit_written(self, leaf) -> None:
        """Edit a written card in place, before it is added.

        The full Create review row (regenerate / one-by-one / split) doesn\u2019t
        fit in a tree leaf, so this is the front and back plus the actions that
        matter most here \u2014 the judgement being made is "is this the right
        replacement for what my deck lacked", which is answered by reading and
        fixing the text, not by re-rolling it repeatedly."""
        card = leaf.data(0, self.ROLE_CARD)
        if not isinstance(card, dict):
            return
        dlg = _WrittenCardDialog(card, self._gen_profile, parent=self)
        res = dlg.exec()
        if res == _WrittenCardDialog.SPLIT:
            # Splitting produces more rows than a single-card dialog can show,
            # so it happens against the tree — keeping the edits made so far.
            leaf.setData(0, self.ROLE_CARD, dlg.values())
            self._card_action(leaf, "split")
            return
        if not res:
            return
        card = dlg.values()
        leaf.setData(0, self.ROLE_CARD, card)
        self._label_written(leaf, card, None, [], 0)
        self._refresh_gen_head()

    def _on_tree_context_menu(self, pos) -> None:
        """The per-card actions, on the card.

        AI Create lays Split / One-by-one / Rewrite out as a button strip beside
        every row; a tree row has no strip, so the same actions hang off the
        right-click and off the edit dialog. Same functions underneath \u2014
        cc.split_card and cc.one_by_one_rewrite \u2014 so a card reshaped here is
        reshaped exactly as it would be there."""
        item = self._tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, self.ROLE_KIND)
        menu = QMenu(self._tree)

        if kind == "newcard":
            ticked = item.checkState(0) == Qt.CheckState.Checked
            menu.addAction("Edit this card\u2026", lambda: self._edit_written(item))
            menu.addAction("\u21ba  Rewrite with AI\u2026",
                           lambda: self._card_action(item, "rewrite"))
            menu.addAction("\u2702\ufe0f  Split into atomic cards",
                           lambda: self._card_action(item, "split"))
            menu.addAction("1\ufe0f\u20e3  One-by-one",
                           lambda: self._card_action(item, "obo"))
            menu.addSeparator()
            menu.addAction("Don\u2019t add this card" if ticked else "Add this card",
                           lambda: item.setCheckState(
                               0, Qt.CheckState.Unchecked if ticked
                               else Qt.CheckState.Checked))
            menu.exec(self._tree.mapToGlobal(pos))
            return

        # Retry: a failed gap is a row in the tree, so the action that fixes it
        # belongs on that row — or on any row belonging to the same gap, since
        # the failure may be showing on the point rather than the folder.
        kg_id = (item.data(0, self.ROLE_KG)
                 or (item.data(0, self.ROLE_KEY) if kind in ("gap", "written")
                     else None))
        job = self._gen_jobs.get(str(kg_id or "")) or {}
        if job.get("status") not in ("error", "interrupted"):
            return
        menu.addAction("Retry writing this gap",
                       lambda: self._on_gen_retry(job.get("id") or ""))
        menu.exec(self._tree.mapToGlobal(pos))

    def _card_action(self, leaf, what: str) -> None:
        """Rewrite / split / one-by-one on a single written card.

        Synchronous with a wait cursor, like the same actions in AI Create: it
        is one short call about one card, and the alternative is a row that
        silently changes some seconds after you stopped looking at it."""
        from .. import card_creator as cc
        from ...core.config import tool_model_for
        card = leaf.data(0, self.ROLE_CARD)
        if not isinstance(card, dict):
            return
        hint = ""
        if what == "rewrite":
            from aqt.qt import QInputDialog
            hint, ok = QInputDialog.getText(self, "Rewrite this card",
                                            "What was wrong with it? (optional)")
            if not ok:
                return
            hint = hint.strip()
        profile = self._gen_profile or {}
        self._set_status({"rewrite": "Rewriting that card\u2026",
                          "split": "Splitting that card\u2026",
                          "obo": "Rewriting that card one-by-one\u2026"}[what])
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            if what == "rewrite":
                out = cc._regenerate_one(card, hint, profile, "",
                                         tool_model_for("card_creation"))
            elif what == "obo":
                out = cc.one_by_one_rewrite(card, profile)
            else:
                out = cc.split_card(card, profile)
        except Exception as e:
            log.error(f"work through: {what} failed: {e}")
            self._set_status(f"That didn\u2019t work: {e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not out:
            self._set_status("The AI didn\u2019t return a replacement \u2014 "
                             "the card is unchanged.")
            return

        self._tree.blockSignals(True)
        try:
            if what == "split" and isinstance(out, list):
                parent = leaf.parent()
                kg_id = str(leaf.data(0, self.ROLE_KG) or "")
                row = parent.indexOfChild(leaf)
                parent.removeChild(leaf)
                for i, c in enumerate(out):
                    new_leaf = QTreeWidgetItem()
                    new_leaf.setData(0, self.ROLE_KIND, "newcard")
                    new_leaf.setData(0, self.ROLE_KEY, i)
                    new_leaf.setData(0, self.ROLE_CARD, dict(c))
                    new_leaf.setData(0, self.ROLE_KG, kg_id)
                    new_leaf.setFlags(new_leaf.flags()
                                      | Qt.ItemFlag.ItemIsUserCheckable)
                    new_leaf.setCheckState(0, Qt.CheckState.Checked)
                    parent.insertChild(row + i, new_leaf)
                    self._label_written(new_leaf, dict(c), None, [], 0)
                parent.setExpanded(True)
                self._set_status(f"Split into {len(out)} card(s) \u2014 still "
                                 "nothing added until you press Apply.")
            else:
                new = out[0] if isinstance(out, list) else out
                card.update({"front": str(new.get("front") or ""),
                             "extra": str(new.get("extra")
                                          or new.get("back") or "")})
                leaf.setData(0, self.ROLE_CARD, card)
                self._label_written(leaf, card, None, [], 0)
                self._set_status("Rewritten \u2014 still nothing added until you "
                                 "press Apply.")
        finally:
            self._tree.blockSignals(False)
        self._refresh_gen_head()

    def _ticked(self) -> dict[str, dict]:
        """{kg_id: {"nids": [...], "covered_points": n, "total_points": n}} from
        what's actually ticked right now — the user's decision, not the
        allocator's, is what commits and what closes a gap."""
        out: dict[str, dict] = {}
        for gi in range(self._tree.topLevelItemCount()):
            g = self._tree.topLevelItem(gi)
            kg_id = g.data(0, self.ROLE_KEY)
            rec = out.setdefault(kg_id, {"nids": [], "covered_points": 0,
                                         "total_points": 0})
            for pi in range(g.childCount()):
                p = g.child(pi)
                if p.data(0, self.ROLE_KIND) != "point":
                    continue
                rec["total_points"] += 1
                hit = False
                for ni in range(p.childCount()):
                    n = p.child(ni)
                    if n.data(0, self.ROLE_KIND) != "note":
                        continue
                    if n.checkState(0) == Qt.CheckState.Checked:
                        hit = True
                        nid = int(n.data(0, self.ROLE_KEY))
                        if nid not in rec["nids"]:
                            rec["nids"].append(nid)
                if hit:
                    rec["covered_points"] += 1
        return out

    def _outcome_split(self) -> tuple[list[str], list[str], list[str]]:
        """(fully covered, partly covered, nothing) gap ids, by what's ticked."""
        full, partial, empty = [], [], []
        for kg_id, rec in self._ticked().items():
            if rec["covered_points"] == 0:
                empty.append(kg_id)
            elif rec["covered_points"] >= rec["total_points"]:
                full.append(kg_id)
            else:
                partial.append(kg_id)
        return full, partial, empty

    def _update_selection_count(self) -> None:
        ticked = self._ticked()
        n = len({nid for rec in ticked.values() for nid in rec["nids"]})
        self._sel_lbl.setText(f"{n} card(s) ticked")
        full, partial, empty = self._outcome_split()
        self._gap_hint.setText(
            f"{len(full)} gap(s) fully covered · {len(partial)} partly · "
            f"{len(empty)} not covered at all.  Only fully-covered gaps are "
            "marked done — the rest stay open, and those are the ones worth "
            "making cards for."
        )
        uncovered = len(partial) + len(empty)
        if self._unified:
            self._send_create_btn.setVisible(False)
            if self._gen_batch_id:
                # ONE screen, ONE button: the found cards and the written cards
                # go in together, under one checkpoint.
                approved = sum(len(c) for _k, c in self._approved_written())
                if self._gen_active:
                    self._commit_btn.setEnabled(False)
                    self._commit_btn.setText("Writing the missing cards…")
                else:
                    self._commit_btn.setEnabled(n > 0 or approved > 0)
                    self._commit_btn.setText(
                        f"Apply everything — {n} found + {approved} written  ▶"
                        if approved else f"Apply — {n} found card(s)")
                return
            # Writing switched off (or unavailable): this is the old two-step —
            # commit what the deck had, then offer the rest to AI Create. It
            # stays enabled with zero ticked, because "my deck covers none of
            # this" is a valid answer and the run should continue, not dead-end.
            self._commit_btn.setEnabled(True)
            self._commit_btn.setText(
                (f"Commit {n} found, then write {uncovered} gap(s)  ▶"
                 if n else f"Write the missing cards for {uncovered} gap(s)  ▶")
                if uncovered else f"Commit {n} found card(s)")
            return
        self._commit_btn.setEnabled(n > 0)
        self._send_create_btn.setText(
            f"→  Send gaps to AI Create…  ({uncovered} uncovered)"
            if uncovered else "→  Send gaps to AI Create…")
        self._send_create_btn.setEnabled(self._tree.topLevelItemCount() > 0)

    # ── commit ───────────────────────────────────────────────────────────────

    def _on_commit(self) -> None:
        """Tag and unsuspend what the deck already had.

        On the one screen this is only half the job — _on_apply_everything runs
        it and the written cards inside a single checkpoint. This path is the
        plain search commit, and the fallback for a unified run with the writing
        switched off."""
        if self._gen_batch_id:
            self._on_apply_everything()
            return
        if not anki_utils.require_col() or not self._coverage:
            return
        ticked = self._ticked()
        all_nids = sorted({nid for rec in ticked.values() for nid in rec["nids"]})
        if not all_nids:
            # In unified mode "nothing ticked" is a real outcome, not a mistake:
            # the deck covered none of it, and the whole batch goes to Create.
            if self._unified:
                self._continue_to_create()
                return
            tooltip("Nothing ticked.")
            return
        full, partial, empty = self._outcome_split()

        untagged = [kg_id for kg_id, rec in ticked.items()
                    if rec["nids"] and not self._tag_text(kg_id)]
        do_unsuspend = self._unsuspend_cb.isChecked()

        lines = [f"Tag {len(all_nids)} note(s), each under its own gap's tag"
                 + (", and unsuspend them?" if do_unsuspend else "?")]
        lines.append(f"\n{len(full)} gap(s) fully covered — these are marked done.")
        if partial:
            lines.append(f"{len(partial)} partly covered — kept OPEN, because some "
                         "of what you wrote them down for still isn't in your deck.")
        if empty:
            lines.append(f"{len(empty)} found nothing and stay open.")
        if untagged:
            lines.append(f"\n{len(untagged)} gap(s) have an empty tag box — their "
                         "cards will be unsuspended but not tagged.")
        lines.append("\nCard content is never modified.")
        if not askUser("\n".join(lines)):
            return

        # One checkpoint around the lot, so one undo reverses the whole batch.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Ankisstant: bulk gap search")

        tagged, unsuspended = self._apply_found(ticked, all_nids, do_unsuspend)
        closed = self._close_gaps(full)

        self._commit_btn.setEnabled(False)
        msg = (f"Done — {len(all_nids)} note(s) committed"
               + (f" ({tagged} tagged)" if tagged != len(all_nids) else "")
               + (f", {unsuspended} card(s) unsuspended" if do_unsuspend else "")
               + f", {closed} gap(s) closed.")
        left = len(partial) + len(empty)
        if left and not self._unified:
            msg += (f" {left} gap(s) stay open — send them to AI Create to make "
                    "the cards your deck is missing.")
        self._status.setText(msg)
        tooltip(msg, period=5000)
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass
        if self._unified:
            # Deferred a tick so the commit's tooltip and the deck reset land
            # before a second dialog opens over the top of them.
            QTimer.singleShot(0, self._continue_to_create)

    def _apply_found(self, ticked: dict, all_nids: list[int],
                     do_unsuspend: bool) -> tuple[int, int]:
        """Tag (and optionally unsuspend) the ticked cards. Returns
        (notes tagged, cards unsuspended).

        No checkpoint and no confirm of its own: both callers own those, and on
        the one screen this runs inside the SAME checkpoint as the newly written
        cards so one undo takes back the whole pass."""
        cids: list[int] = []
        for nid in all_nids:
            try:
                note = mw.col.get_note(nid)
            except Exception:
                continue
            cids.extend(note.card_ids() if hasattr(note, "card_ids")
                        else [c.id for c in note.cards()])

        plans_by_id = {p.kg_id: p for p in self._run_plans}
        tagged = 0
        for kg_id, rec in ticked.items():
            if not rec["nids"]:
                continue
            tags: list[str] = []
            t = self._tag_text(kg_id)
            if t:
                tags.append(t)
            plan = plans_by_id.get(kg_id)
            if plan:
                tags.extend(x for x in plan.tags if x and x not in tags)
            for tag in tags:
                anki_utils.tag_notes(rec["nids"], tag)
            if tags:
                tagged += len(rec["nids"])

        cfg = tool_config("browse")
        audit = (cfg.get("audit_tag") or "").strip()
        if audit:
            anki_utils.tag_notes(all_nids, audit)
        mtag = month_tag()
        if mtag:
            anki_utils.tag_notes(all_nids, mtag)

        unsuspended = 0
        if do_unsuspend and cids:
            before = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
            anki_utils.unsuspend_cards(cids)
            after = sum(1 for cid in cids if mw.col.get_card(cid).queue == -1)
            unsuspended = before - after
        return tagged, unsuspended

    # ── unified: the writing half, on the same screen ────────────────────────

    def _create_payload(self) -> list[dict]:
        """The gaps your deck didn't cover, as KG records carrying ONLY their
        uncovered points.

        This is the same shape _on_send_to_create writes onto the queue, minus
        the queue: Mass AI Create's _fill_plan_table calls
        triage.plan_from_queued_gap on every record it's given, so a record with
        `points` on it arrives already planned and is never re-read. The points
        are the ones your own deck was just measured against — re-reading them
        would cost a call to throw that measurement away."""
        missing_by_kg = self._uncovered_points()
        plans_by_id = {p.kg_id: p for p in self._run_plans}
        out: list[dict] = []
        for kg_id, missing in missing_by_kg.items():
            if not missing:
                continue
            plan = plans_by_id.get(kg_id)
            kg = kg_store.get(kg_id)
            if plan is None or not kg:
                continue
            out.append(create_record_for(
                kg, plan.concept,
                [{"concept": m.point.concept, "terms": list(m.point.terms),
                  "system": m.point.system, "subsystem": m.point.subsystem,
                  "topic": m.point.topic}
                 for m in missing]))
        return out

    # ── writing the missing cards, into the bottom of this same screen ───────

    def _start_generation(self) -> None:
        """Write cards for the points the search found nothing for.

        Fires on its own. "Your deck doesn't have this" is the entire finding of
        the search, and making you press a second button in a second window to
        act on it was the step this screen exists to delete. It is still only a
        PROPOSAL: the cards land in the results tree, under the gap they were
        written for, and nothing reaches the collection until Apply."""
        if self._closed or self._gen_batch_id:
            return
        if not provider_configured() or is_manual_provider():
            self._show_written_ui()
            self._gen_note.setText(
                "Writing the missing cards needs a CLI or API provider — set "
                "one up in Ankisstant Settings.")
            return
        payload = self._create_payload()
        if not payload:
            self._show_written_ui()
            self._gen_note.setText(
                "Every point was covered by a card you already have.")
            return
        notetype_name = (self._notetype.currentText().strip()
                         if self._notetype is not None else "")
        if not notetype_name:
            self._show_written_ui()
            self._gen_note.setText(
                "No notetype is set, so there was nothing to write the missing "
                "cards as. Settings \u2192 AI Create adds notetype profiles.")
            return
        deck_name = ((self._deck.currentText().strip()
                      if self._deck is not None else "") or "Default")

        # Main thread, BEFORE the jobs go off-thread: a cold vocab cache makes
        # every job invent its own tag branch.
        try:
            autotag.refresh_vocab_cache()
        except Exception as e:
            log.warn(f"work through: vocab cache refresh failed: {e}")

        from ...core.config import tool_model_for
        from .. import create_jobs
        from . import gen_prompt

        cc_cfg = tool_config("card_creator")
        base_tags = [t.strip() for t in (cc_cfg.get("default_tags") or [])
                     if str(t).strip()]
        audit = (cc_cfg.get("audit_tag") or "").strip()
        if audit and audit not in base_tags:
            base_tags.append(audit)

        plans, gaps_by_id, tags_by_kg = [], {}, {}
        for rec in payload:
            plan = kg_triage.plan_from_queued_gap(rec)
            if plan is None:
                continue
            plans.append(plan)
            gaps_by_id[plan.kg_id] = rec
            # The tag you filed the FOUND cards under is the tag the WRITTEN
            # ones get. They answer the same gap; having them land in two
            # different branches is precisely what one screen exists to stop.
            tags_by_kg[plan.kg_id] = self._tag_text(plan.kg_id)
        if not plans:
            self._show_written_ui()
            self._gen_note.setText(
                "None of the uncovered points could be turned into a "
                "generation plan \u2014 see the log.")
            return

        records = gen_prompt.build_batch(
            plans=plans, gaps_by_id=gaps_by_id, deck_name=deck_name,
            notetype_name=notetype_name, base_tags=base_tags,
            tags_by_kg=tags_by_kg, model=tool_model_for("card_creation"),
            origin="work-through")
        if not records:
            self._show_written_ui()
            self._gen_note.setText(
                "Couldn't build a generation request for any of those gaps \u2014 "
                "see the log.")
            return

        self._gen_batch_id = records[0]["batch_id"]
        self._gen_active = True
        create_jobs.enqueue_batch(records)
        self._mark_in_progress([r.get("kg_id") for r in records])
        self._show_written_ui()
        pts = sum(len(p.points) for p in plans)
        self._found_head.setText(
            "What your deck has for these gaps \u2014 and, marked NEW, the "
            f"{pts} card(s) being written for what it doesn\u2019t")
        self._gen_note.setText(
            f"{len(records)} gap(s) queued. Read the found cards above while "
            "this runs \u2014 nothing is added to your collection until you "
            "press Apply.")
        self._render_written_placeholders(records)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._poll.start()
        self._on_gen_poll()

    def _mark_in_progress(self, kg_ids) -> None:
        """Handed to a tool, so no longer untouched \u2014 same rule the KG page
        follows when it queues a gap."""
        for kg_id in kg_ids:
            try:
                kg = kg_store.get(kg_id)
                if kg and kg.get("status") == "open":
                    kg_store.update(kg_id, status="in_progress")
            except Exception as e:
                log.warn(f"work through: couldn't mark {kg_id} in progress: {e}")

    def _on_gen_poll(self) -> None:
        if self._closed:
            try:
                self._poll.stop()
            except RuntimeError:
                pass
            return
        from .. import create_jobs
        jobs = create_jobs.by_batch(self._gen_batch_id)
        if not jobs:
            self._poll.stop()
            self._gen_active = False
            self._progress.setVisible(False)
            self._stop_btn.setVisible(False)
            self._gen_note.setText("That batch is no longer in the job list.")
            self._update_selection_count()
            return
        counts = create_jobs.batch_progress(self._gen_batch_id)
        if counts["finished"]:
            self._poll.stop()
            self._gen_active = False
            self._progress.setVisible(False)
            self._stop_btn.setVisible(False)
            self._resurface()
            self._render_generated(jobs)
            return
        try:
            self._progress.setValue(
                int(100 * counts["done"] / max(1, counts["total"])))
        except RuntimeError:
            self._closed = True
            return
        try:
            self._update_written_placeholders(jobs)
            self._gen_note.setText(
                f"{counts['ready']} of {counts['total']} gap(s) written \u00b7 "
                f"{counts['running']} writing \u00b7 {counts['queued']} queued"
                + (f" \u00b7 {counts['error']} failed" if counts["error"] else ""))
        except RuntimeError:
            self._closed = True
            return
        self._update_selection_count()

    # ── the written cards live in the same tree as the found ones ────────

    def _gap_item(self, kg_id: str):
        """The top-level row for a gap, or None."""
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            if it.data(0, self.ROLE_KEY) == kg_id:
                return it
        return None

    def _written_folder(self, kg_id: str, create: bool = False):
        """The '✍ written' child under a gap. One per gap, always last, so the
        found points keep the positions they were rendered in."""
        top = self._gap_item(kg_id)
        if top is None:
            return None
        for i in range(top.childCount()):
            ch = top.child(i)
            if ch.data(0, self.ROLE_KIND) == "written":
                return ch
        if not create:
            return None
        folder = QTreeWidgetItem(top)
        folder.setData(0, self.ROLE_KIND, "written")
        folder.setData(0, self.ROLE_KEY, kg_id)
        folder.setFlags(folder.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        f = folder.font(0)
        f.setItalic(True)
        folder.setFont(0, f)
        return folder

    def _point_node(self, kg_id: str, concept: str):
        """The point row under `kg_id` whose concept is `concept`, or None."""
        top = self._gap_item(kg_id)
        if top is None or not concept:
            return None
        want = " ".join(str(concept).split()).casefold()
        for i in range(top.childCount()):
            ch = top.child(i)
            if ch.data(0, self.ROLE_KIND) != "point":
                continue
            got = " ".join(str(ch.data(0, self.ROLE_CONCEPT) or "").split())
            if got.casefold() == want:
                return ch
        return None

    def _point_concepts(self, job: dict) -> list:
        return [str(c or "") for c in (job.get("mass_points") or [])]

    def _card_home(self, kg_id: str, job: dict, card: dict):
        """Where a written card's row belongs: under the point it answers when
        the model said which one and that point is on screen, otherwise under
        the gap's own written folder.

        Unattributed is a real answer, not a failure — a card covering something
        the decomposition didn't name has no point to sit under, and inventing
        one would file it under a claim it isn't about."""
        try:
            n = int(card.get("point"))
        except (TypeError, ValueError):
            n = 0
        concepts = self._point_concepts(job)
        if 1 <= n <= len(concepts):
            node = self._point_node(kg_id, concepts[n - 1])
            if node is not None:
                return node
        return self._written_folder(kg_id, create=True)

    def _written_items(self) -> list:
        """[(QTreeWidgetItem, card dict)] for every written card on screen.

        A full walk, not two fixed levels: written rows sit under their point
        when the model attributed one and under the gap's folder when it didn't,
        so depth is not something this can assume."""
        out = []

        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, self.ROLE_KIND) == "newcard":
                    card = ch.data(0, self.ROLE_CARD)
                    if isinstance(card, dict):
                        out.append((ch, card))
                walk(ch)

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
        return out

    def _written_rows_under(self, node) -> list:
        """Written rows directly under one node — used to clear a point's old
        placeholder without disturbing the found cards beside it."""
        return [node.child(i) for i in range(node.childCount())
                if node.child(i).data(0, self.ROLE_KIND) in ("newcard", "pending")]

    _PENDING_TEXT = {
        "queued": "\u270d\ufe0e  queued \u2014 a card will be written for this",
        "running": "\u270d\ufe0e  writing a card for this\u2026",
        "ready": "\u270d\ufe0e  written \u2014 see below",
        "error": "\u26a0\ufe0f  couldn\u2019t write a card for this",
        "interrupted": "\u26d4  stopped before this was written",
    }

    def _pending_row(self, node, kg_id: str, text: str):
        """The 'a card is being written for this' row under ONE point.

        Placed before any card exists, deliberately: the tree must not silently
        grow branches under you while you are reading it, and an uncovered point
        that shows nothing at all reads as 'nothing is happening here'."""
        for ch in self._written_rows_under(node):
            if ch.data(0, self.ROLE_KIND) == "pending":
                ch.setText(0, text)
                return ch
        row = QTreeWidgetItem(node)
        row.setData(0, self.ROLE_KIND, "pending")
        row.setData(0, self.ROLE_KG, kg_id)
        row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        f = row.font(0)
        f.setItalic(True)
        row.setFont(0, f)
        row.setText(0, text)
        node.setExpanded(True)
        return row

    def _clear_pending(self, kg_id: str) -> None:
        top = self._gap_item(kg_id)
        if top is None:
            return
        stack = [top]
        while stack:
            it = stack.pop()
            for i in reversed(range(it.childCount())):
                ch = it.child(i)
                if ch.data(0, self.ROLE_KIND) == "pending":
                    it.removeChild(ch)
                else:
                    stack.append(ch)

    def _render_written_placeholders(self, records: list[dict]) -> None:
        """Mark every point that is about to have a card written for it.

        One row under each such POINT rather than one lump under the gap, so the
        answer to 'what is being done about this point' sits with the point."""
        self._tree.blockSignals(True)
        for rec in records:
            kg_id = str(rec.get("kg_id") or "")
            unplaced = 0
            for concept in self._point_concepts(rec):
                node = self._point_node(kg_id, concept)
                if node is None:
                    unplaced += 1
                    continue
                self._pending_row(node, kg_id, self._PENDING_TEXT["queued"])
            top = self._gap_item(kg_id)
            if top is not None:
                top.setExpanded(True)
            if unplaced:
                folder = self._written_folder(kg_id, create=True)
                if folder is not None:
                    folder.setText(
                        0, f"\u270d\ufe0e  queued \u2014 {unplaced} more card(s) "
                           "to write for this gap")
        self._tree.blockSignals(False)

    def _update_written_placeholders(self, jobs: list[dict]) -> None:
        self._tree.blockSignals(True)
        for j in jobs:
            kg_id = str(j.get("kg_id") or "")
            st = str(j.get("status") or "")
            text = self._PENDING_TEXT.get(st, st)
            if st == "error":
                text += " \u2014 " + str(j.get("error") or "generation failed")
            for concept in self._point_concepts(j):
                node = self._point_node(kg_id, concept)
                if node is not None:
                    self._pending_row(node, kg_id, text)
            folder = self._written_folder(kg_id)
            if folder is not None:
                n = len(j.get("mass_points") or [])
                folder.setText(0, {
                    "queued": f"\u270d\ufe0e  queued \u2014 {n} card(s) to write",
                    "running": f"\u23f3  writing {n} card(s)\u2026",
                    # NOT a tick. A tick next to a card that has not been added
                    # to the collection reads as "done" — and nothing here has
                    # touched the deck until Apply.
                    "ready": "\u270d\ufe0e  written \u2014 not added yet",
                    "error": "\u26a0\ufe0f  couldn\u2019t write these \u2014 "
                             + str(j.get("error") or "generation failed"),
                    "interrupted": "\u26d4  stopped before these were written",
                }.get(st, st))
        self._tree.blockSignals(False)

    def _render_generated(self, jobs: list[dict]) -> None:
        """The written cards, as rows under the gap they were written for.

        Filed under the POINT each card answers, so the deck's attempt at a
        point and the card written to replace it are adjacent rows. The model
        states the attribution itself (gen_prompt asks for a "point" number);
        it is never inferred from position, because one point legitimately
        splits into two cards and every later card would then be off by one.
        A card the model attributed to nothing falls back to a folder on the
        gap \u2014 unattributed is an answer, not a failure."""
        from .. import card_creator as cc
        from .mass_create import _norm_front

        self._gen_jobs = {str(j.get("kg_id") or ""): j for j in jobs}
        self._gen_profile = cc._resolved_profile(
            tool_config("card_creator"),
            self._notetype.currentText().strip()
            if self._notetype is not None else "")
        front_field = self._gen_profile.get("front_field", "Text")

        # ONE dup-check pass for the batch, not one per row: it is a find_notes
        # per card on the main thread. Within-batch near-duplicates are flagged
        # too \u2014 two gaps about the same entity produce the same card, and
        # nothing culls a freshly written duplicate the way search dedupes a
        # matched one.
        dups: dict[int, str] = {}
        seen: dict[str, str] = {}
        idx = 0
        for j in jobs:
            for card in (j.get("cards") or []):
                msg = cc.dup_check(card, front_field)
                key = _norm_front(card)
                if key and key in seen:
                    extra = f"\u26a0 near-duplicate of a card under \u2018{seen[key]}\u2019"
                    msg = f"{msg}  \u00b7  {extra}" if msg else extra
                elif key:
                    seen[key] = j.get("title") or "another gap"
                if msg:
                    dups[idx] = msg
                idx += 1

        self._tree.blockSignals(True)
        idx = 0
        for j in jobs:
            kg_id = str(j.get("kg_id") or "")
            if self._gap_item(kg_id) is None:
                idx += len(j.get("cards") or [])
                continue
            self._clear_pending(kg_id)
            folder = self._written_folder(kg_id, create=True)
            for leaf in list(self._written_rows_under(folder)):
                folder.removeChild(leaf)
            cards = list(j.get("cards") or [])
            verdicts = list(j.get("verdicts") or [])
            if not cards:
                st = j.get("status")
                folder.setText(0, (
                    "\u26a0\ufe0f  nothing was written for this gap \u2014 "
                    + str(j.get("error") or "the AI returned no cards")
                    + " \u00b7 right-click to retry")
                    if st in ("error", "interrupted")
                    else "\u2014  the AI returned no cards for this gap")
                idx += 1
                continue
            n_at_gap = 0
            for i, card in enumerate(cards):
                home = self._card_home(kg_id, j, card)
                if home is folder:
                    n_at_gap += 1
                leaf = QTreeWidgetItem(home)
                leaf.setData(0, self.ROLE_KIND, "newcard")
                leaf.setData(0, self.ROLE_KEY, i)
                leaf.setData(0, self.ROLE_CARD, card)
                # The gap is carried on the LEAF, not read off its parent: a row
                # filed under a point has the point as its parent, and the commit
                # needs to know which gap's tag box governs it.
                leaf.setData(0, self.ROLE_KG, kg_id)
                leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                leaf.setCheckState(0, Qt.CheckState.Checked)
                self._label_written(leaf, card, dups.get(idx), verdicts, i)
                home.setExpanded(True)
                idx += 1
            if n_at_gap:
                folder.setText(
                    0, f"\u270d\ufe0e  {n_at_gap} more card(s) written for this "
                       "gap \u2014 not added until you press Apply")
                folder.setExpanded(True)
            else:
                # Every card found a point to sit under, so the catch-all folder
                # has nothing left to hold.
                parent = folder.parent()
                if parent is not None:
                    parent.removeChild(folder)
        self._tree.blockSignals(False)
        self._gen_rendered = True
        self._show_written_ui()
        self._refresh_gen_head()

    def _label_written(self, leaf, card: dict, dup: str | None,
                       verdicts: list, i: int) -> None:
        """One written card\u2019s row: NEW marker, the front, and any warning.

        Marked in words, not by colour \u2014 a row that is merely a different
        shade reads as decoration next to the found cards it sits beside."""
        from ...core import anki_utils
        try:
            front = anki_utils.strip_html(str(card.get("front") or ""))
            back = anki_utils.strip_html(_card_back(card))
        except Exception:
            front, back = str(card.get("front") or ""), _card_back(card)
        bits = []
        v = verdicts[i] if i < len(verdicts) and isinstance(verdicts[i], dict) else {}
        verdict = str(v.get("verdict") or "").upper()
        if verdict in ("FAIL", "FLAG"):
            bits.append("quality: " + verdict.lower())
        if dup:
            bits.append(dup)
        suffix = ("   \u2014 " + "  \u00b7  ".join(bits)) if bits else ""
        leaf.setText(0, f"NEW  {_short(front, 240)}{suffix}")
        leaf.setToolTip(0, "\n\n".join(x for x in (
            f"FRONT\n{front}", (f"BACK\n{back}" if back else ""),
            (v.get("reason") or "") if verdict in ("FAIL", "FLAG") else "",
            "Double-click to edit this card before it is added.") if x))

    def _refresh_gen_head(self) -> None:
        items = self._written_items()
        approved = sum(1 for it, _c in items
                       if it.checkState(0) == Qt.CheckState.Checked)
        try:
            self._found_head.setText(
                "What your deck has for these gaps \u2014 and, marked NEW, the "
                f"cards written for what it didn\u2019t "
                f"({approved} of {len(items)} ticked)")
            self._gen_note.setText(self._gen_note_text())
            self._gen_note.setVisible(True)
        except RuntimeError:
            self._closed = True
            return
        self._update_selection_count()

    def _show_written_ui(self) -> None:
        for _wgt in (self._written_lbl, self._gen_approve_all,
                     self._gen_reject_all):
            _wgt.setVisible(True)
            _wgt.setEnabled(self._gen_rendered)
        self._gen_note.setVisible(True)

    def _gen_note_text(self) -> str:
        """The line under the tree. Rebuilt from BOTH halves\u2019 state, because
        ticking a found card changes what it should say."""
        note = self._crosstalk_note()
        blank = [kg_id for kg_id, j in self._gen_jobs.items()
                 if not (j.get("cards") or [])]
        if blank:
            note = ((note + "  ") if note else "") + (
                f"{len(blank)} gap(s) came back with no cards \u2014 right-click "
                "a \u2018couldn\u2019t write\u2019 row to retry it.")
        return note or ("Rows marked NEW are proposals. Untick any you don\u2019t "
                        "want, double-click one to edit it \u2014 they go in with "
                        "the ticked found cards, on one Apply, in one step.")

    def _crosstalk_note(self) -> str:
        """The one thing a single tree can say that two panels cannot: you have
        since ticked found cards covering a gap that was written for.

        Generation ran on what the SEARCH found, so promoting a candidate card
        afterwards can make a written card redundant. Not auto-unticked \u2014
        that is a judgement \u2014 but said out loud, per gap, in words."""
        if not self._gen_jobs:
            return ""
        full, _partial, _empty = self._outcome_split()
        covered = set(full)
        names = []
        for kg_id, j in self._gen_jobs.items():
            if kg_id not in covered or not (j.get("cards") or []):
                continue
            folder = self._written_folder(kg_id)
            if folder is None:
                continue
            if any(folder.child(k).checkState(0) == Qt.CheckState.Checked
                   for k in range(folder.childCount())):
                names.append(str(j.get("title") or "a gap"))
        if not names:
            return ""
        shown = ", ".join(f"\u201c{n}\u201d" for n in names[:3])
        more = f" and {len(names) - 3} more" if len(names) > 3 else ""
        return (f"You\u2019ve since ticked cards covering every point of "
                f"{shown}{more} \u2014 the NEW cards there may now be redundant.")

    def _on_gen_retry(self, job_id: str) -> None:
        from .. import create_jobs
        if not job_id:
            return
        create_jobs.retry_job(job_id)
        self._gen_active = True
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._gen_note.setText("Retrying\u2026")
        self._poll.start()
        self._update_selection_count()

    # ── the single commit ────────────────────────────────────────────────────

    def _on_apply_everything(self) -> None:
        """Tag and unsuspend what your deck already had, AND add the cards
        written for what it didn't \u2014 inside ONE checkpoint.

        This is the whole reason the two halves share a screen: one decision,
        one confirm, and one Ctrl+Z that takes the entire pass back out."""
        if not anki_utils.require_col() or not self._coverage:
            return
        if self._gen_active and not askUser(
                "Cards are still being written for some of these gaps.\n\n"
                "Apply what your deck already had now, and leave the writing "
                "running? You'd add those cards separately from AI Create.",
                defaultno=True):
            return

        ticked = self._ticked()
        all_nids = sorted({nid for rec in ticked.values() for nid in rec["nids"]})
        batches = self._approved_written()
        n_new = sum(len(c) for _k, c in batches)
        if not all_nids and not n_new:
            tooltip("Nothing ticked and nothing approved \u2014 nothing to apply.")
            return

        full, _partial, _empty = self._outcome_split()
        do_unsuspend = self._unsuspend_cb.isChecked()
        deck = ((self._deck.currentText().strip()
                 if self._deck is not None else "") or "Default")
        untagged = [kg_id for kg_id, rec in ticked.items()
                    if rec["nids"] and not self._tag_text(kg_id)]

        lines = ["Apply this whole pass?", ""]
        if all_nids:
            lines.append(
                f"\u2022 Tag {len(all_nids)} note(s) you already had, each under "
                "its own gap's tag"
                + (", and unsuspend them." if do_unsuspend else "."))
        if n_new:
            lines.append(f"\u2022 Add {n_new} newly written card(s) to "
                         f"\u201c{deck}\u201d across {len(batches)} gap(s).")
        if untagged:
            lines.append(f"\u2022 {len(untagged)} gap(s) have an empty tag box \u2014 "
                         "their found cards are unsuspended but not tagged.")
        lines.append("")
        lines.append("Existing card content is never modified, and one Ctrl+Z "
                     "takes the whole thing back out.")
        if not askUser("\n".join(lines), title="Work through gaps"):
            return

        # ONE checkpoint around both halves.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Ankisstant: work through gaps")

        tagged = unsuspended = 0
        if all_nids:
            tagged, unsuspended = self._apply_found(ticked, all_nids, do_unsuspend)
        created_by_kg = self._apply_created(batches, deck)

        # A gap closes if your deck turned out to cover it, or if cards were
        # just written for it. Everything else stays open on purpose.
        closed = self._close_gaps(sorted(set(
            list(full) + [k for k, ids in created_by_kg.items() if ids])))

        n_created = sum(len(v) for v in created_by_kg.values())
        self._applied = True
        self._commit_btn.setEnabled(False)
        self._commit_btn.setText("Applied")
        bits = []
        if all_nids:
            bits.append(f"{len(all_nids)} found note(s) committed"
                        + (f" ({tagged} tagged)" if tagged != len(all_nids) else "")
                        + (f", {unsuspended} card(s) unsuspended"
                           if do_unsuspend else ""))
        if n_created:
            bits.append(f"{n_created} card(s) written into \u201c{deck}\u201d")
        bits.append(f"{closed} gap(s) closed")
        msg = "Done \u2014 " + ", ".join(bits) + "."
        self._set_status(msg)
        tooltip(msg, period=5000)
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass

    def _approved_written(self) -> list:
        """[(kg_id, [card, ...])] for the ticked written cards, per gap."""
        by_kg: dict[str, list] = {}
        for leaf, card in self._written_items():
            if leaf.checkState(0) != Qt.CheckState.Checked:
                continue
            kg_id = str(leaf.data(0, self.ROLE_KG) or "")
            by_kg.setdefault(kg_id, []).append(card)
        return [(k, v) for k, v in by_kg.items() if v]

    def _apply_created(self, batches, deck: str) -> dict:
        """Commit the ticked written cards. No checkpoint of its own — the
        caller opened one around this and the found cards together."""
        from .. import card_creator as cc
        from .mass_create import commit_ctx_for
        created_by_kg: dict[str, list] = {}
        if not batches:
            return created_by_kg
        profile = self._gen_profile or cc._resolved_profile(
            tool_config("card_creator"),
            self._notetype.currentText().strip()
            if self._notetype is not None else "")
        nt = cc.resolve_notetype(profile)
        if nt is None:
            return created_by_kg
        for kg_id, cards in batches:
            job = self._gen_jobs.get(kg_id) or {}
            # The gap's tag box governs BOTH halves. Re-read it now rather than
            # trusting what the job was enqueued with, so editing the tag after
            # generation moves the written cards along with the found ones —
            # which is the whole point of them sharing a row.
            tags = [t for t in (job.get("tags_raw") or []) if t]
            box = self._tag_text(kg_id)
            if box and box not in tags:
                tags.append(box)
            try:
                ids = cc.commit_cards(
                    cards, ctx=commit_ctx_for(job, profile, tags),
                    deck_name=(job.get("deck_name") or "").strip() or deck,
                    notetype=nt)
            except Exception as e:
                log.error("work through: committing "
                          f"'{job.get('title')}' failed: {e}")
                continue
            created_by_kg[kg_id] = ids
            try:
                cc._append_session(
                    mode="topic", source=f"work-through:{self._gen_batch_id}",
                    topic=str(job.get("title") or ""),
                    cards_proposed=len(job.get("cards") or []),
                    cards_created=len(ids), card_ids=ids)
            except Exception as e:
                log.warn(f"work through: session log failed: {e}")
        # Only once the writing has actually finished: clearing a batch that
        # still has jobs running would delete work in flight.
        if not self._gen_active:
            try:
                from .. import create_jobs
                create_jobs.remove_batch(self._gen_batch_id)
            except Exception as e:
                log.warn(f"work through: couldn't clear the job batch: {e}")
        return created_by_kg

    def _continue_to_create(self) -> None:
        """The old two-window hand-off. Only reached when the writing half is
        switched off on the plan page \u2014 otherwise the cards are written into
        the bottom of the results screen and committed with the found ones."""
        if self._closed:
            return
        payload = self._create_payload()
        if not payload:
            msg = ("Every point was covered by a card you already have — "
                   "nothing left to write.")
            self._set_status(msg)
            tooltip(msg, period=5000)
            return
        pts = sum(len(k["points"]) for k in payload)
        if not askUser(
            f"Write cards for the {pts} point(s) your deck didn't cover, "
            f"across {len(payload)} gap(s)?\n\n"
            "Mass AI Create opens with them already planned — you review every "
            "card before anything is added to your collection.",
            title="Work through gaps"):
            self._set_status(
                f"Stopped after the search. {len(payload)} gap(s) stay open — "
                "run them through AI Create when you want the cards.")
            return
        parent = self.parent()
        self.close()
        from .mass_create import open_mass_create
        open_mass_create(payload, parent=parent)

    def _tag_text(self, kg_id: str) -> str:
        edit = self._tag_edits.get(kg_id)
        return edit.text().strip() if edit is not None else ""

    def _close_gaps(self, kg_ids: list[str]) -> int:
        """Mark fully-covered gaps done. Best-effort: a store failure must never
        undo the tagging and unsuspending that already succeeded."""
        closed = 0
        for kg_id in kg_ids:
            try:
                if kg_store.update(kg_id, status="done"):
                    closed += 1
            except Exception as e:
                log.error(f"bulk search: couldn't close gap {kg_id}: {e}")
        try:
            from .. import knowledge_gaps
            knowledge_gaps._refresh_open_panel()
        except Exception:
            pass
        return closed

    # ── hand-off ─────────────────────────────────────────────────────────────

    def _main_window(self):
        """The Ankisstant window that owns the queues. Walks up from this
        dialog's parent, then falls back to the module singleton — the dialog
        can be parented to Anki's own main window, which has no queues."""
        try:
            from ...ui.main_window import queue_owner
            return queue_owner(self.parent())
        except Exception as e:
            log.warn(f"bulk search: couldn't find the Ankisstant window: {e}")
            return None

    def _uncovered_points(self) -> dict[str, list]:
        """{kg_id: [PointMatch, ...]} for the points with nothing ticked under
        them. These — not the gap title — are what AI Create actually needs:
        'Graves + Tx' with 5 of 7 points already covered wants cards for the
        remaining 2, not another pass at the whole disease."""
        out: dict[str, list] = {}
        by_kg: dict[str, list] = {}
        for m in list(self._coverage.covered) + list(self._coverage.gaps):
            by_kg.setdefault(getattr(m.point, "kg_id", ""), []).append(m)

        for gi in range(self._tree.topLevelItemCount()):
            g = self._tree.topLevelItem(gi)
            kg_id = g.data(0, self.ROLE_KEY)
            group = by_kg.get(kg_id) or []
            missing = []
            for pi in range(g.childCount()):
                p = g.child(pi)
                if p.data(0, self.ROLE_KIND) != "point":
                    continue
                ticked = any(
                    p.child(ni).data(0, self.ROLE_KIND) == "note"
                    and p.child(ni).checkState(0) == Qt.CheckState.Checked
                    for ni in range(p.childCount())
                )
                idx = p.data(0, self.ROLE_KEY)
                if not ticked and isinstance(idx, int) and 0 <= idx < len(group):
                    missing.append(group[idx])
            out[kg_id] = missing
        return out

    def _on_send_to_create(self) -> None:
        """Hand the gaps your deck doesn't cover to AI Create — WITH the specific
        points it couldn't find, and only the ones you pick.

        Uses the same queue shape the KG page pushes, so AI Create can't tell
        where a queued gap came from."""
        if not self._coverage:
            return
        main = self._main_window()
        if main is None:
            showWarning("Open Ankisstant from Tools → Ankisstant first.")
            return

        missing_by_kg = self._uncovered_points()
        plans_by_id = {p.kg_id: p for p in self._run_plans}
        rows = []
        for kg_id, missing in missing_by_kg.items():
            plan = plans_by_id.get(kg_id)
            if plan is None:
                continue
            rows.append((plan, missing))
        if not rows:
            tooltip("Nothing searched yet.")
            return

        dlg = _SendToCreateDialog(rows, parent=self)
        if not dlg.exec():
            return
        chosen = dlg.chosen()          # [(plan, [PointMatch, ...])]
        if not chosen:
            tooltip("Nothing selected.")
            return

        queued, sent_ids = 0, []
        for plan, missing in chosen:
            kg = kg_store.get(plan.kg_id)
            if not kg:
                continue
            gaps_text = [m.point.concept for m in missing]
            notes_bits = []
            own = kg_store.field(kg, "notes")
            if own:
                notes_bits.append(own)
            if gaps_text:
                # Plain prose, because this lands in the Create form's context
                # box and is read by both the user and the model.
                notes_bits.append(
                    "My deck has no card for these parts of this gap:\n"
                    + "\n".join(f"• {t}" for t in gaps_text))
            main.gap_queue.append({
                "title":       kg.get("title", ""),
                "kg_id":       plan.kg_id,
                "stem_html":   kg_store.field(kg, "stem_html") or None,
                "notes":       "\n\n".join(notes_bits) or None,
                "concept":     kg_store.field(kg, "concept") or plan.concept,
                "explanation": kg_store.field(kg, "explanation") or "",
                "images":      list((kg.get("fields") or {}).get("images") or []),
                "kg_type":     (kg.get("type") or "").lower(),
                "group":       str(kg.get("group") or "").strip(),
                "tags":        [t for t in (kg.get("tags") or []) if t],
                # Structured copy of the same thing, for when Create learns to
                # generate one card per uncovered point.
                "points":      [
                    {"concept": m.point.concept, "terms": list(m.point.terms),
                     "system": m.point.system, "subsystem": m.point.subsystem,
                     "topic": m.point.topic}
                    for m in missing
                ],
                "target_cards": max(1, len(gaps_text)),
            })
            queued += 1
            sent_ids.append(plan.kg_id)

        # Handed to a tool, so no longer untouched — same rule as the KG page.
        for kg_id in sent_ids:
            try:
                kg = kg_store.get(kg_id)
                if kg and kg.get("status") == "open":
                    kg_store.update(kg_id, status="in_progress")
            except Exception as e:
                log.warn(f"bulk search: couldn't mark {kg_id} in progress: {e}")
        try:
            from .. import knowledge_gaps
            knowledge_gaps._refresh_open_panel()
        except Exception:
            pass

        if hasattr(main, "refresh_queue_badge"):
            main.refresh_queue_badge()
        # Refresh Create's queue view in place — no tab switch.
        if hasattr(main, "refresh_tool_queue"):
            main.refresh_tool_queue("card_creator")
        msg = (f"Queued {queued} gap(s) for AI Create, with the points your deck "
               "is missing attached.")
        self._set_status(msg)
        tooltip(msg, period=5000)

    # ── misc ─────────────────────────────────────────────────────────────────

    def _on_back(self) -> None:
        # Once cards have been written FOR this search, going back and running a
        # different one would leave a batch of cards attached to results that no
        # longer exist. Close and start again instead — the jobs are on disk, so
        # nothing written is lost either way.
        if self._gen_batch_id:
            tooltip("The cards below were written for this search. Close this "
                    "window and start again to search different gaps.")
            return
        self._stack.setCurrentIndex(0)
        self._back_btn.setVisible(False)
        self._read_btn.setVisible(True)
        self._search_btn.setVisible(True)
        self._commit_btn.setVisible(False)
        self._refresh_buttons()
        self._refresh_title()

    def _set_busy(self, on: bool, msg: str = "", *, progress: bool = False,
                  stoppable: bool = False) -> None:
        self._busy = on
        if msg:
            self._status.setText(msg)
        self._progress.setVisible(on and progress)
        if on and progress:
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
        self._stop_btn.setVisible(on and stoppable)
        self._stop_btn.setEnabled(on and stoppable)
        self._table.setEnabled(not on)
        # Close stays live while busy — that's the whole point of being able to
        # send the run to the background.
        self._close_btn.setEnabled(True)
        self._refresh_buttons()

    def _set_status(self, msg: str) -> bool:
        """Progress arrives from a worker thread long after the user may have
        stopped and closed this window. Returns False if there's nothing left
        to write to."""
        if self._closed:
            return False
        try:
            self._status.setText(msg)
            return True
        except RuntimeError:      # the underlying Qt widget is gone
            self._closed = True
            return False

    def _resurface(self) -> None:
        """Bring a backgrounded run's window back when it has something to show."""
        if not self._backgrounded:
            return
        self._backgrounded = False
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev) -> None:
        """Closing mid-run offers to keep searching. Anki's own long jobs are
        modal-ish and that's what made a 20-minute search feel like a hostage
        situation — there is nothing about this run that needs the window open."""
        if self._busy:
            if askUser(
                "Keep searching in the background?\n\n"
                "Yes — this window hides and reopens by itself when the search "
                "finishes.\n"
                "No — stop the search and close.",
                defaultno=False,
            ):
                self._backgrounded = True
                ev.ignore()
                self.hide()
                tooltip("Still searching — this window will reopen when it's done.",
                        period=4000)
                return
            self._on_stop()
        if self._gen_active:
            if askUser(
                "Cards are still being written for these gaps.\n\n"
                "Yes \u2014 keep writing in the background; this window hides and "
                "reopens by itself when they're ready.\n"
                "No \u2014 stop writing and close.",
                defaultno=False,
            ):
                self._backgrounded = True
                ev.ignore()
                self.hide()
                tooltip("Still writing \u2014 this window will reopen when it's done.",
                        period=4000)
                return
            try:
                from .. import create_jobs
                create_jobs.cancel_batch(self._gen_batch_id)
            except Exception as e:
                log.warn(f"work through: couldn't cancel the batch: {e}")
            self._gen_active = False
        elif self._gen_rendered and not self._applied:
            pending = sum(len(c) for _k, c in self._approved_written())
            if pending:
                # Three real answers, so three buttons. The old two-way question
                # silently left the batch behind, and a leftover batch shows up
                # in AI Create later looking like a create run the user never
                # started \u2014 which is exactly how it was found.
                box = QMessageBox(self)
                box.setWindowTitle("Close without adding?")
                box.setIcon(QMessageBox.Icon.Question)
                box.setText(f"{pending} written card(s) haven\u2019t been added "
                            "yet, and the found cards on this screen aren\u2019t "
                            "committed either.")
                box.setInformativeText(
                    "Keep them and they wait in AI Create as a \u201cWork "
                    "through gaps\u201d batch you can commit or discard there. "
                    "The search results are not saved either way.")
                keep = box.addButton("Close and keep the cards",
                                     QMessageBox.ButtonRole.AcceptRole)
                drop = box.addButton("Close and discard them",
                                     QMessageBox.ButtonRole.DestructiveRole)
                back = box.addButton("Stay here", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(back)
                box.exec()
                clicked = box.clickedButton()
                if clicked is back or clicked is None:
                    ev.ignore()
                    return
                if clicked is drop:
                    try:
                        from .. import create_jobs
                        create_jobs.remove_batch(self._gen_batch_id)
                    except Exception as e:
                        log.warn("work through: couldn't discard the batch: "
                                 f"{e}")
                _ = keep
        self._closed = True
        try:
            self._poll.stop()
        except RuntimeError:
            pass
        ev.accept()
        self.deleteLater()

    def reject(self) -> None:
        # Esc and the window chrome both land here; route them through the same
        # close path so the background offer isn't bypassed.
        self.close()


class _PointApprovalDialog(QDialog):
    """Every learning point the read produced, editable, before anything runs.

    The plan table reviews GAPS; this reviews what each gap was split INTO,
    which is the level the search and the generation both actually work at. A
    point that misreads the gap costs a wasted search and — in unified mode — a
    wasted card, and it is invisible until you look at the points themselves."""

    def __init__(self, plans, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review the learning points")
        self.setMinimumSize(760, 520)
        self._plans = list(plans)

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)
        head = QLabel(
            "Untick a point to drop it, or double-click to edit either column.<br>"
            "<b>Search terms are what is actually looked up in your deck</b> — "
            "the point's wording only decides whether a card that came back is "
            "judged relevant. If a search is missing cards you know you have, "
            "add the words those cards actually use. Comma-separated."
        )
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        col.addWidget(head)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Gap  ·  learning point", "Search terms"])
        th = self._tree.header()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        th.setStretchLastSection(False)
        self._tree.setColumnWidth(1, 260)
        self._tree.setAlternatingRowColors(True)
        self._tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        col.addWidget(self._tree, 1)

        self._items: list[tuple] = []        # (plan, point, QTreeWidgetItem)
        for plan in self._plans:
            top = QTreeWidgetItem(self._tree)
            top.setText(0, f"{plan.title or '(untitled)'}      "
                           f"{len(plan.points)} point(s)")
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for pt in plan.points:
                leaf = QTreeWidgetItem(top)
                leaf.setText(0, pt.concept)
                leaf.setText(1, ", ".join(pt.terms))
                leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable
                              | Qt.ItemFlag.ItemIsEditable)
                leaf.setCheckState(0, Qt.CheckState.Checked)
                levels = " › ".join(x for x in (pt.system, pt.subsystem, pt.topic) if x)
                leaf.setToolTip(0, "\n".join([pt.concept] + ([levels] if levels else [])))
                leaf.setToolTip(1, "The words looked up in your deck, comma-"
                                   "separated. Edit freely — a term that returns "
                                   "nothing is the single biggest cause of a "
                                   "false gap.")
                self._items.append((plan, pt, leaf))
            top.setExpanded(True)

        row = QHBoxLayout()
        for label, state in (("Tick all", Qt.CheckState.Checked),
                             ("Untick all", Qt.CheckState.Unchecked)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, st=state: self._set_all(st))
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("Use these points  ▶")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        col.addLayout(row)
        theme_dialog(self)

    def _set_all(self, state) -> None:
        for _plan, _pt, item in self._items:
            item.setCheckState(0, state)

    def apply(self) -> tuple[int, int]:
        """Write the ticks and edits back onto the plans.

        Returns (points kept, points left with no search terms). A plan can come
        out with none — the caller unticks it rather than searching a gap with
        nothing to search for. A point with no terms is kept, not corrected:
        it can still be reached by the tag leg, and silently reinstating the
        terms the user just deleted would be worse than saying so."""
        keep: dict[int, list] = {}
        termless = 0
        for plan, pt, item in self._items:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            text = " ".join(item.text(0).split())
            if not text:
                continue           # edited down to nothing = dropped
            pt.concept = text
            pt.terms = _parse_terms(item.text(1))
            if not pt.terms:
                termless += 1
            keep.setdefault(id(plan), []).append(pt)
        total = 0
        for plan in self._plans:
            plan.points = keep.get(id(plan), [])
            total += len(plan.points)
        return total, termless


class _WrittenCardDialog(QDialog):
    """Edit one written card before it is added.

    Not the full Create review row. The judgement being made on this screen is
    "is this the right replacement for what my deck didn't have", and the found
    cards it is being compared against are two rows away — so this is the front,
    the back, and the one action that changes the card wholesale. Re-rolling a
    card repeatedly belongs in AI Create, where the whole batch is the subject."""

    SPLIT = 2      # a third exec() outcome, beside accept (1) and reject (0)

    def __init__(self, card: dict, profile: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit the written card")
        self.setMinimumSize(640, 460)
        self._card = dict(card)
        self._profile = profile or {}

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(6)
        head = QLabel("This card hasn't been added to your collection yet \u2014 "
                      "nothing here touches your deck until you press Apply.")
        head.setStyleSheet("color: gray; font-size: 11px;")
        head.setWordWrap(True)
        col.addWidget(head)

        col.addWidget(QLabel("<b>Front</b>"))
        self._front = QPlainTextEdit(str(card.get("front") or ""))
        self._front.setMinimumHeight(140)
        col.addWidget(self._front, 2)
        col.addWidget(QLabel("<b>Back / Extra</b>"))
        # "extra" is the key the generator, the splitter, the one-by-one rewrite
        # and commit_cards all use; "back" is only ever a stray. Reading "back"
        # here showed an empty box for every card and writing it back put the
        # edit somewhere nothing reads.
        self._back = QPlainTextEdit(_card_back(card))
        self._back.setMinimumHeight(90)
        col.addWidget(self._back, 1)

        self._status = QLabel("")
        self._status.setStyleSheet("color: gray; font-size: 11px;")
        self._status.setWordWrap(True)
        col.addWidget(self._status)

        row = QHBoxLayout()
        for text, tip, fn in (
            ("\u21ba  Rewrite with AI\u2026",
             "Write this card again, optionally with a note on what was wrong "
             "with it.", self._on_regen),
            ("1\ufe0f\u20e3  One-by-one",
             "Rewrite as one note with sibling {{c1}} clozes, for AnKing\u2019s "
             "One-by-one reveal. Same rewrite as AI Create\u2019s button.",
             self._on_obo),
            ("\u2702\ufe0f  Split",
             "Break this into the smallest set of atomic single-cloze cards. "
             "They replace this row in the results tree.", self._on_split),
        ):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("Keep this")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        col.addLayout(row)
        theme_dialog(self)

    def _on_regen(self) -> None:
        from aqt.qt import QInputDialog
        from ...core.config import tool_model_for
        hint, ok = QInputDialog.getText(
            self, "Rewrite this card",
            "What was wrong with it? (optional)")
        if not ok:
            return
        self._status.setText("Rewriting\u2026")
        QApplication.processEvents()
        try:
            from .. import card_creator as cc
            new = cc._regenerate_one(self.values(), hint.strip(), self._profile,
                                     "", tool_model_for("card_creation"))
        except Exception as e:
            log.error(f"work through: rewrite failed: {e}")
            self._status.setText(f"Couldn't rewrite it: {e}")
            return
        if not new:
            self._status.setText("The AI didn't return a replacement.")
            return
        self._apply_new(new, "Rewritten \u2014 read it before you keep it.")

    def _apply_new(self, new: dict, msg: str) -> None:
        self._front.setPlainText(str(new.get("front") or ""))
        self._back.setPlainText(_card_back(new))
        self._status.setText(msg)

    def _on_obo(self) -> None:
        """Same rewrite AI Create\u2019s One-by-one button runs, same function."""
        from .. import card_creator as cc
        self._status.setText("Rewriting one-by-one\u2026")
        QApplication.processEvents()
        try:
            new = cc.one_by_one_rewrite(self.values(), self._profile)
        except Exception as e:
            log.error(f"work through: one-by-one failed: {e}")
            self._status.setText(f"Couldn\u2019t rewrite it: {e}")
            return
        if not new:
            self._status.setText("The AI didn\u2019t return a usable card.")
            return
        self._apply_new(new, "Rewritten as sibling clozes \u2014 read it before "
                             "you keep it.")

    def _on_split(self) -> None:
        """Splitting makes MORE cards than this dialog can hold, so it is done
        on the tree row instead \u2014 which is where the extra rows have to
        appear anyway."""
        self.done(self.SPLIT)

    def values(self) -> dict:
        out = dict(self._card)
        out["front"] = self._front.toPlainText().strip()
        out["extra"] = self._back.toPlainText().strip()
        out.pop("back", None)
        return out


class _SendToCreateDialog(QDialog):
    """Pick which searched gaps go to AI Create, and see exactly what each one
    would ask for.

    Exists because "send the uncovered ones" was doing the choosing on his
    behalf. A partly-covered gap is the interesting case — some of it is in the
    deck and some isn't — and whether the residue is worth a card is a judgement
    only he can make, per gap.
    """

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Send gaps to AI Create")
        self.setMinimumSize(720, 480)
        self._rows = rows                    # [(plan, [PointMatch, ...])]

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)
        head = QLabel(
            "Each gap goes over with the specific points your deck had nothing "
            "for — so Create writes cards for the residue, not the whole topic "
            "again. Fully-covered gaps are listed but left unticked.")
        head.setWordWrap(True)
        col.addWidget(head)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(1)
        self._tree.setHeaderLabels(["Gap  ·  what your deck is missing"])
        self._tree.setAlternatingRowColors(True)
        col.addWidget(self._tree, 1)

        for plan, missing in rows:
            top = QTreeWidgetItem(self._tree)
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            n = len(missing)
            state = (f"{n} point(s) uncovered" if n
                     else "fully covered — nothing to make")
            top.setText(0, f"{plan.title}      {state}")
            top.setCheckState(0, Qt.CheckState.Checked if n
                              else Qt.CheckState.Unchecked)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for m in missing:
                leaf = QTreeWidgetItem(top)
                leaf.setText(0, _short(m.point.concept, 160))
                leaf.setToolTip(0, f"{m.point.concept}\n\nterms: {m.point.terms}")
                leaf.setFlags(leaf.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            top.setExpanded(bool(missing))

        row = QHBoxLayout()
        for label, state in (("Tick all", Qt.CheckState.Checked),
                             ("Untick all", Qt.CheckState.Unchecked)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, s=state: self._set_all(s))
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        send = QPushButton("Send to AI Create")
        send.setDefault(True)
        send.clicked.connect(self.accept)
        row.addWidget(send)
        col.addLayout(row)
        theme_dialog(self)

    def _set_all(self, state) -> None:
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, state)

    def chosen(self):
        out = []
        for i, (plan, missing) in enumerate(self._rows):
            if self._tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked:
                out.append((plan, missing))
        return out


def open_unified(kgs: list[dict], parent=None) -> None:
    """Search AND create, on ONE screen: the deck is searched for every gap, the
    points it can't cover are written as cards into the bottom of the same
    results page, and one button commits both under one checkpoint. See
    BulkSearchDialog._build_results_page."""
    open_bulk_search(kgs, parent=parent, unified=True)


def open_bulk_search(kgs: list[dict], parent=None, *, unified: bool = False) -> None:
    """Entry point. `kgs` are full KG records from tools/kg/store.py."""
    kgs = [k for k in (kgs or []) if isinstance(k, dict)
           and str(k.get("title") or "").strip()]
    if not kgs:
        tooltip("No knowledge gaps to search.")
        return
    dlg = BulkSearchDialog(kgs, parent=parent, unified=unified)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    # DELIBERATELY does not read on open. It used to, on the theory that an
    # unread table has nothing worth deciding about — but that spends an AI call
    # the moment the window appears, and it skips straight past the one screen
    # where the notes and the tick boxes can still be corrected. Opening a
    # window is not consent to spend tokens. Press "Read gaps with AI".
