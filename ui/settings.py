# Unified settings dialog for Ankisstant.
# Tabs: Global, QBank, Browse, Create with Claude.
# Global section owns the shared API key, CLI path and provider mode.
# Each tool tab edits its tools[<key>] config slice.

from __future__ import annotations

import re
from datetime import date

from aqt.qt import (
    QApplication, QCheckBox, QComboBox, QDate, QDateEdit, QDialog,
    QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, Qt, QTabWidget,
    QVBoxLayout, QWidget,
)
from aqt.utils import showInfo, showWarning, tooltip

from ..core import api as core_api
from ..core.config import DEFAULTS, load_config, save_config


# ── small helpers ────────────────────────────────────────────────────────────

def _scroll_list(min_h: int = 100, max_h: int = 200) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(inner)
    scroll.setMinimumHeight(min_h)
    scroll.setMaximumHeight(max_h)
    return scroll, inner, layout


def _wrap_scroll(widget: QWidget) -> QScrollArea:
    """Wrap a tab page in a vertical scroll area so long forms don't clip."""
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QScrollArea.Shape.NoFrame)
    sa.setWidget(widget)
    return sa


def _expand_form(layout: QFormLayout) -> QFormLayout:
    """Make QFormLayout field columns fill the row width."""
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    return layout


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())[:24] or "qbank"


# ── sub-dialogs (for QBank list editing) ──────────────────────────────────────

class _QBankEditDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit QBank" if existing else "Add QBank")
        self.setMinimumWidth(520)
        self._existing_key = existing.get("key") if existing else None

        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setVerticalSpacing(10)

        self._name = QLineEdit(existing["name"] if existing else "")
        self._name.setMinimumWidth(360)
        self._name.setPlaceholderText("e.g. Osmosis")
        layout.addRow("Name:", self._name)

        self._url = QLineEdit(existing["url"] if existing else "")
        self._url.setMinimumWidth(360)
        self._url.setPlaceholderText("https://...")
        layout.addRow("URL:", self._url)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        layout.addRow(bb)

    def _on_accept(self):
        if self._name.text().strip() and self._url.text().strip():
            self.accept()

    def result_platform(self) -> dict:
        name = self._name.text().strip()
        return {
            "key":  self._existing_key or _slug(name),
            "name": name,
            "url":  self._url.text().strip(),
        }


class _PeriodEditDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit exam period" if existing else "Add exam period")
        self.setMinimumWidth(420)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setVerticalSpacing(10)

        today = QDate.currentDate()
        self._from = QDateEdit()
        self._from.setCalendarPopup(True)
        self._from.setDate(
            today if not existing else QDate.fromString(existing["from"], "yyyy-MM-dd")
        )
        layout.addRow("From:", self._from)

        self._to = QDateEdit()
        self._to.setCalendarPopup(True)
        self._to.setDate(
            today.addDays(30) if not existing else QDate.fromString(existing["to"], "yyyy-MM-dd")
        )
        layout.addRow("To:", self._to)

        self._spin = QSpinBox()
        self._spin.setRange(1, 9999)
        self._spin.setSuffix(" Q/day")
        self._spin.setValue(existing["daily"] if existing else 100)
        layout.addRow("Daily target:", self._spin)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addRow(bb)

    def result_period(self) -> dict:
        return {
            "from":  self._from.date().toString("yyyy-MM-dd"),
            "to":    self._to.date().toString("yyyy-MM-dd"),
            "daily": self._spin.value(),
        }


