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
# side by side and committed by ONE button. Generation runs while you read the
# found cards, which is the real reason the two halves share a screen: in a
# wizard that time is spent staring at a progress bar.
#
# WHERE THE WRITTEN CARDS GO, AND WHAT CTRL+Z THEN MEANS.
# Apply sends them through Anki's own Add window, one at a time, the way AI
# Create always has: the last look at a card belongs in the editor it will
# live in, not in a tree row. "Add all now" beside it puts them straight in,
# for when you already trust them. That choice decides the undo story, so it
# is stated on the confirm rather than promised here — straight in keeps the
# old guarantee that one Ctrl+Z takes the whole pass back out, while cards
# added through Anki's window are Anki's own undo entries, one each. The found
# half commits immediately either way, because tagging and unsuspending notes
# you already have is not something the Add window can do.
#
# WHY THE RESULTS ARE GROUPED BY GAP, AND TAGGED PER GAP.
# A lecture commits everything under one tag because everything came from one
# lecture. A batch of gaps did not — they're unrelated, and one shared tag would
# throw away the classification triage already did. So every gap gets its OWN
# editable tag (prefilled with the hierarchical base::Type::System::Subsystem::
# Topic tag, the same scheme AI Browse and AI Create use) and its own subtree in
# the results, showing which of its points the deck covers and which it doesn't.

from __future__ import annotations

import re
import threading
import uuid

from aqt import mw
from aqt.qt import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QEvent,
    QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QPlainTextEdit,
    QProgressBar,
    QPushButton, QRect, QSpinBox, QStackedWidget, QStyledItemDelegate, Qt,
    QTableWidget, QTableWidgetItem, QTextCursor,
    QTimer, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from aqt.utils import askUser, showInfo, showWarning, tooltip
from aqt.webview import AnkiWebView

from ...core import anki_utils, api as core_api, log
from ...core.config import month_tag, tool_config
from ...core.qt_utils import (
    attach_tag_completer, is_manual_provider, provider_configured,
    resurface_at_home, theme_dialog, tips_on_checkbox_only,
)
from .. import autotag
from . import store as kg_store, triage as kg_triage
from .. import commit
from .. import pipeline
from ..lecture.door import LectureDoorMixin
from .card_pane import CardPaneMixin


# How many judge-approved-but-not-chosen cards to offer per point, under the
# ones the budget actually picked. The point is override room, not a second
# results list: the allocator's choice is what's ticked, and this is the short
# tail he can promote from if it got one wrong.
EXTRA_CANDIDATES_PER_POINT = 6

# Plan-table columns, named because the table is index-addressed in a dozen
# places and inserting one by hand is how the scope combo ends up in the card
# spinner's column.
# FIND and WRITE are separate columns rather than one "include" tick because
# they are separate decisions: a gap you know your deck covers wants finding
# and not writing, and a gap you know it doesn't wants writing without paying
# for the search. Untick both and the gap sits the run out.
C_GAP, C_FIND, C_WRITE, C_NOTES, C_READ, C_SCOPE, C_CARDS, C_PTS = range(8)


class _AllPoints:
    """A PointMatch stand-in for a gap that was never searched.

    _create_payload reads `.point` off whatever _uncovered_points yields. A gap
    with Find unticked has no matches at all, so its points are wrapped to walk
    the same path rather than growing a second branch that builds the record a
    slightly different way."""

    __slots__ = ("point",)

    def __init__(self, point):
        self.point = point


def split_budget(wants: list, total: int) -> dict:
    """Share `total` cards across [(key, wanted)] in order, never giving a key
    more than it wants. Largest-remainder proportional, so one big section
    can't starve the rest; ties go to the earlier key (teaching order)."""
    wants = [(k, max(0, int(n))) for k, n in wants]
    need = sum(n for _k, n in wants)
    total = max(0, int(total))
    if need <= total:
        return {k: n for k, n in wants}
    if not need:
        return {k: 0 for k, _n in wants}
    shares = [(k, n, n * total / need) for k, n in wants]
    out = {k: min(n, int(sh)) for k, n, sh in shares}
    left = total - sum(out.values())
    order = sorted(range(len(shares)),
                   key=lambda i: (-(shares[i][2] - int(shares[i][2])), i))
    for i in order:
        if left <= 0:
            break
        k, n, _sh = shares[i]
        if out[k] < n:
            out[k] += 1
            left -= 1
    return out


def create_record_for(kg: dict, concept: str, points: list[dict],
                      style_examples: list[str] | None = None) -> dict:
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
        "resources":    list(kg.get("resources") or []),
        "concept":      str(concept or "").strip() or str(kg.get("title") or ""),
        "notes":        "\n\n".join(notes_bits),
        "points":       list(points),
        # One card per uncovered point is the honest default: the point IS the
        # thing the deck is missing.
        "target_cards": max(1, len(points)),
        # Fronts of cards the search found for this gap, so the writer can see
        # what the cards beside its own look like (gen_prompt's STYLE REFERENCE).
        "style_examples": [str(x) for x in (style_examples or []) if str(x).strip()],
    }


def reclassify_points(points, report=None) -> int:
    """Re-derive the tag levels of every point whose wording the user rewrote.

    Qt-free and safe off the main thread (autotag.classify reads the cached
    vocab, not the collection) — it is called from inside the search's
    background job, where the user is already waiting, rather than blocking the
    approval dialog's OK button on one AI call per edit.

    Best-effort: a point that can't be classified keeps empty levels, which
    _suggest_tag already handles by falling through to another point. A wrong
    tag is worse than no tag."""
    todo = [p for p in (points or []) if getattr(p, "_reclassify", False)]
    if not todo:
        return 0
    from ..lecture.extractor import _clean_level
    done = 0
    for i, pt in enumerate(todo, 1):
        if report:
            report(f"Re-reading {i} of {len(todo)} point(s) you rewrote…", 0)
        try:
            levels = autotag.classify(pt.concept) or {}
        except Exception as e:
            log.warn(f"bulk search: couldn't reclassify '{pt.concept[:40]}': {e}")
            levels = {}
        if levels:
            pt.system = _clean_level(levels.get("system"))
            pt.subsystem = _clean_level(levels.get("subsystem"))
            pt.topic = _clean_level(levels.get("topic"))
            done += 1
        pt._reclassify = False
    return done


def _record_points(rec: dict) -> list:
    """The point concepts on a record, whichever shape it is in.

    TWO shapes reach the copy/export path and they are not the same:

      create_record_for()      -> "points",      list[dict] with a "concept"
      gen_prompt.build_job_record -> "mass_points", list[str]

    Everything here used to read `mass_points` only. _create_payload builds the
    first shape, so the checklist came out with a heading and notes for every
    gap and NOT ONE POINT under any of them — the whole thing the export
    exists to produce. Reading both is the fix, and it is also the honest
    description of the data: two producers, one consumer.
    """
    out: list = []
    for p in (rec.get("mass_points") or rec.get("points") or []):
        if isinstance(p, dict):
            p = p.get("concept")
        p = str(p or "").strip()
        if p:
            out.append(p)
    return out


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


def _image_seed(card: dict) -> str:
    """A search phrase for one written card — its front, stripped of markup
    and cloze scaffolding.

    AI Create seeds the same box from the model's own `image_query`. A
    work-through card has none, deliberately: nobody asked the model for
    pictures, because fetching candidates for every card of a twelve-gap pass
    is twenty web round trips for illustrations you mostly don't want. So the
    question the card asks is the description of what would illustrate it."""
    seed = re.sub(r"\{\{c\d+::|::[^}]*\}\}|\}\}", " ", str(card.get("front") or ""))
    seed = re.sub(r"<[^>]+>", " ", seed)
    seed = re.sub(r"\s+", " ", seed)
    # The cloze braces leave a gap where the punctuation was, and a search box
    # reading "sodium ." looks like a typo you made.
    return re.sub(r"\s+([.,;:!?])", r"\1", seed).strip()[:80]


def _pick_card_images(parent, how: str, seed: str) -> list:
    """Image file paths for one card, by whichever of the three routes was
    asked for. The same three the AI Create review row offers, running the
    same functions underneath, so a picture chosen here is chosen the same
    way."""
    from aqt.qt import QFileDialog
    try:
        if how == "file":
            paths, _f = QFileDialog.getOpenFileNames(
                parent, "Attach images to this card", "",
                "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg);;"
                "All files (*)")
            return [p for p in (paths or []) if p]
        from .. import images
        if how == "find":
            return list(images.pick_images(parent, seed) or [])
        return list(images.browse_for_images(parent, seed) or [])
    except Exception as e:
        log.error(f"work through: image {how} failed: {e}")
        showWarning(f"That image step didn't work: {e}")
        return []


def _merged_images(existing, added) -> list:
    """One card's image list with more added — deduped, order kept. Attaching
    the same file twice is a slip, not an instruction to show it twice."""
    seen, out = set(), []
    for p in list(existing or []) + list(added or []):
        p = str(p or "")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class _ProseDelegate(QStyledItemDelegate):
    """A real text box for the two plan-table columns that hold prose.

    THE EDITOR USED TO BE A QLineEdit — Qt’s default for a QTableWidgetItem —
    and a one-line editor is not a small inconvenience for a multi-paragraph
    note. A QLineEdit cannot hold a newline at all, so double-clicking a headed
    differential folded it into one run-on line, and typing a single character
    committed that back over the original. The structure was destroyed by
    LOOKING at it, which is why the notes felt clanky and why headings kept
    going missing from them.

    So: a QPlainTextEdit. Return makes a new line (Qt’s own delegate filter
    deliberately leaves Return alone for text editors), ⌘/Ctrl+Return commits,
    Esc cancels. The editor opens at a usable height however short the row
    beneath it is — the ROW is a preview, the EDITOR is where writing happens
    — and it is nudged back up when it would open past the bottom of the view.

    sizeHint caps the row, which is the other half of the complaint: a
    1,300-character note was rendering as a 30-line row that pushed every other
    gap off the screen. Four or five lines each, the rest on hover, all of it
    in the editor.
    """

    #: about five wrapped lines. Rows are a preview, not the document.
    MAX_ROW = 76
    #: the editor always opens at least this tall, whatever the row is.
    MIN_EDITOR = 150

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self._placeholder = str(placeholder or "")

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        if sz.height() > self.MAX_ROW:
            sz.setHeight(self.MAX_ROW)
        return sz

    def createEditor(self, parent, option, index):
        ed = QPlainTextEdit(parent)
        ed.setTabChangesFocus(True)
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        if self._placeholder:
            ed.setPlaceholderText(self._placeholder)
        ed.setToolTip("Return for a new line · ⌘/Ctrl+Return to save · "
                      "Esc to cancel")
        return ed

    def setEditorData(self, editor, index):
        editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
        editor.moveCursor(QTextCursor.MoveOperation.End)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        rect = QRect(option.rect)
        if rect.height() < self.MIN_EDITOR:
            rect.setHeight(self.MIN_EDITOR)
        view = editor.parentWidget()          # the table’s viewport
        if view is not None:
            over = rect.bottom() - view.height()
            if over > 0:
                rect.moveTop(max(0, rect.top() - over))
        editor.setGeometry(rect)

    def eventFilter(self, obj, ev):
        # The view installs the delegate as the editor’s event filter, so this
        # is the hook for the one key Qt does not already handle for a text
        # edit. Everything else — Esc, Tab, focus-out — is the base class’s.
        if (ev is not None and isinstance(obj, QPlainTextEdit)
                and ev.type() == QEvent.Type.KeyPress
                and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                       | Qt.KeyboardModifier.MetaModifier))):
            self.commitData.emit(obj)
            self.closeEditor.emit(obj)
            return True
        return super().eventFilter(obj, ev)


