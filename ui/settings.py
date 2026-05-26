# Unified settings dialog for Ankisstant.
# Tabs: Global, QBank, Browse, Create with Claude.
# Global section owns the shared API key, CLI path and provider mode.
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
    DEFAULTS, PROVIDER_MODELS, active_family, family_for, load_config, save_config,
)


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
        layout.addRow("Default model:", self._default_model)

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
                    "Gemini's free tier, make sure the model is Gemini 2.5 Flash "
                    "(2.0 Flash and 2.5 Pro have no free quota)."
                )

        mw.taskman.run_in_background(work, done)

    def get_values(self) -> dict:
        self._capture_key()
        return {
            "provider":              self.current_provider(),
            "anthropic_api_key":     self._keys.get("anthropic", ""),
            "gemini_api_key":        self._keys.get("gemini", ""),
            "openai_api_key":        self._keys.get("openai", ""),
            "claude_cli_path":       self._cli_path.text().strip(),
            "claude_cli_extra_args": [t for t in self._cli_extra.text().strip().split() if t],
            "ollama_url":            self._ollama_url.text().strip() or "http://localhost:11434",
            "model_defaults":        self._default_model.values(),
        }


class _GlobalTab(QWidget):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        intro = QLabel(
            "General options. AI provider, keys and models now live on the "
            "<b>AI</b> tab."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setStyleSheet("color: gray;")
        layout.addRow(intro)

        # ── Month tag — temporality for created / unsuspended cards ──────────
        self._month_tag = QCheckBox("Add a month tag to created & unsuspended cards")
        self._month_tag.setChecked(bool(cfg.get("month_tag_enabled", True)))
        self._month_tag.setToolTip(
            "When on, every card you create (Create) or unsuspend/tag (Browse) "
            "also gets a tag <prefix>::<YYYY-MM> for the current month."
        )
        layout.addRow("Month tag:", self._month_tag)

        self._month_tag_prefix = QLineEdit(
            str(cfg.get("month_tag_prefix", "Ankisstant::Month") or "")
        )
        self._month_tag_prefix.setMinimumWidth(360)
        self._month_tag_prefix.setPlaceholderText("e.g. Ankisstant::Month")
        self._month_tag_prefix.setToolTip(
            "Root of the month tag. The current month (YYYY-MM) is appended, "
            "e.g. Ankisstant::Month::2026-05."
        )
        layout.addRow("Month tag prefix:", self._month_tag_prefix)

    def get_values(self) -> dict:
        return {
            "month_tag_enabled":   bool(self._month_tag.isChecked()),
            "month_tag_prefix":    self._month_tag_prefix.text().strip(),
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

        self._enabled = QCheckBox("Enable AI QBank")
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

        fam = active_family()
        self._search_model = _ModelField(
            qb_cfg.get("search_model"), DEFAULTS["tools"]["qbank"]["search_model"],
            fam, min_width=360)
        cf.addRow("Search model (fast):", self._search_model)

        self._card_model = _ModelField(
            qb_cfg.get("card_gen_model"), DEFAULTS["tools"]["qbank"]["card_gen_model"],
            fam, min_width=360)
        cf.addRow("Card-gen model:", self._card_model)
        self._model_form = cf

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
        self._search_model.set_family(family)
        self._card_model.set_family(family)
        _set_form_row_visible(self._model_form, self._search_model, not manual)
        _set_form_row_visible(self._model_form, self._card_model, not manual)

    def get_values(self) -> dict:
        return {
            "enabled":        self._enabled.isChecked(),
            "show_heatmap":   self._show_heatmap.isChecked(),
            "platforms":      list(self._platforms),
            "default_daily":  int(self._spin.value()),
            "target_periods": sorted(self._periods, key=lambda p: p.get("from", "")),
            "exam_dates":     sorted(self._exams,   key=lambda e: e.get("date", "")),
            "search_model":   self._search_model.values(),
            "card_gen_model": self._card_model.values(),
            "card_notetype":  self._notetype.text().strip(),
            "card_deck":      self._deck.text().strip(),
            "card_skill_id":  self._skill.text().strip(),
            "missed_q_field": self._field.text().strip()         or "Missed Questions",
            "tag_root":       self._tag_root.text().strip()      or "Missed_Questions",
            "image_max_width": int(self._image_max_width.value()),
            "capture_zoom_factor": round(int(self._capture_zoom.value()) / 100.0, 3),
            "capture_shortcut": self._capture_shortcut.keySequence().toString(),
        }


class _BrowseTab(QWidget):
    def __init__(self, br_cfg: dict, parent=None):
        super().__init__(parent)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(10)

        self._enabled = QCheckBox("Enable AI Browse")
        self._enabled.setChecked(bool(br_cfg.get("enabled", True)))
        layout.addRow(self._enabled)

        self._model = _ModelField(
            br_cfg.get("model"), DEFAULTS["tools"]["browse"]["model"],
            active_family(), min_width=420)
        layout.addRow("Model:", self._model)
        self._model_form = layout

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

        # Hierarchical auto-tag: the AI suggests a tag for the searched topic
        # and pre-fills the "Tag to apply" field. The scheme (base prefix +
        # type + levels) is the SHARED one configured under Knowledge Gaps —
        # there's no Browse-specific prefix. A free search is tagged as KG; a
        # search loaded from a KG uses that KG's type.
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

        self._source_tags = list(br_cfg.get("source_tags") or [])

    def set_model_family(self, family: str, manual: bool = False) -> None:
        self._model.set_family(family)
        _set_form_row_visible(self._model_form, self._model, not manual)

    def get_values(self) -> dict:
        return {
            "enabled":           self._enabled.isChecked(),
            "model":             self._model.values(),
            "last_used_tag":     self._last_tag.text().strip(),
            "max_results":       int(self._max.value()),
            "notetype_filter":   self._notetype_filter.text().strip(),
            "front_field":       self._front_field.text().strip() or "Text",
            "audit_tag":         self._audit_tag.text().strip(),
            "source_tags":       self._source_tags,
            "auto_tag":          self._auto_tag.isChecked(),
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


class _FieldEditorDialog(QDialog):
    """Modal for editing a single field spec inside a type's schema."""

    def __init__(self, parent=None, existing: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit field" if existing else "Add field")
        self.setMinimumWidth(440)
        layout = _expand_form(QFormLayout(self))
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setVerticalSpacing(8)

        self._existing_key = (existing or {}).get("key", "")

        self._label = QLineEdit(str((existing or {}).get("label", "")))
        self._label.setPlaceholderText("e.g. Question stem, Concept missed")
        layout.addRow("Label:", self._label)

        self._kind = QComboBox()
        for k, lbl in FIELD_KIND_LABELS:
            self._kind.addItem(lbl, k)
        existing_kind = (existing or {}).get("kind", "text")
        idx = self._kind.findData(existing_kind)
        if idx >= 0:
            self._kind.setCurrentIndex(idx)
        layout.addRow("Kind:", self._kind)

        self._placeholder = QLineEdit(str((existing or {}).get("placeholder", "")))
        self._placeholder.setPlaceholderText("Hint shown inside the empty input")
        layout.addRow("Placeholder:", self._placeholder)

        if existing:
            key_lbl = QLabel(f"<small>key: <code>{existing.get('key', '')}</code>"
                             f" — locked once a field has data</small>")
            key_lbl.setTextFormat(Qt.TextFormat.RichText)
            key_lbl.setStyleSheet("color: gray;")
            layout.addRow(key_lbl)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        layout.addRow(bb)

    def _on_save(self):
        if not self._label.text().strip():
            showWarning("Label is required.")
            return
        self.accept()

    def values(self) -> dict:
        label = self._label.text().strip()
        key = self._existing_key or _slugify_type_key(label)
        return {
            "key":         key,
            "label":       label,
            "kind":        self._kind.currentData() or "text",
            "placeholder": self._placeholder.text().strip(),
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

        self._enabled = QCheckBox("Enable Knowledge Gaps tab")
        self._enabled.setChecked(bool(kg_cfg.get("enabled", True)))
        form.addRow(self._enabled)

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

        self._ga_enabled = QCheckBox("Enable Analyse KG")
        self._ga_enabled.setChecked(bool(ga_cfg.get("enabled", True)))
        ab.addRow(self._ga_enabled)

        self._ga_model = _ModelField(
            ga_cfg.get("model"), DEFAULTS["tools"]["gap_analyser"]["model"],
            active_family(), min_width=420)
        ab.addRow("Model:", self._ga_model)
        self._model_form = ab

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
            "enabled":               self._enabled.isChecked(),
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
        self._ga_model.set_family(family)
        _set_form_row_visible(self._model_form, self._ga_model, not manual)

    def get_gap_analyser_values(self) -> dict:
        return {
            "enabled":         self._ga_enabled.isChecked(),
            "model":           self._ga_model.values(),
            "front_field":     self._ga_front_field.text().strip() or "Text",
            "notetype_filter": self._ga_notetype_filter.text().strip(),
            "last_used_tag":   self._ga_last_tag.text().strip(),
            "max_cards":       int(self._ga_max_cards.value()),
            "max_gaps":        int(self._ga_max_gaps.value()),
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

        self._enabled = QCheckBox("Enable AI Create")
        self._enabled.setChecked(bool(cc_cfg.get("enabled", True)))
        top_form.addRow(self._enabled)

        self._model = _ModelField(
            cc_cfg.get("model"), DEFAULTS["tools"]["card_creator"]["model"],
            active_family(), min_width=420)
        top_form.addRow("Model:", self._model)
        self._model_form = top_form

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

    def set_model_family(self, family: str, manual: bool = False) -> None:
        self._model.set_family(family)
        _set_form_row_visible(self._model_form, self._model, not manual)

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
            "model":            self._model.values(),
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
        self._ai_tab      = _AITab(self._original_cfg)
        self._global_tab  = _GlobalTab(self._original_cfg)
        self._qbank_tab   = _QBankTab(self._original_cfg.get("tools", {}).get("qbank", {}))
        self._browse_tab  = _BrowseTab(self._original_cfg.get("tools", {}).get("browse", {}))
        self._kg_tab      = _KnowledgeGapsTab(
            self._original_cfg.get("tools", {}).get("knowledge_gaps", {}),
            self._original_cfg.get("tools", {}).get("gap_analyser", {}),
        )
        self._creator_tab = _CreatorTab(self._original_cfg.get("tools", {}).get("card_creator", {}))
        self._about_tab   = _AboutTab()
        tabs.addTab(_wrap_scroll(self._ai_tab),      "AI")
        tabs.addTab(_wrap_scroll(self._global_tab),  "Global")
        tabs.addTab(_wrap_scroll(self._qbank_tab),   "AI QBank")
        tabs.addTab(_wrap_scroll(self._browse_tab),  "AI Browse")
        tabs.addTab(_wrap_scroll(self._kg_tab),      "Knowledge Gaps")
        tabs.addTab(_wrap_scroll(self._creator_tab), "AI Create")
        tabs.addTab(_wrap_scroll(self._about_tab),   "About")
        root.addWidget(tabs)

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
        for tab in (self._qbank_tab, self._browse_tab, self._kg_tab, self._creator_tab):
            tab.set_model_family(family, manual=manual)

    def _on_save(self):
        cfg = load_config()
        cfg.update(self._ai_tab.get_values())
        cfg.update(self._global_tab.get_values())
        cfg.setdefault("tools", {})
        cfg["tools"]["qbank"]        = {**cfg["tools"].get("qbank", {}),        **self._qbank_tab.get_values()}
        cfg["tools"]["browse"]       = {**cfg["tools"].get("browse", {}),       **self._browse_tab.get_values()}
        cfg["tools"]["knowledge_gaps"] = {**cfg["tools"].get("knowledge_gaps", {}), **self._kg_tab.get_values()}
        cfg["tools"]["gap_analyser"]   = {**cfg["tools"].get("gap_analyser", {}),   **self._kg_tab.get_gap_analyser_values()}
        cfg["tools"]["card_creator"]   = {**cfg["tools"].get("card_creator", {}),   **self._creator_tab.get_values()}
        save_config(cfg)
        # Re-register the capture shortcut so a changed binding works without
        # an Anki restart.
        try:
            from .. import _setup_capture_shortcut
            _setup_capture_shortcut()
        except Exception:
            pass
        tooltip("Settings saved.")
        self.accept()


def open_settings() -> None:
    dlg = SettingsDialog()
    dlg.exec()
