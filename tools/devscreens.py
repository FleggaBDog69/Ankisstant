# Developer screenshots — every page, every Settings tab, most dialogs, as PNGs.
#
# Hidden on purpose: no menu, no button, no setting. It exists so a session
# working on the code (Claude Code, mostly) can SEE the UI instead of reasoning
# about layouts from source. Drop a file and wait:
#
#   touch user_files/dev_screens.request
#
# The patch watcher (tools/kg/patch_inbox.py, every 2s) notices it, deletes it,
# and photographs the UI. Output: user_files/screens/<time>/NN-name.png, an
# index.txt listing them, and user_files/dev_screens.done holding the folder.
#
# OFF-SCREEN. Everything is built in windows marked WA_DontShowOnScreen — laid
# out and painted, never put on the display — so a run doesn't take over the
# screen of whoever is using Anki. The pages are singletons, so for the run
# they're borrowed into a hidden copy of the main window and handed back to
# the real one (if it's open) afterwards.
#
# Each window is taken at its normal size and again narrow, where overflow
# shows, and each scroll area is also grabbed WHOLE (`-full`), since the
# visible part of a long Settings tab hides most of it. index.txt ends with a
# PROBLEMS list found by measuring, not looking: text wider than the widget
# showing it, and widgets that run past the edge of what contains them.
#
# Read-only. Every dialog is closed with reject() so nothing is saved, and the
# Find & write window is built but never run (opening spends no tokens).
#
# Reloads itself on each request, so changes to THIS file don't need an Anki
# restart (changes to anything it photographs still do).

from __future__ import annotations

import importlib
import os
import sys
import time
import traceback

from aqt import mw
from aqt.qt import (
    QAbstractButton, QAbstractScrollArea, QComboBox, QLabel, QPoint,
    QScrollArea, Qt, QTimer, QVBoxLayout, QWidget,
)

from ..core import log

NARROW = (900, 640)
STEP_MS = 250            # let layouts settle and paint between each shot

# Survives importlib.reload (which re-runs this file in the same namespace).
# A start time, not a flag: a run that dies without finishing must not block
# every later request until Anki restarts.
_running = globals().get("_running", 0.0)
STALE_S = 300


def _user_files() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "user_files")


def request_path() -> str:
    return os.path.join(_user_files(), "dev_screens.request")


def done_path() -> str:
    return os.path.join(_user_files(), "dev_screens.done")


def check() -> None:
    """Called from the patch watcher's tick. Cheap when there's no request."""
    if not os.path.exists(request_path()):
        return
    if _running and time.time() - _running < STALE_S:
        return
    try:
        os.remove(request_path())
    except OSError:
        return
    try:
        mod = importlib.reload(sys.modules[__name__])
    except Exception as e:
        log.error(f"dev screens: reload failed, using the loaded copy: {e}")
        mod = sys.modules[__name__]
    mod._begin()


def _begin() -> None:
    global _running
    _running = time.time()
    try:
        _Run().start()
    except Exception as e:
        _running = 0.0
        log.error(f"dev screens failed to start: {e}\n{traceback.format_exc()}")


def _offscreen(w: QWidget, size=None) -> QWidget:
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    if size:
        w.resize(max(size[0], w.minimumWidth()), max(size[1], w.minimumHeight()))
    w.show()
    return w


# ── measuring ────────────────────────────────────────────────────────────────

def _describe(w: QWidget) -> str:
    text = ""
    for attr in ("text", "currentText", "title", "placeholderText"):
        fn = getattr(w, attr, None)
        if callable(fn):
            try:
                text = str(fn() or "")
            except Exception:
                text = ""
            if text:
                break
    text = " ".join(text.replace("&&", "&").split())
    if len(text) > 50:
        text = text[:47] + "…"
    where = []
    p = w.parentWidget()
    while p is not None and len(where) < 2:
        title = getattr(p, "title", None)
        if callable(title) and title():
            where.append(title())
        p = p.parentWidget()
    loc = f" in [{' / '.join(where)}]" if where else ""
    return f"{type(w).__name__} '{text}'{loc}"


def _clip_parent(w: QWidget, top: QWidget) -> QWidget:
    """What actually clips w: the nearest scroll viewport, else the window."""
    p = w.parentWidget()
    while p is not None and p is not top:
        gp = p.parentWidget()
        if isinstance(gp, QAbstractScrollArea) and gp.viewport() is p:
            return p
        p = gp
    return top