class BulkSearchDialog(CardPaneMixin, LectureDoorMixin, QDialog):
    """Read a batch of gaps → search the deck for all of them → commit per gap."""

    # Roles on the tree items, so the commit walk can tell the three row kinds
    # apart without reparsing their labels.
    ROLE_KIND = Qt.ItemDataRole.UserRole          # "gap" | "point" | "note"
    ROLE_KEY = Qt.ItemDataRole.UserRole + 1       # kg_id | point index | nid
    ROLE_CARD = Qt.ItemDataRole.UserRole + 2      # the written card dict itself
    ROLE_CONCEPT = Qt.ItemDataRole.UserRole + 3   # a point row's concept text
    ROLE_KG = Qt.ItemDataRole.UserRole + 4        # owning gap, carried by leaves
    ROLE_TITLE = Qt.ItemDataRole.UserRole + 5     # a gap row's own heading text

    def __init__(self, kgs: list[dict], parent=None, *,
                 plans=None, matches=None, coverage=None, door: str = "",
                 title: str = "", on_committed=None,
                 source: str = "", lecture=None):
        super().__init__(parent or mw)
        # AI Lecture's door (tools/lecture/door.py). Set before anything is
        # built: half the construction asks "is this a lecture?".
        self._lec_init(lecture)
        if self._lecture is not None and on_committed is None:
            on_committed = self._lec_on_committed
        # ── the other doors ──────────────────────────────────────────────────
        # This dialog started as the work-through's own screen and is now THE
        # output page: every tool lands here (see tools/run_view.py). A door
        # that has already done some of the pipeline hands its results in
        # rather than making the user run them again.
        #
        #   plans      already-read work items  -> the READ stage is skipped
        #   matches +
        #   coverage   an already-run deck search -> FIND is skipped too, and
        #              the results page renders straight from them
        #
        # `kgs` are then FLATTENED records (WorkItem.to_kg_record), not store
        # records — a lecture has no knowledge-gap store behind it, so every
        # store lookup in here falls back to the records it was handed.
        self._door = str(door or "work-through")
        self._on_committed = on_committed
        self._seed_plans = list(plans or [])
        self._seed_matches = list(matches or [])
        self._seed_coverage = coverage
        self._seed_title = str(title or "")
        # The source path this door already knows (a lecture's filename, a
        # topic Browse was told about). Prefills the Source box; the user can
        # still change it before Apply.
        self._seed_source = str(source or "")
        self._record = pipeline.RunRecord(run_id=uuid.uuid4().hex[:12],
                                          door=self._door)
        self.setWindowTitle(title or "Find & write — knowledge gaps")
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
        # nid -> the deterministic quality check's one-line finding, or "".
        # Cached because the tree is rebuilt on every tick and the check, cheap
        # as it is, reads the note from the collection to run.
        self._flaw_cache: dict = {}
        try:
            from .. import card_creator as _cc
            self._atomicity_max = _cc._atomicity_max(tool_config("card_creator"))
        except Exception:
            self._atomicity_max = 1

        # ── the writing half of the one screen ───────────────────────────────
        # Unified mode no longer hands the uncovered points to another window:
        # it generates cards for them into the bottom of this results page, and
        # commits them together with the found cards. The JOBS are ordinary
        # create_jobs records on disk (same as Mass AI Create), so this dialog
        # polls a batch_id rather than owning threads — a crash mid-write loses
        # nothing, and the batch can be reopened from AI Create.
        self._deck = None                    # built on the plan page
        self._notetype = None
        self._gen_batch_id = ""
        # job_id -> the create_jobs record. Keyed by JOB, not by gap: a gap
        # gets a second write when you untick its found cards and ask for one,
        # and a dict keyed by gap silently dropped the first job on the floor.
        self._gen_jobs: dict = {}
        self._gen_profile: dict = {}         # the notetype profile cards commit under
        self._gen_active = False             # jobs still running
        self._gen_rendered = False           # the written cards are on screen
        self._gen_shown: set = set()         # job_ids whose cards are rendered
        self._gen_seen: dict = {}            # normalised front -> gap that has it
        # kg_id -> (what the search found, cards it chose, total points).
        # Kept beside the tree so a gap row can be relabelled from live ticks
        # while still being able to say what the search originally concluded.
        self._search_finding: dict = {}
        # kg_id -> (find, write), captured when the run starts.
        self._wants: dict = {}
        self._applied = False
        self._poll = QTimer(self)
        self._poll.setInterval(750)
        self._poll.timeout.connect(self._on_gen_poll)

        self._build()
        self._seed_record()
        if self._seed_plans:
            # Pre-planned: the door that opened this already read the material,
            # so the plan table is filled from its plans and the AI read that
            # would re-derive them is never spent.
            self._plans = list(self._seed_plans)
        self._fill_plan_table()
        if self._seed_plans:
            self._render_plan_table()
        if self._seed_coverage is not None:
            # The door already searched the deck. Render its results directly —
            # this is the same page a work-through search lands on, built from
            # someone else's matches.
            self._matches = list(self._seed_matches)
            self._coverage = self._seed_coverage
            self._run_plans = list(self._seed_plans)
            self._wants = {p.kg_id: (True, True) for p in self._run_plans}
            QTimer.singleShot(0, self._render_results)
        elif self._lecture is not None:
            self._lec_setup_ui()
        elif provider_configured() and not is_manual_provider():
            self._set_status(
                "Nothing has been sent to the AI yet. Edit any gap's notes "
                "first — they're the biggest thing the read goes on — then "
                "press Read gaps with AI.")
        theme_dialog(self)

    # ── the stage strip ──────────────────────────────────────────────────────

    def _seed_record(self) -> None:
        """The run record the strip renders, stamped with what this door has
        already done before the window even opened."""
        r = self._record
        r.mark(pipeline.TAKE_IN, pipeline.DONE, f"{len(self._kgs)} item(s)")
        if self._seed_plans:
            pts = sum(len(getattr(p, "points", []) or []) for p in self._seed_plans)
            r.mark(pipeline.READ, pipeline.SKIPPED,
                   f"{pts} point(s) — read already")
            r.mark(pipeline.APPROVE, pipeline.SKIPPED)
        if self._seed_coverage is not None:
            cov = self._seed_coverage
            r.mark(pipeline.FIND, pipeline.DONE,
                   f"{len(getattr(cov, 'gaps', []) or [])} gap(s)")
        self._sync_strip()

    def _sync_strip(self) -> None:
        strip = getattr(self, "_strip", None)
        if strip is not None:
            strip.set_record(self._record)

    def _stage(self, key: str, state: str, count: str = "",
               error: str = "") -> None:
        """Move one stage and repaint the strip. Every state change in this
        dialog goes through here, so the strip can never drift from the run."""
        self._record.mark(key, state, count, error)
        self._sync_strip()

    def _record_for(self, kg_id: str) -> dict:
        """The record for a work item — the ones this dialog was HANDED first,
        the knowledge-gap store second.

        The order matters: a door with no store behind it (AI Lecture) is only
        reachable at all because the handed-in records win. It also means an
        edit made on the plan page is what the write stage sees, rather than
        whatever the store last persisted."""
        kg_id = str(kg_id or "")
        for k in self._kgs:
            if str(k.get("id") or k.get("kg_id") or "") == kg_id:
                return k
        try:
            return kg_store.get(kg_id) or {}
        except Exception:
            return {}

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # The stage strip: always the same eight boxes whatever came in the
        # door, including the ones this run skipped. See tools/run_view.py.
        from ..run_view import StageStrip
        self._strip = StageStrip()
        root.addWidget(self._strip)
        self._sync_strip()

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

        # The escape hatch from the default, not a rival to it. Apply now sends
        # the written cards through Anki's Add window one at a time, which is
        # right when you want a last look at each — and wrong when you have
        # twenty you already trust. Only ever visible when there are written
        # cards to skip the review OF; the found half is unaffected either way.
        self._add_now_btn = QPushButton("Add all now")
        self._add_now_btn.setMinimumHeight(32)
        self._add_now_btn.setVisible(False)
        self._add_now_btn.setToolTip(
            "Add the written cards straight into your collection, without "
            "opening Anki's Add window for each one.\n\n"
            "Same cards, same tags, same deck as reviewing them — and one "
            "Ctrl+Z takes the whole pass back out, which stops being true "
            "once Anki's own Add window has done the adding.")
        self._add_now_btn.clicked.connect(
            lambda: self._on_apply_everything(review=False))
        btn_row.addWidget(self._add_now_btn)
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

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ["Gap", "Find", "Write", "Your notes", "What the AI read it as",
             "Scope", "Cards", "Pts"])
        hdr = self._table.horizontalHeaderItem(C_FIND)
        hdr.setToolTip("Search your deck for cards that already cover this gap.")
        hdr = self._table.horizontalHeaderItem(C_WRITE)
        hdr.setToolTip(
            "Write cards for this gap. With Find on, only for the points the "
            "search turned up nothing for. With Find off, for every point — "
            "nothing measured them, so nothing can be assumed covered.")
        # What a COLUMN means belongs on its header, not repeated down every
        # row of it — see tips_on_checkbox_only, which is what makes the rows
        # themselves quiet.
        for col_i, tip in (
            (C_NOTES, "This gap's notes, editable in place. Saved back onto the "
                      "gap before the AI reads it, so what you type here shapes "
                      "the run."),
            (C_READ, "How the AI understood each gap — overwrite it to say "
                     "what the gap is really asking for.\n\n"
                     "Your wording is saved onto the gap and the AI reads it "
                     "again from that, so the points, the search and the cards "
                     "all come from what you typed. It sticks: every later run "
                     "reads the gap your way, not the title's.\n\n"
                     "Clear the cell to go back to reading it from the title. "
                     "To change the SEARCH TERMS a point matches on rather than "
                     "the subject itself, use “Review points…”."),
            (C_CARDS, "How many cards this gap should end up with — the cards "
                      "found in your deck AND the ones written for what it "
                      "doesn't have, together. Starts at one per point. "
                      "Changing the scope resets it to that scope's default."),
            (C_PTS, "How many searchable points a gap was split into. A broad "
                    "gap splits into several — that's what actually finds more "
                    "cards, since one point only matches cards about one thing."),
        ):
            h = self._table.horizontalHeaderItem(col_i)
            if h is not None:
                h.setToolTip(tip)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setWordWrap(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(C_GAP, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_NOTES, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_READ, QHeaderView.ResizeMode.Stretch)
        for c in (C_FIND, C_WRITE, C_SCOPE, C_CARDS, C_PTS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(C_GAP, 220)
        # The two columns that hold prose get a real text box to edit in, and a
        # capped row height so one long note can't own the screen. See
        # _ProseDelegate — both halves of "the text boxes are clanky".
        self._notes_delegate = _ProseDelegate(
            "What you already know, what keeps catching you out, what you want "
            "cards on. Several lines are fine.", self._table)
        self._table.setItemDelegateForColumn(C_NOTES, self._notes_delegate)
        self._read_delegate = _ProseDelegate(
            "Say what this gap is actually asking for, in your own words.",
            self._table)
        self._table.setItemDelegateForColumn(C_READ, self._read_delegate)
        self._table.itemChanged.connect(self._on_table_item_changed)
        tips_on_checkbox_only(self._table)
        col.addWidget(self._table, 1)

        self._table_hint = QLabel(
            "Untick any gap to skip it. <b>Your notes are editable here</b> and "
            "are saved back to the gap — they're the main thing the AI reads, so "
            "this is the place to fix a gap it's about to misunderstand. After "
            "reading, edit the reading or the card count on any row too. Rows "
            "marked ⚠ are ones the AI wasn't sure it understood.<br>"
            "Double-click a note or a reading for a proper text box: "
            "<b>Return</b> for a new line, <b>⌘/Ctrl+Return</b> to save, "
            "<b>Esc</b> to cancel. Rows show the first few lines only — hover "
            "for the whole thing."
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
        # WHETHER to write is the Write column, per gap — untick it everywhere
        # to just search. There used to be a second, global "Write cards…"
        # box here too, and the two disagreed: a run with Find off wrote
        # whatever the box said, and unticking it shipped the leftovers to a
        # second window. This strip only says where written cards go.
        strip = QHBoxLayout()
        strip.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Written cards go into deck")
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

        # The two AI Create switches that change what a written card IS, per
        # run, here — before the writing starts on its own. Their defaults
        # are AI Create's (Settings → Create, and the notetype's grounding
        # override), and the cards show what they produced in the card panel.
        opts = QHBoxLayout()
        opts.setContentsMargins(0, 0, 0, 0)
        self._ground_cb = QCheckBox("📚 Ground in guidelines")
        self._ground_cb.setStyleSheet("font-size: 11px;")
        self._ground_cb.setToolTip(
            "Give the writer the Australian/WA clinical-guideline allow-list, "
            "so each card cites a real source instead of an invented one. The "
            "citation shows in the card panel on the results screen.\n\n"
            "Starts from Settings → Create and this notetype's Grounding "
            "override.")
        opts.addWidget(self._ground_cb)
        self._autoimg_cb = QCheckBox("🖼️ Auto-image")
        self._autoimg_cb.setStyleSheet("font-size: 11px;")
        self._autoimg_cb.setChecked(bool(
            (cc_cfg.get("images") or {}).get("auto_find", False)))
        self._autoimg_cb.setToolTip(
            "Let the writer flag the cards a real picture would help (derm, "
            "radiology, anatomy, histology, ECG…). On the results screen, "
            "click one of those cards and its suggested images are there — "
            "one click puts one on the card.\n\n"
            "Starts from Settings → Create → Images.")
        opts.addWidget(self._autoimg_cb)
        opts.addStretch(1)
        outer.addLayout(opts)

        def _sync_ground(*_a) -> None:
            try:
                from .. import card_creator as cc
                prof = cc._resolved_profile(cc_cfg,
                                            self._notetype.currentText().strip())
                self._ground_cb.setChecked(
                    cc._grounding_active(cc_cfg, prof, "topic"))
            except Exception as e:
                log.warn(f"work through: grounding default failed: {e}")
        _sync_ground()
        self._notetype.currentIndexChanged.connect(_sync_ground)
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

        # QT TO ACT, HTML TO READ — both on this one page, swapped in place.
        # The tree is where you tick, edit, rewrite and split; that needs real
        # widgets and always has. The summary is where you read what the run
        # concluded, and prose in a document beats prose crammed into tree row
        # labels. Neither is a second window.
        self._results_stack = QStackedWidget()
        self._results_stack.addWidget(self._build_found_pane())   # 0 — act
        self._summary_view = AnkiWebView()
        self._results_stack.addWidget(self._summary_view)         # 1 — read
        col.addWidget(self._results_stack, 1)

        send_row = QHBoxLayout()
        # THERE IS NO SOURCE BOX. It was an editable field here, prefilled and
        # remembered per door — and that memory is precisely what put
        # Sources::Murtaghs::other onto batches with nothing to do with
        # Murtagh's: a path typed once for one loose batch silently became the
        # default for every run afterwards, in a box nobody reads twice.
        #
        # Provenance for a work-through is DERIVED, never typed. A lecture
        # brings its filename, a group carries its own source (said once, in
        # the group, where it belongs), and a gap type maps to one. When none
        # of those says anything the run has NO source, which is the right
        # answer for course work — the course branch already records where it
        # came from. Typing it here was asking the same question a third time.
        #
        # This label reports what will be applied, and hides when there is
        # nothing to report. Read-only on purpose: a run is not the place to
        # invent provenance, because whatever you invent here is gone the
        # moment the window closes.
        self._source_lbl = QLabel("")
        self._source_lbl.setStyleSheet("font-size: 11px; opacity: .8;")
        self._source_lbl.setVisible(False)
        send_row.addWidget(self._source_lbl)
        self._refresh_source_label()
        self._summary_btn = QPushButton("\U0001F4C4  Read as a summary")
        self._summary_btn.setAutoDefault(False)
        self._summary_btn.setCheckable(True)
        self._summary_btn.setToolTip(
            "The same run, written out as a document — what your deck "
            "covered, what it didn't, and what was written for the difference. "
            "Ticking is done in the tree; this is for reading.")
        self._summary_btn.toggled.connect(self._on_summary_toggled)
        send_row.addWidget(self._summary_btn)
        send_row.addStretch(1)
        col.addLayout(send_row)
        return w

    def _on_summary_toggled(self, on: bool) -> None:
        if on:
            try:
                self._summary_view.stdHtml(self._summary_html())
            except Exception as e:
                log.error(f"run view: couldn't render the summary: {e}")
                self._summary_btn.setChecked(False)
                return
            self._summary_btn.setText("◀  Back to the cards")
        else:
            self._summary_btn.setText("\U0001F4C4  Read as a summary")
        self._results_stack.setCurrentIndex(1 if on else 0)
        # The tick boxes live in the tree, so acting on the run means going
        # back to it. Say so rather than leaving a dead Apply button.
        self._commit_btn.setEnabled(not on and not self._applied)
        self._add_now_btn.setEnabled(not on and not self._applied)

    def _summary_html(self) -> str:
        """This run as a document. Reads the tree, so it always says what is
        ticked NOW rather than what the search originally concluded."""
        from ..run_view import summary_html
        return summary_html(self._record, self._run_plans, self._coverage,
                            ticked=self._ticked(),
                            written=[c for _leaf, c in self._written_items()],
                            approved=[c for c in self._approved_cards_flat()])

    def _approved_cards_flat(self) -> list:
        return [c for _k, cards in self._approved_written() for c in cards]

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
        self._tree.headerItem().setToolTip(1,
            "Applied to every ticked card under a gap. Prefilled from the gap's "
            "own system/subsystem/topic, in the same scheme written cards "
            "use. Edit or clear it freely.")
        # A card's full text is still a hover away — over its checkbox. Double-
        # click opens the note in the browser, which is the better read anyway.
        tips_on_checkbox_only(self._tree)
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        # The card panel (card_pane.py): whichever row is selected, in full —
        # the rows only hold a line. A lecture adds its slides and Lecture
        # Notes to the same panel.
        from aqt.qt import QSplitter
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._tree)
        split.addWidget(self._build_card_pane())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([700, 380])
        self._tree.currentItemChanged.connect(self._pane_show)
        col.addWidget(split, 1)

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
        ctl.addSpacing(14)
        # Always there, whatever the provider is — the whole point of this
        # button is the people who don't generate cards in here at all.
        self._copy_btn = QPushButton("⧉ Copy uncovered points…")
        self._copy_btn.setAutoDefault(False)
        self._copy_btn.setToolTip(
            "Take the points your deck turned out NOT to cover somewhere "
            "else — your own card maker, a chatbot, or back into Things. "
            "That list cost a search and a relevance pass to produce.")
        self._copy_btn.clicked.connect(self._copy_menu)
        ctl.addWidget(self._copy_btn)
        self._lec_more_btn = QPushButton("Lecture ▾")
        self._lec_more_btn.setAutoDefault(False)
        self._lec_more_btn.setToolTip(
            "Search again for the points your deck didn't cover, or save "
            "them with their slide numbers.")
        self._lec_more_btn.clicked.connect(self._lec_more_menu)
        self._lec_more_btn.setVisible(False)
        ctl.addWidget(self._lec_more_btn)
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

    def _tick_cell(self, on: bool, tip: str) -> QTableWidgetItem:
        """A bare checkbox cell. No text: the column header names it, and a
        repeated word down eighty rows is just noise beside the box."""
        it = QTableWidgetItem("")
        it.setFlags((it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    & ~Qt.ItemFlag.ItemIsEditable)
        it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        it.setToolTip(tip)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return it

    def _row_wants(self, row: int) -> tuple:
        """(find, write) for one row, read off the table."""
        f = self._table.item(row, C_FIND)
        w = self._table.item(row, C_WRITE)
        return (f is not None and f.checkState() == Qt.CheckState.Checked,
                w is not None and w.checkState() == Qt.CheckState.Checked)

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
            self._table.setItem(row, C_FIND, self._tick_cell(
                True, "Search your deck for cards covering this gap."))
            self._table.setItem(row, C_WRITE, self._tick_cell(
                True,
                "Write cards for this gap. Only for what the search doesn't "
                "cover, unless Find is off — then for everything, because "
                "nothing measured it."))
            self._table.setItem(row, C_NOTES, self._notes_cell(kg))
            hint = QTableWidgetItem("— not read yet —")
            hint.setFlags(hint.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, C_READ, hint)
            for c in (C_SCOPE, C_CARDS, C_PTS):
                blank = QTableWidgetItem("")
                blank.setFlags(blank.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, c, blank)
        self._table.blockSignals(False)
        self._fit_rows()
        self._refresh_title()

    def _fit_rows(self, row: int | None = None) -> None:
        """Size the plan rows to their content, then CLAMP them.

        resizeRowsToContents on its own takes the tallest cell in the row, and
        with word wrap on a stretched column a long note is a thirty-line cell.
        One gap then fills the screen and the rest of the batch is below the
        fold — on the screen whose entire job is comparing the batch. The
        delegate caps its own two columns; this caps the row, so nothing else
        can quietly reintroduce the problem.

        The text is not lost: the hover carries all of it and the editor opens
        on all of it. A row is a preview."""
        cap = _ProseDelegate.MAX_ROW
        rows = ([row] if row is not None else range(self._table.rowCount()))
        if row is None:
            self._table.resizeRowsToContents()
        else:
            self._table.resizeRowToContents(row)
        for r in rows:
            if self._table.rowHeight(r) > cap:
                self._table.setRowHeight(r, cap)

    def _notes_cell(self, kg: dict) -> QTableWidgetItem:
        """This gap's notes, editable in place.

        Notes are the single biggest input to how the AI reads a gap — a
        two-word title plus "the vascular ones keep tripping me up" is a
        completely different search from the title alone. They were readable
        only as a hover tooltip, on the one screen where you are deciding what
        to run, which is exactly where you notice they're wrong.

        The row shows the first few lines (the delegate caps it) and the
        tooltip carries the rest, so a long note is readable without being
        allowed to push every other gap off the screen."""
        text = kg_store.field(kg, "notes") or ""
        it = QTableWidgetItem(text)
        it.setToolTip(text or "No notes on this gap yet — double-click to add "
                              "some. They are the main thing the AI reads.")
        return it

    def _refresh_title(self) -> None:
        if self._lecture is not None:
            self._title.setText(self._lec_title())
            return
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
        if item.column() in (C_NOTES, C_READ):
            # The row only ever shows the first few lines, so the hover has to
            # carry what was just typed or the rest of it is invisible until
            # the box is opened again.
            self._table.blockSignals(True)
            try:
                item.setToolTip(item.text())
            finally:
                self._table.blockSignals(False)
            self._fit_rows(item.row())
            if item.column() == C_NOTES:
                return
        if item.column() not in (C_GAP, C_FIND, C_WRITE):
            return
        row = item.row()
        if item.column() in (C_FIND, C_WRITE):
            # Ticking either puts the gap back in the run; unticking both takes
            # it out. The title tick stays the master switch.
            find, write = self._row_wants(row)
            gap = self._table.item(row, C_GAP)
            if gap is not None:
                want = Qt.CheckState.Checked if (find or write) \
                    else Qt.CheckState.Unchecked
                if gap.checkState() != want:
                    self._table.blockSignals(True)
                    gap.setCheckState(want)
                    self._table.blockSignals(False)
        elif 0 <= row < len(self._plans) or True:
            # The master switch drives both, so unticking a gap can't leave it
            # half in the run.
            on = item.checkState() == Qt.CheckState.Checked
            self._table.blockSignals(True)
            for c in (C_FIND, C_WRITE):
                cellw = self._table.item(row, c)
                if cellw is not None and not on:
                    cellw.setCheckState(Qt.CheckState.Unchecked)
                elif cellw is not None and on and c == C_FIND:
                    cellw.setCheckState(Qt.CheckState.Checked)
            self._table.blockSignals(False)
        if 0 <= row < len(self._plans):
            gap = self._table.item(row, C_GAP)
            self._plans[row].included = (
                gap is not None and gap.checkState() == Qt.CheckState.Checked)
        self._refresh_buttons()
        self._refresh_title()

    def _sync_notes(self) -> int:
        """Write edited notes back onto the gaps. Returns how many changed.

        Runs BEFORE the read and before the search, not at the end: the whole
        point is that the AI sees what you just typed. Writing to the store as
        well as to the in-memory record means the edit survives the run, so you
        fix a badly-worded gap once rather than every time you look at it."""
        if self._lecture is not None:
            return 0          # a lecture's sections have no store behind them
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

    def _sync_reading(self) -> list:
        """Persist a rewritten reading onto its gap. Returns the rows changed.

        The reading cell is the student saying "no, this gap is asking THIS".
        It used to be folded into the plan's `concept` and nothing else, and
        `concept` is only ever a heading on the generation prompt — so the edit
        changed the label on the work and not the work. Saved here as the gap's
        `read_as`, it goes into the decompose prompt as the subject
        (triage._kg_prompt_line), which re-reads the gap from the student's
        wording, invalidates that gap's cached reading by construction, and
        survives to every later run.
        """
        if self._lecture is not None:
            return []         # read-only on a lecture: the points are the reading
        rewritten: list = []
        for row, p in enumerate(self._plans):
            if row >= len(self._kgs):
                continue
            cell = self._table.item(row, C_READ)
            if cell is None:
                continue
            typed = cell.text().strip()
            if typed == "— not read —":
                typed = ""
            if typed == str(getattr(p, "_read_shown", "") or "").strip():
                continue                      # untouched
            kg = self._kgs[row]
            if typed == str(kg_store.field(kg, "read_as") or "").strip():
                continue                      # already saved, nothing new
            fields = dict(kg.get("fields") or {})
            if typed:
                fields["read_as"] = typed
            else:
                fields.pop("read_as", None)   # cleared: back to the title
            kg["fields"] = fields
            try:
                updated = kg_store.update(str(kg.get("id") or ""), fields=fields)
                if updated:
                    self._kgs[row] = updated
            except Exception as e:
                log.warn(f"bulk search: couldn't save the reading for "
                         f"{kg.get('id')}: {e}")
            p._read_shown = typed
            rewritten.append(row)
        if rewritten:
            try:
                from .. import knowledge_gaps
                knowledge_gaps._refresh_open_panel()
            except Exception:
                pass
        return rewritten

    def _active_plans(self) -> list:
        """Every plan in the run at all — findable or writable."""
        return [p for p in self._plans if p.ok and p.included]

    def _search_plans(self) -> list:
        """The plans to actually SEARCH. A gap with Write on and Find off is in
        the run but never searched: you already know your deck doesn't have
        it, and paying for the search to be told so is the thing this column
        exists to skip."""
        wants = self._wants_by_kg()
        return [p for p in self._active_plans()
                if wants.get(p.kg_id, (True, False))[0]]

    def _wants_by_kg(self) -> dict:
        out = {}
        for row in range(self._table.rowCount()):
            if row < len(self._plans):
                out[self._plans[row].kg_id] = self._row_wants(row)
        return out

    def _refresh_buttons(self) -> None:
        if self._lecture is not None:
            # A lecture reads as a whole, and the copy/paste provider can read
            # one through the file hand-off.
            can_read = not self._busy and (provider_configured()
                                           or is_manual_provider())
        else:
            can_read = (bool(self._checked_rows()) and not self._busy
                        and provider_configured() and not is_manual_provider())
        self._read_btn.setEnabled(can_read)
        has_points = any(p.points for p in self._active_plans())
        self._search_btn.setEnabled(bool(self._active_plans()) and not self._busy)
        self._lp_btn.setEnabled(has_points and not self._busy)

    # ── stage 1: the read ────────────────────────────────────────────────────

    def _on_read(self, *_a, then=None) -> None:
        if self._lecture is not None:
            self._lec_read()
            return
        rows = self._checked_rows()
        if not rows:
            tooltip("Tick at least one gap first.")
            return
        # Notes and rewritten readings first: they are the two biggest inputs to
        # how each gap is read, and an edit sitting in a cell that hasn't been
        # committed yet would be invisible to the call that's about to run.
        saved = self._sync_notes()
        reworded = self._sync_reading()
        # What runs when the read lands. Used by Find, which re-reads any gap
        # whose reading was rewritten before it searches — the rewrite is a new
        # subject, and searching the old points would search the wrong thing.
        self._after_read = then
        kgs = [self._kgs[r] for r in rows]
        self._set_busy(True, (f"Saved notes on {saved} gap(s). " if saved else "")
                       + (f"Re-reading {len(reworded)} gap(s) from your wording. "
                          if reworded else "")
                       + f"Reading {len(kgs)} gap(s) with AI…")
        model = pipeline.dial_for(pipeline.READ)
        self._stage(pipeline.READ, pipeline.RUNNING)

        # Minutes, still: the read is one batch-wide sizing call plus a fan of
        # parallel decompose calls (triage._read_split), and it reports its own
        # progress into the heartbeat's base line. Without a running clock even
        # a progressing label reads as a hang.
        from ..lecture.panel import _Heartbeat

        def _report(msg, _pct=0):
            mw.taskman.run_on_main(lambda m=msg: self._set_status(m))

        def _bg():
            with _Heartbeat(_report, f"Reading {len(kgs)} gap(s) with AI…", 0) as hb:
                return kg_triage.triage(kgs, model=model, report=hb.set_base)

        mw.taskman.run_in_background(_bg, self._on_read_done, uses_collection=False)

    def _on_read_done(self, fut) -> None:
        if self._closed:
            return
        # Taken now, not at the end: a read that fails or is stopped must not
        # leave a continuation armed for whatever runs next.
        then, self._after_read = getattr(self, "_after_read", None), None
        self._set_busy(False)
        self._resurface()
        try:
            plans = fut.result()
        except core_api.ClaudeCancelled:
            self._stage(pipeline.READ, pipeline.PENDING)
            self._status.setText("Stopped.")
            return
        except Exception as e:
            import traceback
            self._stage(pipeline.READ, pipeline.FAILED, error=str(e))
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
        self._stage(pipeline.READ, pipeline.DONE,
                    f"{sum(len(p.points) for p in self._plans)} point(s)")
        self._render_plan_table()
        if then is not None:
            then()
            return
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
            # The read RE-RENDERS the table, so the Find/Write ticks have to be
            # carried across it — otherwise reading the gaps silently resets
            # every choice made before pressing it.
            want_find, want_write = self._row_wants(row)
            self._table.setItem(row, C_FIND, self._tick_cell(
                want_find, "Search your deck for cards covering this gap."))
            self._table.setItem(row, C_WRITE, self._tick_cell(
                want_write,
                "Write cards for this gap. Only for what the search doesn't "
                "cover, unless Find is off — then for everything."))
            # Rebuilt from what's ON SCREEN, not from the store: the read
            # happens after _sync_notes, so the cell is already authoritative
            # and re-reading the record would undo an edit made mid-run.
            keep = self._table.item(row, C_NOTES)
            kept = (keep.text() if keep is not None else
                    (kg_store.field(self._kgs[row], "notes")
                     if row < len(self._kgs) else ""))
            note_cell = QTableWidgetItem(kept)
            # Same as _notes_cell: the row is capped at a few lines, so the
            # whole note has to be on the hover or it is invisible here.
            note_cell.setToolTip(kept or "No notes on this gap yet — "
                                         "double-click to add some.")
            self._table.setItem(row, C_NOTES, note_cell)

            shown = "— not read —" if unread else (p.concept or "")
            reading = QTableWidgetItem(shown)
            reading.setToolTip(shown)
            if unread:
                reading.setFlags(reading.flags() & ~Qt.ItemFlag.ItemIsEditable)
            # A gap you have already reworded says so, in words: the cell shows
            # the AI's reading of YOUR wording, which looks identical to its
            # reading of the title. Without this there is no way to tell that
            # the gap is pinned, or to know there is something to clear.
            own = (str(kg_store.field(self._kgs[row], "read_as") or "").strip()
                   if row < len(self._kgs) else "")
            if own:
                reading.setToolTip(
                    "Read from YOUR wording, not the title:\n\n  " + own
                    + "\n\nOverwrite this cell to change it, or clear it to go "
                      "back to the title.")
            self._table.setItem(row, C_READ, reading)
            # What the AI put here, kept so an EDIT can be told from the
            # untouched cell. Without it the only way to spot a rewrite was to
            # compare against the stored one, which meant re-reading a gap
            # pinned the model's own words as though the student had typed
            # them.
            p._read_shown = shown

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
            cards.valueChanged.connect(lambda _v, r=row: self._on_cards_changed(r))
            self._table.setCellWidget(row, C_CARDS, cards)

            pts = QTableWidgetItem(str(len(p.points)))
            pts.setFlags(pts.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, C_PTS, pts)
        self._table.blockSignals(False)
        self._fit_rows()

        if self._lecture is not None:
            self._lec_fix_table()
            self._refresh_buttons()
            self._refresh_title()
            return

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
            # The reading, as the run's heading. The SUBSTANCE of an edit here
            # is handled by _sync_reading, which saves it onto the gap and has
            # it read again — this only keeps the label on screen matching.
            it1 = self._table.item(row, C_READ)
            if it1 is not None and p.ok and self._lecture is None:
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
        # A rewritten reading is a different subject, and the points hanging off
        # the row were decomposed from the old one. Searching them would search
        # what the student just said this gap ISN'T about, so the gap is read
        # again first and the search runs on what comes back. Every untouched
        # gap in the batch is a cache hit, so the extra leg costs one call.
        if self._sync_reading():
            self._set_status("Your wording changed — re-reading those gaps "
                             "before searching.")
            self._on_read(then=self._on_search)
            return
        self._sync_plans()
        # Remembered now, before the run: the table is re-rendered as results
        # land, and the write half needs to know what was asked for.
        self._wants = self._wants_by_kg()
        plans = self._search_plans()
        write_only = [p for p in self._active_plans() if p not in plans]
        if not plans and not write_only:
            tooltip("Nothing ticked — untick both boxes on every row and "
                    "there's nothing to do.")
            return
        if not plans:
            # Every gap is Write-only. Nothing to search, so go straight to
            # writing rather than reporting an empty search as a failure.
            self._run_plans = list(write_only)
            self._refresh_source_label()
            self._skip_to_writing(write_only)
            return
        points = kg_triage.points_of(plans)
        if not points:
            showWarning("None of those gaps produced anything searchable.")
            return
        self._run_plans = list(plans) + list(write_only)

        import threading
        from ..lecture.panel import run_pipeline

        extra: dict = {}
        if self._lecture is not None:
            if is_manual_provider():
                # No AI provider: the lecture's own file hand-off, round two.
                self._stage(pipeline.APPROVE, pipeline.DONE)
                self._stage(pipeline.FIND, pipeline.RUNNING)
                self._lec_fm_find(points)
                return
            # The lecture's own budget and depth, not per-gap shares: a lecture
            # searched here finds the cards it found on its own page.
            cfg, extra = self._lec_search_args(plans)
            cap_for = cand_cap_for = None
        else:
            cfg = dict(tool_config("lecture"))
            cfg["lo_text"] = ""                   # never the LO branch: points win
            cfg["target_cards"] = kg_triage.total_cards(plans)
            # Depth no longer decides the per-point cap here (cap_for does), but
            # it still sizes the candidate POOL the judge reads. Use the deepest
            # preset so a broad gap has enough candidates to draw a real
            # cluster from.
            cfg["depth"] = "thorough"
            cap_for = kg_triage.cap_for(plans)
            # ...but a narrow gap's point still gets a SMALL pool: the judge
            # reads every candidate, and that read is the slow leg of the run.
            cand_cap_for = kg_triage.cand_cap_for(plans)

        self._cancel_ev = threading.Event()
        cancel_ev = self._cancel_ev
        self._stage(pipeline.APPROVE, pipeline.DONE)
        self._stage(pipeline.FIND, pipeline.RUNNING)
        self._set_busy(True, "Starting…", progress=True, stoppable=True)

        def _report(msg, pct):
            def _ui():
                if self._set_status(msg):
                    self._progress.setValue(int(pct))
            mw.taskman.run_on_main(_ui)

        def _bg():
            core_api.set_cancel_event(cancel_ev)
            try:
                # Points you rewrote in the approval dialog are reclassified
                # first, so the cards this search finds file under what the
                # point now says rather than what it used to.
                reclassify_points(points, report=_report)
                return run_pipeline(None, cfg, report=_report, cancel_ev=cancel_ev,
                                    points=points, cap_for=cap_for,
                                    cand_cap_for=cand_cap_for, **extra)
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
            self._stage(pipeline.FIND, pipeline.PENDING)
            self._status.setText("Stopped.")
            return
        except Exception as e:
            import traceback
            self._stage(pipeline.FIND, pipeline.FAILED, error=str(e))
            log.error(f"bulk search: run failed: {e}\n{traceback.format_exc()}")
            self._status.setText("")
            showWarning(f"Bulk search failed:\n\n{e}")
            return
        self._matches, self._coverage = matches, coverage
        self._stage(pipeline.FIND, pipeline.DONE,
                    f"{len(getattr(coverage, 'gaps', []) or [])} gap(s)")
        self._render_results()

    # ── stage 3: results, grouped by gap ─────────────────────────────────────

    def _skip_to_writing(self, plans: list) -> None:
        """Every gap is Write-only, so there is no search to render.

        The results tree is still the surface — the written cards belong under
        their gap and their points exactly as they would after a search. It is
        just built from the plan rather than from matches, with each point
        marked as unmeasured rather than as a gap, because nothing looked."""
        self._matches, self._coverage = [], None
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tag_edits = {}
        for plan in plans:
            top = QTreeWidgetItem(self._tree)
            top.setData(0, self.ROLE_KIND, "gap")
            top.setData(0, self.ROLE_KEY, plan.kg_id)
            top.setData(0, self.ROLE_TITLE, plan.title)
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsAutoTristate
                         | Qt.ItemFlag.ItemIsUserCheckable)
            top.setCheckState(0, Qt.CheckState.Unchecked)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self._search_finding[plan.kg_id] = ("not searched", 0,
                                                len(plan.points))
            tag_edit = QLineEdit()
            tag_edit.setPlaceholderText("no tag — this gap's cards won't be tagged")
            tag_edit.setText(plan.tags[0] if plan.tags else "")
            attach_tag_completer(tag_edit, multi=False)
            self._tree.setItemWidget(top, 1, tag_edit)
            self._tag_edits[plan.kg_id] = tag_edit
            for pi, pt in enumerate(plan.points):
                node = QTreeWidgetItem(top)
                node.setData(0, self.ROLE_KIND, "point")
                node.setData(0, self.ROLE_KEY, pi)
                node.setData(0, self.ROLE_CONCEPT, pt.concept)
                node.setData(0, self.ROLE_KG, plan.kg_id)
                node.setFlags(node.flags() | Qt.ItemFlag.ItemIsAutoTristate
                              | Qt.ItemFlag.ItemIsUserCheckable)
                node.setCheckState(0, Qt.CheckState.Unchecked)
                node.setText(0, _short(pt.concept, 96)
                             + "   — not searched; a card is being written")
                node.setExpanded(True)
            top.setExpanded(True)
        self._tree.blockSignals(False)
        self._relabel_gaps()
        self._stack.setCurrentIndex(1)
        self._back_btn.setVisible(True)
        self._read_btn.setVisible(False)
        self._search_btn.setVisible(False)
        self._commit_btn.setVisible(True)
        self._title.setText(f"<b>{len(plans)} gap(s)</b> · writing without "
                            "searching")
        self._gap_hint.setText(
            "Find was unticked on every gap, so your deck wasn't searched. "
            "Nothing is claimed to be covered — cards are being written for "
            "every point.")
        self._update_selection_count()
        QTimer.singleShot(0, self._start_generation)

    def _render_results(self) -> None:
        pass

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
            # _relabel_gaps rebuilds this row's heading from the title, so the
            # heading can say what is TICKED now rather than what the search
            # found earlier. Without it the row is skipped and the count
            # freezes at whatever the search reported.
            top.setData(0, self.ROLE_TITLE, plan.title)
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
                    self._make_found_leaf(node, m, nid, chosen=nid in chosen)
                if not chosen and not extra:
                    empty = QTreeWidgetItem(node)
                    empty.setData(0, self.ROLE_KIND, "info")
                    empty.setText(0, "nothing in your deck matched this point's "
                                     "search terms")
                    empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

                node.setExpanded(bool(chosen))
            top.setExpanded(True)

        self._tree.blockSignals(False)
        self._relabel_gaps()

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
        self._refresh_source_label()
        # Start writing straight away, on this same screen. Deferred one tick so
        # the found cards paint first: they are readable for the whole time the
        # rest is being generated, which is the point of not making this a page.
        if self._lecture is not None:
            self._lec_after_results()
        if not self._gen_batch_id:
            QTimer.singleShot(0, self._start_generation)

    def _make_found_leaf(self, node, m, nid: int, *, chosen: bool):
        """One found card's row under its point. Shared by the results render
        and anything that adds cards to a point afterwards (a lecture's
        re-search), so a card reads the same however it got there."""
        from ..lecture import matcher
        leaf = QTreeWidgetItem(node)
        leaf.setData(0, self.ROLE_KIND, "note")
        leaf.setData(0, self.ROLE_KEY, int(nid))
        leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        leaf.setCheckState(0, Qt.CheckState.Checked if chosen
                           else Qt.CheckState.Unchecked)
        susp, tot = m.susp.get(nid, (0, 0))
        # Words, not colour: a suspended card is the one there's something to
        # DO about, and that has to read as text.
        mark = ("⏸ suspended  " if susp else "in rotation  ") if tot else ""
        # Show as much of the card as the row can hold, and keep the rest in
        # the tooltip: deciding whether a card belongs to a gap is unreadable
        # off a 150-character stub.
        full = matcher.note_text(nid, 700)
        # Stage 6 CHECK, on a card you already own. Local, free and instant
        # (see pipeline.check_found) — no AI call — so a found card is held to
        # the same standard as a written one before it's unsuspended.
        flaw = self._found_flaw(nid)
        # A lecture also flags a chosen card that doesn't really answer its
        # point. Flagged, never hidden.
        off = self._lec_leaf_flag(m, nid, chosen) if self._lecture else ""
        flags = [f for f in (flaw, off) if f]
        leaf.setText(0, f"{mark}{_short(full, 260)}"
                        + "".join(f"   [{f}]" for f in flags))
        leaf.setToolTip(0, f"{full}\n\nDouble-click to open this note in the "
                           "Anki browser."
                        + (f"\n\nQuality check: {flaw}" if flaw else "")
                        + (f"\n\n{off[0].upper() + off[1:]}: it matched none of "
                           "the point's search terms and the judge rated it "
                           "tangential." if off else ""))
        return leaf

    def _course_tag(self, group: str | None = None) -> str:
        """The branch shared by every card in a gap's group, or "".

        An absolute group ("SRS 5" -> !!Curtanki::Year_3::SRS::05) is a COURSE
        tag, not a content tag. It used to be returned in place of the content
        tag, so a gap worked inside a case ended up with no !!Fleg::KG:: tag at
        all and the content tree had a hole exactly where the course work was.
        Both are applied now: the content tag per gap, the branch beside it.

        PER GAP, not once for the run, and via group_branch() rather than
        `absolute`. Three things were wrong with asking the run: a relative
        group has a branch too and got none, so the cards WRITTEN for a gap
        landed under its group and the cards FOUND for that same gap on the
        same screen didn't; a group told not to tag as one still got its branch;
        and a selection spanning two groups filed all of it under whichever
        group happened to come first.
        """
        names = ([group] if group is not None
                 else [getattr(p, "group", "") for p in self._run_plans])
        for name in names:
            if not name:
                continue
            try:
                branch = autotag.group_branch(str(name))
            except Exception as e:
                log.warn(f"run view: couldn't resolve the group branch: {e}")
                continue
            if branch:
                return branch
        return ""

    def _run_group(self) -> str:
        """The group this run belongs to — the first one its plans name.

        A run is a batch and a batch has one provenance; when a selection spans
        groups the first is as good an answer as any, and both the course tag
        and the source box show what it picked before anything commits."""
        for plan in self._run_plans:
            g = getattr(plan, "group", "")
            if g:
                return str(g)
        return ""

    def _source_path(self) -> str:
        """The provenance path for this run — most specific first, or "".

        A door that already knows (a lecture's filename, the topic AI Create
        was told about) wins; then the GROUP's own source; then the gap type
        (a missed question is Sources::MQ). Nothing else. `tool` is left out
        of the call deliberately: that argument is what reaches for the last
        path this door was given, and a remembered path is exactly the bug —
        it cannot know whether this batch has anything to do with the last one.

        "" is a real answer, not a hole. Course work carries its provenance in
        the course branch, and guessing at a second one only makes it wrong.
        """
        if self._seed_source:
            return self._seed_source
        kind = ""
        for p in self._run_plans:
            kind = getattr(p, "kg_type", "") or kind
            if kind:
                break
        try:
            return autotag.source_path_for(kg_type_key=kind,
                                           group=self._run_group())
        except Exception as e:
            log.warn(f"run view: couldn't work out the source: {e}")
            return ""

    def _source_tag(self) -> str:
        """The provenance tag for this run — {base}::Sources::…, or ""."""
        from ...core.config import source_tag
        return source_tag(self._source_path())

    def _refresh_source_label(self) -> None:
        """Show the derived source beside the buttons, or show nothing.

        Called again once the run has plans, because the group and the gap type
        both come from them and neither exists when the page is built."""
        lbl = getattr(self, "_source_lbl", None)
        if lbl is None:
            return
        path = self._source_path()
        if not path:
            lbl.setVisible(False)
            lbl.setToolTip("")
            return
        lbl.setText(f"Source: {path}")
        lbl.setVisible(True)
        group = self._run_group()
        lbl.setToolTip(
            "Everything this run commits is tagged under "
            f"{{base}}::Sources::{path}.\n\n"
            + ("It comes from the group — change it in the group's own "
               f"settings and every run for “{group}” follows."
               if group and not self._seed_source else
               "It was derived from what this run is: the file it came from, "
               "or the gap type.")
            + "\n\nIt carries NO body system — the content tag holds that, "
              "once.")

    def _found_flaw(self, nid: int) -> str:
        """One line of quality opinion on a card the deck already has, or "".

        Words, not a colour: this appears in square brackets on the row and in
        the tooltip. It is INFORMATIONAL only — a flawed card is still ticked
        and still commits, because it is his card and the tool's job here is to
        tell him, not to overrule him.
        """
        key = int(nid)
        cache = self._flaw_cache
        if key in cache:
            return cache[key]
        out = ""
        try:
            note = mw.col.get_note(key)
            front = note.fields[0] if note.fields else ""
            v = pipeline.check_found(front, self._atomicity_max)
            if v is not None and getattr(v, "verdict", "") != "PASS":
                out = str(getattr(v, "reason", "") or "").strip()
                # The row can't hold a paragraph; the tooltip carries the rest.
                if len(out) > 90:
                    out = out[:89].rstrip() + "…"
        except Exception:
            out = ""
        cache[key] = out
        return out

    def _suggest_tag(self, plan, group) -> str:
        """This gap's hierarchical tag. Built from the point levels the triage
        pass assigned — preferring a point that actually found cards, since a
        gap point's levels are the least corroborated thing in the group."""
        if self._lecture is not None:
            return self._lec_suggest_tag(plan)
        ordered = [m for m in group if m.final] + [m for m in group if not m.final]
        for m in ordered:
            try:
                # Where this gap's cards file: the content tag under the
                # shared base, or the group's own course branch when it has
                # one (autotag.content_tag_from_levels picks). The SOURCE never
                # comes through here — it rides as a shared tag at commit time,
                # because a run has one source and forty content tags.
                tag = autotag.content_tag_from_levels(m.point.tag_levels,
                                                      group=plan.group)
            except Exception as e:
                log.warn(f"bulk search: content tag failed: {e}")
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

        if kind == "point":
            # The action for "these cards don't actually answer this".
            act = menu.addAction("\u270d\ufe0e  Write a card for this point",
                                 lambda: self._write_for_point(item))
            act.setToolTip(
                "Generate a card for this point specifically. Use it when the "
                "cards found above don\u2019t really answer it \u2014 untick "
                "them, then ask for one that does.")
            ticked = any(
                item.child(i).checkState(0) == Qt.CheckState.Checked
                and item.child(i).data(0, self.ROLE_KIND) == "note"
                for i in range(item.childCount()))
            if ticked:
                menu.addAction(
                    "Untick this point\u2019s found cards",
                    lambda: self._untick_point(item))
            self._lec_menu(menu, item, kind)
            menu.exec(self._tree.mapToGlobal(pos))
            return

        if kind == "note":
            parent = item.parent()
            if parent is not None and parent.data(0, self.ROLE_KIND) == "point":
                menu.addAction(
                    "Untick this and write a card for the point instead",
                    lambda: self._untick_and_write(parent))
                menu.addSeparator()
            menu.addAction("Open in the Anki browser",
                           lambda: self._on_tree_double_clicked(item, 0))
            menu.exec(self._tree.mapToGlobal(pos))
            return

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
            self._image_menu(menu, item)
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

    def _untick_point(self, node) -> None:
        """Clear the ticks on one point's found cards."""
        self._tree.blockSignals(True)
        for i in range(node.childCount()):
            ch = node.child(i)
            if ch.data(0, self.ROLE_KIND) == "note":
                ch.setCheckState(0, Qt.CheckState.Unchecked)
        self._tree.blockSignals(False)
        self._update_selection_count()

    def _untick_and_write(self, node) -> None:
        """The whole gesture in one action: these cards don't answer the point,
        so drop them and ask for one that does."""
        self._untick_point(node)
        self._write_for_point(node)

    def _card_action(self, leaf, what: str) -> None:
        """Rewrite / split / one-by-one on a single written card.

        Synchronous with a wait cursor, like the same actions in AI Create: it
        is one short call about one card, and the alternative is a row that
        silently changes some seconds after you stopped looking at it."""
        from .. import card_creator as cc
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
                                         pipeline.dial_for(pipeline.WRITE))
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
                # An image attached to the card being split was attached to
                # the SUBJECT, not to one sentence of it, so every piece keeps
                # it. Untick it off the ones it doesn't suit; that is quicker
                # than finding it again.
                inherited = list(card.get("_image_paths") or [])
                for i, c in enumerate(out):
                    c = dict(c)
                    if inherited and not c.get("_image_paths"):
                        c["_image_paths"] = list(inherited)
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

    # -- images on a written card --------------------------------------------

    def _image_menu(self, menu, leaf) -> None:
        """One written card's image actions.

        ON REQUEST ONLY, and behind a submenu rather than a strip of buttons.
        Most written cards want no picture, and AI Create's habit of fetching
        candidates for every card it wrote costs a web round trip each -- fine
        for one topic's worth, wrong for a pass covering twelve gaps at once.
        So nothing happens until you ask, and then it is the same three routes
        AI Create offers, running the same functions underneath."""
        card = leaf.data(0, self.ROLE_CARD)
        paths = list((card or {}).get("_image_paths") or [])
        sub = menu.addMenu(f"\U0001f5bc  Images ({len(paths)})" if paths
                           else "\U0001f5bc  Images")
        sub.addAction("Attach from a file\u2026",
                      lambda: self._add_card_images(leaf, "file"))
        sub.addAction("\U0001f50d  Find one online\u2026",
                      lambda: self._add_card_images(leaf, "find"))
        sub.addAction("\U0001f310  Browse for one in Anki\u2026",
                      lambda: self._add_card_images(leaf, "browse"))
        sub.addSeparator()
        spread = sub.addAction("Put these on other cards too\u2026",
                               lambda: self._spread_images(leaf))
        spread.setEnabled(bool(paths))
        spread.setToolTip(
            "One diagram often answers several of these cards. This copies "
            "the attachment, not the file \u2014 it is imported into your "
            "media once, when the cards are actually added.")
        if paths:
            off = sub.addAction(f"Take {len(paths)} image(s) off this card")
            off.triggered.connect(lambda: self._set_card_images(leaf, []))

    def _add_card_images(self, leaf, how: str) -> None:
        card = leaf.data(0, self.ROLE_CARD)
        if not isinstance(card, dict):
            return
        got = _pick_card_images(self, how, _image_seed(card))
        if got:
            self._set_card_images(
                leaf, _merged_images(card.get("_image_paths"), got))

    def _set_card_images(self, leaf, paths: list, *, quiet: bool = False) -> None:
        """Replace one written card's attached images and redraw its row.

        Nothing is copied into Anki's media folder here. cc.set_fields does
        that at add time, so an image attached to a card you then untick never
        enters your collection at all -- the same promise the rest of this
        screen makes about everything on it."""
        card = leaf.data(0, self.ROLE_CARD)
        if not isinstance(card, dict):
            return
        keep = _merged_images(paths, [])
        if keep:
            card["_image_paths"] = keep
        else:
            card.pop("_image_paths", None)
        leaf.setData(0, self.ROLE_CARD, card)
        self._label_written(leaf, card, None, [], 0)
        if quiet:
            return
        self._set_status(
            f"{len(keep)} image(s) on that card \u2014 nothing is added to "
            "your collection until you apply." if keep
            else "Images taken off that card.")

    def _spread_images(self, leaf) -> None:
        """Copy one card's images onto others in the same pass.

        The case this is for: you go looking for a diagram of the loop of
        Henle and it answers four of the cards written for that gap, not one.
        Finding it four times is the same search four times."""
        card = leaf.data(0, self.ROLE_CARD)
        paths = list((card or {}).get("_image_paths") or [])
        if not paths:
            return
        rows = []
        for other, ocard in self._written_items():
            if other is leaf or not isinstance(ocard, dict):
                continue
            kg_id = str(other.data(0, self.ROLE_KG) or "")
            title = str((self._job_for_kg(kg_id) or {}).get("title") or "").strip()
            rows.append((other, ocard, kg_id, title or "(untitled gap)"))
        if not rows:
            tooltip("There is only one written card in this pass.")
            return
        dlg = _ApplyImagesDialog(paths, rows,
                                 str(leaf.data(0, self.ROLE_KG) or ""),
                                 parent=self)
        if not dlg.exec():
            return
        targets = dlg.chosen()
        if not targets:
            return
        self._tree.blockSignals(True)
        try:
            for other, ocard, _kg, _t in targets:
                self._set_card_images(
                    other, _merged_images(ocard.get("_image_paths"), paths),
                    quiet=True)
        finally:
            self._tree.blockSignals(False)
        self._set_status(
            f"{len(paths)} image(s) put on {len(targets)} more card(s) \u2014 "
            "still nothing added until you apply.")

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

    def _relabel_gaps(self) -> None:
        """Rewrite each gap row from what is TICKED right now.

        The row used to be written once, from the allocator's picks, and never
        again — so unticking every card under a gap left the header still
        announcing "3 card(s)", which is the opposite of what the screen was
        being used to decide. The search's own conclusion isn't lost; it moves
        to the tooltip, where a claim about the past belongs."""
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            kg_id = top.data(0, self.ROLE_KEY)
            title = top.data(0, self.ROLE_TITLE)
            if title is None:
                continue
            found, n_found, total = self._search_finding.get(kg_id, ("", 0, 0))

            n_ticked = 0
            pts_ticked = 0
            n_written = 0
            for pi in range(top.childCount()):
                node = top.child(pi)
                if node.data(0, self.ROLE_KIND) != "point":
                    continue
                hits = 0
                for ci in range(node.childCount()):
                    leaf = node.child(ci)
                    if leaf.checkState(0) != Qt.CheckState.Checked:
                        continue
                    kind = leaf.data(0, self.ROLE_KIND)
                    if kind == "note":
                        hits += 1
                    elif kind == "newcard":
                        n_written += 1
                if hits:
                    pts_ticked += 1
                    n_ticked += hits
            for leaf, _card in self._written_items():
                if str(leaf.data(0, self.ROLE_KG) or "") == kg_id \
                        and leaf.parent() is not None \
                        and leaf.parent().data(0, self.ROLE_KIND) == "written" \
                        and leaf.checkState(0) == Qt.CheckState.Checked:
                    n_written += 1

            bits = []
            if total:
                bits.append(f"{pts_ticked}/{total} point(s) ticked")
            bits.append("no cards ticked" if not n_ticked
                        else f"{n_ticked} existing card(s)")
            if n_written:
                bits.append(f"{n_written} new card(s)")
            top.setText(0, f"{title}      " + "  \u00b7  ".join(bits))

    def _update_selection_count(self) -> None:
        ticked = self._ticked()
        self._relabel_gaps()
        n = len({nid for rec in ticked.values() for nid in rec["nids"]})
        self._sel_lbl.setText(f"{n} card(s) ticked")
        full, partial, empty = self._outcome_split()
        self._gap_hint.setText(
            f"{len(full)} gap(s) fully covered · {len(partial)} partly · "
            f"{len(empty)} not covered at all.  Only fully-covered gaps are "
            "marked done — the rest stay open, and those are the ones worth "
            "making cards for."
        )
        if self._gen_batch_id:
            # ONE screen, ONE button: the found cards and the written cards
            # go in together, under one checkpoint.
            approved = sum(len(c) for _k, c in self._approved_written())
            # "Add all now" only exists while there is something written to
            # add; with none, Apply is just the found half and there is no
            # second way of doing it.
            self._add_now_btn.setVisible(approved > 0)
            if self._gen_active:
                self._commit_btn.setEnabled(False)
                self._add_now_btn.setEnabled(False)
                self._commit_btn.setText("Writing the missing cards…")
            else:
                self._commit_btn.setEnabled(
                    not self._applied and (n > 0 or approved > 0))
                self._add_now_btn.setEnabled(
                    not self._applied and approved > 0)
                self._commit_btn.setText(
                    f"Apply — {n} found + review {approved} in Add  ▶"
                    if approved else f"Apply — {n} found card(s)")
                self._add_now_btn.setText(f"Add {approved} now")
            return
        # Nothing being written (Write unticked on every gap, or nothing left
        # to write): Apply is just the found half.
        self._commit_btn.setEnabled(not self._applied and n > 0)
        self._commit_btn.setText(f"Apply — {n} found card(s)")

    # ── commit ───────────────────────────────────────────────────────────────

    def _on_commit(self) -> None:
        """Tag and unsuspend what the deck already had.

        On the one screen this is only half the job — _on_apply_everything runs
        it and the written cards inside a single checkpoint. This path is the
        plain search commit, for a run where nothing is being written."""
        if self._gen_batch_id:
            self._on_apply_everything()
            return
        # `_coverage is None` is a write-only run (Find unticked everywhere),
        # which has cards to commit even though nothing was searched.
        if not anki_utils.require_col():
            return
        if self._coverage is None and not self._run_plans:
            return
        ticked = self._ticked()
        all_nids = sorted({nid for rec in ticked.values() for nid in rec["nids"]})
        if not all_nids:
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
        extra = self._lec_confirm_lines(all_nids)
        if extra:
            lines.append("")
            lines.extend(extra)
        else:
            lines.append("\nCard content is never modified.")
        if not askUser("\n".join(lines)):
            return

        # One checkpoint around the lot, so one undo reverses the whole batch.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Ankisstant: bulk gap search")

        tagged, unsuspended = self._apply_found(ticked, all_nids, do_unsuspend)
        # The door's own commit step (a lecture's slides and notes), inside the
        # same checkpoint — the written-card route runs it in _finish_apply.
        if self._on_committed is not None:
            try:
                self._on_committed({}, all_nids)
            except Exception as e:
                log.error(f"run view: the {self._door} door's commit step "
                          f"failed: {e}")
        closed = self._close_gaps(full)

        self._commit_btn.setEnabled(False)
        msg = (f"Done — {len(all_nids)} note(s) committed"
               + (f" ({tagged} tagged)" if tagged != len(all_nids) else "")
               + (f", {unsuspended} card(s) unsuspended" if do_unsuspend else "")
               + f", {closed} gap(s) closed.")
        left = len(partial) + len(empty)
        if left:
            msg += (f" {left} gap(s) stay open — tick Write on them next run "
                    "to make the cards your deck is missing.")
        self._status.setText(msg)
        tooltip(msg, period=5000)
        try:
            mw.col.reset()
            mw.reset()
        except Exception:
            pass

    def _apply_found(self, ticked: dict, all_nids: list[int],
                     do_unsuspend: bool, source: str = "") -> tuple[int, int]:
        """Tag (and optionally unsuspend) the ticked cards. Returns
        (notes tagged, cards unsuspended).

        No checkpoint and no confirm of its own: both callers own those, and on
        the one screen this runs inside the SAME checkpoint as the newly written
        cards so one undo takes back the whole pass."""
        plans_by_id = {p.kg_id: p for p in self._run_plans}
        per_item: list = []
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
            # This gap's GROUP branch, alongside its content tag. Per gap
            # rather than once for the run, because a run can span groups —
            # see _course_tag. The head node only: the classification under it
            # is what the content tag already says, and on this screen that tag
            # is the box the user can see and edit.
            branch = self._course_tag(getattr(plan, "group", "") if plan else "")
            # Skipped when a tag already on the card sits UNDER that branch,
            # which is the ordinary case for a course group now that its branch
            # carries the classification (autotag.content_tag_from_levels).
            # Anki's sidebar shows the parent node for a child tag, so adding
            # the head as well is a tag that says nothing new.
            if branch and any(t == branch or t.startswith(branch + "::")
                              for t in tags):
                branch = ""
            if branch and branch not in tags:
                tags.append(branch)
            for tag in tags:
                per_item.append((tag, list(rec["nids"])))

        # A lecture's cards carry AI Lecture's audit trail, as they always have.
        cfg = tool_config("lecture" if self._lecture is not None else "browse")
        cplan = pipeline.CommitPlan(
            label="Ankisstant: find & write",
            tag_ops=commit.tag_ops_for(
                per_item=per_item,
                audit=(cfg.get("audit_tag") or "").strip(),
                month=month_tag(),
                # No shared `course`: the group branch rides per gap now, in
                # per_item, so a two-group run can't file one group's cards
                # under the other's branch.
                source=source or self._source_tag(),
                # EVERY ticked note, not just the tagged ones: a gap with an
                # empty tag box still took part in this run and still gets the
                # audit trail, exactly as it did before.
                everything=list(all_nids)),
            unsuspend_nids=list(all_nids) if do_unsuspend else [],
        )
        # checkpoint=False: the caller opened ONE around this and the newly
        # written cards together, which is what makes a single Ctrl+Z take the
        # whole pass back out.
        res = commit.run(cplan, checkpoint=False)
        return res.tagged, res.unsuspended

    # ── unified: the writing half, on the same screen ────────────────────────

    def _create_payload(self, *, for_writing: bool = False) -> list[dict]:
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
        # A gap with Find off was never measured, so every one of its points is
        # uncovered as far as this run knows. Saying "your deck covers it"
        # about a search that never ran would be inventing a result.
        for kg_id, (find, write) in (self._wants or {}).items():
            if write and not find and kg_id in plans_by_id:
                missing_by_kg.setdefault(kg_id, [])
                if not missing_by_kg[kg_id]:
                    missing_by_kg[kg_id] = [
                        _AllPoints(pt) for pt in plans_by_id[kg_id].points]
        # ONE BUDGET FOR FINDING AND WRITING. A gap's card count is how many
        # cards it should end up with, not how many to FIND — so turning it
        # down must not turn into "write cards for whatever didn't fit".
        #
        # Two rules do that. A point whose cards exist but lost the budget
        # lottery is not a gap and is never written for (is_gap is False:
        # the deck covers it, the budget just went elsewhere). And what IS
        # written for a gap is capped at its budget minus the found cards
        # ticked under it.
        allowance: dict = {}
        self._budget_held = 0
        if for_writing and self._coverage is not None:
            for kg_id in list(missing_by_kg):
                missing_by_kg[kg_id] = [
                    m for m in missing_by_kg[kg_id]
                    if getattr(m, "is_gap", True)
                    or not getattr(m, "available", None)]
            allowance = self._write_allowance(
                {k: len(v) for k, v in missing_by_kg.items() if v},
                plans_by_id)
        out: list[dict] = []
        for kg_id, missing in missing_by_kg.items():
            if not missing:
                continue
            # Write unticked: found what your deck has, and that is all you
            # asked for.
            if self._wants and not self._wants.get(kg_id, (True, True))[1]:
                continue
            if kg_id in allowance:
                n_ok = allowance[kg_id]
                self._budget_held += max(0, len(missing) - n_ok)
                if n_ok <= 0:
                    continue
            plan = plans_by_id.get(kg_id)
            kg = self._record_for(kg_id)
            if plan is None or not kg:
                continue
            rec = create_record_for(
                kg, plan.concept,
                [{"concept": m.point.concept, "terms": list(m.point.terms),
                  "system": m.point.system, "subsystem": m.point.subsystem,
                  "topic": m.point.topic}
                 for m in missing],
                style_examples=self._style_examples_for(kg_id))
            if kg_id in allowance:
                # The points stay listed in priority order; gen_prompt cuts the
                # list to this and asks for exactly one card each.
                rec["target_cards"] = max(1, min(len(missing), allowance[kg_id]))
            out.append(rec)
        return out

    def _write_allowance(self, wants: dict, plans_by_id: dict) -> dict:
        """{kg_id: cards this gap may still WRITE} — its budget minus the found
        cards ticked under it.

        A lecture has one budget for the whole lecture (its Coverage dial), so
        there the remainder is shared across the sections that still need cards,
        in teaching order."""
        ticked = self._ticked()
        if self._lecture is not None:
            budget = int(getattr(self._coverage, "budget", 0) or 0)
            if budget <= 0:
                return {}
            found = len({n for r in ticked.values() for n in r["nids"]})
            return split_budget(list(wants.items()), max(0, budget - found))
        out = {}
        for kg_id, n in wants.items():
            plan = plans_by_id.get(kg_id)
            if plan is None:
                continue
            have = len((ticked.get(kg_id) or {}).get("nids") or [])
            out[kg_id] = max(0, kg_triage.clamp_cards(plan.cards) - have)
        return out

    # How many found-card fronts ride along as style examples, and the longest
    # one allowed in. Six is enough to set a register without crowding the
    # prompt; the length cap keeps the examples inside the limit the writer is
    # held to — a 40-word AnKing card would teach the opposite lesson.
    STYLE_EXAMPLES_MAX = 6

    def _style_examples_for(self, kg_id: str) -> list[str]:
        """Fronts of real cards from his deck on this gap's topic — the ones the
        search found, ticked or not, chosen ones first — to show the writer the
        length and directness to match. Cloze fronts only, images stripped,
        within the word limit. Main thread (reads mw.col). Fail-open: no
        examples is the old behaviour, never an error."""
        out: list[str] = []
        try:
            if self._coverage is None or not kg_id:
                return out
            from .. import quality_pass as qp
            nids: list[int] = []
            for m in list(self._coverage.covered) + list(self._coverage.gaps):
                if getattr(m.point, "kg_id", "") != kg_id:
                    continue
                for n in list(m.final) + list(m.candidates):
                    if n not in nids:
                        nids.append(n)
            for nid in nids:
                if len(out) >= self.STYLE_EXAMPLES_MAX:
                    break
                try:
                    note = mw.col.get_note(int(nid))
                except Exception:
                    continue
                front = note.fields[0] if note.fields else ""
                if "{{c" not in front:
                    continue
                front = re.sub(r"<img[^>]*>", "", front).strip()
                n_words = qp.front_word_count(front)
                if 0 < n_words <= qp.MAX_FRONT_WORDS and front not in out:
                    out.append(front)
        except Exception as e:
            log.warn(f"work through: style examples skipped: {e}")
        return out

    # ── taking the uncovered points elsewhere ───────────────────────────────

    def _copy_menu(self) -> None:
        """Hand the residue to whatever tool the user actually writes cards in.

        Not everyone generates cards in here. Some people use a different
        creator add-on, a Custom GPT, or write them by hand — and for them the
        valuable output of this screen is not the cards, it is the LIST: the
        points their own deck turned out not to cover. That list took a search
        and a judge pass to produce and there was no way to get it out.
        """
        payload = self._create_payload()
        if not payload:
            tooltip("Nothing uncovered — every point has a card ticked under it.",
                    period=4000)
            return
        n_gaps = len(payload)
        n_pts = sum(len(_record_points(r)) for r in payload)
        menu = QMenu(self)
        a1 = menu.addAction(f"Copy {n_pts} point(s) as a list")
        a1.setToolTip("Plain text, grouped by gap, with each gap's notes. For "
                      "pasting into your own card maker.")
        a1.triggered.connect(lambda: self._copy_uncovered("points"))
        a2 = menu.addAction(f"Copy the full prompt for {n_gaps} gap(s)")
        a2.setToolTip("Exactly what Ankisstant would send its own AI, "
                      "instructions and all. Paste it into any chatbot.")
        a2.triggered.connect(lambda: self._copy_uncovered("prompt"))
        a3 = menu.addAction("Copy as markdown checklist")
        a3.setToolTip(
            "One '- [ ]' per point, under a heading per gap — the shape Bulk "
            "import reads back, and the shape Things and most outliners paste "
            "into. Re-importing turns each POINT into its own gap, so the "
            "parent gap's notes don't survive that round trip.")
        a3.triggered.connect(lambda: self._copy_uncovered("markdown"))
        menu.addSeparator()
        # Saving to a FILE used to be AI Lecture's alone (panel._export_gaps),
        # and copying to the clipboard used to be this screen's alone. Both are
        # the same job — get the list out to whatever you actually write cards
        # in — so both surfaces now offer both.
        a4 = menu.addAction("Save the checklist to a file…")
        a4.setToolTip("The same markdown, written to a file you can hand to "
                      "another card generator.")
        a4.triggered.connect(self._save_uncovered)
        menu.exec(self._copy_btn.mapToGlobal(self._copy_btn.rect().bottomLeft()))

    def _copy_uncovered(self, kind: str) -> None:
        payload = self._create_payload()
        if not payload:
            return
        if kind == "prompt":
            text = self._uncovered_as_prompt(payload)
            if not text:
                showWarning(
                    "Couldn't build the prompt — that needs a notetype profile "
                    "so the instructions match the card format. Pick one on the "
                    "plan page, or copy the points as a list instead.")
                return
        else:
            text = self._uncovered_as_text(payload, markdown=(kind == "markdown"))
        try:
            QApplication.clipboard().setText(text)
        except Exception as e:
            log.error(f"work through: clipboard write failed: {e}")
            showWarning(f"Couldn't copy that: {e}")
            return
        n_pts = sum(len(_record_points(r)) for r in payload)
        tooltip(f"Copied {n_pts} uncovered point(s) from {len(payload)} gap(s).",
                period=4000)

    def _save_uncovered(self) -> None:
        payload = self._create_payload()
        if not payload:
            return
        text = self._uncovered_as_text(payload, markdown=True)
        stem = "uncovered_points"
        if len(self._run_plans) == 1:
            safe = "".join(ch if (ch.isalnum() or ch in " -_") else "_"
                           for ch in str(self._run_plans[0].title))
            stem = (safe.strip() or stem)[:60].replace(" ", "_")
        from aqt.qt import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the uncovered points", f"{stem}_missing_points.md",
            "Markdown (*.md);;Text (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as e:
            log.error(f"work through: export failed: {e}")
            showWarning(f"Couldn't write that file:\n\n{e}")
            return
        n_pts = sum(len(_record_points(r)) for r in payload)
        showInfo(f"Wrote {n_pts} uncovered point(s) from {len(payload)} "
                 f"item(s) to:\n\n{path}")

    def _uncovered_as_text(self, payload: list, *, markdown: bool) -> str:
        """The residue as text. The gap's own notes ride along, because a point
        read out of context is often not enough to write a card from."""
        out: list[str] = []
        for rec in payload:
            title = str(rec.get("title") or "(untitled)")
            out.append(f"## {title}" if markdown else title.upper())
            concept = str(rec.get("concept") or "").strip()
            if concept and concept != title:
                # Blockquote, not italics: the markdown shape has to read back
                # through Bulk import as the SAME gaps, and an unindented plain
                # line there is a gap of its own.
                out.append(f"> _{concept}_" if markdown else f"  ({concept})")
            notes = str(rec.get("notes") or "").strip()
            if notes:
                # create_record_for appends the missing-points prose to notes
                # for the generator; strip it here or every gap repeats its own
                # point list twice.
                cut = notes.find("My deck has no card for these parts")
                if cut > 0:
                    notes = notes[:cut].strip()
                elif cut == 0:
                    notes = ""
                if notes:
                    out.append(("> " if markdown else "  notes: ")
                               + notes.replace("\n",
                                               "\n> " if markdown else "\n  "))
            for pt in _record_points(rec):
                out.append(f"- [ ] {pt}" if markdown else f"  - {pt}")
            out.append("")
        return "\n".join(out).strip() + "\n"

    def _uncovered_as_prompt(self, payload: list) -> str:
        """The real generation request, assembled but not sent.

        Built through gen_prompt, not a second prompt written for this button —
        a copy that drifts from what the add-on actually sends is worse than no
        copy at all, because it looks authoritative."""
        notetype_name = (self._notetype.currentText().strip()
                         if self._notetype is not None else "")
        if not notetype_name:
            return ""
        from . import gen_prompt
        blocks: list[str] = []
        for rec in payload:
            plan = kg_triage.plan_from_queued_gap(rec)
            if plan is None:
                continue
            try:
                req = gen_prompt.build_generation_request(
                    kg=rec, plan=plan,
                    profile=gen_prompt._profile_for(tool_config("card_creator"),
                                                    notetype_name),
                    cfg=tool_config("card_creator"))
            except Exception as e:
                log.warn(f"work through: couldn't build prompt for "
                         f"{rec.get('title')!r}: {e}")
                continue
            blocks.append(
                f"===== {rec.get('title') or 'gap'} =====\n\n"
                f"[SYSTEM INSTRUCTIONS]\n{req.get('system_prompt', '')}\n\n"
                f"[YOUR TASK]\n{req.get('user_msg', '')}")
        if not blocks:
            return ""
        head = (f"{len(blocks)} gap(s) your deck didn't cover. Each block below "
                "is one independent request — run them one at a time and keep "
                "the replies separate.\n\n") if len(blocks) > 1 else ""
        return head + "\n\n\n".join(blocks) + "\n"

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
        if self._wants and not any(w for _f, w in self._wants.values()):
            # Write unticked on every gap: a search-only run, by choice.
            self._stage(pipeline.WRITE, pipeline.SKIPPED, "Write was off")
            self._stage(pipeline.CHECK, pipeline.SKIPPED)
            return
        if not provider_configured():
            self._show_written_ui()
            self._gen_note.setText(
                "Writing the missing cards needs a provider — set one up in "
                "Ankisstant Settings.")
            return
        payload = self._create_payload(for_writing=True)
        held = getattr(self, "_budget_held", 0)
        if not payload:
            self._stage(pipeline.WRITE, pipeline.SKIPPED,
                        "nothing left to write")
            self._stage(pipeline.CHECK, pipeline.SKIPPED)
            self._show_written_ui()
            self._gen_note.setText(
                f"The card budget is already spent on cards you own — "
                f"{held} uncovered point(s) were left unwritten. Raise a "
                "gap's card count, or right-click a point to write for it "
                "anyway." if held else
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
            tags_by_kg=tags_by_kg, model=pipeline.dial_for(pipeline.WRITE),
            origin="work-through",
            grounding=self._ground_on(), auto_image=self._autoimg_on())
        if not records:
            self._show_written_ui()
            self._gen_note.setText(
                "Couldn't build a generation request for any of those gaps \u2014 "
                "see the log.")
            return

        self._gen_batch_id = records[0]["batch_id"]
        self._stage(pipeline.WRITE, pipeline.RUNNING,
                    f"{len(records)} item(s)")
        self._mark_in_progress([r.get("kg_id") for r in records])
        self._show_written_ui()
        if is_manual_provider():
            # No background worker: the paste provider IS a modal, so it has to
            # run on the main thread, one gap at a time.
            self._render_written_placeholders(records)
            self._run_manual(records)
            return
        self._gen_active = True
        create_jobs.enqueue_batch(records)
        pts = sum(len(p.points) for p in plans)
        self._found_head.setText(
            "What your deck has for these gaps \u2014 and, marked NEW, the "
            f"{pts} card(s) being written for what it doesn\u2019t")
        self._gen_note.setText(
            f"{len(records)} gap(s) queued. Read the found cards above while "
            "this runs \u2014 nothing is added to your collection until you "
            "press Apply."
            + (f" {held} more uncovered point(s) were left unwritten because "
               "their gap's card budget is spent \u2014 right-click a point to "
               "write for it anyway." if held else ""))
        self._render_written_placeholders(records)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._poll.start()
        self._on_gen_poll()

    def _write_for_point(self, node) -> None:
        """Write a card for ONE point, on demand.

        The case this exists for: the search found cards for a point, you read
        them, and they don't actually answer it — so you untick them and want a
        card that does. Nothing else on the screen could express that, because
        generation only ever ran on what the search declared uncovered, and by
        then it had already decided this point was fine.

        It joins the existing batch rather than starting its own, so it shares
        the progress bar, the Stop button, the one Apply and the one undo."""
        kg_id = str(node.data(0, self.ROLE_KG) or "")
        concept = str(node.data(0, self.ROLE_CONCEPT) or "")
        if not kg_id or not concept:
            return
        if not provider_configured():
            showWarning("Writing a card needs a provider — set one up in "
                        "Ankisstant Settings.")
            return
        notetype_name = (self._notetype.currentText().strip()
                         if self._notetype is not None else "")
        if not notetype_name:
            showWarning("No notetype is set, so there's nothing to write the "
                        "card as. Settings → AI Create adds notetype profiles.")
            return

        plan = next((p for p in self._run_plans if p.kg_id == kg_id), None)
        kg = kg_store.get(kg_id)
        if plan is None or not kg:
            return
        # Coverage splits into covered/gaps; a point you are asking about is by
        # definition on the covered side (the gaps were written for already).
        members = (list(self._coverage.covered) + list(self._coverage.gaps)
                   if self._coverage is not None else [])
        point = next((m.point for m in members
                      if m.point.kg_id == kg_id and m.point.concept == concept),
                     None)
        if point is None:
            log.warn(f"work through: no point matching {concept!r} on {kg_id}")
            return

        from .. import create_jobs
        from . import gen_prompt
        try:
            autotag.refresh_vocab_cache()
        except Exception as e:
            log.warn(f"work through: vocab cache refresh failed: {e}")

        rec = create_record_for(kg, plan.concept, [{
            "concept": point.concept, "terms": list(point.terms),
            "system": point.system, "subsystem": point.subsystem,
            "topic": point.topic}],
            style_examples=self._style_examples_for(kg_id))
        one = kg_triage.plan_from_queued_gap(rec)
        if one is None:
            showWarning("Couldn't turn that point into a generation plan — "
                        "see the log.")
            return

        cc_cfg = tool_config("card_creator")
        base_tags = [t.strip() for t in (cc_cfg.get("default_tags") or [])
                     if str(t).strip()]
        audit = (cc_cfg.get("audit_tag") or "").strip()
        if audit and audit not in base_tags:
            base_tags.append(audit)
        records = gen_prompt.build_batch(
            plans=[one], gaps_by_id={kg_id: rec},
            deck_name=((self._deck.currentText().strip()
                        if self._deck is not None else "") or "Default"),
            notetype_name=notetype_name, base_tags=base_tags,
            tags_by_kg={kg_id: self._tag_text(kg_id)},
            model=pipeline.dial_for(pipeline.WRITE), origin="work-through",
            batch_id=self._gen_batch_id,
            grounding=self._ground_on(), auto_image=self._autoimg_on())
        if not records:
            showWarning("Couldn't build a generation request for that point — "
                        "see the log.")
            return

        self._gen_batch_id = records[0]["batch_id"]
        self._mark_in_progress([kg_id])
        self._tree.blockSignals(True)
        row = self._pending_row(node, kg_id, self._WRITING)
        row.setData(0, self.ROLE_KEY, str(records[0].get("id") or ""))
        self._tree.blockSignals(False)
        self._show_written_ui()
        if is_manual_provider():
            # One point, one paste — the case the paste provider suits best.
            self._run_manual(records)
            return
        self._gen_active = True
        create_jobs.enqueue_batch(records)
        self._progress.setRange(0, 100)
        self._progress.setVisible(True)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._poll.start()
        self._update_selection_count()

    def _run_manual(self, records: list[dict]) -> None:
        """The paste provider, gap by gap, on the main thread.

        create_jobs runs its worker off-thread with uses_collection=False, and
        the manual provider is a modal dialog — Qt on a worker thread is not a
        thing. So manual takes the same records and walks them here instead,
        writing each result back through the SAME _apply_result_main_thread the
        background path uses, so a pasted reply is routed, graded and tagged
        identically to a generated one.

        Each gap renders the moment its paste is accepted, so a five-gap run
        shows its answers as you go rather than all at the end."""
        from .. import create_jobs
        self._gen_active = True
        self._progress.setRange(0, len(records))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._stop_btn.setVisible(False)      # Cancel the paste dialog instead
        for i, rec in enumerate(records, 1):
            if self._closed:
                return
            self._gen_note.setText(
                f"Paste gap {i} of {len(records)} — "
                f"“{rec.get('title') or 'this gap'}”. Cancel the "
                "paste box to stop; anything already pasted is kept.")
            QApplication.processEvents()
            job = create_jobs.add(**rec, status=create_jobs.RUNNING)
            try:
                result = create_jobs._worker(job, threading.Event())
            except Exception as e:
                log.error(f"work through (manual): {rec.get('title')!r}: {e}")
                create_jobs.update(job["id"], status=create_jobs.ERROR,
                                   error=str(e))
                self._on_gen_poll()
                continue
            create_jobs._apply_result_main_thread(job["id"], result)
            self._progress.setValue(i)
            self._on_gen_poll()
        self._gen_active = False
        self._progress.setVisible(False)
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
        try:
            # Cards first, then the promises for whatever is still outstanding —
            # in that order, so a gap that just landed is never re-labelled
            # "writing" over its own freshly rendered cards.
            self._render_generated(jobs)
            self._update_written_placeholders(jobs)
        except RuntimeError:
            self._closed = True
            return

        if counts["finished"]:
            self._poll.stop()
            self._gen_active = False
            self._progress.setVisible(False)
            self._stop_btn.setVisible(False)
            self._resurface()
            self._refresh_gen_head()
            self._mark_write_done()
            return
        try:
            self._progress.setValue(
                int(100 * counts["done"] / max(1, counts["total"])))
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

    def _job_for_kg(self, kg_id: str) -> dict:
        """A representative job for a gap, for the things every job of that gap
        shares: the deck it writes into and the tag base it commits under."""
        for j in self._gen_jobs.values():
            if str(j.get("kg_id") or "") == str(kg_id):
                return j
        return {}

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

    # One message for every state that still has an answer coming.
    #
    # A gap that finished early used to flip to "written — see below" while the
    # cards were still waiting on the SLOWEST gap in the batch, so the row
    # announced an output that wasn't on screen yet. Rendering is per gap now
    # (see _render_generated), so the only honest thing to say until a gap's
    # cards are actually there is that it is being written.
    _WRITING = "\u270d\ufe0e  writing a card for this\u2026"
    _PENDING_TEXT = {
        "queued": _WRITING,
        "running": _WRITING,
        "ready": _WRITING,
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
        row.setData(0, self.ROLE_KEY, "")
        row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        f = row.font(0)
        f.setItalic(True)
        row.setFont(0, f)
        row.setText(0, text)
        node.setExpanded(True)
        return row

    def _clear_pending(self, kg_id: str, concepts=None, *,
                       kinds=("pending",), job_id: str = "") -> None:
        """Drop this gap's transient rows.

        `concepts` scopes it to the points ONE job is responsible for, because a
        gap can have two jobs in flight — the batch write, and an on-demand
        write for a single point you asked for after unticking its cards. A
        blanket clear would delete the other one's row."""
        top = self._gap_item(kg_id)
        if top is None:
            return
        want = None
        if concepts is not None:
            want = {" ".join(str(c).split()).casefold() for c in concepts}
        stack = [(top, None)]
        while stack:
            it, concept = stack.pop()
            for i in reversed(range(it.childCount())):
                ch = it.child(i)
                kind = ch.data(0, self.ROLE_KIND)
                if kind == "point":
                    stack.append((ch, ch.data(0, self.ROLE_CONCEPT)))
                    continue
                if kind not in kinds:
                    stack.append((ch, concept))
                    continue
                if job_id and str(ch.data(0, self.ROLE_KEY) or "") != job_id \
                        and kind == "pending":
                    continue
                if want is not None and concept is not None \
                        and " ".join(str(concept).split()).casefold() not in want:
                    continue
                it.removeChild(ch)

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
            if str(j.get("id") or "") in self._gen_shown:
                continue          # its cards are on screen; nothing to promise
            st = str(j.get("status") or "")
            text = self._PENDING_TEXT.get(st, st)
            if st == "error":
                text += " \u2014 " + str(j.get("error") or "generation failed")
            for concept in self._point_concepts(j):
                node = self._point_node(kg_id, concept)
                if node is not None:
                    row = self._pending_row(node, kg_id, text)
                    row.setData(0, self.ROLE_KEY, str(j.get("id") or ""))
            folder = self._written_folder(kg_id)
            if folder is not None:
                n = len(j.get("mass_points") or [])
                if st == "error":
                    txt = ("\u26a0\ufe0f  couldn\u2019t write these \u2014 "
                           + str(j.get("error") or "generation failed"))
                elif st == "interrupted":
                    txt = "\u26d4  stopped before these were written"
                else:
                    # queued, running, and ready-but-not-yet-rendered all read
                    # the same: there is an answer coming and it isn't here yet.
                    txt = f"\u270d\ufe0e  writing {n} card(s)\u2026"
                folder.setText(0, txt)
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

        self._gen_jobs = {str(j.get("id") or ""): j for j in jobs}

        # PER GAP, as each one lands. Waiting for the whole batch meant a gap
        # that finished in twenty seconds sat behind one that took three
        # minutes, with a row claiming an output that wasn't on screen. A gap is
        # rendered once and then left alone.
        fresh = [j for j in jobs
                 if str(j.get("status") or "") in ("ready", "error", "interrupted")
                 and str(j.get("id") or "") not in self._gen_shown]
        if not fresh:
            return          # nothing new landed this tick; don't redo the work

        self._gen_profile = cc._resolved_profile(
            tool_config("card_creator"),
            self._notetype.currentText().strip()
            if self._notetype is not None else "")
        front_field = self._gen_profile.get("front_field", "Text")

        # One dup-check pass over the gaps being rendered now, not one per row:
        # it is a find_notes per card on the main thread. Within-batch
        # near-duplicates are flagged too \u2014 two gaps about the same entity
        # produce the same card, and nothing culls a freshly written duplicate
        # the way search dedupes a matched one. _gen_seen carries across calls so
        # a later gap is still checked against an earlier one.
        dups: dict[int, str] = {}
        idx = 0
        for j in fresh:
            for card in (j.get("cards") or []):
                msg = cc.dup_check(card, front_field)
                key = _norm_front(card)
                if key and key in self._gen_seen:
                    extra = ("\u26a0 near-duplicate of a card under "
                             f"\u2018{self._gen_seen[key]}\u2019")
                    msg = f"{msg}  \u00b7  {extra}" if msg else extra
                elif key:
                    self._gen_seen[key] = j.get("title") or "another gap"
                if msg:
                    dups[idx] = msg
                idx += 1

        self._tree.blockSignals(True)
        idx = 0
        for j in fresh:
            kg_id = str(j.get("kg_id") or "")
            self._gen_shown.add(str(j.get("id") or ""))
            if self._gap_item(kg_id) is None:
                idx += len(j.get("cards") or [])
                continue
            # Only THIS job's own pending rows. A gap can have a second job
            # running (an on-demand write for one point) and clearing the whole
            # gap would wipe the row telling you about it.
            self._clear_pending(kg_id, self._point_concepts(j))
            folder = self._written_folder(kg_id, create=True)
            cards = list(j.get("cards") or [])
            verdicts = list(j.get("verdicts") or [])
            concepts = self._point_concepts(j)
            if not cards:
                st = j.get("status")
                why = str(j.get("error") or "the AI returned no cards")
                msg = (f"\u26a0\ufe0f  nothing was written \u2014 {why} "
                       "\u00b7 right-click to retry"
                       if st in ("error", "interrupted")
                       else "\u2014  the AI returned no cards \u00b7 "
                            "right-click to retry")
                # A single-point job failed for THAT point, so say so there
                # rather than on the gap, where it would read as the whole gap
                # having failed.
                node = (self._point_node(kg_id, concepts[0])
                        if len(concepts) == 1 else None)
                if node is not None:
                    row = self._pending_row(node, kg_id, msg)
                    row.setData(0, self.ROLE_KEY, str(j.get("id") or ""))
                else:
                    folder.setText(0, msg)
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
        if self._lecture is not None:
            self._lec_refresh_badges()

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
        n_img = len(card.get("_image_paths") or [])
        if n_img:
            bits.append(f"{n_img} image(s)")
        elif str(card.get("image_query") or "").strip():
            # Auto-image flagged it. Said on the row so the cards worth
            # opening for a picture can be found without opening every one.
            bits.append("image suggested")
        if str(card.get("source") or "").strip():
            bits.append("cites a source")
        if card.get("_no_emphasis"):
            # The model left the front unmarked AND the deterministic repair
            # couldn't find a known anchor in it (see card_creator's
            # ensure_front_emphasis). Said in words, because the fix is a
            # double-click and five seconds — but only if you notice.
            bits.append("no bold anchor")
        if dup:
            bits.append(dup)
        suffix = ("   \u2014 " + "  \u00b7  ".join(bits)) if bits else ""
        leaf.setText(0, f"NEW  {_short(front, 240)}{suffix}")
        leaf.setToolTip(0, "\n\n".join(x for x in (
            f"FRONT\n{front}", (f"BACK\n{back}" if back else ""),
            (v.get("reason") or "") if verdict in ("FAIL", "FLAG") else "",
            "Double-click to edit this card before it is added.") if x))

    def _mark_write_done(self) -> None:
        """WRITE and CHECK are finished; the cards are on screen for REVIEW.

        CHECK is reported as skipped rather than done when the quality pass is
        off — a stage that didn't run must say so, not quietly read as if it
        had passed everything."""
        n = len(self._written_items())
        self._stage(pipeline.WRITE, pipeline.DONE, f"{n} card(s)")
        graded = any((c or {}).get("grade") for _leaf, c in self._written_items())
        self._stage(pipeline.CHECK,
                    pipeline.DONE if graded else pipeline.SKIPPED,
                    "graded" if graded else "not graded")
        self._stage(pipeline.REVIEW, pipeline.RUNNING)

    def _refresh_gen_head(self) -> None:
        items = self._written_items()
        approved = sum(1 for it, _c in items
                       if it.checkState(0) == Qt.CheckState.Checked)
        # Rendering is per gap, so the count is a running total while a batch is
        # still going. Saying how many gaps are still out stops "3 of 3 ticked"
        # from reading as "that's all of them".
        outstanding = len([k for k in self._gen_jobs if k not in self._gen_shown])
        tail = (f"; {outstanding} more gap(s) still writing"
                if outstanding else "")
        try:
            self._found_head.setText(
                "What your deck has for these gaps \u2014 and, marked NEW, the "
                f"cards written for what it didn\u2019t "
                f"({approved} of {len(items)} ticked{tail})")
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
        # Only gaps that have actually finished. A queued job has no cards yet,
        # and counting it as "came back with no cards" reports a failure that
        # hasn't happened.
        blank = [jid for jid, j in self._gen_jobs.items()
                 if jid in self._gen_shown and not (j.get("cards") or [])]
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
        for _jid, j in self._gen_jobs.items():
            kg_id = str(j.get("kg_id") or "")
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
        # retry_job REUSES the job id, so a job already in _gen_shown would
        # never be drawn again. Forget it, and take its rows with it.
        job = self._gen_jobs.get(job_id) or {}
        kg_id = str(job.get("kg_id") or "")
        self._gen_shown.discard(job_id)
        if kg_id:
            self._tree.blockSignals(True)
            self._clear_pending(kg_id, self._point_concepts(job),
                                kinds=("pending", "newcard"))
            self._tree.blockSignals(False)
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

    def _on_apply_everything(self, review: bool = True) -> None:
        """Tag and unsuspend what your deck already had, AND add the cards
        written for what it didn't.

        This is the whole reason the two halves share a screen: one decision,
        one confirm, and — for the found half — one Ctrl+Z that takes it back
        out.

        `review` is the difference between the two buttons. Ticked (the
        default), the written cards load into Anki's own Add window one at a
        time and you press Add on each, the way AI Create has always worked:
        the last look at a card belongs in the editor it will live in, not in a
        tree row. Off ("Add all now"), they go straight in, which is what this
        screen used to do without asking.

        The found half commits immediately EITHER WAY. Tagging and unsuspending
        notes you already have is not something the Add window can do, and it
        is the half you just spent the screen ticking."""
        # `_coverage is None` is a write-only run (Find unticked everywhere),
        # which has cards to commit even though nothing was searched.
        if not anki_utils.require_col():
            return
        if self._coverage is None and not self._run_plans:
            return
        if self._gen_active:
            # There is nowhere for cards to land after Apply any more, so
            # Apply waits for the writing rather than stranding it.
            tooltip("Cards are still being written — wait for them, or press "
                    "Stop, then Apply.", period=5000)
            return

        ticked = self._ticked()
        all_nids = sorted({nid for rec in ticked.values() for nid in rec["nids"]})
        batches = self._approved_written()
        n_new = sum(len(c) for _k, c in batches)
        if not all_nids and not n_new:
            tooltip("Nothing ticked and nothing approved — nothing to apply.")
            return
        # With nothing written there is no second route to choose between, so
        # the question of reviewing doesn't arise.
        review = bool(review and n_new)

        full, _partial, _empty = self._outcome_split()
        do_unsuspend = self._unsuspend_cb.isChecked()
        deck = ((self._deck.currentText().strip()
                 if self._deck is not None else "") or "Default")
        untagged = [kg_id for kg_id, rec in ticked.items()
                    if rec["nids"] and not self._tag_text(kg_id)]

        lines = ["Apply this whole pass?", ""]
        if all_nids:
            lines.append(
                f"• Tag {len(all_nids)} note(s) you already had, each under "
                "its own gap's tag"
                + (", and unsuspend them." if do_unsuspend else "."))
        if n_new and review:
            lines.append(
                f"• Open Anki's Add window on the {n_new} newly written "
                f"card(s) from {len(batches)} gap(s) — one at a time, you "
                "press Add on each.")
        elif n_new:
            lines.append(f"• Add {n_new} newly written card(s) to "
                         f"“{deck}” across {len(batches)} gap(s).")
        if untagged:
            lines.append(f"• {len(untagged)} gap(s) have an empty tag box — "
                         "their found cards are unsuspended but not tagged.")
        lines.extend(self._lec_confirm_lines(all_nids))
        lines.append("")
        if review:
            # Said plainly rather than repeating the old promise. Cards added
            # through Anki's own Add window are Anki's own undo entries, one
            # per card; the checkpoint below only covers the found half.
            lines.append("Existing card content is never modified. One Ctrl+Z "
                         "takes the tagging back out; each card you add in the "
                         "Add window undoes separately, as usual. Close that "
                         "window early and the cards you hadn't reached are "
                         "dropped — their gaps stay open.")
        else:
            lines.append("Existing card content is never modified, and one "
                         "Ctrl+Z takes the whole thing back out.")
        if not askUser("\n".join(lines), title="Find & write"):
            return

        # ONE checkpoint around both halves.
        if hasattr(mw, "checkpoint"):
            mw.checkpoint("Ankisstant: find & write")

        self._stage(pipeline.REVIEW, pipeline.DONE,
                    f"{len(all_nids) + n_new} kept")
        self._stage(pipeline.COMMIT, pipeline.RUNNING)
        # Derived ONCE, after the confirm (it has a side effect: a source you
        # typed sticks for this door) and used by both halves, so the found and
        # the written cards for a gap can't end up under different sources.
        source = self._source_tag()
        tagged = unsuspended = 0
        if all_nids:
            tagged, unsuspended = self._apply_found(
                ticked, all_nids, do_unsuspend, source)

        # Pressed once. The Add window may be open for minutes, and a second
        # Apply behind it would re-tag everything and re-queue every card.
        self._applied = True
        self._commit_btn.setEnabled(False)
        self._commit_btn.setText("Applied")
        if getattr(self, "_add_now_btn", None) is not None:
            self._add_now_btn.setEnabled(False)

        found = {"all_nids": all_nids, "tagged": tagged,
                 "unsuspended": unsuspended, "do_unsuspend": do_unsuspend,
                 "full": full}

        if not review:
            created_by_kg = self._apply_created(batches, deck, source)
            self._finish_apply(found, created_by_kg,
                               getattr(self, "_created_pairs", {}) or {})
            return

        def _added_up(added: dict, pairs: dict, discarded: int) -> None:
            self._created_pairs = pairs
            # Logged here rather than at hand-off, and per gap, because the
            # session log records what was CREATED. A card you scrolled past
            # in the Add window was never created.
            for kg_id, ids in added.items():
                self._log_session(self._job_for_kg(kg_id) or {}, ids)
            self._clear_job_batch()
            self._finish_apply(found, added, pairs, discarded=discarded)

        queued = self._hand_to_add_window(batches, deck, source, _added_up)
        if not queued:
            # Nothing could be turned into a note — usually a notetype profile
            # pointing at something that no longer exists, which resolve_notetype
            # has already said out loud.
            self._finish_apply(found, {}, {})

    def _finish_apply(self, found: dict, created_by_kg: dict, pairs: dict,
                      discarded: int = 0) -> None:
        """Close the gaps, tell the door, and say what happened.

        Reached either straight after a silent commit or minutes later, when
        the Add window is finally closed. Everything in it is deliberately
        late-bound to what ACTUALLY got added: `created_by_kg` is the cards
        that reached the collection, so a pass abandoned halfway closes the
        gaps it finished and leaves the rest open, which is exactly what the
        work queue then lets you pick back up.

        Fully Qt-guarded: the results window can be gone by the time the Add
        window closes, and a gap that got its cards must still be closed."""
        all_nids = list(found.get("all_nids") or [])
        full = list(found.get("full") or [])
        do_unsuspend = bool(found.get("do_unsuspend"))
        tagged = int(found.get("tagged") or 0)
        unsuspended = int(found.get("unsuspended") or 0)

        # The door's own commit step. AI Lecture attaches slide images here; a
        # door with nothing extra to do passes nothing.
        if self._on_committed is not None:
            try:
                self._on_committed(pairs or {}, all_nids)
            except Exception as e:
                # Never let a door's extra step undo a commit that worked.
                log.error(f"run view: the {self._door} door's commit step "
                          f"failed: {e}")

        # A gap closes if your deck turned out to cover it, or if cards were
        # just written for it. Everything else stays open on purpose.
        closed = self._close_gaps(sorted(set(
            list(full) + [k for k, ids in (created_by_kg or {}).items() if ids])))

        n_created = sum(len(v) for v in (created_by_kg or {}).values())
        bits = []
        if all_nids:
            bits.append(f"{len(all_nids)} found note(s) committed"
                        + (f" ({tagged} tagged)" if tagged != len(all_nids) else "")
                        + (f", {unsuspended} card(s) unsuspended"
                           if do_unsuspend else ""))
        if n_created:
            bits.append(f"{n_created} card(s) added")
        if discarded:
            bits.append(f"{discarded} not added — you closed the Add window")
        if self._lecture is None:
            bits.append(f"{closed} gap(s) closed")
        msg = "Done — " + ", ".join(bits) + "."
        try:
            self._stage(pipeline.COMMIT, pipeline.DONE,
                        f"{len(all_nids)} tagged · {n_created} added")
            self._set_status(msg)
        except RuntimeError:
            # The results window went away while the Add window was open. The
            # gaps are closed and the cards are in; there is nothing to redraw.
            pass
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

    def _commit_units(self, batches, deck: str, source: str = ""):
        """(kg_id, job, cards, notetype, ctx, deck_name) for each gap, ready to
        turn into notes.

        Lifted out of _apply_created because there are now two ways a written
        card reaches the collection — straight in, or one at a time through
        Anki's Add window — and a card you reviewed has to be filed exactly as
        a card you didn't. Everything that decides what the note IS lives here;
        the two routes differ only in how they put it there.

        `source` is this run's provenance tag, re-read from the box at commit
        time for the same reason the tag box is: the box sits on the results
        page, generation has already started by the time you can type in it,
        and the cards written for a gap must move with the cards found for it.
        Retyping it moved the found half and left the written half behind."""
        from .. import card_creator as cc
        from .mass_create import commit_ctx_for
        if not batches:
            return
        profile = self._gen_profile or cc._resolved_profile(
            tool_config("card_creator"),
            self._notetype.currentText().strip()
            if self._notetype is not None else "")
        nt = cc.resolve_notetype(profile)
        if nt is None:
            return
        for kg_id, cards in batches:
            job = self._job_for_kg(kg_id)
            # The gap's tag box governs BOTH halves. Re-read it now rather than
            # trusting what the job was enqueued with, so editing the tag after
            # generation moves the written cards along with the found ones —
            # which is the whole point of them sharing a row.
            tags = [t for t in (job.get("tags_raw") or []) if t]
            box = self._tag_text(kg_id)
            if box and box not in tags:
                tags.append(box)
            # Whatever the job was enqueued with is the source the box was
            # PREFILLED from; if it still says that, this is a no-op. If it
            # doesn't, the box wins — it's the one on screen.
            if source:
                # Drop any OTHER source tag the job was enqueued with rather
                # than adding a second one: a card has one provenance, and the
                # box is the one on screen. Matched on the "…::Sources::" root
                # so a source at a different depth can't slip past.
                root = source.split("::Sources::", 1)[0] + "::Sources::"
                tags = [t for t in tags
                        if not (t.startswith(root) and t != source)]
                if source not in tags:
                    tags.append(source)
            yield (kg_id, job, list(cards), nt,
                   commit_ctx_for(job, profile, tags),
                   (job.get("deck_name") or "").strip() or deck)

    def _clear_job_batch(self) -> None:
        """Drop this pass's generation batch from the job store.

        Only once the writing has actually finished: clearing a batch that
        still has jobs running would delete work in flight."""
        if self._gen_active:
            return
        try:
            from .. import create_jobs
            create_jobs.remove_batch(self._gen_batch_id)
        except Exception as e:
            log.warn(f"work through: couldn't clear the job batch: {e}")

    def _apply_created(self, batches, deck: str, source: str = "") -> dict:
        """Commit the ticked written cards straight in. No checkpoint of its
        own — the caller opened one around this and the found cards together.

        This is the "Add all now" route. The other one hands the same notes to
        Anki's Add window instead; see _hand_to_add_window."""
        created_by_kg: dict[str, list] = {}
        # kg_id -> [(nid, card), ...]. commit_cards adds one note per card in
        # order, so the zip is exact — and it is the only way a door can act on
        # the cards it just wrote (AI Lecture staples each card's source slide
        # onto it, and needs to know WHICH card got which slide).
        self._created_pairs = {}
        for kg_id, job, cards, nt, cctx, deck_name in self._commit_units(
                batches, deck, source):
            # checkpoint=False: _on_apply_everything already opened one around
            # both halves of this screen.
            cplan = pipeline.CommitPlan(
                label="Ankisstant: find & write", new_cards=list(cards),
                deck_name=deck_name, commit_ctx=cctx, notetype=nt)
            res = commit.run(cplan, checkpoint=False)
            if res.errors:
                log.error("work through: committing "
                          f"'{job.get('title')}': " + "; ".join(res.errors))
                continue
            ids = list(cplan.created_ids)
            created_by_kg[kg_id] = ids
            self._created_pairs[kg_id] = list(zip(ids, cards))
            self._log_session(job, ids)
        self._clear_job_batch()
        return created_by_kg

    def _log_session(self, job: dict, ids: list) -> None:
        try:
            from .. import card_creator as cc
            cc._append_session(
                mode="topic", source=f"work-through:{self._gen_batch_id}",
                topic=str(job.get("title") or ""),
                cards_proposed=len(job.get("cards") or []),
                cards_created=len(ids), card_ids=list(ids))
        except Exception as e:
            log.warn(f"work through: session log failed: {e}")

    # ── the Add-window route ─────────────────────────────────────────────────

    def _filler_for(self, nt, card: dict, cctx, deck_id: int):
        """One card's filler for Anki's Add window.

        A closure rather than a prepared note, because the note has to be built
        at the moment the window asks for it: Anki's editor takes ownership of
        the note it is handed, so a batch of notes built up front and held
        would be a batch of notes the editor has already had its way with."""
        from .. import card_creator as cc

        def _fill(ac) -> None:
            note = mw.col.new_note(nt)
            cc.set_fields(note, card, cctx)
            try:
                ac.set_note(note, deck_id=deck_id)
            except Exception as e:
                log.warn(f"work through: set_note fell back to the editor: {e}")
                ac.editor.set_note(note)
            # Anki's sticky tags can overwrite what set_fields put on the note,
            # and the tag WIDGET is what the Add button actually reads.
            try:
                if hasattr(ac.editor, "tags") and hasattr(ac.editor.tags, "setText"):
                    ac.editor.tags.setText(" ".join(note.tags))
            except Exception as e:
                log.warn(f"work through: couldn't refresh the tag box: {e}")

        return _fill

    def _hand_to_add_window(self, batches, deck: str, source: str,
                            then) -> int:
        """Load the written cards into Anki's Add window, one at a time.

        The same notes _apply_created would have committed — same notetype,
        same per-gap tags, same deck, same attached images — built by the same
        cc.set_fields. The only difference is that you see each one in the
        editor and press Add yourself, which is what AI Create has always done
        and what this screen was quietly skipping.

        NOTHING IS CLOSED UP FRONT. A gap closes on a card reaching the
        collection, not on one being offered, and closing the window early is a
        normal way to stop — so `then(added, pairs, discarded)` is called once
        the run is over, with what actually got added."""
        from .. import card_creator as cc
        units = list(self._commit_units(batches, deck, source))
        if not units:
            return 0
        nt = units[0][3]
        fillers, owners, in_order = [], [], []
        for kg_id, _job, cards, unit_nt, cctx, deck_name in units:
            try:
                deck_id = mw.col.decks.id(deck_name or "Default")
            except Exception as e:
                log.error(f"work through: couldn't resolve deck "
                          f"'{deck_name}': {e}")
                continue
            for card in cards:
                fillers.append(self._filler_for(unit_nt, card, cctx, deck_id))
                owners.append(kg_id)
                in_order.append(card)
        if not fillers:
            return 0

        added: dict[str, list] = {}
        pairs: dict[str, list] = {}

        def _on_added(idx: int, note) -> None:
            if not (0 <= idx < len(owners)):
                return
            added.setdefault(owners[idx], []).append(note.id)
            pairs.setdefault(owners[idx], []).append((note.id, in_order[idx]))

        def _on_done(discarded: int) -> None:
            try:
                then(added, pairs, discarded)
            except Exception as e:
                log.error(f"work through: finishing the add run failed: {e}")

        try:
            start_deck = mw.col.decks.id(deck or "Default")
        except Exception:
            start_deck = mw.col.decks.selected()
        cc._start_add_queue(fillers, {"nt": nt, "deck_id": start_deck},
                            on_added=_on_added, on_done=_on_done)
        return len(fillers)

    def _tag_text(self, kg_id: str) -> str:
        edit = self._tag_edits.get(kg_id)
        return edit.text().strip() if edit is not None else ""

    def _close_gaps(self, kg_ids: list[str]) -> int:
        """Mark fully-covered gaps done. Best-effort: a store failure must never
        undo the tagging and unsuspending that already succeeded."""
        if self._lecture is not None:
            return 0          # a lecture's sections aren't knowledge gaps
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
            # Gaps that came from a Things to-do get ticked off there too.
            # Debounced, so a twelve-gap close is one write, not twelve.
            knowledge_gaps.maybe_sync_to_things()
        except Exception:
            pass
        return closed

    # ── hand-off ─────────────────────────────────────────────────────────────

    def _uncovered_points(self) -> dict[str, list]:
        """{kg_id: [PointMatch, ...]} for the points with nothing ticked under
        them. These — not the gap title — are what AI Create actually needs:
        'Graves + Tx' with 5 of 7 points already covered wants cards for the
        remaining 2, not another pass at the whole disease."""
        out: dict[str, list] = {}
        if self._coverage is None:
            # Write-only run: nothing was searched, so there is no coverage to
            # subtract. _create_payload supplies every point instead.
            return out
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
        self._add_now_btn.setVisible(False)
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
        resurface_at_home(self)

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
            # Two answers, not three. There used to be "close and keep the
            # cards", which parked them in AI Create — and nobody goes back
            # for cards they didn't take the first time; they just turned up
            # later looking like gaps queued from nowhere. Not applied = gone.
            if pending and not askUser(
                    f"{pending} written card(s) haven\u2019t been added, and the "
                    "found cards on this screen aren\u2019t committed either.\n\n"
                    "Close anyway? The written cards are thrown away.",
                    defaultno=True, title="Close without adding?"):
                ev.ignore()
                return
        if not self._applied and self._gen_batch_id:
            # Whatever was written and not applied goes now — nothing else
            # will ever show it.
            try:
                from .. import create_jobs
                create_jobs.remove_batch(self._gen_batch_id)
            except Exception as e:
                log.warn(f"find & write: couldn't discard the batch: {e}")
        self._closed = True
        try:
            self._poll.stop()
        except RuntimeError:
            pass
        ev.accept()
        # EMIT finished. Overriding closeEvent without calling up means Qt's own
        # QDialog::closeEvent — the thing that calls reject() and so emits
        # finished — never runs, and reject() here routes back into close(), so
        # the signal could not fire by any route. open_unified's docstring has
        # been promising callers they can watch this window finish; for the
        # work-through queue that promise was silently false, and the queue it
        # was supposed to empty grew to sixteen. done() only hides and emits,
        # so it cannot re-enter the close path we are already in.
        try:
            self.done(QDialog.DialogCode.Rejected)
        except Exception as e:
            log.warn(f"run view: couldn't signal the close: {e}")
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
        self._tree.headerItem().setToolTip(1,
                         "The words looked up in your deck, comma-separated. "
                         "Edit freely — a term that returns nothing is the "
                         "single biggest cause of a false gap.")
        tips_on_checkbox_only(self._tree)
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
        terms the user just deleted would be worse than saying so.

        A point whose WORDING changed is flagged for reclassification and its
        levels are cleared. {system, subsystem, topic} came from the AI's read
        of the old wording and drive the tag the found cards are filed under —
        so rewriting a point to be about something else used to search the new
        terms and file the results under the old topic. The re-derivation
        happens off the main thread in _reclassify_points, not here: this
        dialog closes instantly and must go on doing so."""
        keep: dict[int, list] = {}
        termless = 0
        for plan, pt, item in self._items:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            text = " ".join(item.text(0).split())
            if not text:
                continue           # edited down to nothing = dropped
            if text != pt.concept:
                pt.system = pt.subsystem = pt.topic = ""
                pt._reclassify = True
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
    the back, the actions that change the card wholesale, and a picture if you
    want one. Re-rolling a card repeatedly belongs in AI Create, where the
    whole batch is the subject."""

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

        # Images, on request. Nothing is fetched for a card nobody asked about
        # \u2014 see _image_menu on the results tree, which offers the same three
        # routes on the row itself for when you don\u2019t want the dialog at all.
        img_row = QHBoxLayout()
        for text, tip, how in (
                ("\U0001f4f7  Attach\u2026", "Attach an image file to just this card.",
                 "file"),
                ("\U0001f50d  Find online\u2026",
                 "Search the medical image sources and attach a result to just "
                 "this card.", "find"),
                ("\U0001f310  Browse\u2026",
                 "Browse for an image inside Anki and attach it to just this "
                 "card.", "browse")):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, h=how: self._on_image(h))
            img_row.addWidget(b)
        self._img_clear = QPushButton("Clear")
        self._img_clear.setAutoDefault(False)
        self._img_clear.setToolTip("Take every image back off this card.")
        self._img_clear.clicked.connect(lambda: self._set_images([]))
        img_row.addWidget(self._img_clear)
        self._img_lbl = QLabel("")
        self._img_lbl.setStyleSheet("color: gray; font-size: 11px;")
        img_row.addWidget(self._img_lbl, 1)
        col.addLayout(img_row)

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
        self._refresh_images()
        theme_dialog(self)

    # -- images -------------------------------------------------------------

    def _on_image(self, how: str) -> None:
        got = _pick_card_images(self, how, _image_seed(self._card))
        if got:
            self._set_images(_merged_images(self._card.get("_image_paths"), got))

    def _set_images(self, paths: list) -> None:
        keep = _merged_images(paths, [])
        if keep:
            self._card["_image_paths"] = keep
        else:
            self._card.pop("_image_paths", None)
        self._refresh_images()

    def _refresh_images(self) -> None:
        """The count in words, not thumbnails. This dialog is for reading the
        card; a row of previews would push the text it exists to show off the
        bottom, and the results tree says the same thing on the row."""
        n = len(self._card.get("_image_paths") or [])
        self._img_lbl.setText(
            f"{n} image(s) attached \u2014 added when the card is."
            if n else "No images on this card.")
        self._img_clear.setEnabled(bool(n))

    def _on_regen(self) -> None:
        from aqt.qt import QInputDialog
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
                                     "", pipeline.dial_for(pipeline.WRITE))
        except Exception as e:
            log.error(f"work through: rewrite failed: {e}")
            self._status.setText(f"Couldn't rewrite it: {e}")
            return
        if not new:
            self._status.setText("The AI didn't return a replacement.")
            return
        self._apply_new(new, "Rewritten \u2014 read it before you keep it.")

    def _apply_new(self, new: dict, msg: str) -> None:
        # Keep the rewrite's flags (one_by_one, style…) — values() rebuilds the
        # card from self._card, so a flag left only on `new` never commits.
        self._card.update({k: v for k, v in new.items()
                           if k not in ("front", "extra", "back")})
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


