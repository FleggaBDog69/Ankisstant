# Modeless capture popup — opens fast, saves to queue, closes.

from __future__ import annotations

import hashlib
import os
import re

from aqt import mw
from aqt.qt import (
    QBuffer, QDialog, QHBoxLayout, QImage, QIODevice, QKeySequence, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QShortcut, Qt, QTextEdit, QUrl,
    QVBoxLayout, QWidget,
)

from ...core.config import tool_config, save_tool_config
from ...core.qt_utils import attach_tag_completer
from ..kg import store as kg_store
from ..kg import engine


def _capture_type_meta(type_key: str) -> dict | None:
    """The KG type definition (with its field schema) for `type_key`."""
    try:
        for t in tool_config("knowledge_gaps").get("types") or []:
            if isinstance(t, dict) and str(t.get("key", "")).lower() == type_key:
                return t
    except Exception:
        pass
    return None


def _capture_widget(spec: dict, prefill: dict):
    """(widget, getter, setter) for one capture field spec — reusing the
    screenshot-aware _StemEdit for html fields. Single-line text fields are
    prefilled from `prefill` (the user's last-used system/subsystem/topic)."""
    kind = spec.get("kind", "text")
    ph = spec.get("placeholder", "")
    key = spec.get("key", "")
    if kind == "html":
        w = _StemEdit()
        w.setAcceptRichText(True)
        w.setPlaceholderText(ph or "Paste text or a screenshot (Cmd/Ctrl+V)…")
        w.setMinimumHeight(140)
        return w, (lambda: "" if w.is_empty() else w.get_html()), \
                  (lambda v: w.setHtml(v) if v else w.clear())
    if kind == "longtext":
        w = QPlainTextEdit()
        w.setPlaceholderText(ph)
        w.setMinimumHeight(60)
        return w, (lambda: w.toPlainText().strip()), \
                  (lambda v: w.setPlainText(str(v or "")))
    if kind == "tag":
        w = QLineEdit()
        w.setPlaceholderText(ph or "School::Year3::…")
        try:
            attach_tag_completer(w, multi=False)
        except Exception:
            pass
        return w, (lambda: w.text().strip()), (lambda v: w.setText(str(v or "")))
    # text / url — single line, prefilled if it's a remembered level
    w = QLineEdit()
    w.setPlaceholderText(ph)
    if prefill.get(key):
        w.setText(prefill[key])
    return w, (lambda: w.text().strip()), (lambda v: w.setText(str(v or "")))


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

        # Render the capture inputs from the active KG type's schema — the fields
        # whose source includes capture and that opt into the mq_capture surface
        # (set per field in Settings → Knowledge Gaps). The QBank popup always
        # captures a missed question, so the type is "mq". This makes the popup
        # follow the user's field configuration instead of a hardcoded form.
        self._type_key = "mq"
        type_meta = _capture_type_meta(self._type_key)
        prefill = {"system": last_system, "subsystem": last_subsystem, "topic": last_topic}
        self._field_getters: dict = {}   # key -> getter
        self._has_stem = False
        first_input = None
        specs = engine.capture_fields(type_meta, "mq_capture")

        # The system/subsystem/topic levels are optional here — auto-tag fills
        # them in when you make the card. Tuck them behind a collapsed toggle so
        # the popup is just stem + concept by default, but a tag can still be
        # pre-set when wanted.
        _LEVEL_KEYS = ("system", "subsystem", "topic")
        levels_box = QWidget()
        levels_layout = QVBoxLayout(levels_box)
        levels_layout.setContentsMargins(0, 0, 0, 0)
        levels_layout.setSpacing(6)
        levels_box.setVisible(False)
        has_levels = False
        self._levels_box = levels_box

        for spec in specs:
            key = spec["key"]
            label = spec.get("label") or key
            w, getter, _setter = _capture_widget(spec, prefill)
            target = levels_layout if key in _LEVEL_KEYS else layout
            if spec["kind"] == "html":
                row = QHBoxLayout()
                row.addWidget(QLabel(f"<b>{label}</b>"))
                row.addStretch()
                pbtn = QPushButton("Paste from clipboard")
                pbtn.setStyleSheet("font-size: 11px; padding: 2px 8px;")
                pbtn.clicked.connect(lambda _c=False, ww=w: (ww.setFocus(), ww.paste()))
                target.addLayout(row)
                target.addWidget(w)
                self._has_stem = True
            else:
                target.addWidget(QLabel(f"<b>{label}</b>"))
                target.addWidget(w)
            self._field_getters[key] = getter
            if key in _LEVEL_KEYS:
                has_levels = True
            elif first_input is None:
                first_input = w

        if has_levels:
            toggle = QPushButton("▸ Optional: pre-set tag")
            toggle.setFlat(True)
            toggle.setStyleSheet(
                "text-align:left; color:gray; font-size:11px; border:none; padding:2px 0;")
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)

            def _toggle_levels(_c=False, btn=toggle, box=levels_box):
                show = not box.isVisible()
                box.setVisible(show)
                btn.setText(("▾ " if show else "▸ ") + "Optional: pre-set tag")

            toggle.clicked.connect(_toggle_levels)
            layout.addWidget(toggle)
            layout.addWidget(levels_box)

        self._first_input = first_input

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

        if self._first_input is not None:
            self._first_input.setFocus()

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

    def _save(self) -> None:
        # Collect every rendered schema field, then add the auto-filled platform.
        fields: dict = {}
        for key, getter in self._field_getters.items():
            try:
                fields[key] = getter()
            except Exception:
                fields[key] = ""
        fields["platform"] = self._platform_key
        fields.setdefault("notes", "")
        concept = str(fields.get("concept", "")).strip()
        stem = str(fields.get("stem_html", "")).strip()
        system = str(fields.get("system", "")).strip()
        subsystem = str(fields.get("subsystem", "")).strip()
        topic = str(fields.get("topic", "")).strip()
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
            type=self._type_key,
            status="open",
            fields=fields,
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


def _inferred_platform() -> tuple[str, str]:
    """(key, name) of the QBank to credit a capture to when none was supplied
    and no embedded window is open: the last one the user opened, else their
    only configured platform. ('', '') when it genuinely can't be inferred."""
    cfg = tool_config("qbank")
    platforms = cfg.get("platforms", []) or []
    last_key = cfg.get("last_platform_key", "")
    if last_key:
        for p in platforms:
            if p.get("key") == last_key:
                return p.get("key", ""), p.get("name", "")
    if len(platforms) == 1:
        p = platforms[0]
        return p.get("key", ""), p.get("name", "")
    return "", ""


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
    # Global triggers (app shortcut, panel button, JS bridge) pass a blank
    # platform — figure out which QBank is open instead of leaving the source
    # blank. The in-browser button already passes its own platform.
    if not platform_key:
        try:
            from .browser import active_platform
            platform_key, platform_name = active_platform()
        except Exception:
            pass
    # Still blank — no embedded QBank window is live (the user is likely
    # reviewing in their own browser). Infer the source rather than making them
    # tell us: the QBank they last opened, or their only configured one.
    if not platform_key:
        try:
            platform_key, platform_name = _inferred_platform()
        except Exception:
            pass
    _current = CaptureDialog(platform_key, platform_name)
    _current.show()
    _current.raise_()
    _current.activateWindow()