class _NotetypeProfileDialog(QDialog):
    """Edit one entry in the Creator's notetype profile list."""

    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit notetype" if existing else "Add notetype")
        self.setMinimumWidth(560)
        e = existing or {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        form = _expand_form(QFormLayout())
        form.setVerticalSpacing(8)

        # Notetype name — combobox of installed notetypes for convenience,
        # but editable so the user can pre-configure a notetype that doesn't
        # exist yet in this profile (e.g. a Malleus type they'll install).
        self._name = QComboBox()
        self._name.setEditable(True)
        self._name.setMinimumWidth(360)
        try:
            from aqt import mw as _mw
            if _mw.col is not None:
                for m in sorted(_mw.col.models.all_names()):
                    self._name.addItem(m)
        except Exception:
            pass
        if e.get("name"):
            idx = self._name.findText(e["name"])
            if idx >= 0:
                self._name.setCurrentIndex(idx)
            else:
                self._name.setEditText(e["name"])
        # When the user picks an existing notetype, auto-suggest its fields
        # so they don't have to type them by hand.
        self._name.currentIndexChanged.connect(self._maybe_suggest_fields)
        form.addRow("Notetype:", self._name)

        self._front = QLineEdit(e.get("front_field", "Text"))
        self._front.setMinimumWidth(360)
        self._front.setPlaceholderText("Field that receives the cloze front (e.g. Text)")
        form.addRow("Front field:", self._front)

        self._extra = QLineEdit(e.get("extra_field", "Extra"))
        self._extra.setMinimumWidth(360)
        self._extra.setPlaceholderText("Field that receives supporting text (e.g. Extra)")
        form.addRow("Extra field:", self._extra)

        self._image = QLineEdit(e.get("image_field", e.get("extra_field", "Extra")))
        self._image.setMinimumWidth(360)
        self._image.setPlaceholderText("Field that receives <img> tags (usually same as Extra)")
        form.addRow("Image field:", self._image)

        self._sources = QLineEdit(e.get("sources_field", ""))
        self._sources.setMinimumWidth(360)
        self._sources.setPlaceholderText(
            "Source/citation field (e.g. Source) — leave blank to skip"
        )
        form.addRow("Sources field:", self._sources)

        self._obo = QLineEdit(e.get("one_by_one_field", "One by one"))
        self._obo.setMinimumWidth(360)
        self._obo.setPlaceholderText("AnKing 'One by one' toggle field (leave default if unused)")
        form.addRow("One-by-one field:", self._obo)

        # ── Card-creation skill (per-notetype) ───────────────────────────
        # CLI mode → prepend the invocation string (e.g. '/malleus-anki'
        # or 'Use the malleus-anki skill') to the prompt. Claude Code's
        # skills live in ~/.claude/skills/<name>/SKILL.md and are loaded
        # on demand via description matching, so token cost stays low.
        # API mode → pass the Anthropic custom skill ID server-side.
        self._skill_invocation = QLineEdit(
            e.get("card_creation_skill_invocation",
                  e.get("card_creation_skill_path", ""))
        )
        self._skill_invocation.setMinimumWidth(360)
        self._skill_invocation.setPlaceholderText(
            "e.g. /malleus-anki  or  Use the malleus-anki skill"
        )
        form.addRow("Card creation skill (CLI):", self._skill_invocation)

        self._skill_id = QLineEdit(e.get("card_creation_skill_id", ""))
        self._skill_id.setMinimumWidth(360)
        self._skill_id.setPlaceholderText("skill_… (Anthropic API custom skill ID, used in API mode)")
        form.addRow("Card creation skill (API):", self._skill_id)

        root.addLayout(form)

        skill_hint = QLabel(
            "<small>The CLI field is prepended to every card request — pick whichever "
            "invocation Claude Code understands (slash command or plain English). "
            "Slash commands only work reliably if the addon's CLI run picks them up; "
            "the safer phrasing is <code>Use the &lt;name&gt; skill</code>. "
            "The skill body must live at "
            "<code>~/.claude/skills/&lt;name&gt;/SKILL.md</code>.</small>"
        )
        skill_hint.setWordWrap(True)
        skill_hint.setTextFormat(Qt.TextFormat.RichText)
        skill_hint.setStyleSheet("color: gray;")
        root.addWidget(skill_hint)

        root.addWidget(QLabel("Extra prompt instructions for Claude (optional):"))
        self._instructions = QPlainTextEdit(e.get("extra_instructions", ""))
        self._instructions.setPlaceholderText(
            "e.g. 'Malleus style — front fact only, no clinical context. "
            "Use field <Mnemonic> for memory aids instead of Extra.'\n\n"
            "Claude will be told which fields this notetype has and will follow "
            "these instructions when drafting cards."
        )
        self._instructions.setMinimumHeight(120)
        root.addWidget(self._instructions, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _maybe_suggest_fields(self, _idx: int) -> None:
        # Only fill fields that are still at their default — never clobber
        # something the user has customised.
        name = self._name.currentText().strip()
        if not name:
            return
        try:
            from aqt import mw as _mw
            nt = _mw.col.models.by_name(name)
        except Exception:
            return
        if not nt:
            return
        fields = [f["name"] for f in nt.get("flds", [])]
        if not fields:
            return
        def pick(candidates: list[str], fallback: str) -> str:
            for c in candidates:
                for fname in fields:
                    if fname.lower() == c.lower():
                        return fname
            return fallback
        if self._front.text().strip() in ("", "Text"):
            self._front.setText(pick(["Text", "Front"], fields[0]))
        if self._extra.text().strip() in ("", "Extra"):
            self._extra.setText(pick(["Extra", "Back", "Notes"], fields[-1]))
        if self._image.text().strip() in ("", "Extra"):
            self._image.setText(self._extra.text())
        if self._sources.text().strip() == "":
            # Only auto-fill if the notetype actually has a sources-shaped field.
            for cand in ("Source", "Sources", "References", "Citations"):
                for fname in fields:
                    if fname.lower() == cand.lower():
                        self._sources.setText(fname)
                        break
                if self._sources.text().strip():
                    break
        if self._obo.text().strip() in ("", "One by one"):
            self._obo.setText(pick(["One by one", "OneByOne"], "One by one"))

    def _on_accept(self):
        if not self._name.currentText().strip():
            tooltip("Pick a notetype name.")
            return
        self.accept()

    def result_profile(self) -> dict:
        return {
            "name":               self._name.currentText().strip(),
            "front_field":        self._front.text().strip() or "Text",
            "extra_field":        self._extra.text().strip() or "Extra",
            "image_field":        self._image.text().strip() or (self._extra.text().strip() or "Extra"),
            "sources_field":      self._sources.text().strip(),
            "one_by_one_field":   self._obo.text().strip()   or "One by one",
            "card_creation_skill_invocation": self._skill_invocation.text().strip(),
            "card_creation_skill_id":         self._skill_id.text().strip(),
            "extra_instructions": self._instructions.toPlainText().strip(),
        }


class _ExamDateEditDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit exam date" if existing else "Add exam date")
        self.setMinimumWidth(420)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setVerticalSpacing(10)

        today = QDate.currentDate()
        self._date = QDateEdit()
        self._date.setCalendarPopup(True)
        self._date.setDate(
            today if not existing else QDate.fromString(existing["date"], "yyyy-MM-dd")
        )
        layout.addRow("Date:", self._date)

        self._label = QLineEdit(existing.get("label", "") if existing else "")
        self._label.setMinimumWidth(320)
        self._label.setPlaceholderText("e.g. OSCE, Finals")
        layout.addRow("Label:", self._label)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        layout.addRow(bb)

    def _on_accept(self):
        if self._label.text().strip():
            self.accept()

    def result_exam(self) -> dict:
        return {
            "date":  self._date.date().toString("yyyy-MM-dd"),
            "label": self._label.text().strip(),
        }


# ── tab widgets ──────────────────────────────────────────────────────────────

class _GlobalTab(QWidget):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        intro = QLabel(
            "Two backends are supported: the local <b>Claude Code CLI</b> "
            "(uses your subscription quota), or the <b>Anthropic API</b> "
            "(pay-per-token, requires a key from console.anthropic.com)."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addRow(intro)

        self._mode = QComboBox()
        self._mode.addItem("Auto — prefer CLI, fall back to API", "auto")
        self._mode.addItem("Claude Code CLI only (subscription)", "cli")
        self._mode.addItem("Anthropic API only (paid)",           "api")
        idx = max(0, self._mode.findData((cfg.get("provider_mode") or "auto").lower()))
        self._mode.setCurrentIndex(idx)
        layout.addRow("Provider mode:", self._mode)

        # API key with show/hide toggle.
        key_row = QHBoxLayout()
        self._key = QLineEdit(cfg.get("anthropic_api_key", ""))
        self._key.setMinimumWidth(420)
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("sk-ant-…")
        key_row.addWidget(self._key, 1)
        self._show_key = QPushButton("Show")
        self._show_key.setCheckable(True)
        self._show_key.setFixedWidth(56)
        self._show_key.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self._show_key)
        layout.addRow("Anthropic API key:", key_row)

        self._cli_path = QLineEdit(cfg.get("claude_cli_path", ""))
        self._cli_path.setMinimumWidth(480)
        self._cli_path.setPlaceholderText("Auto-detect (leave blank) — e.g. /usr/local/bin/claude")
        layout.addRow("Claude CLI path:", self._cli_path)

        cli_hint = QLabel(
            "<small>Leave blank to auto-detect. On macOS, GUI apps don't inherit your "
            "shell PATH, so you may need to set this explicitly. Run "
            "<code>which claude</code> to find it.</small>"
        )
        cli_hint.setWordWrap(True)
        cli_hint.setTextFormat(Qt.TextFormat.RichText)
        cli_hint.setStyleSheet("color: gray;")
        layout.addRow("", cli_hint)

        self._cli_extra = QLineEdit(" ".join(cfg.get("claude_cli_extra_args") or []))
        self._cli_extra.setMinimumWidth(480)
        self._cli_extra.setPlaceholderText("e.g. --permission-mode bypassPermissions")
        layout.addRow("CLI extra args:", self._cli_extra)

        self._model_default = QLineEdit(cfg.get("model_default", "claude-sonnet-4-6"))
        self._model_default.setMinimumWidth(320)
        self._model_default.setPlaceholderText("claude-sonnet-4-6")
        layout.addRow("Default model:", self._model_default)

        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self._test_btn)
        test_row.addStretch(1)
        layout.addRow(test_row)

    def _toggle_key_visibility(self, checked: bool):
        self._key.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._show_key.setText("Hide" if checked else "Show")

    def _on_test(self):
        # Save the form values into a transient config snapshot for the test
        # call, without persisting them (Save isn't clicked yet).
        from ..core.config import load_config as _load
        snapshot = _load()
        snapshot.update(self.get_values())
        save_config(snapshot)
        self._test_btn.setEnabled(False)
        self._test_btn.setText("Testing…")
        QApplication.processEvents()
        try:
            reply = core_api.ask_claude(
                prompt="Reply with the word ok and nothing else.",
                system="You are a connection test. Reply with the single word: ok",
                max_tokens=16,
                show_errors=False,
            )
        finally:
            self._test_btn.setEnabled(True)
            self._test_btn.setText("Test connection")
        if reply:
            showInfo(f"Connection OK.\n\nReply: {reply!r}")
        else:
            showWarning("Test failed — see Anki's console for the error.")

    def get_values(self) -> dict:
        return {
            "provider_mode":       self._mode.currentData() or "auto",
            "anthropic_api_key":   self._key.text().strip(),
            "claude_cli_path":     self._cli_path.text().strip(),
            "claude_cli_extra_args": [t for t in self._cli_extra.text().strip().split() if t],
            "model_default":       self._model_default.text().strip() or "claude-sonnet-4-6",
        }


class _QBankTab(QWidget):
    def __init__(self, qb_cfg: dict, parent=None):
        super().__init__(parent)
        self._platforms: list[dict] = [dict(p) for p in qb_cfg.get("platforms", [])]
        self._periods:   list[dict] = [dict(p) for p in qb_cfg.get("target_periods", [])]
        self._exams:     list[dict] = [dict(e) for e in qb_cfg.get("exam_dates", [])]

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self._enabled = QCheckBox("Enable QBank with Claude")
        self._enabled.setChecked(bool(qb_cfg.get("enabled", True)))
        root.addWidget(self._enabled)

        self._show_heatmap = QCheckBox("Show heatmap on the deck browser home screen")
        self._show_heatmap.setChecked(bool(qb_cfg.get("show_heatmap", True)))
        root.addWidget(self._show_heatmap)

        # ── Platforms ─────────────────────────────────────────────────────────
        plat_box = QGroupBox("QBanks")
        pl = QVBoxLayout(plat_box)
        plat_hint = QLabel("Each QBank opens in its own embedded browser with persistent login cookies.")
        plat_hint.setStyleSheet("color: gray; font-size: 11px;")
        plat_hint.setWordWrap(True)
        pl.addWidget(plat_hint)
        self._plat_scroll, _inner, self._plat_list = _scroll_list(120, 220)
        pl.addWidget(self._plat_scroll)
        add_plat = QPushButton("+ Add QBank")
        add_plat.clicked.connect(self._add_platform)
        pl.addWidget(add_plat)
        root.addWidget(plat_box)
        self._rebuild_platforms()

        # ── Targets / exam dates ──────────────────────────────────────────────
        tgt_box = QGroupBox("Daily targets & exam dates")
        tl = QVBoxLayout(tgt_box)
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default daily target:"))
        self._spin = QSpinBox()
        self._spin.setRange(0, 9999)
        self._spin.setSuffix(" Q/day")
        self._spin.setSpecialValueText("No target")
        self._spin.setValue(int(qb_cfg.get("default_daily", 0)))
        default_row.addWidget(self._spin)
        default_row.addStretch(1)
        tl.addLayout(default_row)

        tl.addWidget(QLabel("Exam-period overrides:"))
        self._per_scroll, _i, self._per_list = _scroll_list(80, 140)
        tl.addWidget(self._per_scroll)
        add_per = QPushButton("+ Add exam period")
        add_per.clicked.connect(self._add_period)
        tl.addWidget(add_per)

        tl.addWidget(QLabel("Exam dates (shown red on the heatmap):"))
        self._exam_scroll, _i2, self._exam_list = _scroll_list(80, 140)
        tl.addWidget(self._exam_scroll)
        add_ex = QPushButton("+ Add exam date")
        add_ex.clicked.connect(self._add_exam)
        tl.addWidget(add_ex)
        root.addWidget(tgt_box)
        self._rebuild_periods()
        self._rebuild_exams()

        # ── Capture / card-gen ───────────────────────────────────────────────
        cap_box = QGroupBox("Capture & AI card generation")
        cf = _expand_form(QFormLayout(cap_box))

        self._search_model = QLineEdit(qb_cfg.get("search_model", "claude-haiku-4-5-20251001"))
        self._search_model.setMinimumWidth(360)
        cf.addRow("Search model (fast):", self._search_model)

        self._card_model = QLineEdit(qb_cfg.get("card_gen_model", "claude-sonnet-4-6"))
        self._card_model.setMinimumWidth(360)
        cf.addRow("Card-gen model:", self._card_model)

        self._notetype = QLineEdit(qb_cfg.get("card_notetype", ""))
        self._notetype.setMinimumWidth(360)
        self._notetype.setPlaceholderText("e.g. Cloze")
        cf.addRow("New card note type:", self._notetype)

        self._deck = QLineEdit(qb_cfg.get("card_deck", ""))
        self._deck.setMinimumWidth(360)
        self._deck.setPlaceholderText("e.g. Default")
        cf.addRow("New card deck:", self._deck)

        self._skill = QLineEdit(qb_cfg.get("card_skill_id", ""))
        self._skill.setMinimumWidth(360)
        self._skill.setPlaceholderText("skill_01… (leave blank for none)")
        cf.addRow("Card-gen skill ID:", self._skill)

        self._field = QLineEdit(qb_cfg.get("missed_q_field", "Missed Questions"))
        self._field.setMinimumWidth(360)
        cf.addRow("Append-to field:", self._field)

        self._tag_root = QLineEdit(qb_cfg.get("tag_root", "Missed_Questions"))
        self._tag_root.setMinimumWidth(360)
        cf.addRow("Tag root:", self._tag_root)

        root.addWidget(cap_box)
        root.addStretch(1)

    # ── Platforms rebuild ────────────────────────────────────────────────────

    def _rebuild_platforms(self):
        while self._plat_list.count():
            item = self._plat_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._platforms:
            lbl = QLabel("No QBanks configured.")
            lbl.setStyleSheet("color: gray; font-size: 11px;")
            self._plat_list.addWidget(lbl)
            self._plat_list.addStretch()
            return
        for i, p in enumerate(self._platforms):
            row = QHBoxLayout()
            lbl = QLabel(f"<b>{p['name']}</b>  <span style='color:gray;font-size:11px'>{p['url']}</span>")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lbl, stretch=1)
            edit_btn = QPushButton("Edit")
            edit_btn.setFixedWidth(48)
            edit_btn.clicked.connect(lambda _, idx=i: self._edit_platform(idx))
            row.addWidget(edit_btn)
            del_btn = QPushButton("Remove")
            del_btn.setFixedWidth(64)
            del_btn.clicked.connect(lambda _, idx=i: self._remove_platform(idx))
            row.addWidget(del_btn)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self._plat_list.addWidget(wrapper)
        self._plat_list.addStretch()

    def _add_platform(self):
        dlg = _QBankEditDialog(self)
        if dlg.exec():
            p = dlg.result_platform()
            existing = {x["key"] for x in self._platforms}
            if p["key"] in existing:
                p["key"] = p["key"] + str(len(self._platforms))
            self._platforms.append(p)
            self._rebuild_platforms()

    def _edit_platform(self, idx: int):
        dlg = _QBankEditDialog(self, existing=self._platforms[idx])
        if dlg.exec():
            self._platforms[idx] = dlg.result_platform()
            self._rebuild_platforms()

    def _remove_platform(self, idx: int):
        self._platforms.pop(idx)
        self._rebuild_platforms()

    # ── Periods rebuild ──────────────────────────────────────────────────────

    def _rebuild_periods(self):
        while self._per_list.count():
            item = self._per_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._periods:
            lbl = QLabel("No exam periods — default target applies every day.")
            lbl.setStyleSheet("color: gray; font-size: 11px;")
            self._per_list.addWidget(lbl)
            self._per_list.addStretch()
            return
        self._periods.sort(key=lambda p: p.get("from", ""))
        for i, p in enumerate(self._periods):
            row = QHBoxLayout()
            lbl = QLabel(f"{p['from']}  →  {p['to']}:  <b>{p['daily']} Q/day</b>")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lbl, stretch=1)
            del_btn = QPushButton("Remove")
            del_btn.setFixedWidth(64)
            del_btn.clicked.connect(lambda _, idx=i: self._remove_period(idx))
            row.addWidget(del_btn)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self._per_list.addWidget(wrapper)
        self._per_list.addStretch()

    def _add_period(self):
        dlg = _PeriodEditDialog(self)
        if dlg.exec():
            self._periods.append(dlg.result_period())
            self._rebuild_periods()

    def _remove_period(self, idx: int):
        self._periods.pop(idx)
        self._rebuild_periods()

    # ── Exams rebuild ────────────────────────────────────────────────────────

    def _rebuild_exams(self):
        while self._exam_list.count():
            item = self._exam_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._exams:
            lbl = QLabel("No exam dates configured.")
            lbl.setStyleSheet("color: gray; font-size: 11px;")
            self._exam_list.addWidget(lbl)
            self._exam_list.addStretch()
            return
        self._exams.sort(key=lambda e: e.get("date", ""))
        for i, e in enumerate(self._exams):
            row = QHBoxLayout()
            lbl = QLabel(f"<b>{e['date']}</b>  —  {e['label']}")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lbl, stretch=1)
            del_btn = QPushButton("Remove")
            del_btn.setFixedWidth(64)
            del_btn.clicked.connect(lambda _, idx=i: self._remove_exam(idx))
            row.addWidget(del_btn)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self._exam_list.addWidget(wrapper)
        self._exam_list.addStretch()

    def _add_exam(self):
        dlg = _ExamDateEditDialog(self)
        if dlg.exec():
            self._exams.append(dlg.result_exam())
            self._rebuild_exams()

    def _remove_exam(self, idx: int):
        self._exams.pop(idx)
        self._rebuild_exams()

    def get_values(self) -> dict:
        return {
            "enabled":        self._enabled.isChecked(),
            "show_heatmap":   self._show_heatmap.isChecked(),
            "platforms":      list(self._platforms),
            "default_daily":  int(self._spin.value()),
            "target_periods": sorted(self._periods, key=lambda p: p.get("from", "")),
            "exam_dates":     sorted(self._exams,   key=lambda e: e.get("date", "")),
            "search_model":   self._search_model.text().strip() or "claude-haiku-4-5-20251001",
            "card_gen_model": self._card_model.text().strip()    or "claude-sonnet-4-6",
            "card_notetype":  self._notetype.text().strip(),
            "card_deck":      self._deck.text().strip(),
            "card_skill_id":  self._skill.text().strip(),
            "missed_q_field": self._field.text().strip()         or "Missed Questions",
            "tag_root":       self._tag_root.text().strip()      or "Missed_Questions",
        }


