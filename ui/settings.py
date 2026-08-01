# Unified settings dialog for Ankisstant.
# Tabs: AI, Tools, QBank, Browse, Knowledge Gaps, Create, About.
# AI tab owns the shared API key, CLI path and provider mode.
# Each tool tab edits its tools[<key>] config slice.

from __future__ import annotations

import re
from datetime import date

from aqt.qt import (
    QApplication, QCheckBox, QColor, QColorDialog, QComboBox, QDate,
    QDateEdit, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QKeySequence, QKeySequenceEdit, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, Qt,
    QTabWidget, QVBoxLayout, QWidget,
)
from aqt.utils import showInfo, showWarning, tooltip

from ..core import api as core_api
from ..core.config import (
    DEFAULTS, PROVIDER_MODELS, active_family, ai_tools, family_for, load_config,
    save_config, tool_config, tool_enabled,
)
from ..core.qt_utils import attach_deck_completer, attach_tag_completer
from ..tools import skills_catalog


def _model_combo(current: str, fallback: str = "claude-sonnet-4-6",
                 min_width: int = 320, models: list[str] | None = None) -> QComboBox:
    """Editable combobox seeded with `models` (defaults to the Anthropic list).
    currentText() returns the model ID — either a picked one or whatever the
    user typed."""
    cb = QComboBox()
    cb.setEditable(True)
    cb.addItems(models if models is not None else PROVIDER_MODELS["anthropic"])
    cur = (current or "").strip() or fallback
    if cur and cb.findText(cur) < 0:
        cb.addItem(cur)
    cb.setCurrentText(cur)
    cb.setMinimumWidth(min_width)
    cb.setToolTip("Pick a known model or type a custom model ID.")
    return cb


def _coerce_model_dict(stored, default_dict: dict) -> dict:
    """Normalise a stored model field (dict or legacy string) into a full
    {family: id} dict, backfilling missing families from `default_dict`."""
    if isinstance(stored, dict):
        return {**default_dict, **{k: v for k, v in stored.items() if v}}
    if isinstance(stored, str) and stored.strip():
        return {**default_dict, "anthropic": stored.strip()}
    return dict(default_dict)


class _ModelField(QWidget):
    """Provider-aware model picker. Holds one model ID per family and shows the
    one for the active family; switching family preserves each family's pick so
    a user who jumps between providers keeps their per-provider choices."""

    def __init__(self, stored, default_dict: dict, family: str,
                 min_width: int = 320, parent=None):
        super().__init__(parent)
        self._models = _coerce_model_dict(stored, default_dict)
        self._family = family
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._cb = QComboBox()
        self._cb.setEditable(True)
        self._cb.setMinimumWidth(min_width)
        self._cb.setToolTip(
            "Pick a known model or type a custom model ID. Saved separately per "
            "provider, so switching providers keeps each one's choice."
        )
        lay.addWidget(self._cb)
        self._populate()

    def _populate(self) -> None:
        self._cb.blockSignals(True)
        self._cb.clear()
        self._cb.addItems(PROVIDER_MODELS.get(self._family, []))
        cur = (self._models.get(self._family) or "").strip()
        if not cur:
            known = PROVIDER_MODELS.get(self._family, [""])
            cur = known[0] if known else ""
        if cur and self._cb.findText(cur) < 0:
            self._cb.addItem(cur)
        self._cb.setCurrentText(cur)
        self._cb.blockSignals(False)

    def _capture(self) -> None:
        self._models[self._family] = self._cb.currentText().strip()

    def set_family(self, family: str) -> None:
        self._capture()
        self._family = family
        self._populate()

    def values(self) -> dict:
        self._capture()
        return dict(self._models)


class _SkillField(QWidget):
    """Provider-channel-aware skill editor for an AI matrix tool. Holds two
    channels — `anthropic` (an Anthropic custom skill_id) and `cli` (a Claude
    Code invocation like '/malleus-anki') — and shows the one that matches the
    current provider. Other providers (gemini/openai/ollama/manual) can't use
    skills, so the field is disabled with an explanatory placeholder. Switching
    provider preserves each channel's value, mirroring _ModelField."""

    def __init__(self, stored: dict | None, provider: str, min_width: int = 360, parent=None):
        super().__init__(parent)
        s = stored if isinstance(stored, dict) else {}
        self._channels = {"anthropic": (s.get("anthropic") or "").strip(),
                          "cli": (s.get("cli") or "").strip()}
        self._provider = (provider or "auto").lower()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._edit = QLineEdit()
        self._edit.setMinimumWidth(min_width)
        lay.addWidget(self._edit)
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: gray;")
        self._hint.setTextFormat(Qt.TextFormat.RichText)
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)
        self._populate()

    @staticmethod
    def _channel_for(provider: str) -> str:
        return "cli" if provider == "cli" else "anthropic"

    @staticmethod
    def _supported(provider: str) -> bool:
        return provider in ("auto", "cli", "anthropic")

    def _populate(self) -> None:
        ch = self._channel_for(self._provider)
        supported = self._supported(self._provider)
        self._edit.blockSignals(True)
        self._edit.setText(self._channels.get(ch, ""))
        self._edit.setEnabled(supported)
        if not supported:
            self._edit.setPlaceholderText("Skills are Anthropic-only — ignored on this provider")
            self._hint.setText(
                "<small>This provider can't run skills; the inline prompt is used instead.</small>")
        elif ch == "cli":
            self._edit.setPlaceholderText("/skill-name  or  Use the <name> skill")
            self._hint.setText(
                "<small>CLI invocation — the skill body lives at "
                "<code>~/.claude/skills/&lt;name&gt;/SKILL.md</code>.</small>")
        else:
            self._edit.setPlaceholderText("skill_… (Anthropic custom skill ID)")
            self._hint.setText(
                "<small>Anthropic custom skill ID (skills beta). Leave blank for none.</small>")
        self._edit.blockSignals(False)

    def _capture(self) -> None:
        ch = self._channel_for(self._provider)
        if self._supported(self._provider):
            self._channels[ch] = self._edit.text().strip()

    def set_provider(self, provider: str) -> None:
        self._capture()
        self._provider = (provider or "auto").lower()
        self._populate()

    def values(self) -> dict:
        self._capture()
        return dict(self._channels)


class _OverridableModel(QWidget):
    """A per-tool model picker that opts into overriding the consolidated AI-tab
    default. The checkbox drives `<field>_override`; when off, the tool reads the
    shared ai_matrix value and the picker is disabled. Lets a tool tab pin its own
    model (the 'hybrid' model) without scattering the primary choice."""

    def __init__(self, stored, default_dict: dict, family: str, override: bool,
                 *, label: str = "Use a model just for this tool", min_width: int = 360,
                 parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self._chk = QCheckBox(label + "  (otherwise uses the AI-tab default)")
        self._chk.setChecked(bool(override))
        self._chk.setToolTip(
            "Off: this tool uses the model set in Settings → AI for its tool row. "
            "On: pin a different model here, just for this tool.")
        lay.addWidget(self._chk)
        self._model = _ModelField(stored, default_dict, family, min_width=min_width)
        lay.addWidget(self._model)
        self._chk.toggled.connect(self._model.setEnabled)
        self._model.setEnabled(self._chk.isChecked())

    def set_family(self, family: str) -> None:
        self._model.set_family(family)

    def set_enabled_all(self, enabled: bool) -> None:
        """Hide/show in manual mode (no model needed)."""
        self.setVisible(enabled)

    def values(self) -> tuple[dict, bool]:
        return self._model.values(), self._chk.isChecked()


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


def _synapse_theme(widget: QWidget, dialog: bool = False) -> None:
    """Apply the SynapsePro bridge stylesheet, if there is one.

    A no-op without SynapsePro, so every window that calls this looks exactly as
    it did before the bridge existed. Applied *after* the layout is built, so
    per-widget stylesheets that encode meaning (status colours, queue
    highlights) still win over the blanket sheet.
    """
    try:
        from ..core import synapse
        synapse.apply_stylesheet(widget, dialog=dialog)
    except Exception:
        pass


def _expand_form(layout: QFormLayout) -> QFormLayout:
    """Make QFormLayout field columns fill the row width."""
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    return layout


def _set_form_row_visible(form: QFormLayout, field: QWidget, visible: bool) -> None:
    """Show/hide a whole form row (label + field) by its field widget. Uses
    Qt 6.4+ setRowVisible when available, else hides both widgets manually."""
    try:
        form.setRowVisible(field, visible)
        return
    except Exception:
        pass
    field.setVisible(visible)
    try:
        lbl = form.labelForField(field)
        if lbl is not None:
            lbl.setVisible(visible)
    except Exception:
        pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())[:24] or "qbank"


def _link_checkbox(a: QCheckBox, b: QCheckBox) -> None:
    """Keep two checkboxes that edit the same shared setting in sync — Qt
    only re-emits `toggled` when the value actually changes, so this can't
    loop. Used for "Month tag", which is shown on both the Browse and Create
    tabs (it's a single global setting, just relevant to both flows)."""
    a.toggled.connect(b.setChecked)
    b.toggled.connect(a.setChecked)


def _link_lineedit(a: QLineEdit, b: QLineEdit) -> None:
    """Keep two line edits that edit the same shared setting in sync (see
    `_link_checkbox` — same no-loop guarantee via Qt's change-only emission)."""
    a.textChanged.connect(b.setText)
    b.textChanged.connect(a.setText)


