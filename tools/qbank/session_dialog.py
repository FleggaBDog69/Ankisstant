# Manual session entry — log questions done outside the QBank browser (phone app, paper).

from __future__ import annotations

from aqt import mw
from aqt.qt import (
    QComboBox, QDate, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QVBoxLayout,
)

from ...core.config import tool_config
from ...core.qt_utils import theme_dialog
from .stats import add_session, ensure_storage


_OTHER = "__other__"


class _SessionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent or mw)
        self.setWindowTitle("Log QBank session")
        self.setMinimumWidth(440)

        cfg = tool_config("qbank")
        platforms = cfg.get("platforms", [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        intro = QLabel("Record questions done outside the QBank browser (e.g. phone, paper).")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setVerticalSpacing(8)

        self._source = QComboBox()
        for p in platforms:
            self._source.addItem(p["name"], p["key"])
        self._source.addItem("Other / Manual", _OTHER)
        form.addRow("Source:", self._source)

        self._other_name = QLineEdit()
        self._other_name.setPlaceholderText("e.g. UWorld, Phone, Paper exam")
        self._other_name.setVisible(False)
        form.addRow("Source name:", self._other_name)
        self._other_label = form.labelForField(self._other_name)
        self._other_label.setVisible(False)
        self._source.currentIndexChanged.connect(self._on_source_change)

        self._date = QDateEdit()
        self._date.setCalendarPopup(True)
        self._date.setDate(QDate.currentDate())
        self._date.setMaximumDate(QDate.currentDate())
        form.addRow("Date:", self._date)

        self._correct = QSpinBox()
        self._correct.setRange(0, 9999)
        form.addRow("Correct:", self._correct)

        self._incorrect = QSpinBox()
        self._incorrect.setRange(0, 9999)
        form.addRow("Incorrect:", self._incorrect)

        layout.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        theme_dialog(self)

    def _on_source_change(self, _idx: int) -> None:
        is_other = self._source.currentData() == _OTHER
        self._other_name.setVisible(is_other)
        self._other_label.setVisible(is_other)

    def _save(self) -> None:
        correct   = self._correct.value()
        incorrect = self._incorrect.value()
        if correct == 0 and incorrect == 0:
            return
        if self._source.currentData() == _OTHER:
            raw = self._other_name.text().strip().lower() or "manual"
            platform_key = "".join(c if c.isalnum() else "_" for c in raw)[:24] or "manual"
        else:
            platform_key = self._source.currentData()
        iso = self._date.date().toString("yyyy-MM-dd")
        ensure_storage(platform_key)
        add_session(correct, incorrect, platform_key, on_date=iso)
        try:
            mw.deckBrowser.refresh()
        except Exception:
            pass
        self.accept()


_current: "_SessionDialog | None" = None


def _is_live(dlg) -> bool:
    if dlg is None:
        return False
    try:
        return dlg.isVisible()
    except RuntimeError:
        return False


def open_session_dialog() -> None:
    global _current
    if _is_live(_current):
        _current.raise_()
        _current.activateWindow()
        return
    _current = _SessionDialog()
    _current.show()
    _current.raise_()
    _current.activateWindow()