def _needed_width(w: QWidget) -> int:
    if isinstance(w, QComboBox):
        fm = w.fontMetrics()
        return fm.horizontalAdvance(w.currentText()) + 34   # arrow + padding
    return w.sizeHint().width()


def audit(top: QWidget) -> list[str]:
    out: list[str] = []
    flagged: set[int] = set()
    for w in top.findChildren(QWidget):
        try:
            if not w.isVisibleTo(top) or w.width() <= 2 or w.height() <= 2:
                continue
            if any(id(a) in flagged for a in _ancestors(w, top)):
                continue
            clip = _clip_parent(w, top)
            # Horizontal scroll is never wanted in these windows; anything
            # wider than what's clipping it is cut off.
            x = w.mapTo(clip, QPoint(0, 0)).x() if clip is not w else 0
            over = x + w.width() - clip.width()
            if over > 2 and not isinstance(w, QAbstractScrollArea) \
                    and w.parentWidget() is not None \
                    and not isinstance(w.parentWidget(), QAbstractScrollArea):
                out.append(f"runs {over}px past the right edge: {_describe(w)}")
                flagged.add(id(w))
                continue
            if isinstance(w, QLabel):
                if w.wordWrap() or w.pixmap() is not None and not w.pixmap().isNull() \
                        or not w.text():
                    continue
            elif not isinstance(w, (QAbstractButton, QComboBox)):
                continue
            need = _needed_width(w)
            if need > w.width() + 3:
                out.append(f"text cut ({need}px wanted, {w.width()}px given): {_describe(w)}")
        except RuntimeError:
            continue
    return out


def _ancestors(w: QWidget, top: QWidget):
    p = w.parentWidget()
    while p is not None and p is not top:
        yield p
        p = p.parentWidget()


# ── the run ──────────────────────────────────────────────────────────────────