class _BrowseTab(QWidget):
    def __init__(self, br_cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        self._enabled = QCheckBox("Enable Browse with Claude")
        self._enabled.setChecked(bool(br_cfg.get("enabled", True)))
        layout.addRow(self._enabled)

        self._model = QLineEdit(br_cfg.get("model", "claude-sonnet-4-6"))
        self._model.setMinimumWidth(420)
        layout.addRow("Model:", self._model)

        self._last_tag = QLineEdit(br_cfg.get("last_used_tag", ""))
        self._last_tag.setMinimumWidth(420)
        self._last_tag.setPlaceholderText("e.g. School::Year3")
        layout.addRow("Last-used tag:", self._last_tag)

        self._max = QSpinBox()
        self._max.setRange(1, 1000)
        self._max.setValue(int(br_cfg.get("max_results", 50)))
        layout.addRow("Max results:", self._max)

        self._notetype_filter = QLineEdit(br_cfg.get("notetype_filter", ""))
        self._notetype_filter.setMinimumWidth(420)
        self._notetype_filter.setPlaceholderText("e.g. Cloze (leave blank to search all)")
        layout.addRow("Notetype filter:", self._notetype_filter)

        self._front_field = QLineEdit(br_cfg.get("front_field", "Text"))
        self._front_field.setMinimumWidth(420)
        layout.addRow("Front field:", self._front_field)

        self._audit_tag = QLineEdit(br_cfg.get("audit_tag", ""))
        self._audit_tag.setMinimumWidth(420)
        self._audit_tag.setPlaceholderText("e.g. Ankisstant::AI::Browse")
        layout.addRow("Audit tag:", self._audit_tag)

        self._gap = QCheckBox("Enable post-search gap report (asks Claude what's missing)")
        self._gap.setChecked(bool(br_cfg.get("enable_gap_report", False)))
        layout.addRow(self._gap)

        st_hint = QLabel("<small>Source-deck badges are edited in the addon config JSON.</small>")
        st_hint.setStyleSheet("color: gray;")
        st_hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addRow(st_hint)

        self._source_tags = list(br_cfg.get("source_tags") or [])

    def get_values(self) -> dict:
        return {
            "enabled":           self._enabled.isChecked(),
            "model":             self._model.text().strip() or "claude-sonnet-4-6",
            "last_used_tag":     self._last_tag.text().strip(),
            "max_results":       int(self._max.value()),
            "notetype_filter":   self._notetype_filter.text().strip(),
            "front_field":       self._front_field.text().strip() or "Text",
            "audit_tag":         self._audit_tag.text().strip(),
            "enable_gap_report": self._gap.isChecked(),
            "source_tags":       self._source_tags,
        }


class _GapAnalyserTab(QWidget):
    def __init__(self, ga_cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        self._enabled = QCheckBox("Enable Analyse Knowledge Gaps")
        self._enabled.setChecked(bool(ga_cfg.get("enabled", True)))
        layout.addRow(self._enabled)

        self._model = QLineEdit(ga_cfg.get("model", "claude-sonnet-4-6"))
        self._model.setMinimumWidth(420)
        layout.addRow("Model:", self._model)

        self._front_field = QLineEdit(ga_cfg.get("front_field", "Text"))
        self._front_field.setMinimumWidth(420)
        layout.addRow("Front field:", self._front_field)

        self._notetype_filter = QLineEdit(ga_cfg.get("notetype_filter", ""))
        self._notetype_filter.setMinimumWidth(420)
        self._notetype_filter.setPlaceholderText("blank = search all notetypes")
        layout.addRow("Notetype filter:", self._notetype_filter)

        self._last_tag = QLineEdit(ga_cfg.get("last_used_tag", ""))
        self._last_tag.setMinimumWidth(420)
        self._last_tag.setPlaceholderText("most-recent tag used (auto-saved)")
        layout.addRow("Last-used tag:", self._last_tag)

        self._max_cards = QSpinBox()
        self._max_cards.setRange(5, 500)
        self._max_cards.setValue(int(ga_cfg.get("max_cards", 80)))
        layout.addRow("Max cards sent to Claude:", self._max_cards)

        self._max_gaps = QSpinBox()
        self._max_gaps.setRange(1, 30)
        self._max_gaps.setValue(int(ga_cfg.get("max_gaps", 10)))
        layout.addRow("Max gaps to return:", self._max_gaps)

        hint = QLabel(
            "<small>This tool pulls cards under a tag, asks Claude what's missing, "
            "and pushes the approved gaps into the Knowledge Gaps queue.</small>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)
        layout.addRow(hint)

    def get_values(self) -> dict:
        return {
            "enabled":         self._enabled.isChecked(),
            "model":           self._model.text().strip() or "claude-sonnet-4-6",
            "front_field":     self._front_field.text().strip() or "Text",
            "notetype_filter": self._notetype_filter.text().strip(),
            "last_used_tag":   self._last_tag.text().strip(),
            "max_cards":       int(self._max_cards.value()),
            "max_gaps":        int(self._max_gaps.value()),
        }


class _KnowledgeGapsTab(QWidget):
    def __init__(self, kg_cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        self._enabled = QCheckBox("Enable Knowledge Gaps tab")
        self._enabled.setChecked(bool(kg_cfg.get("enabled", True)))
        layout.addRow(self._enabled)

        self._show_home_button = QCheckBox(
            "Show ＋ KG button on the deck browser home screen"
        )
        self._show_home_button.setChecked(bool(kg_cfg.get("show_home_button", True)))
        layout.addRow(self._show_home_button)

        self._confirm_on_delete = QCheckBox(
            "Confirm before deleting a KG"
        )
        self._confirm_on_delete.setChecked(bool(kg_cfg.get("confirm_on_delete", True)))
        layout.addRow(self._confirm_on_delete)

        hint = QLabel(
            "<small>The Knowledge Gaps tab is the unified queue for things you "
            "don't know — from manual notes, the Analyse KG sub-feature, captured "
            "QBank misses, or items saved from Browse. From any KG you can send "
            "to Browse with Claude, or create a card directly.</small>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)
        layout.addRow(hint)

    def get_values(self) -> dict:
        return {
            "enabled":               self._enabled.isChecked(),
            "show_home_button":      self._show_home_button.isChecked(),
            "confirm_on_delete":     self._confirm_on_delete.isChecked(),
            "default_status_on_add": "open",
        }


class _CreatorTab(QWidget):
    def __init__(self, cc_cfg: dict, parent=None):
        super().__init__(parent)
        # Snapshot the profile list so add/edit/remove dialogs operate on a
        # working copy that we only persist on Save.
        self._profiles: list[dict] = [dict(p) for p in cc_cfg.get("notetypes", [])]
        # Remember the prior selected notetype so we can preserve it across
        # the settings dialog even if the profile list is re-ordered.
        self._selected_notetype: str = cc_cfg.get("selected_notetype", "") \
            or cc_cfg.get("default_notetype", "")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ── Basic options (top form) ─────────────────────────────────────────
        top_form = _expand_form(QFormLayout())
        top_form.setVerticalSpacing(10)

        self._enabled = QCheckBox("Enable Create with Claude")
        self._enabled.setChecked(bool(cc_cfg.get("enabled", True)))
        top_form.addRow(self._enabled)

        self._model = QLineEdit(cc_cfg.get("model", "claude-sonnet-4-6"))
        self._model.setMinimumWidth(420)
        top_form.addRow("Model:", self._model)

        self._deck = QComboBox()
        self._deck.setEditable(True)
        self._deck.setMinimumWidth(420)
        try:
            from aqt import mw as _mw
            if _mw.col is not None:
                for n in sorted(d.name for d in _mw.col.decks.all_names_and_ids()):
                    self._deck.addItem(n)
        except Exception:
            pass
        default_deck = cc_cfg.get("default_deck", "")
        if default_deck:
            idx = self._deck.findText(default_deck)
            if idx >= 0:
                self._deck.setCurrentIndex(idx)
            else:
                self._deck.setEditText(default_deck)
        top_form.addRow("Default deck:", self._deck)

        self._tags = QLineEdit(", ".join(cc_cfg.get("default_tags", [])))
        self._tags.setMinimumWidth(420)
        self._tags.setPlaceholderText("comma-separated")
        top_form.addRow("Default tags:", self._tags)

        self._audit_tag = QLineEdit(cc_cfg.get("audit_tag", ""))
        self._audit_tag.setMinimumWidth(420)
        top_form.addRow("Audit tag:", self._audit_tag)

        self._n_cards = QSpinBox()
        self._n_cards.setRange(1, 40)
        self._n_cards.setValue(int(cc_cfg.get("default_n_cards", 10)))
        top_form.addRow("Default # cards:", self._n_cards)

        self._gap_n_cards = QSpinBox()
        self._gap_n_cards.setRange(1, 20)
        self._gap_n_cards.setValue(int(cc_cfg.get("gap_n_cards", 3)))
        top_form.addRow("# cards per LO gap:", self._gap_n_cards)

        root.addLayout(top_form)

        # ── Notetype profiles ────────────────────────────────────────────────
        nt_box = QGroupBox("Notetypes")
        nt_layout = QVBoxLayout(nt_box)
        nt_hint = QLabel(
            "The creator panel's notetype dropdown lists these profiles. "
            "Each profile maps a notetype to its field layout and can carry "
            "its own prompt addendum so Claude tailors output to that style "
            "(e.g. the Malleus deck). Add as many as you like."
        )
        nt_hint.setWordWrap(True)
        nt_hint.setStyleSheet("color: gray; font-size: 11px;")
        nt_layout.addWidget(nt_hint)

        self._nt_scroll, _nt_inner, self._nt_list = _scroll_list(140, 280)
        nt_layout.addWidget(self._nt_scroll)

        add_nt = QPushButton("+ Add notetype")
        add_nt.clicked.connect(self._add_profile)
        nt_layout.addWidget(add_nt)
        root.addWidget(nt_box)
        self._rebuild_profiles()

        root.addStretch(1)

    # ── notetype profile list ────────────────────────────────────────────────

    def _rebuild_profiles(self):
        while self._nt_list.count():
            item = self._nt_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._profiles:
            lbl = QLabel(
                "No notetype profiles configured. Add one to make it "
                "available in the creator panel's dropdown."
            )
            lbl.setStyleSheet("color: gray; font-size: 11px;")
            lbl.setWordWrap(True)
            self._nt_list.addWidget(lbl)
            self._nt_list.addStretch()
            return
        for i, p in enumerate(self._profiles):
            row = QHBoxLayout()
            instr_marker = " · custom prompt" if p.get("extra_instructions") else ""
            skill_marker = ""
            if p.get("card_creation_skill_invocation") or p.get("card_creation_skill_id"):
                skill_marker = " · skill"
            sources_marker = ""
            if p.get("sources_field"):
                sources_marker = f" · src=<code>{p['sources_field']}</code>"
            lbl = QLabel(
                f"<b>{p['name']}</b>"
                f"  <span style='color:gray;font-size:11px'>"
                f"front=<code>{p.get('front_field', 'Text')}</code> · "
                f"extra=<code>{p.get('extra_field', 'Extra')}</code> · "
                f"image=<code>{p.get('image_field', p.get('extra_field', 'Extra'))}</code>"
                f"{sources_marker}{skill_marker}{instr_marker}</span>"
            )
            lbl.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lbl, stretch=1)
            edit_btn = QPushButton("Edit")
            edit_btn.setFixedWidth(48)
            edit_btn.clicked.connect(lambda _, idx=i: self._edit_profile(idx))
            row.addWidget(edit_btn)
            del_btn = QPushButton("Remove")
            del_btn.setFixedWidth(64)
            del_btn.clicked.connect(lambda _, idx=i: self._remove_profile(idx))
            row.addWidget(del_btn)
            wrapper = QWidget()
            wrapper.setLayout(row)
            self._nt_list.addWidget(wrapper)
        self._nt_list.addStretch()

    def _add_profile(self):
        dlg = _NotetypeProfileDialog(self)
        if dlg.exec():
            self._profiles.append(dlg.result_profile())
            self._rebuild_profiles()

    def _edit_profile(self, idx: int):
        dlg = _NotetypeProfileDialog(self, existing=self._profiles[idx])
        if dlg.exec():
            self._profiles[idx] = dlg.result_profile()
            self._rebuild_profiles()

    def _remove_profile(self, idx: int):
        self._profiles.pop(idx)
        self._rebuild_profiles()

    def get_values(self) -> dict:
        tags_raw = [t.strip() for t in self._tags.text().split(",") if t.strip()]
        # Keep selected_notetype valid — if the previously selected notetype
        # was deleted, fall back to the first available profile (or blank).
        names = [p.get("name", "") for p in self._profiles]
        selected = self._selected_notetype if self._selected_notetype in names else (names[0] if names else "")
        # Legacy field names stay in config so older code (and the migration
        # path) keeps working, but the panel reads from the profile list.
        first = self._profiles[0] if self._profiles else {}
        return {
            "enabled":          self._enabled.isChecked(),
            "model":            self._model.text().strip() or "claude-sonnet-4-6",
            "default_deck":     self._deck.currentText().strip(),
            "default_tags":     tags_raw,
            "audit_tag":        self._audit_tag.text().strip(),
            "default_n_cards":  int(self._n_cards.value()),
            "gap_n_cards":      int(self._gap_n_cards.value()),
            "notetypes":        list(self._profiles),
            "selected_notetype": selected,
            "default_notetype": selected,  # legacy mirror
            "front_field":      first.get("front_field", "Text"),
            "extra_field":      first.get("extra_field", "Extra"),
            "one_by_one_field": first.get("one_by_one_field", "One by one"),
        }


# ── about tab ────────────────────────────────────────────────────────────────

def _addon_version() -> str:
    try:
        import json as _json
        import os as _os
        here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        with open(_os.path.join(here, "manifest.json"), encoding="utf-8") as fh:
            return _json.load(fh).get("version", "?")
    except Exception:
        return "?"


class _AboutTab(QWidget):
    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)

        title = QLabel(f"<h2 style='margin:0'>Ankisstant {_addon_version()}</h2>")
        title.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(title)

        from aqt.qt import QTextBrowser
        body = QTextBrowser()
        body.setOpenExternalLinks(True)
        body.setHtml(
            "<p>Four Claude-powered tools, bundled together:</p>"
            "<ul>"
            "<li><b>QBank with Claude</b> — log missed QBank questions and "
            "generate cards from them.</li>"
            "<li><b>Browse with Claude</b> — natural-language search across "
            "your collection.</li>"
            "<li><b>Analyse Knowledge Gaps</b> — audit a tag against a learning "
            "objective.</li>"
            "<li><b>Create with Claude</b> — generate cards from text, URLs, "
            "PDFs, or PowerPoints.</li>"
            "</ul>"
            "<p><b>No telemetry.</b> Nothing is sent anywhere except your chosen "
            "Claude provider (Anthropic API or the local Claude Code CLI).</p>"
            "<p>Anki collection access stays local. Your API key and config live "
            "in this profile's <code>meta.json</code> only.</p>"
        )
        body.setMinimumHeight(260)
        v.addWidget(body, 1)

        btn_row = QHBoxLayout()
        rerun_btn = QPushButton("Re-run welcome wizard…")
        rerun_btn.clicked.connect(self._rerun_welcome)
        btn_row.addWidget(rerun_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        notices = QLabel(
            "<small>Bundles ported code from "
            "<a href='https://ankiweb.net/shared/info/1021636467'>"
            "Card Management</a> by Ren Tatsumoto (AGPL v3). See "
            "<code>NOTICES.md</code> in the addon folder for details.</small>"
        )
        notices.setTextFormat(Qt.TextFormat.RichText)
        notices.setOpenExternalLinks(True)
        notices.setWordWrap(True)
        v.addWidget(notices)

        v.addStretch(1)

    def _rerun_welcome(self):
        try:
            from .welcome import open_welcome
            open_welcome()
        except Exception as e:
            tooltip(f"Couldn't open welcome: {e}")


# ── main dialog ──────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_cfg = load_config()
        self.setWindowTitle("Ankisstant — Settings")
        self.setMinimumSize(820, 680)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tabs = QTabWidget()
        self._global_tab  = _GlobalTab(self._original_cfg)
        self._qbank_tab   = _QBankTab(self._original_cfg.get("tools", {}).get("qbank", {}))
        self._browse_tab  = _BrowseTab(self._original_cfg.get("tools", {}).get("browse", {}))
        self._gap_tab     = _GapAnalyserTab(self._original_cfg.get("tools", {}).get("gap_analyser", {}))
        self._kg_tab      = _KnowledgeGapsTab(self._original_cfg.get("tools", {}).get("knowledge_gaps", {}))
        self._creator_tab = _CreatorTab(self._original_cfg.get("tools", {}).get("card_creator", {}))
        self._about_tab   = _AboutTab()
        tabs.addTab(_wrap_scroll(self._global_tab),  "Global")
        tabs.addTab(_wrap_scroll(self._qbank_tab),   "QBank with Claude")
        tabs.addTab(_wrap_scroll(self._browse_tab),  "Browse with Claude")
        tabs.addTab(_wrap_scroll(self._kg_tab),      "Knowledge Gaps")
        tabs.addTab(_wrap_scroll(self._gap_tab),     "Analyse Knowledge Gaps")
        tabs.addTab(_wrap_scroll(self._creator_tab), "Create with Claude")
        tabs.addTab(_wrap_scroll(self._about_tab),   "About")
        root.addWidget(tabs)

        restart_hint = QLabel(
            "<small>Some changes (enable/disable a tool, change deck-browser hooks) "
            "require an Anki restart to fully take effect.</small>"
        )
        restart_hint.setTextFormat(Qt.TextFormat.RichText)
        restart_hint.setStyleSheet("color: gray;")
        restart_hint.setWordWrap(True)
        root.addWidget(restart_hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _on_save(self):
        cfg = load_config()
        cfg.update(self._global_tab.get_values())
        cfg.setdefault("tools", {})
        cfg["tools"]["qbank"]        = {**cfg["tools"].get("qbank", {}),        **self._qbank_tab.get_values()}
        cfg["tools"]["browse"]       = {**cfg["tools"].get("browse", {}),       **self._browse_tab.get_values()}
        cfg["tools"]["gap_analyser"]   = {**cfg["tools"].get("gap_analyser", {}),   **self._gap_tab.get_values()}
        cfg["tools"]["knowledge_gaps"] = {**cfg["tools"].get("knowledge_gaps", {}), **self._kg_tab.get_values()}
        cfg["tools"]["card_creator"]   = {**cfg["tools"].get("card_creator", {}),   **self._creator_tab.get_values()}
        save_config(cfg)
        tooltip("Settings saved.")
        self.accept()


def open_settings() -> None:
    dlg = SettingsDialog()
    dlg.exec()