class _ApplyImagesDialog(QDialog):
    """Pick which other written cards get this card's images.

    A picker rather than a blanket "apply to this gap", because the case that
    prompted it cuts both ways: one diagram answers several cards of a gap,
    and sometimes it answers a card of the gap next door. So the siblings
    start ticked \u2014 that is the common half \u2014 and everything else in the
    pass is listed under its own gap, one click away."""

    ROLE_ROW = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, paths, rows, same_kg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Put these images on other cards")
        self.setMinimumSize(640, 480)
        self._rows = list(rows)

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(6)
        head = QLabel(
            f"The {len(paths)} image(s) on that card will be added to every "
            "card you tick below. Nothing is copied into your media folder "
            "until the cards themselves are added.")
        head.setWordWrap(True)
        head.setStyleSheet("color: gray; font-size: 11px;")
        col.addWidget(head)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        col.addWidget(self._tree, 1)

        groups: dict = {}
        for idx, (_leaf, card, kg_id, title) in enumerate(self._rows):
            g = groups.get(kg_id)
            if g is None:
                g = QTreeWidgetItem(self._tree)
                g.setText(0, title + ("    (this gap)" if kg_id == same_kg else ""))
                # Not checkable: ticking a gap would read as "and its future
                # cards too", which this cannot honour \u2014 the pass is written.
                g.setFlags(Qt.ItemFlag.ItemIsEnabled)
                groups[kg_id] = g
            it = QTreeWidgetItem(g)
            try:
                front = anki_utils.strip_html(str(card.get("front") or ""))
            except Exception:
                front = str(card.get("front") or "")
            it.setText(0, _short(front, 200))
            it.setData(0, self.ROLE_ROW, idx)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if kg_id == same_kg
                             else Qt.CheckState.Unchecked)
            n_has = len(card.get("_image_paths") or [])
            if n_has:
                it.setToolTip(0, f"This card already has {n_has} image(s); "
                                 "these are added alongside them.")
        self._tree.expandAll()

        row = QHBoxLayout()
        for text, state in (("Tick all", Qt.CheckState.Checked),
                            ("Tick none", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, st=state: self._set_all(st))
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("Apply to the ticked cards")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        col.addLayout(row)
        theme_dialog(self)

    def _set_all(self, state) -> None:
        for g in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(g)
            for i in range(top.childCount()):
                top.child(i).setCheckState(0, state)

    def chosen(self) -> list:
        """The ticked rows, as (leaf, card, kg_id, title) tuples."""
        out = []
        for g in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(g)
            for i in range(top.childCount()):
                it = top.child(i)
                if it.checkState(0) != Qt.CheckState.Checked:
                    continue
                idx = it.data(0, self.ROLE_ROW)
                if isinstance(idx, int) and 0 <= idx < len(self._rows):
                    out.append(self._rows[idx])
        return out


def open_unified(kgs: list[dict], parent=None):
    """Search AND create, on ONE screen: the deck is searched for every gap, the
    points it can't cover are written as cards into the bottom of the same
    results page, and one button commits both under one checkpoint. See
    BulkSearchDialog._build_results_page.

    Returns the dialog, or None if it couldn't open. The caller needs it to
    know when the pass is OVER — the window is modeless, so returning from
    this function means it has appeared, not that anything has been done with
    it. The work-through queue hangs its emptying off dialog.finished.

    `kgs` are full KG records from tools/kg/store.py."""
    kgs = [k for k in (kgs or []) if isinstance(k, dict)
           and str(k.get("title") or "").strip()]
    if not kgs:
        tooltip("No knowledge gaps to search.")
        return None
    dlg = BulkSearchDialog(kgs, parent=parent)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    # DELIBERATELY does not read on open. It used to, on the theory that an
    # unread table has nothing worth deciding about — but that spends an AI call
    # the moment the window appears, and it skips straight past the one screen
    # where the notes and the tick boxes can still be corrected. Opening a
    # window is not consent to spend tokens. Press "Read gaps with AI".
    return dlg