class _Run:
    def __init__(self):
        self.out = os.path.join(_user_files(), "screens",
                                time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.out, exist_ok=True)
        self.n = 0
        self.steps: list = []
        self.notes: list[str] = []
        self.problems: list[str] = []
        self.host = None
        self.body = None
        self.step = ""

    # ── plumbing ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._plan()
        QTimer.singleShot(STEP_MS, self._next)

    def add(self, name: str, fn) -> None:
        self.steps.append((name, fn))

    def now(self, *steps) -> None:
        """Run these next, ahead of whatever is already queued."""
        self.steps[0:0] = list(steps)

    def _next(self) -> None:
        global _running
        if not self.steps:
            try:
                self._finish()
            except Exception as e:
                log.error(f"dev screens: finish failed: {e}\n{traceback.format_exc()}")
            _running = 0.0
            return
        name, fn = self.steps.pop(0)
        self.step = f"{name} ({len(self.steps)} left)"
        self._write_index()          # names the step, so a hang says where
        try:
            fn()
        except Exception as e:
            self.notes.append(f"{name}: FAILED — {e}")
            log.error(f"dev screens: {name} failed: {e}\n{traceback.format_exc()}")
        self._write_index()          # so a stall still leaves a record
        QTimer.singleShot(STEP_MS, self._next)

    def shot(self, widget, name: str, *, full: bool = True) -> None:
        if widget is None:
            self.notes.append(f"{name}: no widget")
            return
        self.n += 1
        fname = f"{self.n:02d}-{name}.png"
        widget.grab().save(os.path.join(self.out, fname))
        self.notes.append(f"{fname}  {widget.width()}x{widget.height()}")
        for p in audit(widget):
            self.problems.append(f"{fname}: {p}")
        if not full:
            return
        # Whatever a scroll area is hiding below the fold.
        k = 0
        for sa in widget.findChildren(QScrollArea):
            try:
                inner = sa.widget()
                if inner is None or not sa.isVisibleTo(widget):
                    continue
                if inner.height() <= sa.viewport().height() + 4:
                    continue
                if inner.width() < 300:          # small lists, not pages
                    continue
            except RuntimeError:
                continue
            k += 1
            fn = f"{self.n:02d}-{name}-full{k if k > 1 else ''}.png"
            self._grab_on_window(inner, widget).save(os.path.join(self.out, fn))
            self.notes.append(f"{fn}  {inner.width()}x{inner.height()}  (whole scroll area)")

    @staticmethod
    def _grab_on_window(inner: QWidget, window: QWidget):
        """An inner widget grabbed on its own has no background (it's the
        window's), so it came out pale with washed-out text. Paint it onto the
        window's own colour first."""
        from aqt.qt import QPixmap, QPainter
        ratio = window.devicePixelRatioF()
        pm = QPixmap(int(inner.width() * ratio), int(inner.height() * ratio))
        pm.setDevicePixelRatio(ratio)
        # The theme paints backgrounds with a stylesheet, which the palette
        # doesn't know about — so take the colour from the painted window.
        try:
            img = window.grab().toImage()
            pm.fill(img.pixelColor(3, img.height() - 3))
        except Exception:
            pm.fill(window.palette().window().color())
        painter = QPainter(pm)
        inner.render(painter)
        painter.end()
        return pm

    def _write_index(self) -> None:
        try:
            with open(os.path.join(self.out, "index.txt"), "w") as f:
                f.write("\n".join(self.notes) + "\n")
                if self.step:
                    f.write(f"\n[running: {self.step}]\n")
                f.write(f"\nPROBLEMS ({len(self.problems)})\n")
                f.write("\n".join(self.problems) + "\n")
        except OSError:
            pass

    # ── what gets taken ──────────────────────────────────────────────────────

    def _plan(self) -> None:
        try:
            from ..core.config import get_window_geometry
            g = get_window_geometry()
            normal = (int(g.get("width") or 1100), int(g.get("height") or 760))
        except Exception:
            normal = (1100, 760)
        for label, size in (("", normal), ("narrow-", NARROW)):
            self.add(f"{label}host", lambda s=size: self._make_host(s))
            for name, fn in (
                ("gaps", lambda: self._gaps_page(empty=True)),
                ("gap-open", self._open_a_gap),
                ("groups", lambda: self._groups(pick=False)),
                ("group-picked", lambda: self._groups(pick=True)),
                ("lecture", lambda: self.body.show_page("lecture")),
                ("qbank", lambda: self.body.show_page("qbank")),
            ):
                self.add(label + name, fn)
                self.add(label + name, lambda n=label + name: self.shot(self.host, n))
        # Settings opens at its minimum size, so a "narrow" copy would be the
        # same picture twice; the -full shots show what's below the fold.
        self.add("settings", lambda: self._settings(None, ""))
        self.add("work-through", self._work_through)
        self.add("dialogs", self._dialogs)

    def _make_host(self, size) -> None:
        from ..ui import main_window as mwnd
        from ..core import synapse
        self._drop_host()
        host = QWidget(None, Qt.WindowType.Window)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        self.body = mwnd.AnkisstantBody(compact=False)
        lay.addWidget(self.body)
        try:
            synapse.apply_stylesheet(host)
        except Exception:
            pass
        self.host = _offscreen(host, size)

    def _drop_host(self) -> None:
        """Hand the borrowed page singletons back before the copy is deleted,
        or deleting it would delete them too."""
        if self.host is None:
            return
        # Everything the copy's body was handed by a tool's get_panel(): those
        # are the module-level singletons, and they must outlive this copy.
        for w in list(getattr(self.body, "_panel_cache", {}).values()):
            try:
                w.setParent(None)
            except RuntimeError:
                pass
        self.host.hide()
        self.host.deleteLater()
        self.host = self.body = None

    def gaps_panel(self):
        from . import knowledge_gaps as kg
        return kg._panel

    def _open_gaps(self):
        from .kg import store
        return [g for g in store.load_all()
                if g.get("status") != "done" and not g.get("dismissed")]

    def _gaps_page(self, *, empty: bool) -> None:
        self.body.show_page("knowledge_gaps")
        p = self.gaps_panel()
        if p is not None:
            p._set_view("gaps")
            if empty:
                try:
                    p._detail.load(None)
                except Exception:
                    pass

    def _open_a_gap(self) -> None:
        p = self.gaps_panel()
        gap = next(iter(self._open_gaps()), None)
        if p is not None and gap is not None:
            p.jump_to_gap(gap["id"])

    def _groups(self, *, pick: bool) -> None:
        self.body.show_page("knowledge_gaps")
        p = self.gaps_panel()
        if p is None:
            return
        p._set_view("groups")
        lst = p._groups_view._list
        lst.clearSelection()
        if pick and lst.count():
            lst.setCurrentRow(0)

    def _settings(self, size, label: str) -> None:
        from ..ui.settings import SettingsDialog
        dlg = _offscreen(SettingsDialog(None), size)
        tabs = dlg._tabs
        steps = []
        for i in range(tabs.count()):
            name = "settings-" + label + "".join(
                c if c.isalnum() else "-" for c in tabs.tabText(i).lower()).strip("-")
            steps.append(("settings", lambda i=i: tabs.setCurrentIndex(i)))
            steps.append(("settings", lambda n=name: self.shot(dlg, n)))
        steps.append(("settings", dlg.reject))       # never saves
        self.now(*steps)

    def _work_through(self) -> None:
        from .kg.bulk_search import BulkSearchDialog
        gaps = self._open_gaps()[:2]
        if not gaps:
            self.notes.append("work-through: no open gaps to open it on")
            return
        steps = []
        for label, size in (("", (1300, 840)), ("narrow-", NARROW)):
            box = {}
            steps += [
                ("work-through", lambda b=box, s=size: b.setdefault(
                    "d", _offscreen(BulkSearchDialog(gaps), s))),
                ("work-through", lambda b=box, l=label:
                    self.shot(b["d"], f"{l}work-through-plan")),
                ("work-through", lambda b=box: b["d"].close()),
            ]
        self.now(*steps)

    def _dialogs(self) -> None:
        """The smaller windows, each built off-screen, shot, and cancelled."""
        from . import knowledge_gaps as kgm
        from ..ui import settings as st
        from .qbank.session_dialog import _SessionDialog
        from .qbank.browser import _QuestionCountDialog
        from ..core.config import load_config
        cfg = load_config()
        tools = cfg.get("tools", {})
        first = lambda xs: (xs or [None])[0]          # noqa: E731
        kg_cfg = tools.get("knowledge_gaps", {})
        kg_type = first(kg_cfg.get("types"))
        from .kg import store
        try:
            groups = list(store.groups())
        except Exception:
            groups = []
        qb = tools.get("qbank", {})
        makers = [
            ("add-kg", lambda: kgm.AddKGDialog()),
            ("group-assign", lambda: kgm._GroupAssignDialog(groups, "", 3)),
            ("group-edit", lambda: kgm._GroupEditDialog(first(groups) or "Group", 4, 12, groups)),
            ("group-manager", lambda: kgm._GroupManagerDialog(self.gaps_panel())),
            ("bulk-import", lambda: kgm._BulkImportDialog()),
            ("kg-type-editor", lambda: st._TypeEditorDialog(None, kg_type)),
            ("kg-field-editor", lambda: st._FieldEditorDialog(
                None, first((kg_type or {}).get("fields")))),
            ("notetype-profile", lambda: st._NotetypeProfileDialog(
                None, first(tools.get("card_creator", {}).get("notetypes")))),
            ("qbank-edit", lambda: st._QBankEditDialog(None, first(qb.get("platforms")))),
            ("period-edit", lambda: st._PeriodEditDialog(None, first(qb.get("target_periods")))),
            ("exam-date-edit", lambda: st._ExamDateEditDialog(None, first(qb.get("exam_dates")))),
            ("grounding-sources", lambda: st._GroundingSourcesDialog(
                list((tools.get("card_creator", {}).get("grounding") or {}).get("sources") or []))),
            ("qbank-session", lambda: _SessionDialog()),
            ("qbank-question-count", lambda: _QuestionCountDialog("QBank")),
            # Not the setup wizard: its shot is taken, then the run never
            # comes back from it (12 Oct 2026, not chased). It's a first-run
            # screen; check it by hand when it changes.
        ]
        steps = []
        for name, make in makers:
            box = {}

            def build(make=make, box=box, name=name):
                d = make()
                d.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                box["d"] = _offscreen(d)

            def snap(box=box, name=name):
                if "d" in box:
                    self.shot(box["d"], "dlg-" + name)

            def close(box=box):
                if "d" in box:
                    box["d"].reject()

            steps += [(name, build), (name, snap), (name, close)]
        self.now(*steps)

    def _finish(self) -> None:
        self._drop_host()
        # Give the borrowed pages back to the real window, if one is open.
        try:
            from ..ui import main_window as mwnd
            real = mwnd._current
            if real is not None and mwnd._is_live(real):
                key = mwnd._current_tool_key(real) or "knowledge_gaps"
                for page in mwnd.PAGES:
                    real.show_page(page)
                real.show_page(key)
        except Exception as e:
            log.error(f"dev screens: couldn't hand the pages back: {e}")
        self.step = ""
        self._write_index()
        with open(done_path(), "w") as f:
            f.write(self.out + "\n")
        log.info(f"dev screens: {self.n} screenshot(s), "
                 f"{len(self.problems)} problem(s) in {self.out}")
