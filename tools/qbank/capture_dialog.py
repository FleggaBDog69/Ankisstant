# Modeless capture popup — opens fast, saves to queue, closes.

from __future__ import annotations

import hashlib
import os
import re

from aqt import mw
from aqt.qt import (
    QBuffer, QDialog, QHBoxLayout, QImage, QIODevice, QKeySequence, QLabel,
    QLineEdit, QPushButton, QShortcut, Qt, QTextEdit, QUrl, QVBoxLayout,
)

from ...core.config import tool_config, save_tool_config
from ..kg import store as kg_store


# Webviews whose zoom we tweak while the capture popup is open. We restore
# each one's prior zoom on close. Only the embedded QBank platform browser
# is touched — shrinking it makes the question easier to fit on screen and
# screenshot into the capture popup. Anki's own reviewer is left alone (this
# zoom is a QBank affordance, not a card-review one).
def _zoomable_webviews() -> list:
    """Return the live QBank browser webviews we can call setZoomFactor on."""
    views = []
    try:
        from .browser import _windows
        for win in _windows.values():
            try:
                v = getattr(win, "view", None)
                if v is not None and v.isVisible():
                    views.append(v)
            except Exception:
                pass
    except Exception:
        pass
    return views


class _StemEdit(QTextEdit):
    """Accepts pasted images, saving them to Anki's media folder."""

    # Width (px) used to render pasted images inside the dialog only — keeps
    # screenshots as small placeholders so the editor doesn't get swamped.
    # Stripped in get_html() so the saved stem_html still references full-size
    # images for the Anki card.
    _PREVIEW_WIDTH = 96

    def __init__(self, parent=None):
        super().__init__(parent)
        # Resolve relative <img src="qbank_capture_*.png"> references against
        # Anki's media folder. Without this, saved screenshots come back as
        # broken-image icons after the dialog reopens.
        self._apply_media_base_url()

    def _apply_media_base_url(self) -> None:
        try:
            if mw.col is None:
                return
            media_dir = mw.col.media.dir()
            if not media_dir.endswith(os.sep):
                media_dir += os.sep
            self.document().setBaseUrl(QUrl.fromLocalFile(media_dir))
        except Exception as e:
            print(f"[ankisstant] _StemEdit base url failed: {e}")

    def setHtml(self, html) -> None:
        # Re-assert the base URL on every reload — Qt resets the document on
        # setHtml in some versions, which loses the resolver.
        self._apply_media_base_url()
        super().setHtml(html)

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
                    self.insertHtml(
                        f'<img src="{fname}" width="{self._PREVIEW_WIDTH}"><br>'
                    )
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
        # Swap the display-only width="96" we set on pasted images for a
        # card-appropriate max-width style. Without this, screenshots get
        # appended at their native pixel size and dominate the Anki card.
        try:
            max_w = int(tool_config("qbank").get("image_max_width", 300))
        except Exception:
            max_w = 300
        body = re.sub(
            r'(<img\b[^>]*?)\s+width="\d+"',
            rf'\1 style="max-width:{max_w}px;height:auto;"',
            body,
            flags=re.IGNORECASE,
        )
        body = re.sub(r'(<img\b[^>]*?)\s+height="\d+"', r'\1', body, flags=re.IGNORECASE)
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
        last_system    = cfg.get("last_system", "")
        last_subsystem = cfg.get("last_subsystem", "")
        last_topic     = cfg.get("last_topic", "")

        # Shrink the underlying Anki webview(s) so the popup doesn't cover
        # the question text. Saved per-view so we can restore the prior
        # zoom on close (some users run Anki at non-1.0 base zoom).
        self._prior_zooms: list[tuple[object, float]] = []
        try:
            target_zoom = float(cfg.get("capture_zoom_factor", 0.7))
        except (TypeError, ValueError):
            target_zoom = 0.7
        if 0.3 <= target_zoom < 1.0:
            for v in _zoomable_webviews():
                try:
                    self._prior_zooms.append((v, float(v.zoomFactor())))
                    v.setZoomFactor(target_zoom)
                except Exception as e:
                    print(f"[ankisstant] capture zoom set failed: {e}")

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

        levels_hint = QLabel(
            "<span style='color:gray;font-size:10px'>"
            "Saved with the missed question — used to build the tag later when "
            "you make a card.</span>"
        )
        levels_hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(levels_hint)

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

    # ── window placement ───────────────────────────────────────────────
    # Open at the bottom-right of the active Anki window so the popup
    # doesn't cover the question being reviewed. If the user has dragged
    # the window before, prefer their last position (saved on close).

    def showEvent(self, ev):
        super().showEvent(ev)
        try:
            cfg = tool_config("qbank")
            saved = cfg.get("capture_window_pos") or None
            if isinstance(saved, dict) and "x" in saved and "y" in saved:
                self.move(int(saved["x"]), int(saved["y"]))
                return
            # Default: bottom-right of Anki main window, with a small margin.
            geo = mw.frameGeometry()
            margin = 24
            x = geo.right() - self.width() - margin
            y = geo.bottom() - self.height() - margin
            # Clamp to non-negative — if the main window is off-screen, fall
            # back to top-left of its frame.
            self.move(max(0, x), max(0, y))
        except Exception as e:
            print(f"[ankisstant] capture placement failed: {e}")

    def _restore_zoom(self) -> None:
        for view, prior in self._prior_zooms:
            try:
                view.setZoomFactor(prior)
            except Exception as e:
                print(f"[ankisstant] capture zoom restore failed: {e}")
        self._prior_zooms = []

    def _persist_pos(self) -> None:
        try:
            p = self.pos()
            cfg = tool_config("qbank")
            cfg["capture_window_pos"] = {"x": int(p.x()), "y": int(p.y())}
            save_tool_config("qbank", cfg)
        except Exception as e:
            print(f"[ankisstant] capture pos persist failed: {e}")

    def closeEvent(self, ev):
        self._persist_pos()
        self._restore_zoom()
        super().closeEvent(ev)

    def accept(self):
        self._persist_pos()
        self._restore_zoom()
        super().accept()

    def reject(self):
        self._persist_pos()
        self._restore_zoom()
        super().reject()

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