def _month_tag_group(cfg: dict) -> tuple[QGroupBox, QCheckBox, QLineEdit]:
    """Build a 'Month tag' group box — shown on both the Browse and Create
    tabs since both flows can apply it. Returns the box plus the checkbox/
    line-edit so the caller can sync the two copies and read back values."""
    box = QGroupBox("Month tag")
    form = _expand_form(QFormLayout(box))
    form.setVerticalSpacing(8)
    chk = QCheckBox("Add a month tag to created & unsuspended cards")
    chk.setChecked(bool(cfg.get("month_tag_enabled", True)))
    chk.setToolTip(
        "When on, every card you create (Create) or unsuspend/tag (Browse) "
        "also gets a tag <prefix>::<YYYY-MM> for the current month."
    )
    form.addRow(chk)
    prefix = QLineEdit(str(cfg.get("month_tag_prefix", "Ankisstant::Month") or ""))
    prefix.setMinimumWidth(360)
    prefix.setPlaceholderText("e.g. Ankisstant::Month")
    prefix.setToolTip(
        "Root of the month tag. The current month (YYYY-MM) is appended, "
        "e.g. Ankisstant::Month::2026-05."
    )
    form.addRow("Prefix:", prefix)
    return box, chk, prefix


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
        _synapse_theme(self, dialog=True)

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
        _synapse_theme(self, dialog=True)

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

        self._slide = QLineEdit(e.get("slide_field", ""))
        self._slide.setMinimumWidth(360)
        self._slide.setPlaceholderText(
            "Field for lecture-slide images from a PDF (blank → 'Lecture Notes', else Extra)"
        )
        form.addRow("Lecture/slides field:", self._slide)

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

        # ── Card creation: skill OR prompt (mutually exclusive) ──────────
        # A notetype either delegates to a skill (cheaper — the skill carries the
        # instructions) or uses the inline prompt/field instructions below. Never
        # both: a skill's tuned output would fight the inline guidance.
        self._cc_mode = QComboBox()
        self._cc_mode.addItems(["Use a skill (cheaper)", "Use prompt instructions"])
        _mode0 = str(e.get("card_creation_mode") or
                     ("skill" if (e.get("card_creation_skill_id")
                                  or e.get("card_creation_skill_invocation")) else "prompt")).lower()
        self._cc_mode.setCurrentIndex(0 if _mode0 == "skill" else 1)
        self._cc_mode.setToolTip(
            "Skill: the AI loads your skill (its instructions live in the skill, not "
            "the request) — cheaper per card. Prompt: send the inline instructions "
            "below. The two are mutually exclusive.")
        self._cc_mode.currentIndexChanged.connect(self._sync_cc_mode)
        form.addRow("Card creation uses:", self._cc_mode)

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
        self._sync_cc_mode()  # grey out skill fields when in prompt mode

        # ── Quality-pass override (per-notetype) ─────────────────────────
        # The cloze rubric doesn't fit Q&A notetypes (e.g. Malleus), so each
        # profile can force the quality pass on/off regardless of the global
        # toggle. 'Inherit' (default) follows the global setting.
        self._qp_override = QComboBox()
        self._qp_override.addItems(["Inherit global", "Force ON", "Force OFF"])
        self._qp_override.setCurrentIndex(
            {"inherit": 0, "on": 1, "off": 2}.get(
                str(e.get("quality_pass_override", "inherit")).lower(), 0)
        )
        self._qp_override.setToolTip(
            "Whether the card quality pass scores cards from this notetype. "
            "Set 'Force OFF' for Q&A notetypes — the rubric is cloze-only."
        )
        form.addRow("Quality pass:", self._qp_override)

        # ── Source grounding override (per-notetype) ─────────────────────
        # Grounding injects the selected region's guideline citation allow-list in
        # topic-mode generation. Global default in Settings → Create; this lets
        # a profile force it on/off. 'Inherit' (default) follows the global.
        self._grounding_override = QComboBox()
        self._grounding_override.addItems(["Inherit global", "Force ON", "Force OFF"])
        self._grounding_override.setCurrentIndex(
            {"inherit": 0, "on": 1, "off": 2}.get(
                str(e.get("grounding_override", "inherit")).lower(), 0)
        )
        self._grounding_override.setToolTip(
            "Whether topic-mode cards for this notetype are grounded in your "
            "selected region's guideline allow-list. 'Inherit' follows Settings → Create."
        )
        form.addRow("Grounding:", self._grounding_override)

        # ── Card format ──────────────────────────────────────────────────
        # 'cloze' uses the cloze generator + scorer; 'qa' uses the Q&A prompt
        # (front=question, extra=answer) — e.g. the bundled Malleus Q&A notetype.
        self._card_format = QComboBox()
        self._card_format.addItems(["Cloze deletion", "Q&A (front / back)"])
        self._card_format.setCurrentIndex(
            1 if str(e.get("card_format", "cloze")).lower() == "qa" else 0
        )
        self._card_format.setToolTip(
            "Cloze: standard {{c1::…}} cards graded by the cloze rubric.\n"
            "Q&A: a question on the front and the answer on the back (no cloze). "
            "Pair with 'Quality pass: Force OFF' — the rubric is cloze-only."
        )
        form.addRow("Card format:", self._card_format)

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

        root.addWidget(QLabel("Extra prompt instructions for AI (optional):"))
        self._instructions = QPlainTextEdit(e.get("extra_instructions", ""))
        self._instructions.setPlaceholderText(
            "e.g. 'Malleus style — front fact only, no clinical context. "
            "Use field <Mnemonic> for memory aids instead of Extra.'\n\n"
            "AI will be told which fields this notetype has and will follow "
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
        _synapse_theme(self, dialog=True)

    def _cc_skill_mode(self) -> str:
        return "skill" if self._cc_mode.currentIndex() == 0 else "prompt"

    def _sync_cc_mode(self, *_args) -> None:
        """Skill fields are live only in 'skill' mode; the inline-prompt box is the
        focus in 'prompt' mode (always editable, but it's what's actually sent)."""
        on = self._cc_skill_mode() == "skill"
        self._skill_invocation.setEnabled(on)
        self._skill_id.setEnabled(on)

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
            "slide_field":        self._slide.text().strip(),
            "sources_field":      self._sources.text().strip(),
            "one_by_one_field":   self._obo.text().strip()   or "One by one",
            "card_creation_mode":             self._cc_skill_mode(),
            "card_creation_skill_invocation": self._skill_invocation.text().strip(),
            "card_creation_skill_id":         self._skill_id.text().strip(),
            "quality_pass_override": ["inherit", "on", "off"][self._qp_override.currentIndex()],
            "grounding_override": ["inherit", "on", "off"][self._grounding_override.currentIndex()],
            "card_format": "qa" if self._card_format.currentIndex() == 1 else "cloze",
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
        _synapse_theme(self, dialog=True)

    def _on_accept(self):
        if self._label.text().strip():
            self.accept()

    def result_exam(self) -> dict:
        return {
            "date":  self._date.date().toString("yyyy-MM-dd"),
            "label": self._label.text().strip(),
        }


# ── tab widgets ──────────────────────────────────────────────────────────────

_PROVIDER_LABELS = [
    ("auto",      "Auto — Claude CLI, fall back to Anthropic API"),
    ("cli",       "Claude Code CLI only (subscription)"),
    ("anthropic", "Anthropic API"),
    ("gemini",    "Gemini API (free tier)"),
    ("openai",    "OpenAI API"),
    ("ollama",    "Ollama (local model, no key)"),
    ("manual",    "BYO AI — paste from any chatbot"),
]

# Per-family key metadata for the dynamic key field.
_KEY_META = {
    "anthropic": ("Anthropic API key:", "sk-ant-…",
                  "anthropic_api_key", "console.anthropic.com"),
    "gemini":    ("Gemini API key:",    "AIza…",
                  "gemini_api_key",    "aistudio.google.com/apikey"),
    "openai":    ("OpenAI API key:",    "sk-…",
                  "openai_api_key",    "platform.openai.com/api-keys"),
}


def _key_family_for(provider: str) -> str | None:
    """Which API key the dynamic field edits for a given provider. CLI uses no
    key; auto edits the Anthropic key (its fallback path)."""
    if provider in ("anthropic", "auto"):
        return "anthropic"
    if provider in ("gemini", "openai"):
        return provider
    return None  # cli


def _matrix_tool_in_use(matrix_key: str) -> bool:
    """Whether a per-tool model row on the AI tab corresponds to a tool the
    user has actually selected (Settings → Tools). Greyed out otherwise —
    e.g. no point seeing a 'Card creation' model picker if only Browse is on.
    Mirrors the call sites of `tool_model_for(<matrix_key>, ...)`."""
    if matrix_key == "card_creation":
        return tool_enabled("card_creator")
    if matrix_key == "search":
        return tool_enabled("browse") and not tool_config("browse").get("native_only")
    if matrix_key == "native_search":
        return tool_enabled("browse")
    if matrix_key == "gap_analysis":
        return tool_enabled("knowledge_gaps") and tool_enabled("gap_analyser")
    if matrix_key == "quality_pass":
        return tool_enabled("card_creator")
    return True


class _AITab(QWidget):
    """Dedicated AI/provider screen: pick a provider, paste its key, choose a
    default model. The key field and CLI rows adapt to the selected provider."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.on_provider_changed = None  # set by SettingsDialog to fan out
        self._keys = {
            "anthropic": cfg.get("anthropic_api_key", "") or "",
            "gemini":    cfg.get("gemini_api_key", "") or "",
            "openai":    cfg.get("openai_api_key", "") or "",
        }
        self._cur_key_family: str | None = None

        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        intro = QLabel(
            "Choose how Ankisstant talks to an AI. Use the local <b>Claude Code "
            "CLI</b> (your subscription, no key needed), or a paid/free API key "
            "from <b>Anthropic</b>, <b>Google Gemini</b>, or <b>OpenAI</b>."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addRow(intro)

        self._provider = QComboBox()
        for data, label in _PROVIDER_LABELS:
            self._provider.addItem(label, data)
        idx = max(0, self._provider.findData((cfg.get("provider") or "auto").lower()))
        self._provider.setCurrentIndex(idx)
        layout.addRow("Provider:", self._provider)

        # Dynamic API-key row (label + placeholder change per provider).
        self._key_label = QLabel("API key:")
        self._key_container = QWidget()
        kc = QHBoxLayout(self._key_container)
        kc.setContentsMargins(0, 0, 0, 0)
        self._key = QLineEdit()
        self._key.setMinimumWidth(420)
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        kc.addWidget(self._key, 1)
        self._show_key = QPushButton("Show")
        self._show_key.setCheckable(True)
        self._show_key.setFixedWidth(56)
        self._show_key.toggled.connect(self._toggle_key_visibility)
        kc.addWidget(self._show_key)
        layout.addRow(self._key_label, self._key_container)

        self._key_hint = QLabel("")
        self._key_hint.setTextFormat(Qt.TextFormat.RichText)
        self._key_hint.setStyleSheet("color: gray;")
        self._key_hint.setWordWrap(True)
        layout.addRow("", self._key_hint)

        # CLI-only rows, grouped so they can be shown/hidden together.
        self._cli_container = QWidget()
        cli_form = _expand_form(QFormLayout(self._cli_container))
        cli_form.setContentsMargins(0, 0, 0, 0)
        self._cli_path = QLineEdit(cfg.get("claude_cli_path", ""))
        self._cli_path.setMinimumWidth(480)
        self._cli_path.setPlaceholderText("Auto-detect (leave blank) — e.g. /usr/local/bin/claude")
        cli_form.addRow("Claude CLI path:", self._cli_path)
        cli_hint = QLabel(
            "<small>Leave blank to auto-detect. On macOS, GUI apps don't inherit your "
            "shell PATH, so you may need to set this explicitly. Run "
            "<code>which claude</code> to find it.</small>"
        )
        cli_hint.setWordWrap(True)
        cli_hint.setTextFormat(Qt.TextFormat.RichText)
        cli_hint.setStyleSheet("color: gray;")
        cli_form.addRow("", cli_hint)
        self._cli_extra = QLineEdit(" ".join(cfg.get("claude_cli_extra_args") or []))
        self._cli_extra.setMinimumWidth(480)
        self._cli_extra.setPlaceholderText("e.g. --permission-mode bypassPermissions")
        cli_form.addRow("CLI extra args:", self._cli_extra)
        layout.addRow("", self._cli_container)

        # Ollama-only row: the local server URL (no API key needed).
        self._ollama_container = QWidget()
        ollama_form = _expand_form(QFormLayout(self._ollama_container))
        ollama_form.setContentsMargins(0, 0, 0, 0)
        self._ollama_url = QLineEdit(cfg.get("ollama_url", "") or "http://localhost:11434")
        self._ollama_url.setMinimumWidth(480)
        self._ollama_url.setPlaceholderText("http://localhost:11434")
        ollama_form.addRow("Ollama server URL:", self._ollama_url)
        ollama_hint = QLabel(
            "<small>No API key needed. Install Ollama (ollama.com), run "
            "<code>ollama serve</code>, and <code>ollama pull &lt;model&gt;</code> "
            "for the model name you set below.</small>"
        )
        ollama_hint.setWordWrap(True)
        ollama_hint.setTextFormat(Qt.TextFormat.RichText)
        ollama_hint.setStyleSheet("color: gray;")
        ollama_form.addRow("", ollama_hint)
        layout.addRow("", self._ollama_container)

        self._default_model = _ModelField(
            cfg.get("model_defaults"), DEFAULTS["model_defaults"],
            family_for(cfg.get("provider", "auto")), min_width=360,
        )
        layout.addRow("Fallback model:", self._default_model)

        # ── Per-tool model & skill matrix ───────────────────────────────────
        # The single place model (and skill) choices live. Each AI tool gets one
        # model per provider, set here; the tool tabs only override. Skill-capable
        # tools also get a provider-aware skill field.
        prov = (cfg.get("provider") or "auto").lower()
        fam0 = family_for(prov)
        matrix_cfg = cfg.get("ai_matrix") or {}
        self._matrix_box = QGroupBox("Per-tool models")
        mf = _expand_form(QFormLayout(self._matrix_box))
        mf.setVerticalSpacing(8)
        intro_m = QLabel(
            "<small>One model per tool, used everywhere that tool runs. Skills are "
            "managed in the <b>Skills catalog</b> below.</small>")
        intro_m.setWordWrap(True)
        intro_m.setTextFormat(Qt.TextFormat.RichText)
        intro_m.setStyleSheet("color: gray;")
        mf.addRow(intro_m)
        self._matrix_models: dict[str, _ModelField] = {}
        for tool in ai_tools():
            key = tool["key"]
            slot = matrix_cfg.get(key) or {}
            dflt = (DEFAULTS["ai_matrix"].get(key) or {}).get("model") or DEFAULTS["model_defaults"]
            mfield = _ModelField(slot.get("model"), dflt, fam0, min_width=320)
            self._matrix_models[key] = mfield
            mf.addRow(f"{tool['label']} model:", mfield)
            # Grey out rows for tools the user hasn't selected — no point
            # showing a "Card creation model" picker to someone who only
            # wants Browse. Toggle tools in Settings → Tools.
            if not _matrix_tool_in_use(key):
                mfield.setEnabled(False)
                mfield.setToolTip(
                    "Greyed out — this tool isn't selected. Turn it on in "
                    "Settings → Tools to use this model."
                )
                row_label = mf.labelForField(mfield)
                if row_label is not None:
                    row_label.setEnabled(False)
        layout.addRow(self._matrix_box)

        # ── Skills & assistants (provider-adaptive) ─────────────────────────
        # Claude → one-click install bundled skills into ~/.claude/skills/ (each can
        # carry an optional Anthropic skill_id). Manual → ready-made chat assistants.
        # Direct-API providers → built-in prompts, nothing to install.
        self._skill_cfg = dict(cfg.get("skills") or {})
        # The section adapts to the selected provider: Claude → install CLI skills;
        # manual → ready-made chat assistants (GPT / Gem / Claude Project); the
        # direct-API providers → a note that built-in prompts are used.
        # Fall back to the shipped default when a stale empty value was saved (older
        # builds had editable URL fields; an empty save would otherwise shadow it).
        self._gpt_url = (cfg.get("manual_gpt_url") or DEFAULTS.get("manual_gpt_url") or "").strip()
        self._gem_url = (cfg.get("manual_gem_url") or DEFAULTS.get("manual_gem_url") or "").strip()
        self._skills_box = QGroupBox("Skills & assistants")
        sk_outer = QVBoxLayout(self._skills_box)
        self._skills_rows = QVBoxLayout()
        sk_outer.addLayout(self._skills_rows)
        self._skill_id_edits: dict[str, QLineEdit] = {}
        # Populated by _refresh() (called at the end of __init__), which builds the
        # right variant for the current provider.
        layout.addRow(self._skills_box)

        # Shown only for the 'manual' provider, which has no key/model/CLI.
        self._manual_note = QLabel(
            "<b>Bring your own AI — no account needed here.</b><br>"
            "Every Ankisstant tool still works — when you run one, it hands you "
            "a ready-made prompt to copy into <b>any</b> chatbot (ChatGPT, Gemini, "
            "Claude.ai — free versions are fine). Paste the reply back and "
            "Ankisstant takes it from there. Nothing leaves your machine "
            "automatically, and no API key is needed."
        )
        self._manual_note.setTextFormat(Qt.TextFormat.RichText)
        self._manual_note.setWordWrap(True)
        self._manual_note.setStyleSheet(
            "QLabel { background: rgba(80,160,255,0.12); border: 1px solid "
            "rgba(80,160,255,0.5); border-radius: 6px; padding: 10px; }"
        )
        layout.addRow("", self._manual_note)
        self._form = layout

        test_row = QHBoxLayout()
        self._test_btn = QPushButton("Test connection")
        self._test_btn.clicked.connect(self._on_test)
        test_row.addWidget(self._test_btn)
        test_row.addStretch(1)
        layout.addRow(test_row)

        self._provider.currentIndexChanged.connect(self._refresh)
        self._refresh()  # initial sync of key field / CLI rows / default model

    # ── dynamic UI ─────────────────────────────────────────────────────────
    def current_provider(self) -> str:
        return self._provider.currentData() or "auto"

    def current_family(self) -> str:
        return family_for(self.current_provider())

    def _capture_key(self) -> None:
        if self._cur_key_family:
            self._keys[self._cur_key_family] = self._key.text().strip()

    def _refresh(self, *args) -> None:
        self._capture_key()
        provider = self.current_provider()
        kf = _key_family_for(provider)
        self._cur_key_family = kf
        show_key = kf is not None
        self._key_label.setVisible(show_key)
        self._key_container.setVisible(show_key)
        self._key_hint.setVisible(show_key)
        if show_key:
            label, placeholder, _cfg_key, where = _KEY_META[kf]
            if provider == "auto":
                label = "Anthropic API key (fallback):"
            self._key_label.setText(label)
            self._key.setPlaceholderText(placeholder)
            self._key.setText(self._keys.get(kf, ""))
            self._key_hint.setText(f"<small>Get a key at {where}.</small>")
        show_cli = provider in ("auto", "cli")
        self._cli_container.setVisible(show_cli)
        self._ollama_container.setVisible(provider == "ollama")
        manual = provider == "manual"
        self._manual_note.setVisible(manual)
        # Manual has no model and nothing to connection-test.
        _set_form_row_visible(self._form, self._default_model, not manual)
        self._test_btn.setVisible(not manual)
        self._default_model.set_family(family_for(provider))
        # Keep the per-tool matrix in sync with the provider; hide it in manual.
        self._matrix_box.setVisible(not manual)
        for mfield in self._matrix_models.values():
            mfield.set_family(family_for(provider))
        # Swap the Skills & assistants section to fit the selected provider.
        self._rebuild_skills()
        if callable(self.on_provider_changed):
            self.on_provider_changed(self.current_provider())

    def _toggle_key_visibility(self, checked: bool):
        self._key.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._show_key.setText("Hide" if checked else "Show")

    def _on_test(self):
        # Save the form values into a transient config snapshot for the test
        # call, without persisting them (Save isn't clicked yet).
        from ..core.config import load_config as _load
        from ..core import log
        from aqt import mw
        snapshot = _load()
        snapshot.update(self.get_values())
        save_config(snapshot)

        # IMPORTANT: this dialog is modal (open_settings uses dlg.exec()). A
        # nested QProgressDialog + QEventLoop.exec() — what run_claude_text does
        # — runs an event loop *inside* the modal loop, which deadlocks / hard-
        # crashes Anki on macOS. So drive the test with taskman directly: the
        # work() runs off-thread and done() runs back on the main thread, with
        # no nested loop. Same pattern as the setup wizard's connection test.
        self._test_btn.setEnabled(False)
        self._test_btn.setText("Testing…")

        def work():
            return core_api.ask_claude(
                prompt="Reply with the word ok and nothing else.",
                system="You are a connection test. Reply with the single word: ok",
                max_tokens=16,
                show_errors=False,
            )

        def done(fut):
            try:
                reply = fut.result()
            except Exception as e:
                log.error(f"settings connection test crashed: {e!r}")
                reply = None
            try:
                self._test_btn.setEnabled(True)
                self._test_btn.setText("Test connection")
            except RuntimeError:
                return  # dialog closed mid-test
            if reply:
                showInfo(f"Connection OK.\n\nReply: {reply!r}")
            else:
                showWarning(
                    "Test failed — see Anki's console for the error. If you're on "
                    "Gemini's free tier, try the model Gemini 3.5 Flash — it's "
                    "the most reliable free-tier option (2.0 Flash and 2.5 Pro "
                    "have no free quota, and 2.5 Flash often hits quota errors)."
                )

        mw.taskman.run_in_background(work, done)

    # ── skills catalog ──────────────────────────────────────────────────────
    def _capture_skill_ids(self) -> None:
        """Fold the visible Anthropic-ID edits back into the working cfg so a
        rebuild (after install/remove) doesn't lose typed-but-unsaved IDs."""
        for name, edit in self._skill_id_edits.items():
            sid = edit.text().strip()
            if sid:
                self._skill_cfg.setdefault(name, {})["anthropic_skill_id"] = sid
            elif name in self._skill_cfg:
                self._skill_cfg[name].pop("anthropic_skill_id", None)

    def _sk_add_note(self, html: str) -> None:
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet("color: gray;")
        self._skills_rows.addWidget(lbl)

    def _sk_add_assistant(self, label_html: str, button_text: str, on_click) -> None:
        row = QHBoxLayout()
        lbl = QLabel(label_html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        row.addWidget(lbl, 1)
        btn = QPushButton(button_text)
        btn.setAutoDefault(False)
        btn.clicked.connect(lambda _=False: on_click())
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
        self._skills_rows.addWidget(w)

    def _rebuild_skills(self) -> None:
        # Preserve any typed-but-unsaved Anthropic IDs before clearing.
        self._capture_skill_ids()
        while self._skills_rows.count():
            item = self._skills_rows.takeAt(0)
            w = item.widget()
            if w is not None:
                # setParent(None) detaches it from the view NOW; deleteLater alone
                # is deferred, so old rows would float over the new ones (overlay).
                w.setParent(None)
                w.deleteLater()
        self._skill_id_edits = {}
        provider = self.current_provider()

        if provider in ("auto", "cli", "anthropic"):
            self._sk_add_note(
                "<small>Install Ankisstant's skills into your Claude Code folder "
                "(<code>~/.claude/skills/</code>) with one click — no copying files. "
                "Used automatically by the matching tool above. The optional Anthropic "
                "ID is only for users who upload a skill to the Anthropic API.</small>")
            for entry in skills_catalog.catalog():
                self._sk_add_install_card(entry)
            self._sk_add_note(
                "<small>Prefer Claude.ai in the browser? Switch the provider to "
                "<b>Manual / paste</b> for a ready-made Claude Project.</small>")
        elif provider == "manual":
            self._sk_add_note(
                "<small>Ready-made <b>Ankisstant</b> assistants — the instructions "
                "already live inside them, so you paste only the task. Use whichever "
                "AI you have:</small>")
            self._sk_add_manual_assistants()
        else:  # gemini / openai / ollama — direct API, no skill mechanism
            self._sk_add_note(
                "<small>This provider uses Ankisstant's built-in prompts automatically "
                "— nothing to install here. (Skills are a Claude-only feature; every "
                "tool still works.) If you also use the ChatGPT or Gemini app, switch "
                "the provider to <b>Manual / paste</b> for the ready-made assistants.</small>")

    def _sk_add_install_card(self, entry: dict) -> None:
        name = entry["name"]
        card = QWidget()
        cv = QVBoxLayout(card)
        cv.setContentsMargins(0, 2, 0, 2)
        cv.setSpacing(2)
        row = QHBoxLayout()
        status = ("<span style='color:#16a34a'>✓ installed</span>"
                  if entry["installed"] else
                  "<span style='color:gray'>not installed</span>")
        lbl = QLabel(f"<b>{entry['label']}</b> "
                     f"<span style='color:gray;font-size:11px'>· {name} · </span>{status}")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(lbl, 1)
        btn = QPushButton("Remove" if entry["installed"] else "Install")
        btn.setFixedWidth(72)
        if entry["installed"]:
            btn.clicked.connect(lambda _, n=name: self._on_remove_skill(n))
        else:
            btn.clicked.connect(lambda _, n=name: self._on_install_skill(n))
        row.addWidget(btn)
        cv.addLayout(row)
        id_edit = QLineEdit((self._skill_cfg.get(name) or {}).get("anthropic_skill_id", ""))
        id_edit.setPlaceholderText("Anthropic skill_id (optional — API only)")
        self._skill_id_edits[name] = id_edit
        id_widget = QWidget()
        id_form = _expand_form(QFormLayout(id_widget))
        id_form.setContentsMargins(16, 0, 0, 0)
        id_form.addRow("Anthropic ID:", id_edit)
        cv.addWidget(id_widget)
        self._skills_rows.addWidget(card)

    def _sk_add_manual_assistants(self) -> None:
        from aqt.utils import openLink
        if self._gpt_url:
            self._sk_add_assistant(
                "<b>ChatGPT</b> — Ankisstant Custom GPT",
                "↗ Open", lambda: openLink(self._gpt_url))
        if self._gem_url:
            self._sk_add_assistant(
                "<b>Gemini</b> — Ankisstant Gem",
                "↗ Open", lambda: openLink(self._gem_url))
        self._sk_add_assistant(
            "<b>Claude.ai</b> — paste these into a new Project (or the first message)",
            "📋 Copy instructions", self._copy_claude_project)

    def _copy_claude_project(self) -> None:
        text = skills_catalog.claude_project_instructions()
        if not text:
            showWarning("Couldn't find the bundled Claude Project instructions.")
            return
        QApplication.clipboard().setText(text)
        tooltip("Claude Project instructions copied — paste into a new claude.ai Project.",
                period=3000)

    def _on_install_skill(self, name: str) -> None:
        self._capture_skill_ids()
        ok, msg = skills_catalog.install_skill(name)
        (tooltip if ok else showWarning)(msg)
        self._rebuild_skills()

    def _on_remove_skill(self, name: str) -> None:
        self._capture_skill_ids()
        ok, msg = skills_catalog.uninstall_skill(name)
        (tooltip if ok else showWarning)(msg)
        self._rebuild_skills()

    def _skill_values(self) -> dict:
        self._capture_skill_ids()
        return {n: v for n, v in self._skill_cfg.items() if v.get("anthropic_skill_id")}

    def get_values(self) -> dict:
        self._capture_key()
        return {
            "provider":              self.current_provider(),
            "skills":                self._skill_values(),
            "anthropic_api_key":     self._keys.get("anthropic", ""),
            "gemini_api_key":        self._keys.get("gemini", ""),
            "openai_api_key":        self._keys.get("openai", ""),
            "claude_cli_path":       self._cli_path.text().strip(),
            "claude_cli_extra_args": [t for t in self._cli_extra.text().strip().split() if t],
            "ollama_url":            self._ollama_url.text().strip() or "http://localhost:11434",
            "model_defaults":        self._default_model.values(),
            "ai_matrix":             self._matrix_values(),
            # Persisting an edited matrix marks the one-time seed done so later
            # loads don't re-seed over the user's choices.
            "ai_matrix_migrated":    True,
        }

    def _matrix_values(self) -> dict:
        # Models only — skills are managed by the Skills catalog, not the matrix.
        return {key: {"model": mfield.values()} for key, mfield in self._matrix_models.items()}


class _GlobalTab(QWidget):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        intro = QLabel(
            "Pick which Ankisstant tools you actually want — disabled ones "
            "stop showing up here and in the Ankisstant window. (AI provider, "
            "keys and models live on the <b>AI</b> tab; Month tag lives in "
            "Browse / Create settings.)"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addRow(intro)

        # ── Tools — single on/off switchboard ─────────────────────────────────
        # Moved here from each tool's own tab so users who only want a couple
        # of tools aren't shown a wall of tabs/settings for the rest. A
        # disabled tool's tab disappears from this dialog (reopen to see the
        # change) and its entry disappears from the Ankisstant window sidebar.
        tools_box = QGroupBox()
        tb = QVBoxLayout(tools_box)
        tb.setSpacing(6)

        tools_hint = QLabel(
            "<small>Turn tools on or off — disabled tools are hidden from "
            "both this dialog and the Ankisstant window. Reopen Settings "
            "after changing these to see updated tabs.</small>"
        )
        tools_hint.setWordWrap(True)
        tools_hint.setTextFormat(Qt.TextFormat.RichText)
        tools_hint.setStyleSheet("color: gray;")
        tb.addWidget(tools_hint)

        tools_cfg = cfg.get("tools", {})

        self._tool_qbank = QCheckBox("AI QBank")
        self._tool_qbank.setChecked(bool(tools_cfg.get("qbank", {}).get("enabled", True)))
        tb.addWidget(self._tool_qbank)

        browse_row = QHBoxLayout()
        browse_row.addWidget(QLabel("AI Browse:"))
        self._browse_mode = QComboBox()
        self._browse_mode.addItem("Off", "off")
        self._browse_mode.addItem(
            "Native search only — lightweight checkbox in Anki's own Browse window",
            "native_only",
        )
        self._browse_mode.addItem("Full AI Browse panel (includes native search)", "full")
        br_cfg = tools_cfg.get("browse", {})
        if not br_cfg.get("enabled", True):
            cur_mode = "off"
        elif br_cfg.get("native_only"):
            cur_mode = "native_only"
        else:
            cur_mode = "full"
        self._browse_mode.setCurrentIndex(max(0, self._browse_mode.findData(cur_mode)))
        browse_row.addWidget(self._browse_mode, 1)
        tb.addLayout(browse_row)

        self._tool_kg = QCheckBox("Knowledge Gaps")
        self._tool_kg.setChecked(bool(tools_cfg.get("knowledge_gaps", {}).get("enabled", True)))
        tb.addWidget(self._tool_kg)

        self._tool_ga = QCheckBox("    ↳ Analyse Knowledge Gaps (AI sub-feature)")
        self._tool_ga.setChecked(bool(tools_cfg.get("gap_analyser", {}).get("enabled", True)))
        self._tool_ga.setEnabled(self._tool_kg.isChecked())
        self._tool_kg.toggled.connect(self._tool_ga.setEnabled)
        tb.addWidget(self._tool_ga)

        self._tool_creator = QCheckBox("AI Create")
        self._tool_creator.setChecked(bool(tools_cfg.get("card_creator", {}).get("enabled", True)))
        tb.addWidget(self._tool_creator)

        self._tool_update = QCheckBox("Update by Tag")
        self._tool_update.setChecked(bool(tools_cfg.get("update_by_tag", {}).get("enabled", True)))
        tb.addWidget(self._tool_update)

        self._tool_lecture = QCheckBox("AI Lecture")
        self._tool_lecture.setChecked(bool(tools_cfg.get("lecture", {}).get("enabled", True)))
        tb.addWidget(self._tool_lecture)

        layout.addRow(tools_box)

    def get_tool_states(self) -> dict:
        """Per-tool enabled flags (+ Browse's panel mode) — the single source
        of truth for `tools.<key>.enabled` / `tools.browse.native_only`."""
        mode = self._browse_mode.currentData() or "full"
        return {
            "qbank":              self._tool_qbank.isChecked(),
            "browse":             mode != "off",
            "browse_native_only": mode == "native_only",
            "knowledge_gaps":     self._tool_kg.isChecked(),
            "gap_analyser":       self._tool_ga.isChecked(),
            "card_creator":       self._tool_creator.isChecked(),
            "update_by_tag":      self._tool_update.isChecked(),
            "lecture":            self._tool_lecture.isChecked(),
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

        self._show_heatmap = QCheckBox("Show heatmap on the deck browser home screen")
        self._show_heatmap.setChecked(bool(qb_cfg.get("show_heatmap", True)))
        root.addWidget(self._show_heatmap)

        # ── Weakness dashboard ────────────────────────────────────────────────
        weak_box = QGroupBox("Weakness dashboard")
        wl = QFormLayout(weak_box)
        self._show_weakness = QCheckBox("Show weakness dashboard in the AI QBank panel")
        self._show_weakness.setChecked(bool(qb_cfg.get("show_weakness", True)))
        self._show_weakness.setToolTip(
            "Aggregates your captured missed questions by System / Subsystem / "
            "Topic so you can see where you miss most."
        )
        wl.addRow(self._show_weakness)

        # Window options mirror tools/qbank/weakness._WINDOWS: (label, days).
        self._weak_windows = [("Last 7 days", 7), ("Last 30 days", 30),
                              ("Last 90 days", 90), ("All time", 0)]
        self._weakness_window = QComboBox()
        for label, _days in self._weak_windows:
            self._weakness_window.addItem(label)
        cur_days = int(qb_cfg.get("weakness_window_days", 30))
        cur_idx = next((i for i, (_l, d) in enumerate(self._weak_windows)
                        if d == cur_days), 1)
        self._weakness_window.setCurrentIndex(cur_idx)
        wl.addRow("Default time window:", self._weakness_window)

        self._weakness_top_n = QSpinBox()
        self._weakness_top_n.setRange(1, 50)
        self._weakness_top_n.setValue(int(qb_cfg.get("weakness_top_n", 8)))
        self._weakness_top_n.setToolTip("How many systems and topics to list.")
        wl.addRow("Show top N systems/topics:", self._weakness_top_n)
        root.addWidget(weak_box)

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

        # ── Capture / new-card defaults ──────────────────────────────────────
        # The AI model & skill used when generating a card from a captured gap
        # now live on the AI tab's per-tool matrix (Card creation / Search).
        # There are deliberately no tool-local model/skill dropdowns here.
        cap_box = QGroupBox("Capture & new-card defaults")
        cf = _expand_form(QFormLayout(cap_box))

        self._notetype = QLineEdit(qb_cfg.get("card_notetype", ""))
        self._notetype.setMinimumWidth(360)
        self._notetype.setPlaceholderText("e.g. Cloze")
        cf.addRow("New card note type:", self._notetype)

        self._deck = QLineEdit(qb_cfg.get("card_deck", ""))
        self._deck.setMinimumWidth(360)
        self._deck.setPlaceholderText("e.g. Default")
        cf.addRow("New card deck:", self._deck)

        self._field = QLineEdit(qb_cfg.get("missed_q_field", "Missed Questions"))
        self._field.setMinimumWidth(360)
        cf.addRow("Append-to field:", self._field)

        self._mq_explain = QCheckBox(
            "Add an AI-written explanation of the knowledge gap above the question"
        )
        self._mq_explain.setChecked(bool(qb_cfg.get("mq_explain", True)))
        self._mq_explain.setToolTip(
            "When making a card (or tagging via Browse) from a captured missed "
            "question, lead the append-to field with the specific concept missed "
            "and a short explanation of it. Generated in the same AI request, so "
            "it costs no extra round-trip."
        )
        cf.addRow("", self._mq_explain)

        self._tag_root = QLineEdit(qb_cfg.get("tag_root", "Missed_Questions"))
        self._tag_root.setMinimumWidth(360)
        cf.addRow("Tag root:", self._tag_root)

        # ── Capture popup UX (zoom + image width) ────────────────────────
        self._image_max_width = QSpinBox()
        self._image_max_width.setRange(80, 1200)
        self._image_max_width.setSuffix(" px")
        self._image_max_width.setValue(int(qb_cfg.get("image_max_width", 300)))
        self._image_max_width.setToolTip(
            "Max-width applied to pasted screenshots when saved into a card. "
            "Smaller values keep cards compact."
        )
        cf.addRow("Image max width:", self._image_max_width)

        self._capture_zoom = QSpinBox()
        self._capture_zoom.setRange(40, 100)
        self._capture_zoom.setSuffix(" %")
        self._capture_zoom.setValue(int(round(float(qb_cfg.get("capture_zoom_factor", 0.7)) * 100)))
        self._capture_zoom.setToolTip(
            "Zoom level the QBank browser is shrunk to while the capture "
            "popup is open (so more of the question fits on screen for a "
            "screenshot). 100% disables the shrink. Anki's reviewer is "
            "unaffected."
        )
        cf.addRow("Capture zoom factor:", self._capture_zoom)

        self._capture_shortcut = QKeySequenceEdit()
        self._capture_shortcut.setKeySequence(
            QKeySequence(qb_cfg.get("capture_shortcut", "Ctrl+M"))
        )
        self._capture_shortcut.setToolTip(
            "Application-wide shortcut that opens the capture popup, even while "
            "reviewing. Clear it to disable."
        )
        cf.addRow("Capture shortcut:", self._capture_shortcut)

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

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # QBank has no tool-local model widgets; models resolve from the AI tab.
        pass

    def get_values(self) -> dict:
        return {
            "show_heatmap":   self._show_heatmap.isChecked(),
            "show_weakness":  self._show_weakness.isChecked(),
            "weakness_window_days": self._weak_windows[self._weakness_window.currentIndex()][1],
            "weakness_top_n": int(self._weakness_top_n.value()),
            "platforms":      list(self._platforms),
            "default_daily":  int(self._spin.value()),
            "target_periods": sorted(self._periods, key=lambda p: p.get("from", "")),
            "exam_dates":     sorted(self._exams,   key=lambda e: e.get("date", "")),
            "card_notetype":  self._notetype.text().strip(),
            "card_deck":      self._deck.text().strip(),
            "missed_q_field": self._field.text().strip()         or "Missed Questions",
            "mq_explain":     self._mq_explain.isChecked(),
            "tag_root":       self._tag_root.text().strip()      or "Missed_Questions",
            "image_max_width": int(self._image_max_width.value()),
            "capture_zoom_factor": round(int(self._capture_zoom.value()) / 100.0, 3),
            "capture_shortcut": self._capture_shortcut.keySequence().toString(),
        }


class _BrowseTab(QWidget):
    def __init__(self, br_cfg: dict, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self._br_cfg = br_cfg
        self._native_only = bool(br_cfg.get("native_only", False))
        self._source_tags = list(br_cfg.get("source_tags") or [])
        cfg = cfg or {}

        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        # On/off + the native-search-only mode now live in Settings → Tools,
        # since they decide whether this whole tab is even shown.

        if self._native_only:
            note = QLabel(
                "<small>Only the lightweight in-browser <b>✨ AI Search</b> is "
                "active — the full AI Browse panel is hidden, so its settings "
                "are too. Switch modes in <b>Settings → Tools</b>.</small>"
            )
            note.setWordWrap(True)
            note.setTextFormat(Qt.TextFormat.RichText)
            note.setStyleSheet("color: gray;")
            layout.addRow(note)
        else:
            # Model lives on the AI tab (Settings → AI → "Search / Browse"
            # model). There is deliberately no tool-local model picker here —
            # the per-tool matrix is the single source of truth.

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

            # Hierarchical auto-tag: the AI suggests a tag for the searched
            # topic and pre-fills the "Tag to apply" field. The scheme (base
            # prefix + type + levels) is the SHARED one configured under
            # Knowledge Gaps — there's no Browse-specific prefix. A free
            # search is tagged as KG; a search loaded from a KG uses that
            # KG's type.
            self._auto_tag = QCheckBox("Auto-suggest a hierarchical tag on topic search")
            self._auto_tag.setChecked(bool(br_cfg.get("auto_tag", True)))
            self._auto_tag.setToolTip(
                "When you search a topic, ask the AI for {system}::{subsystem}::"
                "{topic} and pre-fill the 'Tag to apply' field using the shared "
                "auto-tag scheme (set under Knowledge Gaps → Auto-tag). Won't "
                "overwrite a tag you've already typed or one carried from a KG."
            )
            layout.addRow("Auto-tag:", self._auto_tag)
            at_hint = QLabel(
                "<small>Tag scheme &amp; base prefix live in "
                "<b>Knowledge Gaps → Auto-tag</b>.</small>"
            )
            at_hint.setStyleSheet("color: gray;")
            at_hint.setTextFormat(Qt.TextFormat.RichText)
            layout.addRow("", at_hint)

            st_hint = QLabel("<small>Source-deck badges are edited in the addon config JSON.</small>")
            st_hint.setStyleSheet("color: gray;")
            st_hint.setTextFormat(Qt.TextFormat.RichText)
            layout.addRow(st_hint)

            # Shared with Create — both flows can apply it, so it's shown
            # (and kept in sync) in both places. See _link_checkbox/_link_lineedit.
            mt_box, self._month_tag, self._month_tag_prefix = _month_tag_group(cfg)
            layout.addRow(mt_box)

        # ── Native browser AI Search — scope filters ─────────────────────────
        # Restricts what the "✨ AI Search" checkbox in Anki's own Browse window
        # searches over. The AI-generated terms are AND-ed with these — e.g.
        # "(term1) OR (term2)" "(deck:"A" OR deck:"B")" "is:suspended".
        scope = br_cfg.get("native_ai_search_scope") or {}
        scope_box = QGroupBox("Native browser AI Search — scope")
        sf = _expand_form(QFormLayout(scope_box))
        sf.setVerticalSpacing(10)

        scope_hint = QLabel(
            "<small>Optional — narrows what the search-bar checkbox in Anki's "
            "own Browse window searches. Leave blank to search everywhere.</small>"
        )
        scope_hint.setStyleSheet("color: gray;")
        scope_hint.setTextFormat(Qt.TextFormat.RichText)
        scope_hint.setWordWrap(True)
        sf.addRow(scope_hint)

        self._scope_decks = QLineEdit(", ".join(scope.get("decks") or []))
        self._scope_decks.setMinimumWidth(420)
        self._scope_decks.setPlaceholderText("e.g. School::Year3, MCAT (comma-separated, blank = all decks)")
        attach_deck_completer(self._scope_decks, multi=True)
        sf.addRow("Limit to decks:", self._scope_decks)

        self._scope_tags = QLineEdit(", ".join(scope.get("tags") or []))
        self._scope_tags.setMinimumWidth(420)
        self._scope_tags.setPlaceholderText("e.g. weak, AnKing (comma-separated, blank = all tags)")
        attach_tag_completer(self._scope_tags, multi=True)
        sf.addRow("Limit to tags:", self._scope_tags)

        self._scope_suspended = QComboBox()
        self._scope_suspended.addItem("Any (suspended or not)", "any")
        self._scope_suspended.addItem("Only suspended cards", "suspended")
        self._scope_suspended.addItem("Only unsuspended cards", "unsuspended")
        cur_susp = (scope.get("suspended") or "any").strip().lower()
        idx = max(0, self._scope_suspended.findData(cur_susp))
        self._scope_suspended.setCurrentIndex(idx)
        sf.addRow("Suspended:", self._scope_suspended)

        layout.addRow(scope_box)

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # No tool-local model widget: the model resolves from the AI tab matrix.
        return

    def get_values(self) -> dict:
        decks = [d.strip() for d in self._scope_decks.text().split(",") if d.strip()]
        tags = [t.strip() for t in self._scope_tags.text().split(",") if t.strip()]

        # In native-only mode the full-panel fields aren't built — preserve
        # whatever was already on disk so toggling modes never loses settings.
        if self._native_only:
            full_panel = {
                "last_used_tag":   self._br_cfg.get("last_used_tag", ""),
                "max_results":     self._br_cfg.get("max_results", 50),
                "notetype_filter": self._br_cfg.get("notetype_filter", ""),
                "front_field":     self._br_cfg.get("front_field", "Text"),
                "audit_tag":       self._br_cfg.get("audit_tag", ""),
                "auto_tag":        self._br_cfg.get("auto_tag", True),
            }
        else:
            full_panel = {
                "last_used_tag":   self._last_tag.text().strip(),
                "max_results":     int(self._max.value()),
                "notetype_filter": self._notetype_filter.text().strip(),
                "front_field":     self._front_field.text().strip() or "Text",
                "audit_tag":       self._audit_tag.text().strip(),
                "auto_tag":        self._auto_tag.isChecked(),
            }

        return {
            **full_panel,
            "source_tags":       self._source_tags,
            "native_ai_search_scope": {
                "decks":     decks,
                "tags":      tags,
                "suspended": self._scope_suspended.currentData() or "any",
            },
        }


def _slugify_type_key(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_").lower()
    return s[:24] or "type"


FIELD_KIND_LABELS = [
    ("text",     "Text (single line)"),
    ("longtext", "Long text (multi-line)"),
    ("html",     "Rich text + screenshots (HTML)"),
    ("url",      "URL"),
    ("tag",      "Anki tag (with autocomplete)"),
]


_ROLE_LABELS = [
    ("front",      "Front (cloze/question)"),
    ("extra",      "Extra (back/supporting)"),
    ("image",      "Image field"),
    ("one_by_one", "One-by-one toggle"),
    ("missed_q",   "Missed Questions field"),
]
_TARGET_KIND_LABELS = [
    ("none",  "Keep in the gap only (don't write to a card)"),
    ("role",  "A card role (resolved per notetype)"),
    ("field", "A named field on a notetype"),
    ("tag",   "An Anki tag (hierarchical scheme)"),
]


class _FieldEditorDialog(QDialog):
    """Modal for editing a single field spec inside a type's schema, including the
    declarative behaviour (where it's captured, whether the AI fills it, which AI
    flows request it, and which Anki field it writes to)."""

    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit field" if existing else "Add field")
        self.setMinimumWidth(480)
        # Pre-fill from the engine's normalised view so known fields (concept,
        # explanation, …) show their inferred defaults rather than blanks.
        try:
            from ..tools.kg import engine as _eng
            norm = _eng.normalise_field(existing or {}) if existing else {}
        except Exception:
            norm = dict(existing or {})

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)
        layout = _expand_form(QFormLayout())
        layout.setVerticalSpacing(8)
        root.addLayout(layout)

        self._existing_key = (existing or {}).get("key", "")

        self._label = QLineEdit(str((existing or {}).get("label", "")))
        self._label.setPlaceholderText("e.g. Question stem, Concept missed")
        layout.addRow("Label:", self._label)

        self._kind = QComboBox()
        for k, lbl in FIELD_KIND_LABELS:
            self._kind.addItem(lbl, k)
        idx = self._kind.findData((existing or {}).get("kind", "text"))
        if idx >= 0:
            self._kind.setCurrentIndex(idx)
        layout.addRow("Kind:", self._kind)

        self._placeholder = QLineEdit(str((existing or {}).get("placeholder", "")))
        self._placeholder.setPlaceholderText("Hint shown inside the empty input")
        layout.addRow("Placeholder:", self._placeholder)

        # ── Source ──────────────────────────────────────────────────────────
        self._source = QComboBox()
        for k, lbl in [("capture", "Captured (you type/paste it)"),
                       ("ai", "AI-generated"),
                       ("capture+ai", "Captured, AI may fill")]:
            self._source.addItem(lbl, k)
        si = self._source.findData(norm.get("source", "capture"))
        self._source.setCurrentIndex(si if si >= 0 else 0)
        self._source.currentIndexChanged.connect(self._sync_dynamic)
        layout.addRow("Source:", self._source)

        # Show-on (capture surfaces)
        surf = set(norm.get("surfaces") or ["mq_capture", "home", "add_kg"])
        self._surf_mq = QCheckBox("MQ capture");  self._surf_mq.setChecked("mq_capture" in surf)
        self._surf_home = QCheckBox("Home");       self._surf_home.setChecked("home" in surf)
        self._surf_add = QCheckBox("Add-KG");      self._surf_add.setChecked("add_kg" in surf)
        srow = QHBoxLayout()
        for w in (self._surf_mq, self._surf_home, self._surf_add):
            srow.addWidget(w)
        srow.addStretch(1)
        self._surf_wrap = QWidget(); self._surf_wrap.setLayout(srow)
        srow.setContentsMargins(0, 0, 0, 0)
        layout.addRow("Show on:", self._surf_wrap)

        # Used-in (AI flows)
        flows = set(norm.get("flows") or ["create"])
        self._flow_create = QCheckBox("Create"); self._flow_create.setChecked("create" in flows)
        self._flow_browse = QCheckBox("Browse"); self._flow_browse.setChecked("browse" in flows)
        frow = QHBoxLayout()
        frow.addWidget(self._flow_create); frow.addWidget(self._flow_browse); frow.addStretch(1)
        frow.setContentsMargins(0, 0, 0, 0)
        fwrap = QWidget(); fwrap.setLayout(frow)
        layout.addRow("Used in:", fwrap)

        # Cardinality
        self._cardinality = QComboBox()
        self._cardinality.addItem("Per note (one value)", "note")
        self._cardinality.addItem("Per card (one each)", "card")
        ci = self._cardinality.findData(norm.get("cardinality", "note"))
        self._cardinality.setCurrentIndex(ci if ci >= 0 else 0)
        layout.addRow("Cardinality:", self._cardinality)

        # ── Anki output target ─────────────────────────────────────────────
        tgt = norm.get("anki_target") or {}
        self._tgt_kind = QComboBox()
        for k, lbl in _TARGET_KIND_LABELS:
            self._tgt_kind.addItem(lbl, k)
        ki = self._tgt_kind.findData(tgt.get("kind", "none"))
        self._tgt_kind.setCurrentIndex(ki if ki >= 0 else 0)
        self._tgt_kind.currentIndexChanged.connect(self._sync_dynamic)
        layout.addRow("Write to:", self._tgt_kind)

        self._tgt_role = QComboBox()
        for k, lbl in _ROLE_LABELS:
            self._tgt_role.addItem(lbl, k)
        ri = self._tgt_role.findData(tgt.get("role", "front"))
        self._tgt_role.setCurrentIndex(ri if ri >= 0 else 0)
        self._tgt_role_row = self._tgt_role
        layout.addRow("Role:", self._tgt_role)

        self._tgt_field = QLineEdit(str(tgt.get("field", "")))
        self._tgt_field.setPlaceholderText("exact field name on the notetype")
        layout.addRow("Field name:", self._tgt_field)
        self._tgt_notetype = QLineEdit(str(tgt.get("notetype", "")))
        self._tgt_notetype.setPlaceholderText("notetype this field belongs to (optional)")
        layout.addRow("On notetype:", self._tgt_notetype)

        self._tgt_pos = QSpinBox(); self._tgt_pos.setRange(0, 99)
        self._tgt_pos.setValue(int(tgt.get("position", 0) or 0))
        layout.addRow("Order (position):", self._tgt_pos)
        self._tgt_mode = QComboBox()
        for k in ("append", "prepend", "replace"):
            self._tgt_mode.addItem(k, k)
        mi = self._tgt_mode.findData(tgt.get("mode", "append"))
        self._tgt_mode.setCurrentIndex(mi if mi >= 0 else 0)
        layout.addRow("Combine mode:", self._tgt_mode)

        # ── Advanced: AI prompt + refs ─────────────────────────────────────
        self._adv_lbl = QLabel("<b>AI prompt</b> — what the AI should produce for this field:")
        self._adv_lbl.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self._adv_lbl)
        self._ai_prompt = QPlainTextEdit(str(norm.get("ai_prompt", "")))
        self._ai_prompt.setPlaceholderText(
            "e.g. a brief teaching explanation (1–3 sentences) of the concept missed — "
            "the mechanism/principle, pitched at a Year 3 AU med student, plain prose")
        self._ai_prompt.setMinimumHeight(70)
        root.addWidget(self._ai_prompt)
        self._ai_refs = QLineEdit(", ".join(norm.get("ai_refs") or []))
        self._ai_refs.setPlaceholderText("other field keys to feed as context, comma-separated (e.g. concept, stem_html)")
        refrow = _expand_form(QFormLayout())
        refrow.addRow("AI inputs:", self._ai_refs)
        root.addLayout(refrow)

        if existing:
            key_lbl = QLabel(f"<small>key: <code>{existing.get('key', '')}</code>"
                             f" — locked once a field has data</small>")
            key_lbl.setTextFormat(Qt.TextFormat.RichText)
            key_lbl.setStyleSheet("color: gray;")
            root.addWidget(key_lbl)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._sync_dynamic()
        _synapse_theme(self, dialog=True)

    def _sync_dynamic(self, *_args) -> None:
        src = self._source.currentData() or "capture"
        has_capture = "capture" in src
        has_ai = "ai" in src
        for w in (self._surf_mq, self._surf_home, self._surf_add):
            w.setEnabled(has_capture)
        self._adv_lbl.setVisible(has_ai)
        self._ai_prompt.setVisible(has_ai)
        self._ai_refs.setEnabled(has_ai)
        kind = self._tgt_kind.currentData() or "none"
        self._tgt_role.setEnabled(kind == "role")
        self._tgt_field.setEnabled(kind == "field")
        self._tgt_notetype.setEnabled(kind == "field")
        ordered = kind in ("role", "field")
        self._tgt_pos.setEnabled(ordered)
        self._tgt_mode.setEnabled(ordered)

    def _on_save(self):
        if not self._label.text().strip():
            showWarning("Label is required.")
            return
        if self._source.currentData() == "ai" and not self._flow_create.isChecked() \
                and not self._flow_browse.isChecked():
            showWarning("An AI field must be used in at least one flow (Create or Browse).")
            return
        if self._tgt_kind.currentData() == "field" and not self._tgt_field.text().strip():
            showWarning("Pick a field name for the 'named field' target.")
            return
        self.accept()

    def values(self) -> dict:
        label = self._label.text().strip()
        key = self._existing_key or _slugify_type_key(label)
        surfaces = [s for s, w in (("mq_capture", self._surf_mq), ("home", self._surf_home),
                                   ("add_kg", self._surf_add)) if w.isChecked()]
        flows = [f for f, w in (("create", self._flow_create), ("browse", self._flow_browse))
                 if w.isChecked()]
        refs = [r.strip() for r in self._ai_refs.text().split(",") if r.strip()]
        return {
            "key":         key,
            "label":       label,
            "kind":        self._kind.currentData() or "text",
            "placeholder": self._placeholder.text().strip(),
            "source":      self._source.currentData() or "capture",
            "surfaces":    surfaces,
            "flows":       flows or ["create"],
            "cardinality": self._cardinality.currentData() or "note",
            "ai_prompt":   self._ai_prompt.toPlainText().strip(),
            "ai_refs":     refs,
            "anki_target": {
                "kind":     self._tgt_kind.currentData() or "none",
                "role":     self._tgt_role.currentData() or "",
                "field":    self._tgt_field.text().strip(),
                "notetype": self._tgt_notetype.text().strip(),
                "position": int(self._tgt_pos.value()),
                "mode":     self._tgt_mode.currentData() or "append",
            },
        }


class _TypeEditorDialog(QDialog):
    """Modal for adding/editing a KG type entry, including its field schema."""

    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit type" if existing else "Add type")
        self.setMinimumWidth(540)
        self.setMinimumHeight(560)

        # Working copy of the fields list — committed on Save.
        self._fields: list[dict] = [dict(f) for f in (existing or {}).get("fields") or []]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        form = _expand_form(QFormLayout())
        form.setVerticalSpacing(8)

        self._name = QLineEdit(str((existing or {}).get("name", "")))
        self._name.setPlaceholderText("e.g. MQ, Drug fact, Mnemonic")
        form.addRow("Name:", self._name)

        # Color row — line edit + a swatch button that opens QColorDialog.
        color = str((existing or {}).get("color", "#6b7280"))
        color_row = QHBoxLayout()
        self._color = QLineEdit(color)
        self._color.setPlaceholderText("#rrggbb")
        self._color.setMaximumWidth(120)
        self._swatch = QPushButton("Pick…")
        self._swatch.clicked.connect(self._pick_color)
        color_row.addWidget(self._color)
        color_row.addWidget(self._swatch)
        color_row.addStretch(1)
        self._color_preview = QLabel("  ")
        self._color_preview.setFixedWidth(28)
        self._color_preview.setStyleSheet(
            f"background:{color}; border-radius: 4px; border: 1px solid #999;"
        )
        color_row.addWidget(self._color_preview)
        self._color.textChanged.connect(self._update_preview)
        wrap = QWidget()
        wrap.setLayout(color_row)
        form.addRow("Colour:", wrap)

        self._description = QPlainTextEdit(str((existing or {}).get("description", "")))
        self._description.setMinimumHeight(50)
        self._description.setPlaceholderText("Optional — what is this type for?")
        form.addRow("Description:", self._description)

        # Auto-tag opt-in for this type. When enabled, Create/Browse ask Claude
        # for {system, subsystem, topic} and build a tag using the SHARED
        # scheme: <base>::<this type's name>::System::Subsystem::Topic. The base
        # prefix and template are set once, below the type list.
        self._auto_tag = QCheckBox("Generate hierarchical tag from concept")
        self._auto_tag.setChecked(bool((existing or {}).get("auto_tag", False)))
        self._auto_tag.setToolTip(
            "When a card is made from a KG of this type, AI extracts "
            "System / Subsystem / Topic and the addon appends a tag of the form "
            "<base>::<TypeName>::System::Subsystem::Topic to every card. The "
            "<base> prefix is shared across all types (set below the type list)."
        )
        form.addRow("Auto-tag:", self._auto_tag)
        type_name_hint = QLabel(
            "<small>The type's <b>name</b> above becomes the second tag segment "
            "(e.g. <code>!!Fleg::MQ::…</code>).</small>"
        )
        type_name_hint.setStyleSheet("color: gray;")
        type_name_hint.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("", type_name_hint)

        if existing:
            key_lbl = QLabel(f"<small>key: <code>{existing.get('key', '')}</code></small>")
            key_lbl.setTextFormat(Qt.TextFormat.RichText)
            key_lbl.setStyleSheet("color: gray;")
            form.addRow(key_lbl)
            self._key = existing.get("key", _slugify_type_key(existing.get("name", "")))
        else:
            self._key = None

        outer.addLayout(form)

        # Fields editor.
        fields_box = QGroupBox("Fields (schema for KGs of this type)")
        fb = QVBoxLayout(fields_box)
        fb.setContentsMargins(10, 8, 10, 8)
        fb.setSpacing(6)
        fb_hint = QLabel(
            "These are the inputs shown on the KG detail page for entries "
            "of this type. Reorder with the arrows. The MQ defaults — "
            "<code>concept</code>, <code>stem_html</code>, <code>system</code>, "
            "<code>subsystem</code>, <code>topic</code>, <code>platform</code>, "
            "<code>notes</code> — are read by the QBank capture dialog and "
            "shouldn't be renamed unless you know what you're doing."
        )
        fb_hint.setTextFormat(Qt.TextFormat.RichText)
        fb_hint.setStyleSheet("color: gray; font-size: 11px;")
        fb_hint.setWordWrap(True)
        fb.addWidget(fb_hint)

        self._fields_list = QListWidget()
        self._fields_list.setAlternatingRowColors(True)
        self._fields_list.itemDoubleClicked.connect(lambda _it: self._edit_field())
        self._refresh_fields_list()
        fb.addWidget(self._fields_list)

        f_btn_row = QHBoxLayout()
        add_f = QPushButton("＋ Add field"); add_f.clicked.connect(self._add_field)
        edit_f = QPushButton("Edit…");      edit_f.clicked.connect(self._edit_field)
        rm_f = QPushButton("Remove");       rm_f.clicked.connect(self._remove_field)
        up_f = QPushButton("↑");            up_f.setFixedWidth(28); up_f.clicked.connect(lambda: self._move_field(-1))
        dn_f = QPushButton("↓");            dn_f.setFixedWidth(28); dn_f.clicked.connect(lambda: self._move_field(1))
        f_btn_row.addWidget(add_f); f_btn_row.addWidget(edit_f); f_btn_row.addWidget(rm_f)
        f_btn_row.addStretch(1)
        f_btn_row.addWidget(up_f); f_btn_row.addWidget(dn_f)
        fb.addLayout(f_btn_row)

        outer.addWidget(fields_box)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        _synapse_theme(self, dialog=True)

    def _pick_color(self):
        current = QColor(self._color.text().strip() or "#6b7280")
        chosen = QColorDialog.getColor(current, self, "Pick a colour")
        if chosen.isValid():
            self._color.setText(chosen.name())

    def _update_preview(self, txt: str):
        if re.match(r"^#[0-9A-Fa-f]{6}$", txt or ""):
            self._color_preview.setStyleSheet(
                f"background:{txt}; border-radius: 4px; border: 1px solid #999;"
            )

    def _on_save(self):
        name = self._name.text().strip()
        if not name:
            showWarning("Name is required.")
            return
        color = self._color.text().strip() or "#6b7280"
        if not re.match(r"^#[0-9A-Fa-f]{6}$", color):
            showWarning(f"Colour {color!r} isn't a valid #rrggbb hex.")
            return
        self.accept()

    # ── fields sub-editor ────────────────────────────────────────────────────

    def _refresh_fields_list(self):
        self._fields_list.clear()
        kind_lookup = dict(FIELD_KIND_LABELS)
        for f in self._fields:
            kind_lbl = kind_lookup.get(f.get("kind", "text"), f.get("kind", ""))
            label = f"{f.get('label','')}    "
            sub = f"key={f.get('key','')} · {kind_lbl}"
            if f.get("placeholder"):
                ph = f["placeholder"]
                sub += f' · "{ph[:40] + ("…" if len(ph) > 40 else "")}"'
            li = QListWidgetItem(label + "  ·  " + sub)
            li.setData(Qt.ItemDataRole.UserRole, f.get("key"))
            self._fields_list.addItem(li)

    def _selected_field_index(self) -> int:
        li = self._fields_list.currentItem()
        if li is None:
            return -1
        key = li.data(Qt.ItemDataRole.UserRole)
        for i, f in enumerate(self._fields):
            if f.get("key") == key:
                return i
        return -1

    def _add_field(self):
        dlg = _FieldEditorDialog(self)
        if not dlg.exec():
            return
        new = dlg.values()
        # Avoid duplicate keys.
        existing_keys = {f["key"] for f in self._fields}
        base = new["key"]
        i = 2
        while new["key"] in existing_keys:
            new["key"] = f"{base}_{i}"
            i += 1
        self._fields.append(new)
        self._refresh_fields_list()

    def _edit_field(self):
        idx = self._selected_field_index()
        if idx < 0:
            tooltip("Pick a field first.")
            return
        dlg = _FieldEditorDialog(self, existing=self._fields[idx])
        if not dlg.exec():
            return
        self._fields[idx] = dlg.values()
        self._refresh_fields_list()

    def _remove_field(self):
        idx = self._selected_field_index()
        if idx < 0:
            tooltip("Pick a field first.")
            return
        from aqt.utils import askUser
        if not askUser(
            f"Remove field '{self._fields[idx].get('label', '')}'?\n\n"
            "Existing KGs that have data for this key keep it in storage — "
            "it just won't show in the editor any more.",
            defaultno=True,
        ):
            return
        self._fields.pop(idx)
        self._refresh_fields_list()

    def _move_field(self, delta: int):
        idx = self._selected_field_index()
        if idx < 0:
            return
        target = idx + delta
        if target < 0 or target >= len(self._fields):
            return
        self._fields[idx], self._fields[target] = self._fields[target], self._fields[idx]
        self._refresh_fields_list()
        self._fields_list.setCurrentRow(target)

    def values(self, fallback_key: str = "") -> dict:
        name = self._name.text().strip()
        return {
            "key":              self._key or _slugify_type_key(name) or fallback_key,
            "name":             name,
            "color":            self._color.text().strip() or "#6b7280",
            "description":      self._description.toPlainText().strip(),
            "auto_tag":         bool(self._auto_tag.isChecked()),
            "fields":           [dict(f) for f in self._fields],
        }


class _KnowledgeGapsTab(QWidget):
    def __init__(self, kg_cfg: dict, ga_cfg: dict | None = None, parent=None):
        super().__init__(parent)
        # Working copy of the types list — committed on Save.
        self._types: list[dict] = [dict(t) for t in (kg_cfg.get("types") or [])]
        ga_cfg = ga_cfg or {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Basic options form
        form = _expand_form(QFormLayout())
        form.setVerticalSpacing(8)

        self._show_home_button = QCheckBox(
            "Show ＋ KG button on the deck browser home screen"
        )
        self._show_home_button.setChecked(bool(kg_cfg.get("show_home_button", True)))
        form.addRow(self._show_home_button)

        self._confirm_on_delete = QCheckBox("Confirm before deleting a KG")
        self._confirm_on_delete.setChecked(bool(kg_cfg.get("confirm_on_delete", True)))
        form.addRow(self._confirm_on_delete)

        self._default_type_on_add = QComboBox()
        self._rebuild_default_type_combo(
            str(kg_cfg.get("default_type_on_add") or "kg")
        )
        form.addRow("Default type on Add:", self._default_type_on_add)

        # Shared auto-tag scheme — the single source of truth for BOTH Create
        # and Browse. Base prefix + per-type name + extracted levels.
        self._auto_tag_base = QLineEdit(str(kg_cfg.get("auto_tag_base") or ""))
        self._auto_tag_base.setMinimumWidth(420)
        self._auto_tag_base.setPlaceholderText("e.g. !!Fleg")
        self._auto_tag_base.setToolTip(
            "Shared base prefix for all auto-tags. Cards from Create and Browse "
            "sit together under <base>::MQ, <base>::KG, etc. (the type name is "
            "added automatically). Leave blank to disable auto-tagging entirely."
        )
        form.addRow("Auto-tag base:", self._auto_tag_base)

        self._tag_scheme = QLineEdit(
            kg_cfg.get("tag_scheme_template") or "{base}::{type}::{system}::{subsystem}::{topic}"
        )
        self._tag_scheme.setMinimumWidth(420)
        self._tag_scheme.setPlaceholderText("{base}::{type}::{system}::{subsystem}::{topic}")
        self._tag_scheme.setToolTip(
            "Template shared by Create and Browse. Available slots: "
            "{base} (the prefix above), {type} (the KG type's name, e.g. MQ/KG), "
            "{system}, {subsystem}, {topic}. Empty slots are dropped, so a topic "
            "with no clear subsystem still produces a usable tag."
        )
        form.addRow("Auto-tag template:", self._tag_scheme)

        root.addLayout(form)

        # Types editor section
        types_box = QGroupBox("KG Types")
        tv = QVBoxLayout(types_box)
        tv.setContentsMargins(10, 8, 10, 8)
        tv.setSpacing(6)
        hint = QLabel(
            "Categories for KG entries. The three default types — MQ, KG, LO — "
            "map onto QBank captures, manual entries, and Analyse-LO results. "
            "Add your own, rename, recolour, or delete."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        tv.addWidget(hint)

        self._types_list = QListWidget()
        self._types_list.setAlternatingRowColors(True)
        self._types_list.itemDoubleClicked.connect(lambda _it: self._edit_type())
        tv.addWidget(self._types_list)
        self._refresh_types_list()

        type_btn_row = QHBoxLayout()
        add_btn = QPushButton("＋ Add type")
        add_btn.clicked.connect(self._add_type)
        edit_btn = QPushButton("Edit…")
        edit_btn.clicked.connect(self._edit_type)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_type)
        type_btn_row.addWidget(add_btn)
        type_btn_row.addWidget(edit_btn)
        type_btn_row.addWidget(remove_btn)
        type_btn_row.addStretch(1)
        tv.addLayout(type_btn_row)

        root.addWidget(types_box)

        # ── Analyse KG (formerly its own settings tab) ────────────────────────
        analyse_box = QGroupBox("Analyse KG (AI sub-feature)")
        ab = QFormLayout(analyse_box)
        ab.setContentsMargins(10, 8, 10, 8)
        ab.setVerticalSpacing(8)

        # Model lives on the AI tab (Settings → AI → "Gap analysis" model).
        # No tool-local picker here — the per-tool matrix is the single source.

        self._ga_front_field = QLineEdit(ga_cfg.get("front_field", "Text"))
        ab.addRow("Front field:", self._ga_front_field)

        self._ga_notetype_filter = QLineEdit(ga_cfg.get("notetype_filter", ""))
        self._ga_notetype_filter.setPlaceholderText("blank = search all notetypes")
        ab.addRow("Notetype filter:", self._ga_notetype_filter)

        self._ga_last_tag = QLineEdit(ga_cfg.get("last_used_tag", ""))
        self._ga_last_tag.setPlaceholderText("most-recent tag used (auto-saved)")
        ab.addRow("Last-used tag:", self._ga_last_tag)

        self._ga_max_cards = QSpinBox()
        self._ga_max_cards.setRange(5, 500)
        self._ga_max_cards.setValue(int(ga_cfg.get("max_cards", 80)))
        ab.addRow("Max cards sent to AI:", self._ga_max_cards)

        self._ga_max_gaps = QSpinBox()
        self._ga_max_gaps.setRange(1, 30)
        self._ga_max_gaps.setValue(int(ga_cfg.get("max_gaps", 10)))
        ab.addRow("Max gaps to return:", self._ga_max_gaps)

        ga_hint = QLabel(
            "<small>Pulls cards under a tag, asks AI what's missing, and "
            "pushes the approved gaps into the queue above.</small>"
        )
        ga_hint.setTextFormat(Qt.TextFormat.RichText)
        ga_hint.setStyleSheet("color: gray;")
        ga_hint.setWordWrap(True)
        ab.addRow(ga_hint)

        root.addWidget(analyse_box)

        outro = QLabel(
            "<small>The Knowledge Gaps tab is the unified queue for things you "
            "don't know — from manual notes, the Analyse KG sub-feature, captured "
            "QBank misses, or items saved from Browse. From any KG you can send "
            "to AI Browse, or create a card directly.</small>"
        )
        outro.setTextFormat(Qt.TextFormat.RichText)
        outro.setStyleSheet("color: gray;")
        outro.setWordWrap(True)
        root.addWidget(outro)

        root.addStretch(1)

    # ── types editor ─────────────────────────────────────────────────────────

    def _refresh_types_list(self):
        self._types_list.clear()
        for t in self._types:
            label = f"  ●  {t.get('name', '')}    "
            sub = []
            sub.append(f"key={t.get('key','')}")
            nf = len(t.get("fields") or [])
            sub.append(f"{nf} field{'s' if nf != 1 else ''}")
            if t.get("description"):
                d = t["description"]
                sub.append(d[:60] + ("…" if len(d) > 60 else ""))
            li = QListWidgetItem(label + "   ·   ".join(sub))
            li.setData(Qt.ItemDataRole.UserRole, t.get("key"))
            # Tint the bullet with the type's color via foreground role.
            try:
                li.setForeground(QColor(t.get("color", "#6b7280")))
            except Exception:
                pass
            self._types_list.addItem(li)
        # Also keep the "Default type on Add" combo in sync.
        prev = self._default_type_on_add.currentData() if self._default_type_on_add.count() else None
        self._rebuild_default_type_combo(prev)

    def _rebuild_default_type_combo(self, prev_key: str | None) -> None:
        self._default_type_on_add.blockSignals(True)
        self._default_type_on_add.clear()
        for t in self._types:
            self._default_type_on_add.addItem(t.get("name", ""), t.get("key", ""))
        if prev_key:
            idx = self._default_type_on_add.findData(prev_key)
            if idx >= 0:
                self._default_type_on_add.setCurrentIndex(idx)
        self._default_type_on_add.blockSignals(False)

    def _selected_type_key(self) -> str | None:
        li = self._types_list.currentItem()
        if li is None:
            return None
        return li.data(Qt.ItemDataRole.UserRole)

    def _add_type(self):
        dlg = _TypeEditorDialog(self)
        if not dlg.exec():
            return
        v = dlg.values()
        # Ensure key uniqueness — append a suffix if collision.
        existing_keys = {t["key"] for t in self._types}
        base = v["key"]
        i = 2
        while v["key"] in existing_keys:
            v["key"] = f"{base}_{i}"
            i += 1
        self._types.append(v)
        self._refresh_types_list()

    def _edit_type(self):
        key = self._selected_type_key()
        if not key:
            tooltip("Pick a type first.")
            return
        idx = next((i for i, t in enumerate(self._types) if t.get("key") == key), -1)
        if idx < 0:
            return
        dlg = _TypeEditorDialog(self, existing=self._types[idx])
        if not dlg.exec():
            return
        self._types[idx] = dlg.values()
        self._refresh_types_list()

    def _remove_type(self):
        key = self._selected_type_key()
        if not key:
            tooltip("Pick a type first.")
            return
        idx = next((i for i, t in enumerate(self._types) if t.get("key") == key), -1)
        if idx < 0:
            return
        name = self._types[idx].get("name", key)
        ok = showInfo if False else None  # placeholder
        from aqt.utils import askUser
        if not askUser(
            f"Remove the '{name}' type?\n\n"
            "KGs already tagged with this type will keep the key — they'll "
            "render with a faded fallback badge until you reassign them.",
            defaultno=True,
        ):
            return
        self._types.pop(idx)
        self._refresh_types_list()

    def get_values(self) -> dict:
        return {
            "show_home_button":      self._show_home_button.isChecked(),
            "confirm_on_delete":     self._confirm_on_delete.isChecked(),
            "default_status_on_add": "open",
            "default_type_on_add":   self._default_type_on_add.currentData() or "kg",
            "auto_tag_base":         self._auto_tag_base.text().strip(),
            "tag_scheme_template":   (
                self._tag_scheme.text().strip()
                or "{base}::{type}::{system}::{subsystem}::{topic}"
            ),
            "types":                 [dict(t) for t in self._types],
        }

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # No tool-local model widget: the model resolves from the AI tab matrix.
        return

    def get_gap_analyser_values(self) -> dict:
        return {
            "front_field":     self._ga_front_field.text().strip() or "Text",
            "notetype_filter": self._ga_notetype_filter.text().strip(),
            "last_used_tag":   self._ga_last_tag.text().strip(),
            "max_cards":       int(self._ga_max_cards.value()),
            "max_gaps":        int(self._ga_max_gaps.value()),
        }


class _CreatorTab(QWidget):
    def __init__(self, cc_cfg: dict, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        cfg = cfg or {}
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

        # Model lives on the AI tab (Settings → AI → "Card creation" model).
        # No tool-local picker here — the per-tool matrix is the single source.

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

        self._bg_gen = QCheckBox("Generate cards in the background (review later)")
        self._bg_gen.setChecked(bool(cc_cfg.get("background_generation", True)))
        self._bg_gen.setToolTip(
            "When on, Generate runs in the background so you can keep using Anki; "
            "finished cards appear in a 'Ready to review' list. When off, generation "
            "blocks and opens the review window immediately (the old behaviour)."
        )
        top_form.addRow("Background:", self._bg_gen)

        self._max_parallel = QSpinBox()
        self._max_parallel.setRange(1, 6)
        self._max_parallel.setValue(int(cc_cfg.get("max_parallel_jobs", 2)))
        self._max_parallel.setToolTip(
            "How many background generations may run at once. Extra ones queue. "
            "The manual/paste provider always runs one at a time."
        )
        top_form.addRow("Max parallel background jobs:", self._max_parallel)

        root.addLayout(top_form)

        # Shared with Browse — both flows can apply it, so it's shown (and
        # kept in sync) in both places. See _link_checkbox/_link_lineedit.
        mt_box, self._month_tag, self._month_tag_prefix = _month_tag_group(cfg)
        root.addWidget(mt_box)

        # ── Notetype profiles ────────────────────────────────────────────────
        nt_box = QGroupBox("Notetypes")
        nt_layout = QVBoxLayout(nt_box)
        nt_hint = QLabel(
            "The creator panel's notetype dropdown lists these profiles. "
            "Each profile maps a notetype to its field layout and can carry "
            "its own prompt addendum so AI tailors output to that style "
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

        # ── Card quality pass ────────────────────────────────────────────────
        # Seed preserves keys we don't surface in the UI (grader skill id/
        # invocation) so the wholesale {**existing, **get_values()} save in
        # _on_save doesn't drop them.
        self._qp_seed = dict(cc_cfg.get("quality_pass", {}) or {})
        qp_box = QGroupBox("Card quality pass")
        qp_layout = QVBoxLayout(qp_box)
        qp_hint = QLabel(
            "Scores each generated card against a retrieval-force rubric before "
            "the review screen, and acts on the score (pass / flag / regenerate). "
            "Off by default. Per-notetype overrides live in each notetype's editor."
        )
        qp_hint.setWordWrap(True)
        qp_hint.setStyleSheet("color: gray; font-size: 11px;")
        qp_layout.addWidget(qp_hint)

        self._qp_enabled = QCheckBox("Score generated cards before review")
        self._qp_enabled.setChecked(bool(self._qp_seed.get("enabled", False)))
        qp_layout.addWidget(self._qp_enabled)

        qp_form = _expand_form(QFormLayout())
        qp_form.setVerticalSpacing(8)

        self._qp_mode = QComboBox()
        self._qp_mode.addItem("Separate batch call (can use a cheaper grader model)", "separate_call")
        self._qp_mode.addItem("Same call as generation (self-grade, no extra round-trip)", "same_call")
        self._qp_mode.setCurrentIndex(
            1 if self._qp_seed.get("grading_mode") == "same_call" else 0)
        qp_form.addRow("Grading mode:", self._qp_mode)

        self._qp_action = QComboBox()
        self._qp_action.addItem("Flag only — pre-fill the regen hint", "flag_only")
        self._qp_action.addItem("Auto-regenerate failures", "auto_regenerate")
        self._qp_action.setCurrentIndex(
            1 if self._qp_seed.get("verdict_action") == "auto_regenerate" else 0)
        qp_form.addRow("On failure:", self._qp_action)

        self._qp_atomicity = QSpinBox()
        self._qp_atomicity.setRange(1, 6)
        self._qp_atomicity.setValue(int(self._qp_seed.get("atomicity_max_clozes", 2)))
        self._qp_atomicity.setToolTip(
            "How sensitive the atomicity check is. A note with this many "
            "independent clozes (or fewer) passes; only MORE than this is flagged "
            "for splitting. 2 means a clean 2-cloze card is fine. Also tells the "
            "generator how many coupled clozes it may use, so creation and "
            "scoring agree.")
        qp_form.addRow("Flag cards with more than N clozes:", self._qp_atomicity)

        self._qp_retries = QSpinBox()
        self._qp_retries.setRange(0, 3)
        self._qp_retries.setValue(int(self._qp_seed.get("auto_regen_max_retries", 2)))
        qp_form.addRow("Max auto-regen retries:", self._qp_retries)

        # Grader model lives on the AI tab (Settings → AI → "Quality pass" model).

        qp_layout.addLayout(qp_form)

        self._qp_regen_manual = QCheckBox(
            "Allow auto-regenerate in manual/paste mode (each retry is another paste)")
        self._qp_regen_manual.setChecked(bool(self._qp_seed.get("auto_regen_in_manual", False)))
        qp_layout.addWidget(self._qp_regen_manual)

        self._qp_prefer_skill = QCheckBox(
            "Prefer the anki-card-scorer skill on Anthropic (saves tokens)")
        self._qp_prefer_skill.setChecked(bool(self._qp_seed.get("prefer_skill", True)))
        qp_layout.addWidget(self._qp_prefer_skill)

        root.addWidget(qp_box)

        # ── Source grounding ─────────────────────────────────────────────────
        self._grounding_seed = dict(cc_cfg.get("grounding", {}) or {})
        gr_box = QGroupBox("Source grounding")
        gr_layout = QVBoxLayout(gr_box)
        gr_hint = QLabel(
            "Injects a curated clinical-guideline citation allow-list (for your "
            "chosen <b>region</b>) into <b>topic-based</b> generation, so cards cite "
            "real URLs instead of invented ones. In source mode your pasted material "
            "is the source, so grounding is skipped. Per-notetype overrides live "
            "in each notetype's editor; a per-batch checkbox sits on the Create panel."
        )
        gr_hint.setTextFormat(Qt.TextFormat.RichText)
        gr_hint.setWordWrap(True)
        gr_hint.setStyleSheet("color: gray; font-size: 11px;")
        gr_layout.addWidget(gr_hint)

        self._ground_enabled = QCheckBox("Ground topic-based cards in clinical guidelines by default")
        self._ground_enabled.setChecked(bool(self._grounding_seed.get("enabled", True)))
        gr_layout.addWidget(self._ground_enabled)

        self._ground_fetch_live = QCheckBox(
            "Fetch live guideline text into the prompt (slower; blocking network I/O)")
        self._ground_fetch_live.setChecked(bool(self._grounding_seed.get("fetch_live", False)))
        self._ground_fetch_live.setToolTip(
            "When on, the add-on downloads the allow-listed guideline pages and "
            "feeds their text to the model. Off (default) just supplies the URL "
            "allow-list, which already stops invented citations and keeps "
            "generation fast. With a CLI provider the model can fetch URLs itself."
        )
        gr_layout.addWidget(self._ground_fetch_live)

        # Region preset + editable source registry (transferable across regions).
        from ..grounding.guidelines import preset_keys, preset_label
        self._grounding_sources = list(self._grounding_seed.get("sources") or [])
        gr_form = _expand_form(QFormLayout())
        self._ground_region = QComboBox()
        for _k in preset_keys():
            self._ground_region.addItem(preset_label(_k), _k)
        self._ground_region.addItem("Custom (edit sources below)", "custom")
        _seed_region = str(self._grounding_seed.get("region") or "au_wa").lower()
        if _seed_region not in ("au_wa", "usa", "intl", "custom"):
            _seed_region = "au_wa"
        # Back-compat: an old config with edited sources but no region key is custom.
        if self._grounding_sources and not self._grounding_seed.get("region"):
            _seed_region = "custom"
        self._ground_region.setCurrentIndex(max(0, self._ground_region.findData(_seed_region)))
        self._ground_region.setToolTip(
            "Which shipped guideline set to cite. 'Custom' uses the sources you "
            "manage below; switching to a region uses its built-in set.")
        self._ground_region.currentIndexChanged.connect(self._on_ground_region_changed)
        gr_form.addRow("Guideline region:", self._ground_region)

        self._ground_region_label = QLineEdit(str(self._grounding_seed.get("region_label") or ""))
        self._ground_region_label.setPlaceholderText("Label for your custom set (e.g. 'UK & NICE')")
        self._ground_region_label.setToolTip(
            "Shown in the citation-block header. Only used for the Custom region.")
        gr_form.addRow("Custom label:", self._ground_region_label)
        gr_layout.addLayout(gr_form)

        src_row = QHBoxLayout()
        self._ground_src_summary = QLabel("")
        self._ground_src_summary.setStyleSheet("color: gray; font-size: 11px;")
        src_row.addWidget(self._ground_src_summary, 1)
        manage_btn = QPushButton("Manage sources…")
        manage_btn.clicked.connect(self._manage_grounding_sources)
        src_row.addWidget(manage_btn)
        gr_layout.addLayout(src_row)
        self._on_ground_region_changed()

        root.addWidget(gr_box)

        # ── Online images ────────────────────────────────────────────────────
        self._images_seed = dict(cc_cfg.get("images", {}) or {})
        img_box = QGroupBox("Online images")
        img_layout = QVBoxLayout(img_box)
        img_hint = QLabel(
            "Find images for cards from the review screen. <b>Auto-image</b> lets "
            "the AI flag cards that need a visual and fetches candidates for just "
            "those; per-card 🔍 search and 🌐 browse always work. Scraped sources "
            "(DermNet, Radiopaedia) are best-effort and may break; Bing needs an "
            "API key."
        )
        img_hint.setTextFormat(Qt.TextFormat.RichText)
        img_hint.setWordWrap(True)
        img_hint.setStyleSheet("color: gray; font-size: 11px;")
        img_layout.addWidget(img_hint)

        self._img_auto = QCheckBox("Auto-find images for visual cards by default")
        self._img_auto.setChecked(bool(self._images_seed.get("auto_find", False)))
        img_layout.addWidget(self._img_auto)

        srcs = self._images_seed.get("sources") or {}
        src_box = QHBoxLayout()
        src_box.addWidget(QLabel("Sources:"))
        self._img_src_wikipedia = QCheckBox("Wikipedia")
        self._img_src_wikipedia.setChecked(bool(srcs.get("wikipedia", True)))
        self._img_src_openi = QCheckBox("Open-i")
        self._img_src_openi.setChecked(bool(srcs.get("openi", True)))
        self._img_src_dermnet = QCheckBox("DermNet")
        self._img_src_dermnet.setChecked(bool(srcs.get("dermnet", True)))
        self._img_src_radiopaedia = QCheckBox("Radiopaedia")
        self._img_src_radiopaedia.setChecked(bool(srcs.get("radiopaedia", True)))
        self._img_src_bing = QCheckBox("Bing")
        self._img_src_bing.setChecked(bool(srcs.get("bing", False)))
        for w in (self._img_src_wikipedia, self._img_src_openi, self._img_src_dermnet,
                  self._img_src_radiopaedia, self._img_src_bing):
            src_box.addWidget(w)
        src_box.addStretch(1)
        img_layout.addLayout(src_box)

        img_form = _expand_form(QFormLayout())
        self._img_bing_key = QLineEdit(str(self._images_seed.get("bing_api_key") or ""))
        self._img_bing_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._img_bing_key.setPlaceholderText("Azure Bing Image Search key (blank → Bing off)")
        img_form.addRow("Bing API key:", self._img_bing_key)

        self._img_browse_engine = QComboBox()
        self._img_browse_engine.addItem("DuckDuckGo (embeds cleanly)", "duckduckgo")
        self._img_browse_engine.addItem("Bing Images", "bing")
        self._img_browse_engine.addItem("Google Images", "google")
        self._img_browse_engine.addItem("Custom URL…", "custom")
        _eng = str(self._images_seed.get("browse_engine") or "duckduckgo")
        _ei = max(0, self._img_browse_engine.findData(_eng))
        self._img_browse_engine.setCurrentIndex(_ei)
        img_form.addRow("Browse engine:", self._img_browse_engine)

        self._img_browse_custom = QLineEdit(str(self._images_seed.get("browse_custom_url") or ""))
        self._img_browse_custom.setPlaceholderText("https://example.com/search?q={q}  ({q} = query)")
        img_form.addRow("Custom browse URL:", self._img_browse_custom)
        img_layout.addLayout(img_form)

        root.addWidget(img_box)

        root.addStretch(1)

    # ── grounding source editor ──────────────────────────────────────────────

    def _on_ground_region_changed(self, *_):
        is_custom = self._ground_region.currentData() == "custom"
        self._ground_region_label.setEnabled(is_custom)
        self._refresh_ground_src_summary()

    def _refresh_ground_src_summary(self):
        from ..grounding.guidelines import region_preset
        region = self._ground_region.currentData()
        if region == "custom":
            n = len(self._grounding_sources)
            self._ground_src_summary.setText(
                f"{n} custom guideline source(s) configured."
                if n else "No custom sources yet — add some via Manage sources…")
        else:
            n = len(region_preset(region)["sources"])
            self._ground_src_summary.setText(
                f"Using the built-in {self._ground_region.currentText()} set "
                f"({n} sources). Edit them to switch to Custom.")

    def _manage_grounding_sources(self):
        from ..grounding.guidelines import region_preset
        region = self._ground_region.currentData()
        # Seed the editor from the custom list when on Custom, else from the
        # selected region's preset (a starting point the user can tweak).
        if region == "custom" and self._grounding_sources:
            current = self._grounding_sources
        else:
            current = [dict(g) for g in region_preset(region)["sources"]]
        # When the Create-tab combo is on a preset, seed the dialog from that
        # region; otherwise (custom) default the dialog's preset picker to au_wa.
        dlg_region = region if region in ("au_wa", "usa", "intl") else "au_wa"
        dlg = _GroundingSourcesDialog(current, dlg_region, self)
        if dlg.exec():
            self._grounding_sources = dlg.result_sources()
            # A non-empty list means we're now Custom; an empty result (reset to a
            # preset) snaps the region combo back to that preset.
            target = "custom" if self._grounding_sources else dlg.result_region()
            self._ground_region.setCurrentIndex(
                max(0, self._ground_region.findData(target)))
            self._on_ground_region_changed()

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
            qp_ov = str(p.get("quality_pass_override", "inherit")).lower()
            qp_marker = f" · QP:{qp_ov}" if qp_ov in ("on", "off") else ""
            gr_ov = str(p.get("grounding_override", "inherit")).lower()
            gr_marker = f" · ground:{gr_ov}" if gr_ov in ("on", "off") else ""
            fmt = str(p.get("card_format", "cloze")).lower()
            fmt_marker = " · Q&A" if fmt == "qa" else ""
            lbl = QLabel(
                f"<b>{p['name']}</b>"
                f"  <span style='color:gray;font-size:11px'>"
                f"front=<code>{p.get('front_field', 'Text')}</code> · "
                f"extra=<code>{p.get('extra_field', 'Extra')}</code> · "
                f"image=<code>{p.get('image_field', p.get('extra_field', 'Extra'))}</code>"
                f"{sources_marker}{skill_marker}{qp_marker}{gr_marker}{fmt_marker}{instr_marker}</span>"
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

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # No tool-local model widgets: card-creation and grader models both
        # resolve from the AI tab matrix.
        return

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
            "default_deck":     self._deck.currentText().strip(),
            "default_tags":     tags_raw,
            "audit_tag":        self._audit_tag.text().strip(),
            "default_n_cards":  int(self._n_cards.value()),
            "gap_n_cards":      int(self._gap_n_cards.value()),
            "background_generation": self._bg_gen.isChecked(),
            "max_parallel_jobs": int(self._max_parallel.value()),
            "notetypes":        list(self._profiles),
            "selected_notetype": selected,
            "default_notetype": selected,  # legacy mirror
            "front_field":      first.get("front_field", "Text"),
            "extra_field":      first.get("extra_field", "Extra"),
            "one_by_one_field": first.get("one_by_one_field", "One by one"),
            # Spread over the seed so un-exposed keys (grader skill id/invocation)
            # survive the wholesale replace in _on_save.
            "quality_pass": {
                **self._qp_seed,
                "enabled":              self._qp_enabled.isChecked(),
                "grading_mode":         self._qp_mode.currentData(),
                "verdict_action":       self._qp_action.currentData(),
                "atomicity_max_clozes": int(self._qp_atomicity.value()),
                "auto_regen_max_retries": int(self._qp_retries.value()),
                "auto_regen_in_manual": self._qp_regen_manual.isChecked(),
                "prefer_skill":         self._qp_prefer_skill.isChecked(),
                # Grader model now resolves from the AI tab matrix; neutralise any
                # stale per-tool override so it can't shadow the matrix choice.
                "grader_model_override": False,
            },
            "grounding": _grounding_values(
                self._grounding_seed,
                self._ground_enabled.isChecked(),
                self._ground_fetch_live.isChecked(),
                self._ground_region.currentData(),
                self._ground_region_label.text().strip(),
                self._grounding_sources,
            ),
            "images": {
                **self._images_seed,
                "auto_find": self._img_auto.isChecked(),
                "sources": {
                    "wikipedia":   self._img_src_wikipedia.isChecked(),
                    "openi":       self._img_src_openi.isChecked(),
                    "dermnet":     self._img_src_dermnet.isChecked(),
                    "radiopaedia": self._img_src_radiopaedia.isChecked(),
                    "bing":        self._img_src_bing.isChecked(),
                },
                "bing_api_key":      self._img_bing_key.text().strip(),
                "browse_engine":     self._img_browse_engine.currentData(),
                "browse_custom_url": self._img_browse_custom.text().strip(),
            },
        }


def _grounding_values(seed: dict, enabled: bool, fetch_live: bool, region: str,
                      custom_label: str, custom_sources: list) -> dict:
    """Build the card_creator.grounding config dict. For a region preset we store
    region=<key> with sources=[] (resolved live from the preset) and a derived
    label; for 'custom' we store the edited sources + the user's label."""
    from ..grounding.guidelines import preset_label
    out = dict(seed)
    out["enabled"] = enabled
    out["fetch_live"] = fetch_live
    if region == "custom":
        out["region"] = "custom"
        out["sources"] = list(custom_sources)
        out["region_label"] = custom_label or "Custom"
    else:
        out["region"] = region
        out["sources"] = []
        out["region_label"] = preset_label(region)
    return out


class _GroundingSourcesDialog(QDialog):
    """Editable list of guideline sources (name / URL / fetchable / specialties),
    so the citation allow-list is transferable across regions. A preset loader
    populates the list from a shipped region set; 'Reset to … preset' discards
    edits and falls back to that region's built-in set."""

    def __init__(self, sources: list, region: str = "au_wa", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage guideline sources")
        self.setMinimumSize(880, 600)
        self._sources = [dict(s) for s in (sources or [])]
        self._region = region if region in ("au_wa", "usa", "intl") else "au_wa"
        self._cleared = False
        self._build()
        _synapse_theme(self, dialog=True)

    def _build(self):
        from ..grounding.guidelines import preset_keys, preset_label
        root = QVBoxLayout(self)
        hint = QLabel(
            "These URLs form the citation allow-list injected into topic-based "
            "generation — the model may cite only these. <b>Specialties</b> are "
            "comma-separated keywords matched against the topic/tags ( * = always). "
            "Editing this list makes it your <b>Custom</b> set; URLs must be real."
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(hint)

        # Preset loader — populate the list from a shipped region set.
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Start from preset:"))
        self._preset_combo = QComboBox()
        for _k in preset_keys():
            self._preset_combo.addItem(preset_label(_k), _k)
        self._preset_combo.setCurrentIndex(max(0, self._preset_combo.findData(self._region)))
        preset_row.addWidget(self._preset_combo, 1)
        load_btn = QPushButton("Load preset")
        load_btn.setToolTip("Replace the list below with this region's built-in sources "
                            "(you can then tweak them).")
        load_btn.clicked.connect(self._load_preset)
        preset_row.addWidget(load_btn)
        root.addLayout(preset_row)

        body = QHBoxLayout()
        body.setSpacing(16)
        self._list = QListWidget()
        self._list.setMinimumWidth(300)
        self._list.currentRowChanged.connect(self._on_row)
        body.addWidget(self._list, 2)

        form = _expand_form(QFormLayout())
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._f_name = QLineEdit()
        self._f_url = QLineEdit()
        self._f_specs = QLineEdit()
        self._f_specs.setPlaceholderText("cardiology, arrhythmia, *")
        for w in (self._f_name, self._f_url, self._f_specs):
            w.setMinimumWidth(380)
            w.setClearButtonEnabled(True)
            w.textChanged.connect(self._on_field_edit)
        self._f_url.setPlaceholderText("https://… (must be a real page)")
        self._f_fetchable = QCheckBox("Publicly fetchable (no login)")
        self._f_fetchable.toggled.connect(self._on_field_edit)
        form.addRow("Name:", self._f_name)
        form.addRow("URL:", self._f_url)
        form.addRow("Specialties:", self._f_specs)
        form.addRow("", self._f_fetchable)
        fw = QWidget()
        fw.setLayout(form)
        body.addWidget(fw, 3)
        root.addLayout(body, 1)

        btns = QHBoxLayout()
        add = QPushButton("+ Add")
        add.clicked.connect(self._add)
        rem = QPushButton("− Remove")
        rem.clicked.connect(self._remove)
        self._defaults_btn = QPushButton(f"Reset to {preset_label(self._region)} preset")
        self._defaults_btn.setToolTip("Discard edits and fall back to the selected "
                                      "region's built-in guideline set.")
        self._defaults_btn.clicked.connect(self._use_defaults)
        btns.addWidget(add)
        btns.addWidget(rem)
        btns.addStretch(1)
        btns.addWidget(self._defaults_btn)
        root.addLayout(btns)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._loading = False
        self._reload_list()
        if self._sources:
            self._list.setCurrentRow(0)

    def _load_preset(self):
        from ..grounding.guidelines import region_preset, preset_label
        from aqt.utils import askUser
        key = self._preset_combo.currentData()
        if self._sources and not askUser(
            f"Replace the current list with the built-in {preset_label(key)} set?",
            parent=self):
            return
        self._region = key
        self._sources = [dict(g) for g in region_preset(key)["sources"]]
        self._defaults_btn.setText(f"Reset to {preset_label(key)} preset")
        self._reload_list()
        if self._sources:
            self._list.setCurrentRow(0)

    def _reload_list(self):
        self._loading = True
        self._list.clear()
        for s in self._sources:
            self._list.addItem(QListWidgetItem(s.get("name") or s.get("url") or "(unnamed)"))
        self._loading = False

    def _on_row(self, row: int):
        self._loading = True
        if 0 <= row < len(self._sources):
            s = self._sources[row]
            self._f_name.setText(str(s.get("name") or ""))
            self._f_url.setText(str(s.get("url") or ""))
            self._f_specs.setText(", ".join(s.get("specialties") or []))
            self._f_fetchable.setChecked(bool(s.get("fetchable")))
        else:
            self._f_name.clear()
            self._f_url.clear()
            self._f_specs.clear()
            self._f_fetchable.setChecked(False)
        self._loading = False

    def _on_field_edit(self, *_):
        if self._loading:
            return
        row = self._list.currentRow()
        if not (0 <= row < len(self._sources)):
            return
        specs = [p.strip() for p in self._f_specs.text().split(",") if p.strip()]
        self._sources[row] = {
            "name": self._f_name.text().strip(),
            "url": self._f_url.text().strip(),
            "fetchable": self._f_fetchable.isChecked(),
            "specialties": specs,
        }
        item = self._list.item(row)
        if item:
            item.setText(self._f_name.text().strip() or self._f_url.text().strip() or "(unnamed)")

    def _add(self):
        self._sources.append({"name": "New source", "url": "", "fetchable": False,
                              "specialties": []})
        self._reload_list()
        self._list.setCurrentRow(len(self._sources) - 1)
        self._f_name.setFocus()
        self._f_name.selectAll()

    def _remove(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._sources):
            del self._sources[row]
            self._reload_list()
            self._list.setCurrentRow(min(row, len(self._sources) - 1))

    def _use_defaults(self):
        # Reset to the currently selected region's preset (the dialog's _region,
        # which tracks the last loaded preset). The caller switches the Create-tab
        # region combo to result_region() when the result list is empty.
        self._region = self._preset_combo.currentData() or self._region
        self._cleared = True
        self.accept()

    def result_sources(self) -> list:
        """The edited list, or [] when 'Reset to … preset' was chosen (empty →
        the caller falls back to result_region()'s built-in set)."""
        if self._cleared:
            return []
        return [s for s in self._sources if s.get("url") and s.get("name")]

    def result_region(self) -> str:
        """The region whose preset should be used when result_sources() is empty."""
        return self._region


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


class _LectureTab(QWidget):
    """AI Lecture settings. Small by design — the tool is a focused one, and its
    per-run knobs (points, budget, vision) also live on the panel itself; this
    tab sets their defaults and the tagging that has no panel control."""

    def __init__(self, lec_cfg: dict, parent=None):
        super().__init__(parent)
        self._lec_cfg = lec_cfg or {}
        d = DEFAULTS["tools"]["lecture"]

        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        intro = QLabel(
            "<small>Defaults for AI Lecture. The point target, note budget and "
            "vision toggle also appear on the tool's own panel — this sets what "
            "they start at.</small>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addRow(intro)

        # Model lives on the AI tab (Settings → AI → the Lecture rows); no
        # tool-local model picker, matching Browse.

        self._target_points = QSpinBox()
        self._target_points.setRange(5, 200)
        self._target_points.setValue(int(self._lec_cfg.get("target_points", d["target_points"])))
        self._target_points.setToolTip(
            "A target, not a cap — the AI aims here and consolidates toward it, "
            "but whatever it finds is kept. Ignored when you paste your own "
            "learning objectives.")
        layout.addRow("Learning points (target):", self._target_points)

        self._target_cards = QSpinBox()
        self._target_cards.setRange(5, 500)
        self._target_cards.setValue(int(self._lec_cfg.get("target_cards", d["target_cards"])))
        self._target_cards.setToolTip(
            "Rough estimate of how many notes to surface — not a hard budget. "
            "Shared out so every point gets its best note before any gets a "
            "second, and never padded: expect fewer than this, not exactly it.")
        layout.addRow("Notes to find (approx.):", self._target_cards)

        self._candidate_cap = QSpinBox()
        self._candidate_cap.setRange(3, 25)
        self._candidate_cap.setValue(int(self._lec_cfg.get("candidate_cap", d["candidate_cap"])))
        self._candidate_cap.setToolTip(
            "How many candidate notes per point the AI judges for relevance. "
            "Lower = smaller, faster, cheaper relevance calls — but it drops "
            "cards: the pre-judge ranking only loosely predicts what the judge "
            "keeps, so trimming the tail loses roughly one good card per point "
            "for every 3 you cut. 10 is the quality floor; lower only to save "
            "cost on a throttled account.")
        layout.addRow("Candidates per point:", self._candidate_cap)

        self._rel_workers = QSpinBox()
        self._rel_workers.setRange(1, 8)
        self._rel_workers.setValue(
            int(self._lec_cfg.get("relevance_workers", d["relevance_workers"])))
        self._rel_workers.setToolTip(
            "How many relevance calls run at once. Judging is the slow leg — "
            "tens of seconds per call — and the calls are independent, so this "
            "is close to a straight division of the wait: 4 workers turned a "
            "20-minute bulk gap search into a few minutes. Each one is a "
            "separate AI request, so raise it only if your provider is happy "
            "with the concurrency. 1 = the old one-at-a-time behaviour.")
        layout.addRow("Parallel relevance calls:", self._rel_workers)

        self._use_vision = QCheckBox("Read diagrams and tables as images (slower, costs more)")
        self._use_vision.setChecked(bool(self._lec_cfg.get("use_vision", d["use_vision"])))
        self._use_vision.setToolTip(
            "Off by default. Text and local OCR read almost every slide. Turn on "
            "only for lectures whose meaning lives in the LAYOUT (a drug-to-class "
            "matching table). Costs ~4-5x and adds minutes.")
        layout.addRow("Vision:", self._use_vision)

        self._use_ocr = QCheckBox("Use local OCR for pages with no text layer")
        self._use_ocr.setChecked(bool(self._lec_cfg.get("use_local_ocr", d["use_local_ocr"])))
        self._use_ocr.setToolTip(
            "Soft-detects tesseract or another add-on's OCR for scanned / "
            "image-only pages. Never runs on a page that already has text. Falls "
            "back to vision (if on) when no engine is found.")
        layout.addRow("Local OCR:", self._use_ocr)

        self._high_yield = QCheckBox("Drop low-yield padding (stats, 'emerging' asides, long lists)")
        self._high_yield.setChecked(bool(self._lec_cfg.get("high_yield_only", d["high_yield_only"])))
        self._high_yield.setToolTip(
            "On by default. Keeps only points a card could actually test. Off "
            "extracts everything the lecture says. Also on the tool's panel.")
        layout.addRow("No low-yield:", self._high_yield)

        self._tag_root = QLineEdit(str(self._lec_cfg.get("tag_root", d["tag_root"])))
        self._tag_root.setMinimumWidth(420)
        self._tag_root.setPlaceholderText("lecture")
        self._tag_root.setToolTip(
            "The parent tag matches land under — the panel pre-fills "
            "'<root>::<lecture name>' from the file name.")
        layout.addRow("Tag root:", self._tag_root)

        self._audit_tag = QLineEdit(str(self._lec_cfg.get("audit_tag", d["audit_tag"])))
        self._audit_tag.setMinimumWidth(420)
        self._audit_tag.setPlaceholderText("e.g. !!Fleg::AI::Lecture")
        attach_tag_completer(self._audit_tag)
        self._audit_tag.setToolTip(
            "A marker tag added to every note this tool touches, so AI-sourced "
            "notes stay auditable. Leave blank to skip it.")
        layout.addRow("Audit tag:", self._audit_tag)

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # No tool-local model widget: the model resolves from the AI tab matrix.
        return

    def get_values(self) -> dict:
        return {
            "target_points":  int(self._target_points.value()),
            "target_cards":   int(self._target_cards.value()),
            "candidate_cap":  int(self._candidate_cap.value()),
            "relevance_workers": int(self._rel_workers.value()),
            "use_vision":     self._use_vision.isChecked(),
            "use_local_ocr":  self._use_ocr.isChecked(),
            "high_yield_only": self._high_yield.isChecked(),
            "tag_root":       self._tag_root.text().strip() or "lecture",
            "audit_tag":      self._audit_tag.text().strip(),
        }


class _SynapseTab(QWidget):
    """The SynapsePro bridge — one kill switch per behaviour.

    Every switch here returns Ankisstant to exactly how it looked and behaved
    before the bridge existed, which is the contract the whole integration is
    built on. Nothing on this page writes anything into SynapsePro; the bridge
    only ever reads its palette.
    """

    def __init__(self, syn_cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = syn_cfg or {}
        d = DEFAULTS["synapse"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # State in words, never by colouring something green — a status you can
        # only read by its colour isn't a status.
        try:
            from ..core import synapse
            present = synapse.synapse_available()
        except Exception:
            present = False
        status = QLabel(
            "<b>SynapsePro detected.</b> The settings below are active."
            if present else
            "<b>SynapsePro not detected.</b> Everything below is inert until it's "
            "installed and enabled — Ankisstant looks and behaves exactly as it "
            "always has."
        )
        status.setTextFormat(Qt.TextFormat.RichText)
        status.setWordWrap(True)
        layout.addWidget(status)

        intro = QLabel(
            "<small>When SynapsePro is running, Ankisstant borrows its colours "
            "and puts its front door in SynapsePro's icon strip, so the two read "
            "as one app. Turn any of it off and that part goes back to how it "
            "was. Nothing here changes SynapsePro itself.</small>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addWidget(intro)

        self._checks: dict[str, QCheckBox] = {}

        def add_group(title: str, rows: list[tuple[str, str, str]]) -> None:
            box = QGroupBox(title)
            form = QVBoxLayout(box)
            form.setContentsMargins(12, 10, 12, 10)
            form.setSpacing(6)
            for key, label, tip in rows:
                cb = QCheckBox(label)
                cb.setChecked(bool(self._cfg.get(key, d[key])))
                cb.setToolTip(tip)
                hint = QLabel(f"<small>{tip}</small>")
                hint.setTextFormat(Qt.TextFormat.RichText)
                hint.setWordWrap(True)
                hint.setStyleSheet("color: gray; margin-left: 20px;")
                form.addWidget(cb)
                form.addWidget(hint)
                self._checks[key] = cb
            layout.addWidget(box)

        add_group("Appearance", [
            ("theme_bridge", "Use SynapsePro's colours",
             "Ankisstant follows whichever colour theme SynapsePro is on, "
             "including a change made while a window is open."),
            ("match_font", "Also use its font",
             "Off by default — every other add-on window uses Anki's own font, "
             "so matching SynapsePro's makes Ankisstant the odd one out."),
            ("theme_settings", "Theme the main window and Settings",
             "The two big windows. Spin boxes, drop-downs and lists are "
             "restyled; checkboxes are deliberately left native so their "
             "state always reads correctly."),
            ("theme_dialogs", "Theme the smaller dialogs",
             "Add KG, the Create editors, bulk search, screenshot capture and "
             "the rest."),
        ])

        add_group("Where Ankisstant lives", [
            ("sidebar_buttons", "Add Ankisstant to SynapsePro's icon strip",
             "Two buttons: one opens Ankisstant, one adds a knowledge gap "
             "without opening anything. The per-tool icons stay inside "
             "Ankisstant's own rail rather than crowding the strip."),
            ("hide_toolbar_link", "Drop the \"Ankisstant\" link from Anki's top toolbar",
             "Only while the strip button is actually there — if SynapsePro is "
             "missing or the button fails to appear, the link stays put. The "
             "Tools menu entry and Ctrl+Shift+L are never touched."),
        ])

        mode_box = QGroupBox("Opening Ankisstant")
        mode_layout = _expand_form(QFormLayout(mode_box))
        mode_layout.setContentsMargins(12, 10, 12, 10)
        self._open_mode = QComboBox()
        self._open_mode.addItem("Side panel, beside SynapsePro's own panels", "dock")
        self._open_mode.addItem("Separate window", "window")
        idx = self._open_mode.findData(self._cfg.get("open_mode", d["open_mode"]))
        self._open_mode.setCurrentIndex(max(0, idx))
        self._open_mode.setToolTip(
            "The pop-out button inside Ankisstant switches between these too — "
            "this is just what it starts as.")
        mode_layout.addRow("Open as:", self._open_mode)
        mode_hint = QLabel(
            "<small>As a side panel it sits on the right and several panels can "
            "be open at once. The nav column becomes an icon rail there to leave "
            "room for the tools. Without SynapsePro it's always a separate "
            "window.</small>")
        mode_hint.setTextFormat(Qt.TextFormat.RichText)
        mode_hint.setWordWrap(True)
        mode_hint.setStyleSheet("color: gray;")
        mode_layout.addRow(mode_hint)
        layout.addWidget(mode_box)

        layout.addStretch(1)

    def set_model_family(self, family: str, manual: bool = False) -> None:
        # No model widget on this tab — it configures presentation, not AI.
        return

    def get_values(self) -> dict:
        values = {k: cb.isChecked() for k, cb in self._checks.items()}
        values["open_mode"] = self._open_mode.currentData() or "dock"
        # dock_width isn't exposed: it's whatever the user last dragged the
        # panel to, and a number box for it would be a worse control than the
        # panel edge itself.
        return values


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
            "<p>Four AI-powered tools, bundled together:</p>"
            "<ul>"
            "<li><b>AI QBank</b> — log missed QBank questions and "
            "generate cards from them.</li>"
            "<li><b>AI Browse</b> — natural-language search across "
            "your collection.</li>"
            "<li><b>Analyse Knowledge Gaps</b> — audit a tag against a learning "
            "objective.</li>"
            "<li><b>AI Create</b> — generate cards from text, URLs, "
            "PDFs, or PowerPoints.</li>"
            "</ul>"
            "<p><b>No telemetry.</b> Nothing is sent anywhere except your chosen "
            "AI provider (Anthropic API, Gemini, OpenAI, Ollama, or the local Claude Code CLI).</p>"
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

        thanks_patrick = QLabel(
            "<small>Thanks to "
            "<a href='https://drpatricklee.substack.com/'>Patrick Lee</a> "
            "for feedback and inspiration.</small>"
        )
        thanks_patrick.setTextFormat(Qt.TextFormat.RichText)
        thanks_patrick.setOpenExternalLinks(True)
        thanks_patrick.setWordWrap(True)
        v.addWidget(thanks_patrick)

        thanks_heatmap = QLabel(
            "<small>Thanks to "
            "<a href='https://ankiweb.net/shared/info/1771074083'>"
            "Review Heatmap</a> by Glutanimate for the original heatmap.</small>"
        )
        thanks_heatmap.setTextFormat(Qt.TextFormat.RichText)
        thanks_heatmap.setOpenExternalLinks(True)
        thanks_heatmap.setWordWrap(True)
        v.addWidget(thanks_heatmap)

        v.addStretch(1)

    def _rerun_welcome(self):
        try:
            from .welcome import open_welcome
            open_welcome()
        except Exception as e:
            tooltip(f"Couldn't open welcome: {e}")


# ── main dialog ──────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent=None, initial_tab: str | None = None):
        super().__init__(parent)
        self._original_cfg = load_config()
        self.setWindowTitle("Ankisstant — Settings")
        self.setMinimumSize(820, 680)
        self._build(initial_tab)
        _synapse_theme(self)

    def _build(self, initial_tab: str | None = None):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tabs = QTabWidget()
        self._ai_tab      = _AITab(self._original_cfg)
        self._global_tab  = _GlobalTab(self._original_cfg)
        self._qbank_tab   = _QBankTab(self._original_cfg.get("tools", {}).get("qbank", {}))
        self._browse_tab  = _BrowseTab(self._original_cfg.get("tools", {}).get("browse", {}), self._original_cfg)
        self._kg_tab      = _KnowledgeGapsTab(
            self._original_cfg.get("tools", {}).get("knowledge_gaps", {}),
            self._original_cfg.get("tools", {}).get("gap_analyser", {}),
        )
        self._creator_tab = _CreatorTab(self._original_cfg.get("tools", {}).get("card_creator", {}), self._original_cfg)
        self._lecture_tab = _LectureTab(self._original_cfg.get("tools", {}).get("lecture", {}))
        self._synapse_tab = _SynapseTab(self._original_cfg.get("synapse", {}))
        self._about_tab   = _AboutTab()

        # Browse and Create both show a "Month tag" group editing the same
        # shared setting — keep the two copies in sync so either one reflects
        # what'll actually be saved (read back from the Create tab's copy,
        # which always exists; Browse's may be absent in native-only mode).
        if hasattr(self._browse_tab, "_month_tag"):
            _link_checkbox(self._browse_tab._month_tag, self._creator_tab._month_tag)
            _link_lineedit(self._browse_tab._month_tag_prefix, self._creator_tab._month_tag_prefix)

        tabs.addTab(_wrap_scroll(self._ai_tab),      "AI")
        tabs.addTab(_wrap_scroll(self._global_tab),  "Tools")

        # Tabs for disabled tools are hidden entirely — keeps the dialog from
        # being a wall of irrelevant settings for users who only want one or
        # two tools. The tab widgets themselves are still built (above) so
        # get_values() always has something to read on save, and a tool
        # re-enabled later picks its settings back up unchanged.
        self._tab_index_for_key: dict[str, int] = {}
        if tool_enabled("qbank"):
            self._tab_index_for_key["qbank"] = tabs.addTab(_wrap_scroll(self._qbank_tab), "AI QBank")
        if tool_enabled("browse"):
            self._tab_index_for_key["browse"] = tabs.addTab(_wrap_scroll(self._browse_tab), "AI Browse")
        if tool_enabled("knowledge_gaps"):
            self._tab_index_for_key["knowledge_gaps"] = tabs.addTab(_wrap_scroll(self._kg_tab), "Knowledge Gaps")
        if tool_enabled("card_creator"):
            self._tab_index_for_key["card_creator"] = tabs.addTab(_wrap_scroll(self._creator_tab), "AI Create")
        if tool_enabled("lecture"):
            self._tab_index_for_key["lecture"] = tabs.addTab(_wrap_scroll(self._lecture_tab), "AI Lecture")

        # Not gated on a tool being enabled — it configures the whole add-on's
        # presentation, not a tool. Hidden only when SynapsePro isn't installed,
        # where every switch on it would be a no-op with nothing to explain.
        try:
            from ..core import synapse as _synapse
            if _synapse.synapse_available():
                tabs.addTab(_wrap_scroll(self._synapse_tab), "SynapsePro")
        except Exception:
            pass

        tabs.addTab(_wrap_scroll(self._about_tab),   "About")
        root.addWidget(tabs)
        self._tabs = tabs

        if initial_tab and initial_tab in self._tab_index_for_key:
            tabs.setCurrentIndex(self._tab_index_for_key[initial_tab])

        # Keep every tool tab's model pickers in sync with the AI tab's provider.
        self._ai_tab.on_provider_changed = self._sync_tool_models
        self._sync_tool_models(self._ai_tab.current_provider())

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

    def _sync_tool_models(self, provider: str):
        manual = (provider or "").lower() == "manual"
        family = family_for(provider)
        for tab in (self._qbank_tab, self._browse_tab, self._kg_tab,
                    self._creator_tab, self._lecture_tab):
            tab.set_model_family(family, manual=manual)

    def _on_save(self):
        cfg = load_config()
        cfg.update(self._ai_tab.get_values())
        # Month tag — a single shared setting, edited from both the Browse
        # and Create tabs (kept in sync at construction; the Create tab's
        # copy always exists, even when Browse is in native-only mode).
        cfg["month_tag_enabled"] = bool(self._creator_tab._month_tag.isChecked())
        cfg["month_tag_prefix"]  = self._creator_tab._month_tag_prefix.text().strip()
        cfg.setdefault("tools", {})
        cfg["tools"]["qbank"]        = {**cfg["tools"].get("qbank", {}),        **self._qbank_tab.get_values()}
        cfg["tools"]["browse"]       = {**cfg["tools"].get("browse", {}),       **self._browse_tab.get_values()}
        cfg["tools"]["knowledge_gaps"] = {**cfg["tools"].get("knowledge_gaps", {}), **self._kg_tab.get_values()}
        cfg["tools"]["gap_analyser"]   = {**cfg["tools"].get("gap_analyser", {}),   **self._kg_tab.get_gap_analyser_values()}
        cfg["tools"]["card_creator"]   = {**cfg["tools"].get("card_creator", {}),   **self._creator_tab.get_values()}
        cfg["tools"]["lecture"]        = {**cfg["tools"].get("lecture", {}),        **self._lecture_tab.get_values()}

        # Tool on/off + Browse's panel mode — single source of truth is the
        # Tools tab's switchboard (each tool's own tab no longer has
        # its own "Enable X" checkbox).
        states = self._global_tab.get_tool_states()
        cfg["tools"]["qbank"]["enabled"]          = states["qbank"]
        cfg["tools"]["browse"]["enabled"]         = states["browse"]
        cfg["tools"]["browse"]["native_only"]     = states["browse_native_only"]
        cfg["tools"]["knowledge_gaps"]["enabled"] = states["knowledge_gaps"]
        cfg["tools"]["gap_analyser"]["enabled"]   = states["gap_analyser"]
        cfg["tools"]["card_creator"]["enabled"]   = states["card_creator"]
        cfg["tools"].setdefault("update_by_tag", {})
        cfg["tools"]["update_by_tag"]["enabled"]  = states["update_by_tag"]
        cfg["tools"].setdefault("lecture", {})
        cfg["tools"]["lecture"]["enabled"]        = states["lecture"]

        # Top-level, not under cfg["tools"] — it's bridge configuration, and it
        # must not show up in the Tools on/off switchboard. Merged rather than
        # replaced so dock_width (set by dragging the panel, not by this tab)
        # survives a save.
        cfg["synapse"] = {**cfg.get("synapse", {}), **self._synapse_tab.get_values()}

        save_config(cfg)
        # Re-register the capture shortcut so a changed binding works without
        # an Anki restart.
        try:
            from .. import _setup_capture_shortcut
            _setup_capture_shortcut()
        except Exception:
            pass
        # Add or drop the SynapsePro strip buttons (and the toolbar link with
        # them) straight away, rather than making the user restart to see a
        # switch they just flipped take effect.
        try:
            from ..core import synapse_sidebar
            synapse_sidebar.apply_settings_change()
        except Exception:
            pass
        tooltip("Settings saved.")
        self.accept()


def open_settings(initial_tab: str | None = None) -> None:
    """Open Settings, optionally jumping straight to a tool's tab —
    e.g. open_settings("browse") for the AI Browse tab."""
    dlg = SettingsDialog(initial_tab=initial_tab)
    dlg.exec()
