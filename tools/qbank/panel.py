# The QBank tool panel — shown inside the main Ankisstant window.
# The heatmap and per-platform browser launchers also live on the deck browser
# home screen; this panel offers a separate entry point + capture/review/log
# from inside the suite UI.

from __future__ import annotations

from datetime import date

from aqt.qt import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, Qt, QVBoxLayout,
    QWidget,
)

from ...core.config import tool_config
from ...core.qt_utils import make_help_button
from ..kg import store as kg_store
from .browser import open_platform, open_browser, _QuestionCountDialog
from .capture_dialog import open_capture
from .session_dialog import open_session_dialog
from .stats import load_combined_stats, get_streak, add_session
from .targets import get_target


def _practice_questions_available() -> bool:
    try:
        import practice_questions  # noqa: F401
        return True
    except Exception:
        return False


_pq_windows: list = []   # keep PQ library dialogs alive while open


def _on_practice_questions_closed() -> None:
    """Prompt for a question count when the Practice Qs window closes, mirroring
    the QBank browser windows, and log it under the practice_questions platform."""
    from aqt import mw
    dialog = _QuestionCountDialog("Practice Qs")
    if dialog.exec():
        correct, incorrect = dialog.values()
        add_session(correct, incorrect, "practice_questions")
        try:
            mw.deckBrowser.refresh()
        except Exception:
            pass


def _open_practice_questions() -> None:
    # Build the Practice Questions library window ourselves (soft import — the
    # addon is optional) so we can hook its close to the session-count prompt.
    try:
        from practice_questions.library import LibraryDialog  # type: ignore
        dlg = LibraryDialog()
        _pq_windows.append(dlg)

        def _finished(_result, _d=dlg):
            try:
                _pq_windows.remove(_d)
            except ValueError:
                pass
            _on_practice_questions_closed()

        dlg.finished.connect(_finished)
        dlg.show()
        return
    except Exception as e:
        print(f"[ankisstant] practice_questions LibraryDialog hook failed: {e}")
    # Fallback: launch via the addon's own entry point (no session prompt).
    try:
        from practice_questions.library import show_library  # type: ignore
        show_library()
    except Exception as e:
        print(f"[ankisstant] practice_questions launch failed: {e}")


_PRACTICE_QS_PLATFORM = {"key": "practice_questions", "name": "Practice Qs"}


def _qbank_open_kg_count() -> int:
    return kg_store.count(
        lambda kg: kg.get("source") == "qbank" and kg.get("status") == "open"
    )


def _open_kg_page_for_qbank() -> None:
    """Open the main window's Knowledge Gaps tab filtered to QBank items."""
    from ...ui.main_window import open_main_window
    open_main_window()
    try:
        from ...ui.main_window import _current as _mw
        if _mw is None:
            return
        _mw._show_tool("knowledge_gaps")
        # Reach into the panel and switch its filter.
        from .. import knowledge_gaps as kg_tool
        if kg_tool._panel is not None:
            kg_tool._panel.set_filter("qbank")
    except Exception as e:
        print(f"[ankisstant] open KG page for qbank failed: {e}")


class QBankPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def refresh(self) -> None:
        """Rebuild the panel — call when stats/queue may have changed."""
        # Strip everything and rebuild rather than maintain per-widget state.
        old = self.layout()
        if old is not None:
            while old.count():
                item = old.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            QWidget().setLayout(old)
        self._build()

    def _build(self) -> None:
        cfg = tool_config("qbank")
        platforms = cfg.get("platforms", [])

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title_row = QHBoxLayout()
        title = QLabel("<h2 style='margin:0'>AI QBank</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(make_help_button(
            "AI QBank — help",
            "<h3>What it does</h3>"
            "<p>Open a QBank platform inside Anki, capture questions you got "
            "wrong, and queue them for review. AI finds the matching Anki "
            "cards so you can re-rate them as Again.</p>"
            "<h3>Workflow</h3>"
            "<ol>"
            "<li>Click <b>Capture missed</b> (or open one of the platform "
            "buttons) and paste the stem + correct answer.</li>"
            "<li>Open <b>Review</b> — AI searches your collection. "
            "Tag &amp; re-grade matching cards, or create a new one.</li>"
            "<li>Watch the daily heatmap on the deck browser fill up.</li>"
            "</ol>"
            "<h3>Re-grading</h3>"
            "<p>Cards are re-rated as Again via the v3 / FSRS scheduler. "
            "On older schedulers the addon falls back to a queue reset.</p>"
            "<h3>Settings</h3>"
            "<p>Platforms, targets, default notetype, and the 'missed questions' "
            "field name are in <b>Ankisstant Settings → AI QBank</b>.</p>",
            self,
        ))
        root.addLayout(title_row)

        subtitle = QLabel(
            "Open a QBank in an embedded browser, capture questions you miss, "
            "and Claude finds the matching Anki cards."
        )
        subtitle.setStyleSheet("color: gray;")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # ── Today summary ────────────────────────────────────────────────────
        summary_frame = QFrame()
        summary_frame.setFrameShape(QFrame.Shape.StyledPanel)
        summary_layout = QHBoxLayout(summary_frame)
        summary_layout.setContentsMargins(14, 10, 14, 10)

        pq_present = _practice_questions_available()
        stats_platforms = list(platforms) + ([_PRACTICE_QS_PLATFORM] if pq_present else [])

        try:
            stats = load_combined_stats(stats_platforms)
            today_iso = date.today().isoformat()
            today_done = stats.get(today_iso, {}).get("total", 0)
            today_target = get_target(date.today(), cfg)
            streak, _ = get_streak(stats)
        except Exception as e:
            print(f"[ankisstant] panel summary failed: {e}")
            today_done = today_target = streak = 0

        if today_target > 0:
            today_text = f"<b>{today_done}</b> / {today_target} today"
        else:
            today_text = f"<b>{today_done}</b> today"
        streak_word = "day" if streak == 1 else "days"
        n_queue = _qbank_open_kg_count()

        for html in (today_text,
                     f"Streak: <b>{streak}</b> {streak_word}",
                     f"Queue: <b>{n_queue}</b>"):
            lbl = QLabel(html)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            summary_layout.addWidget(lbl)
            summary_layout.addStretch(1)
        # Trim trailing stretch from the last loop iteration.
        if summary_layout.count() > 0:
            last = summary_layout.takeAt(summary_layout.count() - 1)
            if last is not None and last.widget() is None:
                pass  # spacer item, already removed by takeAt

        root.addWidget(summary_frame)

        # ── Platform launchers ───────────────────────────────────────────────
        root.addWidget(QLabel("<b>Open QBank</b>"))
        grid = QGridLayout()
        grid.setSpacing(8)
        slot = 0
        for p in platforms:
            btn = QPushButton(p["name"])
            btn.setMinimumHeight(36)
            btn.clicked.connect(
                lambda _checked=False, _p=p: open_platform(_p["key"], _p["name"], _p["url"])
            )
            grid.addWidget(btn, slot // 3, slot % 3)
            slot += 1
        if pq_present:
            pq_btn = QPushButton("Practice Qs")
            pq_btn.setMinimumHeight(36)
            pq_btn.setToolTip("Practice Questions addon — local screenshot library")
            pq_btn.clicked.connect(lambda _checked=False: _open_practice_questions())
            grid.addWidget(pq_btn, slot // 3, slot % 3)
            slot += 1
        # Always offer a general browser — use any site without configuring it.
        browser_btn = QPushButton("🌐 Open Browser")
        browser_btn.setMinimumHeight(36)
        browser_btn.setToolTip("Open a blank web browser with the capture toolbar — "
                               "navigate to any site via the address bar")
        browser_btn.clicked.connect(lambda _checked=False: open_browser())
        grid.addWidget(browser_btn, slot // 3, slot % 3)
        wrapper = QWidget()
        wrapper.setLayout(grid)
        root.addWidget(wrapper)
        if not platforms:
            root.addWidget(QLabel("<i>No QBanks configured yet — add some in "
                                  "Settings, or just use Open Browser.</i>"))

        # ── Actions ──────────────────────────────────────────────────────────
        root.addWidget(QLabel("<b>Actions</b>"))
        actions_row = QHBoxLayout()
        capture_btn = QPushButton("📌 Capture missed Q")
        capture_btn.clicked.connect(lambda: open_capture("", ""))
        actions_row.addWidget(capture_btn)

        review_btn = QPushButton(f"Review queue ({n_queue})")
        review_btn.clicked.connect(_open_kg_page_for_qbank)
        actions_row.addWidget(review_btn)

        log_btn = QPushButton("+ Log session")
        log_btn.clicked.connect(lambda: open_session_dialog())
        actions_row.addWidget(log_btn)

        actions_row.addStretch(1)
        actions_wrapper = QWidget()
        actions_wrapper.setLayout(actions_row)
        root.addWidget(actions_wrapper)

        # ── Weakness dashboard ───────────────────────────────────────────────
        if cfg.get("show_weakness", True):
            try:
                from .weakness import WeaknessSection
                root.addWidget(WeaknessSection())
            except Exception as e:
                print(f"[ankisstant] weakness section failed: {e}")

        root.addStretch(1)
