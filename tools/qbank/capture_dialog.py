# Modeless capture popup — opens fast, saves to queue, closes.

from __future__ import annotations

import hashlib
import re

from aqt import mw
from aqt.qt import (
    QBuffer, QDialog, QHBoxLayout, QImage, QIODevice, QKeySequence, QLabel,
    QLineEdit, QPushButton, QShortcut, Qt, QTextEdit, QVBoxLayout,
)

from ...core.config import tool_config, save_tool_config
from ..kg import store as kg_store


class _StemEdit(QTextEdit):
    """Accepts pasted images, saving them to Anki's media folder."""

    def insertFromMimeData(self, source) -> None:
        if source.hasImage():
            img_data = source.imageData()
            try:
                image = QImage(img_data)
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                image.save(buf, "PNG")
                data = bytes(buf.data())
                digest = hashlib.md5(data).hexdigest()[:10]
                desired = f"qbank_capture_{digest}.png"
                if mw.col is not None:
                    fname = mw.col.media.write_data(desired, data)
                    self.insertHtml(f'<img src="{fname}"><br>')
                else:
                    self.insertHtml("[image — open a profile first]<br>")
            except Exception as e:
                print(f"[ankisstant] image paste failed: {e}")
                self.insertHtml("[image paste failed]<br>")
            return
        super().insertFromMimeData(source)

    def get_html(self) -> str:
        full = self.toHtml()
        m = re.search(r"<body[^>]*>(.*)</body>", full, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else full
        body = re.sub(r"(<p[^>]*>\s*(<br[^>]*/?>)?\s*</p>\s*)+$", "", body, flags=re.IGNORECASE)
        return body.strip()

    def is_empty(self) -> bool:
        return not self.toPlainText().strip() and "<img " not in self.toHtml().lower()


_current: "CaptureDialog | None" = None


class CaptureDialog(QDialog):
    def __init__(self, platform_key: str = "", platform_name: str = "", parent=None):
        super().__init__(parent or mw)
        self._platform_key = platform_key
        self.setWindowTitle("📌 Capture missed question")
        self.setMinimumWidth(620)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        cfg = tool_config("qbank")
        tag_root       = cfg.get("tag_root", "Missed_Questions")
        last_system    = cfg.get("last_system", "")
        last_subsystem = cfg.get("last_subsystem", "")
        last_topic     = cfg.get("last_topic", "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        if platform_name:
            src = QLabel(f"Source: <b>{platform_name}</b>")
            src.setStyleSheet("color: gray; font-size: 11px;")
            layout.addWidget(src)

        layout.addWidget(QLabel("<b>Specific concept missed</b>"))
        self._concept = QLineEdit()
        self._concept.setPlaceholderText("e.g. digoxin toxicity is worsened by hypokalaemia")
        layout.addWidget(self._concept)

        layout.addWidget(QLabel("<b>System / Subsystem / Topic</b>"))
        levels_row = QHBoxLayout()
        self._system = QLineEdit(last_system)
        self._system.setPlaceholderText("System (e.g. Cardio)")
        self._subsystem = QLineEdit(last_subsystem)
        self._subsystem.setPlaceholderText("Subsystem (e.g. Arrhythmia)")
        self._topic = QLineEdit(last_topic)
        self._topic.setPlaceholderText("Topic (e.g. Digoxin)")
        levels_row.addWidget(self._system)
        levels_row.addWidget(self._subsystem)
        levels_row.addWidget(self._topic)
        layout.addLayout(levels_row)

        tag_preview = QLabel(
            f"<span style='color:gray;font-size:10px'>"
            f"Tag → {tag_root}::&lt;System&gt;::&lt;Subsystem&gt;::&lt;Topic&gt;  "
            f"<i>(blank levels are skipped)</i></span>"
        )
        tag_preview.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(tag_preview)

        stem_row = QHBoxLayout()
        stem_row.addWidget(QLabel("<b>Question stem</b>"))
        stem_row.addStretch()
        paste_btn = QPushButton("Paste from clipboard")
        paste_btn.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        paste_btn.clicked.connect(self._paste)
        stem_row.addWidget(paste_btn)
        layout.addLayout(stem_row)

        self._stem = _StemEdit()
        self._stem.setAcceptRichText(True)
        self._stem.setPlaceholderText("Paste text or a screenshot (Cmd/Ctrl+V)…")
        self._stem.setMinimumHeight(140)
        layout.addWidget(self._stem)

        img_hint = QLabel("Screenshots work — they're saved to Anki's media folder automatically.")
        img_hint.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(img_hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save  (⌘↩)")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        QShortcut(QKeySequence("Ctrl+Return"), self, self._save)
        QShortcut(QKeySequence("Meta+Return"), self, self._save)

        self._concept.setFocus()

    def _paste(self) -> None:
        self._stem.setFocus()
        self._stem.paste()

    def _save(self) -> None:
        concept   = self._concept.text().strip()
        system    = self._system.text().strip()
        subsystem = self._subsystem.text().strip()
        topic     = self._topic.text().strip()
        stem      = "" if self._stem.is_empty() else self._stem.get_html()
        if not concept and not stem:
            return
        # Title defaults to concept; falls back to first ~60 chars of stem plaintext.
        title = concept
        if not title:
            import re as _re
            plain = _re.sub(r"<[^>]+>", " ", stem)
            plain = _re.sub(r"\s+", " ", plain).strip()
            title = plain[:60] or "(captured)"
        kg_store.add(
            title=title,
            source="qbank",
            type="mq",
            status="open",
            fields={
                "concept":   concept,
                "stem_html": stem,
                "system":    system,
                "subsystem": subsystem,
                "topic":     topic,
                "platform":  self._platform_key,
                "notes":     "",
            },
        )
        # Tell any open KG panel to refresh.
        try:
            from .. import knowledge_gaps
            knowledge_gaps._refresh_open_panel()
        except Exception:
            pass
        try:
            cfg = tool_config("qbank")
            changed = False
            for key, val in (("last_system", system),
                             ("last_subsystem", subsystem),
                             ("last_topic", topic)):
                if cfg.get(key) != val:
                    cfg[key] = val
                    changed = True
            if changed:
                save_tool_config("qbank", cfg)
        except Exception as e:
            print(f"[ankisstant] persist last_levels failed: {e}")
        try:
            mw.deckBrowser.refresh()
        except Exception:
            pass
        self.accept()


def _is_live(dlg) -> bool:
    if dlg is None:
        return False
    try:
        return dlg.isVisible()
    except RuntimeError:
        return False


def open_capture(platform_key: str = "", platform_name: str = "") -> None:
    global _current
    if _is_live(_current):
        _current.raise_()
        _current.activateWindow()
        return
    _current = CaptureDialog(platform_key, platform_name)
    _current.show()
    _current.raise_()
    _current.activateWindow()
