# Developer screenshots — every page, every Settings tab, as PNGs.
#
# Hidden on purpose: no menu, no button, no setting. It exists so a session
# working on the code (Claude Code, mostly) can SEE the UI instead of reasoning
# about layouts from source. Drop a file and wait:
#
#   touch user_files/dev_screens.request
#
# The patch watcher (tools/kg/patch_inbox.py, every 2s) notices it, deletes it,
# and walks the UI: the Gaps page (empty, and with a gap open), the Groups
# view (empty, and with a group picked), AI Lecture, AI QBank, every Settings
# tab, and a Find & write plan page on two open gaps. Each is taken at the
# window's own size and again at a narrow 900×640, which is where overflow
# shows. Output: user_files/screens/<time>/NN-name.png, then
# user_files/dev_screens.done holding that folder's path.
#
# Read-only. Settings is closed with reject() so nothing is saved, the
# work-through is opened but never read (opening doesn't spend tokens) and is
# closed again, and the window goes back to the page it was on.

from __future__ import annotations

import os
import time

from aqt import mw
from aqt.qt import QTimer

from ..core import log

NARROW = (900, 640)
STEP_MS = 300            # let layouts settle and paint between each shot


def _user_files() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "user_files")


def request_path() -> str:
    return os.path.join(_user_files(), "dev_screens.request")


def done_path() -> str:
    return os.path.join(_user_files(), "dev_screens.done")


_running = False


def check() -> None:
    """Called from the patch watcher's tick. Cheap when there's no request."""
    global _running
    if _running or not os.path.exists(request_path()):
        return
    try:
        os.remove(request_path())
    except OSError:
        return
    _running = True
    try:
        _Run().start()
    except Exception as e:
        _running = False
        log.error(f"dev screens failed to start: {e}")


class _Run:
    def __init__(self):
        self.out = os.path.join(_user_files(), "screens",
                                time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.out, exist_ok=True)
        self.n = 0
        self.steps: list = []
        self.notes: list[str] = []
        self._restore_page = None
        self._restore_size = None

    # ── plumbing ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from ..ui import main_window as mwnd
        mwnd.open_main_window()
        self.mwnd = mwnd
        body, host = mwnd._current, mwnd._host
        self._restore_page = mwnd._current_tool_key(body) if body else None
        if host is not None and not hasattr(host, "setFloating"):
            self._restore_size = (host.width(), host.height())
        self._plan()
        QTimer.singleShot(STEP_MS, self._next)

    def _next(self) -> None:
        global _running
        if not self.steps:
            self._finish()
            _running = False
            return
        name, fn = self.steps.pop(0)
        try:
            fn()
        except Exception as e:
            self.notes.append(f"{name}: FAILED — {e}")
            log.error(f"dev screens: {name} failed: {e}")
        QTimer.singleShot(STEP_MS, self._next)

    def shot(self, widget, name: str) -> None:
        if widget is None:
            self.notes.append(f"{name}: no widget")
            return
        self.n += 1
        fname = f"{self.n:02d}-{name}.png"
        widget.grab().save(os.path.join(self.out, fname))
        self.notes.append(f"{fname}  {widget.width()}x{widget.height()}")

    def host(self):
        return self.mwnd._host

    def body(self):
        return self.mwnd._current

    def gaps_panel(self):
        from . import knowledge_gaps as kg
        return kg._panel

    def resize_host(self, size) -> None:
        h = self.host()
        if h is not None and not hasattr(h, "setFloating") and size:
            h.resize(*size)

    # ── what gets taken ──────────────────────────────────────────────────────

    def _plan(self) -> None:
        add = lambda name, fn: self.steps.append((name, fn))   # noqa: E731
        for label, size in (("", None), ("narrow-", NARROW)):
            add(f"{label}size", lambda s=size: self.resize_host(s or self._restore_size))
            add(f"{label}gaps", lambda: self._gaps_page(empty=True))
            add(f"{label}gaps", lambda l=label: self.shot(self.host(), f"{l}gaps"))
            add(f"{label}gap-open", self._open_a_gap)
            add(f"{label}gap-open", lambda l=label: self.shot(self.host(), f"{l}gap-open"))
            add(f"{label}groups", lambda: self._groups(pick=False))
            add(f"{label}groups", lambda l=label: self.shot(self.host(), f"{l}groups"))
            add(f"{label}group-picked", lambda: self._groups(pick=True))
            add(f"{label}group-picked", lambda l=label: self.shot(self.host(), f"{l}group-picked"))
            for page in ("lecture", "qbank"):
                add(f"{label}{page}", lambda p=page: self.body().show_page(p))
                add(f"{label}{page}", lambda p=page, l=label: self.shot(self.host(), f"{l}{p}"))
        add("size", lambda: self.resize_host(self._restore_size))
        add("settings", self._settings)
        add("work-through", self._work_through)

    def _gaps_page(self, *, empty: bool) -> None:
        self.body().show_page("knowledge_gaps")
        p = self.gaps_panel()
        if p is not None:
            p._set_view("gaps")
            if empty:
                try:
                    p._detail.load(None)
                except Exception:
                    pass

    def _open_a_gap(self) -> None:
        from .kg import store
        p = self.gaps_panel()
        gap = next((g for g in store.load_all()
                    if g.get("status") != "done" and not g.get("dismissed")), None)
        if p is not None and gap is not None:
            p.jump_to_gap(gap["id"])

    def _groups(self, *, pick: bool) -> None:
        self.body().show_page("knowledge_gaps")
        p = self.gaps_panel()
        if p is None:
            return
        p._set_view("groups")
        lst = p._groups_view._list
        lst.clearSelection()
        if pick and lst.count():
            lst.setCurrentRow(0)

    def _settings(self) -> None:
        from ..ui.settings import SettingsDialog
        dlg = SettingsDialog(self.host())
        dlg.show()
        tabs = dlg._tabs
        steps = []
        for size_label, size in (("", None), ("narrow-", dlg.minimumSize())):
            if size is not None:
                steps.append(lambda s=size: dlg.resize(s))
            for i in range(tabs.count()):
                name = "settings-" + size_label + "".join(
                    c if c.isalnum() else "-" for c in tabs.tabText(i).lower()).strip("-")
                steps.append(lambda i=i: tabs.setCurrentIndex(i))
                steps.append(lambda n=name: self.shot(dlg, n))
        steps.append(dlg.reject)       # never saves
        # Run these next, ahead of whatever was queued after Settings.
        self.steps[0:0] = [("settings", s) for s in steps]

    def _work_through(self) -> None:
        from .kg import store
        from .kg.bulk_search import open_unified
        gaps = [g for g in store.load_all()
                if g.get("status") != "done" and not g.get("dismissed")][:2]
        if not gaps:
            self.notes.append("work-through: no open gaps to open it on")
            return
        dlg = open_unified(gaps, parent=self.host())
        if dlg is None:
            return
        self.steps[0:0] = [
            ("work-through", lambda: self.shot(dlg, "work-through-plan")),
            ("work-through", lambda: dlg.resize(*NARROW)),
            ("work-through", lambda: self.shot(dlg, "narrow-work-through-plan")),
            ("work-through", dlg.close),
        ]

    def _finish(self) -> None:
        try:
            if self._restore_page:
                self.body().show_page(self._restore_page)
            self.resize_host(self._restore_size)
        except Exception:
            pass
        with open(os.path.join(self.out, "index.txt"), "w") as f:
            f.write("\n".join(self.notes) + "\n")
        with open(done_path(), "w") as f:
            f.write(self.out + "\n")
        log.info(f"dev screens: {self.n} screenshot(s) in {self.out}")
